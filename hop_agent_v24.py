"""
汎用ロジック（デッキ非依存）

特定のデッキ構成・カードIDに依存しない、ポケモンTCG全般で通用する判断ロジックをまとめる。
- サイド枚数の計算（prize_count）
- 相手の攻撃でアクティブが倒されるかの推定（can_opponent_ko）
- サイド負けを避けるための安全リトリート判定（should_safety_retreat）

カードデータ・攻撃データのテーブル（card_table / attack_table）もここで構築する。
これらはルール（カードの性能）に基づくものでデッキタイプに依存しないため汎用側に置く。
"""

from cg.api import Pokemon, all_card_data

# カードID -> CardData のルックアップテーブル（ルール由来でデッキ非依存）
card_table = {c.cardId: c for c in all_card_data()}

# 注意: all_attack() は評価環境（検証エピソード）で問題を起こすため呼び出さない。
# 攻撃テーブルは空にし、can_opponent_ko/安全リトリートは無効化して動作する。
attack_table = {}

# サイド枚数を減らす特殊カードのID（ルール由来）
_PRIZE_REDUCING_ENERGY_ID = 12      # このエネルギーが付いていると取られるサイドが1枚減る
_PRIZE_REDUCING_TOOL_ID = 1172      # 「リーリエ」系ポケモンに付くと取られるサイドが1枚減る道具


def prize_count(pokemon: Pokemon) -> int:
    """このポケモンが倒された時に相手が取るサイドの枚数を返す。
    メガ進化ex=3枚、ex=2枚、それ以外=1枚を基本とし、サイド軽減カードの効果を差し引く。
    """
    data = card_table[pokemon.id]
    count = 3 if data.megaEx else 2 if data.ex else 1
    for card in pokemon.energyCards:
        if card.id == _PRIZE_REDUCING_ENERGY_ID:
            count -= 1
    for card in pokemon.tools:
        if card.id == _PRIZE_REDUCING_TOOL_ID and "Lillie" in data.name:
            count -= 1
    return max(0, count)


def bench_pokemon_count(my_state) -> int:
    """バトル場＋ベンチにいるポケモンの数を返す。"""
    count = len(my_state.active)
    for p in my_state.bench:
        if p is not None:
            count += 1
    return count


def estimate_attack_damage(defender_data, attacker_data, attack) -> int:
    """攻撃の基本ダメージに、防御側ポケモンの弱点・抵抗力を適用した推定ダメージを返す。
    効果テキストによる増減（条件付きダメージ等）は考慮せず、基本ダメージで近似する。
    """
    damage = attack.damage
    if damage <= 0:
        return 0
    if defender_data.weakness is not None and defender_data.weakness == attacker_data.energyType:
        damage *= 2
    elif defender_data.resistance is not None and defender_data.resistance == attacker_data.energyType:
        damage -= 30
    return damage


def can_opponent_ko(my_active: Pokemon, op_state) -> bool:
    """相手のバトル場・ベンチのポケモンの攻撃で、自分のアクティブが倒される可能性があるか判定する。
    エネルギーは枚数のみで判定（必要枚数を満たすか）し、弱点・抵抗力を考慮する。
    倒される可能性がある場合は安全側に倒れて True を返す。
    """
    if my_active is None:
        return False
    my_data = card_table.get(my_active.id)
    if my_data is None:
        return False
    my_hp = my_active.hp

    op_pokemons = []
    if op_state.active:
        op_pokemons.append(op_state.active[0])
    for p in op_state.bench:
        if p is not None:
            op_pokemons.append(p)

    for opp in op_pokemons:
        if opp is None:
            continue
        opp_data = card_table.get(opp.id)
        if opp_data is None:
            continue
        energy_count = len(opp.energies)
        for atk_id in opp_data.attacks:
            atk = attack_table.get(atk_id)
            if atk is None:
                continue
            # 必要エネルギー枚数を満たすか（種類は枚数で近似）
            if energy_count < len(atk.energies):
                continue
            if estimate_attack_damage(my_data, opp_data, atk) >= my_hp:
                return True
    return False


def safer_bench_exists(my_state, active_prize: int) -> bool:
    """ベンチに「倒された時に取られるサイドが、現在のアクティブより少ない」ポケモンがいるか。"""
    return any(p is not None and prize_count(p) < active_prize for p in my_state.bench)


def should_safety_retreat(my_state, op_state, my_prize: int, max_prize_scope: int = 3) -> bool:
    """サイド負けを避けるために逃げるべき状況かを判定する（デッキ非依存）。

    条件:
      1. アクティブが倒されると取られるサイドが、残りサイド以上＝即負け
      2. 残りサイドが少ない（max_prize_scope 以下）
      3. 相手の攻撃でアクティブが倒される可能性がある
      4. ベンチに「より取られるサイドが少ない」ポケモンがいる（逃げる意味がある）
    """
    active = my_state.active[0] if my_state.active else None
    if active is None:
        return False
    active_prize = prize_count(active)
    if active_prize < my_prize:
        return False
    if my_prize > max_prize_scope:
        return False
    if not can_opponent_ko(active, op_state):
        return False
    return safer_bench_exists(my_state, active_prize)


# ===== デッキ固有ロジック (Hop) =====

import os
from collections import defaultdict

from cg.api import AreaType, CardType, EnergyType, Observation, SelectContext, OptionType, Card, Pokemon, to_observation_class


"""
ホップ (Hop's) Deck - Agent（デッキ固有ロジック）

戦術方針:
- ホップのポケモンを並べ、カビゴン(特性+30)・ハロンタウン(+30)・こだわりハチマキ(+30)で火力を積む。
- ウッウ「きまぐれスピット」(相手サイド3〜4で120) を主力に、カビゴンのダイナミックプレス、
  ピッピexのフルムーンロンドで詰める。ボスの指令で的を呼ぶ。
- ファントム→オーロット、ウールー→バイウールー（進化時にベンチを呼ぶ特性）に進化。
- 重要: kaggle_environments は main.py の「最後の関数」をエージェントとして呼ぶため、agent は末尾に定義する。
"""

file_path = "deck.csv"
if not os.path.exists(file_path):
    file_path = "/kaggle_simulations/agent/" + file_path
with open(file_path, "r") as file:
    csv = file.read().split("\n")
my_deck = []
for i in range(60):
    my_deck.append(int(csv[i].strip()))

# --- カードID ---
Hop_Phantump = 878
Hop_Trevenant = 879
Hop_Snorlax = 304
Hop_Cramorant = 311
Hop_Wooloo = 309
Hop_Dubwool = 310
Lillie_Clefairy_ex = 272
Genesect = 142
Shaymin = 343
Poke_Pad = 1152
Hop_Bag = 1115
Ultra_Ball = 1121
Night_Stretcher = 1097
Pokegear = 1122
Switch = 1123
Secret_Box = 1092
Hop_Choice_Band = 1171
Air_Balloon = 1174
Lillie_Determination = 1227
Rocket_Petrel = 1219
Hassel = 1193
Boss_Orders = 1182
Postwick = 1255
Telepath_Energy = 19
Mist_Energy = 11

# --- 技ID ---
Cramorant_Fickle = 433
Snorlax_Press = 422
Phantump_Splash = 1266   # ボクレー Splashing Dodge: 10ダメージ＋コイン表で次の相手ターンのダメージ無効
Trevenant_Revenge = 1267
Trevenant_Corner = 1268
Dubwool_Headbutt = 432
Wooloo_Smash = 431
Clefairy_FullMoon = 371
Genesect_Magnetic = 185

PSYCHIC = EnergyType.PSYCHIC
EARLY_GAME_TURNS = 3

# シェイミ(Flower Curtain)が防げるのは「ベンチへのダメージ」。ダメージカウンターを乗せる効果
# (ドラパルトのファントムダイブ等)は防げない。相手の攻撃テキストから「相手のベンチに"ダメージ"を
# 与える(カウンターではない)」攻撃IDを抽出しておく。
BENCH_DAMAGE_ATTACKS = set()
try:
    from cg.api import all_attack as _all_attack
    for _a in _all_attack():
        _t = (_a.text or "").lower()
        if ("opponent" in _t and "bench" in _t and "damage" in _t
                and "counter" not in _t):
            BENCH_DAMAGE_ATTACKS.add(_a.attackId)
except Exception:
    BENCH_DAMAGE_ATTACKS = set()


def _is_hops(card_id: int) -> bool:
    data = card_table.get(card_id)
    return data is not None and "Hop" in data.name


class AttackPlan:
    attacker = -1
    target = -1
    attack_id = -1
    remain_hp = -1
    need_boss = False


plan = AttackPlan()
pre_turn = 0
op_prize_prev = 6          # 前ターン開始時の相手の残サイド数
hop_ko_last_turn = False   # 前の相手ターンに自分(ホップ)のポケモンが気絶したか（Revenge +100判定用）


def get_card(obs: Observation, area: AreaType, index: int, player_index: int):
    ps = obs.current.players[player_index]
    match area:
        case AreaType.DECK:
            return obs.select.deck[index]
        case AreaType.HAND:
            return ps.hand[index]
        case AreaType.DISCARD:
            return ps.discard[index]
        case AreaType.ACTIVE:
            return ps.active[index]
        case AreaType.BENCH:
            return ps.bench[index]
        case AreaType.PRIZE:
            return ps.prize[index]
        case AreaType.STADIUM:
            return obs.current.stadium[index]
        case AreaType.LOOKING:
            return obs.current.looking[index]
        case _:
            return None


def psychic_count(p: Pokemon) -> int:
    return sum(1 for e in p.energies if e == PSYCHIC)


def pokemon_score(p: Pokemon) -> int:
    data = card_table[p.id]
    score = prize_count(p) * 1000
    score += len(p.energies) * 120
    score += len(p.tools) * 80
    if p.id == Hop_Snorlax:
        score += 500   # 特性「ふとっぱら」常駐の+30が強力
    elif p.id == Lillie_Clefairy_ex:
        score += 350
    elif p.id == Hop_Cramorant:
        score += 250
    elif p.id == Hop_Trevenant:
        score += 200
    elif p.id == Hop_Dubwool:
        score += 150
    score += p.hp
    return score


def attack_feasible(p: Pokemon, attack_id: int, has_band: bool) -> bool:
    """そのポケモンがその技を撃てるか（エネルギー充足）。バンドは無色コストを1減らす。"""
    total = len(p.energies)
    need_psychic = 0
    need_total = 0
    if attack_id == Cramorant_Fickle:
        need_total = 1
    elif attack_id == Phantump_Splash:
        need_total = 1
    elif attack_id == Snorlax_Press:
        need_total = 3
    elif attack_id == Trevenant_Revenge:
        need_total = 1
    elif attack_id == Trevenant_Corner:
        need_total = 3
        need_psychic = 1
    elif attack_id == Dubwool_Headbutt:
        need_total = 3
    elif attack_id == Wooloo_Smash:
        need_total = 3
    elif attack_id == Clefairy_FullMoon:
        need_total = 2
        need_psychic = 1
    else:
        return False
    if has_band and _is_hops(p.id):
        need_total = max(need_psychic, need_total - 1)  # 無色1軽減
    return total >= need_total and psychic_count(p) >= need_psychic


def attack_damage(attacker: Pokemon, attack_id: int, target: Pokemon, target_is_active: bool,
                  op_prize: int, total_bench: int, boost: int, fairy_zone: bool = False,
                  revenge_bonus: bool = False) -> int:
    data = card_table[target.id]
    if attack_id == Cramorant_Fickle:
        base = 120 if op_prize in (3, 4) else 0
        if base == 0:
            return 0
    elif attack_id == Phantump_Splash:
        base = 10
    elif attack_id == Snorlax_Press:
        base = 140
    elif attack_id == Trevenant_Revenge:
        # Horrifying Revenge: 通常30。前の相手ターンにホップのポケモンが攻撃で気絶していたら+100。
        base = 130 if revenge_bonus else 30
    elif attack_id == Trevenant_Corner:
        base = 90
    elif attack_id == Dubwool_Headbutt:
        base = 80
    elif attack_id == Wooloo_Smash:
        base = 50
    elif attack_id == Clefairy_FullMoon:
        base = 20 + 20 * total_bench
    else:
        return 0
    dmg = base + (boost if target_is_active else 0)
    # 弱点・抵抗力（攻撃側タイプ）
    atk_type = card_table[attacker.id].energyType
    weak = data.weakness
    # ピッピexのファミリーゾーン: 相手の{N}=ドラゴンタイプの弱点を超(P)に変える
    if fairy_zone and data.energyType == EnergyType.DRAGON:
        weak = PSYCHIC
    if weak is not None and weak == atk_type:
        dmg *= 2
    elif data.resistance is not None and data.resistance == atk_type:
        dmg -= 30
    return dmg


ATTACKS_OF = {
    Hop_Phantump: [Phantump_Splash],
    Hop_Cramorant: [Cramorant_Fickle],
    Hop_Snorlax: [Snorlax_Press],
    Hop_Trevenant: [Trevenant_Corner, Trevenant_Revenge],
    Hop_Dubwool: [Dubwool_Headbutt],
    Hop_Wooloo: [Wooloo_Smash],
    Lillie_Clefairy_ex: [Clefairy_FullMoon],
}


def _agent_impl(obs_dict: dict) -> list[int]:
    obs = to_observation_class(obs_dict)
    if obs.select is None:
        return my_deck

    state = obs.current
    select = obs.select
    context = select.context
    my_index = state.yourIndex
    my_state = state.players[my_index]
    op_state = state.players[1 - my_index]
    my_prize = len(my_state.prize)
    op_prize = len(op_state.prize)

    is_early_game = state.turn <= EARLY_GAME_TURNS
    field_pokemon_count = bench_pokemon_count(my_state)
    bench_is_full = field_pokemon_count >= 6

    global plan, pre_turn, op_prize_prev, hop_ko_last_turn
    if pre_turn != state.turn:
        pre_turn = state.turn
        plan = AttackPlan()
        # 相手の残サイドが前ターンより減っていたら、相手は前ターンにサイドを取った
        # ＝こちらのポケモンが攻撃で気絶した可能性が高い → オーロットRevengeが+100になる。
        if op_prize > 0:
            hop_ko_last_turn = (op_prize < op_prize_prev)
            op_prize_prev = op_prize
        else:
            hop_ko_last_turn = False

    field_counts = defaultdict(int)
    hand_counts = defaultdict(int)
    discard_counts = defaultdict(int)
    for c in my_state.active + my_state.bench:
        if c is not None:
            field_counts[c.id] += 1
    for c in my_state.hand:
        hand_counts[c.id] += 1
    for c in my_state.discard:
        discard_counts[c.id] += 1

    stadium_id = 0
    for c in state.stadium:
        stadium_id = c.id

    snorlax_in_play = field_counts[Hop_Snorlax] >= 1
    postwick_active = stadium_id == Postwick
    total_bench = 0
    for ps in state.players:
        for b in ps.bench:
            if b is not None:
                total_bench += 1

    safety_retreat = should_safety_retreat(my_state, op_state, my_prize)

    # ウッウ「きまぐれスピット」(相手サイド3〜4で120, ブースト込みで大火力/1エネ)の活用。
    # 相手サイドが3〜4枚で、ベンチに撃てるウッウ(エネ1以上 or こだわりハチマキ付)がいて、
    # アクティブがウッウでないなら、前に出して撃ちたい。
    active_pk = my_state.active[0] if my_state.active else None
    cram_ready_bench = any(
        p is not None and p.id == Hop_Cramorant
        and (len(p.energies) >= 1 or any(t.id == Hop_Choice_Band for t in p.tools))
        for p in my_state.bench
    )
    want_cramorant = (op_prize in (3, 4) and cram_ready_bench
                      and (active_pk is None or active_pk.id != Hop_Cramorant))

    # ピッピexのファミリーゾーン: 自分の場にピッピexがいると、相手のドラゴンの弱点が超になる。
    fairy_zone = field_counts[Lillie_Clefairy_ex] >= 1
    op_has_dragon = any(
        p is not None and card_table[p.id].energyType == EnergyType.DRAGON
        for p in ([op_state.active[0] if op_state.active else None] + list(op_state.bench))
    )
    # 相手が悪タイプ（オーロット/ボクレー＝超は悪弱点なので一方的に狩られる）の時は、
    # 無色アタッカーのカビゴン(HP150/ダイナミックプレス140・弱点闘)を解禁してアタッカーにする。
    # 悪に弱点を突かれない貴重な打点で、超の主力が機能しない対面の主軸になる。
    op_has_dark = any(
        p is not None and card_table[p.id].energyType == EnergyType.DARKNESS
        for p in ([op_state.active[0] if op_state.active else None] + list(op_state.bench))
    )

    # ゲノセクトの特性ACE Nullifierは相手のACE SPEC使用を封じるもの。
    # 相手が既にACE SPECを使った(捨て札にある/場に道具として貼られている)後は、出す必要がない。
    op_used_ace = any(getattr(card_table.get(c.id), "aceSpec", False) for c in op_state.discard)
    if not op_used_ace:
        for p in ([op_state.active[0] if op_state.active else None] + list(op_state.bench)):
            if p is not None and any(getattr(card_table.get(t.id), "aceSpec", False) for t in p.tools):
                op_used_ace = True
                break

    # シェイミ(Flower Curtain)はベンチへの「ダメージ」を防ぐ（ダメージカウンターは防げない）。
    # 相手がベンチに"ダメージ"を与える攻撃(水のオーガポンex/まりぃのオーロンゲex等)を持つ時のみ価値が高い。
    # ドラパルト(カウンター)では効かないので、その場合は温存対象。
    op_does_bench_damage = any(
        p is not None and any(aid in BENCH_DAMAGE_ATTACKS for aid in card_table[p.id].attacks)
        for p in ([op_state.active[0] if op_state.active else None] + list(op_state.bench))
    )
    # 手札にポケモンが2枚以上あり、ドロー(リーリエの決意等)も無く、相手がベンチダメージデッキでないなら、
    # シェイミはベンチに出さず手札に温存する。
    hand_pokemon = sum(1 for c in my_state.hand if card_table[c.id].cardType == CardType.POKEMON)
    # リーリエの決意は手札を山札に戻すので、手札のシェイミは戻ってしまう＝ある時は先に出してよい
    has_dig = hand_counts[Lillie_Determination] >= 1
    avoid_shaymin = (hand_pokemon >= 2 and not has_dig and not op_does_bench_damage)

    # シェイミ/ゲノセクトは使い所が限定的。使わない局面ではハイパーボール等のコストで優先的に捨てる。
    shaymin_useful = op_does_bench_damage          # ベンチダメージデッキ相手の時だけ価値あり
    genesect_useful = not op_used_ace              # 相手がACE SPECを使う前だけ価値あり

    # テレパス超エネルギーは「超ポケモンに手張りすると基本超ポケモンを2体ベンチに出す」効果。
    # ボクレー(基本超)はテレパスで自動的にベンチに出せるので、手札がある時は
    # サーチ/手動ベンチ展開はボクレー以外（テレパスで持ってこれないカビゴン/ウッウ等）を優先する。
    has_telepath = hand_counts[Telepath_Energy] >= 1

    # リーリエのピッピexは「ベンチに置くだけ」でファミリーゾーン（相手ドラゴンの弱点を超に）が発動する。
    # そのためピッピは基本ベンチ要員。攻撃（フルムーンロンド）するのは「このKOで勝てる」本当のフィニッシュ時のみ。
    want_clefairy = False
    clef_bench = next((p for p in my_state.bench
                       if p is not None and p.id == Lillie_Clefairy_ex
                       and len(p.energies) >= 2 and any(e == PSYCHIC for e in p.energies)), None)
    if clef_bench is not None and (active_pk is None or active_pk.id != Lillie_Clefairy_ex):
        _oa = op_state.active[0] if op_state.active else None
        if _oa is not None:
            _dmg = attack_damage(clef_bench, Clefairy_FullMoon, _oa, True, op_prize, total_bench, 0, fairy_zone)
            # 後半（相手サイド3以下）に、ピッピでアクティブをKOできる時だけ前に出してフィニッシュする。
            if _oa.hp <= _dmg and op_prize <= 3:
                want_clefairy = True

    # テレパス検索で「ピッピ(2サイドの的)」を1体だけ取るべきか判断する条件:
    # ピッピを場に出す(ファミリーゾーン発動=相手ドラゴンの弱点が超)と、こちらのアクティブの
    # 攻撃で相手のドラゴンアクティブを"一発KO"できるなら、1枠でもピッピを優先する。
    fairy_enables_ohko = False
    if op_has_dragon and not fairy_zone and active_pk is not None:
        _oa2 = op_state.active[0] if op_state.active else None
        if _oa2 is not None and card_table[_oa2.id].energyType == EnergyType.DRAGON:
            _band = any(t.id == Hop_Choice_Band for t in active_pk.tools)
            _boost = 0
            if _is_hops(active_pk.id):
                if snorlax_in_play:
                    _boost += 30
                if postwick_active:
                    _boost += 30
                if _band:
                    _boost += 30
            for _atk in ATTACKS_OF.get(active_pk.id, []):
                if attack_feasible(active_pk, _atk, _band):
                    _d = attack_damage(active_pk, _atk, _oa2, True, op_prize, total_bench,
                                       _boost, True, hop_ko_last_turn)
                    if _oa2.hp <= _d:
                        fairy_enables_ohko = True
                        break

    # --- 攻撃プラン（アクティブで殴る前提＋ボスで的を呼ぶ） ---
    can_attack = False
    can_use_boss = False
    if context == SelectContext.MAIN:
        for o in select.option:
            if o.type == OptionType.ATTACK:
                can_attack = True
            elif o.type == OptionType.PLAY:
                c = get_card(obs, AreaType.HAND, o.index, my_index)
                if c is not None and c.id == Boss_Orders:
                    can_use_boss = True

        my_active = my_state.active[0] if my_state.active else None
        if state.turn >= 2 and my_active is not None and can_attack:
            op_cards = []
            if op_state.active:
                op_cards.append(op_state.active[0])
            for p in op_state.bench:
                op_cards.append(p)
            has_band = any(t.id == Hop_Choice_Band for t in my_active.tools)
            boost = 0
            if _is_hops(my_active.id):
                if snorlax_in_play:
                    boost += 30
                if postwick_active:
                    boost += 30
                if has_band:
                    boost += 30
            best = -1
            for atk in ATTACKS_OF.get(my_active.id, []):
                if not attack_feasible(my_active, atk, has_band):
                    continue
                for j, opp in enumerate(op_cards):
                    if opp is None:
                        continue
                    is_active = (j == 0)
                    if not is_active and not can_use_boss:
                        continue
                    dmg = attack_damage(my_active, atk, opp, is_active, op_prize, total_bench, boost,
                                        fairy_zone, hop_ko_last_turn)
                    if dmg <= 0:
                        continue
                    score = pokemon_score(opp)
                    if opp.hp <= dmg:
                        score += 2000 + prize_count(opp) * 800
                        if prize_count(opp) >= op_prize:
                            score = 100000
                    else:
                        # 倒しきれない「チップ」目的では Revenge+100 気絶ボーナスを当てにしない。
                        # （ドラパルトex等の高HP相手に、非確定の大ダメージを見込んで主力オーロットを
                        #   前に出し、Phantom Diveで返り討ち→サイドを与えるテンポ負けを防ぐ。
                        #   確定KOできる時のRevenge130は上のKO判定で正しく活きる。）
                        dmg_chip = attack_damage(my_active, atk, opp, is_active, op_prize,
                                                 total_bench, boost, fairy_zone, False)
                        score *= dmg_chip / max(1, opp.hp)
                    if is_active:
                        score += 200
                    # ダイナミックプレスは自傷80。KOできない時は控える。
                    # さらに自傷で自分のカビゴン（常駐+30）が気絶する場合は、勝ち確KO以外は強く避ける。
                    if atk == Snorlax_Press:
                        if opp.hp > dmg:
                            score -= 150
                        if my_active.hp <= 80 and not (opp.hp <= dmg and prize_count(opp) >= op_prize):
                            score -= 1200
                    if score > best:
                        best = score
                        plan.attacker = 0
                        plan.target = j
                        plan.attack_id = atk
                        plan.remain_hp = opp.hp - dmg
                        plan.need_boss = (not is_active)

    def energy_score(p: Pokemon, active: bool) -> int:
        # エネルギー付与優先順位:
        #   1.オーロット(3エネ目標=コーナー起動) 2.ウッウ(1エネ、サイド3/4圏内のみ)
        #   3.ピッピ(終盤フィニッシュ時) 4.カビゴン(悪相手のみ) 5.ウールー/バイウールー
        #
        # ★スタック優先: オーロットはCorner(3エネ90点+補正)が主力。
        #   1エネ横展開では Revenge 発動なしに30点しか出せず、相手への脅威が皆無になる。
        #   3エネ貯まればCorner+ポストウィック+カビゴン+ハチマキ=180点(フェアリーゾーン下でx2=360点)。
        #   → オーロット1体に3エネ貯まるまで優先。複数オーロットも同様に各3エネを目指す。
        nene = len(p.energies)
        score = 8000
        if active:
            score += 30
        if p.id == Hop_Trevenant:
            # 3エネ(Corner起動)まで積極的に付ける。3エネ超は他に回す。
            if nene == 0:
                score += 400   # 最優先: 1エネ目(Revenge起動+Corner準備)
            elif nene == 1:
                score += 200   # 2エネ目もTrevenantへ
            elif nene == 2:
                score += 80    # 3エネ目(Corner起動=最低限の火力を確保)
            else:
                score -= 600   # 3エネ以上は不要、他に回す
        elif p.id == Hop_Phantump:
            # アクティブのボクレー(殴り要員)のみ1エネ優先。ベンチは進化要員で不要。
            if active:
                score += 150 if nene == 0 else 5
            else:
                score += 20 if nene == 0 else -50
        elif p.id == Hop_Cramorant:
            # サイド3/4圏内のみ。圏外は強くゲート（8000-3000=5000: Trevenant 3エネ未満より低い）。
            if op_prize in (3, 4):
                score += 130 if nene == 0 else 5          # 1エネあればFickle起動。横展開OK
            else:
                score -= 3000   # 圏外では絶対に付けない（v23の-200から大幅強化）
        elif p.id == Lillie_Clefairy_ex:
            # 終盤フィニッシュ時は最優先でフルムーンロンド分を集める。準備は1エネまで。
            if want_clefairy or (op_prize <= 3 and op_has_dragon):
                score += 150
            elif len(p.energies) >= 1:
                score -= 2000
            else:
                score += 110                              # 準備として1個まで
        elif p.id == Hop_Snorlax:
            # 悪相手のみアタッカー解禁。それ以外はベンチ常駐要員でエネ不要。
            score += 90 if op_has_dark else -200
        elif p.id == Hop_Wooloo:
            score += 70
        elif p.id == Hop_Dubwool:
            score += 80
        elif p.id == Genesect:
            score -= 300  # 鋼エネが無く攻撃不可
        else:
            score -= 30
        return score

    scores = []
    for o in select.option:
        score = 0
        if o.type == OptionType.NUMBER:
            score = o.number if o.number is not None else 0
        elif o.type == OptionType.YES:
            score = 1
        elif o.type == OptionType.CARD:
            card = get_card(obs, o.area, o.index, o.playerIndex)
            if card is not None:
                if context == SelectContext.SWITCH or context == SelectContext.TO_ACTIVE:
                    if o.playerIndex == my_index and isinstance(card, Pokemon):
                        score += len(card.energies) * 2
                        if my_prize <= 3:
                            if prize_count(card) == 1:
                                score += 60
                            elif prize_count(card) >= my_prize:
                                score -= 200
                        if card.id == Hop_Trevenant:
                            score += 40
                        # カビゴンは特性ふとっぱら(常駐+30)要員。にげ4＋自傷80のためバトル場には出さず
                        # ベンチに置く（昇格は避ける）。ただし相手が悪デッキの時は無色アタッカーとして
                        # 前に出して殴る（超の主力が弱点で機能しないため）。
                        if card.id == Hop_Snorlax:
                            score += 60 if op_has_dark else -80
                        # ウッウ「きまぐれスピット」は相手サイド3/4でしか撃てない。
                        # サイド3/4なら前に出して撃つ(+300)が、それ以外では撃てないので
                        # バトル場に出さない(-150)。アクティブがやられた後の昇格でも
                        # ウッウを選ばず、オーロット等のアタッカーを優先させる。
                        if card.id == Hop_Cramorant:
                            score += 300 if want_cramorant else -150
                        # ピッピで相手アクティブをKOできるなら前に出してフルムーンロンド
                        if want_clefairy and card.id == Lillie_Clefairy_ex:
                            score += 400
                        # それ以外ではピッピexをバトル場に出さない（2サイドの的になる）。
                        # 昇格(TO_ACTIVE)でピッピが選ばれるのを防ぎ、他のアタッカーを優先。
                        elif card.id == Lillie_Clefairy_ex:
                            score -= 500
                        if o.index == plan.attacker - 1:
                            score += 100
                    elif o.playerIndex != my_index:
                        if o.index == plan.target - 1:
                            score += 100
                elif context == SelectContext.SETUP_ACTIVE_POKEMON:
                    # スタート時、手札にボクレー（ファントム）があれば最優先でバトル場に出す。
                    # （壁として固く、オーロットへ進化して主軸になる）
                    if card.id == Hop_Phantump:
                        score = 10
                    elif card.id == Hop_Wooloo:
                        score = 6   # ダブウールへ進化するアタッカー。ボクレー不在時の第一候補
                    elif card.id == Hop_Cramorant:
                        # ウッウは「相手サイド3/4でしか撃てない」situationalアタッカー。
                        # スタート(サイド6)では攻撃できずベンチに置きたいので低優先。
                        # ただし他に出せるたねが無ければ110HPの壁として出る。
                        score = 3
                    elif card.id == Hop_Snorlax:
                        # カビゴンはベンチで特性を使いたい。最初のバトル場には極力出さない
                        score = 2
                    elif card.id in (Lillie_Clefairy_ex, Genesect, Shaymin):
                        # ピッピ(2サイドの的)/ゲノセクト(攻撃不可)/シェイミ(ベンチ要員)は
                        # スタートのバトル場に出さない（最後の手段でのみ）。
                        score = 1
                    else:
                        score = 4
                elif context == SelectContext.TO_HAND:
                    score = to_hand_score(card.id, field_counts, hand_counts, has_telepath,
                                          len(my_state.hand), op_has_dragon)
                elif context == SelectContext.ATTACH_FROM:
                    if isinstance(card, Pokemon):
                        score = energy_score(card, o.area == AreaType.ACTIVE)
                elif context in (SelectContext.DISCARD, SelectContext.TO_DECK, SelectContext.TO_DECK_BOTTOM):
                    score = discard_priority(card.id, field_counts, hand_counts,
                                             shaymin_useful, genesect_useful)
                else:
                    if isinstance(card, Pokemon):
                        score = pokemon_score(card)
                        # ボクレーはデッキのエンジン＝テレパスの貼り先＋オーロットの進化元。
                        # ベンチ展開/サーチでは常に最優先で場に出す（テレパスを使うにも場のボクレーが要る）。
                        if card.id == Hop_Phantump:
                            score += 700
                        # ホップのバッグ等のベンチ配置/サーチでも重複制限を効かせる
                        # （カビゴンは2体目を場に出さない 等）
                        if card.id in (Hop_Snorlax, Lillie_Clefairy_ex, Hop_Cramorant, Shaymin, Genesect) \
                                and field_counts[card.id] >= 1:
                            score = -100
                        # ピッピex(2サイドの的)のベンチ展開:
                        #  ・非ドラゴン相手: ファミリーゾーン無意味＆2サイドの置物なので出さない(-100)。
                        #  ・ドラゴン相手: ファミリーゾーンが有効だが、テレパス検索でボクレーより先に
                        #    取られないようボクレー(~1770)の次に置く(1600)。2枠なら「ボクレー1+ピッピ1」、
                        #    1枠ならボクレー優先。ただしファミリーゾーンで相手を一発KOできる時は最優先(3000)。
                        if card.id == Lillie_Clefairy_ex:
                            if not op_has_dragon:
                                score = -100
                            elif fairy_enables_ohko:
                                score = 3000
                            else:
                                score = 1600
                        # シェイミは温存条件ならベンチ展開しない（初期ベンチ配置含む）
                        if card.id == Shaymin and avoid_shaymin:
                            score = -100
                        # ゲノセクトは相手がACE SPECを使った後はベンチ展開しない
                        if card.id == Genesect and op_used_ace:
                            score = -100
                    else:
                        score = 100
        elif o.type == OptionType.PLAY:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            data = card_table[card.id]
            if data.cardType == CardType.POKEMON:
                score = 20000
                if is_early_game:
                    score += 5000
                benched = sum(1 for p in my_state.bench if p is not None)
                if card.id == Hop_Phantump:
                    # オーロット（最優先アタッカー）の種火。まずボクレーを2体ベンチに並べる。
                    # 2体並ぶまではオーロット進化(EVOLVEスコア≒9400)より優先。
                    # 2体並んだら、3体目を出すよりオーロットへの進化を優先する（=低めに抑える）。
                    fphan = field_counts[Hop_Phantump]
                    if fphan < 2:
                        score += 8000   # 2体目まで最優先（進化より上）
                    else:
                        score = 8000    # 3体目以降は進化(9000+)より低く
                elif card.id == Hop_Cramorant:
                    # ウッウ(きまぐれスピット)は相手サイド3〜4で撃てる。撃てる圏が近づくまで温存する。
                    # 自分のサイドが5枚以下になったらベンチ展開を優先。ベンチが2体以下なら盤面確保で即出す。
                    if field_counts[Hop_Cramorant] >= 1:
                        score = -1  # 2体目は出さない
                    elif benched <= 2:
                        pass        # 盤面が薄い→通常通り出す
                    elif my_prize <= 5:
                        pass        # サイドが減った→攻撃圏が近いので優先して出す
                    else:
                        score = 100  # まだ温存（手札に留める）
                elif card.id == Lillie_Clefairy_ex:
                    if field_counts[Lillie_Clefairy_ex] >= 1 or not op_has_dragon:
                        # ファミリーゾーンはドラゴンの弱点を超にする特性。ドラゴン相手以外では
                        # 効果ゼロで、2サイドのピッピexを置くと倒されて2枚取られる置物になる。
                        # → ドラゴン相手(op_has_dragon)以外はベンチに出さない。
                        score = -1
                    else:
                        # 相手がドラゴン（ドラパルト等）なら、ファミリーゾーン起動のため最優先で展開
                        score = 26000
                elif card.id == Shaymin:
                    # 既に場にいる、または温存条件ならベンチに出さない
                    if field_counts[Shaymin] >= 1 or avoid_shaymin:
                        score = -1
                elif card.id == Genesect:
                    # ACE Nullifier起動が目的。既に場にいる、または相手がACE SPECを使った後は出さない。
                    if field_counts[Genesect] >= 1 or op_used_ace:
                        score = -1
                elif card.id == Hop_Snorlax:
                    if field_counts[Hop_Snorlax] >= 1:
                        score = -1
            else:
                score = play_trainer_score(card.id, my_state, op_state, my_prize, is_early_game,
                                           bench_is_full, field_counts, hand_counts, stadium_id, can_attack,
                                           want_cramorant or want_clefairy)
        elif o.type == OptionType.ATTACH:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            if card.id == Hop_Choice_Band:
                # ハチマキは「ホップのポケモン」の打点に有効。シェイミ/ピッピexには付けない。
                # ただしゲノセクトには特性ACE Nullifier起動のため許容（攻撃役より低優先＝余り1枚を回す）。
                if pokemon.id == Genesect and not pokemon.tools:
                    score = 6000
                elif not _is_hops(pokemon.id):
                    score = -1
                else:
                    score = 7000
                    if pokemon.id in (Hop_Cramorant, Hop_Snorlax, Hop_Trevenant, Hop_Dubwool):
                        score += 200
            elif card.id == Air_Balloon:
                # ふうせんはゲノセクトに付けて特性ACE Nullifierを起動するのを最優先
                # （相手のACE SPEC＝Unfair Stamp/Maximum Belt/Hero's Cape等の使用を封じる）。
                if pokemon.id == Genesect and not pokemon.tools:
                    score = 9000
                else:
                    score = 2000
            elif card.id in (Telepath_Energy, Mist_Energy):
                is_active_target = (o.inPlayArea == AreaType.ACTIVE)
                score = energy_score(pokemon, is_active_target)
                if plan.attacker == 0 and is_active_target:
                    score += 150
                # ルール1: バトル場のボクレー/オーロットがエネ未装着なら最優先で付ける
                if is_active_target and pokemon.id in (Hop_Phantump, Hop_Trevenant) \
                        and len(pokemon.energies) == 0:
                    score += 500
                # ルール2: ミスト/テレパス超が両方手札にある時の選び分け。
                #   ベンチ展開あり→ミストエネルギー、ベンチ展開なし→テレパス超エネルギー
                if hand_counts[Mist_Energy] >= 1 and hand_counts[Telepath_Energy] >= 1:
                    bench_developed = any(p is not None for p in my_state.bench)
                    if bench_developed and card.id == Mist_Energy:
                        score += 80
                    elif (not bench_developed) and card.id == Telepath_Energy:
                        score += 80
            else:
                score = 3000
        elif o.type == OptionType.EVOLVE:
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            target_card = get_card(obs, AreaType.HAND, o.index, my_index) if o.index is not None else None
            score = 9000 + len(pokemon.energies) * 10
            tid = target_card.id if target_card is not None else None
            if tid == Hop_Trevenant:
                score += 400   # オーロットは最優先アタッカー。進化を最優先する
            elif tid == Hop_Dubwool:
                score += 300   # 進化時にベンチを呼べる（Defiant Horn）
        elif o.type == OptionType.ABILITY:
            card = get_card(obs, o.area, o.index, my_index)
            score = 8000
            if card.id == 1267:  # スタジアム特性等
                score = 1
        elif o.type == OptionType.RETREAT:
            if safety_retreat:
                score = 9500
            elif want_cramorant or want_clefairy:
                score = 5000   # ウッウ/ピッピを前に出して撃つ
            elif plan.attacker >= 1:
                score = 2000
            else:
                score = -1
        elif o.type == OptionType.ATTACK:
            if o.attackId == plan.attack_id and plan.attacker == 0:
                # KO可能なら最優先。KO不可でも相手を追い詰める攻撃は積極的に行う。
                if plan.remain_hp <= 0:
                    score = 4000   # KO確定: 最優先（v23の2500から引き上げ）
                elif plan.remain_hp < 50:
                    score = 2500   # あと1-2発でKO: 攻撃継続優先
                else:
                    score = 1800   # チップダメージ（v23の1500から微増）
            else:
                score = 800  # プラン外の攻撃（v23の600から微増）
        scores.append(score)

    desc = [i for i, _ in sorted(enumerate(scores), key=lambda x: x[1], reverse=True)]

    # 1回の選択で複数枚取る場合（例: ホップのバッグで2体ベンチ）の重複制限。
    # 山札からの配置/サーチで、上限のあるポケモンを場の数＋選択数が上限を超えて取らない。
    caps = {Hop_Snorlax: 1, Hop_Cramorant: 1, Lillie_Clefairy_ex: 1, Shaymin: 1, Genesect: 1}
    maxc = select.maxCount
    running = defaultdict(int)
    chosen = []
    for i in desc:
        if len(chosen) >= maxc:
            break
        # 負スコア＝「取らない方が良い」ペナルティ（ドラゴン相手以外のピッピ、温存シェイミ、
        # 2体目カビゴン等）。maxCountに空きがあっても拾わない。minCountで法的に必要な場合のみ
        # 後段の補充で取る。
        if scores[i] < 0:
            continue
        o = select.option[i]
        # 手札/山札から新たに場へ加える選択にのみキャップを掛ける（場の入れ替え=BENCH/ACTIVEは除外）
        if getattr(o, "area", None) in (AreaType.DECK, AreaType.HAND):
            c = get_card(obs, o.area, o.index, o.playerIndex if o.playerIndex is not None else my_index)
            cid = c.id if c is not None else None
            if cid in caps:
                if field_counts.get(cid, 0) + running[cid] >= caps[cid]:
                    continue
                running[cid] += 1
        chosen.append(i)
    # minCountを満たせない場合は残りから補充（合法性優先）
    if len(chosen) < select.minCount:
        for i in desc:
            if i not in chosen:
                chosen.append(i)
                if len(chosen) >= select.minCount:
                    break
    return chosen[:maxc]


def to_hand_score(card_id: int, field_counts, hand_counts, has_telepath=False, hand_size=99,
                  op_has_dragon=False) -> int:
    """サーチで手札に加える優先度。hand_size=サーチ時点の手札枚数（ペトレル等の搬入先選択用）。"""
    # ペトレル等でトレーナーズをサーチする時の優先度。
    # シークレットボックス(ACE SPEC, トレーナーズ4種を一括サーチ)が普段の最優先。
    # ただし使用には手札を3枚トラッシュする必要があり、手札が少ないと使えない。
    # 手札が少ない時は、次のターンの手札を増やすためリーリエの決意（引き直し）を最優先で持ってくる。
    if card_id == Secret_Box:
        # サーチ後の手札=hand_size+1。3枚トラッシュして使うには手札が3枚以上必要。
        return 400 if hand_size >= 3 else -50
    if card_id == Lillie_Determination:
        return 380 if hand_size <= 3 else 90
    if card_id == Hop_Phantump:
        # ボクレーはデッキのエンジン＝テレパスの貼り先＋オーロットの進化元。常に最優先でサーチする。
        # （テレパスは「場のボクレーに貼って基本超2体を追加ベンチ」する手段なので、まずボクレーを
        #  手札→場に出すことが前提。テレパス保持を理由にボクレーを後回しにしてはいけない。）
        return 320
    if card_id == Hop_Snorlax:
        return -100 if field_counts[Hop_Snorlax] >= 1 else 300  # 1体目は最優先、2体目は取らない
    if card_id == Hop_Cramorant:
        return -100 if field_counts[Hop_Cramorant] >= 1 else 260
    if card_id == Lillie_Clefairy_ex:
        # ファミリーゾーンはドラゴン相手専用。to_hand_scoreはop_has_dragonを持たないため
        # 控えめ評価に留め、ドラゴン相手のベンチ展開判断はPLAY/CARD側のop_has_dragonで行う。
        # （ドラゴン相手にピッピを"積極サーチ"するとアタッカー展開のテンポを損ない逆効果＝v20で実証）
        return 60 if field_counts[Lillie_Clefairy_ex] == 0 else 20
    if card_id in (Hop_Trevenant, Hop_Wooloo, Hop_Dubwool):
        return 150
    if card_id == Hop_Choice_Band:
        return 140
    if card_id == Boss_Orders:
        return 130
    if card_id in (Telepath_Energy, Mist_Energy):
        return 120 - hand_counts[card_id] * 20
    if card_id == Postwick:
        return 110
    return 80 - hand_counts[card_id] * 10


def discard_priority(card_id: int, field_counts, hand_counts,
                     shaymin_useful=True, genesect_useful=True) -> int:
    """捨てる/戻す優先度（大きいほど先に手放す）。"""
    # 使い所が無いシェイミ/ゲノセクトはハイパーボール等のコストで優先的にトラッシュする
    if card_id == Shaymin and not shaymin_useful:
        return 400
    if card_id == Genesect and not genesect_useful:
        return 400
    if card_id in (Hop_Snorlax, Hop_Cramorant, Boss_Orders, Hop_Choice_Band, Lillie_Clefairy_ex):
        return -50
    if card_id in (Telepath_Energy, Mist_Energy):
        return 250 if hand_counts[card_id] >= 3 else 60
    if field_counts[card_id] >= 1 or hand_counts[card_id] >= 2:
        return 200
    return 100


def play_trainer_score(card_id, my_state, op_state, my_prize, is_early_game,
                       bench_is_full, field_counts, hand_counts, stadium_id, can_attack,
                       want_cramorant=False) -> int:
    have_snorlax = field_counts[Hop_Snorlax] >= 1
    if card_id == Hop_Bag:
        # 基本ホップを2体ベンチ展開。序盤最優先
        if not bench_is_full:
            return 16000 if is_early_game else 9000
        return -1
    if card_id == Poke_Pad:
        # ルールボックス無しポケモンをサーチ（カビゴン/ウッウ等）
        if not have_snorlax:
            return 12000
        return 8000
    if card_id == Ultra_Ball:
        return 9000
    if card_id == Pokegear:
        return 3000  # サポートを探す
    if card_id == Rocket_Petrel:
        return 3100  # 任意のトレーナーをサーチ（サポート）
    if card_id == Lillie_Determination:
        draw_amt = 8 if my_prize == 6 else 6
        if my_state.deckCount + len(my_state.hand) < draw_amt + 1:
            return -1   # 山札切れ(n=0)防止
        return 3200 if len(my_state.hand) <= 4 else 600
    if card_id == Hassel:
        # 自分のポケモンが前のターンに気絶した時のみ使える。撃てるなら強ドロー
        return 3300 if len(my_state.hand) <= 5 else 500
    if card_id == Boss_Orders:
        if plan.need_boss and plan.target >= 1:
            return 8500
        return -1
    if card_id == Switch:
        if want_cramorant:
            return 9000   # ウッウを前に出して きまぐれスピットを撃つ
        if plan.attacker >= 1:
            return 4000
        return 500
    if card_id == Secret_Box:
        return 1500
    if card_id == Postwick:
        # ホップの攻撃+30。自分のスタジアムが無ければ貼る
        if stadium_id == Postwick:
            return -1
        return 6000
    return 5000


# 重要: kaggle_environments は main.py の「最後に定義された関数」をエージェントとして呼ぶ。
# そのため agent はファイルの最後に定義する。
def agent(obs_dict: dict) -> list[int]:
    """評価環境でのクラッシュ防止ラッパー：例外時も必ず合法手を返す。"""
    try:
        return _agent_impl(obs_dict)
    except Exception:
        try:
            obs = to_observation_class(obs_dict)
        except Exception:
            return my_deck
        if obs.select is None:
            return my_deck
        n = len(obs.select.option)
        if n == 0:
            return []
        mx = obs.select.maxCount if obs.select.maxCount else 1
        return list(range(n))[:mx]

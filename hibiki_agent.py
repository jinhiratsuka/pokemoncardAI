import os
import sys
from collections import defaultdict

from cg.api import AreaType, CardType, EnergyType, Observation, SelectContext, OptionType, Card, Pokemon, to_observation_class

# 汎用ロジック（デッキ非依存）はagent_genericに分離している
from agent_generic import (
    card_table,
    attack_table,
    prize_count,
    bench_pokemon_count,
    can_opponent_ko,
    should_safety_retreat,
)

"""
ヒビキのバクフーン (Ethan's Typhlosion) Deck - Agent（デッキ固有ロジック）

戦術方針:
- ヒノアラシ→（マグマラシ or ふしぎなアメ）→バクフーンを最速で立てる。
- マグマラシの特性「きずなのたび」とヒビキの冒険でヒビキの冒険を引き込み、
  使った冒険を捨て札に貯めてバディブラストの火力を伸ばす。
- バディブラスト = 40 + 捨て札のヒビキの冒険×60。ビクティニ(+10)・ブレイブパングル(+30)・
  からておうのとっくん(対ex +40)で上乗せする。序盤はスチームアルティ(160)で殴る。
- キチキギスexのクルーアロー(任意の相手に100)とボスの指令で的を処理する。
- サイドが詰まったらagent_genericの安全リトリートで延命する。
"""

file_path = "deck.csv"
if not os.path.exists(file_path):
    file_path = "/kaggle_simulations/agent/" + file_path
with open(file_path, "r") as file:
    csv = file.read().split("\n")
my_deck = []
for i in range(60):
    my_deck.append(int(csv[i]))

# --- カードID定数 ---
Ethan_Cyndaquil = 352
Ethan_Quilava = 353
Ethan_Typhlosion = 354
Victini = 202
Dunsparce = 305
Dudunsparce = 66
Fezandipiti_ex = 140
Shaymin = 343
Budew = 235
Psyduck = 858
Ultra_Ball = 1121
Buddy_Poffin = 1086
Rare_Candy = 1079
Night_Stretcher = 1097
Redeemable_Ticket = 1114
Tool_Scrapper = 1137
Secret_Box = 1092
Brave_Bangle = 1175
Ethan_Adventure = 1215
Lillie_Determination = 1227
Boss_Orders = 1182
Lana_Aid = 1184
Crispin = 1198
Black_Belt_Training = 1211
Battle_Cage = 1264
Gravity_Mountain = 1252
Basic_Fire_Energy = 2

# --- 技ID定数 ---
Buddy_Blast = 490
Steam_Artillery = 491
Cruel_Arrow = 183

EARLY_GAME_TURNS = 3
FIRE = EnergyType.FIRE


class AttackPlan:
    attacker = -1       # 攻撃するポケモン（0=アクティブ, 1+=ベンチindex+1）
    target = -1         # 攻撃対象（0=相手アクティブ, 1+=相手ベンチindex+1）
    attack_id = -1
    remain_hp = -1      # 攻撃後の相手の残りHP（<=0でKO）
    need_boss = False   # ボスの指令でベンチを呼ぶ必要があるか


plan = AttackPlan()
pre_turn = 0
ability_used_quilava = False


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


def fire_count(pokemon: Pokemon) -> int:
    return sum(1 for e in pokemon.energies if e == FIRE)


def pokemon_score(pokemon: Pokemon) -> int:
    """場のポケモンの価値（入れ替え・残す判断などに使うデッキ固有の重み付け）。"""
    data = card_table[pokemon.id]
    score = prize_count(pokemon) * 1000
    score += len(pokemon.energies) * 120
    score += len(pokemon.tools) * 80
    if pokemon.id == Ethan_Typhlosion:
        score += 600
    elif pokemon.id == Ethan_Quilava:
        score += 250
    elif pokemon.id == Fezandipiti_ex:
        score += 300
    elif pokemon.id == Victini:
        score += 200  # 火力補強の常駐特性
    elif pokemon.id == Dudunsparce:
        score += 120
    score += pokemon.hp
    return score


def typhlosion_damage(attack_id: int, target_data, discard_counts, victini_in_play: bool,
                      bangle: bool, black_belt: bool) -> int:
    """バクフーンの技の対象への推定ダメージ。"""
    if attack_id == Buddy_Blast:
        dmg = 40 + 60 * discard_counts[Ethan_Adventure]
    elif attack_id == Steam_Artillery:
        dmg = 160
    else:
        return 0
    # 「弱点・抵抗力を適用する前」に加わる補正
    if victini_in_play:
        dmg += 10
    if bangle:
        dmg += 30
    if black_belt and target_data.ex:
        dmg += 40
    # 弱点・抵抗力
    if target_data.weakness is not None and target_data.weakness == FIRE:
        dmg *= 2
    elif target_data.resistance is not None and target_data.resistance == FIRE:
        dmg -= 30
    return dmg


def agent(obs_dict: dict) -> list[int]:
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

    is_early_game = state.turn <= EARLY_GAME_TURNS
    field_pokemon_count = bench_pokemon_count(my_state)
    bench_is_full = field_pokemon_count >= 6

    global plan, pre_turn, ability_used_quilava
    if pre_turn != state.turn:
        pre_turn = state.turn
        plan = AttackPlan()
        ability_used_quilava = False

    # 場・手札・捨て札の集計
    field_counts = defaultdict(int)
    hand_counts = defaultdict(int)
    discard_counts = defaultdict(int)
    for card in my_state.active + my_state.bench:
        if card is not None:
            field_counts[card.id] += 1
    for card in my_state.hand:
        hand_counts[card.id] += 1
    for card in my_state.discard:
        discard_counts[card.id] += 1

    victini_in_play = field_counts[Victini] >= 1
    stadium_id = 0
    for card in state.stadium:
        stadium_id = card.id

    # 安全リトリート判定（汎用ロジックに委譲）
    safety_retreat = should_safety_retreat(my_state, op_state, my_prize)

    # 先攻/後攻の判定（スボミーの使い方に使う）
    first_decided = state.firstPlayer != -1
    going_first = first_decided and state.firstPlayer == my_index
    going_second = first_decided and state.firstPlayer != my_index
    cyndaquil_active = (my_state.active and my_state.active[0] is not None
                        and my_state.active[0].id == Ethan_Cyndaquil)

    # --- MAIN: 攻撃プランを立てる ---
    can_attack = False
    can_use_boss = False
    black_belt_played = False  # このターンにからておうのとっくんを使ったか（手札にある＝これから使える）
    if context == SelectContext.MAIN:
        for o in select.option:
            if o.type == OptionType.ATTACK:
                can_attack = True
            elif o.type == OptionType.PLAY:
                c = get_card(obs, AreaType.HAND, o.index, my_index)
                if c is not None and c.id == Boss_Orders:
                    can_use_boss = True
                elif c is not None and c.id == Black_Belt_Training:
                    black_belt_played = True

        my_active = my_state.active[0] if my_state.active else None
        op_active = op_state.active[0] if op_state.active else None
        op_cards = []
        if op_active is not None:
            op_cards.append(op_active)
        for p in op_state.bench:
            op_cards.append(p)

        if state.turn >= 2 and my_active is not None and can_attack:
            best_score = -1
            # 想定アタッカーはアクティブ。バクフーン/キチキギスの技を評価する。
            af = fire_count(my_active)
            at_total = len(my_active.energies)
            usable = []
            if my_active.id == Ethan_Typhlosion:
                if af >= 1:
                    usable.append(Buddy_Blast)
                if af >= 2 and at_total >= 3:
                    usable.append(Steam_Artillery)
            elif my_active.id == Fezandipiti_ex:
                if at_total >= 3:
                    usable.append(Cruel_Arrow)

            bangle = any(t.id == Brave_Bangle for t in my_active.tools)
            for atk in usable:
                for j, op_pokemon in enumerate(op_cards):
                    if op_pokemon is None:
                        continue
                    # ベンチを狙うにはボス（クルーアローは直接ベンチ可）
                    target_is_bench = j >= 1
                    if target_is_bench and atk != Cruel_Arrow and not can_use_boss:
                        continue
                    data = card_table[op_pokemon.id]
                    if atk == Cruel_Arrow:
                        damage = 100  # 弱点・抵抗力無視
                    else:
                        damage = typhlosion_damage(atk, data, discard_counts,
                                                   victini_in_play, bangle, black_belt_played)
                    eff_hp = op_pokemon.hp
                    score = pokemon_score(op_pokemon)
                    if eff_hp <= damage:
                        # KOできる。サイドを取れるほど高評価
                        score += 2000 + prize_count(op_pokemon) * 800
                    else:
                        score *= damage / max(1, eff_hp)
                    # KOで勝てるなら最優先
                    if eff_hp <= damage and prize_count(op_pokemon) >= len(op_state.prize):
                        score = 100000
                    if j == 0:
                        score += 250  # アクティブを殴るのが基本
                    if atk == Steam_Artillery:
                        score += 30  # 安定火力をやや優先
                    if score > best_score:
                        best_score = score
                        plan.attacker = 0
                        plan.target = j
                        plan.attack_id = atk
                        plan.remain_hp = eff_hp - damage
                        plan.need_boss = target_is_bench and atk != Cruel_Arrow

    # エネルギー付け先の評価（アクティブのアタッカー優先）
    have_set_typhlosion = any(
        p is not None and p.id == Ethan_Typhlosion and fire_count(p) >= 2
        for p in (my_state.active + my_state.bench)
    )

    def energy_score(pokemon: Pokemon, active: bool) -> int:
        score = 8000
        if active:
            score += 20
        if pokemon.id == Ethan_Typhlosion:
            # バクフーンを最優先でエネルギー満タンに（スチームアルティ160を撃てる状態を作る）
            score += 300
            if fire_count(pokemon) < 2:
                score += 150  # 2つ目までは強く欲しい
        elif pokemon.id in (Ethan_Cyndaquil, Ethan_Quilava):
            score += 40  # 進化後にエネルギーを引き継ぐ（バクフーン育成）
        elif pokemon.id == Fezandipiti_ex:
            # キチキギスはサブ。バクフーンが育ってから、かつエネルギーが余る時のみ
            if have_set_typhlosion and len(pokemon.energies) < 3:
                score += 20
            else:
                score -= 200
        else:
            score -= 80
        return score

    # --- 各選択肢をスコアリング ---
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
                    if o.playerIndex == my_index:
                        if isinstance(card, Pokemon):
                            score += len(card.energies) * 2
                            # サイドが少ない時は「取られるサイドが少ない」ポケモンを前に
                            if my_prize <= 3:
                                if prize_count(card) == 1:
                                    score += 60
                                elif prize_count(card) >= my_prize:
                                    score -= 200
                            if card.id == Ethan_Typhlosion:
                                score += 50  # 基本はバクフーンを前に
                            elif card.id == Fezandipiti_ex and len(card.energies) >= 3:
                                score += 20
                            if o.index == plan.attacker - 1:
                                score += 100
                    else:
                        # 相手をアクティブに上げる選択（ボス等）→ planの的を優先
                        if o.index == plan.target - 1:
                            score += 100

                elif context == SelectContext.SETUP_ACTIVE_POKEMON:
                    # 最初のアクティブ：殴れる/進化できるたねを優先、exは避ける
                    if card.id == Budew:
                        # 後攻：1ターン目にムズムズ花粉（アイテムロック）を打ちたいので最優先。
                        # 先攻：手札にあれば前に置く（盤面を守りつつ次ターンにロック）。
                        score = 10 if going_second else 8
                    elif card.id == Ethan_Cyndaquil:
                        score = 6
                    elif card.id == Victini:
                        score = 2
                    elif card.id == Fezandipiti_ex:
                        score = 1  # exは極力前に出さない
                    else:
                        score = 3

                elif context == SelectContext.TO_HAND:
                    # サーチ（ヒビキの冒険・ハイパーボール・ポフィン等）で手札に加えるカードの優先度
                    score = to_hand_score(card.id, field_counts, hand_counts, discard_counts,
                                          going_first, cyndaquil_active)

                elif context == SelectContext.ATTACH_FROM:
                    if isinstance(card, Pokemon):
                        score = energy_score(card, o.area == AreaType.ACTIVE)

                elif context == SelectContext.DISCARD or context == SelectContext.TO_DECK or context == SelectContext.TO_DECK_BOTTOM:
                    # 捨てる/デッキに戻すカード：価値が低いものほど高スコア（先に手放す）
                    score = discard_priority(card.id, field_counts, hand_counts, discard_counts)

                else:
                    # その他のカード選択：場のポケモンなら価値で、それ以外は中庸
                    if isinstance(card, Pokemon):
                        score = pokemon_score(card)
                    else:
                        score = 100

        elif o.type == OptionType.PLAY:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            data = card_table[card.id]
            if data.cardType == CardType.POKEMON:
                score = 20000
                if is_early_game:
                    score += 5000
                # 同じシステムポケモンは複数並べない
                if card.id in (Victini, Dudunsparce, Fezandipiti_ex, Shaymin, Psyduck, Budew):
                    if field_counts[card.id] >= 1:
                        score = -1
            else:
                score = play_trainer_score(
                    card.id, state, my_state, op_state, my_prize, is_early_game,
                    bench_is_full, field_counts, hand_counts, discard_counts,
                    stadium_id, safety_retreat, can_attack,
                )

        elif o.type == OptionType.ATTACH:
            card = get_card(obs, AreaType.HAND, o.index, my_index)
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            if card.id == Brave_Bangle:
                score = 7000
                # ルールボックスなしのアタッカー（バクフーン）に付ける
                if pokemon.id == Ethan_Typhlosion:
                    score += 300
                elif pokemon.id == Fezandipiti_ex:
                    score = -1  # exには効果なし
            elif card.id == Basic_Fire_Energy:
                score = energy_score(pokemon, o.inPlayArea == AreaType.ACTIVE)
                if plan.attacker == 0 and o.inPlayArea == AreaType.ACTIVE:
                    score += 150
            else:
                score = 3000

        elif o.type == OptionType.EVOLVE:
            pokemon = get_card(obs, o.inPlayArea, o.inPlayIndex, my_index)
            target_card = get_card(obs, AreaType.HAND, o.index, my_index) if o.index is not None else None
            score = 9000 + len(pokemon.energies) * 10
            tid = target_card.id if target_card is not None else None
            if tid == Ethan_Quilava:
                # マグマラシに進化＝特性「きずなのたび」でヒビキの冒険をサーチできる。
                # まだ場にマグマラシがいない（特性が無い）時は最優先でエンジンを起動する。
                if field_counts[Ethan_Quilava] == 0:
                    score += 700
                else:
                    score += 250
            elif tid == Ethan_Typhlosion:
                # 既にバクフーンが1体いて山札にヒビキの冒険が残るとみられる場合は、
                # 「安全な時だけ」マグマラシを温存し特性で冒険を引き続きサーチする。
                # 以下のプレッシャー下では温存せず進化して殴り返す/耐える：
                #   - サイドで負けている / 相手の攻撃でアクティブが倒される / 相手に育った大型exがいる
                #   - 進化するマグマラシ自身が瀕死（HPを上げて延命）
                adventures_accounted = hand_counts[Ethan_Adventure] + discard_counts[Ethan_Adventure]
                adventure_may_remain = adventures_accounted < 4  # 4枚すべて手札/捨て札に無い＝山札/サイドに残存
                already_have_typhlosion = field_counts[Ethan_Typhlosion] >= 1
                in_danger = pokemon.hp < pokemon.maxHp and pokemon.hp <= 60
                behind_on_prize = len(op_state.prize) < my_prize
                my_active = my_state.active[0] if my_state.active else None
                active_threatened = can_opponent_ko(my_active, op_state) if my_active is not None else False
                op_big_ex = any(
                    p is not None and card_table[p.id].ex and len(p.energies) >= 2
                    for p in ([op_state.active[0] if op_state.active else None] + list(op_state.bench))
                )
                under_pressure = behind_on_prize or active_threatened or op_big_ex
                if already_have_typhlosion and adventure_may_remain and not in_danger and not under_pressure:
                    score = -1  # 安全な時のみマグマラシを温存
                else:
                    score += 400
            elif tid == Dudunsparce:
                score += 80

        elif o.type == OptionType.ABILITY:
            card = get_card(obs, o.area, o.index, my_index)
            if card.id == Ethan_Quilava:
                # きずなのたび：山札にヒビキの冒険が残っていれば毎回サーチする。
                # マグマラシが複数いる場合は、各マグマラシの特性を基本的にすべて使う
                # （冒険は手札に貯めてベンチ展開に使い、余りは捨て札へ送ってバディブラストを伸ばす）。
                adventures_accounted = hand_counts[Ethan_Adventure] + discard_counts[Ethan_Adventure]
                if adventures_accounted < 4:  # 山札/サイドにまだ冒険が残っている＝サーチできる
                    score = 12000
                else:
                    score = 50
            elif card.id == Dudunsparce:
                # にげあしドロー：手札が少ない時に
                score = 11000 if len(my_state.hand) <= 4 else 200
            elif card.id == Fezandipiti_ex:
                score = 11500  # 気絶時ドロー（使える時は強力）
            elif card.id == 1267:  # Lumiose City等のスタジアム特性
                score = 1
            else:
                score = 8000

        elif o.type == OptionType.RETREAT:
            if safety_retreat:
                score = 9500
            elif plan.attacker >= 1:
                score = 2000
            else:
                active = my_state.active[0] if my_state.active else None
                # 弱いたね（ヒノアラシ等）が前で、ベンチに育ったバクフーンがいれば下がる
                if active is not None and active.id in (Ethan_Cyndaquil, Budew, Psyduck):
                    has_attacker = any(
                        p is not None and p.id in (Ethan_Typhlosion, Fezandipiti_ex)
                        for p in my_state.bench
                    )
                    score = 1500 if has_attacker else -1
                else:
                    score = -1

        elif o.type == OptionType.ATTACK:
            score = 1000
            if o.attackId == plan.attack_id:
                score += 200
            if plan.remain_hp <= 0 and o.attackId == plan.attack_id:
                score += 500
            # ヒノアラシのEmberはエネルギーを捨てるので、KOプランが無ければ避ける
            if o.attackId == 488 and not (plan.attack_id == 488 and plan.remain_hp <= 0):
                score = 5

        scores.append(score)

    # ボスの指令が必要なプランで、まだ殴れない（的がベンチ）の場合は
    # ボスのPLAYが上で高得点化される。通常はスコア最大を選ぶ。
    desc_indices = [i for i, _ in sorted(enumerate(scores), key=lambda x: x[1], reverse=True)]
    return desc_indices[:select.maxCount]


def to_hand_score(card_id: int, field_counts, hand_counts, discard_counts,
                  going_first=False, cyndaquil_active=False) -> int:
    """サーチで手札に加えるカードの優先度（デッキの噛み合わせ重視）。"""
    if card_id == Budew:
        # 先攻でヒビキのヒノアラシが既にバトル場なら、わざわざスボミーを呼ばずライン展開を優先
        if going_first and cyndaquil_active:
            return 15
        return 90 if field_counts[Budew] == 0 else 20
    have_typhlosion_line = (field_counts[Ethan_Typhlosion] >= 1)
    # 進化ラインの完成を最優先
    if card_id == Ethan_Typhlosion:
        return 300 if hand_counts[card_id] == 0 else 120
    if card_id == Ethan_Cyndaquil:
        # ヒビキの冒険などのサーチではベンチ展開（ヒノアラシ）を優先（エネルギーより上）
        if field_counts[Ethan_Cyndaquil] + field_counts[Ethan_Quilava] + field_counts[Ethan_Typhlosion] == 0:
            return 280
        return 160
    if card_id == Ethan_Quilava:
        return 200 if not have_typhlosion_line else 90
    if card_id == Ethan_Adventure:
        return 250  # サーチ＆バディブラストの燃料。最優先級で持ってくる
    if card_id == Rare_Candy:
        return 160
    if card_id == Victini:
        return 150 if field_counts[Victini] == 0 else 40
    if card_id == Basic_Fire_Energy:
        return 140 - hand_counts[card_id] * 30
    if card_id == Boss_Orders:
        return 120
    if card_id == Fezandipiti_ex:
        return 110 if field_counts[Fezandipiti_ex] == 0 else 20
    if card_id in (Dunsparce, Dudunsparce):
        return 100
    if card_id in (Shaymin, Psyduck):
        return 90 if field_counts[card_id] == 0 else 20
    return 80 - hand_counts[card_id] * 20


def discard_priority(card_id: int, field_counts, hand_counts, discard_counts) -> int:
    """捨てる/デッキ戻し対象の優先度（大きいほど先に手放す）。
    ヒビキの冒険は捨て札に置くとバディブラストが伸びるので、過剰分は積極的に手放す。
    """
    # 絶対に手放したくない核
    if card_id in (Ethan_Typhlosion, Rare_Candy, Boss_Orders):
        return -100
    # 余ったヒビキの冒険は捨ててバディブラストを強化（手札に1枚は残す）
    if card_id == Ethan_Adventure:
        return 500 if hand_counts[card_id] >= 2 else 10
    # 余剰エネルギー
    if card_id == Basic_Fire_Energy:
        return 300 if hand_counts[card_id] >= 3 else 60
    # 重複したシステムは捨ててよい
    if card_id in (Victini, Fezandipiti_ex, Budew, Shaymin, Psyduck, Dunsparce, Dudunsparce):
        if field_counts[card_id] >= 1 or hand_counts[card_id] >= 2:
            return 250
        return 40
    # 進化前の余りは中程度
    if card_id in (Ethan_Cyndaquil, Ethan_Quilava):
        return 120 if hand_counts[card_id] >= 2 else 30
    return 100


def play_trainer_score(card_id, state, my_state, op_state, my_prize, is_early_game,
                       bench_is_full, field_counts, hand_counts, discard_counts,
                       stadium_id, safety_retreat, can_attack) -> int:
    """トレーナーズをプレイする優先度。"""
    have_typhlosion = field_counts[Ethan_Typhlosion] >= 1

    if card_id == Ultra_Ball:
        # 進化パーツが無い時に最優先で使う（捨てるカードはdiscard_priorityで選ぶ）
        if not have_typhlosion and (hand_counts[Ethan_Typhlosion] == 0 or field_counts[Ethan_Cyndaquil] == 0):
            return 15000
        return 9000
    if card_id == Buddy_Poffin:
        if is_early_game and not bench_is_full:
            return 16000  # 序盤のたね展開を最優先
        return 9000
    if card_id == Rare_Candy:
        # 手札にバクフーンがあり、場にヒノアラシがいる時だけ価値が高い
        if hand_counts[Ethan_Typhlosion] >= 1 and field_counts[Ethan_Cyndaquil] >= 1 and not have_typhlosion:
            return 17000
        return -1
    if card_id == Ethan_Adventure:
        # サポート。サーチ＋捨て札の燃料化（バディブラスト強化）。毎ターン優先的に使う
        return 3300
    if card_id == Lillie_Determination:
        # 手札が細い時のドロー（サポート）
        return 3200 if len(my_state.hand) <= 4 else 600
    if card_id == Crispin:
        # エネルギー加速・サーチ。バクフーンがエネルギー不足なら優先度を上げる
        active = my_state.active[0] if my_state.active else None
        if active is not None and active.id == Ethan_Typhlosion and fire_count(active) < 2:
            return 3400
        return 2800
    if card_id == Lana_Aid:
        # 捨て札からポケモン/エネルギーを回収。前半はレートが低く、
        # 後半にヒビキのヒノアラシ/マグマラシ/バクフーンが倒されて捨て札にあるほどレートが上がる。
        line_in_discard = (discard_counts[Ethan_Cyndaquil]
                           + discard_counts[Ethan_Quilava]
                           + discard_counts[Ethan_Typhlosion])
        if line_in_discard >= 1:
            # 倒された進化ラインを回収して立て直す（枚数が多いほど価値が高い）
            return 3200 + min(line_in_discard, 3) * 300
        if discard_counts[Basic_Fire_Energy] >= 2 and len(my_state.discard) >= 6:
            return 1200  # エネルギーだけでも終盤の回収価値
        return 150  # 前半は低レート
    if card_id == Black_Belt_Training:
        # 相手アクティブがexで、それを攻撃でKOできるなら強力
        op_active = op_state.active[0] if op_state.active else None
        if can_attack and op_active is not None and card_table[op_active.id].ex:
            return 8000
        return -1
    if card_id == Boss_Orders:
        # ベンチの的を呼ぶプランがある時
        if plan.need_boss and plan.target >= 1:
            return 8500
        # 終盤、相手の弱いベンチを呼んでサイドを取る
        return -1
    if card_id == Tool_Scrapper:
        # 相手の道具を割りたい時（簡易：相手のアクティブに道具があれば）
        op_active = op_state.active[0] if op_state.active else None
        if op_active is not None and len(op_active.tools) >= 1:
            return 2000
        return -1
    if card_id == Night_Stretcher:
        # 捨て札に炎エネルギーor必要なポケモンがある時だけ使う（空撃ち防止）
        has_fire = discard_counts[Basic_Fire_Energy] >= 1
        has_key = (discard_counts[Ethan_Typhlosion] >= 1 or discard_counts[Ethan_Cyndaquil] >= 1
                   or discard_counts[Fezandipiti_ex] >= 1)
        if has_fire or has_key:
            return 2400
        return -1
    if card_id == Redeemable_Ticket:
        if len(my_state.hand) <= 2:
            return 2500
        return 100
    if card_id == Secret_Box:
        return 1500
    if card_id == Gravity_Mountain:
        # 相手に2進化が多い時に出す（自分のバクフーンも縮むので控えめ）
        op_stage2 = sum(1 for p in [op_state.active[0] if op_state.active else None] + list(op_state.bench)
                        if p is not None and card_table[p.id].stage2)
        if stadium_id == Gravity_Mountain:
            return -1
        if op_stage2 >= 1:
            return 4500
        return 800
    if card_id == Battle_Cage:
        if stadium_id == Battle_Cage:
            return -1
        return 1200
    return 5000

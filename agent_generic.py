"""
汎用ロジック（デッキ非依存）

特定のデッキ構成・カードIDに依存しない、ポケモンTCG全般で通用する判断ロジックをまとめる。
- サイド枚数の計算（prize_count）
- 相手の攻撃でアクティブが倒されるかの推定（can_opponent_ko）
- サイド負けを避けるための安全リトリート判定（should_safety_retreat）

カードデータ・攻撃データのテーブル（card_table / attack_table）もここで構築する。
これらはルール（カードの性能）に基づくものでデッキタイプに依存しないため汎用側に置く。
"""

from cg.api import Pokemon, all_card_data, all_attack

# カードID -> CardData、攻撃ID -> Attack のルックアップテーブル（ルール由来でデッキ非依存）
card_table = {c.cardId: c for c in all_card_data()}
attack_table = {a.attackId: a for a in all_attack()}

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

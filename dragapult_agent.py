import os
from collections import defaultdict

from cg.api import (
    AreaType, CardType, EnergyType, Observation, SelectContext, OptionType,
    Card, Pokemon, all_card_data, all_attack, to_observation_class,
)

"""
Dragapult ex Deck Agent

戦略:
- Dreepy→(Rare CandyでDrakloak省略)→Dragapult exを最速展開。
- Sparkling Crystal装備でPhantom Dive({R}{P})が実質1エネ→200点+ベンチ6カウンター分散。
- Noctowl進化時(テラ在場): Stage1/2ポケモンサーチで展開加速。
- Farfetch'd着地時: Sparkling CrystalをサーチしてDragapult exに即装備。
- Hero's Capeで実質420HP。Boss's Ordersで高価値ベンチを引き出す。
"""

# カードID定数
Dreepy          = 119
Drakloak        = 120
Dragapult_ex    = 121
Hoothoot        = 64
Noctowl_id      = 173
Farfetchd       = 123
Rare_Candy      = 1079
Ultra_Ball      = 1121
Buddy_Poffin    = 1086
Tera_Orb        = 1127
Spark_Crystal   = 1165
Night_Stretcher = 1097
Heros_Cape      = 1159
Air_Balloon     = 1174
Lillie_Det      = 1227
Carmine         = 1192
Boss_Orders     = 1182
Crispin         = 1198
Brocks_Scout    = 1210
Fire_E          = 2
Psychic_E       = 5
ENERGY_IDS      = {Fire_E, Psychic_E}

_cards   = all_card_data()
card_tbl = {c.cardId: c for c in _cards}
_attacks = all_attack()
atk_tbl  = {a.attackId: a for a in _attacks}

file_path = "deck.csv"
if not os.path.exists(file_path):
    file_path = "/kaggle_simulations/agent/" + file_path
with open(file_path) as f:
    _lines = f.read().split("\n")
my_deck = [int(_lines[i]) for i in range(60)]

EARLY_TURNS = 4


class Plan:
    attacker     = -1   # 0=アクティブ, 1〜5=ベンチindex
    target       = -1   # 0=相手アクティブ, 1〜=相手ベンチindex
    attack_idx   = -1   # 0=Jet Headbutt, 1=Phantom Dive
    remain_hp    = -1
    need_energy  = False
    need_crystal = False


_plan    = Plan()
_pre_trn = 0


def _get(obs, area, idx, pi):
    ps = obs.current.players[pi]
    if area == AreaType.DECK:    return obs.select.deck[idx]
    if area == AreaType.HAND:    return ps.hand[idx]
    if area == AreaType.DISCARD: return ps.discard[idx]
    if area == AreaType.ACTIVE:  return ps.active[idx]
    if area == AreaType.BENCH:   return ps.bench[idx]
    if area == AreaType.PRIZE:   return ps.prize[idx]
    if area == AreaType.STADIUM: return obs.current.stadium[idx]
    if area == AreaType.LOOKING: return obs.current.looking[idx]
    return None


def _has_crystal(poke: Pokemon) -> bool:
    return any(t.id == Spark_Crystal for t in poke.tools)


def _prize_cnt(poke: Pokemon) -> int:
    d = card_tbl[poke.id]
    n = 3 if d.megaEx else 2 if d.ex else 1
    for c in poke.energyCards:
        if c.id == 12: n -= 1   # Legacy Energy
    return max(0, n)


def _poke_score(poke: Pokemon) -> int:
    d = card_tbl[poke.id]
    s = _prize_cnt(poke) * 1000 + len(poke.energies) * 150 + len(poke.tools) * 100
    if d.stage2: s += 250
    elif d.stage1: s += 130
    return s + poke.hp


def agent(obs_dict: dict) -> list[int]:
    global _plan, _pre_trn

    obs = to_observation_class(obs_dict)
    if obs.select is None:
        return my_deck

    state = obs.current
    sel   = obs.select
    ctx   = sel.context
    mi    = state.yourIndex
    me    = state.players[mi]
    op    = state.players[1 - mi]
    is_early = state.turn <= EARLY_TURNS

    if _pre_trn != state.turn:
        _pre_trn = state.turn
        _plan    = Plan()

    hc = defaultdict(int)
    fc = defaultdict(int)
    dc = defaultdict(int)
    for c in me.hand:
        hc[c.id] += 1
    for c in (me.active + me.bench):
        if c: fc[c.id] += 1
    for c in me.discard:
        dc[c.id] += 1

    # アクティブポケモンがDragapult exかどうか
    active_is_dragapult = bool(me.active and me.active[0] and me.active[0].id == Dragapult_ex)
    bench_has_dragapult = any(p and p.id == Dragapult_ex for p in me.bench)

    # ── メインフェーズ 攻撃プランニング ──────────────────────────────
    if ctx == SelectContext.MAIN:
        my_row = [me.active[0]] + list(me.bench)
        op_row = [op.active[0]] + list(op.bench)

        # [FIX1] Switchカードはデッキに無いのでRETREATのみ確認
        can_sw    = any(o.type == OptionType.RETREAT for o in sel.option)
        can_op_sw = any(
            o.type == OptionType.PLAY and me.hand[o.index].id == Boss_Orders
            for o in sel.option if o.type == OptionType.PLAY
        )

        best = -1
        for i, mp in enumerate(my_row):
            if not mp or mp.id != Dragapult_ex: continue
            if i > 0 and not can_sw: continue

            crystal = _has_crystal(mp)
            ec      = len(mp.energies)

            for aidx, (req, bdmg) in enumerate([(1, 70), (1 if crystal else 2, 200)]):
                avail  = ec
                need_e = False
                if ec < req:
                    has_e = (hc[Fire_E] + hc[Psychic_E]) > 0
                    if not state.energyAttached and has_e:
                        need_e = True
                        avail  = ec + 1
                    else:
                        continue
                if avail < req:
                    continue

                need_cr = (aidx == 1 and not crystal)
                if need_cr and hc[Spark_Crystal] == 0:
                    continue

                for j, opp in enumerate(op_row):
                    if not opp: continue
                    if j > 0 and not can_op_sw: continue

                    # [FIX3] ウィークネス込みのダメージでremain_hpを計算
                    dmg = bdmg
                    od  = card_tbl[opp.id]
                    if od.weakness == EnergyType.DRAGON:
                        dmg *= 2

                    s = _poke_score(opp)
                    if opp.hp <= dmg:
                        pc = _prize_cnt(opp)
                        if len(op.prize) <= pc:
                            s = 50000
                    else:
                        s = s * dmg // max(opp.hp, 1)

                    s += (300 if i == 0 else 0) + (200 if j == 0 else 0)
                    s += 500 if aidx == 1 else 0
                    s += avail * 10

                    if s > best:
                        best              = s
                        _plan.attacker    = i
                        _plan.target      = j
                        _plan.attack_idx  = aidx
                        _plan.remain_hp   = opp.hp - dmg  # [FIX3] ウィークネス反映
                        _plan.need_energy = need_e
                        _plan.need_crystal = need_cr

    # ── オプションスコアリング ─────────────────────────────────────
    scores = []
    for o in sel.option:
        s = 0

        if o.type == OptionType.NUMBER:
            s = o.number

        elif o.type == OptionType.YES:
            s = 1

        elif o.type == OptionType.CARD:
            card = _get(obs, o.area, o.index, o.playerIndex)
            if card is None:
                scores.append(0); continue
            ec = len(card.energies) if isinstance(card, Pokemon) else 0

            if ctx in (SelectContext.SWITCH, SelectContext.TO_ACTIVE):
                if o.playerIndex == mi:
                    # [FIX7] Dragapult exへの入れ替えスコアを大幅に引き上げ
                    if card.id == Dragapult_ex:
                        s = 5000 + ec * 100
                    elif card.id == Drakloak:
                        s = 500
                    else:
                        s = 50
                    if o.area == AreaType.BENCH and o.index + 1 == _plan.attacker:
                        s += 3000
                else:
                    if o.area == AreaType.BENCH and o.index + 1 == _plan.target:
                        s += 3000
                    else:
                        s = 100

            elif ctx in (SelectContext.SETUP_ACTIVE_POKEMON, SelectContext.SETUP_BENCH_POKEMON,
                         SelectContext.TO_BENCH):  # [FIX2] TO_BENCHを追加
                s = {Dreepy: 30, Hoothoot: 20, Farfetchd: 15}.get(card.id, 5) \
                    if isinstance(card, Pokemon) else 5

            elif ctx == SelectContext.TO_HAND:
                s = 50 - hc[card.id] * 15
                if isinstance(card, Pokemon):
                    s += {Dragapult_ex: 80, Drakloak: 60, Dreepy: 40,
                          Noctowl_id: 30, Hoothoot: 25, Farfetchd: 15}.get(card.id, 5)
                else:
                    s += {Spark_Crystal: 70, Rare_Candy: 60, Heros_Cape: 50,
                          Tera_Orb: 45, Boss_Orders: 40, Night_Stretcher: 35,
                          Crispin: 35, Fire_E: 30, Psychic_E: 30}.get(card.id, 10)

            elif ctx == SelectContext.DISCARD:
                # [FIX4] 高スコア=捨てやすい。どうぐ・サポーターを保護
                if isinstance(card, Pokemon):
                    s = 0
                elif card.id in (Spark_Crystal, Rare_Candy, Tera_Orb, Heros_Cape,
                                 Night_Stretcher, Crispin, Boss_Orders, Brocks_Scout):
                    s = 5   # 重要カードは最後まで温存
                elif card.id in (Lillie_Det, Carmine):
                    s = 10 if hc[card.id] <= 1 else 40
                elif card.id in ENERGY_IDS:
                    s = 20
                else:
                    s = 30  # その他トレーナーズ

            elif ctx == SelectContext.ATTACH_FROM:
                if card.id == Dragapult_ex:
                    s = 1000 - ec * 100
                elif card.id == Drakloak:
                    s = 300
                else:
                    s = 50

        elif o.type == OptionType.PLAY:
            card = _get(obs, AreaType.HAND, o.index, mi)
            d    = card_tbl[card.id]

            if d.cardType == CardType.POKEMON:
                s = 10000
                if card.id == Dreepy    and fc[Dreepy]    >= 4: s = -1
                if card.id == Hoothoot  and fc[Hoothoot]  >= 2: s = -1
                if card.id == Farfetchd and fc[Farfetchd] >= 2: s = -1

            elif d.cardType == CardType.ITEM:
                need_basic = fc[Dreepy] < 4 or fc[Hoothoot] < 2
                if card.id == Buddy_Poffin:
                    s = 14000 if (need_basic and is_early) else 8000 if need_basic else -1
                elif card.id == Tera_Orb:
                    s = 13000 if fc[Dragapult_ex] < 3 else -1
                elif card.id == Ultra_Ball:
                    s = 12000
                elif card.id == Rare_Candy:
                    s = 11000 if (fc[Dreepy] >= 1 and hc[Dragapult_ex] >= 1) else -1
                elif card.id == Night_Stretcher:
                    s = 7000
                elif card.id == Air_Balloon:
                    s = 4000
                else:
                    s = 5000

            elif d.cardType == CardType.SUPPORTER:
                if state.supporterPlayed:
                    s = -1
                elif card.id == Boss_Orders:
                    s = 6000 if _plan.target >= 1 else -1
                elif card.id == Crispin:
                    no_e = not me.active or len(me.active[0].energies) == 0
                    s = 5500 if no_e else 3000
                elif card.id == Lillie_Det:
                    s = 5000
                elif card.id == Brocks_Scout:
                    s = 4000 if fc[Dragapult_ex] < 2 else 1000
                elif card.id == Carmine:
                    s = 3500 if is_early else 2000
                else:
                    s = 2000

        elif o.type == OptionType.ATTACH:
            # ポケモンどうぐ・エネルギーはATTACHとして来る
            card = _get(obs, AreaType.HAND, o.index, mi)
            poke = _get(obs, o.inPlayArea, o.inPlayIndex, mi)
            if poke is None:
                scores.append(0); continue

            if card.id == Spark_Crystal:
                s = 8000 if poke.id == Dragapult_ex else -1
            elif card.id == Heros_Cape:
                s = 7000 + (500 if poke.id == Dragapult_ex else 0)
            elif card.id == Air_Balloon:
                s = 3000
            else:
                # エネルギー付与
                if poke.id == Dragapult_ex:
                    s = 9000 - len(poke.energies) * 500
                    att = 0 if o.inPlayArea == AreaType.ACTIVE else 1 + o.inPlayIndex
                    if att == _plan.attacker and _plan.need_energy:
                        s += 1000
                elif poke.id == Drakloak:
                    s = 2000
                else:
                    s = 100

        elif o.type == OptionType.EVOLVE:
            poke = _get(obs, o.inPlayArea, o.inPlayIndex, mi)
            s = 15000 + (len(poke.energies) if poke else 0)

        elif o.type == OptionType.ABILITY:
            s = 20000

        elif o.type == OptionType.RETREAT:
            # [FIX6] アクティブがDragapult ex以外でベンチにDragapult exがいる場合も入れ替え
            if _plan.attacker >= 1:
                s = 2000
            elif not active_is_dragapult and bench_has_dragapult:
                s = 1500
            else:
                s = -1

        elif o.type == OptionType.ATTACK:
            atk = atk_tbl.get(o.attackId)
            s   = 1000 + (atk.damage if atk else 0)
            if atk and atk.damage >= 150:   # Phantom Dive
                s += 500

        # OptionType.END は score=0 のまま

        scores.append(s)

    # [FIX5] 有効なオプション(score>=0)を優先し、全て負の場合のみ負スコアから選ぶ
    desc  = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    valid = [i for i in desc if scores[i] >= 0]
    result = valid if valid else desc
    return result[:sel.maxCount]

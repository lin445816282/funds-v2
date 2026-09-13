# -*- coding: utf-8 -*-
"""
方案A 真实下单回测重建：56组追负达朗贝尔±5
口径（与前端 computeLadderChips / computeLadderGuideTrack / computeLadderNumbers 一致）：
  - 56组 = C(8,3) 三家店组合（一店~六店 + 集合14 + 集合16）
  - 每天每组：pgood = 排位≤25 的家数；negHit = pgood<2（≥2家排位>25 → 追负命中）
  - 筹码 chip：起始5，命中 max(5,chip-5)，未中 chip+5，chip>70 重置5
  - 盈亏：命中 +chip×23 / 未中 -chip×24
  - order_amount = chip × 号码数（3家店 threshold=24 号码投票≥2 入选）
  - 本金滚动：2万起，capital≤0 归零(bankrupt++)，capital≥base×2 提取25%
数据源：
  - records 表 (category=cat_1783487972049) → 每天每店排位
  - order_numbers 表 (threshold=24) → 每天每店24码
输出：ladder_scheme_a_daily + ladder_scheme_a_detail 表（先打印，验证后写入）
"""
import sqlite3, json, math
from itertools import combinations

DB = "/home/xiaolin/projects/funds-v2/backend/funds-v2.db"
TH = 25
START = 5
RESET = 70
INIT_CAP = 20000
START_DATE = "2026-05-01"


def js_round(x):
    """对齐 JS Math.round：四舍五入（0.5 向上），Python round 是银行家舍入会差 ±1"""
    return math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)

# 门店 → collection_id 映射（order_numbers 表，单店为负数）
STORE_CID = {"一店": -23, "二店": -24, "三店": -25, "四店": -26, "五店": -28, "六店": -29, "集合14": 14, "集合16": 16}
STORES = ["一店", "二店", "三店", "四店", "五店", "六店", "集合14", "集合16"]


def load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # 排位
    day_rank = {}
    for r in conn.execute("SELECT store, date, amount FROM records WHERE category='cat_1783487972049'"):
        day_rank.setdefault(r["date"], {})[r["store"]] = r["amount"]
    # 号码 (threshold=24)
    day_nums = {}
    for r in conn.execute("SELECT date, collection_id, numbers_json FROM order_numbers WHERE threshold=24"):
        day_nums.setdefault(r["date"], {})[r["collection_id"]] = json.loads(r["numbers_json"])
    conn.close()
    return day_rank, day_nums


def rebuild():
    day_rank, day_nums = load()
    dates = sorted(d for d in day_rank if d >= START_DATE)
    combos = list(combinations(STORES, 3))

    # 每组独立 chip 状态
    chips = {c: START for c in combos}
    cap = INIT_CAP
    base = INIT_CAP
    wd_total = 0
    bankrupt = 0
    daily = []

    for dd in dates:
        rank = day_rank.get(dd, {})
        nums = day_nums.get(dd, {})
        day_profit = 0
        day_order = 0
        day_hits = 0
        day_total = 0
        detail = []
        for trio in combos:
            a, b, c = rank.get(trio[0]), rank.get(trio[1]), rank.get(trio[2])
            if a is None or b is None or c is None:
                continue
            pgood = (1 if a <= TH else 0) + (1 if b <= TH else 0) + (1 if c <= TH else 0)
            neg_hit = 1 if pgood < 2 else 0
            chip = chips[trio]
            # 号码投票≥2
            vote = {}
            for st in trio:
                cid = STORE_CID[st]
                for n in nums.get(cid, []):
                    vote[n] = vote.get(n, 0) + 1
            picked = sorted(n for n, v in vote.items() if v >= 2)
            order_amt = chip * len(picked)
            # 盈亏（真实下单口径）：命中返 47×chip，净赚 47×chip−下单额；未中亏全部下单额
            pnl = (47 * chip - order_amt) if neg_hit else -order_amt
            day_profit += pnl
            day_order += order_amt
            day_total += 1
            if neg_hit:
                day_hits += 1
                chips[trio] = max(START, chip - 5)
            else:
                chips[trio] = chip + 5
                if chips[trio] > RESET:
                    chips[trio] = START
            detail.append({
                "trio": "+".join(trio), "chip": chip,
                "nums_json": json.dumps(picked), "order_amt": order_amt, "neg_hit": neg_hit,
            })
        # 本金滚动（整数口径：capital = cap - wd，非浮点 0.75 乘法）
        cap += day_profit
        if cap <= 0:
            bankrupt += 1
            cap = INIT_CAP
            base = INIT_CAP
        wd = 0
        if cap >= base * 2:
            wd = js_round(cap * 0.25)
            wd_total += wd
            cap = cap - wd
            base = cap
        daily.append({
            "date": dd, "profit": day_profit, "order_amount": day_order,
            "capital": js_round(cap), "withdraw": js_round(wd), "bankrupt": bankrupt,
            "hits": day_hits, "total": day_total, "_detail": detail,
        })
    return daily


def sync_to_db(daily=None):
    """重算并写入 ladder_scheme_a_daily + ladder_scheme_a_detail 表（幂等：先删后插）。返回最新日期。"""
    if daily is None:
        daily = rebuild()
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM ladder_scheme_a_daily")
    conn.execute("DELETE FROM ladder_scheme_a_detail")
    for d in daily:
        conn.execute(
            "INSERT INTO ladder_scheme_a_daily (date, profit, order_amount, capital, withdraw, bankrupt, hits, total) VALUES (?,?,?,?,?,?,?,?)",
            (d["date"], d["profit"], d["order_amount"], d["capital"], d["withdraw"], d["bankrupt"], d["hits"], d["total"]),
        )
        for det in d["_detail"]:
            conn.execute(
                "INSERT INTO ladder_scheme_a_detail (date, trio, chip, nums_json, order_amt, neg_hit) VALUES (?,?,?,?,?,?)",
                (d["date"], det["trio"], det["chip"], det["nums_json"], det["order_amt"], det["neg_hit"]),
            )
    conn.commit()
    conn.close()
    return daily[-1]["date"] if daily else None


if __name__ == "__main__":
    daily = rebuild()
    print(f"共 {len(daily)} 天")
    for d in daily[:3]:
        print({k: d[k] for k in ("date", "profit", "order_amount", "capital", "withdraw", "bankrupt", "hits", "total")})
    print("...")
    for d in daily[-3:]:
        print({k: d[k] for k in ("date", "profit", "order_amount", "capital", "withdraw", "bankrupt", "hits", "total")})
    print(f"最新日期: {daily[-1]['date']}")
    print("（如需写库，调用 sync_to_db()）")

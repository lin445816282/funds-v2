"""
批量同步：从 sim_guides → order_history + order_amounts
每个日期合并正/反帮扶门店，计算1-49号码金额，写入存档
"""
import sqlite3
import json
from datetime import datetime as dt, timedelta
from collections import defaultdict

FUNDS_DB = "/home/xiaolin/projects/funds-v2/backend/funds-v2.db"

def sync_all():
    db = sqlite3.connect(FUNDS_DB)
    cur = db.cursor()

    # 读取所有 sim_guides
    cur.execute('SELECT id, date, rankings, result FROM sim_guides ORDER BY date, id')
    rows = cur.fetchall()

    # 按日期分组，保留最新正/反
    by_date = defaultdict(lambda: {'positive': None, 'negative': None, 'rankings': {}})
    for id_, date, rankings_json, result_json in rows:
        result = json.loads(result_json)
        mode = result.get('mode', '')
        rankings = json.loads(rankings_json) if rankings_json else {}
        by_date[date]['rankings'] = rankings
        if mode in ('positive', 'negative'):
            by_date[date][mode] = result

    # 抽签号映射（从 draw_records）
    cur.execute('SELECT date, draw_number FROM draw_records')
    draw_map = {r[0]: r[1] for r in cur.fetchall()}

    # 门店排位（从 records）
    cur.execute("SELECT date, store, amount FROM records WHERE category='ranking' ORDER BY date")
    from collections import defaultdict as dd
    ranking_map = dd(dict)
    for r in cur.fetchall():
        ranking_map[r[0]][r[1]] = r[2]

    synced = 0
    skipped = 0
    errors = []

    for date, entry in sorted(by_date.items()):
        pos = entry['positive']
        neg = entry['negative']
        if not pos and not neg:
            skipped += 1
            continue

        # 出手日 = 排位日 + 1 天
        action_date = (dt.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        # 合并正反帮扶门店 → 列表（同一门店可同时出现在正/反两边）
        store_both = {}  # store → {positive: cap, negative: cap}
        if pos:
            voted_pos = [c for c in pos.get('consensus', []) if c.get('votes', 0) > 0]
            for c in voted_pos:
                cap = max(c.get('caps', {}).values())
                s = c['store']
                if s not in store_both:
                    store_both[s] = {}
                store_both[s]['positive'] = cap
        if neg:
            voted_neg = [c for c in neg.get('consensus', []) if c.get('votes', 0) > 0]
            for c in voted_neg:
                cap = max(c.get('caps', {}).values())
                s = c['store']
                if s not in store_both:
                    store_both[s] = {}
                store_both[s]['negative'] = cap

        if not store_both:
            skipped += 1
            continue

        # 计算 1-49 号码金额 + 构建 stores 列表
        num_amounts = defaultdict(float)
        pos_nums = list(range(1, 26))   # 1-25
        neg_nums = list(range(26, 50))  # 26-49
        stores_list = []

        for s, modes in store_both.items():
            for mode in ('positive', 'negative'):
                cap = modes.get(mode, 0)
                if cap <= 0:
                    continue
                stores_list.append({'store': s, 'capital': cap, 'mode': mode})
                nums = pos_nums if mode == 'positive' else neg_nums
                per_num = cap / max(len(nums), 1)
                for n in nums:
                    num_amounts[n] += per_num

        # 写入 order_amounts
        cur.execute("DELETE FROM order_amounts WHERE date=?", (action_date,))
        now_ts = dt.now().isoformat()
        for n in range(1, 50):
            amt = round(num_amounts.get(n, 0), 2)
            if amt > 0:
                cur.execute(
                    "INSERT INTO order_amounts (date, number, amount, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (action_date, n, amt, now_ts, now_ts)
                )

        # 写入 order_history
        total_cap = sum(s['capital'] for s in stores_list)

        draw_number = draw_map.get(action_date, 0)
        # rankings 来自 sim_guides
        rankings_snap = entry['rankings']
        
        # amounts 快照
        amounts_snap = {str(n): round(v, 2) for n, v in num_amounts.items() if v > 0}

        cur.execute("DELETE FROM order_history WHERE date=? AND action_date=?", (date, action_date))
        cur.execute(
            """INSERT INTO order_history 
               (date, action_date, mode, stores_json, total_capital,
                amounts_json, draw_number, rankings_json, acknowledged)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (date, action_date, "full",
             json.dumps(stores_list, ensure_ascii=False),
             total_cap,
             json.dumps(amounts_snap, ensure_ascii=False),
             draw_number,
             json.dumps(rankings_snap, ensure_ascii=False))
        )
        synced += 1

    db.commit()
    db.close()

    print(f"同步完成: {synced} 天, 跳过 {skipped} 天, 错误 {len(errors)}")
    for e in errors[:5]:
        print(f"  ⚠ {e}")
    return synced


if __name__ == '__main__':
    sync_all()

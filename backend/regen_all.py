#!/usr/bin/env python3
"""全量重跑 sim_guides + order_history — 正反帮扶都用前一日排位"""
import json, sqlite3, sys, os

# Ensure output is unbuffered
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

os.chdir('/home/xiaolin/projects/funds-v2/backend')
from simulate import run_daily_guide_for_date, save_order_history, STORE_NAMES

db = sqlite3.connect('funds-v2.db')
dates = db.execute('SELECT DISTINCT date FROM sim_guides ORDER BY date').fetchall()
dates = [d[0] for d in dates]
print(f'Total dates: {len(dates)}')

for mode in ['positive', 'negative']:
    print(f'\n=== {mode} mode ===')
    for idx, date in enumerate(dates):
        if idx % 10 == 0:
            print(f'{idx}/{len(dates)}: {date}')
        try:
            run_daily_guide_for_date(date, mode, max_iter=10)
        except Exception as e:
            print(f'ERROR {date}/{mode}: {e}')

# Update order_history
print('\n=== Updating order_history ===')
updated = 0
for idx, date in enumerate(dates):
    if idx % 10 == 0:
        print(f'{idx}/{len(dates)}: {date}')
    try:
        pos_sim = db.execute(
            "SELECT result FROM sim_guides WHERE date=? AND json_extract(result, '$.mode')='positive' ORDER BY id DESC LIMIT 1",
            (date,)
        ).fetchone()
        neg_sim = db.execute(
            "SELECT result FROM sim_guides WHERE date=? AND json_extract(result, '$.mode')='negative' ORDER BY id DESC LIMIT 1",
            (date,)
        ).fetchone()
        if pos_sim and neg_sim:
            pos_res = json.loads(pos_sim[0])
            neg_res = json.loads(neg_sim[0])
            stores_data = []
            for c in pos_res.get('consensus', []):
                caps = c.get('caps', {})
                stores_data.append({'store': c['store'], 'capital': max(caps.values()) if caps else 0, 'mode': 'positive'})
            for c in neg_res.get('consensus', []):
                caps = c.get('caps', {})
                stores_data.append({'store': c['store'], 'capital': max(caps.values()) if caps else 0, 'mode': 'negative'})
            store_order = {s: i for i, s in enumerate(STORE_NAMES)}
            stores_data.sort(key=lambda x: store_order.get(x['store'], 999))
            save_order_history(date, stores_data)
            updated += 1
    except Exception as e:
        print(f'ERROR order {date}: {e}')

db.close()
print(f'\nDONE: {updated} updated')

"""快速重跑 sim_guides"""
import sys, os
sys.path.insert(0, '/home/xiaolin/projects/funds-v2/backend')
os.chdir('/home/xiaolin/projects/funds-v2/backend')

from simulate import run_daily_guide_for_date
import sqlite3

FUNDS_DB = '/home/xiaolin/projects/funds-v2/backend/funds-v2.db'
RANKING_CAT = 'cat_1783487972049'

db = sqlite3.connect(FUNDS_DB)
dates = [r[0] for r in db.execute(
    "SELECT DISTINCT date FROM records WHERE category=? AND date >= '2026-04-10' ORDER BY date",
    (RANKING_CAT,)
).fetchall()]
db.close()

print(f"Dates: {len(dates)}, {dates[0]} ~ {dates[-1]}", flush=True)

for i, bet_date in enumerate(dates[1:], 1):
    for mode in ["positive", "negative"]:
        try:
            result = run_daily_guide_for_date(bet_date, mode, max_iter=10)
            if isinstance(result, dict) and "error" in result:
                print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: ! {result['error']}", flush=True)
            else:
                voted = [c["store"] for c in result.get("consensus", []) if c.get("votes", 0) > 0]
                print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: {len(voted)} {', '.join(voted)}", flush=True)
        except Exception as e:
            print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: EXCEPTION {e}", flush=True)
            import traceback; traceback.print_exc()

db2 = sqlite3.connect(FUNDS_DB)
cnt = db2.execute("SELECT COUNT(*) FROM sim_guides").fetchone()[0]
db2.close()
print(f"\nDone! sim_guides: {cnt} records", flush=True)

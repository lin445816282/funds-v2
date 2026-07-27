"""重新生成 sim_guides：从 2025-10-01 开始"""
import sys, os, json, sqlite3

sys.path.insert(0, '/home/xiaolin/projects/funds-v2/backend')
os.chdir('/home/xiaolin/projects/funds-v2/backend')

from simulate import run_daily_guide_for_date

FUNDS_DB = '/home/xiaolin/projects/funds-v2/backend/funds-v2.db'
RANKING_CAT = "cat_1783487972049"
START_DATE = "2025-10-01"

# Clean
db = sqlite3.connect(FUNDS_DB)
sg = db.execute("SELECT COUNT(*) FROM sim_guides").fetchone()[0]
oh = db.execute("SELECT COUNT(*) FROM order_history").fetchone()[0]
odr = db.execute("SELECT COUNT(*) FROM order_daily_results").fetchone()[0]
db.execute("DELETE FROM sim_guides")
db.execute("DELETE FROM order_history")
db.execute("DELETE FROM order_daily_results")
db.commit()
print(f"清理: sim_guides {sg}->0, order_history {oh}->0, order_daily_results {odr}->0", flush=True)

dates = [r[0] for r in db.execute(
    "SELECT DISTINCT date FROM records WHERE category=? AND date >= ? ORDER BY date",
    (RANKING_CAT, START_DATE)
).fetchall()]
db.close()

total = (len(dates) - 1) * 2
print(f"{len(dates)}天 ({dates[0]} ~ {dates[-1]}), {total}条", flush=True)

count = 0
errors = 0
for i, bet_date in enumerate(dates[1:], 1):
    for mode in ["positive", "negative"]:
        count += 1
        try:
            result = run_daily_guide_for_date(bet_date, mode, max_iter=10)
            if isinstance(result, dict) and "error" in result:
                errors += 1
                print(f"[{count}/{total}] {bet_date} {mode}: !{result['error']}", flush=True)
            else:
                voted = [c["store"] for c in result.get("consensus", []) if c.get("votes", 0) > 0]
                caps = {c["store"]: max(c.get("caps", {}).values()) if c.get("caps") else 0 for c in result.get("consensus", [])}
                cap_str = " ".join([f"{s}({caps.get(s,0)})" for s in voted])
                print(f"[{count}/{total}] {bet_date} {mode}: {len(voted)}家 {cap_str}", flush=True)
        except Exception as e:
            errors += 1
            print(f"[{count}/{total}] {bet_date} {mode}: !!{e}", flush=True)

db = sqlite3.connect(FUNDS_DB)
cnt = db.execute("SELECT COUNT(*) FROM sim_guides").fetchone()[0]
by_month = db.execute("SELECT substr(date,1,7) as m, COUNT(*) as c FROM sim_guides GROUP BY m ORDER BY m").fetchall()
db.close()

print(f"\nDONE {cnt}/{total} errors={errors}", flush=True)
for m, c in by_month:
    print(f"  {m}: {c}", flush=True)

"""重新生成 sim_guides：下注日=最新有排位日，排位来自前一日"""
import sys, os, json, sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

FUNDS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "funds-v2.db")
RANKING_CAT = "cat_1783487972049"

# 1. 删除全部
db = sqlite3.connect(FUNDS_DB)
cnt = db.execute("SELECT COUNT(*) FROM sim_guides").fetchone()[0]
print(f"删除前: {cnt}条")
db.execute("DELETE FROM sim_guides")
db.commit()

# 获取所有有排位的日期
dates = [r[0] for r in db.execute(
    "SELECT DISTINCT date FROM records WHERE category=? ORDER BY date", (RANKING_CAT,)
).fetchall()]
db.close()
print(f"有排位的日期: {len(dates)}天, {dates[0]} ~ {dates[-1]}")

# 2. 逐日生成
from simulate import run_daily_guide_for_date

for i, bet_date in enumerate(dates[1:], 1):
    pred_date = dates[i-1]
    for mode in ["positive", "negative"]:
        try:
            result = run_daily_guide_for_date(bet_date, mode, max_iter=1)
            if isinstance(result, dict) and "error" in result:
                print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: ⚠{result['error']}")
            else:
                voted = [c["store"] for c in result.get("consensus", []) if c.get("votes", 0) > 0]
                print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: {len(voted)}家 {', '.join(voted)}")
        except Exception as e:
            print(f"[{i}/{len(dates)-1}] {bet_date} {mode}: 异常 {e}")

db = sqlite3.connect(FUNDS_DB)
cnt = db.execute("SELECT COUNT(*) FROM sim_guides").fetchone()[0]
db.close()
print(f"\n完成! 共 {cnt} 条")

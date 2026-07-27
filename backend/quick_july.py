"""快速重算 2026-07 sim_guides"""
import sys, os
sys.path.insert(0, '/home/xiaolin/projects/funds-v2/backend')
os.chdir('/home/xiaolin/projects/funds-v2/backend')

from simulate import run_daily_guide_for_date

dates = [f"2026-07-{d:02d}" for d in range(1, 28)]
total = len(dates) * 2
print(f"7月 {len(dates)}天, {total}条", flush=True)

count = 0
for bet_date in dates:
    for mode in ["positive", "negative"]:
        count += 1
        try:
            result = run_daily_guide_for_date(bet_date, mode, max_iter=10)
            if isinstance(result, dict) and "error" in result:
                print(f"[{count}/{total}] {bet_date} {mode}: !{result['error']}", flush=True)
            else:
                voted = [c["store"] for c in result.get("consensus", []) if c.get("votes", 0) > 0]
                caps = {c["store"]: max(c.get("caps", {}).values()) if c.get("caps") else 0 for c in result.get("consensus", [])}
                cap_str = " ".join([f"{s}({caps.get(s,0)})" for s in voted])
                print(f"[{count}/{total}] {bet_date} {mode}: {len(voted)}家 {cap_str}", flush=True)
        except Exception as e:
            print(f"[{count}/{total}] {bet_date} {mode}: !!{e}", flush=True)

print(f"\nDONE", flush=True)

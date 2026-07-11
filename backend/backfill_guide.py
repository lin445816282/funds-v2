"""
回填历史下单指南 — 逐天补跑正帮扶+反帮扶，严禁未来数据泄露
每期只用 <= 当天的数据做优化和预测
"""
import sys, json, sqlite3
from datetime import datetime, date, timedelta

# 设置路径
sys.path.insert(0, '/home/xiaolin/projects/funds-v2/backend')

from simulate import (
    FUNDS_DB, WH_DB, STORE_NAMES, RANKING_CAT,
    optimize, _predict_orders, _build_consensus, _save_guide
)

def load_data_until(end_date, days=90):
    """加载数据，最多到 end_date（含），不泄露未来"""
    fv = sqlite3.connect(FUNDS_DB)
    fv.row_factory = sqlite3.Row
    
    # 排位数据：只到 end_date
    rankings = {}
    rows = fv.execute("""
        SELECT store, date, amount FROM records 
        WHERE category=? AND date >= '2026-04-11' AND date <= ?
        ORDER BY date, store
    """, (RANKING_CAT, end_date)).fetchall()
    
    seen = set()
    for r in rows:
        key = (r["store"], r["date"])
        if key not in seen:
            seen.add(key)
            d = r["date"]
            if d not in rankings:
                rankings[d] = {}
            rankings[d][r["store"]] = int(r["amount"])
    
    # 收入数据：只到 end_date
    store_income = {}
    rows_i = fv.execute("""
        SELECT date, store, amount FROM records 
        WHERE category='income' AND date >= '2026-04-11' AND date <= ?
        ORDER BY date, store
    """, (end_date,)).fetchall()
    for r in rows_i:
        d = r["date"]
        if d not in store_income:
            store_income[d] = {}
        store_income[d][r["store"]] = r["amount"] or 0
    fv.close()
    
    # 抽签数据：只到 end_date
    wh = sqlite3.connect(WH_DB)
    wh.row_factory = sqlite3.Row
    draws = {}
    rows_d = wh.execute("""
        SELECT date, draw_number FROM analysis_daily 
        WHERE project_id=19 AND date >= '2026-04-11' AND date <= ?
        ORDER BY date
    """, (end_date,)).fetchall()
    for r in rows_d:
        draws[r["date"]] = r["draw_number"]
    wh.close()
    
    # 合并
    dates = sorted(set(list(rankings.keys()) + list(draws.keys())))
    result = []
    for d in dates:
        result.append({
            "date": d,
            "draw": draws.get(d, 0),
            "rankings": rankings.get(d, {}),
            "income": store_income.get(d, {}),
        })
    
    # 只取最近 days 天
    if len(result) > days:
        result = result[-days:]
    
    return result


def run_guide_for_date(target_date, mode):
    """对指定日期跑一种模式的下单指南"""
    data = load_data_until(target_date)
    if not data:
        return {"error": f"无数据: {target_date}"}
    
    # 找目标日期（必须有开奖结果）
    last_day = None
    for d in reversed(data):
        if d["date"] == target_date and d.get("draw", 0) > 0 and d.get("rankings"):
            last_day = d
            break
    if not last_day:
        return {"error": f"{target_date} 无有效开奖结果"}
    
    today_rankings = last_day["rankings"]
    today_date = last_day["date"]
    
    algorithms = [
        ("coordinate", "坐标下降"),
        ("uniform", "等权统一"),
        ("positive_only", "仅正向出手"),
        ("stop_neg2", "连亏止损"),
    ]
    
    algo_results = []
    for algo_key, algo_name in algorithms:
        try:
            params, result = optimize(data, mode, algo_key)
            orders, detail = _predict_orders(today_rankings, params)
            algo_results.append({
                "name": algo_name, "key": algo_key,
                "profit": result["total_profit"],
                "params": {s: params[s] for s in STORE_NAMES},
                "orders": orders,
                "detail": detail,
            })
        except Exception as e:
            print(f"  ⚠ {algo_name} 失败: {e}")
    
    if not algo_results:
        return {"error": f"{target_date} 所有算法失败"}
    
    consensus = _build_consensus(algo_results)
    
    result = {
        "date": today_date,
        "mode": mode,
        "today_rankings": today_rankings,
        "algorithms": algo_results,
        "consensus": consensus,
    }
    
    _save_guide(result)
    return result


def main():
    mode_labels = {"positive": "正帮扶", "negative": "反帮扶"}
    
    # 6-12 到 7-9（7-10/7-11 已有）
    start = date(2026, 6, 12)
    end = date(2026, 7, 9)
    
    current = start
    success = 0
    fail = 0
    
    while current <= end:
        d_str = current.strftime("%Y-%m-%d")
        for mode in ["positive", "negative"]:
            label = mode_labels[mode]
            print(f"\n[{d_str}] {label}...", end=" ", flush=True)
            result = run_guide_for_date(d_str, mode)
            if "error" in result:
                print(f"❌ {result['error']}")
                fail += 1
            else:
                profit = sum(a["profit"] for a in result["algorithms"]) / len(result["algorithms"])
                print(f"✅ 均利={profit:.0f}万")
                success += 1
        current += timedelta(days=1)
    
    print(f"\n{'='*40}")
    print(f"完成: 成功 {success}, 失败 {fail}")


if __name__ == "__main__":
    main()

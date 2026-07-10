"""
总部出手模拟引擎 — 坐标下降法优化每店参数
数据源：funds-v2.db（排位） + warehouse.db（抽签号）
"""
import os, random, sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDS_DB = os.path.join(BASE_DIR, "funds-v2.db")
WH_DB = "/home/xiaolin/projects/number-warehouse/backend/data/warehouse.db"

# ── 常量 ────────────────────────────────────
CAPITAL_OPTIONS = [10, 20, 40]       # 配资档位（万）
MODE_OPTIONS = ["positive", "negative"]  # 正=排位≤阈值出手，负=排位>阈值出手
STORE_NAMES = ["一店","二店","三店","四店","五店","六店","集合14","集合16"]
RANKING_CAT = "cat_1783487972049"  # 排位(汇总)分类ID

# ═══════════════ 数据加载 ═══════════════════
def load_data(days=90):
    """返回 {date: {draw: int, rankings: {store: rank}}} 列表，按日期排序"""
    fv = sqlite3.connect(FUNDS_DB)
    fv.row_factory = sqlite3.Row
    
    # 获取排位数据
    rankings = {}
    rows = fv.execute("""
        SELECT store, date, amount FROM records 
        WHERE category=? AND date >= '2026-04-11' AND date <= '2026-07-09'
        ORDER BY date, store
    """, (RANKING_CAT,)).fetchall()
    
    # 去重：同一store+date取最新的一条
    seen = set()
    for r in rows:
        key = (r["store"], r["date"])
        if key not in seen:
            seen.add(key)
            d = r["date"]
            if d not in rankings:
                rankings[d] = {}
            rankings[d][r["store"]] = int(r["amount"])
    fv.close()
    
    # 获取抽签数据
    wh = sqlite3.connect(WH_DB)
    wh.row_factory = sqlite3.Row
    draws = {}
    rows_d = wh.execute("""
        SELECT date, draw_number FROM analysis_daily 
        WHERE project_id=19 AND date >= '2026-04-11' AND date <= '2026-07-09'
        ORDER BY date
    """).fetchall()
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
            "rankings": rankings.get(d, {})
        })
    return result


# ═══════════════ 内层模拟 ═══════════════════
def simulate_one_day(day_data, store_params, shots_per_day=3):
    """
    单日模拟：过滤达标店 → 按capital降序选≤3家 → 计算盈亏
    返回 (profit, shot_count, hits)
    """
    draw = day_data["draw"]
    rankings = day_data["rankings"]
    
    # 过滤达标店
    qualified = []
    for s in STORE_NAMES:
        rank = rankings.get(s)
        if rank is None:
            continue
        params = store_params.get(s, {})
        threshold = params.get("threshold", 25)
        mode = params.get("mode", "positive")
        capital = params.get("capital", 10)
        
        if mode == "positive":
            if rank <= threshold:
                qualified.append((s, capital))
        else:  # negative
            if rank > threshold:
                qualified.append((s, capital))
    
    # 按capital降序 → 取前3
    qualified.sort(key=lambda x: -x[1])
    selected = qualified[:shots_per_day]
    
    if not selected:
        return 0, 0, 0, []
    
    profit = 0
    hits = 0
    shot_details = []
    for s, cap in selected:
        if draw <= 25:
            profit += cap * 25 * 0.88  # 命中
            hits += 1
            shot_details.append({"store": s, "capital": cap, "hit": True})
        else:
            profit -= cap * 25  # 未中
            shot_details.append({"store": s, "capital": cap, "hit": False})
    
    return round(profit, 2), len(selected), hits, shot_details


def simulate_full(store_params, data, shots_per_day=3, stop_on_neg2=False):
    """
    全量90天模拟
    返回 {total_profit, total_shots, total_hits, daily: [...]}
    stop_on_neg2: True=连续两天负收益则停止（负负则结束）
    """
    total_profit = 0
    total_shots = 0
    total_hits = 0
    daily = []
    max_drawdown_val = 0
    peak = 0
    neg_streak = 0  # 连续负天数
    
    for day_data in data:
        profit, shots, hits, shot_details = simulate_one_day(day_data, store_params, shots_per_day)
        
        # 负负则结束（连续两天亏损）
        if stop_on_neg2:
            if profit < 0:
                neg_streak += 1
                if neg_streak >= 2:
                    # 本日出局：计入后切断
                    total_profit += profit
                    total_shots += shots
                    total_hits += hits
                    daily.append({
                        "date": day_data["date"],
                        "draw": day_data["draw"],
                        "profit": profit,
                        "shots": shots,
                        "hits": hits,
                        "shot_details": shot_details
                    })
                    daily.append({"__stopped__": True, "__reason__": "连续两天负收益，止损出局"})
                    break
            else:
                neg_streak = 0
        
        total_profit += profit
        total_shots += shots
        total_hits += hits
        
        # 跟踪最大回撤
        if total_profit > peak:
            peak = total_profit
        dd = peak - total_profit
        if dd > max_drawdown_val:
            max_drawdown_val = dd
        
        daily.append({
            "date": day_data["date"],
            "draw": day_data["draw"],
            "profit": profit,
            "shots": shots,
            "hits": hits,
            "shot_details": shot_details
        })
    
    return {
        "total_profit": round(total_profit, 2),
        "total_shots": total_shots,
        "total_hits": total_hits,
        "hit_rate": round(total_hits / total_shots * 100, 1) if total_shots else 0,
        "max_drawdown": round(max_drawdown_val, 2),
        "daily": daily
    }


# ═══════════════ 外层优化 ═══════════════
def optimize(data, mode="positive", algorithm="coordinate", max_iter=10):
    """优化8店参数，支持4种算法"""
    if algorithm == "uniform":
        return _uniform_optimize(data, mode)
    elif algorithm == "positive_only":
        return _positive_only_optimize(data, mode, max_iter)
    elif algorithm == "stop_neg2":
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=True)
    else:  # coordinate
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=False)


def _coordinate_optimize(data, mode, max_iter=10, stop_on_neg2=False):
    """坐标下降法优化8店参数"""
    # 初始化随机参数
    store_params = {}
    for s in STORE_NAMES:
        store_params[s] = {
            "threshold": random.randint(1, 49),
            "capital": random.choice(CAPITAL_OPTIONS),
            "mode": mode
        }
    
    best_result = _sim_with_opt(store_params, data, stop_on_neg2)
    best_total = best_result["total_profit"]
    
    for iteration in range(max_iter):
        improved = False
        for s in STORE_NAMES:
            best_for_store = store_params[s].copy()
            best_store_profit = best_total
            
            # 遍历该店的294种组合
            for t in range(1, 50):
                for c in CAPITAL_OPTIONS:
                    for m in MODE_OPTIONS:
                        store_params[s] = {"threshold": t, "capital": c, "mode": m}
                        result = _sim_with_opt(store_params, data, stop_on_neg2)
                        if result["total_profit"] > best_store_profit:
                            best_store_profit = result["total_profit"]
                            best_for_store = {"threshold": t, "capital": c, "mode": m}
                            improved = True
            
            # 恢复最佳参数
            store_params[s] = best_for_store
            best_total = best_store_profit
        
        if not improved:
            break
    
    return store_params, best_result


def _uniform_optimize(data, mode):
    """等权参数：8店用相同的阈值+配资，暴力搜最优"""
    best_params = None
    best_result = None
    best_profit = -float("inf")
    
    for t in range(1, 50):
        for c in CAPITAL_OPTIONS:
            for m in MODE_OPTIONS:
                sp = {s: {"threshold": t, "capital": c, "mode": m} for s in STORE_NAMES}
                result = simulate_full(sp, data)
                if result["total_profit"] > best_profit:
                    best_profit = result["total_profit"]
                    best_result = result
                    best_params = sp
    
    return best_params, best_result


def _positive_only_optimize(data, mode, max_iter=10):
    """仅正帮扶：限制mode=positive，坐标下降"""
    store_params = {}
    for s in STORE_NAMES:
        store_params[s] = {
            "threshold": random.randint(1, 49),
            "capital": random.choice(CAPITAL_OPTIONS),
            "mode": "positive"
        }
    
    best_result = simulate_full(store_params, data)
    best_total = best_result["total_profit"]
    
    for iteration in range(max_iter):
        improved = False
        for s in STORE_NAMES:
            best_for_store = store_params[s].copy()
            best_store_profit = best_total
            
            for t in range(1, 50):
                for c in CAPITAL_OPTIONS:
                    store_params[s] = {"threshold": t, "capital": c, "mode": "positive"}
                    result = simulate_full(store_params, data)
                    if result["total_profit"] > best_store_profit:
                        best_store_profit = result["total_profit"]
                        best_for_store = {"threshold": t, "capital": c, "mode": "positive"}
                        improved = True
            
            store_params[s] = best_for_store
            best_total = best_store_profit
        
        if not improved:
            break
    
    return store_params, best_result


def _sim_with_opt(store_params, data, stop_on_neg2=False):
    """条件模拟——stop_on_neg2时用负负止损"""
    if stop_on_neg2:
        return simulate_full(store_params, data, stop_on_neg2=True)
    return simulate_full(store_params, data)


# ═══════════════ API：模拟数据 ═══════════════
def get_simulate_data(days=90):
    data = load_data(days)
    return {
        "days": len(data),
        "date_range": f"{data[0]['date']} ~ {data[-1]['date']}" if data else "",
        "stores": STORE_NAMES,
        "data": data
    }


# ═══════════════ API：跑优化 ═══════════════
def run_optimize(days=90, mode="positive", algorithm="coordinate"):
    data = load_data(days)
    params, result = optimize(data, mode, algorithm)
    
    # 计算每店真实贡献（从全量模拟的daily记录中统计）
    store_contrib = {s: {"shots":0, "hits":0, "profit":0} for s in STORE_NAMES}
    for day in result["daily"]:
        for shot in day.get("shot_details", []):
            sn = shot.get("store")
            if sn in store_contrib:
                store_contrib[sn]["shots"] += 1
                if shot.get("hit"):
                    store_contrib[sn]["hits"] += 1
                    store_contrib[sn]["profit"] += shot.get("capital", 0) * 25 * 0.88
                else:
                    store_contrib[sn]["profit"] -= shot.get("capital", 0) * 25
    
    store_details = []
    for s in STORE_NAMES:
        p = params[s]
        sc = store_contrib[s]
        store_details.append({
            "name": s,
            "threshold": p["threshold"],
            "capital": p["capital"],
            "mode": p["mode"],
            "qualified_days": sc["shots"],
            "hits": sc["hits"],
            "estimated_profit": round(sc["profit"], 2)
        })
    
    return {
        "mode": mode,
        "params": {s: params[s] for s in STORE_NAMES},
        "result": {
            "total_profit": result["total_profit"],
            "total_shots": result["total_shots"],
            "total_hits": result["total_hits"],
            "hit_rate": result["hit_rate"],
            "max_drawdown": result["max_drawdown"]
        },
        "store_details": store_details,
        "daily": result["daily"]
    }


# ═══════════════ API：手动模拟 ═══════════════
def run_manual(stores_config, days=90, algorithm=None):
    """
    stores_config: [{"name":"一店","threshold":20,"capital":10,"mode":"positive"}, ...]
    """
    data = load_data(days)
    store_params = {}
    for sc in stores_config:
        store_params[sc["name"]] = {
            "threshold": sc.get("threshold", 25),
            "capital": sc.get("capital", 10),
            "mode": sc.get("mode", "positive")
        }
    stop_neg2 = (algorithm == "stop_neg2")
    result = simulate_full(store_params, data, stop_on_neg2=stop_neg2)
    return {
        "result": {
            "total_profit": result["total_profit"],
            "total_shots": result["total_shots"],
            "total_hits": result["total_hits"],
            "hit_rate": result["hit_rate"],
            "max_drawdown": result["max_drawdown"]
        },
        "daily": result["daily"]
    }


def store_params_to_dict(sp):
    return {s: sp[s] if isinstance(sp[s], dict) else sp[s] for s in STORE_NAMES}

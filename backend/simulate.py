"""
总部出手模拟引擎 — 坐标下降法优化每店参数
数据源：funds-v2.db（排位） + warehouse.db（抽签号）
"""
import os, random, sqlite3, json
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FUNDS_DB = os.path.join(BASE_DIR, "funds-v2.db")
WH_DB = "/home/xiaolin/projects/number-warehouse/backend/data/warehouse.db"

# ── 常量 ────────────────────────────────────
CAPITAL_OPTIONS = [10, 20, 40]       # 配资档位（万）
THRESHOLD_OPTIONS = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45]
NEG_THRESHOLD_OPTIONS = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45]
MODE_OPTIONS = ["positive", "negative"]
STORE_NAMES = ["一店","二店","三店","四店","五店","六店","集合14","集合16"]
RANKING_CAT = "cat_1783487972049"
# 盈亏倍数：正帮扶买25个号(中+22,不中-25)，反帮扶买24个号(中+23,不中-24)
HIT_MULT = {"positive": 22, "negative": 23}
MISS_MULT = {"positive": 25, "negative": 24}

# ═══════════════ 数据加载 ═══════════════════
def load_data(days=90):
    """返回 {date: {draw: int, rankings: {store: rank}}} 列表，按日期排序"""
    from datetime import datetime, timedelta
    from dateutil.relativedelta import relativedelta
    
    fv = sqlite3.connect(FUNDS_DB)
    fv.row_factory = sqlite3.Row
    
    # 动态计算起始日期：最新排位日往前3个月
    latest_row = fv.execute(
        "SELECT MAX(date) FROM records WHERE category=?", (RANKING_CAT,)
    ).fetchone()
    latest_date = latest_row[0] if latest_row and latest_row[0] else None
    if latest_date:
        dt = datetime.strptime(latest_date, "%Y-%m-%d")
        start_dt = dt - relativedelta(months=3)
        start_date = start_dt.strftime("%Y-%m-%d")
    else:
        start_date = '2026-04-11'  # fallback
    
    # 获取排位数据
    rankings = {}
    rows = fv.execute("""
        SELECT store, date, amount FROM records 
        WHERE category=? AND date >= ?
        ORDER BY date, store
    """, (RANKING_CAT, start_date)).fetchall()
    
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
    # 获取各店实际收入（盈亏判定用）
    store_income = {}
    rows_i = fv.execute("""
        SELECT date, store, amount FROM records 
        WHERE category='income' AND date >= ?
        ORDER BY date, store
    """, (start_date,)).fetchall()
    for r in rows_i:
        d = r["date"]
        if d not in store_income:
            store_income[d] = {}
        store_income[d][r["store"]] = r["amount"] or 0
    fv.close()
    
    # 获取抽签数据
    wh = sqlite3.connect(WH_DB)
    wh.row_factory = sqlite3.Row
    draws = {}
    rows_d = wh.execute("""
        SELECT date, draw_number FROM analysis_daily 
        WHERE project_id=19 AND date >= ?
        ORDER BY date
    """, (start_date,)).fetchall()
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
    return result


# ═══════════════ 内层模拟 ═══════════════════
def simulate_one_day(day_data, store_params, prev_rankings, shots_per_day=3):
    """
    单日模拟：用「前一日」排位过滤达标店 → 按capital降序选≤3家 → 用当天draw计算盈亏
    返回 (profit, shot_count, hits, shot_details, qual_summary)
    qual_summary: [{store,rank,threshold,mode,capital,qualified,selected}] 各店的达标+选中判定过程
    """
    draw = day_data["draw"]
    rankings = prev_rankings or {}
    
    # 构建 qual_summary：逐一记录每家店的判定逻辑
    qual_summary = []
    qualified_list = []
    for s in STORE_NAMES:
        rank = rankings.get(s)
        params = store_params.get(s, {})
        threshold = params.get("threshold", 25)
        mode = params.get("mode", "positive")
        capital = params.get("capital", 10)
        
        qualified = False
        if rank is not None:
            if mode == "positive":
                qualified = (rank <= threshold)
            else:
                qualified = (rank > threshold)
        
        qual_summary.append({
            "store": s, "rank": rank, "threshold": threshold,
            "mode": mode, "capital": capital, "qualified": qualified, "selected": False
        })
        if qualified:
            qualified_list.append((s, capital, rank if rank is not None else 999, mode))
    
    # 排序：配资降序 → 同配资按排位质量（正=排位越低越好，反=排位越高越好）
    qualified_list.sort(key=lambda x: (-x[1], x[2] if x[3] == "positive" else -x[2]))
    selected = qualified_list[:shots_per_day]
    
    # 标记选中
    sel_set = set(s for s, _, _, _ in selected)
    for q in qual_summary:
        if q["store"] in sel_set:
            q["selected"] = True
    
    if not selected:
        return 0, 0, 0, [], qual_summary
    
    profit = 0
    hits = 0
    shot_details = []
    day_income = day_data.get("income", {})
    today_rankings = day_data.get("rankings", {})
    for s, cap, _, _ in selected:
        store_mode = store_params.get(s, {}).get("mode", "positive")
        # 命中由当天排位决定：正帮扶排≤25命中，反帮扶排>25命中
        today_rank = today_rankings.get(s)
        if today_rank is not None:
            base_hit = (today_rank <= 25)
            hit = base_hit if store_mode == "positive" else not base_hit
        else:
            hit = False  # 无排位数据，算失手
        if hit:
            profit += cap * HIT_MULT.get(store_mode, 22)
            hits += 1
            shot_details.append({"store": s, "capital": cap, "hit": True})
        else:
            profit -= cap * MISS_MULT.get(store_mode, 25)
            shot_details.append({"store": s, "capital": cap, "hit": False})
    
    return round(profit, 2), len(selected), hits, shot_details, qual_summary


def simulate_full(store_params, data, shots_per_day=3, stop_on_neg2=False):
    """
    全量90天模拟，每天用前一日排位决策
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
    prev_rankings = None  # 首日无前日数据
    
    for day_data in data:
        profit, shots, hits, shot_details, qual_summary = simulate_one_day(day_data, store_params, prev_rankings, shots_per_day)
        
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
                        "shot_details": shot_details,
                        "qual_summary": qual_summary
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
            "shot_details": shot_details,
            "qual_summary": qual_summary
        })
        # 用当日排位作为明天的决策依据
        prev_rankings = day_data["rankings"]
    
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
    """优化8店参数，支持4种算法（精简版：阈值10档，迭代3轮）"""
    if algorithm == "uniform":
        return _uniform_optimize(data, mode)
    elif algorithm == "positive_only":
        return _positive_only_optimize(data, mode, max_iter)
    elif algorithm == "stop_neg2":
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=True)
    else:  # coordinate
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=False)


def _coordinate_optimize(data, mode, max_iter=10, stop_on_neg2=False):
    """坐标下降 + 5个均匀起点 → 取最优（确定性）"""
    # 5个均匀分布起点：(阈,资)，反帮扶去掉1/5
    starters = [(10,10),(15,20),(25,40),(35,20),(45,10)] if mode == "negative" else [(5,10),(15,20),(25,40),(35,20),(45,10)]
    thresholds = NEG_THRESHOLD_OPTIONS if mode == "negative" else THRESHOLD_OPTIONS
    best_params = None
    best_result = None
    best_profit = -float("inf")
    
    for th0, cap0 in starters:
        store_params = {}
        for s in STORE_NAMES:
            store_params[s] = {"threshold": th0, "capital": cap0, "mode": mode}
        
        result = _sim_with_opt(store_params, data, stop_on_neg2)
        cur_best = result["total_profit"]
        
        for iteration in range(max_iter):
            improved = False
            for s in STORE_NAMES:
                best_for_store = store_params[s].copy()
                best_store_profit = cur_best
                
                # 遍历该店的组合（10阈值×3配资×锁定模式）
                for t in thresholds:
                    for c in CAPITAL_OPTIONS:
                        store_params[s] = {"threshold": t, "capital": c, "mode": mode}
                        result = _sim_with_opt(store_params, data, stop_on_neg2)
                        if result["total_profit"] > best_store_profit:
                            best_store_profit = result["total_profit"]
                            best_for_store = {"threshold": t, "capital": c, "mode": mode}
                            improved = True
                
                # 恢复最佳参数
                store_params[s] = best_for_store
                cur_best = best_store_profit
            
            if not improved:
                break
        
        if cur_best > best_profit:
            best_profit = cur_best
            best_params = {s: store_params[s].copy() for s in STORE_NAMES}
            best_result = _sim_with_opt(store_params, data, stop_on_neg2)  # 用最优参数重跑
    
    return best_params, best_result


def _uniform_optimize(data, mode):
    """等权参数：暴力搜最优后逐店微调阈值（±相邻档），保留个体差异空间"""
    thresholds = NEG_THRESHOLD_OPTIONS if mode == "negative" else THRESHOLD_OPTIONS
    best_params = None
    best_result = None
    best_profit = -float("inf")
    
    # 阶段1：暴力搜统一参数（10×3=30种，锁定模式）
    for t in thresholds:
        for c in CAPITAL_OPTIONS:
            sp = {s: {"threshold": t, "capital": c, "mode": mode} for s in STORE_NAMES}
            result = simulate_full(sp, data)
            if result["total_profit"] > best_profit:
                best_profit = result["total_profit"]
                best_result = result
                best_params = sp
    
    # 阶段2：逐店微调阈值（±相邻档→8×5=40次模拟）
    if best_params:
        opt = best_params[STORE_NAMES[0]]
        cap, best_mode = opt["capital"], opt["mode"]
        base_t = opt["threshold"]
        for store in STORE_NAMES:
            for dt in [-10, -5, 0, 5, 10]:
                t = base_t + dt
                if t not in THRESHOLD_OPTIONS:
                    continue
                sp = {s: dict(best_params[s]) for s in STORE_NAMES}
                sp[store]["threshold"] = t
                result = simulate_full(sp, data)
                if result["total_profit"] > best_profit:
                    best_profit = result["total_profit"]
                    best_result = result
                    best_params = sp
    
    return best_params, best_result


def _positive_only_optimize(data, mode, max_iter=10):
    """仅指定模式：固定5个分布均匀起点 → 坐标下降 → 取最优（确定性）"""
    # 5个均匀分布起点：(阈,资)，反帮扶去掉1/5
    thresholds = NEG_THRESHOLD_OPTIONS if mode == "negative" else THRESHOLD_OPTIONS
    starters = [(10,10),(15,20),(25,40),(35,20),(45,10)] if mode == "negative" else [(5,10),(15,20),(25,40),(35,20),(45,10)]
    best_params = None
    best_result = None
    best_profit = -float("inf")
    
    for th0, cap0 in starters:
        store_params = {}
        for s in STORE_NAMES:
            store_params[s] = {"threshold": th0, "capital": cap0, "mode": mode}
        
        result = simulate_full(store_params, data)
        cur_best = result["total_profit"]
        
        for iteration in range(max_iter):
            improved = False
            for s in STORE_NAMES:
                best_for_store = store_params[s].copy()
                best_store_profit = cur_best
                
                for t in thresholds:
                    for c in CAPITAL_OPTIONS:
                        store_params[s] = {"threshold": t, "capital": c, "mode": mode}
                        result = simulate_full(store_params, data)
                        if result["total_profit"] > best_store_profit:
                            best_store_profit = result["total_profit"]
                            best_for_store = {"threshold": t, "capital": c, "mode": mode}
                            improved = True
                
                store_params[s] = best_for_store
                cur_best = best_store_profit
            
            if not improved:
                break
        
        if cur_best > best_profit:
            best_profit = cur_best
            best_params = {s: store_params[s].copy() for s in STORE_NAMES}
            best_result = simulate_full(store_params, data)  # 用最优参数重跑
    
    return best_params, best_result


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
    
    # 训练与预测分离：优化只用 D-1 前数据，避免偷看最新一天
    last_day = data[-1] if data else None
    train_data = [d for d in data if d["date"] != last_day["date"]] if last_day else data
    params, _ = optimize(train_data, mode, algorithm)
    
    # 用完整数据跑模拟（参数未经最新日训练，但回测覆盖全量）
    stop_neg2 = (algorithm == "stop_neg2")
    result = simulate_full(params, data, stop_on_neg2=stop_neg2)
    
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


# ═══════════════ API：每日下单指南 ═══════════════
def run_daily_guide(days=90, mode="positive", max_iter=10):
    """跑4组优化 + 取最新排位 + 逐店判定 → 投票汇总
    mode: "positive"=正帮扶（排位≤阈值出手）, "negative"=反帮扶（排位>阈值出手）
    max_iter: 坐标下降迭代轮数（3=全精度, 1=快速）
    """
    data = load_data(days)
    if not data:
        return {"error": "无数据"}

    # 找最新有排位日 → 用当日排位预测次日出手
    last_day = None
    for d in reversed(data):
        if d.get("rankings"):
            last_day = d
            break
    if not last_day:
        return {"error": "无排位数据"}

    today_rankings = last_day["rankings"]
    today_date = last_day["date"]

    # ── 关键：优化只用 D-1 之前的数据，D 本身不参与训练（避免数据泄露）──
    train_data = [d for d in data if d.get("date") != today_date]

    # ── 验证：仓库 threshold 号码是否就绪 ──
    import urllib.request
    try:
        url = "http://localhost:8016/api/threshold/results?" + urllib.parse.urlencode({"date": today_date})
        with urllib.request.urlopen(url, timeout=3) as resp:
            th_data = json.loads(resp.read())
        if not th_data.get("items"):
            return {"error": f"仓库{today_date}无threshold号码，请先在数字仓库同步", "ranking_date": today_date}
    except Exception as e:
        return {"error": f"仓库验证失败: {e}", "ranking_date": today_date}

    algorithms = [
        ("coordinate", "坐标下降"),
        ("uniform", "等权统一"),
        ("positive_only", "仅正向出手"),
        ("stop_neg2", "连亏止损"),
    ]

    algo_results = []
    for algo_key, algo_name in algorithms:
        params, result = optimize(train_data, mode, algo_key, max_iter)
        orders, detail = _predict_orders(today_rankings, params)
        algo_results.append({
            "name": algo_name, "key": algo_key,
            "profit": result["total_profit"],
            "params": {s: params[s] for s in STORE_NAMES},
            "orders": orders,
            "detail": detail,
        })

    consensus = _build_consensus(algo_results)

    result = {
        "date": today_date,
        "mode": mode,
        "today_rankings": today_rankings,
        "algorithms": algo_results,
        "consensus": consensus,
    }

    # 保存历史
    _save_guide(result)
    return result


def run_daily_guide_for_date(bet_date, mode="positive", max_iter=10):
    """按指定下注日生成指南：排位来自 bet_date-1 的前一个有排位日，评估用 bet_date"""
    data = load_data(90)
    if not data:
        return {"error": "无数据"}

    # 找 bet_date 前最近的有排位日
    pred_day = None
    for d in reversed(data):
        if d.get("date") < bet_date and d.get("rankings"):
            pred_day = d
            break
    if not pred_day:
        return {"error": f"{bet_date}无前一日排位数据"}

    pred_rankings = pred_day["rankings"]

    # 训练用 bet_date 之前的数据
    train_data = [d for d in data if d.get("date") < bet_date]

    algorithms = [
        ("coordinate", "坐标下降"),
        ("uniform", "等权统一"),
        ("positive_only", "仅正向出手"),
        ("stop_neg2", "连亏止损"),
    ]

    algo_results = []
    for algo_key, algo_name in algorithms:
        params, result_opt = optimize(train_data, mode, algo_key, max_iter)
        orders, detail = _predict_orders(pred_rankings, params)
        algo_results.append({
            "name": algo_name, "key": algo_key,
            "profit": result_opt["total_profit"],
            "params": {s: params[s] for s in STORE_NAMES},
            "orders": orders,
            "detail": detail,
        })

    consensus = _build_consensus(algo_results)

    result = {
        "date": bet_date,
        "mode": mode,
        "pred_rankings": pred_rankings,
        "algorithms": algo_results,
        "consensus": consensus,
    }

    _save_guide(result)
    return result


def _pull_one_date(date_str):
    """拉取单个日期 → 返回 (date, items转列表, 错误信息)"""
    import urllib.request, urllib.parse
    url = "http://localhost:8016/api/threshold/results?" + urllib.parse.urlencode({"date": date_str})
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return None, None, str(e)
    if not data.get("items"):
        return None, None, "无数据"
    return data["date"], data["items"], None


def _save_order_items(date, items):
    """入库单日数据 → 返回写入条数（跳过与已有数据相同的）"""
    db = sqlite3.connect(FUNDS_DB)
    count = 0
    for item in items:
        cid = item["collection_id"]
        sname = item.get("summary_name", "")
        for threshold, nums in item.get("thresholds", {}).items():
            th = int(threshold)
            nums_json = json.dumps(nums["numbers"])
            # 检查是否与已有数据相同
            old = db.execute(
                "SELECT numbers_json FROM order_numbers WHERE date=? AND collection_id=? AND threshold=?",
                (date, cid, th)
            ).fetchone()
            if old and old[0] == nums_json:
                continue  # 完全相同，跳过
            db.execute(
                "INSERT OR REPLACE INTO order_numbers (date, collection_id, threshold, summary_name, numbers_json) VALUES (?,?,?,?,?)",
                (date, cid, th, sname, nums_json)
            )
            count += 1
    db.commit()
    db.close()
    return count


def pull_threshold_numbers(target_date=None, from_date=None, to_date=None):
    """从数字仓库拉取阈值号码 → 入库 order_numbers 表
    target_date: 单日期(YYYY-MM-DD)，不传且无范围则取最新
    from_date/to_date: 日期范围，批量拉取。from_date='auto' 时自动查询仓库最早日期
    """
    # ── auto 模式：查仓库最早可用日期 ──
    if from_date == "auto":
        wh = sqlite3.connect(WH_DB)
        r = wh.execute(
            "SELECT MIN(date) FROM collection_threshold_numbers WHERE collection_id != 0"
        ).fetchone()
        wh.close()
        if r and r[0]:
            from_date = r[0]
        else:
            from datetime import date as dt
            from_date = dt.today().strftime("%Y-%m-%d")
    # ── 范围模式 ──
    if from_date and to_date:
        from datetime import datetime, timedelta
        d = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        total = 0
        dates = []
        errors = []
        while d <= end:
            ds = d.strftime("%Y-%m-%d")
            date, items, err = _pull_one_date(ds)
            if err:
                errors.append(f"{ds}: {err}")
            else:
                n = _save_order_items(date, items)
                total += n
                dates.append(ds)
            d += timedelta(days=1)
        return {
            "ok": True,
            "dates": dates,
            "total_items": total,
            "errors": errors
        }

    # ── 单日期模式 ──
    date, items, err = _pull_one_date(target_date)
    if err:
        return {"ok": False, "error": err}
    count = _save_order_items(date, items)
    return {"ok": True, "date": date, "items": count, "collections": len(items)}


def list_order_numbers(page=1, limit=20):
    """分页查 order_numbers 表，按日期聚合"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row

    total = db.execute(
        "SELECT COUNT(DISTINCT date) FROM order_numbers"
    ).fetchone()[0]

    offset = (page - 1) * limit
    rows = db.execute("""
        SELECT date, COUNT(*) as cnt, MAX(pulled_at) as pulled_at
        FROM order_numbers
        GROUP BY date ORDER BY date DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()

    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "count": r["cnt"],
            "pulled_at": r["pulled_at"] or ""
        })

    db.close()
    return {"ok": True, "total": total, "page": page, "limit": limit, "items": items}


def delete_order_numbers_by_date(date):
    """删除指定日期的所有入库记录"""
    db = sqlite3.connect(FUNDS_DB)
    cursor = db.execute("DELETE FROM order_numbers WHERE date=?", (date,))
    deleted = cursor.rowcount
    db.commit()
    db.close()
    return {"ok": True, "date": date, "deleted": deleted}


def get_order_numbers_detail(date):
    """获取指定日期的所有 order_numbers 记录明细"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM order_numbers WHERE date=? ORDER BY collection_id ASC, threshold ASC",
        (date,)
    ).fetchall()
    items = []
    for r in rows:
        nums = json.loads(r["numbers_json"]) if r["numbers_json"] else []
        items.append({
            "id": r["id"],
            "collection_id": r["collection_id"],
            "threshold": r["threshold"],
            "summary_name": r["summary_name"] or "",
            "numbers": nums,
            "pulled_at": r["pulled_at"] or ""
        })
    db.close()
    return {"ok": True, "date": date, "count": len(items), "items": items}


# ── 门店 → collection_id 映射 ─────────────────
STORE_TO_COLLECTION = {
    "一店": -23, "二店": -24, "三店": -25,
    "四店": -26, "五店": -28, "六店": -29,
    "集合14": 14, "集合16": 16,
}
COLLECTION_TO_STORE = {v: k for k, v in STORE_TO_COLLECTION.items()}

def _load_order_numbers(ranking_date=None):
    """从 order_numbers 表取号码 → 按门店映射返回 {store: {top25:[], bottom24:[]}}
    ranking_date: 排位日期。直接用于 order_numbers.date 查询。
    """
    from datetime import datetime, timedelta

    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row

    target_date = ranking_date
    if target_date:
        # 检查 ranking_date 是否存在
        exists = db.execute(
            "SELECT 1 FROM order_numbers WHERE date=?", (target_date,)
        ).fetchone()
        if not exists:
            target_date = None

    # 回退到最新
    if not target_date:
        row = db.execute(
            "SELECT date FROM order_numbers ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            db.close()
            return {}
        target_date = row["date"]

    rows = db.execute(
        "SELECT collection_id, threshold, numbers_json, pulled_at FROM order_numbers WHERE date=?",
        (target_date,)
    ).fetchall()

    db.close()

    result = {"date": target_date}
    if ranking_date and ranking_date == target_date:
        result["matched"] = True
    pulled_at = None
    for r in rows:
        cid = r["collection_id"]
        th = r["threshold"]
        store = COLLECTION_TO_STORE.get(cid)
        if not store:
            continue
        if store not in result:
            result[store] = {}
        result[store][str(th)] = json.loads(r["numbers_json"])
        if r["pulled_at"] and not pulled_at:
            pulled_at = r["pulled_at"]

    # 补充空门店
    for store in STORE_TO_COLLECTION:
        if store not in result:
            result[store] = {}

    result["pulled_at"] = pulled_at
    return result


def _get_latest_numbers_date():
    """返回 order_numbers 表最新日期，无则返回 None"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT date FROM order_numbers ORDER BY date DESC LIMIT 1").fetchone()
    db.close()
    return row["date"] if row else None


def get_order_sheet(days=90, target_date=None, guide_date=None):
    """合并正反帮扶指南 → 下单表（门店列表 + 1-49空网格）
    优先从 sim_guides 缓存读取，避免重新计算。
    target_date: 指定目标日期，用最新共识 + 该日号码计算金额（不存 DB）。
    guide_date: 指定指南日期，默认用最新。
    """
    from datetime import datetime as dt_module
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row

    def _get_guide(mode):
        """获取指定 mode 的 sim_guide，指定 guide_date 时精确匹配不回落"""
        if guide_date:
            row = db.execute(
                "SELECT date, rankings, result FROM sim_guides WHERE date=? AND json_extract(result, '$.mode')=? ORDER BY id DESC LIMIT 1",
                (guide_date, mode)
            ).fetchone()
            return row  # 找到返回，找不到返回 None → 触发实时计算
        return db.execute(
            "SELECT date, rankings, result FROM sim_guides WHERE json_extract(result, '$.mode')=? ORDER BY date DESC, id DESC LIMIT 1",
            (mode,)
        ).fetchone()

    def extract_stores(consensus):
        stores = []
        for c in consensus:
            caps = c.get("caps", {})
            max_cap = max(caps.values()) if caps else 0
            stores.append({
                "store": c["store"],
                "capital": max_cap,
                "votes": f"{c['votes']}/{c['out_of']}",
                "caps": caps
            })
        store_order = {s: i for i, s in enumerate(STORE_NAMES)}
        stores.sort(key=lambda x: store_order.get(x["store"], 999))
        return stores

    # 从缓存读正反帮扶（优先 guide_date，回退最新）
    pos_row = _get_guide("positive")
    neg_row = _get_guide("negative")
    db.close()

    if pos_row and neg_row:
        pos = json.loads(pos_row["result"])
        neg = json.loads(neg_row["result"])
        pos_stores = extract_stores(pos.get("consensus", []))
        neg_stores = extract_stores(neg.get("consensus", []))
        ranking_date = pos["date"]
        action_date = target_date if target_date else ranking_date
        numbers = _load_order_numbers(action_date)
        # 号码日期 > 共识日期时，优先从 order_history 取实际下单金额
        if action_date != ranking_date:
            amounts_data = _load_amounts_from_order_history(action_date)
            if not amounts_data:
                amounts_data = _build_amounts_data(pos_stores, neg_stores, numbers, ranking_date)
        else:
            amounts_data = _load_amounts_from_db(action_date, ranking_date)
            if not amounts_data:
                amounts_data = _build_amounts_data(pos_stores, neg_stores, numbers, ranking_date)
        numbers_date = action_date if action_date != ranking_date else None
        return {
            "date": ranking_date,
            "guide_date": ranking_date,
            "numbers_date": numbers_date,
            "rankings": pos.get("pred_rankings") or pos.get("today_rankings", {}),
            "cached": True,
            "numbers": numbers,
            "positive": {
                "stores": pos_stores,
                "total_capital": sum(s["capital"] for s in pos_stores)
            },
            "negative": {
                "stores": neg_stores,
                "total_capital": sum(s["capital"] for s in neg_stores)
            },
            "order_amounts": amounts_data
        }

    # 缓存无 → 实时计算（用目标日期窗口避免前视偏差）
    print("[order-sheet] 无缓存，实时计算中...")
    bet_date = target_date if target_date else guide_date if guide_date else None
    pos = run_daily_guide_for_date(bet_date, "positive", max_iter=10) if bet_date else run_daily_guide(days, "positive", max_iter=10)
    neg = run_daily_guide_for_date(bet_date, "negative", max_iter=10) if bet_date else run_daily_guide(days, "negative", max_iter=10)
    pos_stores = extract_stores(pos.get("consensus", []))
    neg_stores = extract_stores(neg.get("consensus", []))
    ranking_date2 = pos.get("date", "")
    action_date2 = target_date if target_date else ranking_date2
    numbers2 = _load_order_numbers(action_date2)
    if target_date:
        amounts_data2 = _build_amounts_data(pos_stores, neg_stores, numbers2, ranking_date2)
    else:
        amounts_data2 = _load_amounts_from_db(action_date2, ranking_date2)
        if not amounts_data2:
            amounts_data2 = _build_amounts_data(pos_stores, neg_stores, numbers2, ranking_date2)
    numbers_date2 = action_date2 if not target_date and action_date2 != ranking_date2 else (target_date if target_date else None)
    return {
        "date": ranking_date2 if ranking_date2 else action_date2,
        "guide_date": ranking_date2 if ranking_date2 else "",
        "numbers_date": numbers_date2,
        "rankings": pos.get("pred_rankings") or pos.get("today_rankings", {}),
        "cached": False,
        "numbers": numbers2,
        "positive": {
            "stores": pos_stores,
            "total_capital": sum(s["capital"] for s in pos_stores)
        },
        "negative": {
            "stores": neg_stores,
            "total_capital": sum(s["capital"] for s in neg_stores)
        },
        "order_amounts": amounts_data2
    }


def _load_amounts_from_db(action_date, ranking_date):
    """从 order_amounts 存档表读取，避免动态重算。返回格式同 _build_amounts_data"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT number, amount FROM order_amounts WHERE date=? AND amount>0 ORDER BY number",
        (action_date,)
    ).fetchall()
    day_index = db.execute("SELECT COUNT(DISTINCT date) FROM order_amounts").fetchone()[0]
    db.close()
    if not rows:
        return None  # 无存档，需实时计算
    amounts = {}
    for r in rows:
        amounts[r["number"]] = r["amount"]
    total = sum(amounts.values())
    return {"date": ranking_date, "action_date": action_date, "amounts": amounts, "day_index": day_index, "total": total}


def _load_amounts_from_order_history(action_date):
    """从 order_history 的 amounts_json 读取已下单金额。返回格式同 _build_amounts_data"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT amounts_json FROM order_history WHERE action_date=? AND amounts_json IS NOT NULL ORDER BY id DESC LIMIT 1",
        (action_date,)
    ).fetchone()
    db.close()
    if not row or not row["amounts_json"]:
        return None
    try:
        amounts = json.loads(row["amounts_json"])
    except:
        return None
    total = sum(amounts.values())
    return {"date": action_date, "action_date": action_date, "amounts": amounts, "day_index": 0, "total": total}


def _build_amounts_data(pos_stores, neg_stores, numbers, ranking_date):
    """根据正反帮扶门店 → 累计1-49号码金额 → 写入 order_amounts"""
    from datetime import datetime, timedelta
    action_date = ranking_date
    
    num_amounts = {}
    # 正帮扶 → Top25
    for s in pos_stores:
        store_nums = numbers.get(s["store"], {})
        top25 = store_nums.get("25", [])
        if top25 and s["capital"]:
            for n in top25:
                num_amounts[n] = num_amounts.get(n, 0) + s["capital"]
    # 反帮扶 → Bottom24
    for s in neg_stores:
        store_nums = numbers.get(s["store"], {})
        bot24 = store_nums.get("24", [])
        if bot24 and s["capital"]:
            for n in bot24:
                num_amounts[n] = num_amounts.get(n, 0) + s["capital"]
    
    # 写入 order_amounts 表
    db = sqlite3.connect(FUNDS_DB)
    db.execute("DELETE FROM order_amounts WHERE date=?", (action_date,))
    for n in range(1, 50):
        amt = num_amounts.get(n, 0)
        db.execute(
            "INSERT OR REPLACE INTO order_amounts (date, number, amount) VALUES (?,?,?)",
            (action_date, n, amt)
        )
    db.commit()
    # 日期序号 = 已有多少天数记录
    day_index = db.execute("SELECT COUNT(DISTINCT date) FROM order_amounts").fetchone()[0]
    db.close()
    
    total = sum(num_amounts.values())
    return {"date": ranking_date, "action_date": action_date, "amounts": num_amounts, "day_index": day_index, "total": total}


def _save_guide(result):
    """保存到 sim_guides 表 — 自动去重：先删同日期+同mode旧记录再插入"""
    try:
        db = sqlite3.connect(FUNDS_DB)
        rankings = result.get("pred_rankings") or result.get("today_rankings", {})
        mode = result.get("mode", "")
        date = result["date"]
        # 去重：删掉同一日期+同一mode的旧记录
        db.execute(
            "DELETE FROM sim_guides WHERE date=? AND json_extract(result, '$.mode')=?",
            (date, mode)
        )
        db.execute(
            "INSERT INTO sim_guides (date, rankings, result) VALUES (?, ?, ?)",
            (date, json.dumps(rankings), json.dumps(result, ensure_ascii=False))
        )
        db.commit()
        db.close()
    except Exception as e:
        print(f"[_save_guide] 保存失败: {e}")


def get_guide_history(limit=20, mode=None, offset=0):
    """获取历史下单指南 — 按(日期,mode)去重，支持mode过滤，附当天/累计结果值
    返回 {"rows": [...], "total": N}"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, date, rankings, result, created_at FROM sim_guides ORDER BY id DESC"
    ).fetchall()
    db.close()

    # 按(date, effective_mode)去重取最新
    seen = set()
    deduped = []
    for r in rows:
        d = r["date"]
        try:
            res = json.loads(r["result"])
        except Exception:
            res = {}
        em = res.get("mode")
        if em is None:
            # 旧数据：推断mode用于去重key
            try:
                rankings = json.loads(r["rankings"])
            except Exception:
                rankings = {}
            consensus = res.get("consensus", [])
            voted = [c for c in consensus if c.get("votes", 0) > 0]
            gt25 = sum(1 for c in voted if rankings.get(c["store"], 0) > 25)
            em = "negative" if gt25 > len(voted) / 2 else "positive"
        if mode and em != mode:
            continue
        key = (d, em)
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # 按日期降序排列，不受插入ID顺序影响
    deduped.sort(key=lambda r: r["date"], reverse=True)
    total = len(deduped)
    # 分页切片
    deduped = deduped[offset:offset+limit]
    dates = [r["date"] for r in deduped]

    # 评估日期 = 下注日（sim_guides.date就是下注日，排位来自前一日）
    guide_to_eval = {d: d for d in dates}  # guide_date -> eval_date = 同一天
    eval_dates = dates

    # 批量查各店实际盈亏 + draw + 排位（都用评估日期=下注日）
    store_amounts = {}  # guide_date -> {store: amount}
    draws = {}  # guide_date -> draw_number（下注日的抽签，决定盈亏）
    eval_rankings = {}  # guide_date -> {store: rank}（用评估日的排位做命中判定）
    store_numbers = {}  # date -> {store: {"positive": set(), "negative": set()}}  精准命中判定用
    if eval_dates:
        db = sqlite3.connect(FUNDS_DB)
        db.row_factory = sqlite3.Row
        placeholders = ",".join(["?" for _ in eval_dates])
        rows = db.execute(
            f"SELECT date, store, amount FROM records WHERE date IN ({placeholders}) AND category='income'",
            eval_dates
        ).fetchall()
        db.close()
        for r in rows:
            eval_date = r["date"]
            for gd, ed in guide_to_eval.items():
                if ed == eval_date:
                    store_amounts.setdefault(gd, {})[r["store"]] = r["amount"]

        # 加载评估日的排位数据（用于命中判定）— 从 sim_guides.rankings 取（pred_rankings=前一日），不取当天实际排位
        db2 = sqlite3.connect(FUNDS_DB)
        db2.row_factory = sqlite3.Row
        rank_rows = db2.execute(
            f"SELECT date, rankings FROM sim_guides WHERE date IN ({placeholders}) ORDER BY id DESC",
            eval_dates
        ).fetchall()
        db2.close()
        for rr in rank_rows:
            eval_date = rr["date"]
            for gd, ed in guide_to_eval.items():
                if ed == eval_date and gd not in eval_rankings:
                    try:
                        eval_rankings[gd] = json.loads(rr["rankings"] or "{}")
                    except:
                        eval_rankings[gd] = {}

        # draw 用评估日（下注日）的抽签，无数据时fallback到指南日
        wh = sqlite3.connect(WH_DB)
        wh.row_factory = sqlite3.Row
        all_draw_dates = list(set(eval_dates + dates))
        dp = ",".join(["?" for _ in all_draw_dates])
        rows_d = wh.execute(
            f"SELECT date, draw_number FROM analysis_daily WHERE project_id=19 AND date IN ({dp})",
            all_draw_dates
        ).fetchall()
        wh.close()
        draw_lookup = {}
        for r in rows_d:
            draw_lookup[r["date"]] = r["draw_number"]
        for gd, ed in guide_to_eval.items():
            draws[gd] = draw_lookup.get(ed) or draw_lookup.get(gd)

        # 加载号码数据用于精准命中判定
        db3 = sqlite3.connect(FUNDS_DB)
        db3.row_factory = sqlite3.Row
        num_rows = db3.execute(
            f"SELECT date, collection_id, threshold, numbers_json FROM order_numbers WHERE date IN ({placeholders})",
            eval_dates
        ).fetchall()
        db3.close()
        for nr in num_rows:
            cid = nr["collection_id"]
            store = COLLECTION_TO_STORE.get(cid)
            if not store:
                continue
            th = nr["threshold"]
            try:
                nums = set(json.loads(nr["numbers_json"] or "[]"))
            except:
                nums = set()
            ed2 = nr["date"]
            store_numbers.setdefault(ed2, {}).setdefault(store, {})
            if th == 25:
                store_numbers[ed2][store]["positive"] = nums
            elif th == 24:
                store_numbers[ed2][store]["negative"] = nums

    # 反转成从旧到新，计算累计
    sorted_asc = list(reversed(deduped))
    all_results = []
    running_capital = 0
    running_profit = 0
    running_days = 0

    for r in sorted_asc:
        try:
            res = json.loads(r["result"])
        except Exception:
            res = {}
        algs = res.get("algorithms", [])
        day_capital = 0

        consensus = res.get("consensus", [])
        voted = [c for c in consensus if c.get("votes", 0) > 0]

        # 加载排位数据（命中判定用）— 用评估日的排位，fallback到指南日排位
        rankings = eval_rankings.get(r["date"]) or json.loads(r["rankings"])

        # 推断每店mode（兼容旧数据没有global mode字段）
        global_mode = res.get("mode")
        store_mode_map = {}
        if global_mode:
            for s in set(c["store"] for c in voted):
                store_mode_map[s] = global_mode
        else:
            # 多数voted店排>25→反帮扶，否则正帮扶
            gt25 = sum(1 for c in voted if rankings.get(c["store"], 0) > 25)
            inferred = "negative" if gt25 > len(voted) / 2 else "positive"
            for s in set(c["store"] for c in voted):
                store_mode_map[s] = inferred

        # 当天各店实际盈亏
        day_store_income = store_amounts.get(r["date"], {})
        earned = {}  # 赚的店 -> amount
        lost = {}    # 亏的店 -> amount
        store_details = []  # 每店：投多少 + 实际赚/亏
        for c in voted:
            s = c["store"]
            amt = day_store_income.get(s)
            if amt is not None:
                if amt > 0:
                    earned[s] = amt
                elif amt < 0:
                    lost[s] = abs(amt)
            caps = c.get("caps", {})
            max_cap = max(caps.values()) if caps else 0
            day_capital += max_cap
            if max_cap > 0:
                # 命中判定：优先用实际号码匹配（精准），回退到排位判定
                store_mode = store_mode_map.get(s, res.get("mode", "positive"))
                rank = rankings.get(s)
                draw_num = draws.get(r["date"])
                sn = store_numbers.get(r["date"], {}).get(s, {})
                target_nums = sn.get(store_mode, set())

                if draw_num is not None and target_nums:
                    # ✅ 精准判定：draw_number 是否在购买的号码中
                    hit = draw_num in target_nums
                elif draw_num is not None and rank is not None:
                    # ⚠️ 回退：无号码数据时用排位判定
                    base_hit = (rank <= 25)
                    hit = base_hit if store_mode == "positive" else not base_hit
                else:
                    hit = None  # 无抽签 → 待定
                # 正帮扶买25号(中+22,亏-25)，反帮扶买24号(中+23,亏-24)
                hm = HIT_MULT.get(store_mode, 22)
                mm = MISS_MULT.get(store_mode, 25)
                if hit is None:
                    net = 0  # 无抽签，无法计算盈亏
                    formula_text = f"待开奖 ×{max_cap}"
                elif hit:
                    net = max_cap * hm
                    formula_text = f"{max_cap}×{hm}={net:+}"
                else:
                    net = -max_cap * mm
                    formula_text = f"-{max_cap}×{mm}={net}"
                store_details.append({
                    "store": s,
                    "capital": max_cap,
                    "rank": rank,
                    "hit": hit,
                    "formula": formula_text,
                    "net": net,
                })

        # 当天公式利润 = 各店net之和
        day_profit = sum(sd["net"] for sd in store_details)

        running_days += 1
        running_capital += day_capital
        running_profit += day_profit

        store_capitals = {}  # 所有店的配资映射
        for c in consensus:
            caps = c.get("caps", {})
            store_capitals[c["store"]] = max(caps.values()) if caps else 0

        all_results.append({
            "id": r["id"],
            "date": r["date"],
            "rankings": json.loads(r["rankings"]),
            "result": res,
            "created_at": r["created_at"],
            "draw": draws.get(r["date"]),
            "capital": day_capital,
            "profit": day_profit,
            "hit_rate": round(sum(1 for sd in store_details if sd["hit"] is True) / max(sum(1 for sd in store_details if sd["hit"] is not None), 1), 2) if any(sd["hit"] is not None for sd in store_details) else None,
            "day_summary": {
                "profit": day_profit,
                "capital": day_capital,
                "stores": [c["store"] for c in voted],
                "top_store": voted[0]["store"] if voted else None,
                "earned": earned,
                "lost": lost,
                "store_details": store_details,
                "store_capitals": store_capitals,
            },
            "total": {
                "days": running_days,
                "capital": running_capital,
                "profit": running_profit,
            }
        })

    # 再反转回最新在前
    all_results.reverse()
    # 过滤：移除评估日无实际数据的指南（7-20排位不能用于7-20自己）
    # 必须有 earned/lost 或有效的 store_details（hit不是None）才算有效评估
    # ← 但保留最新日期（当天刚生成还没收入数据）
    latest_date = all_results[0]["date"] if all_results else None
    all_results = [r for r in all_results
                   if r["day_summary"].get("earned") or r["day_summary"].get("lost")
                   or (r["day_summary"].get("store_details") and any(sd.get("hit") is not None for sd in r["day_summary"]["store_details"]))
                   or r["date"] == latest_date]
    return {"rows": all_results, "total": total}


def _predict_orders(rankings, params):
    """当日排位 + 参数 → 推算明天出手列表 + 每店判定原因"""
    qual_summary = []
    qualified_list = []

    for s in STORE_NAMES:
        rank = rankings.get(s)
        p = params.get(s, {})
        threshold = p.get("threshold", 25)
        mode = p.get("mode", "positive")
        capital = p.get("capital", 10)

        qualified = False
        reason = ""
        if rank is None:
            reason = "无排位"
        elif mode == "positive":
            qualified = (rank <= threshold)
            reason = f"排位{rank}≤阈{threshold}✓" if qualified else f"排位{rank}>阈{threshold}✗"
        else:
            qualified = (rank > threshold)
            reason = f"排位{rank}>阈{threshold}✓(反)" if qualified else f"排位{rank}≤阈{threshold}✗(反)"

        qual_summary.append({
            "store": s, "rank": rank, "threshold": threshold,
            "mode": "正" if mode == "positive" else "反", "capital": capital,
            "qualified": qualified, "selected": False, "reason": reason
        })
        if qualified:
            qualified_list.append((s, capital, rank if rank is not None else 999, mode))
    
    # 排序：配资降序 → 同配资按排位质量（正=排位越低越好，反=排位越高越好）
    qualified_list.sort(key=lambda x: (-x[1], x[2] if x[3] == "positive" else -x[2]))
    selected = qualified_list  # 不限前3，全部达标都出手
    sel_set = set(s for s, _, _, _ in selected)
    
    orders = [{"store": s, "capital": cap} for s, cap, _, _ in selected]

    for q in qual_summary:
        if q["store"] in sel_set:
            q["selected"] = True
            q["reason"] += f" → 入选(配资{q['capital']}w)"
        elif q["qualified"]:
            q["reason"] += f" 配资{q['capital']}w未入选"

    return orders, qual_summary


def _build_consensus(algo_results):
    """多算法投票 → 每店得票+各算法配资"""
    store_votes = {}
    for algo in algo_results:
        for order in algo["orders"]:
            s = order["store"]
            if s not in store_votes:
                store_votes[s] = {"votes": 0, "caps": {}}
            store_votes[s]["votes"] += 1
            store_votes[s]["caps"][algo["key"]] = order["capital"]

    consensus = []
    for s in STORE_NAMES:
        sv = store_votes.get(s, {"votes": 0, "caps": {}})
        consensus.append({
            "store": s, "votes": sv["votes"],
            "out_of": len(algo_results),
            "caps": sv["caps"],
        })
    consensus.sort(key=lambda x: -x["votes"])
    return consensus


# ── 下单金额汇总 ──────────────────────────
def save_order_amounts(date, stores_data):
    """
    根据门店配置 + order_numbers 表计算1-49各号码累计金额 → 写入 order_amounts
    date: 出手日期（action_date）
    stores_data: [{store, capital, mode}, ...] — 仅用于读取cap和mode，号码从order_numbers表读
    """
    from datetime import datetime as dt, timedelta
    
    # 门店名 → collection_id 映射（order_numbers表用collection_id存储）
    STORE_CID = {
        '一店': '-23', '二店': '-24', '三店': '-25', '四店': '-26',
        '五店': '-28', '六店': '-29', '集合14': '14', '集合16': '16'
    }
    
    action_date = date
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    
    # 从 order_numbers 表读取当天的号码（正帮→Top25, 反帮→Top24）
    on_rows = db.execute(
        "SELECT collection_id, threshold, numbers_json FROM order_numbers WHERE date=?",
        (action_date,)
    ).fetchall()
    
    # 构建 store -> {25: set, 24: set}
    store_numbers = {}
    for r in on_rows:
        cid = str(r['collection_id'])
        name = None
        for n, c in STORE_CID.items():
            if c == cid:
                name = n
                break
        if not name:
            continue
        store_numbers.setdefault(name, {})
        store_numbers[name][r['threshold']] = set(json.loads(r['numbers_json']))
    
    db.execute("DELETE FROM order_amounts WHERE date=?", (action_date,))
    num_amounts = {}
    
    for sd in stores_data:
        capital = sd.get("capital", 0)
        mode = sd.get("mode", "")
        store_name = sd.get("store", "")
        if not capital or not store_name:
            continue
        
        # 正帮→Top25(th=25), 反帮→Top24(th=24)
        th = 25 if mode == 'positive' else 24
        nums = store_numbers.get(store_name, {}).get(th, set())
        
        # 如果前端传了numbers且order_numbers没有，fallback到前端数据
        if not nums:
            numbers = sd.get("numbers", [])
            flat = []
            if isinstance(numbers, dict):
                key = "24" if mode == "negative" else "25"
                flat = numbers.get(key, [])
                if not flat:
                    flat = numbers.get("24" if mode != "negative" else "25", [])
            elif isinstance(numbers, list):
                flat = numbers
            nums = set(flat)
        
        for n in nums:
            num_amounts[n] = num_amounts.get(n, 0) + capital
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for n in range(1, 50):
        db.execute(
            "INSERT OR REPLACE INTO order_amounts (date, number, amount, created_at, updated_at) VALUES (?,?,?,?,?)",
             (action_date, n, num_amounts.get(n, 0), now, now)
        )
    db.commit()
    db.close()
    # 如果该日期已有历史记录，自动同步 amounts_json + stores_json + own_capital/profit
    _sync_amounts_to_history(action_date, num_amounts, stores_data)
    return {"ok": True, "date": action_date, "total": sum(num_amounts.values())}


def _sync_amounts_to_history(action_date, num_amounts, stores_data=None):
    """更新正负下单后，自动同步历史记录中的1-49金额 + 门店快照"""
    import json as _json
    try:
        db2 = sqlite3.connect(FUNDS_DB)
        db2.row_factory = sqlite3.Row
        row = db2.execute(
            "SELECT id, draw_number FROM order_history WHERE action_date=? LIMIT 1",
            (action_date,)
        ).fetchone()
        if not row:
            db2.close()
            return
        amounts_json = _json.dumps(
            {str(k): v for k, v in num_amounts.items()}, ensure_ascii=False)
        total_bet = sum(num_amounts.values())
        draw_number = row["draw_number"] or 0
        if draw_number > 0:
            draw_amt = num_amounts.get(draw_number, 0)
            own_profit = round(draw_amt * 47 - total_bet, 2)
            own_capital = round(total_bet, 2)
        else:
            own_profit = None
            own_capital = None
        
        # 同步门店快照（避免 stores 旧 amounts 新）
        if stores_data:
            stores_json = _json.dumps(
                [{"store": s["store"], "capital": s["capital"], "mode": s.get("mode", "")}
                 for s in stores_data if s.get("capital")],
                ensure_ascii=False)
            total_cap = sum(s["capital"] for s in stores_data if s.get("capital"))
            db2.execute(
                "UPDATE order_history SET amounts_json=?, own_profit=?, own_capital=?, stores_json=?, total_capital=? WHERE id=?",
                (amounts_json, own_profit, own_capital, stores_json, total_cap, row["id"])
            )
        else:
            db2.execute(
                "UPDATE order_history SET amounts_json=?, own_profit=?, own_capital=? WHERE id=?",
                (amounts_json, own_profit, own_capital, row["id"])
            )
        db2.commit()
        db2.close()
    except:
        pass


def get_order_amounts(date=None, list_all=False):
    """读取下单金额。list_all=True 返回所有日期列表，含 day_index"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    if list_all:
        dates = db.execute("SELECT DISTINCT date FROM order_amounts ORDER BY date").fetchall()
        result = []
        for i, row in enumerate(dates):
            d = row["date"]
            amounts_rows = db.execute(
                "SELECT number, amount FROM order_amounts WHERE date=? ORDER BY number", (d,)
            ).fetchall()
            amounts = {}
            for r in amounts_rows:
                amounts[r["number"]] = r["amount"]
            # 取该日期最新的一条 created_at
            ts_row = db.execute(
                "SELECT created_at FROM order_amounts WHERE date=? AND created_at IS NOT NULL ORDER BY created_at DESC LIMIT 1", (d,)
            ).fetchone()
            created_at = ts_row["created_at"] if ts_row else None
            result.append({"date": d, "day_index": i + 1, "amounts": amounts, "total": sum(amounts.values()), "created_at": created_at})
        db.close()
        return {"items": result}
    if not date:
        row = db.execute("SELECT date FROM order_amounts ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            db.close()
            return {"date": None, "amounts": {}}
        date = row["date"]
    rows = db.execute(
        "SELECT number, amount FROM order_amounts WHERE date=? ORDER BY number", (date,)
    ).fetchall()
    db.close()
    amounts = {}
    for r in rows:
        amounts[r["number"]] = r["amount"]
    return {"date": date, "amounts": amounts}


# ── 下单历史（独立表，不受算法重跑影响）──────────
def _compute_from_order_numbers(action_date, stores):
    """从 order_numbers 重新计算该日期的 1-49 金额。
    stores: [{store, capital, mode}, ...] 来自 save_order_history 的 stores_data"""
    import json as _json
    try:
        db = sqlite3.connect(FUNDS_DB)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT collection_id, threshold, numbers_json FROM order_numbers WHERE date=?",
            (action_date,)
        ).fetchall()
        db.close()
        if not rows:
            return {}
        # 构建 store → {25: [...], 24: [...]}
        store_nums = {}
        for r in rows:
            store = COLLECTION_TO_STORE.get(r["collection_id"])
            if not store:
                continue
            if store not in store_nums:
                store_nums[store] = {}
            store_nums[store][str(r["threshold"])] = _json.loads(r["numbers_json"])
        # 计算
        num_amounts = {}
        for s in stores:
            cap = s.get("capital", 0)
            if not cap:
                continue
            nums_data = store_nums.get(s["store"], {})
            if s.get("mode") == "negative":
                guides = nums_data.get("24", [])
            else:
                guides = nums_data.get("25", [])
            for n in guides:
                num_amounts[n] = num_amounts.get(n, 0) + cap
        return {str(k): v for k, v in num_amounts.items()}
    except:
        return {}


def save_order_history(date, stores_data, amounts=None):
    """把下单时的门店快照写入 order_history，永久存档。
    同时快照：下单金额、抽签号、当日各店排位"""
    from datetime import datetime as dt, timedelta
    action_date = date
    stores = []
    total = 0
    for sd in stores_data:
        capital = sd.get("capital", 0)
        if not capital:
            continue
        store_mode = sd.get("mode", "")
        if not store_mode:
            nums = sd.get("numbers", {})
            if isinstance(nums, dict) and nums:
                has25 = "25" in nums
                has24 = "24" in nums
                if has25 and not has24:
                    store_mode = "positive"
                elif has24 and not has25:
                    store_mode = "negative"
                else:
                    store_mode = "positive"
            else:
                store_mode = "positive"
        stores.append({
            "store": sd.get("store", ""),
            "capital": capital,
            "mode": store_mode,
        })
        total += capital
    if not stores:
        return {"ok": False, "error": "无有效门店"}

    # 快照1：下单金额 — 优先从 order_numbers 计算（保证与当前 stores 配置一致）
    amounts_snapshot = _compute_from_order_numbers(action_date, stores)
    # 兜底：order_amounts 表（用户手动更新下单金额后的缓存）
    if not amounts_snapshot:
        try:
            db0 = sqlite3.connect(FUNDS_DB)
            rows = db0.execute(
                "SELECT number, amount FROM order_amounts WHERE date=? AND amount>0 ORDER BY number",
                (action_date,)
            ).fetchall()
            if rows:
                amounts_snapshot = {str(r[0]): r[1] for r in rows}
            db0.close()
        except:
            pass
    # 最后兜底：传参
    if not amounts_snapshot and amounts and isinstance(amounts, dict):
        amounts_snapshot = {str(k): v for k, v in amounts.items() if v}

    # 快照2：抽签号（出手日当天）
    draw_number = 0
    try:
        wh = sqlite3.connect(WH_DB)
        wh.row_factory = sqlite3.Row
        r = wh.execute(
            "SELECT draw_number FROM analysis_daily WHERE project_id=19 AND date=?",
            (action_date,)
        ).fetchone()
        if r:
            draw_number = r["draw_number"]
        wh.close()
    except:
        pass

    # 快照3：排位数据（排位日各店 ranking）
    rankings = {}
    try:
        db2 = sqlite3.connect(FUNDS_DB)
        rows = db2.execute(
            "SELECT store, amount FROM records WHERE date=? AND category=?",
            (date, RANKING_CAT)
        ).fetchall()
        for row in rows:
            rankings[row[0]] = row[1]
        db2.close()
    except:
        pass

    db = sqlite3.connect(FUNDS_DB)
    db.execute("DELETE FROM order_history WHERE date=?", (action_date,))
    db.execute(
        """INSERT INTO order_history 
           (date, action_date, mode, stores_json, total_capital,
            amounts_json, draw_number, rankings_json, history_date, acknowledged)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (action_date, action_date, "full", json.dumps(stores, ensure_ascii=False), total,
         json.dumps(amounts_snapshot, ensure_ascii=False), draw_number,
         json.dumps(rankings, ensure_ascii=False), action_date)
    )
    db.commit()
    db.close()
    return {"ok": True, "date": action_date, "action_date": action_date,
            "stores": len(stores), "total_capital": total,
            "draw_number": draw_number}


def get_order_history(limit=30, offset=0):
    """读取下单历史（独立表），含命中率计算 + 实际结果（从 sim_guides）"""
    db = sqlite3.connect(FUNDS_DB)
    db.row_factory = sqlite3.Row
    # 全量总数
    total_count = db.execute("SELECT COUNT(*) FROM order_history").fetchone()[0]
    rows = db.execute(
        "SELECT * FROM order_history ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    db.close()

    # 批量加载 sim_guides 实际结果 — 优先用 history_date（出手日=模拟日期）
    # 构建 row_id → guide_date 映射
    guide_dates = []
    row_guide_map = {}  # row_id -> guide_date (or None)
    for r in rows:
        gd = r["history_date"] or r["date"]
        row_guide_map[r["id"]] = gd
        guide_dates.append(gd)
    unique_guide_dates = list(set(guide_dates))
    guide_map = {}  # guide_date -> {positive: day_summary, negative: day_summary}
    if unique_guide_dates:
        db = sqlite3.connect(FUNDS_DB)
        db.row_factory = sqlite3.Row
        placeholders = ",".join(["?" for _ in unique_guide_dates])
        g_rows = db.execute(
            f"SELECT id, date, rankings, result FROM sim_guides WHERE date IN ({placeholders}) ORDER BY id DESC",
            unique_guide_dates
        ).fetchall()
        db.close()

        # 按(date, mode)去重取最新
        seen = set()
        store_amounts = {}  # date -> {store: amount} 用于盈亏计算
        if g_rows:
            # 批量查实际盈亏
            g_dates = list(set(r["date"] for r in g_rows))
            db2 = sqlite3.connect(FUNDS_DB)
            db2.row_factory = sqlite3.Row
            p2 = ",".join(["?" for _ in g_dates])
            income_rows = db2.execute(
                f"SELECT date, store, amount FROM records WHERE date IN ({p2}) AND category='income'",
                g_dates
            ).fetchall()
            db2.close()
            for ir in income_rows:
                store_amounts.setdefault(ir["date"], {})[ir["store"]] = ir["amount"]

        for gr in g_rows:
            try:
                res = json.loads(gr["result"])
            except:
                res = {}
            rankings = json.loads(gr["rankings"] or "{}")
            em = res.get("mode")
            if em is None:
                # 旧数据推断mode
                consensus = res.get("consensus", [])
                voted = [c for c in consensus if c.get("votes", 0) > 0]
                gt25 = sum(1 for c in voted if rankings.get(c["store"], 0) > 25)
                em = "negative" if gt25 > len(voted) / 2 else "positive"
            key = (gr["date"], em)
            if key in seen:
                continue
            seen.add(key)

            # 构建 day_summary（同 get_guide_history 逻辑）
            consensus = res.get("consensus", [])
            voted = [c for c in consensus if c.get("votes", 0) > 0]
            day_income = store_amounts.get(gr["date"], {})
            store_caps = {}
            store_dets = []
            day_capital = 0
            day_profit = 0
            for c in voted:
                s = c["store"]
                caps = c.get("caps", {})
                max_cap = max(caps.values()) if caps else 0
                store_caps[s] = max_cap
                day_capital += max_cap
                if max_cap > 0:
                    rank = rankings.get(s)
                    if rank is not None:
                        base_hit = (rank <= 25)
                        hit = base_hit if em == "positive" else not base_hit
                    else:
                        hit = False
                    hm = HIT_MULT.get(em, 22)
                    mm = MISS_MULT.get(em, 25)
                    net = max_cap * hm if hit else -max_cap * mm
                    store_dets.append({
                        "store": s, "capital": max_cap, "rank": rank,
                        "hit": hit, "net": net,
                    })
                    day_profit += net
            ds = {
                "capital": day_capital,
                "profit": day_profit,
                "store_capitals": store_caps,
                "store_details": store_dets,
            }
            guide_map.setdefault(gr["date"], {})[em] = ds

    # 从 order_amounts 存档表读当天演算快照（不再动态重算）
    num_map = {}  # order_history.date -> {number: amount}
    if rows:
        db3 = sqlite3.connect(FUNDS_DB)
        db3.row_factory = sqlite3.Row
        # 收集所有 order_history.date 和 action_date
        row_dates = list(set(r["date"] for r in rows))
        ad_rows = db3.execute(
            "SELECT date, MAX(action_date) as ad FROM order_history WHERE date IN ({}) GROUP BY date".format(
                ",".join(["?" for _ in row_dates])),
            row_dates
        ).fetchall()
        date_to_ad = {r["date"]: r["ad"] for r in ad_rows if r["ad"]}
        if date_to_ad:
            ad_dates = list(set(date_to_ad.values()))
            oa_rows = db3.execute(
                "SELECT date, number, amount FROM order_amounts WHERE date IN ({})".format(
                    ",".join(["?" for _ in ad_dates])),
                ad_dates
            ).fetchall()
            ad_num_map = {}
            for oa in oa_rows:
                ad_num_map.setdefault(oa["date"], {})[str(oa["number"])] = oa["amount"]
            for d in row_dates:
                ad = date_to_ad.get(d)
                if ad:
                    num_map[d] = ad_num_map.get(ad, {})
        db3.close()

    # 门店对应号码：从 order_numbers 表查
    store_num_map = {}  # action_date -> {store: {top25:[], top24:[]}}
    if rows:
        db4 = sqlite3.connect(FUNDS_DB)
        db4.row_factory = sqlite3.Row
        action_dates = list(set(
            r["action_date"] for r in rows if r["action_date"]
        ))
        if action_dates:
            ph = ",".join(["?" for _ in action_dates])
            on_rows = db4.execute(
                f"SELECT date, collection_id, threshold, numbers_json FROM order_numbers WHERE date IN ({ph})",
                action_dates
            ).fetchall()
            for onr in on_rows:
                dt = onr["date"]
                cid = onr["collection_id"]
                th = onr["threshold"]
                store = COLLECTION_TO_STORE.get(cid)
                if not store:
                    continue
                try:
                    nums = json.loads(onr["numbers_json"])
                except:
                    nums = []
                store_num_map.setdefault(dt, {}).setdefault(store, {})
                store_num_map[dt][store]["top25" if th == 25 else "top24"] = nums
        db4.close()

    result = []
    pos_hits = pos_total = neg_hits = neg_total = 0
    for r in rows:
        stores = json.loads(r["stores_json"] or "[]")
        rankings = {}
        try:
            rankings = json.loads(r["rankings_json"] or "{}")
        except:
            pass
        amounts = {}
        try:
            amounts = json.loads(r["amounts_json"] or "{}")
        except:
            pass
        # 命中判定：优先用实际号码匹配（精准），回退到排位判定
        # 只有当draw_number>0（已开奖）时才计算盈亏，未开奖的统一置0
        has_draw = (r["draw_number"] or 0) > 0
        draw_num = r["draw_number"] or 0
        store_hits = {}      # {store: hit}
        store_hits_pos = {}  # {store: hit} -- 正帮扶命中
        store_hits_neg = {}  # {store: hit} -- 反帮扶命中
        own_profit = r["own_profit"]
        own_capital = r["own_capital"]
        # 如果DB中无值但已开奖 → 算一次并写回
        if has_draw and (own_profit is None or own_capital is None):
            total_bet = sum(float(v) for v in amounts.values())
            draw_amt = float(amounts.get(str(r["draw_number"]), 0))
            own_profit = round(draw_amt * 47 - total_bet, 2)
            own_capital = round(total_bet, 2)
            # 写回DB
            try:
                db_w = sqlite3.connect(FUNDS_DB)
                db_w.execute(
                    "UPDATE order_history SET own_profit=?, own_capital=? WHERE id=?",
                    (own_profit, own_capital, r["id"])
                )
                db_w.commit()
                db_w.close()
            except:
                pass
        elif not has_draw:
            own_profit = 0
            own_capital = 0
        else:
            own_profit = own_profit or 0
            own_capital = own_capital or 0
            for s in stores:
                store_name = s["store"]
                store_mode = s.get("mode", "positive")
                # 精准判定：draw_number 是否在各店购买的号码中
                sm = store_num_map.get(r["action_date"], {}).get(store_name, {})
                key = "top25" if store_mode == "positive" else "top24"
                nums = sm.get(key, [])
                if draw_num and draw_num > 0 and nums:
                    hit = draw_num in nums
                else:
                    # 回退：无号码数据时用排位判定
                    rk = rankings.get(store_name)
                    if rk is not None:
                        hit = (rk <= 25) if store_mode == "positive" else (rk > 25)
                    else:
                        hit = False
                if store_mode == "negative":
                    store_hits_neg[store_name] = hit
                    neg_total += 1
                    if hit: neg_hits += 1
                else:
                    store_hits_pos[store_name] = hit
                    pos_total += 1
                    if hit: pos_hits += 1
                store_hits[store_name] = hit

        # 附加实际结果（来自 sim_guides — 用修正后的 guide_date）
        guide_date = row_guide_map.get(r["id"]) or r["history_date"] or r["date"]
        guide_data = guide_map.get(guide_date, {})

        result.append({
            "id": r["id"],
            "date": r["date"],
            "action_date": r["action_date"],
            "history_date": r["history_date"] or r["date"],
            "mode": r["mode"],
            "stores": stores,
            "total_capital": r["total_capital"],
            "acknowledged": r["acknowledged"] if "acknowledged" in r.keys() else 0,
            "created_at": r["created_at"],
            "draw_number": r["draw_number"] or 0,
            "rankings": rankings,
            "amounts": amounts,
            "store_hits": store_hits,
            "store_hits_pos": store_hits_pos,
            "store_hits_neg": store_hits_neg,
            "guide_positive": guide_data.get("positive"),
            "guide_negative": guide_data.get("negative"),
            "computed_amounts": num_map.get(r["date"], {}),
            "own_profit": own_profit,
            "own_capital": own_capital,
            "store_numbers": store_num_map.get(r["action_date"], {}),
        })
    hit_rate = {
        "positive": {"hits": pos_hits, "total": pos_total,
                      "rate": round(pos_hits/pos_total*100,1) if pos_total>0 else 0},
        "negative": {"hits": neg_hits, "total": neg_total,
                      "rate": round(neg_hits/neg_total*100,1) if neg_total>0 else 0},
    }
    return {"rows": result, "total": total_count, "hit_rate": hit_rate}


def ack_order_history(action_date, acknowledged):
    """确认/撤销某个出手日的下单记录"""
    db = sqlite3.connect(FUNDS_DB)
    db.execute(
        "UPDATE order_history SET acknowledged=? WHERE action_date=?",
        (1 if acknowledged else 0, action_date)
    )
    db.commit()
    updated = db.total_changes
    db.close()
    return {"ok": True, "action_date": action_date, "acknowledged": bool(acknowledged), "updated": updated}

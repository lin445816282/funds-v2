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
MODE_OPTIONS = ["positive", "negative"]
STORE_NAMES = ["一店","二店","三店","四店","五店","六店","集合14","集合16"]
RANKING_CAT = "cat_1783487972049"
# 盈亏倍数：正帮扶买25个号(中+22,不中-25)，反帮扶买24个号(中+23,不中-24)
HIT_MULT = {"positive": 22, "negative": 23}
MISS_MULT = {"positive": 25, "negative": 24}

# ═══════════════ 数据加载 ═══════════════════
def load_data(days=90):
    """返回 {date: {draw: int, rankings: {store: rank}}} 列表，按日期排序"""
    fv = sqlite3.connect(FUNDS_DB)
    fv.row_factory = sqlite3.Row
    
    # 获取排位数据
    rankings = {}
    rows = fv.execute("""
        SELECT store, date, amount FROM records 
        WHERE category=? AND date >= '2026-04-11'
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
    # 获取各店实际收入（盈亏判定用）
    store_income = {}
    rows_i = fv.execute("""
        SELECT date, store, amount FROM records 
        WHERE category='income' AND date >= '2026-04-11'
        ORDER BY date, store
    """).fetchall()
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
        WHERE project_id=19 AND date >= '2026-04-11'
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
            qualified_list.append((s, capital))
    
    # 按capital降序 → 取前3
    qualified_list.sort(key=lambda x: -x[1])
    selected = qualified_list[:shots_per_day]
    
    # 标记选中
    sel_set = set(s for s, _ in selected)
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
    for s, cap in selected:
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
def optimize(data, mode="positive", algorithm="coordinate", max_iter=3):
    """优化8店参数，支持4种算法（精简版：阈值10档，迭代3轮）"""
    if algorithm == "uniform":
        return _uniform_optimize(data, mode)
    elif algorithm == "positive_only":
        return _positive_only_optimize(data, mode, max_iter)
    elif algorithm == "stop_neg2":
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=True)
    else:  # coordinate
        return _coordinate_optimize(data, mode, max_iter, stop_on_neg2=False)


def _coordinate_optimize(data, mode, max_iter=3, stop_on_neg2=False):
    """坐标下降 + 5个均匀起点 → 取最优（确定性）"""
    # 5个均匀分布起点：(阈,资)
    starters = [(5,10),(15,20),(25,40),(35,20),(45,10)]
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
                for t in THRESHOLD_OPTIONS:
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
    best_params = None
    best_result = None
    best_profit = -float("inf")
    
    # 阶段1：暴力搜统一参数（10×3=30种，锁定模式）
    for t in THRESHOLD_OPTIONS:
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


def _positive_only_optimize(data, mode, max_iter=3):
    """仅指定模式：固定5个分布均匀起点 → 坐标下降 → 取最优（确定性）"""
    # 5个均匀分布起点：(阈,资) = (5,10),(15,20),(25,40),(35,20),(45,10)
    starters = [(5,10),(15,20),(25,40),(35,20),(45,10)]
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
                
                for t in THRESHOLD_OPTIONS:
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


# ═══════════════ API：每日下单指南 ═══════════════
def run_daily_guide(days=90, mode="positive"):
    """跑4组优化 + 取最新排位 + 逐店判定 → 投票汇总
    mode: "positive"=正帮扶（排位≤阈值出手）, "negative"=反帮扶（排位>阈值出手）
    """
    data = load_data(days)
    if not data:
        return {"error": "无数据"}

    # 找最新有真实开奖结果的日子（draw>0），避免排位入库但未开奖的日子
    last_day = None
    for d in reversed(data):
        if d.get("draw", 0) > 0 and d.get("rankings"):
            last_day = d
            break
    if not last_day:
        return {"error": "无有效数据（需有开奖结果）"}

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
        params, result = optimize(data, mode, algo_key)
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


def _save_guide(result):
    """保存到 sim_guides 表"""
    try:
        db = sqlite3.connect(FUNDS_DB)
        db.execute(
            "INSERT INTO sim_guides (date, rankings, result) VALUES (?, ?, ?)",
            (result["date"], json.dumps(result["today_rankings"]), json.dumps(result, ensure_ascii=False))
        )
        db.commit()
        db.close()
    except Exception:
        pass  # 保存失败不影响主流程


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

    # 批量查当天各店实际盈亏 + draw
    store_amounts = {}  # date -> {store: amount}
    draws = {}  # date -> draw_number
    if dates:
        db = sqlite3.connect(FUNDS_DB)
        db.row_factory = sqlite3.Row
        placeholders = ",".join(["?" for _ in dates])
        rows = db.execute(
            f"SELECT date, store, amount FROM records WHERE date IN ({placeholders}) AND category='income'",
            dates
        ).fetchall()
        db.close()
        for r in rows:
            store_amounts.setdefault(r["date"], {})[r["store"]] = r["amount"]

        # 查draw
        wh = sqlite3.connect(WH_DB)
        wh.row_factory = sqlite3.Row
        rows_d = wh.execute(
            f"SELECT date, draw_number FROM analysis_daily WHERE project_id=19 AND date IN ({placeholders})",
            dates
        ).fetchall()
        wh.close()
        for r in rows_d:
            draws[r["date"]] = r["draw_number"]

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

        # 加载排位数据（命中判定用）
        rankings = json.loads(r["rankings"])

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
                # 命中由排位决定：正帮扶排≤25命中，反帮扶排>25命中
                store_mode = store_mode_map.get(s, res.get("mode", "positive"))
                rank = rankings.get(s)
                if rank is not None:
                    base_hit = (rank <= 25)
                    hit = base_hit if store_mode == "positive" else not base_hit
                else:
                    hit = False
                # 正帮扶买25号(中+22,亏-25)，反帮扶买24号(中+23,亏-24)
                hm = HIT_MULT.get(store_mode, 22)
                mm = MISS_MULT.get(store_mode, 25)
                net = max_cap * hm if hit else -max_cap * mm
                store_details.append({
                    "store": s,
                    "capital": max_cap,
                    "rank": rank,
                    "hit": hit,
                    "formula": f"{max_cap}×{hm}={net:+}" if hit else f"-{max_cap}×{mm}={net}",
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
            qualified_list.append((s, capital))

    qualified_list.sort(key=lambda x: -x[1])
    selected = qualified_list[:3]
    sel_set = set(s for s, _ in selected)

    orders = [{"store": s, "capital": cap} for s, cap in selected]

    for q in qual_summary:
        if q["store"] in sel_set:
            q["selected"] = True
            q["reason"] += f" → 入选(配资{q['capital']}w前3)"
        elif q["qualified"]:
            q["reason"] += f" 配资{q['capital']}w未入前3"

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

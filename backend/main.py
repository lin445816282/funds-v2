"""funds-v2 独立服务 — 多门店资金看板 + 预测数据 API"""
import os, json, uuid, secrets, asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse

_executor = ThreadPoolExecutor(max_workers=8)
from fastapi.middleware.cors import CORSMiddleware
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "funds-v2.db")
PASSWORD = "123456"
EXTERNAL_API_KEY = "funds-v2-ext-2026"  # 给外部系统的对接密钥

app = FastAPI(title="funds-v2", docs_url=None, redoc_url=None)

# ── CORS ───────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB ─────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ═══════════════ 初始化 ═══════════════════
def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            action TEXT NOT NULL,
            store TEXT,
            detail TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS order_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            collection_id INTEGER NOT NULL,
            threshold INTEGER NOT NULL,
            summary_name TEXT NOT NULL DEFAULT '',
            numbers_json TEXT NOT NULL,
            pulled_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(date, collection_id, threshold)
        );
        CREATE TABLE IF NOT EXISTS order_amounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            number INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            UNIQUE(date, number)
        );
        CREATE TABLE IF NOT EXISTS order_daily_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            result TEXT NOT NULL,
            created TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS order_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            action_date TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'full',
            stores_json TEXT NOT NULL,
            total_capital INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS draw_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            day_seq INTEGER NOT NULL,
            draw_number INTEGER NOT NULL,
            synced_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()

def log_op(action, store, detail, data=None):
    """记录操作日志到 funds-v2.db"""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO operation_logs (ts, action, store, detail, data) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), action, store, detail, json.dumps(data) if data else "")
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[log_op err] {e}")


def _log_admin_login(ip: str, target: str, success: bool, detail: str = ""):
    """跨库写管理后台 login_logs 表（stock_agg.db）"""
    try:
        import sqlite3 as _sqlite
        _adb = _sqlite.connect("/home/xiaolin/projects/stock-aggregator/data/stock_agg.db")
        _adb.execute(
            "INSERT INTO login_logs (ip, target, success, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (ip, target, 1 if success else 0, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        _adb.commit()
        _adb.close()
    except Exception:
        pass

init_db()

# ═══════════════ 认证 ═══════════════════
# 公开路由白名单
PUBLIC_PATHS = {"/", "/api/auth/login", "/favicon.ico", "/api/external/push", "/api/external/template"}

def is_public(path: str) -> bool:
    # 静态文件也放行
    if path.startswith("/static") or path.startswith("/assets"):
        return True
    return path in PUBLIC_PATHS

async def require_auth(request: Request):
    """中间件：非公开路由需要有效 token 或 X-API-Key"""
    if is_public(request.url.path):
        return
    # 允许外部 API Key
    if request.headers.get("X-API-Key") == EXTERNAL_API_KEY:
        return
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    conn = get_db()
    try:
        row = conn.execute("SELECT token FROM sessions WHERE token=?", (token,)).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="登录过期")
    finally:
        conn.close()

# ═══════════════ 纯净化 ═══════════════════
ALLOWED_COLS = {'store', 'date', 'category', 'amount', 'note'}

def sanitize_cols(updates):
    """确保 SQL 字段名在白名单内"""
    safe = []
    for col in updates:
        if col in ALLOWED_COLS:
            safe.append(col)
    return safe

# ═══════════════ API: 登录 ═══════════════
@app.post("/api/auth/login")
async def auth_login(request: Request):
    data = await request.json()
    pwd = data.get("password", "")
    ip = request.client.host if request.client else "unknown"
    if pwd != PASSWORD:
        _log_admin_login(ip, "funds-v2", False, "密码错误")
        raise HTTPException(status_code=403, detail="密码错误")
    token = secrets.token_hex(32)
    conn = get_db()
    try:
        conn.execute("INSERT INTO sessions (token, created) VALUES (?,?)",
                     (token, datetime.now().isoformat()))
        conn.commit()
        _log_admin_login(ip, "funds-v2", True, "")
        return {"ok": True, "token": token}
    finally:
        conn.close()

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}

# ═══════════════ API: 数据 ═══════════════
@app.get("/api/funds/data")
async def get_funds_data(request: Request):
    await require_auth(request)
    # 默认返回最近365天数据，避免全量超时
    since = request.query_params.get("since", "")
    if not since:
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    conn = get_db()
    try:
        stores = [r["name"] for r in conn.execute("SELECT name FROM stores ORDER BY id").fetchall()]
        cats = [dict(r) for r in conn.execute("SELECT * FROM categories").fetchall()]
        records = [dict(r) for r in conn.execute(
            "SELECT * FROM records WHERE date >= ? ORDER BY date, id", (since,)
        ).fetchall()]
        rules = conn.execute("SELECT * FROM alert_rules").fetchall()
        alert_rules = []
        for r in rules:
            d = dict(r)
            d["on"] = bool(d.pop("on_state", 1))
            d["desc"] = d.pop("description", "")
            alert_rules.append(d)
        settings = conn.execute("SELECT * FROM store_settings").fetchall()
        warn_store_on = {}
        profit_store_on = {}
        for s in settings:
            if s["key"] == "warn":
                warn_store_on[s["store"]] = bool(s["value"])
            elif s["key"] == "profit":
                profit_store_on[s["store"]] = bool(s["value"])
        return {
            "stores": stores,
            "categories": cats,
            "records": records,
            "alert_rules": alert_rules,
            "warn_store_on": warn_store_on,
            "profit_store_on": profit_store_on,
        }
    finally:
        conn.close()

@app.post("/api/funds/records")
async def post_funds_record(request: Request):
    await require_auth(request)
    data = await request.json()
    conn = get_db()
    try:
        store = data.get("store","")
        date = data.get("date","")
        category = data.get("category","")
        amount = data.get("amount",0)
        note = data.get("note","")
        if not store or not date:
            return {"ok": False, "error": "store and date required"}
        # 同名同日同类 → 覆盖（银行流水以后到数据为准）
        conn.execute("""
            INSERT INTO records (store, date, category, amount, note)
            VALUES (?,?,?,?,?)
            ON CONFLICT(date, store, category) DO UPDATE SET
                amount = excluded.amount,
                note = excluded.note
        """, (store, date, category, amount, note))
        new_id = conn.execute("SELECT id FROM records WHERE date=? AND store=? AND category=?", 
                              (date, store, category)).fetchone()["id"]
        conn.commit()
        log_op("新增/更新", store, f"{store} {date} {category} {amount}", {"amount": amount, "date": date, "category": category})
        return {"ok": True, "id": new_id}
    finally:
        conn.close()

@app.delete("/api/funds/records/{rid}")
async def delete_funds_record(rid: str, request: Request):
    await require_auth(request)
    conn = get_db()
    try:
        # 1) TEXT match (new records stored as TEXT)
        cur = conn.execute("DELETE FROM records WHERE CAST(id AS TEXT)=?", (rid,))
        deleted = cur.rowcount
        # 2) Fallback: float match for old REAL-typed records
        if deleted == 0:
            try:
                rid_float = float(rid)
                cur = conn.execute("DELETE FROM records WHERE ABS(id - ?) < 1e-6", (rid_float,))
                deleted = cur.rowcount
            except (ValueError, TypeError):
                pass
        conn.commit()
        if deleted:
            log_op("删除", str(rid), f"删除记录 {rid}", {"rid": rid})
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        conn.close()

@app.put("/api/funds/records/{rid}")
async def update_funds_record(rid: str, request: Request):
    await require_auth(request)
    data = await request.json()
    conn = get_db()
    try:
        rid_num = float(rid) if '.' in str(rid) else int(rid)
        row = conn.execute("SELECT store, date FROM records WHERE id=?", (rid_num,)).fetchone()
        updates = []
        params = []
        for f in ALLOWED_COLS:
            if f in data and data[f] is not None:
                updates.append(f"{f}=?")
                params.append(data[f])
        if updates:
            params.append(rid_num)
            conn.execute("UPDATE records SET " + ",".join(updates) + " WHERE id=?", params)
            conn.commit()
            if row:
                log_op("修改", row["store"], f"{row['store']} {row['date']}", {"fields": list(data.keys())})
            return {"ok": True}
        return {"ok": False, "error": "no fields to update"}
    finally:
        conn.close()

@app.post("/api/funds/data")
async def post_funds_data(request: Request):
    await require_auth(request)
    data = await request.json()
    conn = get_db()
    try:
        # ⚠️ stores/categories 不再从客户端全量覆写
        # 门店和分类只能通过管理面板 API 操作，防止前端脏数据污染
        if "records" in data:
            for r in data["records"]:
                # 只插入不存在的，不覆盖已有数据
                existing = conn.execute("SELECT id FROM records WHERE id=?", (r["id"],)).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO records (id, store, date, category, amount, note) VALUES (?,?,?,?,?,?)",
                        (r["id"], r["store"], r["date"], r["category"],
                         r.get("amount",0), r.get("note",""))
                    )
        if "alert_rules" in data:
            for a in data["alert_rules"]:
                conn.execute(
                    "INSERT OR REPLACE INTO alert_rules (id, cat, type, description, pct, on_state) VALUES (?,?,?,?,?,?)",
                    (a["id"], a.get("cat",""), a["type"], a.get("desc",""),
                     a.get("pct",0), int(a.get("on",1)))
                )
        if "warn_store_on" in data:
            for s, v in data["warn_store_on"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO store_settings (store, key, value) VALUES (?,?,?)",
                    (s, "warn", int(v))
                )
        if "profit_store_on" in data:
            for s, v in data["profit_store_on"].items():
                conn.execute(
                    "INSERT OR REPLACE INTO store_settings (store, key, value) VALUES (?,?,?)",
                    (s, "profit", int(v))
                )
        conn.commit()
        store_counts = {}
        for r in data.get("records", []):
            s = r.get("store","?")
            store_counts[s] = store_counts.get(s, 0) + 1
        print(f"[push] {len(data.get('records',[]))}条, stores={store_counts}")
        return {"ok": True}
    finally:
        conn.close()

# ═══════════════ API: 分类增删改 ═══════════════
@app.post("/api/funds/categories")
async def create_category(request: Request):
    await require_auth(request)
    data = await request.json()
    cat_id = data.get("id", "")
    name = (data.get("name", "") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    if not cat_id:
        import time
        cat_id = "cat_" + str(int(time.time() * 1000))
    dir_ = data.get("dir", "-")
    color = data.get("color", "#3b82f6")
    budget = data.get("budget", 0)
    show = data.get("show", True)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO categories (id, name, dir, color, budget, show) VALUES (?,?,?,?,?,?)",
            (cat_id, name, dir_, color, budget, int(show))
        )
        conn.commit()
        log_op("新增分类", "", f"{name} ({dir_})", {"id": cat_id})
        return {"ok": True, "id": cat_id}
    finally:
        conn.close()

@app.put("/api/funds/categories/{cat_id}")
async def update_category(request: Request, cat_id: str):
    await require_auth(request)
    data = await request.json()
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not existing:
            return {"ok": False, "error": "category not found"}
        allowed = {"name", "dir", "color", "budget", "show"}
        updates = {k: (int(data[k]) if k == "show" else data[k]) for k in allowed & data.keys()}
        if not updates:
            return {"ok": False, "error": "no valid fields"}
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [cat_id]
        conn.execute(f"UPDATE categories SET {set_clause} WHERE id=?", vals)
        conn.commit()
        log_op("修改分类", "", cat_id, updates)
        return {"ok": True}
    finally:
        conn.close()

@app.delete("/api/funds/categories/{cat_id}")
async def delete_category(request: Request, cat_id: str):
    await require_auth(request)
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not existing:
            return {"ok": False, "error": "category not found"}
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        conn.execute("DELETE FROM records WHERE category=?", (cat_id,))
        conn.execute("DELETE FROM alert_rules WHERE cat=?", (cat_id,))
        conn.commit()
        log_op("删除分类", "", f"{existing['name']} ({existing['dir']})", {"id": cat_id})
        return {"ok": True}
    finally:
        conn.close()

# ═══════════════ API: 门店增删 ═══════════════
@app.post("/api/funds/stores")
async def create_store(request: Request):
    await require_auth(request)
    data = await request.json()
    name = (data.get("name", "") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM stores WHERE name=?", (name,)).fetchone()
        if existing:
            return {"ok": False, "error": "已存在"}
        conn.execute("INSERT INTO stores (name) VALUES (?)", (name,))
        conn.commit()
        log_op("新增门店", "-", name)
        return {"ok": True}
    finally:
        conn.close()

@app.delete("/api/funds/stores/{name}")
async def delete_store(request: Request, name: str):
    await require_auth(request)
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM stores WHERE name=?", (name,)).fetchone()
        if not existing:
            return {"ok": False, "error": "store not found"}
        conn.execute("DELETE FROM records WHERE store=?", (name,))
        conn.execute("DELETE FROM stores WHERE name=?", (name,))
        conn.commit()
        log_op("删除门店", "-", name)
        return {"ok": True}
    finally:
        conn.close()

# ═══════════════ API: 操作日志 ═══════════════
@app.get("/api/funds/logs")
async def get_op_logs(request: Request, limit: int = 50):
    await require_auth(request)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM operation_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"logs": [dict(r) for r in rows]}
    finally:
        conn.close()

# ═══════════════ 外部系统对接 API ═══════════════
# 安全：API Key 校验 + 操作日志 + 参数化查询

def check_external_key(request: Request):
    """验证外部系统 API Key"""
    key = request.headers.get("X-API-Key", "")
    if not key or key != EXTERNAL_API_KEY:
        raise HTTPException(status_code=403, detail="无效的 API Key")

@app.post("/api/external/push")
async def external_push(request: Request):
    """外部系统推送流水记录 — 仅 records upsert（分类/门店由前端管理）"""
    check_external_key(request)
    data = await request.json()
    conn = get_db()
    try:
        rec_count = 0
        if "records" in data:
            for r in data["records"]:
                # 用 UNIQUE(date,store,category) 去重，不依赖外部id（仓库传字符串id与INTEGER PK冲突）
                existing = conn.execute(
                    "SELECT id FROM records WHERE date=? AND store=? AND category=?",
                    (str(r["date"]), str(r["store"]), str(r["category"]))
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO records (store, date, category, amount, note) VALUES (?,?,?,?,?)",
                        (str(r["store"]), str(r["date"]), str(r["category"]),
                         float(r.get("amount",0)), str(r.get("note","")))
                    )
                    rec_count += 1
        conn.commit()
        log_op("外部推送", "-", f"recs={rec_count}")
        return {"ok": True, "records": rec_count}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"数据格式错误: {str(e)}")
    finally:
        conn.close()

@app.get("/api/external/template")
async def external_template():
    """返回对接数据格式模板（无需认证，仅供参考）"""
    return {
        "api": "POST /api/external/push",
        "auth": "Header: X-API-Key: <你的密钥>",
        "content_type": "application/json",
        "body": {
            "stores": ["一店", "二店", "三店", "四店"],
            "categories": [
                {"id": "income", "name": "收入", "dir": "+", "color": "#22c55e", "budget": 0, "show": 1},
                {"id": "purchase", "name": "采购", "dir": "-", "color": "#ef4444", "budget": 0, "show": 1}
            ],
            "records": [
                {"id": 1, "store": "一店", "date": "2026-01-01", "category": "income", "amount": 5000, "note": "日结"}
            ]
        },
        "note": "仅 upsert records（id 已存在则跳过）。stores/categories 需通过管理面板添加。可选字段：records.note。Header 认证：X-API-Key: funds-v2-ext-2026"
    }

# ═══════════════ 预测 API ═══════════════
@app.get("/api/sector-predictions")
async def sector_predictions(days: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sector_predictions ORDER BY predict_date DESC, id LIMIT ?",
        (days * 10,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}

@app.get("/api/index-predictions")
async def index_predictions(days: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM index_predictions ORDER BY predict_date DESC, id LIMIT ?",
        (days * 3,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}

# ═══════════════ 总部出手模拟 ═══════════════
from simulate import get_simulate_data, run_optimize, run_manual, run_daily_guide, get_guide_history, get_optimization_log, get_order_sheet, pull_threshold_numbers, list_order_numbers, delete_order_numbers_by_date, get_order_numbers_detail, save_order_amounts, get_order_amounts, save_order_history, get_order_history, ack_order_history

@app.get("/api/simulate/data")
async def api_simulate_data(days: int = 90):
    return get_simulate_data(days)

@app.post("/api/simulate/optimize")
async def api_simulate_optimize(request: Request):
    data = await request.json()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_optimize(
        days=data.get("days", 90),
        mode=data.get("mode", "positive"),
        algorithm=data.get("algorithm", "coordinate")
    ))

@app.post("/api/simulate/manual")
async def api_simulate_manual(request: Request):
    data = await request.json()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_manual(
        stores_config=data.get("stores", []),
        days=data.get("days", 90),
        algorithm=data.get("algorithm")
    ))

@app.get("/api/simulate/daily-guide")
async def api_daily_guide(days: int = 90, mode: str = "positive", max_iter: int = 10):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_daily_guide(days, mode, max_iter))

@app.get("/api/simulate/guide-history")
async def api_guide_history(limit: int = 30, mode: str = None, offset: int = 0):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_guide_history(limit, mode, offset))

@app.get("/api/simulate/optimization-log")
async def api_optimization_log():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, get_optimization_log)

@app.get("/api/simulate/order-sheet")
async def api_order_sheet(days: int = 90, date: str = None, guide_date: str = None):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, lambda: get_order_sheet(days, target_date=date, guide_date=guide_date))
    return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.post("/api/simulate/pull-numbers")
async def api_pull_numbers(date: str = None, from_date: str = None, to_date: str = None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: pull_threshold_numbers(
        target_date=date, from_date=from_date, to_date=to_date
    ))

@app.get("/api/simulate/order-numbers-list")
async def api_order_numbers_list(page: int = 1, limit: int = 20):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: list_order_numbers(page, limit))

@app.get("/api/simulate/order-numbers-detail")
async def api_order_numbers_detail(date: str = ""):
    if not date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_order_numbers_detail(date))

@app.delete("/api/simulate/order-numbers")
async def api_order_numbers_delete(date: str = ""):
    if not date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: delete_order_numbers_by_date(date))

# ── 下单金额 ──
@app.post("/api/simulate/order-amounts")
async def api_save_order_amounts(data: dict):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: save_order_amounts(
        data.get("date", ""), data.get("stores", [])
    ))

@app.get("/api/simulate/order-amounts")
async def api_get_order_amounts(date: str = None, list_all: bool = False):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_order_amounts(date=date, list_all=list_all))

# ── 下单历史（独立表）──
@app.get("/api/simulate/order-history")
async def api_get_order_history(limit: int = 30, offset: int = 0):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, lambda: get_order_history(limit, offset))
    return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.post("/api/simulate/order-history/confirm")
async def api_confirm_order_history(request: Request):
    """用户确认当前下单配置 → 写入 order_history"""
    body = await request.json()
    date = body.get("date", "")
    stores = body.get("stores", [])
    amounts = body.get("amounts", None)
    if not date or not stores:
        raise HTTPException(400, "date and stores required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: save_order_history(date, stores, amounts))

@app.post("/api/simulate/order-history/ack")
async def api_ack_order_history(request: Request):
    """用户确认/撤销某个出手日的下单记录"""
    body = await request.json()
    action_date = body.get("action_date", "")
    acknowledged = body.get("acknowledged", True)
    if not action_date:
        raise HTTPException(400, "action_date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: ack_order_history(action_date, acknowledged))

# ═══════ 抽签记录（从 warehouse 同步）═══════
@app.get("/api/draw-records")
async def api_draw_records(page: int = 1, page_size: int = 30):
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM draw_records").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM draw_records ORDER BY date DESC LIMIT ? OFFSET ?",
            (page_size, (page-1)*page_size)
        ).fetchall()
        # 批量查排位
        dates = [r["date"] for r in rows]
        rankings_map = {}
        if dates:
            placeholders = ",".join(["?"]*len(dates))
            rk_rows = conn.execute(
                f"SELECT date, store, amount FROM records WHERE date IN ({placeholders}) AND category='cat_1783487972049'",
                dates
            ).fetchall()
            for rk in rk_rows:
                rankings_map.setdefault(rk["date"], {})[rk["store"]] = rk["amount"]
        records = []
        for r in rows:
            d = dict(r)
            d["rankings"] = rankings_map.get(r["date"], {})
            records.append(d)
        return {"rows": records, "total": total, "page": page, "page_size": page_size}
    finally:
        conn.close()

@app.post("/api/draw-records/sync")
async def api_draw_records_sync():
    """从 warehouse 同步抽签记录"""
    try:
        wh = sqlite3.connect("/home/xiaolin/projects/number-warehouse/backend/data/warehouse.db")
        wh.row_factory = sqlite3.Row
        wh_rows = wh.execute("SELECT date, day_seq, draw_number FROM records ORDER BY date").fetchall()
        wh.close()
    except Exception as e:
        raise HTTPException(500, f"读取warehouse失败: {e}")

    conn = get_db()
    added = 0
    try:
        for r in wh_rows:
            existing = conn.execute("SELECT id FROM draw_records WHERE date=?", (r["date"],)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO draw_records (date, day_seq, draw_number) VALUES (?,?,?)",
                    (r["date"], r["day_seq"], r["draw_number"])
                )
                added += 1
        conn.commit()
        # 同步传播：更新 order_history 中的 draw_number（如果之前为 0）
        conn.execute("""
            UPDATE order_history 
            SET draw_number = (
                SELECT draw_number FROM draw_records WHERE draw_records.date = order_history.action_date
            )
            WHERE action_date IN (SELECT date FROM draw_records)
            AND (draw_number IS NULL OR draw_number = 0)
        """)
        conn.commit()
        return {"ok": True, "added": added, "total_warehouse": len(wh_rows)}
    finally:
        conn.close()

@app.post("/api/draw-records")
async def api_create_draw_record(request: Request):
    """新增/更新一条抽签记录（warehouse 推送用）"""
    body = await request.json()
    date = body.get("date", "")
    day_seq = body.get("day_seq", 0)
    draw_number = body.get("draw_number", 0)
    if not date:
        raise HTTPException(400, "date required")
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO draw_records (date, day_seq, draw_number) VALUES (?,?,?)",
                     (date, day_seq, draw_number))
        # 同步传播到 order_history
        conn.execute(
            "UPDATE order_history SET draw_number = ? WHERE action_date = ? AND (draw_number IS NULL OR draw_number = 0)",
            (draw_number, date)
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

@app.delete("/api/draw-records/{rid}")
async def api_delete_draw_record(rid: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM draw_records WHERE id=?", (rid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

# ═══════ 日盈亏记录 ═══════
@app.get("/api/simulate/order-daily-results")
async def api_get_order_daily_results():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, get_order_daily_results)

@app.post("/api/simulate/order-daily-results")
async def api_save_order_daily_result(request: Request):
    body = await request.json()
    date = body.get("date", "")
    result = body.get("result", "")
    if not date or result not in ("win", "loss"):
        raise HTTPException(400, "date and result (win/loss) required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: save_order_daily_result(date, result))

def get_order_daily_results():
    """从 order_history 读取真实开奖数据，自动计算赢/亏（rank≤25=win），
    同时合并 order_daily_results 中的手动覆盖。"""
    import json as _json
    conn = get_db()
    # 1. 读取 order_history（真实数据源）
    oh_rows = conn.execute("""
        SELECT action_date as date, draw_number, amounts_json, created_at as created
        FROM order_history 
        WHERE draw_number > 0 AND amounts_json IS NOT NULL
        ORDER BY action_date DESC LIMIT 60
    """).fetchall()
    
    # 2. 读取手动标记（order_daily_results 中 'win'/'loss' 的条目）
    manual_rows = conn.execute("""
        SELECT date, result, created FROM order_daily_results 
        WHERE result IN ('win','loss')
        ORDER BY date DESC
    """).fetchall()
    manual_map = {r["date"]: r["result"] for r in manual_rows}
    
    results = []
    for r in oh_rows:
        date = r["date"]
        if date in manual_map:
            # 手动覆盖优先
            results.append({"date": date, "result": manual_map[date], "created": r["created"]})
        else:
            # 自动计算：从 amounts 算 draw_number 的排位
            try:
                amounts = _json.loads(r["amounts_json"])
                draw = str(r["draw_number"])
                draw_amt = amounts.get(draw, 0)
                # 计算排位：比它大的有几个
                rank = sum(1 for v in amounts.values() if v > draw_amt) + 1
                result = "win" if rank <= 25 else "loss"
                # 同时保存到 order_daily_results 供后续快速读取
                conn.execute(
                    "INSERT OR REPLACE INTO order_daily_results (date, result) VALUES (?,?)",
                    (date, _json.dumps({"draw_number": r["draw_number"], "rank": rank}))
                )
            except:
                result = "loss"
            results.append({"date": date, "result": result, "created": r["created"]})
    
    conn.commit()
    conn.close()
    return results

def save_order_daily_result(date, result):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO order_daily_results (date, result) VALUES (?,?)", (date, result))
    conn.commit()
    conn.close()
    return {"ok": True}

# ═══════ 门店命中率 ═══════
@app.get("/api/simulate/store-hit-rates")
async def api_store_hit_rates(days: int = 30):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_store_hit_rates(days))

def get_store_hit_rates(days=30):
    """计算正帮扶/负帮扶累计命中率（精准号码匹配）"""
    conn = get_db()
    guides = conn.execute(
        "SELECT date, result FROM sim_guides WHERE result IS NOT NULL ORDER BY date DESC LIMIT ?",
        (days,)
    ).fetchall()
    conn.close()

    if not guides:
        return {"positive": None, "negative": None, "threshold": 53.2}

    dates = [g["date"] for g in guides]
    ph = ",".join(["?" for _ in dates])

    # 批量加载 draw_records
    try:
        wh = sqlite3.connect("/home/xiaolin/projects/number-warehouse/backend/data/warehouse.db")
        wh.row_factory = sqlite3.Row
        d_rows = wh.execute(
            f"SELECT date, draw_number FROM analysis_daily WHERE project_id=19 AND date IN ({ph})", dates
        ).fetchall()
        wh.close()
        draw_map = {r["date"]: r["draw_number"] for r in d_rows}
    except:
        draw_map = {}

    # 批量加载 order_numbers
    conn2 = get_db()
    conn2.row_factory = sqlite3.Row
    on_rows = conn2.execute(
        f"SELECT date, collection_id, threshold, numbers_json FROM order_numbers WHERE date IN ({ph})", dates
    ).fetchall()
    conn2.close()

    STORE_CID = {"一店":-23,"二店":-24,"三店":-25,"四店":-26,"五店":-28,"六店":-29,"集合14":14,"集合16":16}
    CID_STORE = {v:k for k,v in STORE_CID.items()}

    num_idx = {}  # date -> {store: {"正":set, "反":set}}
    for nr in on_rows:
        store = CID_STORE.get(nr["collection_id"])
        if not store: continue
        try: nums = set(json.loads(nr["numbers_json"] or "[]"))
        except: nums = set()
        dt = nr["date"]
        key = "正" if nr["threshold"] == 25 else "反"
        num_idx.setdefault(dt, {}).setdefault(store, {})[key] = nums

    pos_hits, pos_total = 0, 0
    neg_hits, neg_total = 0, 0

    for g in guides:
        try:
            result = json.loads(g["result"]) if isinstance(g["result"], str) else g["result"]
        except: continue

        dt = g["date"]
        draw_num = draw_map.get(dt, 0)
        if not draw_num: continue

        mode = result.get("mode", "")
        alg = result.get("algorithms", [{}])[0] if result.get("algorithms") else {}
        detail = alg.get("detail", [])
        sn = num_idx.get(dt, {})

        for d in detail:
            if not d.get("selected"): continue
            store = d["store"]
            store_mode = d.get("mode", "正")
            target = sn.get(store, {}).get(store_mode, set())
            if not target: continue

            if mode == "positive":
                pos_total += 1
                if draw_num in target: pos_hits += 1
            elif mode == "negative":
                neg_total += 1
                if draw_num in target: neg_hits += 1

    return {
        "positive": {
            "name": "正帮扶",
            "hits": pos_hits,
            "total": pos_total,
            "rate": round(pos_hits / pos_total * 100, 1) if pos_total > 0 else None,
            "below": (pos_hits / pos_total * 100 < 53.2) if pos_total >= 5 else False
        } if pos_total > 0 else None,
        "negative": {
            "name": "负帮扶",
            "hits": neg_hits,
            "total": neg_total,
            "rate": round(neg_hits / neg_total * 100, 1) if neg_total > 0 else None,
            "below": (neg_hits / neg_total * 100 < 53.2) if neg_total >= 5 else False
        } if neg_total > 0 else None,
        "threshold": 53.2,
        "days": days
    }

# ── 算法优化日志 ──
OPT_LOG_FILE = os.path.join(os.path.dirname(__file__), "optimization_log.json")

@app.get("/api/simulate/optimization-log")
async def api_optimization_log():
    try:
        with open(OPT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(content={"ok": True, "log": data})
    except FileNotFoundError:
        return JSONResponse(content={"ok": True, "log": []})

# ═══════════════ Static + SPA ═══════════════
STATIC_DIR = os.path.join(BASE_DIR, "static")
if os.path.isdir(STATIC_DIR):
    @app.get("/")
    async def index():
        fp = os.path.join(STATIC_DIR, "funds-v2.html")
        mtime = os.path.getmtime(fp)
        return FileResponse(fp, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0",
            "ETag": '"funds-v2-' + str(int(mtime)) + '"'
        })

    @app.get("/{path:path}")
    async def serve_static(path: str):
        fp = os.path.join(STATIC_DIR, path)
        if os.path.isfile(fp):
            return FileResponse(fp, headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "ETag": '"' + path + '-' + str(int(os.path.getmtime(fp))) + '"'
            })
        return FileResponse(os.path.join(STATIC_DIR, "funds-v2.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)



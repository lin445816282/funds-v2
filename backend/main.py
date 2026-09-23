"""funds-v2 独立服务 — 多门店资金看板 + 预测数据 API"""
import os, json, uuid, secrets, asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
import math, random

_executor = ThreadPoolExecutor(max_workers=8)
from fastapi.middleware.cors import CORSMiddleware
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "funds-v2.db")
PASSWORD = "8283103"
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
        CREATE TABLE IF NOT EXISTS strategy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ts TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            mode TEXT NOT NULL,
            algorithm TEXT,
            params_json TEXT,
            total_profit REAL,
            total_shots INTEGER,
            total_hits INTEGER,
            hit_rate REAL,
            max_drawdown REAL,
            backfilled INTEGER NOT NULL DEFAULT 0,
            actual_profit REAL,
            actual_capital REAL,
            next_day_capital INTEGER
        );
        CREATE TABLE IF NOT EXISTS draw_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            day_seq INTEGER NOT NULL,
            draw_number INTEGER NOT NULL,
            synced_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS ladder_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calc_date TEXT NOT NULL,
            action_date TEXT NOT NULL DEFAULT '',
            scheme TEXT NOT NULL DEFAULT '',
            chips_json TEXT NOT NULL,
            numbers_json TEXT NOT NULL,
            order_json TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(calc_date, action_date, scheme)
        );
    """)
    # 迁移：旧表无 scheme 列时重建（保留数据，scheme 从 order_json 提取）
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(ladder_snapshot)").fetchall()]
    if "scheme" not in _cols:
        conn.execute("ALTER TABLE ladder_snapshot RENAME TO ladder_snapshot_old")
        conn.execute("""
            CREATE TABLE ladder_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calc_date TEXT NOT NULL,
                action_date TEXT NOT NULL DEFAULT '',
                scheme TEXT NOT NULL DEFAULT '',
                chips_json TEXT NOT NULL,
                numbers_json TEXT NOT NULL,
                order_json TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(calc_date, action_date, scheme)
            )
        """)
        _rows = conn.execute("SELECT * FROM ladder_snapshot_old ORDER BY id").fetchall()
        for _r in _rows:
            _scheme = ""
            try:
                _oj = json.loads(_r["order_json"]) if _r["order_json"] else {}
                _scheme = _oj.get("scheme") or ""
            except Exception:
                _scheme = ""
            conn.execute(
                "INSERT INTO ladder_snapshot (id, calc_date, action_date, scheme, chips_json, numbers_json, order_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_r["id"], _r["calc_date"], _r["action_date"], _scheme, _r["chips_json"], _r["numbers_json"], _r["order_json"], _r["created_at"])
            )
        conn.execute("DROP TABLE ladder_snapshot_old")
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
            "server_today": datetime.now().strftime("%Y-%m-%d"),
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
    """外部系统推送流水记录 — upsert（新数据覆盖旧数据）"""
    check_external_key(request)
    data = await request.json()
    conn = get_db()
    try:
        rec_count = 0
        if "records" in data:
            for r in data["records"]:
                amount = float(r.get("amount", 0))
                note = str(r.get("note", ""))
                store = str(r["store"])
                date = str(r["date"])
                category = str(r["category"])
                
                # 跳过无效数据（amount=None/0 可能是未计算完成）
                if amount is None or amount == 0:
                    continue
                    
                conn.execute(
                    "INSERT INTO records (store, date, category, amount, note) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(date,store,category) DO UPDATE SET amount=excluded.amount, note=excluded.note",
                    (store, date, category, amount, note)
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
from simulate import get_simulate_data, run_optimize, run_manual, run_daily_guide, get_guide_history, get_optimization_log, get_order_sheet, pull_threshold_numbers, list_order_numbers, delete_order_numbers_by_date, get_order_numbers_detail, save_order_amounts, get_order_amounts, save_order_history, get_order_history, ack_order_history, load_data, optimize, STORE_NAMES, batch_generate_guides, generate_guides_only, run_single_day, run_single_guide, run_le25_optimize, get_l3_bestcombo_daily

@app.get("/api/simulate/data")
async def api_simulate_data(request: Request, days: int = 90):
    await require_auth(request)
    return get_simulate_data(days)

@app.post("/api/simulate/optimize")
async def api_simulate_optimize(request: Request):
    await require_auth(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_optimize(
        days=data.get("days", 90),
        mode=data.get("mode", "positive"),
        algorithm=data.get("algorithm", "coordinate")
    ))

@app.post("/api/simulate/le25-optimize")
async def api_le25_optimize(request: Request):
    await require_auth(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    year = data.get("year", None)
    return await loop.run_in_executor(_executor, lambda: run_le25_optimize(
        days=data.get("days", 90),
        year=year
    ))

@app.post("/api/simulate/manual")
async def api_simulate_manual(request: Request):
    await require_auth(request)
    data = await request.json()
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_manual(
        stores_config=data.get("stores", []),
        days=data.get("days", 90),
        algorithm=data.get("algorithm")
    ))

@app.get("/api/simulate/daily-guide")
async def api_daily_guide(request: Request, days: int = 90, mode: str = "positive", max_iter: int = 10):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: run_daily_guide(days, mode, max_iter))

@app.get("/api/simulate/guide-history")
async def api_guide_history(request: Request, limit: int = 30, mode: str = None, offset: int = 0, light: int = 0):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_guide_history(limit, mode, offset, bool(light)))

@app.get("/api/simulate/optimization-log")
async def api_optimization_log(request: Request, ):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, get_optimization_log)

@app.get("/api/simulate/order-sheet")
async def api_order_sheet(request: Request, days: int = 90, date: str = None, guide_date: str = None):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, lambda: get_order_sheet(days, target_date=date, guide_date=guide_date))
    return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/api/simulate/l3-bestcombo-daily")
async def api_l3_bestcombo_daily(request: Request):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, get_l3_bestcombo_daily)
    return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.post("/api/simulate/pull-numbers")
async def api_pull_numbers(request: Request, date: str = None, from_date: str = None, to_date: str = None):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: pull_threshold_numbers(
        target_date=date, from_date=from_date, to_date=to_date
    ))

@app.get("/api/simulate/order-numbers-list")
async def api_order_numbers_list(request: Request, page: int = 1, limit: int = 20):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: list_order_numbers(page, limit))

@app.get("/api/simulate/order-numbers-detail")
async def api_order_numbers_detail(request: Request, date: str = ""):
    await require_auth(request)
    if not date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_order_numbers_detail(date))

@app.delete("/api/simulate/order-numbers")
async def api_order_numbers_delete(request: Request, date: str = ""):
    await require_auth(request)
    if not date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: delete_order_numbers_by_date(date))

# ── 下单金额 ──
@app.post("/api/simulate/order-amounts")
async def api_save_order_amounts(request: Request, data: dict):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: save_order_amounts(
        data.get("date", ""), data.get("stores", [])
    ))

@app.get("/api/simulate/order-amounts")
async def api_get_order_amounts(request: Request, date: str = None, list_all: bool = False):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_order_amounts(date=date, list_all=list_all))

# ── 下单历史（独立表）──
@app.get("/api/simulate/order-history")
async def api_get_order_history(request: Request, limit: int = 30, offset: int = 0, store: str = None, stores: str = None, date_from: str = None, date_to: str = None):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, lambda: get_order_history(limit, offset, store, stores, date_from, date_to))
    return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.post("/api/simulate/order-history/confirm")
async def api_confirm_order_history(request: Request):
    await require_auth(request)
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
    await require_auth(request)
    """用户确认/撤销某个出手日的下单记录"""
    body = await request.json()
    action_date = body.get("action_date", "")
    acknowledged = body.get("acknowledged", True)
    if not action_date:
        raise HTTPException(400, "action_date required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: ack_order_history(action_date, acknowledged))


# ═══════ 楼梯下单快照（56组筹码+号码投票+下单）═══════
@app.post("/api/simulate/ladder-snapshot")
async def api_save_ladder_snapshot(request: Request):
    """保存楼梯下单三步快照（用户主动触发）"""
    await require_auth(request)
    body = await request.json()
    calc_date = body.get("calc_date", "")
    action_date = body.get("action_date", "")
    scheme = body.get("scheme", "") or ""
    chips = body.get("chips", None)
    numbers = body.get("numbers", None)
    order = body.get("order", None)
    if not calc_date or chips is None:
        raise HTTPException(400, "calc_date and chips required")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO ladder_snapshot (calc_date, action_date, scheme, chips_json, numbers_json, order_json) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(calc_date, action_date, scheme) DO UPDATE SET "
            "chips_json=excluded.chips_json, numbers_json=excluded.numbers_json, "
            "order_json=excluded.order_json, created_at=datetime('now','localtime')",
            (calc_date, action_date, scheme,
             json.dumps(chips, ensure_ascii=False),
             json.dumps(numbers, ensure_ascii=False),
             json.dumps(order, ensure_ascii=False) if order is not None else "")
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/simulate/ladder-snapshot")
async def api_get_ladder_snapshot(request: Request, calc_date: str = None, scheme: str = None):
    """读取楼梯下单快照（指定 calc_date 查该天，可加 scheme 区分方案；否则最新一份）"""
    await require_auth(request)
    conn = get_db()
    try:
        if calc_date:
            if scheme:
                row = conn.execute("SELECT * FROM ladder_snapshot WHERE calc_date=? AND scheme=? ORDER BY id DESC LIMIT 1", (calc_date, scheme)).fetchone()
            else:
                row = conn.execute("SELECT * FROM ladder_snapshot WHERE calc_date=? ORDER BY id DESC LIMIT 1", (calc_date,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM ladder_snapshot ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {"ok": True, "snapshot": None}
        return {"ok": True, "snapshot": {
            "calc_date": row["calc_date"],
            "action_date": row["action_date"],
            "chips": json.loads(row["chips_json"]),
            "numbers": json.loads(row["numbers_json"]),
            "order": json.loads(row["order_json"]) if row["order_json"] else None,
            "created_at": row["created_at"]
        }}
    finally:
        conn.close()


def _ladder_profit(order_json, draw_number):
    """楼梯下单盈亏 = 开奖号下单额×47 − 总下单额（未买中开奖号则 −total）"""
    try:
        oj = json.loads(order_json) if order_json else {}
        na = oj.get("numAmounts") or {}
        total = oj.get("totalAmt") or 0
        draw_amt = na.get(str(draw_number), 0)
        return round(draw_amt * 47 - total, 2)
    except Exception:
        return None


def _ladder_order_stats(conn):
    """从 ladder_snapshot 算按月/按周统计，按 scheme 独立分组（盈亏 numAmounts×47−total）"""
    draw_rows = conn.execute("SELECT date, draw_number FROM draw_records").fetchall()
    draw_map = {r["date"]: r["draw_number"] for r in draw_rows}
    rows = conn.execute("SELECT calc_date, scheme, order_json FROM ladder_snapshot ORDER BY calc_date").fetchall()
    schemes = {}  # scheme -> {"monthly": {...}, "weekly": {...}}
    for r in rows:
        d = r["calc_date"]
        sch = r["scheme"] or "default"
        dn = draw_map.get(d) or 0
        if dn <= 0:
            continue
        op = _ladder_profit(r["order_json"], dn)
        if op is None:
            continue
        st = schemes.setdefault(sch, {"monthly": {}, "weekly": {}})
        monthly = st["monthly"]
        weekly = st["weekly"]
        m = d[:7]
        monthly.setdefault(m, {"win": 0, "loss": 0, "profit": 0.0, "total": 0})
        monthly[m]["total"] += 1
        monthly[m]["profit"] = round(monthly[m]["profit"] + op, 2)
        if op > 0:
            monthly[m]["win"] += 1
        else:
            monthly[m]["loss"] += 1
        try:
            dd = datetime.strptime(d, "%Y-%m-%d")
            monday = dd - timedelta(days=dd.weekday())
            wkey = monday.strftime("%Y-%m-%d")
            wlabel = monday.strftime("%m-%d")
        except Exception:
            wkey = d[:7]
            wlabel = d[:7]
        weekly.setdefault(wkey, {"win": 0, "loss": 0, "profit": 0.0, "total": 0, "label": wlabel})
        weekly[wkey]["total"] += 1
        weekly[wkey]["profit"] = round(weekly[wkey]["profit"] + op, 2)
        if op > 0:
            weekly[wkey]["win"] += 1
        else:
            weekly[wkey]["loss"] += 1

    def _fmt(m):
        out = []
        for k in sorted(m.keys()):
            v = dict(m[k])
            v["label"] = k
            v["rate"] = round(v["win"] / v["total"] * 100, 1) if v["total"] else 0
            out.append(v)
        return out

    out = {}
    for sch, st in schemes.items():
        out[sch] = {"monthly": _fmt(st["monthly"]), "weekly": _fmt(st["weekly"])}
    return out


@app.get("/api/simulate/ladder-snapshots")
async def api_list_ladder_snapshots(request: Request):
    """列出所有楼梯下单快照（倒序），附带每天开奖结果 + 按月/周统计"""
    await require_auth(request)
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, calc_date, action_date, scheme, created_at, order_json FROM ladder_snapshot ORDER BY id DESC").fetchall()
        draw_rows = conn.execute("SELECT date, draw_number FROM draw_records").fetchall()
        draw_map = {r["date"]: r["draw_number"] for r in draw_rows}
        snapshots = []
        for r in rows:
            dn = draw_map.get(r["calc_date"]) or 0
            own_profit = _ladder_profit(r["order_json"], dn) if dn > 0 else None
            if dn > 0 and own_profit is not None:
                result = "win" if own_profit > 0 else "loss"
            else:
                result = "pending"
            snapshots.append({
                "id": r["id"], "calc_date": r["calc_date"], "action_date": r["action_date"],
                "scheme": r["scheme"], "created_at": r["created_at"],
                "draw_number": dn, "own_profit": own_profit, "result": result
            })
        stats = _ladder_order_stats(conn)
        return {"ok": True, "snapshots": snapshots, "stats": stats}
    finally:
        conn.close()


# ═══════ 抽签记录（从 warehouse 同步）═══════
@app.get("/api/draw-records")
async def api_draw_records(request: Request, page: int = 1, page_size: int = 30):
    await require_auth(request)
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
async def api_draw_records_sync(request: Request, ):
    await require_auth(request)
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

@app.post("/api/draw-records/auto-sync")
async def api_draw_records_auto_sync(request: Request, days: int = 30):
    await require_auth(request)
    """增量同步最近N天抽签记录 → 仅新增不覆盖 → 下单tab打开时自动触发"""
    from datetime import datetime as dt, timedelta
    cutoff = (dt.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        wh = sqlite3.connect("/home/xiaolin/projects/number-warehouse/backend/data/warehouse.db")
        wh.row_factory = sqlite3.Row
        wh_rows = wh.execute(
            "SELECT date, day_seq, draw_number FROM records WHERE date >= ? ORDER BY date",
            (cutoff,)
        ).fetchall()
        wh.close()
    except Exception as e:
        return {"ok": False, "error": f"读取warehouse失败: {e}"}

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
        # 同步传播：更新 order_history 中之前为0的 draw_number
        if added > 0:
            conn.execute("""
                UPDATE order_history 
                SET draw_number = (
                    SELECT draw_number FROM draw_records WHERE draw_records.date = order_history.action_date
                )
                WHERE action_date IN (SELECT date FROM draw_records WHERE date >= ?)
                AND (draw_number IS NULL OR draw_number = 0)
            """, (cutoff,))
            conn.commit()
        return {"ok": True, "added": added}
    finally:
        conn.close()

@app.post("/api/draw-records")
async def api_create_draw_record(request: Request):
    await require_auth(request)
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
async def api_delete_draw_record(request: Request, rid: int):
    await require_auth(request)
    conn = get_db()
    try:
        conn.execute("DELETE FROM draw_records WHERE id=?", (rid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

# ═══════ 日盈亏记录 ═══════
@app.get("/api/simulate/order-daily-results")
async def api_get_order_daily_results(request: Request, ):
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, get_order_daily_results)

@app.post("/api/simulate/order-daily-results")
async def api_save_order_daily_result(request: Request):
    await require_auth(request)
    body = await request.json()
    date = body.get("date", "")
    result = body.get("result", "")
    if not date or result not in ("win", "loss"):
        raise HTTPException(400, "date and result (win/loss) required")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: save_order_daily_result(date, result))

def get_order_daily_results():
    """从 order_history 读取真实开奖数据，自动计算赢/亏（own_profit>0=win），
    同时合并 order_daily_results 中的手动覆盖。"""
    import json as _json
    conn = get_db()
    # 1. 读取 order_history（真实数据源）
    oh_rows = conn.execute("""
        SELECT action_date as date, draw_number, amounts_json, own_profit, created_at as created
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
            # 自动计算：用真实盈亏 own_profit 判断（own_profit>0=win，≤0=loss）
            try:
                op = r["own_profit"]
                if op is None:
                    # own_profit 为空时从 amounts 现算（draw_amt*47 - total_bet）
                    amounts = _json.loads(r["amounts_json"])
                    draw = str(r["draw_number"])
                    draw_amt = amounts.get(draw, 0)
                    total_bet = sum(amounts.values())
                    op = round(draw_amt * 47 - total_bet, 2)
                result = "win" if (op > 0) else "loss"
            except Exception:
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
async def api_store_hit_rates(request: Request, days: int = 30):
    await require_auth(request)
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
    except Exception:
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
        except Exception: nums = set()
        dt = nr["date"]
        key = "正" if nr["threshold"] == 25 else "反"
        num_idx.setdefault(dt, {}).setdefault(store, {})[key] = nums

    pos_hits, pos_total = 0, 0
    neg_hits, neg_total = 0, 0

    for g in guides:
        try:
            result = json.loads(g["result"]) if isinstance(g["result"], str) else g["result"]
        except Exception: continue

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

# ═══════ Kelly 分析 ═══════
@app.get("/api/simulate/kelly-analysis")
async def api_kelly_analysis(request: Request, days: int = 90, capital: int = 100000):
    """Kelly Criterion 分析 — 多窗口对比 + 共识推荐"""
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: compute_kelly_analysis(days, capital))

def _compute_store_hit_rates(db, days):
    """从 order_history 提取各门店×模式的命中统计"""
    ODDS = {"positive": 22/25, "negative": 23/24}
    STORE_ALIAS = {"集合14":"集合14", "集合16":"集合16"}

    rows = db.execute(
        f"SELECT action_date, stores_json, rankings_json, draw_number "
        f"FROM order_history "
        f"WHERE stores_json IS NOT NULL AND rankings_json IS NOT NULL "
        f"AND action_date >= date('now', '-{days} days') "
        f"ORDER BY action_date DESC"
    ).fetchall()

    store_data = {}
    for r in rows:
        try:
            stores = json.loads(r["stores_json"]) if isinstance(r["stores_json"], str) else r["stores_json"]
            rankings = json.loads(r["rankings_json"]) if isinstance(r["rankings_json"], str) else r["rankings_json"]
        except Exception:
            continue
        draw = r["draw_number"] or 0
        if draw <= 0 or not rankings:
            continue

        for s in stores:
            name = s.get("store", "")
            name = STORE_ALIAS.get(name, name)
            mode = s.get("mode", "positive")
            raw_rank = rankings.get(name) or rankings.get(s.get("store", ""))
            if raw_rank is None:
                continue

            key = f"{name}|{mode}"
            if key not in store_data:
                store_data[key] = {"hits": 0, "misses": 0, "shares": 0, "total_capital": 0}

            if mode == "positive":
                hit = (raw_rank <= draw)
            else:
                hit = (raw_rank > draw)
            if hit:
                store_data[key]["hits"] += 1
            else:
                store_data[key]["misses"] += 1
            store_data[key]["shares"] += 1
            store_data[key]["total_capital"] += s.get("capital", 0)

    # 构建结果
    STORE_NAMES = ["一店","二店","三店","四店","五店","六店","集合14","集合16"]
    result = {"stores": [], "days": days, "total_dates": len(rows)}

    pos_betable, neg_betable = [], []
    for store in STORE_NAMES:
        entry = {"store": store, "positive": None, "negative": None}
        for mode in ["positive", "negative"]:
            key = f"{store}|{mode}"
            s = store_data.get(key, {"hits": 0, "misses": 0, "shares": 0, "total_capital": 0})
            total = s["hits"] + s["misses"]
            if total < 5:
                continue

            wr = s["hits"] / total
            b = ODDS[mode]
            kelly_full = max(0, (b * wr - (1 - wr)) / b) if b > 0 else 0
            is_betable = kelly_full > 0

            entry[mode] = {
                "hits": s["hits"], "total": total,
                "win_rate": round(wr * 100, 1),
                "kelly_full": round(kelly_full * 100, 1),
                "kelly_half": round(kelly_full * 50, 1),
                "is_betable": is_betable,
                "shares": s["shares"],
            }
            if is_betable:
                (pos_betable if mode == "positive" else neg_betable).append({
                    "store": store, "kelly": round(kelly_full*100,1), "wr": round(wr*100,1)
                })

        result["stores"].append(entry)

    result["positive_betable"] = sorted(pos_betable, key=lambda x: -x["kelly"])
    result["negative_betable"] = sorted(neg_betable, key=lambda x: -x["kelly"])
    return result

def compute_kelly_analysis(days=90, bankroll=100000):
    """多窗口 Kelly 对比 + 共识推荐"""
    WINDOWS = [30, 60, 90]
    db = get_db()

    # 逐窗口计算
    window_results = {}
    for w in WINDOWS:
        window_results[w] = _compute_store_hit_rates(db, w)
    db.close()

    STORE_NAMES = ["一店","二店","三店","四店","五店","六店","集合14","集合16"]
    MODES = ["positive", "negative"]
    ODDS = {"positive": 22/25, "negative": 23/24}

    # ── 共识分析 ──
    consensus_stores = []
    final_picks = {"positive": [], "negative": []}

    for store in STORE_NAMES:
        entry = {"store": store, "modes": {}}
        for mode in MODES:
            mode_data = {"windows": {}, "stability": 0, "trend": "stable", "consensus": None}

            # 收集各窗口数据
            window_kellys = []
            window_wrs = []
            window_betable = []
            for w in WINDOWS:
                wr_data = window_results[w]
                for s in wr_data["stores"]:
                    if s["store"] == store and s[mode]:
                        m = s[mode]
                        window_kellys.append(m["kelly_full"])
                        window_wrs.append(m["win_rate"])
                        window_betable.append(m["is_betable"])
                        mode_data["windows"][str(w)] = {
                            "win_rate": m["win_rate"],
                            "kelly_full": m["kelly_full"],
                            "kelly_half": m["kelly_half"],
                            "is_betable": m["is_betable"],
                            "hits": m["hits"],
                            "total": m["total"],
                        }
                        break
                else:
                    mode_data["windows"][str(w)] = None

            if not window_kellys:
                entry["modes"][mode] = mode_data
                continue

            # 稳定性 = 有多少窗口可投
            mode_data["stability"] = sum(1 for b in window_betable if b)

            # 趋势：比较30d和90d的胜率
            if len(window_wrs) >= 2 and window_wrs[0] is not None and window_wrs[-1] is not None:
                diff = window_wrs[0] - window_wrs[-1]  # 30d - 90d
                if diff > 3:
                    mode_data["trend"] = "improving"  # 近期更好
                elif diff < -3:
                    mode_data["trend"] = "declining"  # 近期变差

            # 共识推荐：仅当稳定性≥2才推荐，取保守Kelly
            betable_kellys = [k for i, k in enumerate(window_kellys) if window_betable[i]]
            if betable_kellys and mode_data["stability"] >= 2:
                conservative_kelly = min(betable_kellys)
                avg_wr = sum(w for w in window_wrs if w) / max(1, sum(1 for w in window_wrs if w))
                b_odds = 22/25 if mode == "positive" else 23/24

                mc = _monte_carlo_projection(avg_wr/100, b_odds, conservative_kelly/100, bankroll)
                mode_data["consensus"] = {
                    "kelly_pct": conservative_kelly,
                    "kelly_half_pct": round(conservative_kelly / 2, 1),
                    "win_rate_avg": round(avg_wr, 1),
                    "recommend_bet": round(bankroll * conservative_kelly / 100),
                    "recommend_half_bet": round(bankroll * conservative_kelly / 200),
                    "projection": mc,
                    "confidence": "高" if mode_data["stability"] >= 3 else "中",
                }
                final_picks[mode].append({
                    "store": store,
                    "kelly": conservative_kelly,
                    "half_kelly": round(conservative_kelly / 2, 1),
                    "wr": round(avg_wr, 1),
                    "stability": mode_data["stability"],
                    "trend": mode_data["trend"],
                    "confidence": mode_data["consensus"]["confidence"],
                })

            entry["modes"][mode] = mode_data
        consensus_stores.append(entry)

    # 排序
    for mode in MODES:
        final_picks[mode].sort(key=lambda x: (-x["stability"], -x["kelly"]))

    # 生成最终建议文本
    advice = _generate_advice(final_picks, bankroll)

    return {
        "overview": {"bankroll": bankroll, "windows": WINDOWS},
        "windows": {
            str(w): {
                "days": w,
                "total_dates": window_results[w]["total_dates"],
                "positive_betable": window_results[w]["positive_betable"],
                "negative_betable": window_results[w]["negative_betable"],
                "stores": window_results[w]["stores"],
            }
            for w in WINDOWS
        },
        "consensus": {
            "stores": consensus_stores,
            "final_picks": final_picks,
            "advice": advice,
        }
    }

def _generate_advice(final_picks, bankroll):
    """根据共识结果生成自然语言建议"""
    lines = []
    for mode, label in [("positive", "正向"), ("negative", "反向")]:
        picks = final_picks.get(mode, [])
        if not picks:
            lines.append(f"❌ {label}：无稳定可投门店（所有窗口胜率不足保本线）")
            continue

        stable = [p for p in picks if p["stability"] >= 2]
        if not stable:
            lines.append(f"⚠️ {label}：无多窗口共识门店，不建议出手")
            continue

        top = stable[0]
        trend_emoji = {"improving": "📈", "declining": "📉", "stable": "➡️"}
        lines.append(
            f"✅ {label}首选：{top['store']} "
            f"（半Kelly {top['half_kelly']}%，胜率{top['wr']}%，"
            f"稳定性{top['stability']}/3窗 {trend_emoji.get(top['trend'],'')}）"
        )
        for p in stable[1:3]:
            lines.append(f"   备选：{p['store']}（半Kelly {p['half_kelly']}%，稳定性{p['stability']}/3）")

    # 总建议
    total_half_kelly = sum(p["half_kelly"] for mode_picks in final_picks.values() for p in mode_picks if p["stability"] >= 2)
    if total_half_kelly > 0:
        lines.append(f"\n💡 建议以半Kelly分散{len([p for mp in final_picks.values() for p in mp if p['stability']>=2])}家门店，"
                     f"总仓位约{round(total_half_kelly,1)}%（{round(bankroll*total_half_kelly/100):,}元/{bankroll:,}元）")
    else:
        lines.append("\n🛑 当前无可投门店，建议观望等待胜率回升")

    return lines

def _monte_carlo_projection(win_rate, odds, kelly_fraction, bankroll, n_days=30, n_sims=2000):
    """蒙特卡洛模拟：Kelly 仓位下 N 天后的资金分布"""
    outcomes = []
    for _ in range(n_sims):
        br = bankroll
        for _ in range(n_days):
            bet = br * kelly_fraction
            if random.random() < win_rate:
                br += bet * odds  # win
            else:
                br -= bet         # lose
        outcomes.append(br)

    outcomes.sort()
    p10 = outcomes[int(n_sims * 0.10)]
    p25 = outcomes[int(n_sims * 0.25)]
    p50 = outcomes[int(n_sims * 0.50)]
    p75 = outcomes[int(n_sims * 0.75)]
    p90 = outcomes[int(n_sims * 0.90)]
    avg = sum(outcomes) / n_sims
    growth = (p50 / bankroll - 1) * 100 if bankroll > 0 else 0

    return {
        "n_days": n_days,
        "n_sims": n_sims,
        "bankroll": bankroll,
        "worst_p10": round(p10),
        "worst_p25": round(p25),
        "median": round(p50),
        "best_p75": round(p75),
        "best_p90": round(p90),
        "average": round(avg),
        "median_growth_pct": round(growth, 1),
    }

# ═══════════════ 策略最优保存 & 回填 ═══════════════
# NOTE: 必须在 SPA catch-all 之前注册，否则会被 /{path:path} 拦截

@app.get("/api/strategy/optimal")
async def api_strategy_optimal(request: Request, save: int = 0):
    """运行坐标下降+均匀算法，取最优策略保存到 strategy_log
    save=1 时写入 DB"""
    await require_auth(request)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_executor, load_data, 90)
    if not data:
        return {"ok": False, "error": "无数据"}

    modes = ["positive", "negative"]
    algorithms = ["coordinate", "uniform", "stop_neg2", "positive_only"]
    today = datetime.now().strftime("%Y-%m-%d")

    best_overall = None

    for mode in modes:
        for algo in algorithms:
            params, result = optimize(data, mode=mode, algorithm=algo, max_iter=10)
            if params is None or result is None:
                continue
            profit = result.get("total_profit", 0)
            if best_overall is None or profit > best_overall["result"]["total_profit"]:
                best_overall = {
                    "mode": mode,
                    "algorithm": algo,
                    "params": params,
                    "result": result,
                }

    if not best_overall:
        return {"ok": False, "error": "优化无结果"}

    result = best_overall["result"]
    saved_id = None
    if save:
        conn = get_db()
        import json as _j
        conn.execute("INSERT INTO strategy_log (date, mode, algorithm, params_json, total_profit, total_shots, total_hits, hit_rate, max_drawdown) VALUES (?,?,?,?,?,?,?,?,?)", (
            today,
            best_overall["mode"],
            best_overall["algorithm"],
            _j.dumps(best_overall["params"], ensure_ascii=False),
            result["total_profit"],
            result["total_shots"],
            result["total_hits"],
            result["hit_rate"],
            result["max_drawdown"],
        ))
        conn.commit()
        saved_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()

    return {
        "ok": True,
        "saved": bool(save),
        "saved_id": saved_id,
        "date": today,
        "mode": best_overall["mode"],
        "algorithm": best_overall["algorithm"],
        "params": best_overall["params"],
        "result": result,
        "store_details": [
            {"name": s,
             "threshold": best_overall["params"][s]["threshold"],
             "capital": best_overall["params"][s]["capital"],
             "mode": best_overall["params"][s]["mode"]}
            for s in STORE_NAMES
        ],
    }


@app.post("/api/strategy/backfill")
async def api_strategy_backfill(request: Request):
    """回填昨日实际盈亏到 strategy_log（从 order_history 读取）"""
    await require_auth(request)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_db()
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT action_date, mode, stores_json, total_capital, draw_number, own_profit, own_capital, amounts_json FROM order_history WHERE action_date=? ORDER BY id",
        (yesterday,)
    ).fetchall()

    if not rows:
        return {"ok": True, "date": yesterday, "backfilled": False, "reason": "昨日无出手记录"}

    total_actual_profit = sum(r["own_profit"] or 0 for r in rows)
    total_actual_capital = sum(r["own_capital"] or 0 for r in rows)

    # Update strategy_log for yesterday
    conn.execute(
        "UPDATE strategy_log SET backfilled=1, actual_profit=?, actual_capital=? WHERE date=? AND backfilled=0",
        (total_actual_profit, total_actual_capital, yesterday)
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "date": yesterday,
        "backfilled": bool(updated),
        "updated_rows": len(rows),
        "actual_profit": total_actual_profit,
        "actual_capital": total_actual_capital,
    }

# ── 算法优化日志 ──

# ═══════════════ 方案A 自动同步 ═══════════════
def sync_scheme_a_if_stale():
    """方案A 数据落后于排位数据时，自动重算同步（结论 tab 每次加载前调用）"""
    try:
        conn = get_db()
        latest_daily = conn.execute("SELECT MAX(date) FROM ladder_scheme_a_daily").fetchone()[0]
        latest_records = conn.execute("SELECT MAX(date) FROM records WHERE category='cat_1783487972049'").fetchone()[0]
        conn.close()
        if latest_records and (not latest_daily or latest_records > latest_daily):
            import regen_scheme_a
            new_date = regen_scheme_a.sync_to_db()
            return True, new_date
        return False, latest_daily
    except Exception:
        return False, None


# ═══════════════ Static + SPA ═══════════════
@app.get("/api/track/ladder-scheme-a")
async def api_ladder_scheme_a(request: Request):
    """方案A 真实下单逐天数据（ladder_scheme_a_daily 表）"""
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_ladder_scheme_a())


def get_ladder_scheme_a():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT date, profit, capital, withdraw, bankrupt FROM ladder_scheme_a_daily ORDER BY date").fetchall()
    conn.close()
    if not rows:
        return {"ok": False, "error": "无数据"}
    daily = [{"date": r["date"], "profit": r["profit"], "capital": r["capital"],
              "withdraw": r["withdraw"], "bankrupt": r["bankrupt"]} for r in rows]
    monthly = {}
    for r in daily:
        monthly.setdefault(r["date"][:7], 0)
        monthly[r["date"][:7]] += r["profit"]
    last = daily[-1]
    total_wd = sum(r["withdraw"] for r in daily)
    return {
        "ok": True,
        "scheme": "方案A · 0-70（达朗贝尔±5 真实下单）",
        "daily": daily,
        "monthly": [{"month": k, "profit": monthly[k]} for k in sorted(monthly)],
        "summary": {
            "final_capital": last["capital"], "total_withdraw": total_wd,
            "surface_profit": last["capital"] + total_wd, "bankrupt": last["bankrupt"],
            "days": len(daily), "start": daily[0]["date"], "end": last["date"]
        }
    }


@app.get("/api/track/ladder-scheme-a-full")
async def api_ladder_scheme_a_full(request: Request, from_date: str = None, to_date: str = None):
    """方案A 完整查询：逐日(含命中率) + 分月 + 分周 + 指标，支持日期段"""
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_ladder_scheme_a_full(from_date, to_date))


def get_ladder_scheme_a_full(from_date=None, to_date=None):
    import datetime as _dt
    from collections import defaultdict
    sync_scheme_a_if_stale()  # 结论 tab 自动更新：数据落后即重算
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, profit, order_amount, capital, withdraw, bankrupt, hits, total FROM ladder_scheme_a_daily ORDER BY date"
    ).fetchall()
    conn.close()

    daily = [{"date": r["date"], "profit": r["profit"], "order_amount": r["order_amount"],
              "capital": r["capital"], "withdraw": r["withdraw"], "bankrupt": r["bankrupt"],
              "hits": r["hits"], "total": r["total"],
              "rate": round(r["hits"] / r["total"] * 100, 1) if r["total"] else None} for r in rows]

    if from_date:
        daily = [d for d in daily if d["date"] >= from_date]
    if to_date:
        daily = [d for d in daily if d["date"] <= to_date]
    if not daily:
        return {"ok": True, "daily": [], "monthly": [], "weekly": [], "summary": None}

    monthly = defaultdict(lambda: {"profit": 0, "order_amount": 0, "hits": 0, "total": 0})
    for d in daily:
        m = d["date"][:7]
        monthly[m]["profit"] += d["profit"]
        monthly[m]["order_amount"] += d["order_amount"]
        monthly[m]["hits"] += d["hits"]
        monthly[m]["total"] += d["total"]
    monthly_out = [{"month": k, "profit": monthly[k]["profit"], "order_amount": monthly[k]["order_amount"],
                    "hits": monthly[k]["hits"], "total": monthly[k]["total"],
                    "rate": round(monthly[k]["hits"] / monthly[k]["total"] * 100, 1) if monthly[k]["total"] else None}
                   for k in sorted(monthly)]

    weekly = defaultdict(lambda: {"profit": 0, "order_amount": 0})
    for d in daily:
        dd = _dt.datetime.strptime(d["date"], "%Y-%m-%d")
        monday = dd - _dt.timedelta(days=dd.weekday())
        wk = monday.strftime("%Y-%m-%d")
        weekly[wk]["profit"] += d["profit"]
        weekly[wk]["order_amount"] += d["order_amount"]
    weekly_out = [{"week": k, "profit": weekly[k]["profit"], "order_amount": weekly[k]["order_amount"]}
                  for k in sorted(weekly)]

    last = daily[-1]
    total_profit = sum(d["profit"] for d in daily)
    total_order = sum(d["order_amount"] for d in daily)
    total_wd = sum(d["withdraw"] for d in daily)
    total_hits = sum(d["hits"] for d in daily)
    total_cnt = sum(d["total"] for d in daily)
    peak = 0
    max_dd = 0
    for d in daily:
        if d["capital"] > peak:
            peak = d["capital"]
        dd_ = peak - d["capital"]
        if dd_ > max_dd:
            max_dd = dd_
    win_days = sum(1 for d in daily if d["profit"] > 0)
    loss_days = sum(1 for d in daily if d["profit"] < 0)
    summary = {
        "start": daily[0]["date"], "end": last["date"], "days": len(daily),
        "total_profit": total_profit, "total_order_amount": total_order,
        "final_capital": last["capital"], "total_withdraw": total_wd,
        "bankrupt": last["bankrupt"], "max_drawdown": max_dd,
        "win_days": win_days, "loss_days": loss_days,
        "hit_rate": round(total_hits / total_cnt * 100, 1) if total_cnt else None
    }
    return {"ok": True, "daily": daily, "monthly": monthly_out, "weekly": weekly_out, "summary": summary}

@app.get("/api/track/ladder-scheme-a-detail")
async def api_ladder_scheme_a_detail(request: Request, date: str):
    """方案A 某天56组下单明细"""
    await require_auth(request)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, lambda: get_ladder_scheme_a_detail(date))


def get_ladder_scheme_a_detail(date):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trio, chip, nums_json, order_amt, neg_hit FROM ladder_scheme_a_detail WHERE date=? ORDER BY order_amt DESC",
        (date,)
    ).fetchall()
    conn.close()
    detail = [{"trio": r["trio"], "chip": r["chip"], "nums": json.loads(r["nums_json"] or "[]"),
               "order_amt": r["order_amt"], "neg_hit": r["neg_hit"]} for r in rows]
    total = sum(d["order_amt"] for d in detail)
    hit = sum(1 for d in detail if d["neg_hit"])
    return {"ok": True, "date": date, "detail": detail, "total_amt": total,
            "count": len(detail), "hit_count": hit}



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

@app.post("/api/simulate/run-one-day")
async def api_run_one_day(request: Request):
    """单日演算：跑算法投票 + 存入历史"""
    await require_auth(request)
    body = await request.json()
    bet_date = body.get("date", "")
    if not bet_date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, lambda: run_single_day(bet_date))
    except Exception as e:
        return {"ok": False, "date": bet_date, "error": str(e)}


@app.post("/api/simulate/batch-guide")
async def api_batch_guide(request: Request):
    """批量逐日演算：指定日期范围，逐一跑算法投票 + 存入历史"""
    await require_auth(request)
    body = await request.json()
    from_date = body.get("from_date", "")
    to_date = body.get("to_date", "")
    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, lambda: batch_generate_guides(from_date, to_date))
    except Exception as e:
        return {"ok": False, "error": f"批量演算异常: {e}", "total": 0, "ok_count": 0, "skip_count": 0, "error_count": 1}


@app.post("/api/simulate/generate-one-day")
async def api_generate_one_day(request: Request):
    """单日指南生成：只写 sim_guides，不动 order_history"""
    await require_auth(request)
    body = await request.json()
    bet_date = body.get("date", "")
    if not bet_date:
        raise HTTPException(400, "date required")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, lambda: run_single_guide(bet_date))
    except Exception as e:
        return {"ok": False, "date": bet_date, "error": str(e)}


@app.post("/api/simulate/generate-guides")
async def api_generate_guides(request: Request):
    """生成指南缓存：只写 sim_guides，不动 order_history"""
    await require_auth(request)
    body = await request.json()
    from_date = body.get("from_date", "")
    to_date = body.get("to_date", "")
    if not from_date or not to_date:
        raise HTTPException(400, "from_date and to_date required")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, lambda: generate_guides_only(from_date, to_date))
    except Exception as e:
        return {"ok": False, "error": f"生成异常: {e}", "total": 0, "ok_count": 0, "error_count": 1}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)



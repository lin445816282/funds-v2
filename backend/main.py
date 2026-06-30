"""funds-v2 独立服务 — 多门店资金看板 + 预测数据 API"""
import os, json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "funds-v2.db")

app = FastAPI(title="funds-v2", docs_url=None, redoc_url=None)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── API: 资金数据 GET ──────────────────────
@app.get("/api/funds/data")
async def get_funds_data():
    conn = get_db()
    try:
        stores = [r["name"] for r in conn.execute("SELECT name FROM stores ORDER BY id").fetchall()]
        cats = [dict(r) for r in conn.execute("SELECT * FROM categories").fetchall()]
        records = [dict(r) for r in conn.execute("SELECT * FROM records ORDER BY date, id").fetchall()]
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


# ── API: 资金数据 POST（全量保存）─────────
@app.post("/api/funds/data")
async def post_funds_data(request: Request):
    data = await request.json()
    conn = get_db()
    try:
        # Stores
        if "stores" in data:
            conn.execute("DELETE FROM stores")
            for i, name in enumerate(data["stores"]):
                conn.execute("INSERT INTO stores (id, name) VALUES (?,?)", (i+1, name))
        # Categories
        if "categories" in data:
            conn.execute("DELETE FROM categories")
            for c in data["categories"]:
                conn.execute(
                    "INSERT INTO categories (id, name, dir, color, budget, show) VALUES (?,?,?,?,?,?)",
                    (c["id"], c["name"], c.get("dir","-"), c.get("color",""),
                     c.get("budget",0), int(c.get("show",1)))
                )
        # Records
        if "records" in data:
            conn.execute("DELETE FROM records")
            for r in data["records"]:
                conn.execute(
                    "INSERT INTO records (id, store, date, category, amount, note) VALUES (?,?,?,?,?,?)",
                    (r["id"], r["store"], r["date"], r["category"],
                     r.get("amount",0), r.get("note",""))
                )
        # Alert rules
        if "alert_rules" in data:
            conn.execute("DELETE FROM alert_rules")
            for a in data["alert_rules"]:
                conn.execute(
                    "INSERT INTO alert_rules (id, cat, type, description, pct, on_state) VALUES (?,?,?,?,?,?)",
                    (a["id"], a.get("cat",""), a["type"], a.get("desc",""),
                     a.get("pct",0), int(a.get("on",1)))
                )
        # Store settings
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
        return {"ok": True}
    finally:
        conn.close()


# ── API: 板块预测 ──────────────────────────
@app.get("/api/sector-predictions")
async def sector_predictions(days: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sector_predictions ORDER BY predict_date DESC, id LIMIT ?",
        (days * 10,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


# ── API: 大盘预测 ──────────────────────────
@app.get("/api/index-predictions")
async def index_predictions(days: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM index_predictions ORDER BY predict_date DESC, id LIMIT ?",
        (days * 3,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


# ── Static + SPA ───────────────────────────
STATIC_DIR = os.path.join(BASE_DIR, "static")
if os.path.isdir(STATIC_DIR):
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "funds-v2.html"))

    @app.get("/{path:path}")
    async def serve_static(path: str):
        fp = os.path.join(STATIC_DIR, path)
        if os.path.isfile(fp):
            return FileResponse(fp)
        return FileResponse(os.path.join(STATIC_DIR, "funds-v2.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)

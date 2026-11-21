from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from foundation import admin_access, assets, collection, configuration, database as store, deployment, record_store
from research import engine as research_engine, model_probe, sessions as research_sessions
from research.api import instance_status, router as research_router

VERSION = "research-relational-20261120.2-preview.1"
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    admin_access.read_account()
    store.initialize()
    if deployment.workers_enabled():
        collection.engine.start()
        research_engine.engine.start()
    try:
        yield
    finally:
        research_engine.engine.stop()
        collection.engine.stop()


app = FastAPI(title="序研 · AI研究与选股", version=VERSION, lifespan=lifespan)
app.include_router(research_router)


@app.middleware("http")
async def local_access(request: Request, call_next):
    # Reject foreign Host headers so a DNS-rebinding page cannot read or drive the loopback service.
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if (request.url.hostname or "") not in allowed_hosts:
        return JSONResponse({"detail": "请从本机地址访问工作台"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if ((origin and origin != str(request.base_url).rstrip("/"))
                or request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse({"detail": "请从本机工作台页面操作"}, status_code=403)
    if request.url.path == "/login" or admin_access.valid(request.cookies.get(admin_access.COOKIE)):
        response = await call_next(request)
    elif request.url.path == "/" and request.method in ("GET", "HEAD"):
        response = RedirectResponse("/login", status_code=303)
    else:
        response = JSONResponse({"detail": "请先登录 ADMIN"}, status_code=401)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # A browser POST from a no-referrer document can send Origin: null.
    # Keep the origin for same-site forms, while omitting it on external links.
    response.headers["Referrer-Policy"] = "same-origin" if request.url.path in {"/", "/login"} else "no-referrer"
    if request.url.path.startswith("/api/") or request.url.path in {"/", "/login", "/logout"} or response.status_code == 401:
        response.headers["Cache-Control"] = "no-store"
    return response


def login_page(error=False):
    page = (Path(__file__).parent / "login.html").read_text(encoding="utf-8")
    if error:
        page = page.replace("<!-- status -->", '<p role="alert">账号或密码不正确。</p>')
    return HTMLResponse(page, status_code=401 if error else 200)


@app.get("/login", include_in_schema=False)
def show_login(request: Request):
    if admin_access.valid(request.cookies.get(admin_access.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return login_page()


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "请提交登录表单")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "登录表单过大")
        body.extend(chunk)
    try:
        form = parse_qs(body.decode("utf-8"), max_num_fields=2, strict_parsing=True)
    except ValueError:
        raise HTTPException(400, "登录表单无效") from None
    username, password = form.get("username", [""])[0], form.get("password", [""])[0]
    if not await run_in_threadpool(admin_access.authenticate, username, password):
        return login_page(error=True)
    admin_access.revoke(request.cookies.get(admin_access.COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(admin_access.COOKIE, admin_access.issue(), max_age=admin_access.MAX_AGE,
                        httponly=True, secure=request.url.scheme == "https", samesite="strict")
    return response


@app.post("/logout", include_in_schema=False)
def logout(request: Request):
    admin_access.revoke(request.cookies.get(admin_access.COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(admin_access.COOKIE, httponly=True, samesite="strict")
    return response


@app.get("/healthz")
def health():
    with store.db(readonly=True) as c:
        c.execute("SELECT 1")
    alive = lambda thread: bool(thread and thread.is_alive())
    workers = {"enabled": bool(deployment.workers_enabled()),
               "collection": alive(collection.engine.worker), "collection_clock": alive(collection.engine.clock),
               "research": alive(research_engine.engine.thread), "research_clock": alive(research_engine.engine.clock)}
    return {"status": "ok", "service": "stock-foundation", "version": VERSION, "pid": os.getpid(), "root": str(store.ROOT), "database": store.description(), "workers": workers, "instance": instance_status()}


@app.get("/api/instance")
def current_instance():
    return instance_status()


@app.get("/api/overview")
def overview():
    # Single read-only connection for all overview data to avoid 5+ round-trips.
    with store.db(readonly=True) as c:
        stat_rows = {r['dataset']: assets.stat_value(r) for r in c.execute('SELECT * FROM asset_stats')}
        stats = [stat_rows.get(k, {"dataset": k, "name": v, "count": 0, "versions": 0, "symbols": 0, "first": None, "last": None})
                 for k, v in assets.DATASETS.items()]
        total = sum(s["count"] for s in stats)
        basics_row = c.execute("SELECT max(day) day, count(DISTINCT symbol) securities FROM heads WHERE dataset='basics'").fetchone()
        latest = basics_row['day'] if basics_row else None
        securities = basics_row['securities'] if basics_row else 0
        daily = [dict(r) for r in c.execute("SELECT day, count(*) n FROM heads WHERE dataset='daily' GROUP BY day ORDER BY day DESC LIMIT 20")][::-1]
        source_rows = [json.loads(r[0]) for r in c.execute('SELECT data FROM sources ORDER BY id')]
        if not any(row['id'] == 'bs_actions' for row in source_rows):
            source_rows.append(assets.ACTIONS_SOURCE.copy())
        paused = store.setting("automation_paused", True, conn=c)
        model_count = c.execute("SELECT count(*) FROM models").fetchone()[0]
        plan_count = c.execute("SELECT count(*) FROM plans").fetchone()[0]
    return {"today": store.today(), "time": store.now(), "version": VERSION, "total_records": total,
            "test_mode": os.environ.get("STOCK_UI_ACCEPTANCE") == "1", "instance": instance_status(),
            "securities": securities, "directory_date": latest, "stats": stats, "daily_coverage": daily,
            "sources": source_rows, "paused": paused,
            "schedules": collection.schedules(), "jobs": collection.list_jobs(8),
            "models": model_count, "plans": plan_count}


@app.get("/api/assets")
def asset_stats():
    return {"items": assets.stats()}


@app.get("/api/records")
def records(dataset: str = "daily", start: str | None = None, end: str | None = None,
            symbol: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10000), cursor: str | None = Query(None, max_length=4096)):
    return assets.rows(dataset, start, end, symbol, limit, offset, cursor)


@app.get("/api/records/{rid}")
def record(rid: int):
    with store.db(readonly=True) as c:
        row = c.execute('SELECT * FROM facts WHERE id=%s', (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        v = dict(row)
        v['payload'] = record_store.load(c,rid)
        v['fingerprint'] = bytes(v['fingerprint']).hex()
        versions = c.execute('SELECT id,source,source_version,first_seen,last_seen FROM facts WHERE dataset=%s AND day=%s AND symbol=%s AND record_key=%s ORDER BY id DESC LIMIT 100', (v["dataset"], v["day"], v["symbol"], v["record_key"])).fetchall()
        origins = c.execute('SELECT source,source_version,first_seen,last_seen FROM fact_origins WHERE fact_id=%s ORDER BY source LIMIT 32', (rid,)).fetchall()
    return {"record": v, "versions": [dict(r) for r in versions], "origins": [dict(r) for r in origins]}


@app.get("/api/export")
def export(dataset: str, start: str, end: str, symbol: str | None = None):
    rows = assets.rows(dataset, start, end, symbol, 10001)["items"]
    if len(rows) > 10000:
        raise HTTPException(422, "单次导出最多一万条，请缩小日期范围或指定股票")
    fields = list(dict.fromkeys(k for r in rows for k in r if k not in ("raw", "fields", "text")))
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        safe = {k: store.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items() if k in fields}
        for k, v in safe.items():
            if isinstance(v, str) and v.startswith(("=", "+", "-", "@")):
                safe[k] = "'" + v
        writer.writerow(safe)
    return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{dataset}-{start}-{end}.csv"'})


@app.get("/api/sources")
def source_list():
    return {"items": assets.sources()}


@app.put("/api/sources/{sid}")
def source_update(sid: str, raw: dict):
    allowed = {"enabled", "automatic", "min_interval", "timeout"}
    if set(raw) - allowed:
        raise HTTPException(422, "来源配置包含不支持的字段")
    for key in ("enabled", "automatic"):
        if key in raw and type(raw[key]) is not bool:
            raise HTTPException(422, "开关须为布尔值")
    if "min_interval" in raw and (type(raw["min_interval"]) not in (int, float) or not 2 <= raw["min_interval"] <= 600):
        raise HTTPException(422, "来源请求间隔须为 2—600 秒")
    if "timeout" in raw and (type(raw["timeout"]) is not int or not 10 <= raw["timeout"] <= 600):
        raise HTTPException(422, "超时须为 10—600 秒")
    v = assets.patch_source(sid, raw)
    store.audit("修改数据源", {"id": sid, **raw})
    return v


@app.get("/api/collection")
def collection_status():
    instance = instance_status()
    paused = store.setting("automation_paused", True)
    active = bool(instance['collection_clock'] and not paused)
    return {"schedules": [{**plan, "effective_enabled": bool(plan['enabled'] and active)}
                          for plan in collection.schedules()],
            "jobs": collection.list_jobs(), "paused": paused, "instance": instance,
            "automatic_collection_enabled": active,
            "automatic_symbols": collection.automatic_symbols(),
            "watchlist": store.setting("watchlist", []), "sources": assets.sources()}


@app.post("/api/collection/jobs")
def collect(raw: dict):
    j = collection.submit(raw)
    return {k: v for k, v in j.items() if k != "items"}


@app.post("/api/collection/jobs/{jid}/{action}")
def control_job(jid: str, action: str):
    return collection.control(jid, action)


@app.get("/api/collection/jobs/{jid}")
def job_detail(jid: str):
    j = collection.get_job(jid)
    with store.db(readonly=True) as c:
        events = [{"created": r[0], **json.loads(r[1])} for r in c.execute('SELECT created,data FROM collection_events WHERE job_id=%s ORDER BY id DESC LIMIT 50', (jid,))]
    return {"job": {k: v for k, v in j.items() if k != "items"}, "events": events}


@app.put("/api/collection/schedules/{sid}")
def update_schedule(sid: str, raw: dict):
    return collection.save_schedule(sid, raw)


@app.post("/api/collection/schedules/{sid}/run")
def run_schedule(sid: str):
    j = collection.run_schedule(sid)
    return {k: v for k, v in j.items() if k != "items"}


@app.post("/api/collection/automation")
def automation(raw: dict):
    if type(raw.get("paused")) is not bool:
        raise HTTPException(422, "暂停状态须为布尔值")
    store.set_setting("automation_paused", raw["paused"])
    store.audit("暂停每日采集" if raw["paused"] else "启用每日采集", {})
    return {"paused": raw["paused"]}


@app.put("/api/collection/watchlist")
def watchlist(raw: dict):
    symbols = raw.get("symbols")
    if not isinstance(symbols, list) or len(symbols) > 500:
        raise HTTPException(422, "关注股票最多 500 只")
    codes = list(dict.fromkeys(assets.symbol(s) for s in symbols))
    store.set_setting("watchlist", codes)
    store.audit("保存采集关注股票", {"symbols": codes})
    return {"symbols": codes}


@app.get("/api/ai")
def ai_configs():
    return {k: configuration.list_items(k) for k in configuration.SCHEMAS} | {"role_bindings": store.setting("saved_ai_role_agents", {}), "execution_enabled": True,
        "automatic_research_enabled":bool(research_sessions.config()['enabled'] and instance_status()['research_clock']),
        "saved_automatic_research_enabled":bool(research_sessions.config()['enabled']), "instance":instance_status(),"execution_scope":"按研究时段与已保存方案执行"}


def require_local_ai_editor(request: Request):
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("origin") != str(request.base_url).rstrip("/")
            or request.headers.get("sec-fetch-site") not in (None, "same-origin")):
        raise HTTPException(403, "请从本机 AI 设置页面操作")


@app.post("/api/ai/models/{model_id}/key")
def show_model_key(model_id: int, request: Request):
    require_local_ai_editor(request)
    return {"api_key": configuration.visible_model_key(model_id)}


@app.post("/api/ai/models/catalog")
def ai_model_catalog(raw: dict, request: Request):
    require_local_ai_editor(request)
    return model_probe.catalog(raw)


@app.post("/api/ai/models/test")
def ai_model_test(raw: dict, request: Request):
    require_local_ai_editor(request)
    return model_probe.test_connection(raw)


@app.post("/api/ai/{kind}")
def create_config(kind: str, raw: dict):
    return configuration.save(kind, raw)


@app.put("/api/ai/{kind}/{item_id}")
def update_config(kind: str, item_id: int, raw: dict):
    return configuration.save(kind, raw, item_id)


@app.get("/api/storage")
def storage_status():
    from foundation import retention
    return retention.status()


@app.get("/api/audit")
def audit():
    with store.db(readonly=True) as c:
        return {"items": [{"created": r[0], "action": r[1], "detail": json.loads(r[2])} for r in c.execute('SELECT created,action,data FROM audit ORDER BY id DESC LIMIT 100')]}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")

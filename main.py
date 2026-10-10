"""OCI Panel v2 —— 自托管多云管理面板（甲骨文为主，WebSSH / 抢机 / 多云 / MCP 一体）。

启动: python main.py，默认端口 9528。
"""
import asyncio
import contextlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
from contextlib import asynccontextmanager

import fastapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import deps
import mcp_service
import store
import sshpool
import tasks
import webapi
import gcp_api
import aws_api
from deps import COOKIE, make_token, verify_pw

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WEEK = deps.WEEK


@asynccontextmanager
async def lifespan(_app):
    store.init_db()
    deps.init_auth()
    tasks.start()
    yield
    tasks.stop()
    await asyncio.to_thread(sshpool.shutdown)


app = fastapi.FastAPI(title="OCI Panel", lifespan=lifespan, docs_url=None, redoc_url=None)

logging.basicConfig(level=logging.INFO)
try:
    os.makedirs(store.DATA_DIR, exist_ok=True)
    _fh = RotatingFileHandler(os.path.join(store.DATA_DIR, "panel.log"),
                              maxBytes=10_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(_fh)
    logging.getLogger("uvicorn.error").addHandler(_fh)
    logging.getLogger("uvicorn.access").addHandler(_fh)
except Exception:
    pass


class LoginBody(BaseModel):
    username: str
    password: str


_login_attempts = {}
_login_lock = threading.Lock()


@app.post("/api/login")
def api_login(body: LoginBody, response: fastapi.Response, request: fastapi.Request):
    if not deps.check_origin(request):
        raise fastapi.HTTPException(403, "请求来源不匹配")
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(address, []) if now - t < 300]
        if len(attempts) >= 10:
            raise fastapi.HTTPException(429, "登录尝试过多，请 5 分钟后重试")
        _login_attempts[address] = attempts + [now]
    valid_user = hmac.compare_digest(body.username.encode("utf-8"),
                                     (store.get_setting("admin_user") or "").encode("utf-8"))
    valid_password = verify_pw(body.password, store.get_setting("admin_pass") or "")
    if not (valid_user and valid_password):
        raise fastapi.HTTPException(401, "用户名或密码错误")
    with _login_lock:
        _login_attempts.pop(address, None)
    response.set_cookie(COOKIE, make_token(), max_age=WEEK, httponly=True, samesite="strict",
                        path=request.scope["panel_prefix"] + "/",
                        secure=os.environ.get("COOKIE_SECURE", "0") == "1")
    return {"ok": True}


@app.post("/api/logout")
def api_logout(response: fastapi.Response, request: fastapi.Request):
    if not deps.check_origin(request):
        raise fastapi.HTTPException(403, "请求来源不匹配")
    response.delete_cookie(COOKIE, path=request.scope["panel_prefix"] + "/")
    return {"ok": True}


@app.get("/api/me")
def api_me(_: None = fastapi.Depends(deps.require_auth)):
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True}


app.include_router(webapi.api)
app.include_router(gcp_api.api)
app.include_router(aws_api.api)


# ---------- Web SSH WebSocket ----------

@app.websocket("/ws/ssh")
async def ws_ssh(websocket: fastapi.WebSocket):
    if not deps.check_origin(websocket):
        await websocket.close(code=4403)
        return
    if not deps.check_token(websocket.cookies.get(COOKIE, "")):
        await websocket.close(code=4401)
        return
    sid = websocket.query_params.get("sid", "")
    rows = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))
    if not rows:
        await websocket.close(code=4404)
        return
    sess = rows[0]
    await websocket.accept()
    revision = websocket.query_params.get("expected_revision", "")
    if revision and not hmac.compare_digest(revision, webapi._ssh_revision(sess)):
        await websocket.send_text("\r\n[连接配置已变更或服务已重启，请刷新页面重新选择目标]\r\n")
        await websocket.close(code=4409)
        return

    import asyncssh

    async def _pump_out(ws, reader):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    with contextlib.suppress(Exception):
                        await ws.close()
                    break
                await ws.send_text(data)
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()

    conn = None
    try:
        conn = await sshpool._connect(sess)
    except Exception as e:
        await websocket.send_text(f"\r\n[连接失败] {e}\r\n")
        await websocket.close()
        return

    try:
        cols = int(websocket.query_params.get("cols", "120"))
        rows_n = int(websocket.query_params.get("rows", "32"))
    except ValueError:
        cols, rows_n = 120, 32

    try:
        process = await conn.create_process(
            term_type="xterm-256color", term_size=(cols, rows_n))
        writer, reader = process.stdin, process.stdout
    except Exception as e:
        await websocket.send_text(f"\r\n[会话打开失败] {e}\r\n")
        await websocket.close()
        conn.close()
        return

    pump = asyncio.create_task(_pump_out(websocket, reader))
    async def watch_auth():
        while deps.check_token(websocket.cookies.get(COOKIE, "")):
            await asyncio.sleep(0.5)
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
    auth_watch = asyncio.create_task(watch_auth())
    try:
        while True:
            msg = await websocket.receive_text()
            if not deps.check_token(websocket.cookies.get(COOKIE, "")):
                await websocket.close(code=4401)
                break
            try:
                payload = json.loads(msg)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("resize"):
                size = payload["resize"]
                with contextlib.suppress(Exception):
                    process.change_terminal_size(int(size.get("cols", 120)),
                                                 int(size.get("rows", 32)))
            else:
                writer.write(msg)
                await writer.drain()
    except fastapi.WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        auth_watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await auth_watch
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        process.close()
        conn.close()
        await conn.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()


# ---------- MCP 端点（无付费墙） ----------

@app.post("/mcp")
async def mcp_endpoint(request: fastapi.Request):
    auth = request.headers.get("Authorization", "")
    token = store.get_setting("mcp_token", "")
    if not token or auth != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}}, status_code=400)
    if isinstance(body, list):
        results = []
        for item in body:
            _, payload = await asyncio.to_thread(mcp_service.handle_rpc, item)
            if payload is not None:
                results.append(payload)
        return JSONResponse(results) if results else fastapi.Response(status_code=202)
    status, payload = await asyncio.to_thread(mcp_service.handle_rpc, body)
    if payload is None:
        return fastapi.Response(status_code=status)
    return JSONResponse(payload, status_code=status)


@app.get("/mcp")
async def mcp_get():
    return JSONResponse({"mcp": "POST JSON-RPC here with Authorization: Bearer <mcp_token>"})


# ---------- 静态页面 ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AccessPathGate:
    """Require the private URL prefix for HTTP, API, MCP, static, and WebSocket."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.inner(scope, receive, send)
        path = scope.get("path", "")
        if path == "/healthz" and scope["type"] == "http":
            return await self.inner(scope, receive, send)
        prefix = "/" + (store.get_setting("access_path") or "")
        if path == prefix and scope["type"] == "http":
            response = RedirectResponse(prefix + "/", status_code=308)
            return await response(scope, receive, send)
        if not path.startswith(prefix + "/"):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4404})
            response = fastapi.Response(status_code=404)
            return await response(scope, receive, send)
        forwarded = dict(scope)
        forwarded["path"] = path[len(prefix):]
        forwarded["raw_path"] = forwarded["path"].encode("utf-8")
        forwarded["root_path"] = ""
        forwarded["panel_prefix"] = prefix

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([(b"x-content-type-options", b"nosniff"),
                                (b"referrer-policy", b"no-referrer"),
                                (b"x-frame-options", b"DENY"),
                                (b"cache-control", b"no-store")])
                message = {**message, "headers": headers}
            await send(message)
        return await self.inner(forwarded, receive, secure_send)


app = AccessPathGate(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "9528")))

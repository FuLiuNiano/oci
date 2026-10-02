"""OCI Panel v2 —— 自托管多云管理面板（甲骨文为主，WebSSH / 抢机 / 多云 / MCP 一体）。

启动: python main.py，默认端口 9527。
"""
import asyncio
import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import threading
import time
from contextlib import asynccontextmanager

import fastapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import deps
import mcp_service
import store
import sshpool
import tasks
import webapi
from deps import COOKIE, make_token, hash_pw

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
    password: str


_login_attempts = {}
_login_lock = threading.Lock()


@app.post("/api/login")
def api_login(body: LoginBody, response: fastapi.Response, request: fastapi.Request):
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(address, []) if now - t < 300]
        if len(attempts) >= 10:
            raise fastapi.HTTPException(429, "登录尝试过多，请 5 分钟后重试")
        _login_attempts[address] = attempts + [now]
    if hash_pw(body.password) != (store.get_setting("admin_pass") or ""):
        raise fastapi.HTTPException(401, "密码错误")
    with _login_lock:
        _login_attempts.pop(address, None)
    response.set_cookie(COOKIE, make_token(), max_age=WEEK, httponly=True, samesite="strict",
                        secure=os.environ.get("COOKIE_SECURE", "0") == "1")
    return {"ok": True}


@app.post("/api/logout")
def api_logout(response: fastapi.Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def api_me(_: None = fastapi.Depends(deps.require_auth)):
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True}


app.include_router(webapi.api)


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
    try:
        while True:
            msg = await websocket.receive_text()
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "9527")))

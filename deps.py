"""认证与公共依赖。"""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

import store

COOKIE = "panel_session"
WEEK = 7 * 86400


def hash_pw(pw: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), b"oci-panel", 200_000).hex()


def init_auth():
    if not store.get_setting("admin_pass"):
        pw = secrets.token_urlsafe(9)
        store.set_setting("admin_pass", hash_pw(pw))
        tip_path = store.DATA_DIR + "/initial_admin_password.txt"
        with open(tip_path, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        import os
        if os.name != "nt":
            os.chmod(tip_path, 0o600)
        print("=" * 56)
        print("首次启动已生成管理员密码:", pw)
        print(f"（同时写入 {tip_path}，登录后请在「设置」里修改）")
        print("=" * 56)
    if not store.get_setting("secret"):
        store.set_setting("secret", secrets.token_hex(32))
    if not store.get_setting("mcp_token"):
        store.set_setting("mcp_token", "mcp_" + secrets.token_hex(16))


def make_token() -> str:
    exp = str(int(time.time()) + WEEK)
    sig = hmac.new(store.get_setting("secret").encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        if int(exp) < time.time():
            return False
        good = hmac.new(store.get_setting("secret").encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)
    except Exception:
        return False


def require_auth(request: Request):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not check_origin(request):
        raise HTTPException(403, "请求来源不匹配")
    if not check_token(request.cookies.get(COOKIE, "")):
        raise HTTPException(401, "未登录")


def check_origin(request):
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == request.headers.get("host")

"""认证与公共依赖。"""
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

import store

COOKIE = "panel_session"
WEEK = 7 * 86400


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_pw(pw: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 200_000 <= int(rounds) <= 1_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError, AttributeError):
        return False


def init_auth():
    if not all(store.get_setting(k) for k in ("admin_user", "admin_pass", "access_path")):
        username = secrets.token_urlsafe(24)
        pw = secrets.token_urlsafe(24)
        access_path = secrets.token_urlsafe(24)
        tip_path = os.path.join(store.DATA_DIR, "initial_admin_credentials.txt")
        pending = tip_path + ".pending"
        with open(pending, "w", encoding="utf-8") as f:
            f.write(f"访问路径: /{access_path}/\n用户名: {username}\n密码: {pw}\n")
        if os.name != "nt":
            os.chmod(pending, 0o600)
        os.replace(pending, tip_path)
        store.set_setting("admin_user", username)
        store.set_setting("admin_pass", hash_pw(pw))
        store.set_setting("secret", secrets.token_hex(32))
        store.set_setting("access_path", access_path)
        legacy = os.path.join(store.DATA_DIR, "initial_admin_password.txt")
        if os.path.isfile(legacy):
            os.remove(legacy)
        print(f"管理员登录信息已生成，请在服务器本地查看 {tip_path}")
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

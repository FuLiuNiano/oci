"""Local startup and persistence checks. --full adds real SDK/SSH regression tests."""
import argparse
import compileall
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
ROOT = Path(__file__).resolve().parent


def static_check():
    assert compileall.compile_dir(str(ROOT), quiet=1, maxlevels=0), "Python syntax error"
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    for name in ("app.js", "cloud.js", "webssh.js"):
        script = ROOT / "static" / name
        source = script.read_text(encoding="utf-8")
        for identifier in re.findall(r'\$\("#([\w-]+)"', source):
            assert identifier in ids, f"{name}: missing #{identifier}"
        if shutil.which("node"):
            subprocess.run(["node", "--check", str(script)], check=True)
    assert 'DOMContentLoaded' in (ROOT / "static/app.js").read_text(encoding="utf-8")
    print("PASS: syntax, page elements and startup entry")


def http_smoke():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix=".selfcheck-", dir=ROOT) as directory:
        env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), PANEL_DATA_DIR=directory,
                   PYTHONIOENCODING="utf-8", COOKIE_SECURE="0")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPCookieProcessor(jar))
        def request(path, body=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type":"application/json"} if body is not None else {})
            try:
                with opener.open(req, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        for run in range(2):
            with open(Path(directory) / "server.log", "a", encoding="utf-8") as log:
                proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")],
                                        cwd=ROOT, env=env, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        if proc.poll() is not None:
                            raise RuntimeError("Panel exited during startup")
                        try:
                            if request("/healthz")[0] == 200:
                                break
                        except (OSError, urllib.error.URLError):
                            pass
                        time.sleep(0.2)
                    else:
                        raise RuntimeError("Panel startup timed out")
                    if run == 0:
                        password = (Path(directory) / "initial_admin_password.txt").read_text().strip()
                        assert request("/api/me")[0] == 401
                        assert request("/api/login", {"password":"wrong"})[0] == 401
                        assert request("/api/login", {"password":password})[0] == 200
                        for path in ("/api/me", "/", "/static/app.js", "/static/cloud.js", "/static/webssh.js",
                                     "/api/accounts", "/api/overview", "/api/launch-tasks", "/api/ssh/sessions"):
                            assert request(path)[0] == 200, path
                        assert request("/api/settings/password", {"old_password":password,
                                    "new_password":"selfcheck-new-password"})[0] == 200
                        assert request("/api/me")[0] == 401
                    else:
                        assert request("/api/login", {"password":password})[0] == 401
                        assert request("/api/login", {"password":"selfcheck-new-password"})[0] == 200
                        assert request("/api/me")[0] == 200
                        assert request("/api/logout", {})[0] == 200
                        assert request("/api/me")[0] == 401
                finally:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
    print("PASS: real HTTP startup, login, password change and restart persistence")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run SDK/SSH tests (install requirements-dev.txt)")
    args = parser.parse_args()
    static_check()
    http_smoke()
    if args.full:
        with tempfile.TemporaryDirectory(prefix=".selfcheck-tests-", dir=ROOT) as directory:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider",
                                     "--basetemp", str(Path(directory) / "cases")], cwd=ROOT)
            if result.returncode:
                sys.exit(result.returncode)
    print("Local checks passed. Real OCI permissions and capacity require your account configuration.")


if __name__ == "__main__":
    main()

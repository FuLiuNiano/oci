"""Optional real-Chrome UI smoke test; OCI replies are explicitly simulated."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix=".browser-check-", dir=ROOT) as directory:
        env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), PANEL_DATA_DIR=directory,
                   COOKIE_SECURE="0", PYTHONIOENCODING="utf-8")
        with open(Path(directory) / "server.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT,
                                    env=env, stdout=log, stderr=log)
            try:
                base = f"http://127.0.0.1:{port}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        with opener.open(base + "/healthz", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.2)
                password = (Path(directory) / "initial_admin_password.txt").read_text().strip()
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="chrome", headless=True)
                    page = browser.new_page(viewport={"width":1440, "height":1000})
                    errors = []
                    page.on("pageerror", lambda error:errors.append(str(error)))
                    def replies(route):
                        path = route.request.url.split(base)[-1].split("?")[0]
                        if path == "/api/panel/metrics":
                            data = {"available":True,"scope":"host","cpu":11,"cores":1,
                                    "memory_used":500*1024**2,"memory_total":1024**3,"memory_percent":49,
                                    "network_rx":7600,"network_tx":4800,"app_memory":54*1024**2,
                                    "disk_used":5*1024**3,"disk_total":20*1024**3}
                        elif path.endswith("/usage"):
                            data = {"currencies":{"USD":0.0},"daily":[],"total":0.0}
                        elif path.endswith("/traffic"):
                            data = {"total_gb":1.25,"per_resource":{"instance1":1.25}}
                        elif path.endswith("/regions"):
                            data = {"data":[{"region":"ap-singapore-1","status":"READY","home":True}]}
                        elif path.endswith("/stats"):
                            data = {"limits":[{"name":"standard-a1-core-count","used":1,"limit":4}]}
                        elif path.endswith("/check"):
                            data = {"ok":True,"account_name":"demo-account","region":"ap-singapore-1",
                                    "checks":[{"name":"实例读取","ok":True,"message":"发现 1 台实例"}]}
                        elif path == "/api/overview":
                            data = {"accounts":1,"instances":1,"running_tasks":0,"ssh_sessions":0,"domains":0,
                                    "states":{"RUNNING":1},"errors":[]}
                        elif path == "/api/oc-info":
                            data = {"ads":["AD1"], "subnets":[{"id":"subnet1","name":"public","public":True}]}
                        elif path == "/api/cloud/oci/instances":
                            data = {"data":[{"id":"instance1","name":"test-instance","state":"RUNNING",
                                             "shape":"VM.Standard.A1.Flex","spec":"1C/6G","public_ip":"203.0.113.2",
                                             "private_ip":"10.0.0.2","ad":"AD1"}]}
                        elif path == "/api/cloud/oci/action":
                            assert route.request.post_data_json["action"] == "SOFTRESET"
                            data = {"ok":True}
                        elif path.startswith("/api/oci/"):
                            data = {"data":[]}
                        else:
                            route.continue_()
                            return
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
                    page.route("**/api/**", replies)
                    page.goto(base)
                    page.locator("#view-login").wait_for(state="visible")
                    page.fill("#login-pass",password)
                    page.click("#btn-login")
                    page.locator("#app").wait_for(state="visible")
                    assert page.locator(".view:visible").count() == 1
                    page.click('nav button[data-view="accounts"]')
                    page.click("#btn-new-account")
                    assert page.locator("#a-platform option").count() == 1
                    page.fill("#a-name","browser-account")
                    page.fill("#a-region","ap-singapore-1")
                    page.fill("#ao-user","ocid1.user.oc1.."+"a"*60)
                    page.fill("#ao-tenancy","ocid1.tenancy.oc1.."+"b"*60)
                    page.fill("#ao-fp", ":".join(["00"]*16))
                    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
                    page.fill("#ao-pk",key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())
                    page.click("#btn-save-account")
                    page.locator('#acct-table button[data-aact="edit"]').wait_for()
                    page.click('#acct-table button[data-aact="edit"]')
                    assert page.input_value("#ao-user").startswith("ocid1.user")
                    assert page.input_value("#ao-fp") == ":".join(["00"]*16)
                    assert page.input_value("#ao-pk") == ""
                    page.fill("#a-name","edited-browser-account")
                    page.click("#btn-save-account")
                    page.locator("#account-form").wait_for(state="hidden")
                    page.click('nav button[data-view="instances"]')
                    page.locator('#inst-table button[data-act="REBOOT"]').wait_for()
                    page.once("dialog", lambda dialog:dialog.accept())
                    page.click('#inst-table button[data-act="REBOOT"]')
                    for name in ("launch","volumes","network","users","objects","domains","ssh","mail","settings"):
                        page.click(f'nav button[data-view="{name}"]')
                        assert page.locator(f"#view-{name}").is_visible()
                    preview = Path(os.environ.get("OCI_UI_PREVIEW_DIR", tempfile.gettempdir()))
                    preview.mkdir(parents=True, exist_ok=True)
                    page.click('nav button[data-view="overview"]')
                    for kind in ("usage", "traffic", "regions", "stats"):
                        page.click(f'[data-cloud-metric="{kind}"]')
                        page.wait_for_function("k => !document.querySelector('[data-cloud-metric=\"'+k+'\"]').disabled", arg=kind)
                    page.locator('#host-cpu').filter(has_text="11%").wait_for()
                    page.wait_for_function("!document.querySelector('#toast').classList.contains('show')")
                    page.screenshot(path=str(preview / "overview.png"), full_page=True)
                    assert "USD" in page.locator('#summary-usage').inner_text()
                    for name, button in (("diagnostics","#btn-account-check"),("cloudmonitor","#btn-cloud-monitor")):
                        page.click(f'nav button[data-view="{name}"]')
                        page.click(button)
                        page.wait_for_function("s => !document.querySelector(s).disabled", arg=button)
                    assert "1.250 GB" in page.locator('#cloud-monitor-results').inner_text()
                    for index, name in enumerate(("Production", "Development", "Backup")):
                        response = page.request.post(base + "/api/ssh/sessions", data={
                            "name":name,"host":f"203.0.113.{10+index}","username":"ubuntu",
                            "auth_type":"password","secret":"synthetic-browser-only","tags":"Ubuntu,OCI"})
                        assert response.ok
                    page.click('nav button[data-view="ssh"]')
                    page.wait_for_function("document.querySelectorAll('.session-card').length === 3")
                    page.screenshot(path=str(preview / "sessions.png"), full_page=True)
                    page.fill('#session-search', 'Development')
                    assert page.locator('.session-card').count() == 1
                    page.click('[data-scopy]')
                    page.wait_for_function("document.querySelectorAll('.session-card').length === 2")
                    page.fill('#session-search','')
                    assert page.locator('.session-card').count() == 4
                    assert 'synthetic-browser-only' not in page.content()
                    # Exercise actual xterm/close UI; only the remote SSH transport is simulated.
                    page.route_web_socket("**/ws/ssh?*", lambda ws:ws.send("Demo terminal\r\n$ "))
                    page.locator('[data-sopen]').first.click()
                    page.locator('#term-area').wait_for(state="visible")
                    page.locator('.term-close').click()
                    page.locator('#term-area').wait_for(state="hidden")
                    page.locator('[data-sopen]').first.click()
                    page.locator('#term-area').wait_for(state="visible")
                    page.locator('.term-close').click()
                    page.click('#btn-theme')
                    assert page.locator('html').get_attribute('data-theme') == 'dark'
                    page.set_viewport_size({"width":390,"height":844})
                    page.evaluate("scrollTo(0, 0)")
                    page.wait_for_function("!document.querySelector('#toast').classList.contains('show')")
                    page.wait_for_timeout(250)  # Allow CSS theme transitions to finish.
                    page.screenshot(path=str(preview / "mobile-dark.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.click('nav button[data-view="settings"]')
                    page.fill("#p-old",password)
                    page.fill("#p-new","browser-new-password")
                    page.click("#btn-save-pass")
                    page.locator("#view-login").wait_for(state="visible")
                    assert not errors, errors
                    browser.close()
                print("PASS: Chrome login, account edit, cloud cards, diagnostics, session search/copy, terminal close/reopen, mobile layout, theme and password change; cloud replies/SSH transport simulated")
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


if __name__ == "__main__":
    main()

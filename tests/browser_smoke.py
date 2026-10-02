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
                        if path == "/api/overview":
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
                    page.fill("#p-old",password)
                    page.fill("#p-new","browser-new-password")
                    page.click("#btn-save-pass")
                    page.locator("#view-login").wait_for(state="visible")
                    assert not errors, errors
                    browser.close()
                print("PASS: Chrome login, page switching, account add/edit, OCI reboot and password change")
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

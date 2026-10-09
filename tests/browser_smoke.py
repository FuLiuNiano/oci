"""Optional real-Chrome UI smoke test; OCI replies are explicitly simulated."""
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright
from browser_oci_isolation import check_oci_account_switching

ROOT = Path(__file__).resolve().parents[1]


def assert_terminal_content_fits(page):
    page.wait_for_function("""() => [...document.querySelectorAll('.term-holder:not(.hide)')].every(holder => {
        const mount = holder.querySelector('.term-mount').getBoundingClientRect();
        const screen = holder.querySelector('.xterm-screen').getBoundingClientRect();
        const frame = holder.getBoundingClientRect();
        return screen.height > 0 && screen.bottom <= mount.bottom + 1 && screen.right <= mount.right + 1 && mount.bottom <= frame.bottom + 1;
    })""")


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix=".browser-check-", dir=ROOT) as directory:
        env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), PANEL_DATA_DIR=directory,
                   COOKIE_SECURE="0", PYTHONIOENCODING="utf-8")
        with open(Path(directory) / "server.log", "w", encoding="utf-8") as log:
            launcher = """import os, sys, threading, uvicorn, main, tasks
tasks.start = lambda: None
server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=int(os.environ['PORT'])))
def stop():
    sys.stdin.read(1)
    server.should_exit = True
threading.Thread(target=stop, daemon=True).start()
server.run()
"""
            proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=ROOT,
                                    env=env, stdout=log, stderr=log, stdin=subprocess.PIPE)
            try:
                base = f"http://127.0.0.1:{port}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        with opener.open(base + "/healthz", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.2)
                details = (Path(directory) / "initial_admin_credentials.txt").read_text(encoding="utf-8").splitlines()
                access_path = details[0].split(": ", 1)[1]
                username = details[1].split(": ", 1)[1]
                password = details[2].split(": ", 1)[1]
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel="chrome", headless=True)
                    page = browser.new_page(viewport={"width":1440, "height":1000})
                    terminal_input = []
                    terminal_routes = {}
                    inputs_by_session = {}
                    batch_downloads = []
                    def ssh_transport(ws):
                        route_sid = parse_qs(urlparse(ws.url).query)['sid'][0]
                        terminal_routes[route_sid] = ws
                        def response(message):
                            if str(message).startswith('{"resize"'):
                                ws.send("Demo terminal\r\n中文 https://example.com/test\r\n$ ")
                            else:
                                terminal_input.append(message)
                                inputs_by_session.setdefault(route_sid, []).append(message)
                        ws.on_message(response)
                    page.route_web_socket(re.compile(r"/ws/ssh\?"), ssh_transport)
                    monitor_requests = []
                    errors = []
                    page.on("pageerror", lambda error:errors.append(str(error)))
                    def replies(route):
                        path = route.request.url.split(base)[-1].split("?")[0]
                        if path.startswith(access_path):
                            path = "/" + path[len(access_path):]
                        if path == "/api/ssh/monitor":
                            monitor_requests.append(route.request.post_data_json["session_id"])
                            data = {"cpu":12.5,"mem":33,"disk":42,"net_rx_mb":100 + len(monitor_requests)*2,"net_tx_mb":200 + len(monitor_requests)}
                        elif path == "/api/ssh/sftp/download-batch":
                            batch_downloads.append(route.request.post_data_json)
                            route.fulfill(content_type='application/zip', body=b'PK-test-browser-download')
                            return
                        elif path == "/api/ssh/sftp/upload":
                            data = {"ok":True,"size":len(route.request.post_data_buffer or b"")}
                        elif path == "/api/ssh/sftp/list":
                            directory = parse_qs(urlparse(route.request.url).query)["path"][0]
                            if directory == "/blocked":
                                route.fulfill(status=502, body="")
                                return
                            assert directory in ("/", "/子目录 space"), directory
                            data = {"data":[{"name":"demo.txt","dir":False,"size":256,"mtime":0}]}
                            if directory == "/":
                                data["data"] = [dict(name=name, dir=True, size=0, mtime=0)
                                                for name in ("blocked", "子目录 space")] + data["data"]
                                data["data"] += [dict(name=f"file-{i}.txt", dir=False, size=1, mtime=0) for i in range(40)]
                        elif path == "/api/panel/metrics":
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
                                             "private_ip":"10.0.0.2","ad":"AD1","can_reset_image":True,
                                             "created":"2026-10-03T00:00:00Z"}]}
                        elif path == "/api/oci/boot-volumes":
                            data = {"data":[{"id":"boot1","instance_id":"instance1","name":"boot",
                                              "size_gbs":50,"vpus":10,"state":"AVAILABLE"}]}
                        elif path == "/api/cloud/oci/action":
                            body = route.request.post_data_json
                            assert body["action"] in ("SOFTRESET", "TERMINATE")
                            if body["action"] == "TERMINATE":
                                assert body["preserve_boot_volume"] is True
                            data = {"ok":True}
                        elif path.startswith("/api/oci/"):
                            data = {"data":[]}
                        else:
                            route.continue_()
                            return
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
                    page.route("**/api/**", replies)
                    page.goto(base + access_path)
                    page.locator("#view-login").wait_for(state="visible")
                    page.fill("#login-user",username)
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
                    check_oci_account_switching(page)
                    page.click('nav button[data-view="instances"]')
                    page.locator('#inst-cards button[data-act="REBOOT"]').wait_for()
                    assert "50 GB" in page.locator("#inst-cards").inner_text()
                    preview = Path(os.environ.get("OCI_UI_PREVIEW_DIR", tempfile.gettempdir()))
                    preview.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(preview / "instance-cards.png"), full_page=True)
                    page.once("dialog", lambda dialog:dialog.accept())
                    page.click('#inst-cards button[data-act="REBOOT"]')
                    page.once("dialog", lambda dialog:dialog.accept("test-instance"))
                    page.click('#inst-cards button[data-act="TERMINATE_KEEP"]')
                    for name in ("launch","volumes","network","users","objects","domains","ssh","mail","settings"):
                        page.click(f'nav button[data-view="{name}"]')
                        assert page.locator(f"#view-{name}").is_visible()
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
                        response = page.request.post(base + access_path + "api/ssh/sessions", data={
                            "name":name,"host":f"203.0.113.{10+index}","username":"ubuntu",
                            "auth_type":"password","secret":"synthetic-browser-only","tags":"Ubuntu,OCI",
                            "proxy_command":"socks5h://user:private-proxy-password@127.0.0.1:1080" if index == 1 else ""})
                        assert response.ok
                    page.click('nav button[data-view="ssh"]')
                    page.wait_for_function("document.querySelectorAll('.session-card').length === 3")
                    page.wait_for_function("document.querySelector('#view-ssh').getAnimations({subtree:true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).every(a => a.playState !== 'running')")
                    page.screenshot(path=str(preview / "sessions.png"), full_page=True)
                    page.fill('#session-search', 'Development')
                    assert page.locator('.session-card').count() == 1
                    page.click('[data-scopy]')
                    page.wait_for_function("document.querySelectorAll('.session-card').length === 2")
                    page.fill('#session-search','')
                    assert page.locator('.session-card').count() == 4
                    assert 'synthetic-browser-only' not in page.content()
                    for button, panel in (("#btn-ssh-fwd", "#ssh-fwd-panel"), ("#btn-ssh-batch", "#ssh-batch"), ("#btn-ssh-monitor", "#ssh-monitor-panel")):
                        page.click(button)
                        assert page.locator(panel).is_visible()
                        assert page.locator(panel).evaluate("el => el.parentElement.id") == "ssh-tools-dock"
                        assert page.locator(panel).evaluate("el => el.getBoundingClientRect().top < innerHeight")
                        page.click(button)
                    # Exercise actual xterm/close UI; only the remote SSH transport is simulated.
                    page.evaluate("""() => {
                        const Base = window.Terminal;
                        window.__testTerminals = [];
                        window.Terminal = class extends Base {
                            constructor(options) { super(options); window.__testTerminals.push(this); }
                        };
                    }""")
                    page.locator('[data-sopen]').first.click()
                    page.locator('#term-area').wait_for(state="visible")
                    page.wait_for_function("document.querySelector('#view-ssh').getAnimations({subtree:true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).every(a => a.playState !== 'running')")
                    assert not errors, errors
                    page.wait_for_function("window.__testTerminals[0].buffer.active.getLine(0).translateToString().includes('Demo')", timeout=5000)
                    assert page.evaluate("document.querySelector('#view-ssh').firstElementChild.id") == "term-area"
                    page.wait_for_function("document.querySelector('#term-live-metrics').textContent.includes('12.5%')")
                    assert '直连' in page.locator('#term-connection-status').inner_text()
                    height_before = page.locator('#term-stack').evaluate('el => el.clientHeight')
                    handle = page.locator('#term-splitter')
                    handle.scroll_into_view_if_needed()
                    rect = handle.bounding_box()
                    page.mouse.move(rect['x'] + rect['width']/2, rect['y'] + rect['height']/2)
                    page.mouse.down()
                    page.mouse.move(rect['x'] + rect['width']/2, rect['y'] + rect['height']/2 - 80, steps=8)
                    page.mouse.up()
                    assert page.locator('#term-stack').evaluate('el => el.clientHeight') < height_before - 50
                    assert_terminal_content_fits(page)
                    initial_samples = len(monitor_requests)
                    page.wait_for_timeout(6000)
                    assert len(monitor_requests) > initial_samples
                    assert page.locator('#term-history svg polyline').count() == 2
                    assert 'MB/s' in page.locator('#term-live-metrics').inner_text()
                    page.locator('.term-holder:visible .xterm-screen').hover()
                    start_scroll = page.evaluate('scrollY')
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(200)
                    assert abs(page.evaluate('scrollY') - start_scroll) < 2
                    page.mouse.wheel(0, -3000)
                    page.wait_for_timeout(200)
                    assert abs(page.evaluate('scrollY') - start_scroll) < 2
                    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
                    page.evaluate("window.__testTerminals[0].select(0, 0, 4)")
                    page.locator('.term-holder:visible .xterm-screen').click(button="right", position={"x":20,"y":10})
                    page.wait_for_function("navigator.clipboard.readText().then(s => s === 'Demo')")
                    assert page.evaluate("window.__testTerminals[0].getSelection()") == "Demo"
                    assert not terminal_input, "Copy must not send terminal input"
                    page.evaluate("window.__testTerminals[0].clearSelection()")
                    page.evaluate("navigator.clipboard.writeText('paste-example')")
                    page.locator('.term-holder:visible .xterm-screen').click(button="right", position={"x":30,"y":140})
                    page.wait_for_timeout(200)
                    assert terminal_input == ['paste-example'], terminal_input
                    page.evaluate("""() => {
                        window.__clipboardRead = navigator.clipboard.readText;
                        navigator.clipboard.readText = () => Promise.reject(new DOMException('Denied', 'NotAllowedError'));
                    }""")
                    page.locator('.term-holder:visible .xterm-screen').click(button="right", position={"x":30,"y":140})
                    page.wait_for_function("document.querySelector('#toast').textContent.includes('Ctrl+V')")
                    assert terminal_input == ['paste-example'], "Denied paste must not send input"
                    page.evaluate("() => { navigator.clipboard.readText = window.__clipboardRead; }")
                    page.context.route("https://example.com/**", lambda route:route.fulfill(body="<title>Test link</title>"))
                    target = page.evaluate("""() => {
                        const t = window.__testTerminals[0];
                        const r = t.element.querySelector('.xterm-screen').getBoundingClientRect();
                        return {x:r.left + r.width/t.cols*12.5, y:r.top + r.height/t.rows*1.5};
                    }""")
                    page.mouse.move(target["x"], target["y"])
                    page.wait_for_timeout(200)
                    pages_before = len(page.context.pages)
                    page.mouse.click(target["x"], target["y"])
                    assert len(page.context.pages) == pages_before
                    with page.expect_popup() as opened:
                        page.keyboard.down("Control")
                        page.mouse.click(target["x"], target["y"])
                        page.keyboard.up("Control")
                    popup = opened.value
                    popup.wait_for_load_state()
                    assert popup.url == "https://example.com/test"
                    assert popup.evaluate("window.opener === null")
                    popup.close()
                    page.evaluate("() => new Promise(resolve => window.__testTerminals[0].write('line\\r\\n'.repeat(180), resolve))")
                    page.evaluate('window.__testTerminals[0].scrollToBottom()')
                    before_buffer = page.evaluate('window.__testTerminals[0].buffer.active.viewportY')
                    before_page = page.evaluate('scrollY')
                    page.locator('.term-holder:visible .xterm-screen').hover()
                    page.mouse.wheel(0, -400)
                    page.wait_for_timeout(250)
                    assert page.evaluate('window.__testTerminals[0].buffer.active.viewportY') < before_buffer
                    assert abs(page.evaluate('scrollY') - before_page) < 2
                    page.click('#btn-term-sftp')
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    assert page.evaluate("document.querySelector('#sftp-panel').parentElement.id") == "term-area"
                    page.locator('#sftp-table [data-fopen="blocked"]').click()
                    page.wait_for_function("document.querySelector('#sftp-status').textContent.includes('HTTP 502')")
                    assert page.locator('#sftp-path').inner_text() == '/'
                    page.locator('#sftp-table [data-fopen="子目录 space"]').click()
                    page.wait_for_function("document.querySelector('#sftp-path').textContent === '/子目录 space'")
                    page.click('#btn-sftp-up')
                    page.wait_for_function("document.querySelector('#sftp-path').textContent === '/'")
                    page.locator('#sftp-table [data-fopen="子目录 space"]').click()
                    page.wait_for_function("document.querySelector('#sftp-path').textContent === '/子目录 space'")
                    page.click('#btn-sftp-close')
                    page.click('#btn-term-sftp')
                    page.wait_for_function("document.querySelector('#sftp-path').textContent === '/子目录 space'")
                    page.locator('#sftp-breadcrumb [data-dir="/"]').click()
                    page.wait_for_function("document.querySelector('#sftp-path').textContent === '/'")
                    page.locator('#sftp-table [data-file="demo.txt"]').check()
                    with page.expect_download() as download:
                        page.click('#btn-sftp-download')
                    assert download.value.suggested_filename.endswith('.zip')
                    assert batch_downloads[-1]['names'] == ['demo.txt']
                    assert batch_downloads[-1]['session_id'] == int(page.locator('#term-tabs [data-tsid].active').get_attribute('data-tsid'))
                    page.locator('#sftp-file-list').hover()
                    page.wait_for_timeout(500)
                    list_page_scroll = page.evaluate('scrollY')
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(300)
                    assert page.locator('#sftp-file-list').evaluate('el => el.scrollTop') > 0
                    assert abs(page.evaluate('scrollY') - list_page_scroll) < 2
                    page.locator('#sftp-file-list').evaluate('el => el.scrollTop = el.scrollHeight')
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(250)
                    assert abs(page.evaluate('scrollY') - list_page_scroll) < 2
                    # Terminal toolbar/padding must allow ordinary page scrolling.
                    page.locator('.terminal-toolbar').hover()
                    page.wait_for_timeout(300)
                    outside_scroll = page.evaluate('scrollY')
                    page.mouse.wheel(0, 300)
                    page.wait_for_timeout(300)
                    assert page.evaluate('scrollY') > outside_scroll + 10
                    # Native XHR uploads, then deterministic intermediate progress and error checks.
                    page.locator('#sftp-upload-input').set_input_files({"name":"native.bin","mimeType":"application/octet-stream","buffer":b'abc' * 2048})
                    page.wait_for_function("document.querySelector('#sftp-upload-label').textContent.includes('远程已确认')")
                    page.evaluate("""() => {
                      window.__NativeXHR = XMLHttpRequest;
                      window.XMLHttpRequest = class {
                        constructor() { this.upload = {}; window.__uploadXHR = this; }
                        open() {} setRequestHeader() {}
                        send(file) { this.file = file; }
                        abort() { this.onabort(); }
                      };
                    }""")
                    page.locator('#sftp-upload-input').set_input_files({"name":"progress.bin","mimeType":"application/octet-stream","buffer":b'x' * 100})
                    page.evaluate("window.__uploadXHR.upload.onprogress({lengthComputable:true,loaded:50,total:100})")
                    assert page.locator('#sftp-upload-meter').evaluate('el => el.value') == 50
                    page.evaluate("window.__uploadXHR.upload.onprogress({lengthComputable:true,loaded:100,total:100})")
                    assert '等待远程写入确认' in page.locator('#sftp-upload-label').inner_text()
                    assert page.locator('#btn-sftp-upload').is_disabled()
                    page.evaluate("Object.assign(window.__uploadXHR,{status:409,responseText:JSON.stringify({detail:'同名文件'})}).onload()")
                    page.wait_for_function("!document.querySelector('#btn-sftp-upload').disabled")
                    assert '上传失败' in page.locator('#sftp-upload-label').inner_text()
                    page.evaluate("""() => {
                        const transfer = new DataTransfer();
                        transfer.items.add(new File(['abc'], 'drag-first.txt'));
                        transfer.items.add(new File(['def'], 'drag-second.txt'));
                        document.querySelector('#sftp-drop').dispatchEvent(new DragEvent('drop', {dataTransfer:transfer, bubbles:true}));
                    }""")
                    assert page.evaluate('window.__uploadXHR.file.name') == 'drag-first.txt'
                    page.click('#btn-sftp-cancel')
                    page.wait_for_function("!document.querySelector('#btn-sftp-upload').disabled")
                    assert '已取消' in page.locator('#sftp-upload-label').inner_text()
                    assert page.evaluate('window.__uploadXHR.file.name') == 'drag-first.txt'
                    page.evaluate('() => { window.XMLHttpRequest = window.__NativeXHR; }')
                    page.click('#btn-theme')
                    page.wait_for_function("window.__testTerminals[0].options.theme.background === '#202020'")
                    page.click('#btn-theme')
                    page.wait_for_function("window.__testTerminals[0].options.theme.background === '#fffdf7'")
                    page.evaluate("scrollTo(0, 0)")
                    page.wait_for_function("!document.querySelector('#toast').classList.contains('show')")
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(preview / "terminal.png"), full_page=True)
                    page.click('#btn-sftp-close')
                    page.locator('#sftp-panel').wait_for(state="hidden")
                    page.click('#btn-term-fullscreen')
                    page.wait_for_function("document.querySelector('#term-area').classList.contains('terminal-fullscreen')")
                    assert page.evaluate("getComputedStyle(document.documentElement).overflow") == 'hidden'
                    fullscreen_scroll = page.evaluate('scrollY')
                    page.locator('.term-holder:visible .xterm-screen').hover()
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(200)
                    assert page.evaluate('scrollY') == fullscreen_scroll
                    assert_terminal_content_fits(page)
                    page.click('#btn-term-sftp')
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    page.locator('#sftp-file-list').hover()
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(300)
                    assert page.locator('#sftp-file-list').evaluate('el => el.scrollTop') > 0
                    assert page.evaluate('scrollY') == fullscreen_scroll
                    split_height = page.locator('#term-stack').evaluate('el => el.clientHeight')
                    page.locator('#term-splitter').focus()
                    page.keyboard.press('ArrowUp')
                    assert page.locator('#term-stack').evaluate('el => el.clientHeight') < split_height - 20
                    for _ in range(30): page.keyboard.press('ArrowDown')
                    assert page.locator('#sftp-panel').evaluate('el => el.getBoundingClientRect().bottom <= innerHeight + 2')
                    for _ in range(8): page.keyboard.press('ArrowUp')
                    assert_terminal_content_fits(page)
                    assert page.locator('#btn-sftp-upload').evaluate('el => el.getBoundingClientRect().height') <= 30
                    assert page.locator('#sftp-file-list').evaluate('el => el.clientHeight') >= 140
                    assert page.locator('#sftp-breadcrumb').evaluate("el => getComputedStyle(el).flexDirection") == 'row'
                    page.evaluate("() => new Promise(resolve => window.__testTerminals[0].write('\\r\\nLAST-PROMPT> ', resolve))")
                    page.evaluate('window.__testTerminals[0].scrollToBottom()')
                    page.locator('#term-splitter').focus()
                    page.keyboard.press('ArrowUp')
                    assert_terminal_content_fits(page)
                    assert page.evaluate('window.__testTerminals[0].buffer.active.viewportY === window.__testTerminals[0].buffer.active.baseY')
                    page.screenshot(path=str(preview / "sftp-fullscreen.png"))
                    page.click('#btn-sftp-close')
                    page.keyboard.press("Escape")
                    assert page.evaluate("getComputedStyle(document.documentElement).overflow") != 'hidden'
                    assert not page.locator('.terminal-fullscreen').count()
                    first_sid = page.locator('#term-tabs [data-tsid].active').get_attribute('data-tsid')
                    page.locator('[data-sopen]').nth(1).click()
                    second_sid = page.locator('#term-tabs [data-tsid].active').get_attribute('data-tsid')
                    page.wait_for_function("document.querySelector('#term-live-metrics').textContent.includes('12.5%')")
                    assert monitor_requests[-1] == int(second_sid) and second_sid != first_sid
                    page.click('#btn-term-split')
                    assert page.locator('.term-holder:visible').count() == 2
                    assert_terminal_content_fits(page)
                    holders = page.locator('.term-holder:visible')
                    holders.first.locator('.xterm-screen').click()
                    page.keyboard.type('first-pane')
                    assert 'first-pane' == ''.join(inputs_by_session[first_sid][-10:])
                    holders.nth(1).locator('.xterm-screen').click()
                    page.keyboard.type('second-pane')
                    assert 'second-pane' == ''.join(inputs_by_session[second_sid][-11:])
                    page.screenshot(path=str(preview / 'ssh-split.png'), full_page=True)
                    page.click('#btn-term-split')
                    assert '代理连接' in page.locator('#term-connection-status').inner_text()
                    page.click('#btn-term-sftp')
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    assert '代理连接' in page.locator('#sftp-route').inner_text()
                    assert 'private-proxy-password' not in page.locator('#term-area').inner_text()
                    # Concurrent per-VPS upload queues must not share target/progress/cancel state.
                    page.evaluate("""() => {
                        window.__NativeXHR = XMLHttpRequest; window.__parallelUploads = [];
                        window.XMLHttpRequest = class {
                            constructor() { this.upload = {}; window.__parallelUploads.push(this); }
                            open(method, url) { this.url = url; } setRequestHeader() {}
                            send(file) { this.file = file; }
                            abort() { this.onabort(); }
                        };
                    }""")
                    page.locator('#sftp-upload-input').set_input_files({'name':'second-vps.bin','mimeType':'application/octet-stream','buffer':b'b'})
                    page.click(f'#term-tabs [data-tsid="{first_sid}"]')
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    assert not page.locator('#btn-sftp-upload').is_disabled()
                    page.locator('#sftp-upload-input').set_input_files([
                        {'name':'first-vps.bin','mimeType':'application/octet-stream','buffer':b'a'},
                        {'name':'never-uploaded.bin','mimeType':'application/octet-stream','buffer':b'x'}])
                    urls = page.evaluate('window.__parallelUploads.map(x => x.url)')
                    assert parse_qs(urlparse(urls[0]).query)['session_id'] == [second_sid]
                    assert parse_qs(urlparse(urls[1]).query)['session_id'] == [first_sid]
                    page.evaluate("Object.assign(window.__parallelUploads[0],{status:200,responseText:'{}'}).onload()")
                    assert 'first-vps.bin' in page.locator('#sftp-upload-label').inner_text()
                    assert page.locator('#btn-sftp-upload').is_disabled()
                    page.click('#btn-sftp-cancel')
                    page.wait_for_function("!document.querySelector('#btn-sftp-upload').disabled")
                    assert page.evaluate('window.__parallelUploads.length') == 2
                    page.click(f'#term-tabs [data-tsid="{second_sid}"]')
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    assert 'second-vps.bin' in page.locator('#sftp-upload-label').inner_text()
                    assert '远程已确认' in page.locator('#sftp-upload-label').inner_text()
                    page.evaluate('() => { window.XMLHttpRequest = window.__NativeXHR; }')
                    pending_reads = []
                    page.route('**/api/ssh/sftp/read?*', lambda route: pending_reads.append(route))
                    page.locator('#sftp-table [data-fedit="demo.txt"]').click()
                    page.wait_for_timeout(100)
                    assert pending_reads
                    page.click(f'#term-tabs [data-tsid="{first_sid}"]')
                    pending_reads.pop().fulfill(content_type='application/json', body='{"content":"OTHER VPS PRIVATE CONTENT"}')
                    page.wait_for_timeout(200)
                    assert page.locator('#sftp-edit').is_hidden()
                    assert 'OTHER VPS PRIVATE CONTENT' not in page.locator('#sftp-panel').inner_text()
                    assert '直连' in page.locator('#sftp-route').inner_text()
                    page.click('#btn-sftp-close')
                    page.wait_for_function("document.querySelector('#term-live-metrics').textContent.includes('12.5%')")
                    assert monitor_requests[-1] == int(first_sid)
                    page.locator(f'#term-tabs [data-tsid="{second_sid}"] + .term-close').click()
                    page.locator('.term-close').click()
                    page.locator('#term-area').wait_for(state="hidden")
                    samples_after_close = len(monitor_requests)
                    page.wait_for_timeout(5500)
                    assert len(monitor_requests) == samples_after_close
                    page.locator('[data-sopen]').first.click()
                    page.locator('#term-area').wait_for(state="visible")
                    assert page.locator('#term-stack').evaluate("el => el.style.getPropertyValue('--terminal-height')")
                    retry_sid = page.locator('#term-tabs [data-tsid].active').get_attribute('data-tsid')
                    terminal_routes[retry_sid].close()
                    page.wait_for_function("!document.querySelector('#btn-term-retry').disabled")
                    page.click('#btn-term-retry')
                    page.wait_for_function("document.querySelector('#term-connection-status').textContent.startsWith('已连接')")
                    page.locator('.term-close').click()
                    page.click('#btn-theme')
                    assert page.locator('html').get_attribute('data-theme') == 'dark'
                    page.set_viewport_size({"width":390,"height":844})
                    page.evaluate("scrollTo(0, 0)")
                    page.wait_for_function("!document.querySelector('#toast').classList.contains('show')")
                    page.wait_for_timeout(250)  # Allow CSS theme transitions to finish.
                    page.screenshot(path=str(preview / "mobile-dark.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.locator('.session-more summary').first.click()
                    page.locator('[data-ssftp]').first.click()
                    page.locator('#sftp-table [data-fopen="demo.txt"]').wait_for()
                    assert page.locator('#sftp-panel').evaluate('el => el.parentElement.id') == 'ssh-tools-dock'
                    page.locator('#sftp-file-list').hover()
                    page.wait_for_timeout(400)
                    mobile_scroll = page.evaluate('scrollY')
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(300)
                    assert page.locator('#sftp-file-list').evaluate('el => el.scrollTop') > 0
                    assert abs(page.evaluate('scrollY') - mobile_scroll) < 2
                    page.screenshot(path=str(preview / "sftp-mobile.png"))
                    page.click('#btn-sftp-close')
                    page.click('nav button[data-view="settings"]')
                    page.wait_for_function("document.querySelector('#cf-settings-state').textContent.includes('Token')")
                    assert page.locator('#mcp-endpoint').inner_text() == base + access_path + 'mcp'
                    page.fill('#cf-email', 'test@example.com')
                    page.fill('#cf-key', 'synthetic-browser-global-key')
                    page.click('#btn-cf-settings-save')
                    page.wait_for_function("document.querySelector('#cf-settings-state').textContent.includes('Global Key：已保存')")
                    assert page.locator('#cf-key').input_value() == ''
                    page.fill('#cf-email', 'updated@example.com')
                    with page.expect_response(lambda r: '/api/cf/settings' in r.url and r.request.method == 'POST') as saved:
                        page.click('#btn-cf-settings-save')
                    assert saved.value.status == 200
                    page.wait_for_function("document.querySelector('#cf-settings-state').textContent.includes('Global Key：已保存')")
                    page.route('**/api/cf/test', lambda route: route.fulfill(content_type='application/json', body='{"ok":true,"zones":1}'))
                    page.click('#btn-cf-test')
                    page.wait_for_function("document.querySelector('#cf-settings-state').textContent.includes('连接成功')")
                    page.fill("#p-old",password)
                    page.fill("#p-new","browser-new-password-32-characters")
                    page.click("#btn-save-pass")
                    page.locator("#view-login").wait_for(state="visible")
                    assert not errors, errors
                    browser.close()
                print("PASS: Chrome login, cloud cards, diagnostics, sessions, terminal right-click copy, Ctrl-click link, SFTP dock, live theme, fullscreen, close/reopen, mobile layout, password change, upload progress/errors, scroll containment, tool panels and live metrics switching/stop; cloud/SSH transport simulated")
            finally:
                if proc.poll() is None:
                    proc.stdin.write(b'x')
                    proc.stdin.flush()
                proc.wait(timeout=20)
                proc.stdin.close()


if __name__ == "__main__":
    main()

"""Chrome verification with real account CRUD and simulated Google cloud responses."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlparse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='.gcp-browser-',dir=ROOT) as directory:
        env=dict(os.environ,HOST='127.0.0.1',PORT=str(port),PANEL_DATA_DIR=directory,PYTHONIOENCODING='utf-8')
        with open(Path(directory)/'server.log','w',encoding='utf-8') as log:
            launcher = """import os, sys, threading, uvicorn, main
server = uvicorn.Server(uvicorn.Config(main.app, host='127.0.0.1', port=int(os.environ['PORT'])))
def stop():
    sys.stdin.read(1)
    server.should_exit = True
threading.Thread(target=stop, daemon=True).start()
server.run()
"""
            proc=subprocess.Popen([sys.executable,'-c',launcher],cwd=ROOT,env=env,stdout=log,stderr=log,stdin=subprocess.PIPE)
            try:
                base=f'http://127.0.0.1:{port}'
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        with opener.open(base+'/healthz',timeout=1): break
                    except OSError: time.sleep(.2)
                details=(Path(directory)/'initial_admin_credentials.txt').read_text(encoding='utf-8').splitlines()
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='chrome',headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1000})
                    errors=[]; requests=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def replies(route):
                        parsed=urlparse(route.request.url); path=parsed.path.split('/api',1)[-1]
                        endpoint=path.rsplit('/',1)[-1]
                        if not path.startswith('/gcp/accounts/') or endpoint not in ('instances','options','create','action','operation','traffic','sync-ssh'):
                            route.continue_(); return
                        params=parse_qs(parsed.query); body=route.request.post_data_json if route.request.method=='POST' else None
                        requests.append((path,params,body))
                        if endpoint=='instances':
                            data={'instances':[dict(id='123',name='browser-vm',status='RUNNING',zone='us-west1-a',machine_type='e2-micro',public_ip='203.0.113.9',private_ip='10.0.0.9',disks=[dict(name='boot',auto_delete=False)])],
                                  'overview':dict(total=1,running=1,free_tier_candidates=1,zones={'us-west1-a':1})}
                        elif endpoint=='options':
                            data={'machine_types':[dict(name='e2-micro',cpus=2,memory_mb=1024)]} if 'zone' in params else dict(zones=['us-west1-a','us-east1-b'],networks=[dict(name='default',automatic=True)],subnets=[])
                        elif endpoint=='traffic': data=dict(rx_bytes=1073741824,tx_bytes=2147483648,rows=[dict(date='2026-10-01',instance_id='123',rx_bytes=1073741824,tx_bytes=2147483648)])
                        elif endpoint=='sync-ssh': data=dict(added=1,updated=0,skipped=0,note='请填写 SSH 私钥')
                        else: data=dict(name='operation-123',status='DONE')
                        route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
                    page.route('**/api/**',replies)
                    page.goto(base+details[0].split(': ',1)[1])
                    page.fill('#login-user',details[1].split(': ',1)[1]); page.fill('#login-pass',details[2].split(': ',1)[1]); page.click('#btn-login')
                    page.locator('#app').wait_for(state='visible')
                    page.click('nav button[data-view="gcp"]')
                    page.wait_for_function("document.querySelector('#gcp-identity').textContent.includes('尚未配置')")
                    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
                    pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
                    public=key.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
                    def add(name,project,proxy):
                        page.click('#gcp-new'); page.fill('#gcp-name',name); page.fill('#gcp-project',project)
                        page.fill('#gcp-json',json.dumps(dict(type='service_account',client_email=f'manager@{project}.iam.gserviceaccount.com',private_key=pem)))
                        page.fill('#gcp-proxy',proxy); page.click('#gcp-account-form button[type="submit"]')
                        page.locator('#gcp-account-form').wait_for(state='hidden')
                        page.wait_for_function('name => document.querySelector("#gcp-identity").textContent.includes(name)',arg=name)
                    add('GCP direct','first-project','')
                    add('GCP proxy','second-project','socks5h://user:secret@127.0.0.1:1080')
                    assert '代理' in page.locator('#gcp-route').inner_text()
                    assert 'secret' not in page.locator('#view-gcp').inner_text()
                    assert page.locator('#inst-account option').count()==0 or 'GCP' not in page.locator('#inst-account').inner_text()
                    page.click('#gcp-refresh'); page.locator('.gcp-instance').wait_for()
                    assert 'GCP proxy' in page.locator('.gcp-instance').inner_text()
                    page.click('#gcp-edit'); assert page.input_value('#gcp-json')=='' and page.input_value('#gcp-proxy')==''
                    page.fill('#gcp-name','GCP proxy edited'); page.click('#gcp-account-form button[type="submit"]')
                    page.locator('#gcp-account-form').wait_for(state='hidden')
                    assert '代理' in page.locator('#gcp-route').inner_text()
                    page.select_option('#gcp-account',label='GCP direct · first-project')
                    assert page.locator('#gcp-instances').inner_text()=='' and page.locator('#gcp-route').inner_text()=='直连'
                    page.click('#gcp-open-create'); page.locator('#gcp-create-form').wait_for(state='visible')
                    assert page.input_value('#gcp-machine')=='e2-micro'
                    page.select_option('#gcp-zone','us-east1-b')
                    page.wait_for_function("document.querySelector('#gcp-machine').value === 'e2-micro'")
                    page.fill('#gcp-key',public)
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.click('#gcp-create-form button[type="submit"]')
                    page.locator('.gcp-instance').wait_for()
                    page.wait_for_function("document.querySelector('#gcp-message').textContent.includes('已读取')")
                    page.once('dialog',lambda dialog:dialog.accept('browser-vm'))
                    page.click('.gcp-instance button[data-action="delete"]')
                    page.wait_for_function("document.querySelector('#gcp-message').textContent.includes('已读取')")
                    page.click('#gcp-traffic'); page.locator('#gcp-traffic-panel').wait_for(state='visible')
                    assert '1.000 GiB' in page.locator('#gcp-traffic-summary').inner_text()
                    page.click('#gcp-open-sync'); page.fill('#gcp-sync-proxy','socks5h://ssh-only:1080')
                    page.click('#gcp-sync-form button[type="submit"]')
                    page.locator('#gcp-sync-form').wait_for(state='hidden')
                    assert any(body and body.get('ssh_proxy')=='socks5h://ssh-only:1080' for _,_,body in requests)
                    assert any(body and body.get('preserve_disks') is True for _,_,body in requests)
                    assert any(body and body.get('zone')=='us-east1-b' and body.get('machine_type')=='e2-micro' for _,_,body in requests)
                    page.screenshot(path=str(Path(tempfile.gettempdir())/'oci-gcp-preview.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
                    page.click('nav button[data-view="accounts"]',force=True)
                    assert page.locator('#a-platform option').count()==1
                    assert not errors,errors
                    browser.close()
                print('PASS: GCP account CRUD, secret masking, direct/proxy switching, OCI selector unchanged, create/actions/polling, traffic, SSH routing, mobile layout; Google transport simulated')
            finally:
                if proc.poll() is None:
                    proc.stdin.write(b'x'); proc.stdin.flush()
                proc.wait(timeout=20)
                proc.stdin.close()


if __name__=='__main__': main()

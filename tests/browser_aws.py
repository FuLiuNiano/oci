"""Real Chrome/account CRUD; only Lightsail resource replies are simulated."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
import urllib.request

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='.aws-browser-',dir=ROOT) as directory:
        env=dict(os.environ,HOST='127.0.0.1',PORT=str(port),PANEL_DATA_DIR=directory,PYTHONIOENCODING='utf-8')
        with open(Path(directory)/'server.log','w',encoding='utf-8') as log:
            launcher="""import os, sys, threading, uvicorn, main, tasks
tasks.start = lambda: None
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
                    errors=[]; calls=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def replies(route):
                        path=urlparse(route.request.url).path.split('/api',1)[-1]
                        endpoint=path.rsplit('/',1)[-1]
                        if not path.startswith('/aws/accounts/') or endpoint not in ('identity','instances','options','create','action','operation','metrics','snapshots','static-ips','sync-ssh'):
                            route.continue_(); return
                        body=route.request.post_data_json if route.request.method=='POST' else None
                        calls.append((path,body))
                        if endpoint=='identity': data=dict(account_id='111111111111',arn='arn:aws:iam::111111111111:user/panel')
                        elif endpoint=='instances':
                            data={'instances':[dict(id='arn:aws:lightsail:vm',name='browser-vm',status='running',zone='ap-northeast-1a',machine_type='nano_3_0',blueprint='Ubuntu 24.04',cpus=2,memory_gb=.5,public_ip='203.0.113.9',private_ip='10.0.0.9',ipv6=['2001:db8::9'],static_ip=False,username='ubuntu',key_name='LightsailDefaultKeyPair',disks=[])], 'overview':dict(total=1,running=1)}
                        elif endpoint=='options':
                            data=dict(zones=['ap-northeast-1a'],blueprints=[dict(id='ubuntu',name='Ubuntu 24.04',min_power=0)],key_pairs=['LightsailDefaultKeyPair'],
                                bundles=[dict(id='nano',name='Nano',cpus=2,memory_gb=.5,disk_gb=20,price=5,power=1,ipv4_count=1),dict(id='nano_ipv6',name='Nano IPv6',cpus=2,memory_gb=.5,disk_gb=20,price=3.5,power=1,ipv4_count=0)])
                        elif endpoint=='metrics': data=dict(rx_bytes=1073741824,tx_bytes=2147483648,rows=[dict(timestamp='2026-10-10T00:00:00Z',cpu=42,rx_bytes=1073741824,tx_bytes=2147483648)])
                        elif endpoint=='snapshots' and body is None: data=[dict(name='browser-backup',source='browser-vm',state='available',size_gb=20)]
                        elif endpoint=='static-ips' and body is None: data=[dict(name='browser-ip',ip='203.0.113.20',attached=False,attached_to='')]
                        elif endpoint=='sync-ssh': data=dict(added=1,updated=0,skipped=0,note='请填写 SSH 私钥')
                        elif endpoint=='operation': data=dict(id='op-123',status='Succeeded',terminal=True)
                        else: data=dict(operations=[dict(id='op-123',status='Started',terminal=False)])
                        route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
                    page.route('**/api/**',replies)
                    page.goto(base+details[0].split(': ',1)[1])
                    page.fill('#login-user',details[1].split(': ',1)[1]); page.fill('#login-pass',details[2].split(': ',1)[1]); page.click('#btn-login')
                    page.locator('#app').wait_for(state='visible')
                    page.click('nav button[data-view="aws"]')
                    page.wait_for_function("document.querySelector('#aws-identity').textContent.includes('尚未配置')")
                    def add(name,number,proxy):
                        page.click('#aws-new'); page.fill('#aws-name',name); page.fill('#aws-access','AKIA'+str(number)*16)
                        page.fill('#aws-secret',str(number)*40); page.fill('#aws-proxy',proxy)
                        page.click('#aws-account-form button[type="submit"]'); page.locator('#aws-account-form').wait_for(state='hidden')
                        page.wait_for_function('name=>document.querySelector("#aws-identity").textContent.includes(name)',arg=name)
                    add('AWS direct',1,''); add('AWS proxy',2,'socks5h://user:proxy-secret@127.0.0.1:1080')
                    assert '代理' in page.locator('#aws-route').inner_text()
                    assert 'proxy-secret' not in page.locator('#view-aws').inner_text()
                    assert 'AWS' not in page.locator('#inst-account').inner_text()
                    page.click('#aws-test'); page.wait_for_function("document.querySelector('#aws-message').textContent.includes('认证成功')")
                    page.click('#aws-refresh'); page.locator('.aws-instance').wait_for()
                    assert 'AWS proxy' in page.locator('.aws-instance').inner_text()
                    page.click('#aws-edit'); assert page.input_value('#aws-access')==page.input_value('#aws-secret')==page.input_value('#aws-proxy')==''
                    page.fill('#aws-name','AWS proxy edited'); page.click('#aws-account-form button[type="submit"]'); page.locator('#aws-account-form').wait_for(state='hidden')
                    assert '代理' in page.locator('#aws-route').inner_text()
                    page.select_option('#aws-account',label='AWS direct · ap-northeast-1')
                    assert page.locator('#aws-instances').inner_text()=='' and page.locator('#aws-route').inner_text()=='直连'
                    page.click('#aws-open-create'); page.locator('#aws-create-form').wait_for(state='visible')
                    assert page.input_value('#aws-bundle')=='nano' and page.locator('#aws-bundle option').count()==1
                    page.select_option('#aws-ip-type','ipv6'); assert page.input_value('#aws-bundle')=='nano_ipv6'
                    page.once('dialog',lambda dialog:dialog.accept()); page.click('#aws-create-form button[type="submit"]')
                    page.wait_for_function("document.querySelector('#aws-message').textContent.includes('已读取')")
                    page.once('dialog',lambda dialog:dialog.accept()); page.click('.aws-instance button[data-action="stop"]')
                    page.wait_for_function("document.querySelector('#aws-message').textContent.includes('已读取')")
                    page.click('.aws-instance button[data-action="metrics"]'); page.locator('#aws-metrics-panel').wait_for(state='visible')
                    assert '1.000 GiB' in page.locator('#aws-metrics-summary').inner_text()
                    page.once('dialog',lambda dialog:dialog.accept('browser-backup')); page.click('.aws-instance button[data-action="snapshot"]')
                    page.locator('#aws-snapshots-panel').wait_for(state='visible'); assert 'available' in page.locator('#aws-snapshot-rows').inner_text()
                    page.click('#aws-open-ips'); page.locator('#aws-ips-panel').wait_for(state='visible')
                    page.once('dialog',lambda dialog:dialog.accept('browser-vm')); page.click('#aws-ip-rows button[data-action="attach"]')
                    page.wait_for_function("document.querySelector('#aws-message').textContent.includes('静态 IP 已读取')")
                    page.click('#aws-open-sync'); page.fill('#aws-sync-proxy','socks5h://ssh-only:1080')
                    page.click('#aws-sync-form button[type="submit"]'); page.locator('#aws-sync-form').wait_for(state='hidden')
                    assert any(body and body.get('ssh_proxy')=='socks5h://ssh-only:1080' for _,body in calls)
                    assert any(body and body.get('ip_type')=='ipv6' and body.get('bundle_id')=='nano_ipv6' for _,body in calls)
                    # Hold an old account reply across a selector switch; it must never render in the other account.
                    page.evaluate("""() => {
                      window.awsOriginalApi=api;
                      api=(path,opts)=>path.includes('/aws/accounts/') && path.includes('/instances?') ? new Promise(resolve=>window.awsLateReply=resolve) : window.awsOriginalApi(path,opts);
                    }""")
                    page.click('#aws-refresh'); page.wait_for_function('!!window.awsLateReply')
                    page.select_option('#aws-account',label='AWS proxy edited · ap-northeast-1')
                    page.evaluate("""() => { window.awsLateReply({instances:[{name:'wrong-account'}],overview:{total:1,running:1}}); api=window.awsOriginalApi; }""")
                    page.wait_for_timeout(150)
                    assert page.locator('#aws-instances').inner_text()==''
                    page.click('#aws-refresh'); page.locator('.aws-instance').wait_for()
                    page.screenshot(path=str(Path(tempfile.gettempdir())/'oci-aws-preview.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
                    page.click('nav button[data-view="accounts"]',force=True)
                    assert page.locator('#a-platform option').count()==1
                    assert not errors,errors
                    browser.close()
                print('PASS: Lightsail account CRUD, key masking, route/region state, OCI untouched, create/bundles/actions, metrics, snapshots, static IPs, SSH proxy, late reply isolation, mobile layout; AWS resources simulated')
            finally:
                if proc.poll() is None:
                    proc.stdin.write(b'x'); proc.stdin.flush()
                proc.wait(timeout=20); proc.stdin.close()


if __name__=='__main__': main()

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import gcp_api
import gcp_service as gcp
import store
from test_proxy import socks_proxy  # Local SOCKS server; does not contact Google.


def key_json(credentials, email='manager@first-project.iam.gserviceaccount.com'):
    return dict(type='service_account', private_key=credentials['private_key'], client_email=email,
                project_id='first-project', token_uri='https://untrusted.invalid/token')


def add_account(client, credentials, project='first-project', proxy='', name='GCP'):
    response = client.post('/api/gcp/accounts', json=dict(name=name, project_id=project,
        credentials=key_json(credentials, f'manager@{project}.iam.gserviceaccount.com'), proxy_url=proxy))
    assert response.status_code == 200, response.text
    return response.json()


def raw_account(a):
    return store.query('SELECT * FROM gcp_accounts WHERE id=?', (a['id'],))[0]


def test_accounts_preserve_oci_and_redact_secrets(client, credentials):
    oid = store.execute('INSERT INTO accounts(name,region,params) VALUES(?,?,?)', ('OCI','ap-tokyo-1',json.dumps(credentials)))
    before = store.query('SELECT * FROM accounts')
    a = add_account(client,credentials,proxy='socks5h://username:secret@127.0.0.1:1080')
    assert 'secret' not in json.dumps(a) and 'username' not in json.dumps(a)
    assert 'private_key' not in json.dumps(client.get('/api/gcp/accounts').json())
    assert 'untrusted.invalid' not in raw_account(a)['credentials']
    response = client.put(f"/api/gcp/accounts/{a['id']}",json=dict(name='GCP edited',project_id=a['project_id'],revision=a['revision']))
    assert response.status_code == 200
    assert raw_account(a)['proxy_url'].endswith('127.0.0.1:1080')
    assert response.json()['revision'] == a['revision']
    body=dict(name='GCP direct',project_id=a['project_id'],revision=a['revision'],proxy_url='')
    assert client.put(f"/api/gcp/accounts/{a['id']}",json=body).status_code == 400
    response = client.put(f"/api/gcp/accounts/{a['id']}",json={**body,'remove_proxy':True})
    assert response.status_code == 200 and response.json()['revision'] != a['revision']
    assert client.get(f"/api/gcp/accounts/{a['id']}/instances",params={'revision':a['revision']}).status_code == 409
    assert client.get('/api/accounts').json()['data'][0]['id'] == oid
    assert len(client.get('/api/accounts').json()['data']) == 1
    assert client.delete(f"/api/gcp/accounts/{a['id']}",params={'revision':response.json()['revision']}).status_code == 200
    assert store.query('SELECT * FROM accounts') == before


@pytest.mark.parametrize('proxy', ['file:///tmp/x','https://host/path','socks5h://host:70000','garbage'])
def test_invalid_proxy_never_saved(client, credentials, proxy):
    response = client.post('/api/gcp/accounts',json=dict(name='bad',project_id='first-project',credentials=key_json(credentials),proxy_url=proxy))
    assert response.status_code == 400
    assert client.get('/api/gcp/accounts').json() == []


def test_routes_require_login(client,credentials):
    a = add_account(client,credentials)
    client.cookies.clear()
    assert client.get('/api/gcp/accounts').status_code == 401
    assert client.post(f"/api/gcp/accounts/{a['id']}/action",json=dict(revision=a['revision'],zone='us-west1-a',name='vm',action='stop')).status_code == 401


def test_separate_jwt_and_proxy_routes_even_with_environment(credentials,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://unrelated.invalid:1234')
    key = serialization.load_pem_private_key(credentials['private_key'].encode(),None)
    calls = []
    def transport(session, method, url, **kwargs):
        assert session.trust_env is False
        proxy = kwargs['proxies']
        assert kwargs['allow_redirects'] is False
        if url == gcp.TOKEN_URL:
            parts = kwargs['data']['assertion'].split('.')
            decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
            key.public_key().verify(decode(parts[2]),('.'.join(parts[:2])).encode(),padding.PKCS1v15(),hashes.SHA256())
            claims = json.loads(decode(parts[1]))
            assert claims['aud'] == gcp.TOKEN_URL and claims['exp']-claims['iat'] == 3600
            token = claims['iss']
            calls.append((url,token,proxy))
            data = {'access_token':token,'expires_in':3600}
        else:
            token = kwargs['headers']['Authorization'].removeprefix('Bearer ')
            project = url.split('/projects/')[1].split('/')[0]
            assert token == f'manager@{project}.iam.gserviceaccount.com'
            assert proxy == ({} if project == 'first-project' else {'http':'socks5h://host:1080','https':'socks5h://host:1080'})
            calls.append((url,token,proxy)); data = {'items':{}}
        response = Mock(status_code=200,ok=True); response.json.return_value=data
        return response
    monkeypatch.setattr(requests.Session,'request',transport)
    def worker(project,proxy):
        with gcp.Client(dict(project_id=project,credentials=key_json(credentials,f'manager@{project}.iam.gserviceaccount.com'),proxy_url=proxy)) as c:
            assert c.instances() == []
            assert c.instances() == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(worker,'first-project',''),pool.submit(worker,'second-project','socks5://host:1080')]
        for f in futures: f.result()
    assert sum(url == gcp.TOKEN_URL for url,_,_ in calls) == 2
    assert len(calls) == 6


def test_proxy_failure_and_redirect_do_not_retry_direct(credentials,monkeypatch):
    account=dict(project_id='first-project',credentials=key_json(credentials),proxy_url='socks5h://127.0.0.1:1')
    request = Mock(side_effect=requests.exceptions.ProxyError('contains-password'))
    monkeypatch.setattr(requests.Session,'request',request)
    with gcp.Client(account) as c:
        with pytest.raises(gcp.GcpError,match='未回退直连'): c.instances()
    assert request.call_count == 1
    assert request.call_args.kwargs['proxies']['https'] == account['proxy_url']
    response=Mock(status_code=302)
    request.side_effect=None; request.return_value=response; request.reset_mock()
    with gcp.Client(account) as c:
        with pytest.raises(gcp.GcpError,match='重定向'): c.instances()
    assert request.call_count == 1


def test_pagination_and_traffic_scope(credentials,monkeypatch):
    with gcp.Client(dict(project_id='first-project',credentials=key_json(credentials))) as c:
        responses=[{'items':{'zones/us-west1-a':{'instances':[{'id':'1','name':'one','zone':'us-west1-a','machineType':'e2-micro','status':'RUNNING'}]}},'nextPageToken':'page-2'}, {'items':{'zones/us-east1-a':{'instances':[{'id':'2','name':'two','zone':'us-east1-a','machineType':'e2-micro','status':'TERMINATED'}]}}}]
        call=Mock(side_effect=responses); monkeypatch.setattr(c,'call',call)
        rows=c.instances()
        assert len(rows)==2 and gcp.overview(rows)['free_tier_candidates']==2
        assert call.call_args_list[1].kwargs['params']['pageToken']=='page-2'
        page={'timeSeries':[{'resource':{'labels':{'instance_id':'1'}},'points':[{'interval':{'endTime':'2026-10-01T00:00:00Z'},'value':{'int64Value':'12'}}]}]}
        call.side_effect=[page,page]
        result=c.traffic()
        assert result['rx_bytes']==12 and result['tx_bytes']==12
        for request in call.call_args_list[-2:]:
            assert 'project_id="first-project"' in request.kwargs['params']['filter']
            assert request.kwargs['monitoring'] is True


def test_create_request_and_preserve_disk_deletion(credentials,monkeypatch):
    key=serialization.load_pem_private_key(credentials['private_key'].encode(),None)
    pub=key.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    with gcp.Client(dict(project_id='first-project',credentials=key_json(credentials))) as c:
        call=Mock(return_value={'name':'operation-1','status':'DONE'}); monkeypatch.setattr(c,'call',call)
        body=dict(name='vm',zone='us-west1-a',machine_type='e2-micro',image='debian-12',disk_gb=20,network='default',username='debian',ssh_key=pub)
        c.create(body)
        request=call.call_args.args[2]
        assert request['disks'][0]['autoDelete'] is False
        assert 'projects/first-project/' in request['networkInterfaces'][0]['network']
        assert not request.get('serviceAccounts')
        with pytest.raises(gcp.GcpError): c.create({**body,'ssh_key':pub+'\n'+pub})
        with pytest.raises(gcp.GcpError): c.create({**body,'zone':'../other-project'})
        call.reset_mock()
        call.side_effect=[{'disks':[{'deviceName':'boot','autoDelete':True},{'deviceName':'data','autoDelete':False}]}, {'name':'operation-2'}, {'name':'operation-2','status':'DONE'}, {'name':'operation-3'}]
        c.action('us-west1-a','vm','delete')
        assert [r.args[0] for r in call.call_args_list] == ['GET','POST','GET','DELETE']
        assert call.call_args_list[1].kwargs['params']['autoDelete']=='false'
        assert 'deleteDisks' not in call.call_args.kwargs['params']


def test_disk_preservation_failure_blocks_delete(credentials,monkeypatch):
    with gcp.Client(dict(project_id='first-project',credentials=key_json(credentials))) as c:
        call=Mock(side_effect=[{'disks':[{'deviceName':'boot','autoDelete':True}]},gcp.GcpError('permission denied')])
        monkeypatch.setattr(c,'call',call)
        with pytest.raises(gcp.GcpError): c.action('us-west1-a','vm','delete')
        assert all(r.args[0] != 'DELETE' for r in call.call_args_list)


def test_ip_rotation_rejects_static_and_reports_partial_failure(credentials,monkeypatch):
    instance={'networkInterfaces':[{'name':'nic0','accessConfigs':[{'name':'External NAT','type':'ONE_TO_ONE_NAT','natIP':'203.0.113.1'}]}]}
    with gcp.Client(dict(project_id='first-project',credentials=key_json(credentials))) as c:
        call=Mock(side_effect=[instance,{'items':[{'name':'reserved'}]}]); monkeypatch.setattr(c,'call',call)
        with pytest.raises(gcp.GcpError,match='静态 IP'): c.action('us-west1-a','vm','change-ip')
        assert call.call_count == 2
        call.reset_mock(); call.side_effect=[instance,{}, {'name':'operation-1'}, {'name':'operation-1','status':'DONE'},gcp.GcpError('no quota')]
        with pytest.raises(gcp.GcpError,match='旧公网配置已移除'): c.action('us-west1-a','vm','change-ip')


def test_sync_same_ip_different_accounts_preserves_existing_ssh(client,credentials,monkeypatch):
    original=store.execute('INSERT INTO ssh_sessions(name,host,secret,proxy_command) VALUES(?,?,?,?)',('OCI VPS','203.0.113.1','oci-secret',''))
    before=store.query('SELECT * FROM ssh_sessions WHERE id=?',(original,))[0]
    rows=[dict(id='vm-id',name='vm',public_ip='203.0.113.1',private_ip='10.0.0.1',zone='us-west1-a',machine_type='e2-micro')]
    monkeypatch.setattr(gcp.Client,'instances',lambda _:rows)
    a=add_account(client,credentials); b=add_account(client,credentials,'second-project','socks5h://host:1080','GCP proxy')
    def sync(account,proxy=''):
        return client.post(f"/api/gcp/accounts/{account['id']}/sync-ssh",json=dict(revision=account['revision'],ssh_proxy=proxy))
    assert sync(a).json()['added']==1
    assert sync(b,'socks5h://ssh-proxy:1080').json()['added']==1
    assert sync(a,'socks5h://different:1080').json()['updated']==1
    sessions=store.query('SELECT * FROM ssh_sessions ORDER BY id')
    assert len(sessions)==3 and sessions[0]==before
    assert sessions[1]['proxy_command']=='' and sessions[2]['proxy_command']=='socks5h://ssh-proxy:1080'
    assert json.loads(sessions[1]['metadata'])['account_id']==a['id']
    assert json.loads(sessions[2]['metadata'])['account_id']==b['id']
    rows[0]['public_ip']='203.0.113.2'
    assert sync(a).json()['skipped']==1
    assert store.query('SELECT host FROM ssh_sessions WHERE id=?',(sessions[1]['id'],))[0]['host']=='203.0.113.1'
    assert sync(b,'http://not-socks:1080').status_code==400


def test_actions_confirm_and_stale_revision_block_network(client,credentials,monkeypatch):
    a=add_account(client,credentials)
    mock=Mock(); monkeypatch.setattr(gcp.Client,'action',mock)
    path=f"/api/gcp/accounts/{a['id']}/action"
    body=dict(revision=a['revision'],name='vm',zone='us-west1-a',action='delete')
    assert client.post(path,json=body).status_code==400
    assert client.post(path,json={**body,'revision':'stale','confirmed_name':'vm'}).status_code==409
    assert mock.call_count==0
    mock.return_value={'name':'operation-1'}
    assert client.post(path,json={**body,'confirmed_name':'vm'}).status_code==200
    assert mock.call_args.args == ('us-west1-a','vm','delete',True)


def test_real_https_direct_and_socks_jwt_routes(credentials,socks_proxy,tmp_path,monkeypatch):
    import ssl
    import threading
    from datetime import datetime, timedelta, timezone
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import ipaddress
    from urllib.parse import parse_qs
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    proxy,destinations,_ = socks_proxy
    keys={'manager@first-project.iam.gserviceaccount.com':serialization.load_pem_private_key(credentials['private_key'].encode(),None),
          'manager@second-project.iam.gserviceaccount.com':rsa.generate_private_key(public_exponent=65537,key_size=2048)}
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def reply(self,data):
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        def do_POST(self):
            body=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            pieces=body['assertion'][0].split('.')
            decode=lambda s:base64.urlsafe_b64decode(s+'='*(-len(s)%4))
            claims=json.loads(decode(pieces[1])); email=claims['iss']
            keys[email].public_key().verify(decode(pieces[2]),('.'.join(pieces[:2])).encode(),padding.PKCS1v15(),hashes.SHA256())
            seen.append(('token',email)); self.reply(dict(access_token=email,expires_in=3600))
        def do_GET(self):
            email=self.headers['Authorization'].removeprefix('Bearer ')
            assert email in keys
            if '/projects/' in self.path: assert email.split('@')[1].split('.')[0] in self.path
            seen.append(('api',email)); self.reply({'items':{}})
        def log_message(self,*_): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    key=next(iter(keys.values())); subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'GCP local test')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.DNSName('gcp.example.invalid')]),critical=False)
          .sign(key,hashes.SHA256()))
    certpath=tmp_path/'local-ca.pem'; keypath=tmp_path/'local-server.pem'
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM)); keypath.write_text(credentials['private_key'])
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(str(certpath),str(keypath))
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    port=server.server_port
    monkeypatch.setattr(gcp,'TOKEN_URL',f'https://127.0.0.1:{port}/token')
    monkeypatch.setattr(gcp,'COMPUTE',f'https://127.0.0.1:{port}/projects/')
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1'); monkeypatch.setenv('ALL_PROXY','socks5h://127.0.0.1:1'); monkeypatch.setenv('NO_PROXY','*')
    def configuration(project,proxy):
        email=f'manager@{project}.iam.gserviceaccount.com'
        pem=keys[email].private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        return dict(project_id=project,credentials=dict(type='service_account',client_email=email,private_key=pem),proxy_url=proxy)
    try:
        with gcp.Client(configuration('first-project','')) as direct, gcp.Client(configuration('second-project',proxy)) as proxied:
            direct.session.verify=proxied.session.verify=str(certpath)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(c.instances) for c in (direct,proxied)]
                for future in futures: assert future.result(timeout=10)==[]
            assert destinations == [('127.0.0.1',port)]*2
            proxied._send('GET',f'https://gcp.example.invalid:{port}/remote-dns',headers={'Authorization':'Bearer '+proxied.token})
            assert destinations[-1]==('gcp.example.invalid',port)
            count=len(seen)
            with gcp.Client(configuration('second-project','socks5h://127.0.0.1:1')) as dead:
                dead.session.verify=str(certpath)
                with pytest.raises(gcp.GcpError,match='未回退直连'): dead.instances()
            assert len(seen)==count
            assert direct.instances()==[] and len(seen)==count+1
            assert len([x for x in seen if x[0]=='token'])==2
    finally:
        server.shutdown(); server.server_close(); thread.join(3)

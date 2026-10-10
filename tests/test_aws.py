"""Real AWS models and signatures; synthetic credentials and local transports only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

import aws_api
import aws_service as aws
import store
from test_proxy import socks_proxy


def config(proxy='', region='ap-northeast-1', number=1, token=''):
    return dict(region=region,proxy_url=proxy,credentials=dict(access_key_id='AKIA' + str(number)*16,
        secret_access_key=str(number)*40,session_token=token))


def add_account(client,proxy='',region='ap-northeast-1',number=1):
    c=config(proxy,region,number)
    response=client.post('/api/aws/accounts',json=dict(name=f'AWS {number}',region=region,proxy_url=proxy,**c['credentials']))
    assert response.status_code==200,response.text
    return response.json()


def response(data,status=200):
    result=requests.Response(); result.status_code=status; result._content=json.dumps(data).encode()
    result.headers['Content-Type']='application/x-amz-json-1.1'
    return result


def verify_signature(url,headers,body,secret):
    authorization=headers['Authorization']
    scope=authorization.split('Credential=')[1].split(',',1)[0]
    access,day,region,service,_=scope.split('/')
    signed=authorization.split('SignedHeaders=')[1].split(',',1)[0].split(';')
    request=AWSRequest(method='POST',url=url,data=body,headers={k:v for k,v in headers.items() if k.lower() in signed})
    request.context['timestamp']=headers['X-Amz-Date']
    signer=SigV4Auth(Credentials(access,secret,headers.get('X-Amz-Security-Token')),service,region)
    canonical=signer.canonical_request(request)
    to_sign=signer.string_to_sign(request,canonical)
    signing=hmac.new(('AWS4'+secret).encode(),day.encode(),hashlib.sha256).digest()
    for part in (region,service,'aws4_request'): signing=hmac.new(signing,part.encode(),hashlib.sha256).digest()
    assert authorization.endswith('Signature='+hmac.new(signing,to_sign.encode(),hashlib.sha256).hexdigest())
    return access,region,service


def test_simultaneous_signed_routes_ignore_environment(monkeypatch):
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','AWS_ENDPOINT_URL','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_PROFILE','AWS_REGION','AWS_DATA_PATH'):
        monkeypatch.setenv(key,'synthetic-unwanted-default')
    seen=[]
    def transport(session,method,url,**kwargs):
        assert session.trust_env is False and kwargs['allow_redirects'] is False
        number=1 if kwargs['proxies']=={} else 2
        access,region,service=verify_signature(url,kwargs['headers'],kwargs['data'],str(number)*40)
        assert access=='AKIA'+str(number)*16
        assert region==('ap-northeast-1' if number==1 else 'us-west-2') and service=='lightsail'
        assert url==f'https://lightsail.{region}.amazonaws.com/'
        assert kwargs['proxies']==({} if number==1 else {'http':'socks5h://proxy:1080','https':'socks5h://proxy:1080'})
        assert kwargs['headers'].get('X-Amz-Security-Token')==('temporary-token' if number==2 else None)
        seen.append((access,region,kwargs['proxies']))
        return response({'instances':[]})
    monkeypatch.setattr(requests.Session,'request',transport)
    def worker(c):
        with aws.Client(c) as a:
            for _ in range(3): assert a.instances()==[]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(worker,config()),executor.submit(worker,config('socks5://proxy:1080','us-west-2',2,'temporary-token'))]
        for f in futures: f.result()
    assert len(seen)==6


@pytest.mark.parametrize('proxy',[None,123,{},'bad://host','http://host:99999','https://host/path'])
def test_invalid_route_never_connects(proxy,monkeypatch):
    call=Mock(); monkeypatch.setattr(requests.Session,'request',call)
    with pytest.raises(aws.AwsError): aws.Client(config(proxy))
    call.assert_not_called()


def test_failure_redirect_and_error_hide_secrets(monkeypatch):
    call=Mock(side_effect=requests.exceptions.ProxyError('secret-auth-proxy'))
    monkeypatch.setattr(requests.Session,'request',call)
    with aws.Client(config('socks5h://user:secret-auth-proxy@host:1080')) as c:
        with pytest.raises(aws.AwsError,match='未回退直连') as error: c.instances()
        assert 'secret-auth-proxy' not in str(error.value) and call.call_count==1
        call.side_effect=None; call.return_value=response({},302)
        with pytest.raises(aws.AwsError,match='重定向'): c.instances()
        call.return_value=response({'__type':'AccessDeniedException','message':'synthetic-secret-key'},400)
        with pytest.raises(aws.AwsError,match='权限不足') as error: c.instances()
        assert 'synthetic-secret-key' not in str(error.value)


def test_account_crud_masks_keys_preserves_oci(client,account):
    import gcp_api
    gcp_api.init_tables()
    store.execute('INSERT INTO gcp_accounts(name,project_id,credentials,proxy_url) VALUES(?,?,?,?)',
        ('existing GCP','existing-project',json.dumps({'client_email':'synthetic@example.invalid'}),'socks5h://gcp-only:1080'))
    gcp_before=store.query('SELECT * FROM gcp_accounts')
    before=store.query('SELECT * FROM accounts')
    a=add_account(client,'socks5h://user:proxy-secret@host:1080')
    assert 'proxy-secret' not in json.dumps(a) and '1'*40 not in json.dumps(a)
    assert a['access_key_hint']=='…1111' and a['proxied']
    assert client.get('/api/accounts').json()['data'][0]['id']==account['id']
    path=f"/api/aws/accounts/{a['id']}"
    edited=client.put(path,json=dict(name='edited',region='us-west-2',revision=a['revision'])).json()
    assert edited['proxied'] and edited['revision']!=a['revision']
    assert client.get(path+'/instances',params={'revision':a['revision']}).status_code==409
    assert client.put(path,json=dict(name='edited',region='us-west-2',revision=edited['revision'],proxy_url='')).status_code==400
    result=client.put(path,json=dict(name='direct',region='us-west-2',revision=edited['revision'],proxy_url='',remove_proxy=True))
    assert result.status_code==200 and result.json()['route']=='直连'
    assert store.query('SELECT * FROM accounts')==before
    assert store.query('SELECT * FROM gcp_accounts')==gcp_before
    assert client.delete(path,params={'revision':result.json()['revision']}).status_code==200


def test_key_rotation_requires_pair_and_token(client):
    a=add_account(client)
    path=f"/api/aws/accounts/{a['id']}"
    base=dict(name='edited',region=a['region'],revision=a['revision'])
    assert client.put(path,json={**base,'access_key_id':'AKIA'+'2'*16}).status_code==400
    assert client.put(path,json={**base,'access_key_id':'ASIA'+'2'*16,'secret_access_key':'2'*40}).status_code==400
    b=client.put(path,json={**base,'access_key_id':'ASIA'+'2'*16,'secret_access_key':'2'*40,'session_token':'test-token'}).json()
    assert b['temporary']
    assert client.put(path,json={**base,'revision':b['revision'],'clear_token':True}).status_code==400
    result=client.put(path,json={**base,'revision':b['revision'],'access_key_id':'AKIA'+'3'*16,'secret_access_key':'3'*40})
    assert result.status_code==200 and not result.json()['temporary']


def test_real_sdk_models_paginate_create_metrics_operations(monkeypatch):
    calls=[]
    def transport(_,method,url,**kwargs):
        operation=kwargs['headers']['X-Amz-Target'].split('.')[-1]
        body=json.loads(kwargs['data']); calls.append((operation,body))
        results={
            'GetRegions':{'regions':[{'name':'ap-northeast-1','availabilityZones':[{'zoneName':'ap-northeast-1a','state':'available'}]}]},
            'GetBlueprints':{'blueprints':[dict(blueprintId='ubuntu_24_04',name='Ubuntu',platform='LINUX_UNIX',isActive=True,minPower=0)]},
            'GetBundles':{'bundles':[dict(bundleId='nano_3_0',name='Nano',power=1,isActive=True,cpuCount=2,ramSizeInGb=.5,diskSizeInGb=20,price=5,supportedPlatforms=['LINUX_UNIX'],publicIpv4AddressCount=1)]},
            'GetKeyPairs':{'keyPairs':[{'name':'LightsailDefaultKeyPair'}]},
            'CreateInstances':{'operations':[{'id':'op-create','status':'Started','isTerminal':False}]},
            'GetOperation':{'operation':{'id':'op-create','status':'Succeeded','isTerminal':True}},
            'GetInstance':{'instance':{'name':'vm-test'}},
            'GetInstanceMetricData':{'metricData':[{'timestamp':datetime.now(timezone.utc).timestamp(), 'sum':123, 'average':50}]}}
        if operation=='GetInstances':
            results[operation]={'instances':[dict(arn='arn:'+body.get('pageToken','first'),name='vm-test',state={'name':'running'},location={'availabilityZone':'ap-northeast-1a'},hardware={'cpuCount':2,'ramSizeInGb':1},ipv6Addresses=['2001:db8::1'])]}
            if not body.get('pageToken'): results[operation]['nextPageToken']='second'
        return response(results[operation])
    monkeypatch.setattr(requests.Session,'request',transport)
    with aws.Client(config()) as c:
        assert len(c.instances())==2
        assert calls[1][1]['pageToken']=='second'
        body=dict(name='vm-test',zone='ap-northeast-1a',blueprint_id='ubuntu_24_04',bundle_id='nano_3_0',key_name='LightsailDefaultKeyPair',ip_type='dualstack')
        assert c.create(body)['operations'][0]['id']=='op-create'
        assert calls[-1][1]==dict(instanceNames=['vm-test'],availabilityZone='ap-northeast-1a',blueprintId='ubuntu_24_04',bundleId='nano_3_0',keyPairName='LightsailDefaultKeyPair',ipAddressType='dualstack')
        assert c.operation('op-create')['terminal']
        data=c.metrics('vm-test',90)
        assert data['rx_bytes']==123 and data['tx_bytes']==123 and data['rows'][0]['cpu']==50
        assert all(b['period']==86400 and b['instanceName']=='vm-test' for n,b in calls if n=='GetInstanceMetricData')
        with pytest.raises(aws.AwsError): c.create({**body,'zone':'us-west-2a'})


def test_ipv6_bundle_validation_and_failed_operation(monkeypatch):
    with aws.Client(config()) as c:
        options=dict(zones=['ap-northeast-1a'],blueprints=[dict(id='ubuntu',min_power=0)],bundles=[dict(id='nano',power=1,ipv4_count=0)],key_pairs=['key'])
        monkeypatch.setattr(c,'options',lambda:options)
        call=Mock(return_value={'operations':[]}); monkeypatch.setattr(c,'call',call)
        body=dict(name='vm-test',zone='ap-northeast-1a',blueprint_id='ubuntu',bundle_id='nano',key_name='key',ip_type='dualstack')
        with pytest.raises(aws.AwsError,match='不含公网 IPv4'): c.create(body)
        call.assert_not_called()
        c.create({**body,'ip_type':'ipv6'})
        assert call.call_args.kwargs['ipAddressType']=='ipv6'
        call.return_value={'operation':dict(id='op',status='Failed',isTerminal=True,errorCode='NoQuota',errorDetails='sensitive-error')}
        with pytest.raises(aws.AwsError,match='NoQuota') as error: c.operation('op')
        assert 'sensitive-error' not in str(error.value)


def test_sync_aws_id_region_instance_not_host_preserves_other_accounts(client,monkeypatch):
    sid=store.execute('INSERT INTO ssh_sessions(name,host,secret,proxy_command) VALUES(?,?,?,?)',('existing OCI','203.0.113.1','old-key',''))
    before=store.query('SELECT * FROM ssh_sessions WHERE id=?',(sid,))[0]
    a=add_account(client); b=add_account(client,'socks5h://api-proxy:1080',number=2)
    rows=[dict(id='arn:aws:lightsail:ap-northeast-1:111111111111:Instance/vm',name='vm',status='running',public_ip='203.0.113.1',private_ip='10.0.0.1',ipv6=[],zone='ap-northeast-1a',machine_type='nano',username='bitnami')]
    cloud={'id':'111111111111'}
    monkeypatch.setattr(aws.Client,'identity',lambda _:{'account_id':cloud['id']})
    monkeypatch.setattr(aws.Client,'instances',lambda _:rows)
    def sync(a,proxy=''):
        return client.post(f"/api/aws/accounts/{a['id']}/sync-ssh",json=dict(revision=a['revision'],ssh_proxy=proxy))
    assert sync(a).json()['added']==1 and sync(b,'socks5h://ssh-proxy:1080').json()['added']==1
    assert sync(b,'socks5h://changed:1080').json()['updated']==1
    sessions=store.query('SELECT * FROM ssh_sessions ORDER BY id')
    assert len(sessions)==3 and sessions[0]==before
    assert sessions[1]['proxy_command']=='' and sessions[2]['proxy_command']=='socks5h://ssh-proxy:1080'
    assert sessions[1]['username']=='bitnami'
    cloud['id']='222222222222'
    assert sync(a).json()['added']==1 # Rotated cloud identity cannot overwrite old host/secret/route.
    rows[0]['public_ip']='203.0.113.2'
    assert sync(a).json()['skipped']==1
    assert store.query('SELECT host FROM ssh_sessions ORDER BY id')[-1]['host']=='203.0.113.1'
    assert sync(b,'http://wrong:1080').status_code==400


def test_sync_stale_revision_after_fetch_blocks_writes(client,monkeypatch):
    a=add_account(client)
    monkeypatch.setattr(aws.Client,'identity',lambda _:{'account_id':'111111111111'})
    def fetch(_):
        store.execute('UPDATE aws_accounts SET proxy_url=? WHERE id=?',('socks5h://changed:1080',a['id']))
        return []
    monkeypatch.setattr(aws.Client,'instances',fetch)
    result=client.post(f"/api/aws/accounts/{a['id']}/sync-ssh",json={'revision':a['revision']})
    assert result.status_code==409 and not store.query('SELECT * FROM aws_ssh_links')


def test_parallel_sync_does_not_duplicate_or_replace_session(client,monkeypatch):
    import threading
    a=add_account(client)
    barrier=threading.Barrier(2)
    monkeypatch.setattr(aws.Client,'identity',lambda _:{'account_id':'111111111111'})
    def instances(_):
        barrier.wait(timeout=5)
        return [dict(id='arn:aws:lightsail:vm',name='vm',status='running',public_ip='203.0.113.1',private_ip='',ipv6=[],zone='ap-northeast-1a',machine_type='nano',username='ubuntu')]
    monkeypatch.setattr(aws.Client,'instances',instances)
    body=aws_api.SyncBody(revision=a['revision'],ssh_proxy='socks5h://ssh-only:1080')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(aws_api.sync_ssh,a['id'],body) for _ in range(2)]
        results=[f.result(timeout=10) for f in futures]
    assert sum(r['added'] for r in results)==1 and sum(r['updated'] for r in results)==1
    assert len(store.query('SELECT * FROM ssh_sessions'))==1
    assert len(store.query('SELECT * FROM aws_ssh_links'))==1


def test_aws_endpoints_require_login(client,monkeypatch):
    call=Mock(); monkeypatch.setattr(requests.Session,'request',call)
    assert client.post('/api/logout').status_code==200
    for path in ('/api/aws/accounts','/api/aws/regions','/api/aws/accounts/1/instances?revision=abc'):
        assert client.get(path).status_code==401
    assert client.post('/api/aws/accounts',json=dict(name='invalid',region='us-west-2')).status_code==401
    call.assert_not_called()


def test_destructive_confirmations_and_route_revision(client,monkeypatch):
    a=add_account(client); call=Mock(); monkeypatch.setattr(aws.Client,'action',call)
    path=f"/api/aws/accounts/{a['id']}"
    body=dict(revision=a['revision'],name='vm-test',action='delete')
    assert client.post(path+'/action',json=body).status_code==400
    assert client.post(path+'/action',json={**body,'revision':'stale','confirmed_name':'vm-test'}).status_code==409
    call.assert_not_called()
    call.return_value={'operations':[]}
    assert client.post(path+'/action',json={**body,'confirmed_name':'vm-test'}).status_code==200
    assert client.post(path+'/snapshots',json=dict(revision=a['revision'],name='snap-test',action='delete')).status_code==400
    assert client.post(path+'/static-ips',json=dict(revision=a['revision'],name='ip-test',action='release')).status_code==400


def test_static_ip_does_not_steal_or_release_attached_ip(monkeypatch):
    with aws.Client(config()) as c:
        call=Mock(return_value={'staticIp':{'name':'ip-test','isAttached':True,'attachedTo':'other-vm'}})
        monkeypatch.setattr(c,'call',call)
        with pytest.raises(aws.AwsError,match='其他实例'): c.static_ip_action('ip-test','attach','my-vm')
        with pytest.raises(aws.AwsError,match='先解绑'): c.static_ip_action('ip-test','release')
        assert all(r.args[1]=='GetStaticIp' for r in call.call_args_list)


def test_real_https_socks_remote_dns_and_fail_closed(socks_proxy,tmp_path,monkeypatch):
    import ipaddress
    import ssl
    import threading
    from datetime import timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    proxy,destinations,_=socks_proxy
    seen=[]; errors=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body=self.rfile.read(int(self.headers['Content-Length']))
                headers=requests.structures.CaseInsensitiveDict(self.headers.items())
                access=headers['Authorization'].split('Credential=')[1].split('/')[0]
                number=int(access[-1])
                verify_signature('https://'+self.headers['Host']+self.path,headers,body,str(number)*40)
                seen.append(access)
                self.send_response(200); self.send_header('Content-Type','application/x-amz-json-1.1'); self.end_headers()
                self.wfile.write(b'{"instances":[]}')
            except Exception as error:
                errors.append(str(error)); self.send_error(500)
        def log_message(self,*_): pass
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'AWS local test')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1')),x509.DNSName('aws.example.invalid')]),critical=False)
        .sign(key,hashes.SHA256()))
    certpath=tmp_path/'ca.pem'; keypath=tmp_path/'server.pem'
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(str(certpath),str(keypath))
    server.socket=tls.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    original=aws.AWSRequest
    def local_request(**kwargs):
        # Redirect only this synthetic test's endpoint, before signing. Production has no endpoint override.
        hostname='127.0.0.1' if 'ap-northeast-1' in kwargs['url'] else 'aws.example.invalid'
        kwargs['url']=f'https://{hostname}:{server.server_port}/'
        return original(**kwargs)
    monkeypatch.setattr(aws,'AWSRequest',local_request)
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1'); monkeypatch.setenv('ALL_PROXY','socks5h://127.0.0.1:1'); monkeypatch.setenv('NO_PROXY','*')
    try:
        with aws.Client(config()) as direct,aws.Client(config(proxy,'us-west-2',2)) as proxied:
            direct.session.verify=proxied.session.verify=str(certpath)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(c.instances) for c in (direct,proxied)]
                for future in futures: assert future.result(timeout=10)==[]
            assert not errors,errors
            assert destinations==[('aws.example.invalid',server.server_port)]
            count=len(seen)
            with aws.Client(config('socks5h://127.0.0.1:1','us-west-2',2)) as dead:
                dead.session.verify=str(certpath)
                with pytest.raises(aws.AwsError,match='未回退直连'): dead.instances()
            assert len(seen)==count
            assert direct.instances()==[] and len(seen)==count+1
    finally:
        server.shutdown(); server.server_close(); thread.join(3)

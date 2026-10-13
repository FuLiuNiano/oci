from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

import aws_ec2
import aws_service
from test_aws import config, response, add_account


def test_ec2_real_signatures_keep_credentials_region_and_proxy_separate(monkeypatch):
    seen=[]
    def transport(session,method,url,**kwargs):
        authorization=kwargs['headers']['Authorization']
        number=1 if 'AKIA'+'1'*16 in authorization else 2
        expected_region='ap-northeast-1' if number==1 else 'us-west-2'
        assert url==f'https://ec2.{expected_region}.amazonaws.com/'
        assert f'/{expected_region}/ec2/aws4_request' in authorization
        assert kwargs['proxies']==({} if number==1 else {'http':'socks5h://proxy:1080','https':'socks5h://proxy:1080'})
        assert not session.trust_env and not kwargs['allow_redirects']
        assert 'Action=DescribeInstances' in kwargs['data']
        seen.append(number)
        r=requests.Response();r.status_code=200
        r._content=b'<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"><reservationSet/></DescribeInstancesResponse>'
        r.headers['Content-Type']='text/xml';return r
    monkeypatch.setattr(requests.Session,'request',transport)
    def work(a):
        with aws_ec2.Client(a) as client:assert client.instances()==[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work,[config(),config('socks5://proxy:1080','us-west-2',2)]))
    assert sorted(seen)==[1,2]


def test_cost_query_uses_fixed_billing_endpoint_own_route_and_pagination(monkeypatch):
    calls=[]
    def transport(session,method,url,**kwargs):
        assert url=='https://ce.us-east-1.amazonaws.com/'
        assert '/us-east-1/ce/aws4_request' in kwargs['headers']['Authorization']
        assert kwargs['proxies']=={'http':'socks5h://proxy:1080','https':'socks5h://proxy:1080'}
        calls.append(kwargs)
        data={'ResultsByTime':[]}
        if len(calls)==1:data['NextPageToken']='next'
        return response(data)
    monkeypatch.setattr(requests.Session,'request',transport)
    with aws_ec2.Client(config('socks5://proxy:1080','us-west-2',2)) as client:
        result=client.costs(90)
    assert len(calls)==2 and result['days']==90 and '全部' in result['scope']


def ec2_client():
    return aws_ec2.Client(config())


def create_body():
    return {'client_token':'12345678-1234-1234-1234-123456789abc','name':'test','image':'ami-12345678','instance_type':'t3.micro','subnet':'subnet-12345678',
            'groups':['sg-12345678'],'key_name':'my-key','public_ipv4':False}


def test_create_validates_vpc_and_image_before_mutation(monkeypatch):
    client=ec2_client()
    client.options=lambda:{'subnets':[{'id':'subnet-12345678','vpc':'vpc-own'}],
                           'groups':[{'id':'sg-12345678','vpc':'vpc-foreign'}],'keys':['my-key']}
    op=Mock();client.call=op
    with pytest.raises(aws_ec2.AwsError,match='VPC'):client.create(create_body())
    op.assert_not_called();client.session.close()


def test_create_uses_one_instance_token_and_explicit_network():
    client=ec2_client()
    client.options=lambda:{'subnets':[{'id':'subnet-12345678','vpc':'vpc-own'}],
                           'groups':[{'id':'sg-12345678','vpc':'vpc-own'}],'keys':['my-key']}
    observed=[]
    def call(service,operation,**params):
        observed.append((service,operation,params))
        return {'DescribeImages':{'Images':[{'State':'available','Architecture':'x86_64'}]},
                'DescribeInstanceTypes':{'InstanceTypes':[{'ProcessorInfo':{'SupportedArchitectures':['x86_64']}}]},
                'RunInstances':{'Instances':[{'InstanceId':'i-12345678'}]}}[operation]
    client.call=call
    assert client.create(create_body())['instance_ids']==['i-12345678']
    request=observed[-1][2]
    assert request['MinCount']==request['MaxCount']==1
    assert request['ClientToken'] and not request['NetworkInterfaces'][0]['AssociatePublicIpAddress']
    assert request['ClientToken']==create_body()['client_token']
    assert request['MetadataOptions']['HttpTokens']=='required'
    assert 'IamInstanceProfile' not in request
    client.session.close()


def test_ec2_api_uses_aws_revision_and_requires_confirmation(client,monkeypatch):
    a=add_account(client)
    method=Mock(return_value={'message':'ok'});monkeypatch.setattr(aws_ec2.Client,'action',method)
    base=f"/api/tools/aws/{a['id']}/ec2-action"
    body={'revision':'stale','instance_id':'i-12345678','action':'stop','confirm':True}
    assert client.post(base,json=body).status_code==409
    body.update(revision=a['revision'],confirm=False)
    assert client.post(base,json=body).status_code==400
    method.assert_not_called()


def test_ec2_sync_two_accounts_same_ip_keep_sessions_and_routes_separate(client,monkeypatch):
    import json,store
    first=add_account(client,number=1)
    second=add_account(client,proxy='socks5h://cloud-proxy:1080',number=2)
    class Fake:
        def __init__(self,account):self.account=account
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def identity(self):return {'account_id':str(self.account['id'])}
        def instances(self):return [{'id':'i-12345678','name':'same','status':'running','zone':'ap-northeast-1a',
            'machine_type':'t3.micro','public_ip':'203.0.113.2','private_ip':'10.0.0.2','ipv6':[]}]
    monkeypatch.setattr(aws_ec2,'Client',Fake)
    for a,proxy in ((first,''),(second,'socks5h://ssh-proxy:1081')):
        r=client.post(f"/api/tools/aws/{a['id']}/ec2-sync",json={'revision':a['revision'],'username':'ubuntu','ssh_proxy':proxy})
        assert r.status_code==200 and r.json()['added']==1
    rows=store.query('SELECT * FROM ssh_sessions ORDER BY id')
    assert len(rows)==2 and rows[0]['proxy_command']=='' and rows[1]['proxy_command']=='socks5h://ssh-proxy:1081'
    assert json.loads(rows[0]['metadata'])['account_id']==first['id']
    assert json.loads(rows[1]['metadata'])['account_id']==second['id']
    store.execute("UPDATE ssh_sessions SET secret='synthetic-key',proxy_command='socks5h://preserved:1082' WHERE id=?",(rows[0]['id'],))
    before=store.query('SELECT * FROM ssh_sessions ORDER BY id')
    r=client.post(f"/api/tools/aws/{first['id']}/ec2-sync",json={'revision':first['revision'],'username':'root','ssh_proxy':''})
    assert r.status_code==200 and r.json()['updated']==1
    after=store.query('SELECT * FROM ssh_sessions ORDER BY id')
    assert before==after

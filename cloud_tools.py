"""Authenticated cloud tools; all requests use an explicitly selected account."""
import hashlib
import hmac
import ipaddress
import json
import secrets
from typing import Literal

import oci
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import aws_api
import aws_ec2
import gcp_api
import gcp_service
import oci_service as oracle
import store
from deps import require_auth

api = APIRouter(prefix='/api/tools', dependencies=[Depends(require_auth)])
_key = secrets.token_bytes(32)


def fingerprint(account):
    return hashlib.sha256(json.dumps([account['id'], account['region'], store.account_params(account)], sort_keys=True).encode()).hexdigest()


def oracle_revision(account):
    return hmac.new(_key, fingerprint(account).encode(), hashlib.sha256).hexdigest()


def selected(platform, aid, revision):
    if platform == 'aws':
        return aws_api.get_account(aid, revision)
    if platform == 'gcp':
        return gcp_api.get_account(aid, revision)
    rows = store.query("SELECT * FROM accounts WHERE platform='oci' AND id=?", (aid,))
    if not rows:
        raise HTTPException(404,'OCI 账号不存在')
    account = rows[0]
    if store.account_disabled('oci', aid):
        raise HTTPException(409,'该 OCI 配置已停用，请先启用')
    if not hmac.compare_digest(revision, oracle_revision(account)):
        raise HTTPException(409,'OCI 配置或线路已变化，请刷新后重试')
    return account


@api.get('/accounts/{platform}')
def accounts(platform: Literal['oci','aws','gcp']):
    if platform=='oci':
        rows=[{**store.account_public(a),'revision':oracle_revision(a)} for a in store.query("SELECT * FROM accounts WHERE platform='oci'")]
    else:
        module=aws_api if platform=='aws' else gcp_api
        rows=module.accounts()
    return [{**a,'enabled':not store.account_disabled(platform,a['id'])} for a in rows]


class Toggle(BaseModel):
    revision: str
    enabled: bool


@api.post('/accounts/{platform}/{aid}/enabled')
def toggle(platform: Literal['oci','aws','gcp'], aid:int, body:Toggle):
    # Validate configuration even while disabled, without calling cloud APIs.
    table={'oci':'accounts','aws':'aws_accounts','gcp':'gcp_accounts'}[platform]
    if platform!='oci':
        (aws_api if platform=='aws' else gcp_api).init_tables()
    rows=store.query(f'SELECT * FROM {table} WHERE id=?',(aid,))
    if not rows or (platform=='oci' and rows[0]['platform']!='oci'):
        raise HTTPException(404,'配置不存在')
    version=oracle_revision(rows[0]) if platform=='oci' else (aws_api if platform=='aws' else gcp_api).revision(rows[0])
    if not hmac.compare_digest(body.revision, version):
        raise HTTPException(409,'配置已变化，请刷新后重试')
    store.set_json_setting(f'account_disabled:{platform}:{aid}', not body.enabled)
    return {'enabled':body.enabled,'message':'仅更改本地配置启用状态，不删除云资源或 SSH 会话'}


def gcp_summary(account):
    with gcp_service.Client(account) as client:
        instances=client.instances()
        project=client.call('GET','')
        result={'project_id':client.project,'overview':gcp_service.overview(instances),
                'quotas':project.get('quotas',[])}
        try:
            billing=client._send('GET', f'https://cloudbilling.googleapis.com/v1/projects/{client.project}/billingInfo',
                headers={'Authorization':'Bearer '+client._access_token()})
            result['billing']={'enabled':billing.get('billingEnabled',False),'account':billing.get('billingAccountName','')}
        except gcp_service.GcpError as e:
            result['billing']={'error':str(e)}
        return result


def oracle_query(account, operation, days):
    if operation=='traffic':
        return {**oracle.traffic_usage_gb(account,hours=days*24),'days':days,
                'scope':'当前区域仍挂载网卡的出站流量，包含内网，不等同于计费流量'}
    if operation=='costs':
        return {**oracle.usage_cost(account,days),'scope':'所选 OCI 租户的全部区域（按用量报告权限可见范围）'}
    if operation=='quotas':
        return oracle.stats(account)
    if operation=='regions':
        identity=oracle._client(oci.identity.IdentityClient,account)
        return {'subscribed':oracle.subscribed_regions(account),
                'available':[{'name':r.name,'key':r.key} for r in oracle._all(identity.list_regions)]}
    if operation=='subscription':
        client=oracle._client(oci.tenant_manager_control_plane.SubscriptionClient,account)
        return oci.util.to_dict(oracle._all(client.list_subscriptions,compartment_id=oracle.build_config(account)['tenancy']))
    if operation=='users':
        return oracle.list_users(account)
    if operation=='current-key':
        p=store.account_params(account)
        return {'user_id':p['user_ocid'],'fingerprint':p['fingerprint'],
                'keys':oracle.list_api_keys(account,p['user_ocid'])}
    if operation=='work-requests':
        client=oracle._client(oci.work_requests.WorkRequestClient,account)
        return oci.util.to_dict(oracle._all(client.list_work_requests,oracle.compartment_of(account)))
    if operation=='tasks':
        return store.query('SELECT id,display_name,shape,status,attempts,last_error,updated_at FROM launch_tasks WHERE account_id=? ORDER BY id DESC',(account['id'],))
    if operation=='automation':
        import cloud_automation
        return cloud_automation.public_config(account)
    raise HTTPException(400,'不支持的查询')


@api.get('/{platform}/{aid}/{operation}')
def query(platform:Literal['oci','aws','gcp'],aid:int,operation:str,revision:str,days:int=Query(90,ge=1,le=90),resource:str=''):
    account=selected(platform,aid,revision)
    try:
        if platform=='oci':
            if operation=='work-errors':
                client=oracle._client(oci.work_requests.WorkRequestClient,account)
                request=client.get_work_request(resource).data
                if request.compartment_id!=oracle.compartment_of(account):
                    raise HTTPException(400,'工作请求不属于当前配置 compartment')
                return oci.util.to_dict(oracle._all(client.list_work_request_errors,resource))
            return oracle_query(account,operation,days)
        if platform=='gcp':
            if operation=='overview': return gcp_summary(account)
            if operation=='operations':
                with gcp_service.Client(account) as client:
                    return [op for p in client.pages('aggregated/operations') for scope in p.get('items',{}).values() for op in scope.get('operations',[])]
            if operation=='traffic': return gcp_api.run(account,'traffic',days)
        if platform=='aws':
            with aws_ec2.Client(account) as client:
                if operation=='costs': return client.costs(days)
                if operation=='quotas': return client.quotas(resource or 'ec2')
                if operation=='identity': return client.identity()
                if operation=='ec2-instances': return client.instances()
                if operation=='ec2-options': return client.options()
                if operation=='ec2-metrics': return client.metrics(resource,days)
                if operation=='operations': return list(client.pages('GetOperations','operations'))
        raise HTTPException(400,'不支持的查询')
    except (oracle.OciError, oracle.OciTransportError, oci.exceptions.ServiceError) as e:
        raise HTTPException(400,oracle.fmt_err(e)) from None
    except (aws_ec2.AwsError,gcp_service.GcpError) as e:
        raise HTTPException(400,str(e)) from None


class Mutation(BaseModel):
    revision:str
    instance_id:str=''
    region:str=''
    source:str=''
    port:int=Field(default=22,ge=1,le=65535)
    user_ids:list[str]=Field(default_factory=list,max_length=100)
    confirm:bool=False


def open_port(account, instance_id, source, port):
    try:
        source=str(ipaddress.ip_network(source,strict=False))
    except ValueError:
        raise oracle.OciError('请输入明确的来源 IPv4/IPv6 CIDR') from None
    net=oracle._client(oci.core.VirtualNetworkClient,account)
    vnic=oracle._primary_vnic(net,oracle.compartment_of(account),instance_id,oracle._client(oci.core.ComputeClient,account))
    subnet=net.get_subnet(vnic.subnet_id).data
    if not subnet.security_list_ids:
        raise oracle.OciError('子网没有安全列表')
    response=net.get_security_list(subnet.security_list_ids[0])
    security=response.data
    if security.vcn_id!=subnet.vcn_id or not response.headers.get('etag'):
        raise oracle.OciError('安全列表身份或版本无法确认，未修改')
    rule=oci.core.models.IngressSecurityRule(source=source,source_type='CIDR_BLOCK',protocol='6',is_stateless=False,
        tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=port,max=port)))
    # Preserve all prior rules; suppress a repeat of the same explicit source/port.
    old=list(security.ingress_security_rules or [])
    same=lambda r:r.protocol=='6' and r.source==source and not r.is_stateless and r.tcp_options and r.tcp_options.destination_port_range and r.tcp_options.destination_port_range.min==port and r.tcp_options.destination_port_range.max==port
    if any(same(r) for r in old): return {'message':'对应规则已存在，未重复添加'}
    net.update_security_list(security.id,oci.core.models.UpdateSecurityListDetails(ingress_security_rules=old+[rule],
        egress_security_rules=security.egress_security_rules),if_match=response.headers['etag'])
    return {'message':'已追加子网共享安全列表规则；同一子网其他实例也受影响，系统防火墙仍需单独配置'}


@api.post('/oci/{aid}/actions/{operation}')
def oracle_mutation(aid:int,operation:str,body:Mutation):
    account=selected('oci',aid,body.revision)
    if not body.confirm: raise HTTPException(400,'请确认操作后提交')
    try:
        if operation=='open-port': return open_port(account,body.instance_id,body.source,body.port)
        if operation=='subscribe-region':
            identity=oracle._client(oci.identity.IdentityClient,account)
            region=next((r for r in oracle._all(identity.list_regions) if r.name==body.region),None)
            if region is None:
                raise oracle.OciError('区域无效')
            if body.region in {r['region'] for r in oracle.subscribed_regions(account)}:
                return {'message':'区域已订阅，未重复提交'}
            identity.create_region_subscription(oci.identity.models.CreateRegionSubscriptionDetails(region_key=region.key),
                oracle.build_config(account)['tenancy'])
            return {'message':'订阅请求已提交；实际可用区域由订阅权限决定，现有 profile 与代理保持不变'}
        if operation=='clear-mfa':
            users={u['id'] for u in oracle.list_users(account)}
            if not body.user_ids or any(uid not in users for uid in body.user_ids):
                raise oracle.OciError('请选择当前账号的用户，已阻止跨账号操作')
            results=[]
            for uid in dict.fromkeys(body.user_ids):
                try: results.append({'id':uid,**oracle.clear_user_mfa(account,uid)})
                except Exception as e: results.append({'id':uid,'error':oracle.fmt_err(e)})
            return results
        if operation=='delete-current-key':
            p=store.account_params(account)
            result=oracle.delete_api_key(account,p['user_ocid'],p['fingerprint'])
            store.set_json_setting(f"account_disabled:oci:{aid}",True)
            return {**result,'message':'当前 API 密钥已删除，本地配置已停用；恢复需提供有效密钥'}
        raise HTTPException(400,'不支持的操作')
    except (oracle.OciError,oci.exceptions.ServiceError,oracle.OciTransportError) as e:
        raise HTTPException(400,oracle.fmt_err(e)) from None


class Ec2Create(BaseModel):
    revision:str
    client_token:str=Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    name:str=Field(min_length=1,max_length=100)
    image:str=Field(pattern=r'^ami-[a-f0-9]{8,32}$')
    instance_type:str=Field(pattern=r'^[a-z0-9]+\.[a-z0-9]+$')
    subnet:str
    groups:list[str]=Field(min_length=1,max_length=5)
    key_name:str
    public_ipv4:bool=False
    confirm:bool=False


class Automation(BaseModel):
    revision:str
    notify_changes:bool=False
    daily_report:bool=False
    auto_ip:bool=False
    expand_regions:bool=False
    instance_id:str=''
    session_id:int|None=None
    confirm:bool=False


@api.post('/oci/{aid}/automation')
def automation(aid:int,body:Automation):
    import cloud_automation
    account=selected('oci',aid,body.revision)
    if not body.confirm: raise HTTPException(400,'请确认自动化设置')
    cfg=body.model_dump(exclude={'revision','confirm'})
    previous=store.get_json_setting(f'cloud_automation:{aid}',{}) or {}
    cfg['last_change']=previous.get('last_change',0)
    if body.auto_ip:
        links=store.query('SELECT session_id FROM oci_ssh_links WHERE account_id=? AND region=? AND instance_id=?',
            (aid,account['region'],body.instance_id))
        rows=store.query('SELECT * FROM ssh_sessions WHERE id=?',(body.session_id,))
        if not rows or not links or links[0]['session_id']!=body.session_id:
            raise HTTPException(400,'自动换 IP 只能使用已关联当前账号、区域和实例的 SSH 会话')
        if rows[0]['proxy_command']:
            raise HTTPException(400,'SSH 代理超时无法区分代理与目标故障，不启用自动换 IP；可使用状态通知和手动换 IP')
        cfg['session_fingerprint']=cloud_automation.session_fingerprint(rows[0])
    cfg.update(account_fingerprint=fingerprint(account),version=secrets.token_hex(16),failures=0,last_result='设置已保存')
    store.set_json_setting(f'cloud_automation:{aid}',cfg)
    return cloud_automation.public_config(account)


@api.post('/aws/{aid}/ec2-create')
def ec2_create(aid:int,body:Ec2Create):
    account=selected('aws',aid,body.revision)
    if not body.confirm: raise HTTPException(400,'请确认创建可能产生费用')
    try:
        with aws_ec2.Client(account) as client: return client.create(body.model_dump())
    except aws_ec2.AwsError as e: raise HTTPException(400,str(e)) from None


class Ec2Action(BaseModel):
    revision:str
    instance_id:str
    action:Literal['start','stop','reboot','delete']
    confirm:bool=False


@api.post('/aws/{aid}/ec2-action')
def ec2_action(aid:int,body:Ec2Action):
    account=selected('aws',aid,body.revision)
    if not body.confirm: raise HTTPException(400,'请确认实例操作')
    try:
        with aws_ec2.Client(account) as client: return client.action(body.instance_id,body.action)
    except aws_ec2.AwsError as e: raise HTTPException(400,str(e)) from None


@api.post('/aws/{aid}/ec2-sync')
def ec2_sync(aid:int,body:aws_api.SyncBody):
    import sshpool
    account=selected('aws',aid,body.revision)
    try: sshpool._socks_proxy({'proxy_command':body.ssh_proxy})
    except Exception: raise HTTPException(400,'SSH 代理格式无效，未回退直连') from None
    try:
        with aws_ec2.Client(account) as client:
            cloud_id=client.identity()['account_id']
            rows=client.instances()
    except aws_ec2.AwsError as e: raise HTTPException(400,str(e)) from None
    with aws_api._config_lock:
        selected('aws',aid,body.revision)
        added=updated=skipped=0
        for instance in rows:
            host=instance['public_ip'] or next(iter(instance['ipv6']),'') or (instance['private_ip'] if body.allow_private else '')
            if not host or instance['status'] not in ('running','pending'):
                skipped+=1
                continue
            key=(aid,cloud_id,account['region'],instance['id'])
            links=store.query('SELECT session_id FROM aws_ssh_links WHERE account_id=? AND cloud_account_id=? AND region=? AND instance_id=?',key)
            session=store.query('SELECT * FROM ssh_sessions WHERE id=?',(links[0]['session_id'],)) if links else []
            metadata=json.dumps({'source':'aws','service':'ec2','account_id':aid,'cloud_account_id':cloud_id,
                'region':account['region'],'instance_id':instance['id'],'zone':instance['zone'],
                'machine_type':instance['machine_type'],'shape':instance['machine_type'],'state':instance['status'],
                'ipv6_addresses':instance['ipv6']})
            if session:
                store.execute('UPDATE ssh_sessions SET metadata=? WHERE id=?',(metadata,session[0]['id']))
                updated+=1
            else:
                sid=store.execute('INSERT INTO ssh_sessions(name,host,username,auth_type,proxy_command,tags,metadata) VALUES(?,?,?,?,?,?,?)',
                    (f"{account['name']} / {instance['name']}",host,body.username or 'ubuntu','key',body.ssh_proxy,'aws:'+account['name'],metadata))
                store.execute('INSERT INTO aws_ssh_links(account_id,cloud_account_id,region,instance_id,session_id) VALUES(?,?,?,?,?)',(*key,sid))
                added+=1
        return {'added':added,'updated':updated,'skipped':skipped,'message':'新会话请填写对应镜像用户名与私钥；已有 SSH 主机地址、认证和线路保留'}


class Maintenance(BaseModel):
    expected_revision:str
    operation:Literal['load','clear-cache','memory-start','memory-stop']
    confirm:bool=False


@api.post('/ssh/{sid}/maintenance')
def maintenance(sid:int,body:Maintenance):
    import webapi, sshpool, server_tools
    session=webapi._ssh_or_404(sid,body.expected_revision)
    if body.operation!='load' and not body.confirm:
        raise HTTPException(400,'请确认服务器维护操作')
    try:
        output=sshpool._run(sshpool._run_one(session,server_tools.command(body.operation),timeout=20))
        return {'output':output,'route':'代理' if session['proxy_command'] else '直连','session':session['name']}
    except Exception as e:
        raise HTTPException(400,str(e)) from None

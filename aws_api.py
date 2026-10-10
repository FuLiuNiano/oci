"""Independent AWS account storage and authenticated Lightsail routes."""
import hashlib
import hmac
import json
import secrets
import threading
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import aws_service as aws
from deps import require_auth
import sshpool
import store

api = APIRouter(prefix='/api/aws', dependencies=[Depends(require_auth)])
_revision_key = secrets.token_bytes(32)
_config_lock = threading.RLock()


def init_tables():
    store.execute("""CREATE TABLE IF NOT EXISTS aws_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, region TEXT NOT NULL,
        credentials TEXT NOT NULL, proxy_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
    store.execute("""CREATE TABLE IF NOT EXISTS aws_ssh_links (
        account_id INTEGER NOT NULL, cloud_account_id TEXT NOT NULL, region TEXT NOT NULL,
        instance_id TEXT NOT NULL, session_id INTEGER NOT NULL,
        PRIMARY KEY(account_id,cloud_account_id,region,instance_id))""")


def revision(account):
    raw = json.dumps([account[k] for k in ('region','credentials','proxy_url')]).encode()
    return hmac.new(_revision_key,raw,hashlib.sha256).hexdigest()


def public(account):
    result = {k:account[k] for k in ('id','name','region','created_at')}
    credentials = json.loads(account['credentials'])
    proxy = urlsplit(account['proxy_url']) if account['proxy_url'] else None
    result.update(revision=revision(account), access_key_hint='…'+credentials['access_key_id'][-4:],
        temporary=bool(credentials.get('session_token')), proxied=bool(proxy),
        route=f'代理 · {proxy.scheme}://{proxy.hostname}' + (f':{proxy.port}' if proxy.port else '') if proxy else '直连')
    return result


def get_account(aid, expected=None):
    init_tables()
    rows = store.query('SELECT * FROM aws_accounts WHERE id=?',(aid,))
    if not rows: raise HTTPException(404,'AWS 账号不存在')
    account = rows[0]
    if expected is not None and not hmac.compare_digest(expected,revision(account)):
        raise HTTPException(409,'AWS 凭据、区域或线路已改变，请刷新账号后重试')
    return account


def run(account, method, *args):
    try:
        with aws.Client(account) as client: return getattr(client,method)(*args)
    except aws.AwsError as error:
        raise HTTPException(502,str(error)) from None


class AccountBody(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    region: str = Field(min_length=1,max_length=50)
    access_key_id: str | None = Field(default=None,max_length=128)
    secret_access_key: str | None = Field(default=None,max_length=128)
    session_token: str | None = Field(default=None,max_length=16384)
    clear_token: bool = False
    proxy_url: str | None = Field(default=None,max_length=2048)
    remove_proxy: bool = False
    revision: str | None = None


def save_values(body, existing=None):
    if not body.name.strip(): raise HTTPException(400,'账号名称不能为空')
    credentials = json.loads(existing['credentials']) if existing else {}
    for key in ('access_key_id','secret_access_key','session_token'):
        value = getattr(body,key)
        if value is not None: credentials[key] = value
    if body.clear_token:
        if body.session_token: raise HTTPException(400,'清除临时令牌时请留空令牌字段')
        credentials['session_token'] = ''
    # A credential rotation must provide the whole pair; never combine two users' keys.
    rotating = body.access_key_id is not None or body.secret_access_key is not None
    if rotating and (not body.access_key_id or not body.secret_access_key):
        raise HTTPException(400,'更换凭据需要同时填写 Access Key 和 Secret Key')
    if rotating and existing and credentials['access_key_id'] != json.loads(existing['credentials'])['access_key_id']:
        credentials['session_token'] = body.session_token or ''
    proxy = body.proxy_url if body.proxy_url is not None else (existing['proxy_url'] if existing else '')
    if existing and existing['proxy_url'] and not proxy and not body.remove_proxy:
        raise HTTPException(400,'移除 AWS 代理需要明确勾选改为直连')
    try: aws.validate(body.region,credentials,proxy)
    except aws.AwsError as error: raise HTTPException(400,str(error)) from None
    return (body.name.strip(),body.region,json.dumps(credentials),proxy)


@api.get('/regions')
def regions(): return aws.regions()


@api.get('/accounts')
def accounts():
    init_tables()
    return [public(a) for a in store.query('SELECT * FROM aws_accounts ORDER BY id')]


@api.post('/accounts')
def add(body: AccountBody):
    init_tables()
    aid = store.execute('INSERT INTO aws_accounts(name,region,credentials,proxy_url) VALUES(?,?,?,?)',save_values(body))
    return public(get_account(aid))


@api.put('/accounts/{aid}')
def update(aid: int, body: AccountBody):
    with _config_lock:
        if not body.revision: raise HTTPException(409,'请刷新 AWS 账号后编辑')
        values = save_values(body,get_account(aid,body.revision))
        store.execute('UPDATE aws_accounts SET name=?,region=?,credentials=?,proxy_url=? WHERE id=?',(*values,aid))
        return public(get_account(aid))


@api.delete('/accounts/{aid}')
def delete(aid: int, revision: str):
    with _config_lock:
        get_account(aid,revision)
        store.execute('DELETE FROM aws_ssh_links WHERE account_id=?',(aid,))
        store.execute('DELETE FROM aws_accounts WHERE id=?',(aid,))
        return {'ok':True}


@api.get('/accounts/{aid}/identity')
def identity(aid: int, revision: str): return run(get_account(aid,revision),'identity')


@api.get('/accounts/{aid}/instances')
def instances(aid: int, revision: str):
    rows = run(get_account(aid,revision),'instances')
    return {'instances':rows,'overview':{'total':len(rows),'running':sum(i['status']=='running' for i in rows)}}


@api.get('/accounts/{aid}/options')
def options(aid: int, revision: str): return run(get_account(aid,revision),'options')


class CreateBody(BaseModel):
    revision: str
    name: str = Field(min_length=1,max_length=100)
    zone: str = Field(min_length=1,max_length=64)
    blueprint_id: str = Field(min_length=1,max_length=128)
    bundle_id: str = Field(min_length=1,max_length=128)
    key_name: str = Field(min_length=1,max_length=255)
    ip_type: Literal['dualstack','ipv6'] = 'dualstack'


@api.post('/accounts/{aid}/create')
def create(aid: int, body: CreateBody):
    if not body.name.strip(): raise HTTPException(400,'实例名称不能为空')
    return run(get_account(aid,body.revision),'create',body.model_dump())


class ActionBody(BaseModel):
    revision: str
    name: str = Field(min_length=2,max_length=255)
    action: Literal['start','stop','reboot','delete']
    confirmed_name: str = ''


@api.post('/accounts/{aid}/action')
def action(aid: int, body: ActionBody):
    account = get_account(aid,body.revision)
    if body.action=='delete' and body.confirmed_name!=body.name:
        raise HTTPException(400,'请输入实例名称确认删除；系统盘会被删除，请先创建快照')
    return run(account,'action',body.name,body.action)


@api.get('/accounts/{aid}/metrics')
def metrics(aid: int, revision: str, name: str, days: int = Query(7,ge=1,le=90)):
    return run(get_account(aid,revision),'metrics',name,days)


@api.get('/accounts/{aid}/operation')
def operation(aid: int, revision: str, operation_id: str):
    return run(get_account(aid,revision),'operation',operation_id)


@api.get('/accounts/{aid}/snapshots')
def snapshots(aid: int, revision: str): return run(get_account(aid,revision),'snapshots')


class SnapshotBody(BaseModel):
    revision: str
    name: str = Field(min_length=2,max_length=255)
    instance_name: str = Field(default='',max_length=255)
    action: Literal['create','delete'] = 'create'
    confirmed_name: str = ''


@api.post('/accounts/{aid}/snapshots')
def snapshot(aid: int, body: SnapshotBody):
    account = get_account(aid,body.revision)
    if body.action=='delete':
        if body.confirmed_name!=body.name: raise HTTPException(400,'请输入快照名称确认删除')
        return run(account,'delete_snapshot',body.name)
    return run(account,'snapshot',body.instance_name,body.name)


@api.get('/accounts/{aid}/static-ips')
def static_ips(aid: int, revision: str): return run(get_account(aid,revision),'static_ips')


class StaticIpBody(BaseModel):
    revision: str
    name: str = Field(min_length=2,max_length=255)
    action: Literal['allocate','attach','detach','release']
    instance_name: str = Field(default='',max_length=255)
    confirmed_name: str = ''


@api.post('/accounts/{aid}/static-ips')
def static_ip(aid: int, body: StaticIpBody):
    account = get_account(aid,body.revision)
    if body.action in ('detach','release') and body.confirmed_name!=body.name:
        raise HTTPException(400,'请输入静态 IP 名称确认操作')
    return run(account,'static_ip_action',body.name,body.action,body.instance_name)


class SyncBody(BaseModel):
    revision: str
    username: str = Field(default='',pattern=r'^$|^[a-z_][a-z0-9_-]{0,31}$')
    ssh_proxy: str = Field(default='',max_length=2048)
    allow_private: bool = False


@api.post('/accounts/{aid}/sync-ssh')
def sync_ssh(aid: int, body: SyncBody):
    account = get_account(aid,body.revision)
    try: sshpool._socks_proxy({'proxy_command':body.ssh_proxy})
    except Exception: raise HTTPException(400,'SSH 代理无效，请使用 socks5h://地址:端口；未回退直连') from None
    cloud_id = run(account,'identity')['account_id']
    rows = run(account,'instances')
    with _config_lock:
        get_account(aid,body.revision)
        added = updated = skipped = 0
        for instance in rows:
            host = instance['public_ip'] or next(iter(instance.get('ipv6',[])), '') or (instance['private_ip'] if body.allow_private else '')
            if not host or instance['status'] not in ('running','pending'):
                skipped += 1
                continue
            key = (aid,cloud_id,account['region'],instance['id'])
            links = store.query('SELECT session_id FROM aws_ssh_links WHERE account_id=? AND cloud_account_id=? AND region=? AND instance_id=?',key)
            session = store.query('SELECT * FROM ssh_sessions WHERE id=?',(links[0]['session_id'],)) if links else []
            metadata = json.dumps({'source':'aws','account_id':aid,'cloud_account_id':cloud_id,'region':account['region'],
                'instance_id':instance['id'],'zone':instance['zone'],'machine_type':instance['machine_type'],'shape':instance['machine_type'],'state':instance['status']})
            if session:
                store.execute('UPDATE ssh_sessions SET metadata=? WHERE id=?',(metadata,session[0]['id']))
                if session[0]['host'] != host: skipped += 1
                else: updated += 1
            else:
                sid = store.execute('INSERT INTO ssh_sessions(name,host,username,auth_type,proxy_command,tags,metadata) VALUES(?,?,?,?,?,?,?)',
                    (f"{account['name']} / {instance['name']}",host,body.username or instance.get('username') or 'ubuntu','key',body.ssh_proxy,'aws:'+account['name'],metadata))
                store.execute('INSERT OR REPLACE INTO aws_ssh_links(account_id,cloud_account_id,region,instance_id,session_id) VALUES(?,?,?,?,?)',(*key,sid))
                added += 1
        return {'added':added,'updated':updated,'skipped':skipped,'note':'新会话请填写对应 SSH 私钥；已有会话保留认证和线路，IP 改变需手动编辑。'}

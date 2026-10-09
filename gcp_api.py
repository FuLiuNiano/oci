"""Independent GCP accounts and routes. OCI tables and handlers are untouched."""
import hashlib
import hmac
import json
import secrets
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from deps import require_auth
import gcp_service as gcp
import sshpool
import store

api = APIRouter(prefix="/api/gcp", dependencies=[Depends(require_auth)])
_revision_key = secrets.token_bytes(32)


def init_tables():
    store.execute("""CREATE TABLE IF NOT EXISTS gcp_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        project_id TEXT NOT NULL, credentials TEXT NOT NULL, proxy_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")
    store.execute("""CREATE TABLE IF NOT EXISTS gcp_ssh_links (
        account_id INTEGER NOT NULL, project_id TEXT NOT NULL, instance_id TEXT NOT NULL,
        session_id INTEGER NOT NULL, PRIMARY KEY(account_id,project_id,instance_id))""")


def revision(account):
    raw = json.dumps([account[k] for k in ('project_id','credentials','proxy_url')], ensure_ascii=False).encode()
    return hmac.new(_revision_key, raw, hashlib.sha256).hexdigest()


def public(account):
    result = {k:account[k] for k in ('id','name','project_id','created_at')}
    result.update(client_email=json.loads(account['credentials'])['client_email'], revision=revision(account),
                  proxied=bool(account['proxy_url']))
    if account['proxy_url']:
        p = urlsplit(account['proxy_url'])
        result['route'] = f"代理 · {p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else '')
    else:
        result['route'] = '直连'
    return result


def get_account(aid, expected=None):
    init_tables()
    rows = store.query('SELECT * FROM gcp_accounts WHERE id=?', (aid,))
    if not rows: raise HTTPException(404, 'GCP 账号不存在')
    account = rows[0]
    if expected is not None and not hmac.compare_digest(expected, revision(account)):
        raise HTTPException(409, 'GCP 凭据或线路已改变，请刷新账号后重试')
    return account


def run(account, method, *args, **kwargs):
    try:
        with gcp.Client(account) as client:
            return getattr(client, method)(*args, **kwargs)
    except gcp.GcpError as error:
        raise HTTPException(502, str(error)) from None


class AccountBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=63)
    credentials: dict | None = None
    proxy_url: str | None = Field(default=None, max_length=2048)
    remove_proxy: bool = False
    revision: str | None = None


def save_values(body, existing=None):
    credentials = body.credentials if body.credentials is not None else (json.loads(existing['credentials']) if existing else None)
    proxy = body.proxy_url if body.proxy_url is not None else (existing['proxy_url'] if existing else '')
    if not body.name.strip(): raise HTTPException(400, '账号名称不能为空')
    if existing and existing['proxy_url'] and not proxy and not body.remove_proxy:
        raise HTTPException(400, '移除 GCP 代理需要明确勾选改为直连')
    try: gcp.validate(body.project_id, credentials, proxy)
    except gcp.GcpError as error: raise HTTPException(400, str(error)) from None
    # Only store the fields used for authentication, never arbitrary URLs from uploaded JSON.
    credentials = {k:credentials[k] for k in ('type','client_email','private_key','private_key_id') if k in credentials}
    return (body.name.strip(), body.project_id, json.dumps(credentials), proxy)


@api.get('/accounts')
def accounts():
    init_tables()
    return [public(a) for a in store.query('SELECT * FROM gcp_accounts ORDER BY id')]


@api.post('/accounts')
def add(body: AccountBody):
    init_tables()
    values = save_values(body)
    aid = store.execute('INSERT INTO gcp_accounts(name,project_id,credentials,proxy_url) VALUES(?,?,?,?)', values)
    return public(get_account(aid))


@api.put('/accounts/{aid}')
def update(aid: int, body: AccountBody):
    if not body.revision: raise HTTPException(409, '请刷新 GCP 账号后编辑')
    existing = get_account(aid, body.revision)
    values = save_values(body, existing)
    store.execute('UPDATE gcp_accounts SET name=?,project_id=?,credentials=?,proxy_url=? WHERE id=?', (*values,aid))
    return public(get_account(aid))


@api.delete('/accounts/{aid}')
def delete_account(aid: int, revision: str):
    get_account(aid, revision)
    store.execute('DELETE FROM gcp_ssh_links WHERE account_id=?', (aid,))
    store.execute('DELETE FROM gcp_accounts WHERE id=?', (aid,))
    return {'ok':True}  # Existing cloud instances and SSH sessions are retained.


@api.get('/accounts/{aid}/instances')
def instances(aid: int, revision: str):
    account = get_account(aid, revision)
    rows = run(account, 'instances')
    return {'instances':rows, 'overview':gcp.overview(rows)}


@api.get('/accounts/{aid}/options')
def options(aid: int, revision: str, zone: str | None = None):
    return run(get_account(aid, revision), 'options', zone)


@api.get('/accounts/{aid}/traffic')
def traffic(aid: int, revision: str, days: int = Query(90, ge=1, le=90)):
    return run(get_account(aid, revision), 'traffic', days)


class CreateBody(BaseModel):
    revision: str
    name: str = Field(min_length=1, max_length=63)
    zone: str
    machine_type: str
    image: Literal['debian-12','ubuntu-2404'] = 'debian-12'
    disk_gb: int = Field(default=20, ge=10, le=2048)
    network: str
    subnet: str = ''
    public_ip: bool = True
    username: str = 'debian'
    ssh_key: str = Field(min_length=1, max_length=8192)


@api.post('/accounts/{aid}/create')
def create(aid: int, body: CreateBody):
    return run(get_account(aid, body.revision), 'create', body.model_dump())


class ActionBody(BaseModel):
    revision: str
    zone: str
    name: str
    action: Literal['start','stop','reset','delete','change-ip']
    confirmed_name: str = ''
    preserve_disks: bool = True


@api.post('/accounts/{aid}/action')
def action(aid: int, body: ActionBody):
    account = get_account(aid, body.revision)
    if body.action in ('delete','change-ip') and body.confirmed_name != body.name:
        raise HTTPException(400, '请输入实例名称确认操作')
    return run(account, 'action', body.zone, body.name, body.action, body.preserve_disks)


@api.get('/accounts/{aid}/operation')
def operation(aid: int, revision: str, zone: str, name: str):
    return run(get_account(aid, revision), 'operation', zone, name)


class SyncBody(BaseModel):
    revision: str
    username: str = Field(default='debian', pattern=r'^[a-z_][a-z0-9_-]{0,31}$')
    ssh_proxy: str = Field(default='', max_length=2048)
    allow_private: bool = False


@api.post('/accounts/{aid}/sync-ssh')
def sync_ssh(aid: int, body: SyncBody):
    account = get_account(aid, body.revision)
    try: sshpool._socks_proxy({'proxy_command':body.ssh_proxy})
    except Exception: raise HTTPException(400, 'SSH 代理无效，请使用 socks5h://地址:端口；未回退直连') from None
    rows = run(account, 'instances')
    get_account(aid, body.revision)  # Do not persist results after credentials/route changed during the fetch.
    added = updated = skipped = 0
    for instance in rows:
        host = instance['public_ip'] or (instance['private_ip'] if body.allow_private else '')
        if not host:
            skipped += 1
            continue
        key = (aid, account['project_id'], instance['id'])
        links = store.query('SELECT session_id FROM gcp_ssh_links WHERE account_id=? AND project_id=? AND instance_id=?', key)
        session = store.query('SELECT * FROM ssh_sessions WHERE id=?', (links[0]['session_id'],)) if links else []
        metadata = json.dumps({'source':'gcp','account_id':aid,'project_id':account['project_id'], 'instance_id':instance['id'], 'zone':instance['zone'], 'machine_type':instance['machine_type'], 'shape':instance['machine_type'], 'state':instance.get('status','')})
        if session:
            # A changed host is not silently applied to a live connection or old host trust.
            store.execute('UPDATE ssh_sessions SET metadata=? WHERE id=?', (metadata,session[0]['id']))
            if session[0]['host'] != host: skipped += 1
            else: updated += 1
        else:
            sid = store.execute('INSERT INTO ssh_sessions(name,host,username,auth_type,proxy_command,tags,metadata) VALUES(?,?,?,?,?,?,?)',
                (f"{account['name']} / {instance['name']}",host,body.username,'key',body.ssh_proxy,'gcp:'+account['name'],metadata))
            store.execute('INSERT OR REPLACE INTO gcp_ssh_links(account_id,project_id,instance_id,session_id) VALUES(?,?,?,?)', (*key,sid))
            added += 1
    return {'added':added,'updated':updated,'skipped':skipped, 'note':'新会话请编辑填写 SSH 私钥；已有会话保留认证和线路。IP 改变的会话需手动编辑主机地址。'}

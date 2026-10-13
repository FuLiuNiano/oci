"""Opt-in OCI monitoring. Persisted targets include configuration fingerprints."""
import time
from datetime import datetime, timedelta, timezone

import notify
import oci_service
import sshpool
import store


def session_fingerprint(session):
    import hashlib, json
    return hashlib.sha256(json.dumps([session.get(k) for k in
        ('id','host','port','username','auth_type','secret','proxy_command')],sort_keys=True).encode()).hexdigest()


def public_config(account):
    cfg=store.get_json_setting(f"cloud_automation:{account['id']}",{}) or {}
    return {k:cfg.get(k,default) for k,default in
        [('notify_changes',False),('daily_report',False),('auto_ip',False),('expand_regions',False),('instance_id',''),
         ('session_id',None),('failures',0),('last_result','尚未启用'),('last_change',0)]}


def tick(now=None):
    from cloud_tools import fingerprint
    now=time.time() if now is None else now
    for account in store.query("SELECT * FROM accounts WHERE platform='oci'"):
        if store.account_disabled('oci',account['id']): continue
        key=f"cloud_automation:{account['id']}"
        cfg=store.get_json_setting(key,{}) or {}
        if not any(cfg.get(k) for k in ('notify_changes','daily_report','auto_ip','expand_regions')): continue
        if cfg.get('account_fingerprint')!=fingerprint(account):
            cfg.update(notify_changes=False,daily_report=False,auto_ip=False,expand_regions=False,last_result='账号配置或线路已变化，请重新确认自动化设置')
            store.update_json_setting_version(key,cfg.get('version'),cfg)
            continue
        try:
            if cfg.get('expand_regions'):
                regions=oci_service.subscribed_regions(account)
                latest=store.get_json_setting(key,{}) or {}
                fresh=store.query('SELECT * FROM accounts WHERE id=?',(account['id'],))
                if (latest.get('version')==cfg.get('version') and latest.get('expand_regions') and fresh and
                    fingerprint(fresh[0])==cfg['account_fingerprint'] and not store.account_disabled('oci',account['id'])):
                    params=store.account_params(account)
                    existing=store.query("SELECT * FROM accounts WHERE platform='oci'")
                    owner=lambda p:tuple(p.get(k) for k in ('tenancy_ocid','user_ocid'))
                    for region in regions:
                        if region['status']!='READY' or any(a['region']==region['region'] and owner(store.account_params(a))==owner(params) for a in existing): continue
                        store.execute('INSERT INTO accounts(name,region,params) VALUES(?,?,?)',
                            (account['name']+' · '+region['region'],region['region'],store.json.dumps(params)))
                        existing.append({'region':region['region'],'params':params})
            instances=oci_service.list_instances(account)
            latest=store.get_json_setting(key,{}) or {}
            fresh=store.query('SELECT * FROM accounts WHERE id=?',(account['id'],))
            if (latest.get('version')!=cfg.get('version') or not fresh or
                fingerprint(fresh[0])!=cfg['account_fingerprint'] or store.account_disabled('oci',account['id'])):
                continue
            current={i['id']:i['state'] for i in instances}
            previous=cfg.get('states')
            if cfg.get('notify_changes') and previous is not None:
                changes=[f"{i['name']}：{previous.get(i['id'],'新增')} → {i['state']}" for i in instances if previous.get(i['id'])!=i['state']]
                changes += [f"实例 {iid}：不再出现在当前区域列表" for iid in previous if iid not in current]
                if changes: notify.send('实例状态变化 · '+account['name'],'\n'.join(changes))
            cfg['states']=current
            date=(datetime.fromtimestamp(now,timezone.utc)+timedelta(hours=8))
            if cfg.get('daily_report') and date.hour>=9 and cfg.get('last_daily')!=str(date.date()):
                traffic=oci_service.traffic_usage_gb(account,24)
                result=notify.send('每日报告 · '+account['name'],f"区域：{account['region']}\n实例：{len(instances)}\n运行：{sum(i['state']=='RUNNING' for i in instances)}\n24 小时网卡出站：{traffic['total_gb']} GB（包含内网）")
                cfg['last_daily']=str(date.date())
                cfg['last_result']='每日报告已处理' if result else '未配置通知渠道，报告未发送'
            if cfg.get('auto_ip'): monitor_ip(account,cfg,instances,now)
        except Exception as error:
            cfg['last_result']=oci_service.fmt_err(error)
        # Do not replace a setting edited or disabled while the network request was pending.
        if not store.account_disabled('oci',account['id']):
            store.update_json_setting_version(key,cfg.get('version'),cfg)


def monitor_ip(account,cfg,instances,now):
    from cloud_tools import fingerprint
    target=next((i for i in instances if i['id']==cfg.get('instance_id')),None)
    if not target or target['state']!='RUNNING':
        cfg['failures']=0
        cfg['last_result']='实例未运行或不存在，不更换 IP'
        return
    links=store.query('SELECT session_id FROM oci_ssh_links WHERE account_id=? AND region=? AND instance_id=?',
        (account['id'],account['region'],target['id']))
    rows=store.query('SELECT * FROM ssh_sessions WHERE id=?',(cfg.get('session_id'),))
    if not rows or not links or links[0]['session_id']!=rows[0]['id'] or session_fingerprint(rows[0])!=cfg.get('session_fingerprint'):
        cfg.update(auto_ip=False,last_result='会话关联或连接配置已变化，自动换 IP 已停用')
        return
    session=rows[0]
    if session['host']!=target.get('public_ip'):
        cfg.update(auto_ip=False,last_result='会话主机与当前公网 IPv4 不一致，自动换 IP 已停用')
        return
    try:
        sshpool.test_session(session)
        cfg.update(failures=0,last_result='SSH 连接正常')
        return
    except Exception as error:
        message=str(error)
        # Proxy errors, auth errors, host-trust errors and a refused service are not evidence of an unreachable IP.
        if 'SSH 连接超时' not in message or 'SOCKS5' in message:
            cfg.update(failures=0,last_result='非明确的目标连接超时，不自动换 IP：'+message)
            return
    cfg['failures']=cfg.get('failures',0)+1
    cfg['last_result']=f"连续目标连接超时 {cfg['failures']}/3"
    if cfg['failures']<3 or now-cfg.get('last_change',0)<3600: return
    key=f"cloud_automation:{account['id']}"
    latest=store.get_json_setting(key,{}) or {}
    accounts=store.query('SELECT * FROM accounts WHERE id=?',(account['id'],))
    sessions=store.query('SELECT * FROM ssh_sessions WHERE id=?',(session['id'],))
    if (not latest.get('auto_ip') or latest.get('version')!=cfg.get('version') or not accounts or
        fingerprint(accounts[0])!=cfg['account_fingerprint'] or not sessions or
        session_fingerprint(sessions[0])!=cfg['session_fingerprint'] or store.account_disabled('oci',account['id'])): return
    result=oci_service.change_public_ip(account,target['id'])
    cfg.update(auto_ip=False,last_change=now,failures=0,
        last_result=f"已更换 IPv4：{result.get('old_ip','')} → {result.get('new_ip','')}；请核对 SSH/域名地址后重新启用监控")
    # Do not silently rewrite saved connection identity, credentials, proxies or DNS.
    notify.send('自动换 IP · '+account['name'],cfg['last_result'])

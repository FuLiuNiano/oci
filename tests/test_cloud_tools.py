import copy
import json
from unittest.mock import Mock

import oci
import pytest
import requests

import aws_api
import aws_ec2
import cloud_tools
import cloud_automation
import oci_service
import oci_ssh
import sshpool
import store
from test_aws import config, add_account, response
from test_ipv6_public import public_network
from test_oci_ssh import saved_session, machine


def oracle_body(account,**values):
    return {'revision':cloud_tools.oracle_revision(account),'confirm':True,**values}


def test_worker_cannot_restore_replaced_or_deleted_automation(database):
    key = 'cloud_automation:1'
    old = {'version': 'old', 'auto_ip': True, 'failures': 3}
    new = {'version': 'new', 'auto_ip': False}
    store.set_json_setting(key, new)
    assert not store.update_json_setting_version(key, 'old', old)
    assert store.get_json_setting(key) == new
    assert store.update_json_setting_version(key, 'new', {**new, 'failures': 0})
    store.execute('DELETE FROM settings WHERE key=?', (key,))
    assert not store.update_json_setting_version(key, 'old', old)
    assert store.get_json_setting(key) is None


def test_region_edit_preserves_connection_revision_and_proxy(client):
    data={'name':'server','host':'203.0.113.2','region':'日本东京','secret':'synthetic-password',
          'proxy_command':'socks5h://proxy:1080'}
    sid=client.post('/api/ssh/sessions',json=data).json()['id']
    before=client.get('/api/ssh/sessions').json()['data'][0]
    data['region']='德国'
    assert client.put(f'/api/ssh/sessions/{sid}',json=data).status_code==200
    after=client.get('/api/ssh/sessions').json()['data'][0]
    assert after['connection_revision']==before['connection_revision']
    assert after['region']=='德国' and after['proxy_command']==before['proxy_command']
    copied=client.post(f'/api/ssh/sessions/{sid}/copy').json()['id']
    assert store.query('SELECT region FROM ssh_sessions WHERE id=?',(copied,))[0]['region']=='德国'


def test_disabled_oracle_never_connects_and_can_be_reenabled(client,account,monkeypatch):
    rev=cloud_tools.oracle_revision(account)
    call=Mock();monkeypatch.setattr(oci.core.ComputeClient,'__init__',call)
    assert client.post(f"/api/tools/accounts/oci/{account['id']}/enabled",json={'revision':rev,'enabled':False}).status_code==200
    with pytest.raises(oci_service.OciError,match='停用'): oci_service._client(oci.core.ComputeClient,account)
    call.assert_not_called()
    assert client.post(f"/api/tools/accounts/oci/{account['id']}/enabled",json={'revision':rev,'enabled':True}).status_code==200
    assert not store.account_disabled('oci',account['id'])


def test_stale_oracle_revision_prevents_mutation(client,account,monkeypatch):
    rev=cloud_tools.oracle_revision(account)
    account['params']['proxy_url']='socks5h://proxy:1080'
    store.execute('UPDATE accounts SET params=? WHERE id=?',(json.dumps(account['params']),account['id']))
    op=Mock();monkeypatch.setattr(cloud_tools,'open_port',op)
    r=client.post(f"/api/tools/oci/{account['id']}/actions/open-port",json={'revision':rev,'confirm':True,'instance_id':'instance1','source':'203.0.113.1/32'})
    assert r.status_code==409;op.assert_not_called()


def test_cloud_port_rule_preserves_old_rules_and_etag(sdk,account):
    data,calls,_,_,_=public_network(sdk)
    # public_network has security list ETags and stateful old IPv4 rules.
    result=cloud_tools.open_port(account,'instance1','203.0.113.1/32',8443)
    assert '共享' in result['message']
    update=next(c[2] for c in calls if c[0]=='update_security_list')
    assert update['header_params']['if-match']
    rules=update['body'].ingress_security_rules
    assert any(r.source=='203.0.113.1/32' and r.tcp_options.destination_port_range.min==8443 for r in rules)
    assert len(rules)>1


def test_mfa_validates_all_users_before_any_deletion(client,account,monkeypatch):
    monkeypatch.setattr(oci_service,'list_users',lambda a:[{'id':'own'}])
    clear=Mock();monkeypatch.setattr(oci_service,'clear_user_mfa',clear)
    r=client.post(f"/api/tools/oci/{account['id']}/actions/clear-mfa",json=oracle_body(account,user_ids=['own','foreign']))
    assert r.status_code==400;clear.assert_not_called()


def test_disabled_aws_config_does_not_touch_gcp_with_same_id(client):
    a=add_account(client)
    assert client.post(f"/api/tools/accounts/aws/{a['id']}/enabled",json={'revision':a['revision'],'enabled':False}).status_code==200
    assert store.account_disabled('aws',a['id']) and not store.account_disabled('gcp',a['id'])
    r=client.get(f"/api/tools/aws/{a['id']}/identity?revision={a['revision']}")
    assert r.status_code==409


def automation_config(account,sid,**extra):
    row=store.query('SELECT * FROM ssh_sessions WHERE id=?',(sid,))[0]
    cfg={'auto_ip':True,'instance_id':'instance1','session_id':sid,'version':'test-version',
         'account_fingerprint':cloud_tools.fingerprint(account),'session_fingerprint':cloud_automation.session_fingerprint(row),**extra}
    store.set_json_setting(f"cloud_automation:{account['id']}",cfg)
    return cfg


@pytest.mark.parametrize('error',['SSH 认证失败','SSH 主机公钥与已保存记录不符','SOCKS5 代理不可达','目标拒绝 SSH 连接'])
def test_auto_ip_never_changes_for_auth_trust_proxy_or_service_failure(account,monkeypatch,error):
    sid=saved_session(account);store.execute("UPDATE ssh_sessions SET proxy_command='' WHERE id=?",(sid,))
    oci_ssh.sync_instance(account,machine())
    cfg=automation_config(account,sid)
    monkeypatch.setattr(sshpool,'test_session',Mock(side_effect=sshpool.SshError(error)))
    change=Mock();monkeypatch.setattr(oci_service,'change_public_ip',change)
    cloud_automation.monitor_ip(account,cfg,[machine(state='RUNNING')],10000)
    assert cfg['failures']==0;change.assert_not_called()


def test_auto_ip_only_linked_instance_then_pauses_without_rewriting_ssh(account,monkeypatch):
    sid=saved_session(account);store.execute("UPDATE ssh_sessions SET proxy_command='' WHERE id=?",(sid,))
    oci_ssh.sync_instance(account,machine())
    cfg=automation_config(account,sid,failures=2)
    before=store.query('SELECT * FROM ssh_sessions WHERE id=?',(sid,))[0]
    monkeypatch.setattr(sshpool,'test_session',Mock(side_effect=sshpool.SshError('SSH 连接超时')))
    change=Mock(return_value={'old_ip':'203.0.113.2','new_ip':'203.0.113.3'})
    monkeypatch.setattr(oci_service,'change_public_ip',change);monkeypatch.setattr(cloud_automation.notify,'send',Mock())
    cloud_automation.monitor_ip(account,cfg,[machine(state='RUNNING')],10000)
    change.assert_called_once_with(account,'instance1')
    assert not cfg['auto_ip'] and cfg['last_change']==10000
    assert store.query('SELECT * FROM ssh_sessions WHERE id=?',(sid,))[0]==before


def test_auto_ip_foreign_link_and_stale_session_never_probe(account,monkeypatch):
    sid=saved_session(account)
    cfg=automation_config(account,sid)
    probe=Mock();monkeypatch.setattr(sshpool,'test_session',probe)
    cloud_automation.monitor_ip(account,cfg,[machine(state='RUNNING')],10000)
    assert not cfg['auto_ip'];probe.assert_not_called()


def test_automation_rejects_proxy_and_foreign_session(client,account):
    sid=saved_session(account);oci_ssh.sync_instance(account,machine())
    r=client.post(f"/api/tools/oci/{account['id']}/automation",json=oracle_body(account,auto_ip=True,session_id=sid,instance_id='instance1'))
    assert r.status_code==400 and '代理' in r.json()['detail']
    r=client.post(f"/api/tools/oci/{account['id']}/automation",json=oracle_body(account,auto_ip=True,session_id=sid,instance_id='foreign'))
    assert r.status_code==400


def test_region_expansion_copies_only_own_params_and_never_changes_existing(account,monkeypatch):
    cfg={'expand_regions':True,'version':'test','account_fingerprint':cloud_tools.fingerprint(account)}
    store.set_json_setting(f"cloud_automation:{account['id']}",cfg)
    monkeypatch.setattr(oci_service,'subscribed_regions',lambda a:[{'region':'us-phoenix-1','status':'READY'}])
    monkeypatch.setattr(oci_service,'list_instances',lambda a:[])
    cloud_automation.tick(10000);cloud_automation.tick(10100)
    rows=store.query('SELECT * FROM accounts ORDER BY id')
    assert len(rows)==2 and rows[0]['params']==rows[1]['params']==account['params']
    assert rows[1]['region']=='us-phoenix-1' and rows[1]['auto_restart']==0


def test_maintenance_checks_revision_before_ssh(client,monkeypatch):
    sid=client.post('/api/ssh/sessions',json={'name':'server','host':'203.0.113.2'}).json()['id']
    probe=Mock();monkeypatch.setattr(sshpool,'_run',probe)
    r=client.post(f'/api/tools/ssh/{sid}/maintenance',json={'operation':'load','expected_revision':'stale'})
    assert r.status_code==409;probe.assert_not_called()


def test_maintenance_code_is_bounded_and_compiles():
    import server_tools
    compile(server_tools.HOLD,'hold','exec')
    for start in (True,False): compile(server_tools.memory_script(start),'parent','exec')
    assert 'time.sleep(900)' in server_tools.HOLD and '256*1024*1024' in server_tools.HOLD
    with pytest.raises(ValueError):server_tools.command('arbitrary-command')


def test_gcp_overview_billing_and_quotas_keep_each_project_route(credentials,monkeypatch):
    import gcp_service
    from test_gcp import key_json
    observed=[]
    monkeypatch.setattr(gcp_service.Client,'_access_token',lambda self:self.email)
    def send(self,method,url,**kwargs):
        observed.append((self.project,url,dict(self.proxies),self.session.trust_env,kwargs['headers']['Authorization']))
        assert self.project in url and self.project in kwargs['headers']['Authorization']
        if url.endswith('aggregated/instances'): return {'items':{}}
        if url.endswith('/billingInfo'): return {'billingEnabled':True,'billingAccountName':'billingAccounts/test'}
        assert url.endswith('/'+self.project)
        return {'quotas':[{'metric':'CPUS_ALL_REGIONS','limit':8,'usage':1}]}
    monkeypatch.setattr(gcp_service.Client,'_send',send)
    for i in (1,2):
        project='project-'+str(i)
        account={'project_id':project,'credentials':key_json(credentials,f'manager@{project}.iam.gserviceaccount.com'),
                 'proxy_url':'' if i==1 else 'socks5://proxy:1080'}
        result=cloud_tools.gcp_summary(account)
        assert result['quotas'][0]['usage']==1 and result['billing']['enabled']
    assert len(observed)==6
    for project,url,proxy,trust_env,auth in observed:
        assert not trust_env
        assert proxy==({} if project=='project-1' else {'http':'socks5h://proxy:1080','https':'socks5h://proxy:1080'})


def test_monitoring_default_off_and_changed_config_disables_it(account,monkeypatch):
    probe=Mock();monkeypatch.setattr(oci_service,'list_instances',probe)
    cloud_automation.tick(10000);probe.assert_not_called()
    cfg={'notify_changes':True,'account_fingerprint':'outdated','version':'old'}
    store.set_json_setting(f"cloud_automation:{account['id']}",cfg)
    cloud_automation.tick(10000);probe.assert_not_called()
    saved=store.get_json_setting(f"cloud_automation:{account['id']}")
    assert not saved['notify_changes'] and not saved['auto_ip']


def test_subscribe_uses_region_key_not_name(client,account,monkeypatch):
    from types import SimpleNamespace
    identity=SimpleNamespace(list_regions=lambda:None,create_region_subscription=Mock())
    monkeypatch.setattr(oci_service,'_client',lambda cls,a:identity)
    monkeypatch.setattr(oci_service,'_all',lambda fn,*args,**kw:[SimpleNamespace(name='ap-tokyo-1',key='NRT')])
    monkeypatch.setattr(oci_service,'subscribed_regions',lambda a:[])
    r=client.post(f"/api/tools/oci/{account['id']}/actions/subscribe-region",json=oracle_body(account,region='ap-tokyo-1'))
    assert r.status_code==200
    details,tenancy=identity.create_region_subscription.call_args.args
    assert details.region_key=='NRT' and tenancy==account['params']['tenancy_ocid']


def test_region_expansion_account_edit_during_fetch_never_copies_old_configuration(account,monkeypatch):
    cfg={'expand_regions':True,'version':'test','account_fingerprint':cloud_tools.fingerprint(account)}
    store.set_json_setting(f"cloud_automation:{account['id']}",cfg)
    def regions(a):
        params={**account['params'],'proxy_url':'socks5h://new-proxy:1080'}
        store.execute('UPDATE accounts SET params=? WHERE id=?',(json.dumps(params),account['id']))
        return [{'region':'us-phoenix-1','status':'READY'}]
    monkeypatch.setattr(oci_service,'subscribed_regions',regions)
    monkeypatch.setattr(oci_service,'list_instances',lambda a:[])
    cloud_automation.tick(10000)
    assert len(store.query('SELECT id FROM accounts'))==1


def test_quick_launch_uses_stored_task_target_and_never_runs_paused_task(client,account,monkeypatch):
    import tasks
    tid=store.execute('INSERT INTO launch_tasks(account_id,display_name,shape) VALUES(?,?,?)',
        (account['id'],'quick','VM.Standard.E2.1.Micro'))
    run=Mock();monkeypatch.setattr(tasks,'_run_launch_task',run)
    assert client.post(f'/api/launch-tasks/{tid}/run-now').status_code==200
    assert run.call_args.args[0]['account_id']==account['id']
    store.execute("UPDATE launch_tasks SET status='stopped' WHERE id=?",(tid,))
    assert client.post(f'/api/launch-tasks/{tid}/run-now').status_code==400
    assert run.call_count==1

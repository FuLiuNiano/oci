import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import mcp_service
import oci_service
import store
import tasks
import webapi
from test_ssh import ssh_server


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_cancel_queued_launch_during_previous_launch(client, account, monkeypatch, operation):
    ids = [client.post('/api/launch-tasks', json={
        'account_id': account['id'], 'ssh_key': 'ssh-ed25519 test',
    }).json()['id'] for _ in range(2)]
    calls = []
    def launch(acct, task):
        calls.append(task['id'])
        if task['id'] == ids[0]:
            assert client.post(f'/api/launch-tasks/{ids[1]}/{operation}').status_code == 200
        return 'instance', 'AD1'
    monkeypatch.setattr(oci_service, 'launch_once', launch)
    monkeypatch.setattr(tasks, '_wait_ip_and_notify', lambda *args: None)
    tasks._tick_launch_tasks()
    assert calls == [ids[0]]
    row = store.query('SELECT status FROM launch_tasks WHERE id=?', (ids[1],))
    assert row == ([{'status': 'stopped'}] if operation == 'stop' else [])


def test_inflight_launch_cannot_be_deleted_or_restarted(client, account, monkeypatch):
    tid = client.post('/api/launch-tasks', json={
        'account_id': account['id'], 'ssh_key': 'ssh-ed25519 test',
    }).json()['id']
    entered, finish = threading.Event(), threading.Event()
    def launch(*args):
        entered.set()
        assert finish.wait(5)
        return 'instance1', 'AD1'
    monkeypatch.setattr(oci_service, 'launch_once', launch)
    monkeypatch.setattr(tasks, '_wait_ip_and_notify', lambda *args: None)
    worker = threading.Thread(target=tasks._tick_launch_tasks)
    worker.start()
    try:
        assert entered.wait(3)
        for operation in ('stop', 'delete', 'start'):
            assert client.post(f'/api/launch-tasks/{tid}/{operation}').status_code == 409
    finally:
        finish.set()
        worker.join(5)
    assert store.query('SELECT status,instance_id FROM launch_tasks WHERE id=?', (tid,)) == [
        {'status': 'success', 'instance_id': 'instance1'}]


def test_password_change_disconnects_idle_real_ssh(client, ssh_server):
    sid = client.post('/api/ssh/sessions', json=ssh_server).json()['id']
    with client.websocket_connect(f'/ws/ssh?sid={sid}') as ws:
        ws.send_text('hello\n')
        assert 'hello' in ws.receive_text()
        assert client.post('/api/settings/password', json={
            'old_password': client.initial_password, 'new_password': 'z' * 32,
        }).status_code == 200
        assert client.get('/api/me').status_code == 401
        with pytest.raises(WebSocketDisconnect) as result:
            ws.receive_text()
        assert result.value.code == 4401


def test_successful_task_cannot_be_stopped_then_relaunched(client, account):
    tid = client.post('/api/launch-tasks', json={
        'account_id': account['id'], 'ssh_key': 'ssh-ed25519 test',
    }).json()['id']
    store.execute("UPDATE launch_tasks SET status='success',instance_id='created' WHERE id=?", (tid,))
    assert client.post(f'/api/launch-tasks/{tid}/stop').status_code == 400
    assert client.post(f'/api/launch-tasks/{tid}/start').status_code == 400
    assert store.query('SELECT status FROM launch_tasks WHERE id=?', (tid,))[0]['status'] == 'success'


@pytest.mark.parametrize('size', [0, 4])
def test_preview_small_objects_uses_real_sdk_contract(sdk, account, size):
    import io
    data, calls = sdk
    data['get_namespace'] = 'ns'
    body = SimpleNamespace(raw=io.BytesIO(b'test'[:size]), close=Mock())
    data['get_object'] = body
    assert oci_service.get_object(account, 'bucket', 'file') == {
        'content': 'test'[:size], 'size': size, 'truncated': False}
    assert calls[-1][2]['header_params']['range'] == 'bytes=0-2000000'
    body.close.assert_called_once()


def test_preview_empty_object_range_rejection(sdk, account):
    import oci
    data, _ = sdk
    data['get_namespace'] = 'ns'
    data['get_object'] = oci.exceptions.ServiceError(416, 'InvalidRange', {}, 'empty')
    data['head_object'] = lambda *args, **kw: oci.response.Response(200, {'content-length': '0'}, None, None)
    assert oci_service.get_object(account, 'bucket', 'empty') == {'content': '', 'truncated': False, 'size': 0}


def test_mcp_stop_and_start_share_restart_exclusions(client, account, monkeypatch):
    store.execute('UPDATE accounts SET auto_restart=1 WHERE id=?', (account['id'],))
    actions = []
    monkeypatch.setattr(oci_service, 'instance_action', lambda a, i, op: actions.append(op) or {'ok': True})
    monkeypatch.setattr(oci_service, 'list_instances', lambda a: [
        {'id': 'instance1', 'name': 'test', 'state': 'STOPPED'}])
    args = {'account_id': account['id'], 'instance_id': 'instance1', 'action': 'STOP'}
    mcp_service._dispatch('instance_action', args)
    tasks._tick_auto_restart()
    assert actions == ['SOFTSTOP']
    store.set_json_setting(f"traffic_block:{account['id']}", ['instance1'])
    mcp_service._dispatch('instance_action', {**args, 'action': 'START'})
    for prefix in ('manual_stop', 'traffic_block'):
        assert store.get_json_setting(f"{prefix}:{account['id']}") == []


@pytest.mark.parametrize('range_supported', [True, False])
def test_object_preview_bounds_upstream_read(account, monkeypatch, range_supported):
    class Body:
        def __init__(self):
            self.raw = Mock()
            self.raw.read.return_value = b'x' * 17
            self.close = Mock()
        @property
        def content(self):
            raise AssertionError('Must not download the entire object')
    body = Body()
    sdk = Mock()
    sdk.get_namespace.return_value.data = 'ns'
    sdk.get_object.return_value = SimpleNamespace(data=body, status=206 if range_supported else 200,
        headers={'Content-Range': 'bytes 0-16/9000000000'} if range_supported else {'Content-Length': '9000000000'})
    monkeypatch.setattr(oci_service, '_client', lambda *args: sdk)
    result = oci_service.get_object(account, 'bucket', 'large', max_bytes=16)
    assert result == {'content': 'x' * 16, 'size': 9000000000, 'truncated': True}
    sdk.get_object.assert_called_once_with('ns', 'bucket', 'large', range='bytes=0-16')
    body.raw.read.assert_called_once_with(17)
    body.close.assert_called_once()
    sdk.base_client.session.close.assert_called_once()


def test_cf_credentials_preserve_replace_and_explicit_clear(client, monkeypatch):
    path = '/api/cf/settings'
    assert client.post(path, json={'email': 'a@example.com', 'global_key': 'key1'}).status_code == 200
    assert client.post(path, json={'email': 'b@example.com', 'global_key': ''}).status_code == 200
    saved = store.get_json_setting('cf_params')
    assert saved['cf_account_key'] == 'key1' and saved['cf_email'] == 'b@example.com'
    assert client.post(path, json={'api_token': 'token1'}).status_code == 200
    assert client.post(path, json={}).status_code == 200
    public = client.get(path).json()
    assert public == {'has_token': True, 'has_key': True, 'email': 'b@example.com'}
    assert client.post(path, json={'api_token': 'token2'}).status_code == 200
    assert store.get_json_setting('cf_params')['cf_api_token'] == 'token2'
    assert client.post(path, json={'clear_token': True}).status_code == 200
    monkeypatch.setattr(webapi.cf, 'list_zones', lambda cfg: [{'id': 'zone'}] if cfg['cf_account_key'] == 'key1' else [])
    assert client.post('/api/cf/test').json() == {'ok': True, 'zones': 1}
    assert client.post(path, json={'clear_global_key': True}).status_code == 200
    assert client.get(path).json() == {'has_token': False, 'has_key': False, 'email': ''}


def test_trusted_proxy_clients_have_separate_login_limits(client):
    import main
    # Same proxy peer, different real client IPs. Incoming XFF is overwritten by nginx.
    wrapped = ProxyHeadersMiddleware(main.app, trusted_hosts=['127.0.0.1'])
    with TestClient(wrapped, client=('127.0.0.1', 1234)) as proxied:
        for _ in range(10):
            response = proxied.post(client.prefix + '/api/login',
                headers={'X-Forwarded-For': '192.0.2.1'}, json={'username': 'bad', 'password': 'bad'})
            assert response.status_code == 401
        body = {'username': client.initial_username, 'password': client.initial_password}
        assert proxied.post(client.prefix + '/api/login', headers={'X-Forwarded-For': '192.0.2.1'}, json=body).status_code == 429
        assert proxied.post(client.prefix + '/api/login', headers={'X-Forwarded-For': '192.0.2.2'}, json=body).status_code == 200
    with TestClient(wrapped, client=('192.0.2.99', 1234)) as untrusted:
        untrusted.post(client.prefix + '/api/login', headers={'X-Forwarded-For': 'fake'}, json={'username': 'bad', 'password': 'bad'})
    assert 'fake' not in main._login_attempts
    assert '192.0.2.99' in main._login_attempts

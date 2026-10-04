import asyncio
import socket

import asyncssh
import pytest

import sshpool
import store


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True
    def password_auth_supported(self):
        return True
    def validate_password(self, username, password):
        return username == "test" and password == "test-password"
    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return True


@pytest.fixture
def ssh_server(database):
    (database / "folder").mkdir()
    (database / "binary.dat").write_bytes(bytes(range(256)))
    async def handle(process):
        if process.command:
            process.stdout.write("ok\n")
            process.exit(0)
        else:
            while True:
                try:
                    line = await process.stdin.readline()
                    if not line:
                        break
                    process.stdout.write("echo:"+line)
                except asyncssh.TerminalSizeChanged:
                    continue
    async def create():
        return await asyncssh.create_server(Server,"127.0.0.1",0,
            server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
            process_factory=handle,
            sftp_factory=lambda channel:asyncssh.SFTPServer(channel,chroot=str(database)))
    server=sshpool._run(create())
    sess={"name":"test","host":"127.0.0.1","port":server.get_port(),"username":"test",
          "auth_type":"password","secret":"test-password"}
    yield sess
    async def close():
        server.close()
        await server.wait_closed()
    sshpool._run(close())
    sshpool.shutdown()


def test_real_ssh_exec_and_host_key_persistence(ssh_server):
    assert sshpool.test_session(ssh_server) == "ok"
    key_id=f"ssh_hostkey:127.0.0.1:{ssh_server['port']}"
    assert store.get_setting(key_id)
    store.set_setting(key_id,asyncssh.generate_private_key("ssh-rsa").export_public_key().decode())
    with pytest.raises(sshpool.SshError):
        sshpool.test_session(ssh_server)


def test_real_sftp_list_and_binary_roundtrip(ssh_server):
    entries=sshpool.sftp_list(ssh_server,"/")
    assert any(f["name"]=="folder" and f["dir"] for f in entries)
    assert sshpool.sftp_read(ssh_server,"/binary.dat") == bytes(range(256))
    sshpool.sftp_write(ssh_server,"/written.dat",b"\x00\xffhello")
    assert sshpool.sftp_read(ssh_server,"/written.dat") == b"\x00\xffhello"
    sshpool.sftp_rename(ssh_server,"/written.dat","/renamed.dat")
    sshpool.sftp_delete(ssh_server,"/renamed.dat")


def test_sftp_upload_binary_and_duplicate_protection(client, ssh_server, database):
    sid = store.execute("INSERT INTO ssh_sessions(name,host,port,username,secret) VALUES(?,?,?,?,?)",
        ("upload", ssh_server["host"], ssh_server["port"], "test", "test-password"))
    endpoint = f"/api/ssh/sftp/upload?session_id={sid}&path=/folder/upload.bin"
    data = bytes(range(256)) * 400
    response = client.post(endpoint, content=data,
                           headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 200, response.text
    assert response.json()["size"] == len(data)
    assert (database / "folder" / "upload.bin").read_bytes() == data
    duplicate = client.post(endpoint, content=b"changed")
    assert duplicate.status_code == 409
    assert (database / "folder" / "upload.bin").read_bytes() == data
    invalid = client.post(f"/api/ssh/sftp/upload?session_id={sid}&path=relative.bin",
                          content=b"bad")
    assert invalid.status_code == 400


def test_sftp_upload_limit_removes_partial_file(client, ssh_server, database, monkeypatch):
    original = sshpool.sftp_upload
    async def small_limit(sess, path, chunks):
        return await original(sess, path, chunks, max_bytes=4)
    monkeypatch.setattr(sshpool, "sftp_upload", small_limit)
    sid = store.execute("INSERT INTO ssh_sessions(name,host,port,username,secret) VALUES(?,?,?,?,?)",
        ("upload", ssh_server["host"], ssh_server["port"], "test", "test-password"))
    response = client.post(f"/api/ssh/sftp/upload?session_id={sid}&path=/too-big.bin",
                           content=b"12345")
    assert response.status_code == 413
    assert not (database / "too-big.bin").exists()


def test_batch_download_and_changed_target_guard(client, ssh_server, database):
    import io
    import zipfile
    body = {**ssh_server, "name":"batch", "secret":"test-password"}
    sid = client.post('/api/ssh/sessions', json=body).json()['id']
    row = next(s for s in client.get('/api/ssh/sessions').json()['data'] if s['id'] == sid)
    (database / '中文.txt').write_bytes(b'isolated-file')
    request = dict(session_id=sid, path='/', names=['binary.dat', '中文.txt'], expected_revision=row['connection_revision'])
    response = client.post('/api/ssh/sftp/download-batch', json=request)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('binary.dat') == bytes(range(256))
        assert archive.read('中文.txt') == b'isolated-file'
    assert client.post('/api/ssh/sftp/download-batch', json={**request, 'names':['../secret']}).status_code == 400
    body['host'] = 'other-vps.invalid'
    assert client.put(f'/api/ssh/sessions/{sid}', json=body).status_code == 200
    assert client.post('/api/ssh/sftp/download-batch', json=request).status_code == 409
    assert client.post(f'/api/ssh/sftp/upload?session_id={sid}&path=/blocked&expected_revision={row["connection_revision"]}', content=b'wrong').status_code == 409
    assert client.post('/api/ssh/sftp/write', json=dict(session_id=sid, path='/blocked', content='wrong', expected_revision=row['connection_revision'])).status_code == 409
    assert not (database / 'blocked').exists()
    revision = row['connection_revision']
    for route in ('list', 'read', 'download'):
        assert client.get(f'/api/ssh/sftp/{route}?session_id={sid}&path=/&expected_revision={revision}').status_code == 409
    assert client.post('/api/ssh/monitor', json=dict(session_id=sid, expected_revision=revision)).status_code == 409
    with client.websocket_connect(f'/ws/ssh?sid={sid}&expected_revision={revision}') as ws:
        assert '连接配置已变更' in ws.receive_text()


def test_upload_cancel_removes_partial_remote_file(ssh_server, database):
    async def chunks():
        yield b'partial'
        raise asyncio.CancelledError()
    async def cancel():
        with pytest.raises(asyncio.CancelledError):
            await sshpool.sftp_upload(ssh_server, '/cancelled.bin', chunks())
    sshpool._run(cancel())
    assert not (database / 'cancelled.bin').exists()


def test_batch_size_limit(ssh_server):
    with pytest.raises(sshpool.SshError, match='64 MB'):
        sshpool.sftp_download_batch(ssh_server, '/', ['binary.dat'], max_bytes=10)


def test_real_forward_stays_alive_after_create(ssh_server):
    async def echo(reader,writer):
        writer.write(await reader.read(20))
        await writer.drain()
        writer.close()
    async def create_echo():
        return await asyncio.start_server(echo,"127.0.0.1",0)
    server=sshpool._run(create_echo())
    port=server.sockets[0].getsockname()[1]
    result=sshpool.start_forward(ssh_server,"local",0,"127.0.0.1",port)
    local=next(i["local_port"] for i in sshpool.list_forwards() if i["id"]==result["id"])
    with socket.create_connection(("127.0.0.1",local),timeout=5) as sock:
        sock.sendall(b"forward-ok")
        assert sock.recv(20) == b"forward-ok"
    sshpool.stop_forward(result["id"])
    async def close():
        server.close()
        await server.wait_closed()
    sshpool._run(close())


def test_real_websocket_terminal(client, ssh_server):
    sid=store.execute("INSERT INTO ssh_sessions(name,host,port,username,secret) VALUES(?,?,?,?,?)",
        ("test",ssh_server["host"],ssh_server["port"],"test","test-password"))
    with client.websocket_connect(f"/ws/ssh?sid={sid}") as ws:
        ws.send_text("hello\n")
        assert "hello" in ws.receive_text()
        ws.send_text('{"resize":{"cols":80,"rows":24}}')
        ws.send_text("second\n")
        assert "second" in ws.receive_text()

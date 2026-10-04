"""Real local SOCKS transport tests; no cloud keys or external servers used."""
import asyncio
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import oci
import pytest
from oci._vendor import requests as sdk_requests
from test_ssh import ssh_server  # Reuse the real local SSH server fixture.
from starlette.websockets import WebSocketDisconnect

import oci_service
import sshpool
import store


def test_proxy_failure_hides_credentials_and_remains_retryable(account, monkeypatch):
    secret = "synthetic-proxy-secret"
    account["params"]["proxy_url"] = f"socks5h://test:{secret}@127.0.0.1:1080"
    session = sdk_requests.Session()
    def fail(*args, **kwargs):
        raise sdk_requests.exceptions.ProxyError(account["params"]["proxy_url"])
    monkeypatch.setattr(session, "send", fail)
    factory = lambda _: SimpleNamespace(base_client=SimpleNamespace(session=session))
    guarded = oci_service._client(factory, account).base_client.session
    with pytest.raises(oci_service.OciTransportError, match="未回退直连") as result:
        guarded.get("http://cloud.example.invalid/")
    assert secret not in str(result.value)
    assert oci_service.is_transient(result.value)
    assert oci_service.is_transient(sdk_requests.exceptions.ConnectionError("temporary"))


@pytest.mark.parametrize("key", [None, False, 123, []])
def test_account_rejects_invalid_key_type(client, key):
    response = client.post("/api/accounts", json={"name": "bad-key", "region": "ap-tokyo-1",
                                                "params": {"private_key": key}})
    assert response.status_code == 400


def test_ssh_ignores_server_wide_ssh_config(ssh_server, tmp_path, monkeypatch):
    from pathlib import Path
    config = tmp_path / "unexpected_ssh_config"
    config.write_text("Host *\n    ProxyCommand nonexistent-proxy-program\n")
    expanduser = Path.expanduser
    def isolated_home(path):
        if path == Path("~", ".ssh", "config"):
            return config
        return expanduser(path)
    monkeypatch.setattr(Path, "expanduser", isolated_home)
    assert sshpool.test_session(ssh_server) == "ok"


@pytest.mark.parametrize("value", [None, False, 0, [], {}])
def test_invalid_proxy_type_never_enables_direct_route(account, value):
    account["params"]["proxy_url"] = value
    with pytest.raises(oci_service.OciError, match="已阻止连接"):
        oci_service._client(oci.identity.IdentityClient, account)


def test_removing_account_proxy_requires_explicit_confirmation(client, credentials):
    body = {"name": "proxy-test", "region": "ap-singapore-1",
            "params": {**credentials, "proxy_url": "socks5h://127.0.0.1:1080"}}
    aid = client.post("/api/accounts", json=body).json()["id"]
    body["params"]["proxy_url"] = ""
    assert client.put(f"/api/accounts/{aid}", json=body).status_code == 400
    saved = store.query("SELECT * FROM accounts WHERE id=?", (aid,))[0]
    assert saved["params"]["proxy_url"] == "socks5h://127.0.0.1:1080"
    body["params"]["proxy_url"] = None
    assert client.put(f"/api/accounts/{aid}", json=body).status_code == 400
    body["params"]["proxy_url"] = ""
    body["remove_proxy"] = True
    assert client.put(f"/api/accounts/{aid}", json=body).status_code == 200
    saved = store.query("SELECT * FROM accounts WHERE id=?", (aid,))[0]
    assert saved["params"]["proxy_url"] == ""


@pytest.fixture
def socks_proxy():
    yield from _socks_proxy()


@pytest.fixture
def second_socks_proxy():
    yield from _socks_proxy()


def _socks_proxy():
    destinations = []
    connections = []
    async def serve(reader, writer):
        connections.append(writer)
        upstream = None
        try:
            version, size = await reader.readexactly(2)
            assert version == 5
            await reader.readexactly(size)
            writer.write(b"\x05\x00")
            await writer.drain()
            version, command, _, kind = await reader.readexactly(4)
            assert version == 5 and command == 1
            if kind == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
            elif kind == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            port = int.from_bytes(await reader.readexactly(2), "big")
            destinations.append((host, port))
            source, upstream = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            async def pump(src, dst):
                try:
                    while data := await src.read(65536):
                        dst.write(data)
                        await dst.drain()
                finally:
                    dst.close()
            await asyncio.gather(pump(reader, upstream), pump(source, writer))
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if upstream:
                upstream.close()
    async def start():
        return await asyncio.start_server(serve, "127.0.0.1", 0)
    server = sshpool._run(start())
    port = server.sockets[0].getsockname()[1]
    yield f"socks5://127.0.0.1:{port}", destinations, connections
    async def close():
        server.close()
        await server.wait_closed()
    sshpool._run(close())


@pytest.mark.parametrize("redirect", [False, True])
def test_oci_transport_forces_proxy_and_remote_dns(account, socks_proxy, monkeypatch, redirect):
    proxy, destinations, _ = socks_proxy
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if redirect and self.path == "/":
                self.send_response(302)
                self.send_header("Location", f"http://redirect.example.invalid:{self.server.server_port}/final")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"through-socks-only")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    account["params"]["proxy_url"] = proxy
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    client = oci_service._client(oci.identity.IdentityClient, account)
    session = client.base_client.session
    session.proxies.clear()  # Transport guard must still force the saved proxy.
    try:
        result = session.get(f"http://cloud.example.invalid:{server.server_port}/",
                             proxies={"http": None}, timeout=3)
        assert result.text == "through-socks-only"
        expected = [("cloud.example.invalid", server.server_port)]
        if redirect:
            expected.append(("redirect.example.invalid", server.server_port))
        assert destinations == expected
        assert not session.trust_env
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_oci_dead_proxy_does_not_reach_direct_target(account):
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(True)
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Bound but not listening: it cannot be reused by another process during this test.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        account["params"]["proxy_url"] = f"socks5://127.0.0.1:{unavailable.getsockname()[1]}"
        session = oci_service._client(oci.identity.IdentityClient, account).base_client.session
        try:
            with pytest.raises(oci_service.OciTransportError):
                session.get(f"http://127.0.0.1:{server.server_port}/", timeout=2)
            assert not hits
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(3)


def test_oci_proxy_setup_error_is_not_ignored(account):
    class BrokenSession:
        @property
        def proxies(self):
            return {}
        @proxies.setter
        def proxies(self, _):
            raise RuntimeError("synthetic-private-error")
    account["params"]["proxy_url"] = "socks5://127.0.0.1:1080"
    with pytest.raises(oci_service.OciError, match="已阻止连接") as result:
        oci_service._client(lambda _: SimpleNamespace(base_client=SimpleNamespace(session=BrokenSession())), account)
    assert "synthetic-private-error" not in str(result.value)


def test_two_oci_accounts_keep_routes_separate(account, socks_proxy, monkeypatch):
    proxy, destinations, _ = socks_proxy
    aid = store.execute("INSERT INTO accounts(name,region,params) VALUES(?,?,?)",
        ("second", account["region"], json.dumps({**account["params"], "proxy_url": proxy})))
    second = store.query("SELECT * FROM accounts WHERE id=?", (aid,))[0]
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.path.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    direct = oci_service._client(oci.identity.IdentityClient, account).base_client.session
    proxied = oci_service._client(oci.identity.IdentityClient, second).base_client.session
    try:
        assert direct is not proxied
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(direct.get, f"http://127.0.0.1:{server.server_port}/direct",
                timeout=3, proxies={"http": "http://127.0.0.1:1"})
            b = pool.submit(proxied.get, f"http://cloud.example.invalid:{server.server_port}/proxy", timeout=3)
            assert a.result().text == "/direct"
            assert b.result().text == "/proxy"
        assert destinations == [("cloud.example.invalid", server.server_port)]
        assert sorted(hits) == ["/direct", "/proxy"]
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            second["params"]["proxy_url"] = f"socks5://127.0.0.1:{unavailable.getsockname()[1]}"
            failed = oci_service._client(oci.identity.IdentityClient, second).base_client.session
            try:
                with pytest.raises(oci_service.OciTransportError):
                    failed.get(f"http://127.0.0.1:{server.server_port}/must-not-arrive", timeout=2)
                assert direct.get(f"http://127.0.0.1:{server.server_port}/still-direct", timeout=3).status_code == 200
                assert "/must-not-arrive" not in hits
                assert proxied.get(f"http://cloud.example.invalid:{server.server_port}/still-proxy", timeout=3).status_code == 200
            finally:
                failed.close()
    finally:
        direct.close()
        proxied.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_two_oci_accounts_use_different_proxies_concurrently(account, socks_proxy, second_socks_proxy, monkeypatch, tmp_path):
    import copy
    import ssl
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    proxy_a, destinations_a, connections_a = socks_proxy
    proxy_b, destinations_b, _ = second_socks_proxy
    first = copy.deepcopy(account)
    second = copy.deepcopy(account)
    first['params']['proxy_url'] = proxy_a
    second['params']['proxy_url'] = proxy_b
    second['params']['user_ocid'] = 'ocid1.user.oc1..' + 'c' * 60
    second['params']['fingerprint'] = ':'.join(['11'] * 16)
    second['params']['private_key'] = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    second['name'] = 'second-proxy-account'
    aid = store.execute('INSERT INTO accounts(name,region,params) VALUES(?,?,?)',
        (second['name'], second['region'], json.dumps(second['params'])))
    second = store.query('SELECT * FROM accounts WHERE id=?', (aid,))[0]
    store.execute('UPDATE accounts SET params=? WHERE id=?', (json.dumps(first['params']), first['id']))
    first = store.query('SELECT * FROM accounts WHERE id=?', (first['id'],))[0]
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'socks5h://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '*')
    requests_seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append((self.headers['Host'], self.headers.get('Authorization', ''), self.path))
            if self.path.endswith('/redirect'):
                self.send_response(302)
                self.send_header('Location', 'https://redirect-' + self.headers['Host'] + '/final')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'[]')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    key = serialization.load_pem_private_key(account['params']['private_key'].encode(), password=None)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'local-test')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                x509.DNSName(name) for name in ('account-a.example.invalid', 'account-b.example.invalid',
                    'redirect-account-a.example.invalid', 'redirect-account-b.example.invalid')]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'proxy-test-cert.pem', tmp_path / 'proxy-test-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_text(account['params']['private_key'])
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_path), str(key_path))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ca = oci_service._client(oci.identity.IdentityClient, first)
    cb = oci_service._client(oci.identity.IdentityClient, second)
    ca.base_client.endpoint = f'https://account-a.example.invalid:{server.server_port}'
    cb.base_client.endpoint = f'https://account-b.example.invalid:{server.server_port}'
    ca.base_client.session.verify = cb.base_client.session.verify = str(cert_path)
    try:
        assert ca.base_client.session is not cb.base_client.session
        assert ca.base_client.signer is not cb.base_client.signer
        def query(client):
            for _ in range(3):
                assert client.list_regions().data == []
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(query, ca), pool.submit(query, cb)
            a.result(timeout=10)
            b.result(timeout=10)
        assert destinations_a == [('account-a.example.invalid', server.server_port)] * 3
        assert destinations_b == [('account-b.example.invalid', server.server_port)] * 3
        for host, auth, _ in requests_seen:
            expected = first if host.startswith('account-a.') else second
            other = second if expected is first else first
            assert expected['params']['user_ocid'] in auth
            assert expected['params']['fingerprint'] in auth
            assert other['params']['user_ocid'] not in auth
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(ca.base_client.session.get, ca.base_client.endpoint + '/redirect', timeout=3)
            b = pool.submit(cb.base_client.session.get, cb.base_client.endpoint + '/redirect', timeout=3)
            assert a.result().status_code == b.result().status_code == 200
        assert destinations_a[-2:] == [('account-a.example.invalid', server.server_port),
                                       ('redirect-account-a.example.invalid', server.server_port)]
        assert destinations_b[-2:] == [('account-b.example.invalid', server.server_port),
                                       ('redirect-account-b.example.invalid', server.server_port)]
        # Failure of account A's newly saved proxy must not reach the direct target,
        # change account B's route, or mutate the already-created account A session.
        with socket.socket() as unavailable:
            unavailable.bind(('127.0.0.1', 0))
            first['params']['proxy_url'] = f'socks5h://127.0.0.1:{unavailable.getsockname()[1]}'
            failed = oci_service._client(oci.identity.IdentityClient, first).base_client.session
            try:
                with pytest.raises(oci_service.OciTransportError):
                    failed.get(f'http://127.0.0.1:{server.server_port}/must-not-arrive', timeout=2)
                assert cb.list_regions().data == []
                assert ca.list_regions().data == []
                assert destinations_b[-1] == ('account-b.example.invalid', server.server_port)
                assert destinations_a[-1] == ('account-a.example.invalid', server.server_port)
                assert all(path != '/must-not-arrive' for _, _, path in requests_seen)
            finally:
                failed.close()
    finally:
        ca.base_client.session.close()
        cb.base_client.session.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_two_ssh_connections_drop_only_failed_proxy(ssh_server, socks_proxy):
    proxy, destinations, connections = socks_proxy
    async def check():
        direct, proxied = await asyncio.gather(
            sshpool._connect(ssh_server),
            sshpool._connect({**ssh_server, "host": "cloud.example.invalid", "proxy_command": proxy}))
        try:
            assert (await direct.run("before", check=True)).stdout == "ok\n"
            assert (await proxied.run("before", check=True)).stdout == "ok\n"
            attempts = len(destinations)
            for writer in list(connections):
                writer.close()
            await asyncio.wait_for(proxied.wait_closed(), 3)
            assert not direct.is_closed()
            assert (await direct.run("after", check=True)).stdout == "ok\n"
            assert len(destinations) == attempts
        finally:
            direct.close()
            proxied.close()
            await asyncio.gather(direct.wait_closed(), proxied.wait_closed())
    sshpool._run(check())


def test_real_ssh_socks_proxy_includes_host_key_check(ssh_server, socks_proxy):
    proxy, destinations, _ = socks_proxy
    sess = {**ssh_server, "host": "cloud.example.invalid", "proxy_command": proxy}
    assert sshpool.test_session(sess) == "ok"
    assert destinations == [(sess["host"], sess["port"])] * 2
    proxy_port = proxy.rsplit(":", 1)[1]
    sess["proxy_command"] = f"nc -X 5 -x 127.0.0.1:{proxy_port} %h %p"
    assert sshpool.test_session(sess) == "ok"
    assert destinations[-1] == (sess["host"], sess["port"])


def test_active_ssh_closes_when_socks_proxy_disconnects(ssh_server, socks_proxy):
    proxy, destinations, connections = socks_proxy
    sess = {**ssh_server, "host": "cloud.example.invalid", "proxy_command": proxy}
    async def connect_and_drop():
        conn = await sshpool._connect(sess)
        attempts = len(destinations)
        for writer in list(connections):
            writer.close()
        await asyncio.wait_for(conn.wait_closed(), 3)
        assert len(destinations) == attempts
    sshpool._run(connect_and_drop())


@pytest.mark.parametrize("known_key", [False, True])
def test_ssh_dead_proxy_never_calls_direct_connector(database, monkeypatch, known_key):
    sess = {"host": "127.0.0.1", "port": 22, "auth_type": "password", "secret": "synthetic"}
    if known_key:
        store.set_setting("ssh_hostkey:127.0.0.1:22", asyncssh.generate_private_key("ssh-rsa").export_public_key().decode())
    key_probe, connector = AsyncMock(), AsyncMock()
    monkeypatch.setattr(asyncssh, "get_server_host_key", key_probe)
    monkeypatch.setattr(asyncssh, "connect", connector)
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        sess["proxy_command"] = f"socks5://127.0.0.1:{unavailable.getsockname()[1]}"
        with pytest.raises(sshpool.SshError, match="不会改为直连"):
            asyncio.run(sshpool._connect(sess))
    key_probe.assert_not_called()
    connector.assert_not_called()


@pytest.mark.parametrize("value", ["socks5://", "socks5://127.0.0.1:70000", "nc %h %p", "sh -c 'nc %h %p'"])
def test_invalid_ssh_proxy_is_blocked(value):
    with pytest.raises(sshpool.SshError, match="已阻止连接"):
        sshpool._socks_proxy({"proxy_command": value})


def test_ssh_socks_auth_rejection_never_starts_target_connection(database, monkeypatch):
    async def reject_auth(reader, writer):
        try:
            _, size = await reader.readexactly(2)
            await reader.readexactly(size)
            writer.write(b"\x05\x02")
            await writer.drain()
            _, size = await reader.readexactly(2)
            await reader.readexactly(size)
            size = (await reader.readexactly(1))[0]
            await reader.readexactly(size)
            writer.write(b"\x01\x01")
            await writer.drain()
        finally:
            writer.close()
    async def start():
        return await asyncio.start_server(reject_auth, "127.0.0.1", 0)
    server = sshpool._run(start())
    port = server.sockets[0].getsockname()[1]
    connector = AsyncMock()
    monkeypatch.setattr(asyncssh, "connect", connector)
    try:
        with pytest.raises(sshpool.SshError, match="不会改为直连"):
            sshpool.test_session({"host":"127.0.0.1", "port":22,
                "proxy_command":f"socks5h://test:synthetic@127.0.0.1:{port}"})
        connector.assert_not_called()
    finally:
        async def close():
            server.close()
            await server.wait_closed()
        sshpool._run(close())


@pytest.mark.parametrize("change", [{"proxy_command":"socks5h://127.0.0.1:1"},
                                    {"username":"other-account"}, {"secret":"new-secret"},
                                    {"host":"other.example.invalid"}])
def test_changing_saved_proxy_disconnects_existing_terminal(client, ssh_server, change):
    body = {"name":"proxy-change", "host":ssh_server["host"], "port":ssh_server["port"],
            "username":ssh_server["username"], "secret":ssh_server["secret"]}
    sid = client.post("/api/ssh/sessions", json=body).json()["id"]
    with client.websocket_connect(f"/ws/ssh?sid={sid}") as ws:
        ws.send_text("hello\n")
        assert "hello" in ws.receive_text()
        body.update(change)
        assert client.put(f"/api/ssh/sessions/{sid}", json=body).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_two_vps_credentials_files_and_proxy_failure_are_isolated(database, socks_proxy, second_socks_proxy):
    seen = []
    roots = [database / "vps-a", database / "vps-b"]
    for index, root in enumerate(roots):
        root.mkdir()
        (root / "identity.txt").write_text(str(index))
    def server_type(index):
        class IsolatedServer(asyncssh.SSHServer):
            def begin_auth(self, username): return True
            def password_auth_supported(self): return True
            def validate_password(self, username, password):
                seen.append((index, username, password))
                return username == f"user-{index}" and password == f"secret-{index}"
        return IsolatedServer
    async def check():
        servers, connections = [], []
        try:
            for index in range(2):
                servers.append(await asyncssh.create_server(server_type(index), "127.0.0.1", 0,
                    server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
                    sftp_factory=lambda channel, root=roots[index]: asyncssh.SFTPServer(channel, chroot=str(root))))
            sessions = [dict(host=f"vps-{i}.example.invalid", port=servers[i].get_port(),
                             username=f"user-{i}", secret=f"secret-{i}", auth_type="password",
                             proxy_command=proxy[0]) for i, proxy in enumerate((socks_proxy, second_socks_proxy))]
            connections.extend(await asyncio.gather(*(sshpool._connect(s) for s in sessions)))
            for i, conn in enumerate(connections):
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open("/identity.txt", "r") as remote:
                        assert await remote.read() == str(i)
                    async with sftp.open("/write.txt", "w") as remote:
                        await remote.write(f"session-{i}")
            assert sorted(seen) == [(i, f"user-{i}", f"secret-{i}") for i in range(2)]
            with pytest.raises(sshpool.SshError, match="认证失败"):
                await sshpool._connect({**sessions[1], "secret":"secret-0"})
            for writer in list(socks_proxy[2]): writer.close()
            await asyncio.wait_for(connections[0].wait_closed(), 3)
            assert not connections[1].is_closed()
            async with connections[1].start_sftp_client() as sftp:
                async with sftp.open("/identity.txt", "r") as remote:
                    assert await remote.read() == "1"
            for i, proxy in enumerate((socks_proxy, second_socks_proxy)):
                assert proxy[1] and all(target == (sessions[i]["host"], sessions[i]["port"]) for target in proxy[1])
            for i, root in enumerate(roots): assert (root / "write.txt").read_text() == f"session-{i}"
        finally:
            for conn in connections: conn.close()
            await asyncio.gather(*(conn.wait_closed() for conn in connections))
            for server in servers: server.close()
            await asyncio.gather(*(server.wait_closed() for server in servers))
    sshpool._run(check())


def test_ssh_authentication_does_not_use_default_keys_or_agent(ssh_server, monkeypatch):
    import os
    monkeypatch.setenv("SSH_AUTH_SOCK", os.path.join(os.getcwd(), "unexpected-agent"))
    args = sshpool._connect_args(ssh_server)
    assert args["agent_path"] is None and args["client_keys"] == []
    assert sshpool.test_session(ssh_server) == "ok"
    with pytest.raises(sshpool.SshError, match="认证失败"):
        sshpool.test_session({**ssh_server, "secret":"incorrect"})

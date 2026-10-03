"""Real local SOCKS transport tests; no cloud keys or external servers used."""
import asyncio
import socket
import threading
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


@pytest.fixture
def socks_proxy():
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


def test_oci_transport_forces_proxy_and_remote_dns(account, socks_proxy, monkeypatch):
    proxy, destinations, _ = socks_proxy
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
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
        assert destinations == [("cloud.example.invalid", server.server_port)]
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
            with pytest.raises((oci_service.requests.RequestException, sdk_requests.RequestException)):
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


def test_changing_saved_proxy_disconnects_existing_terminal(client, ssh_server):
    body = {"name":"proxy-change", "host":ssh_server["host"], "port":ssh_server["port"],
            "username":ssh_server["username"], "secret":ssh_server["secret"]}
    sid = client.post("/api/ssh/sessions", json=body).json()["id"]
    with client.websocket_connect(f"/ws/ssh?sid={sid}") as ws:
        ws.send_text("hello\n")
        assert "hello" in ws.receive_text()
        body["proxy_command"] = "socks5h://127.0.0.1:1"
        assert client.put(f"/api/ssh/sessions/{sid}", json=body).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()

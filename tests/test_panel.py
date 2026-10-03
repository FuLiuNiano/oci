import json
from unittest.mock import patch

import pytest

import store
import tasks
import oci_service
import deps


def test_login_logout_password_revokes_old_cookie(client):
    import main
    cookie = client.cookies.get(main.COOKIE)
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/settings/password", json={"old_password": client.initial_password,
                                                    "new_password":"n" * 32}).status_code == 200
    assert client.get("/api/me").status_code == 401
    assert not main.deps.check_token(cookie)
    assert client.post("/api/login", json={"username": client.initial_username,
                                          "password":"n" * 32}).status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/accounts").status_code == 401


def test_login_rate_limit(client):
    client.post("/api/logout")
    for _ in range(10):
        assert client.post("/api/login", json={"username": client.initial_username,
                                              "password":"wrong"}).status_code == 401
    assert client.post("/api/login", json={"username": client.initial_username,
                                          "password":"wrong"}).status_code == 429


def test_secret_entry_guards_page_api_static_and_websocket(client):
    import main
    from fastapi.testclient import TestClient
    assert len(client.prefix.removeprefix("/")) == 32
    assert len(client.initial_username) == 32
    assert len(client.initial_password) == 32
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    with TestClient(main.app) as raw:
        assert raw.get("/").status_code == 404
        assert raw.get("/api/me").status_code == 404
        assert raw.get("/static/app.js").status_code == 404
        assert raw.get("/healthz").status_code == 200
        assert raw.get(client.prefix, follow_redirects=False).status_code == 308
        assert raw.get(client.prefix + "/").status_code == 200
        with pytest.raises(Exception):
            with raw.websocket_connect("/ws/ssh?sid=1"):
                pass


def test_login_requires_username_and_cookie_is_scoped(client):
    import main
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "wrong", "password": client.initial_password}).status_code == 401
    result = client.post("/api/login", json={"username": client.initial_username,
                                              "password": client.initial_password})
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert f"Path={client.prefix}/" in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/logout", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.post("/api/login", headers={"Origin": "https://untrusted.example"}, json={
        "username": client.initial_username, "password": client.initial_password}).status_code == 403


def test_old_password_install_rotates_login_without_touching_cloud_data(database):
    store.set_setting("admin_pass", "legacy-hash")
    store.set_setting("secret", "old-session-secret")
    old = database / "initial_admin_password.txt"
    old.write_text("obsolete", encoding="utf-8")
    store.execute("INSERT INTO accounts(name, region, params) VALUES(?,?,?)",
                  ("saved", "us-phoenix-1", "{}"))
    deps.init_auth()
    assert not old.exists()
    assert (database / "initial_admin_credentials.txt").exists()
    assert len(store.get_setting("admin_user")) == 32
    assert len(store.get_setting("access_path")) == 32
    assert store.get_setting("secret") != "old-session-secret"
    assert store.query("SELECT name FROM accounts")[0]["name"] == "saved"


def test_account_add_edit_copy_keeps_credentials(client, credentials):
    body = {"name":"first", "region":"ap-singapore-1", "params":credentials}
    result = client.post("/api/accounts", json=body)
    assert result.status_code == 200, result.text
    aid = result.json()["id"]
    row = client.get("/api/accounts").json()["data"][0]
    assert row["params"]["fingerprint"] == credentials["fingerprint"]
    assert row["params"]["user_ocid"] == credentials["user_ocid"]
    assert "private_key" not in row["params"]
    body["params"] = {**row["params"], "private_key":""}
    body["name"] = "edited"
    assert client.put(f"/api/accounts/{aid}", json=body).status_code == 200
    saved = store.query("SELECT * FROM accounts WHERE id=?", (aid,))[0]
    assert saved["params"]["private_key"] == credentials["private_key"]
    assert saved["params"]["fingerprint"] == credentials["fingerprint"]
    r = client.post(f"/api/accounts/{aid}/copy-region", json={"region":"ap-tokyo-1"})
    assert r.status_code == 200, r.text
    copied = store.query("SELECT * FROM accounts WHERE id=?", (r.json()["id"],))[0]
    assert copied["params"] == saved["params"]


def test_reject_unsupported_platform_and_bad_key(client):
    assert client.post("/api/accounts", json={"name":"bad", "platform":"aws"}).status_code == 400
    assert client.post("/api/accounts", json={"name":"bad", "region":"ap-singapore-1",
        "params":{"private_key":"-----BEGIN PRIVATE KEY-----\nfake"}}).status_code == 400
    assert client.get("/api/cloud/aws/instances?account_id=1").status_code == 400


def test_post_origin_validation(client):
    assert client.post("/api/settings/alerts", headers={"Origin":"https://untrusted.example"},
                       json={}).status_code == 403


def test_api_reboot_accepts_ui_action(client, account, monkeypatch):
    monkeypatch.setattr(oci_service, "instance_action", lambda *args: {"ok":True})
    assert client.post("/api/cloud/oci/action", json={"account_id":account["id"],
        "instance_id":"instance1", "action":"REBOOT"}).status_code == 200


def test_launch_requires_public_key_and_success_cannot_resume(client, account):
    body = {"account_id":account["id"]}
    assert client.post("/api/launch-tasks", json=body).status_code == 400
    body["ssh_key"] = "ssh-ed25519 test"
    r = client.post("/api/launch-tasks", json=body)
    assert r.status_code == 200
    tid = r.json()["id"]
    task = store.query("SELECT * FROM launch_tasks WHERE id=?",(tid,))[0]
    assert task["retry_token"]
    store.execute("UPDATE launch_tasks SET status='success' WHERE id=?",(tid,))
    assert client.post(f"/api/launch-tasks/{tid}/start").status_code == 400


def test_traffic_stop_blocks_auto_restart_after_worker_restart(account, monkeypatch):
    store.execute("UPDATE accounts SET auto_restart=1, traffic_limit_gb=1, traffic_action='stop' WHERE id=?",
                  (account["id"],))
    state = ["RUNNING"]
    actions = []
    monkeypatch.setattr(oci_service,"list_instances",lambda a:[{"id":"instance1","name":"test","state":state[0]}])
    monkeypatch.setattr(oci_service,"traffic_usage_gb",lambda *a,**k:{"total_gb":2, "per_resource":{}})
    monkeypatch.setattr(oci_service,"instance_action",lambda a,i,action:actions.append(action))
    monkeypatch.setattr(tasks.time,"sleep",lambda _:None)
    tasks._tick_traffic()
    assert actions == ["SOFTSTOP"]
    state[0] = "STOPPED"
    tasks._tick_auto_restart()
    assert actions == ["SOFTSTOP"]
    assert store.get_json_setting(f"traffic_block:{account['id']}") == ["instance1"]


def test_manual_stop_is_not_restarted(client, account, monkeypatch):
    actions=[]
    store.execute("UPDATE accounts SET auto_restart=1 WHERE id=?",(account["id"],))
    monkeypatch.setattr(oci_service,"instance_action",lambda a,i,action:actions.append(action) or {"ok":True})
    client.post("/api/cloud/oci/action",json={"account_id":account["id"],"instance_id":"instance1","action":"STOP"})
    monkeypatch.setattr(oci_service,"list_instances",lambda a:[{"id":"instance1","name":"test","state":"STOPPED"}])
    tasks._tick_auto_restart()
    assert actions == ["STOP"]


def test_mcp_auth_and_malformed_request(client):
    assert client.post("/mcp",json={}).status_code == 401
    h={"Authorization":"Bearer "+store.get_setting("mcp_token")}
    assert client.post("/mcp",headers=h,json=[]).status_code == 202
    assert client.post("/mcp",headers=h,json=42).status_code == 400
    r=client.post("/mcp",headers=h,json={"jsonrpc":"2.0","id":1,"method":"tools/list"})
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 5


def test_static_files_and_health(client):
    assert client.get("/healthz").json()["ok"]
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200

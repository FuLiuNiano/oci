import copy
import pytest

import oci_ssh
import oci_service
import store


def saved_session(account, host="203.0.113.2", tag=None):
    return store.execute("INSERT INTO ssh_sessions(name,host,secret,proxy_command,tags) VALUES(?,?,?,?,?)",
                         ("saved", host, "synthetic-password", "socks5h://127.0.0.1:1080", tag or "oci:" + account["name"]))


def machine(**kwargs):
    return {"id": "instance1", "name": "instance", "public_ip": "203.0.113.2",
            "ipv6_addresses": ["2001:db8::10"], **kwargs}


def test_ipv6_sync_keeps_session_connection_unchanged(client, account):
    sid = saved_session(account)
    before = client.get("/api/ssh/sessions").json()["data"][0]
    assert oci_ssh.sync_instance(account, machine())["updated"] == 1
    after = client.get("/api/ssh/sessions").json()["data"][0]
    assert after["metadata"]["ipv6_addresses"] == ["2001:db8::10"]
    assert after["oci_account_id"] == account["id"] and after["oci_instance_id"] == "instance1"
    assert before["connection_revision"] == after["connection_revision"]
    raw = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0]
    assert raw["host"] == "203.0.113.2" and raw["secret"] == "synthetic-password"
    assert raw["proxy_command"] == "socks5h://127.0.0.1:1080"


def test_same_ip_different_account_never_updates_other_session(account):
    sid = saved_session(account)
    oci_ssh.sync_instance(account, machine())
    second = copy.deepcopy(account)
    second["id"] = store.execute("INSERT INTO accounts(name,region,params) VALUES(?,?,?)",
        ("other", account["region"], store.json.dumps(account["params"])))
    second["name"] = "other"
    result = oci_ssh.sync_instance(second, machine(ipv6_addresses=["2001:db8::20"]), create=True)
    assert result["session_id"] != sid
    assert oci_ssh.metadata_of(store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0])["ipv6_addresses"] == ["2001:db8::10"]
    oci_ssh.sync_instance(account, machine(ipv6_addresses=["2001:db8::30"]))
    other = store.query("SELECT * FROM ssh_sessions WHERE id=?", (result["session_id"],))[0]
    assert oci_ssh.metadata_of(other)["ipv6_addresses"] == ["2001:db8::20"]


def test_unrelated_manual_session_not_adopted(account):
    sid = saved_session(account, tag="manual")
    assert oci_ssh.sync_instance(account, machine())["updated"] == 0
    assert store.query("SELECT metadata FROM ssh_sessions WHERE id=?", (sid,))[0]["metadata"] == "{}"


def test_ambiguous_legacy_sessions_not_adopted(account):
    saved_session(account)
    saved_session(account)
    assert oci_ssh.sync_instance(account, machine())["updated"] == 0


def test_cloud_refresh_and_ipv6_operation_update_existing_ssh(client, account, monkeypatch):
    sid = saved_session(account)
    monkeypatch.setattr(oci_service, "list_instances", lambda a: [machine()])
    assert client.get(f'/api/cloud/oci/instances?account_id={account["id"]}').status_code == 200
    monkeypatch.setattr(oci_service, "attach_ipv6", lambda a, i: machine(ipv6_addresses=["2001:db8::20"], ipv6="2001:db8::20"))
    response = client.post("/api/oci/ipv6", json={"account_id": account["id"], "instance_id": "instance1"})
    assert response.status_code == 200 and response.json()["ssh_sync"]["session_id"] == sid
    assert oci_ssh.metadata_of(store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0])["ipv6_addresses"] == ["2001:db8::20"]


def test_public_api_uses_selected_account_and_source(client, account, monkeypatch):
    observed = []
    def public(a, instance_id, source, port):
        observed.append((a["id"], instance_id, source, port))
        return machine(ipv6="2001:db8::10", message="configured")
    monkeypatch.setattr(oci_service, "configure_ipv6_public", public)
    response = client.post("/api/oci/ipv6/public", json={"account_id": account["id"], "instance_id": "instance1",
                                                        "ssh_source": "2001:db8::1/128", "ssh_port": 2222})
    assert response.status_code == 200
    assert observed == [(account["id"], "instance1", "2001:db8::1/128", 2222)]


def test_explicit_binding_of_legacy_session_preserves_route(client, account, sdk):
    from test_ipv6_public import public_network
    public_network(sdk)
    sid = saved_session(account, tag="oci:old-account-name")
    response = client.post("/api/oci/ipv6/public", json={"account_id":account["id"], "instance_id":"instance1", "ssh_session_id":sid})
    assert response.status_code == 200
    assert response.json()["ssh_sync"]["session_id"] == sid
    row = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0]
    assert row["proxy_command"] == "socks5h://127.0.0.1:1080" and row["host"] == "203.0.113.2"
    assert oci_ssh.metadata_of(row)["ipv6_addresses"] == ["2001:db8::10"]


def test_wrong_ssh_target_rejected_before_cloud_write(client, account, sdk):
    from test_ipv6_public import public_network
    _, calls, *_ = public_network(sdk)
    sid = saved_session(account, host="203.0.113.99", tag="manual")
    response = client.post("/api/oci/ipv6/public", json={"account_id":account["id"], "instance_id":"instance1", "ssh_session_id":sid})
    assert response.status_code == 400 and "主机地址" in response.json()["detail"]
    assert not any(c[0].startswith("update_") for c in calls)


def test_other_account_binding_cannot_be_taken_over(account):
    sid = saved_session(account)
    oci_ssh.sync_instance(account, machine())
    other = {**account, "id":account["id"] + 1}
    with pytest.raises(oci_service.OciError, match="其他账号"):
        oci_ssh.validate_binding(other, machine(), sid)


def test_other_cloud_session_cannot_be_bound(account):
    sid = saved_session(account, tag="aws:test")
    store.execute("UPDATE ssh_sessions SET metadata=? WHERE id=?", ('{"source":"aws"}', sid))
    with pytest.raises(oci_service.OciError, match="其他云平台"):
        oci_ssh.validate_binding(account, machine(), sid)


def test_repeated_private_ip_is_not_proof_for_manual_binding(account):
    sid = saved_session(account, host="10.0.0.2", tag="manual")
    with pytest.raises(oci_service.OciError, match="公网地址"):
        oci_ssh.validate_binding(account, machine(private_ip="10.0.0.2"), sid)


@pytest.mark.parametrize("metadata", [
    {"cloud_provider": "oci", "cloud_account_id": -1},
    {"cloud_provider": "aws"},
    {"cloud_provider": "gcp"},
])
def test_legacy_tag_cannot_override_owner_metadata(account, metadata):
    sid = saved_session(account)
    store.execute("UPDATE ssh_sessions SET metadata=? WHERE id=?", (store.json.dumps(metadata), sid))
    before = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0]
    assert oci_ssh.sync_instance(account, machine())["updated"] == 0
    assert store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0] == before
    assert not store.query("SELECT * FROM oci_ssh_links WHERE session_id=?", (sid,))


def test_legacy_private_address_is_not_automatically_adopted(account):
    sid = saved_session(account, host="10.0.0.2")
    assert oci_ssh.sync_instance(account, machine(private_ip="10.0.0.2"))["updated"] == 0
    assert not store.query("SELECT * FROM oci_ssh_links WHERE session_id=?", (sid,))


@pytest.mark.parametrize("field,value", [("cloud_region", "us-phoenix-1"), ("cloud_instance_id", "other-instance")])
def test_unlinked_session_keeps_region_and_instance_ownership(account, field, value):
    sid = saved_session(account)
    metadata = {"cloud_provider": "oci", "cloud_account_id": account["id"], field: value}
    store.execute("UPDATE ssh_sessions SET metadata=? WHERE id=?", (store.json.dumps(metadata), sid))
    assert oci_ssh.sync_instance(account, machine())["updated"] == 0
    with pytest.raises(oci_service.OciError, match="其他区域或实例"):
        oci_ssh.validate_binding(account, machine(), sid)


def test_cache_revision_isolated_and_changes_with_route_or_credentials(account):
    before = store.account_public(account)
    assert before['cache_revision'] == store.account_public(account)['cache_revision']
    assert before['cache_revision'] != store.account_public({**account, 'id': account['id']+1})['cache_revision']
    for changed in ({**account, 'region': 'us-phoenix-1'},
                    {**account, 'params': {**account['params'], 'proxy_url': 'socks5h://test:1080'}},
                    {**account, 'params': {**account['params'], 'private_key': 'changed-key'}}):
        assert before['cache_revision'] != store.account_public(changed)['cache_revision']
    assert 'private_key' not in before['params']

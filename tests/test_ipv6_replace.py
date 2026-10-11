import copy

import oci
import pytest

import oci_service as service
import oci_ssh
import store
from test_ipv6 import setup_network
from test_oci_ssh import saved_session, machine


def replacement_network(sdk):
    data, calls, _, _ = setup_network(sdk, enabled=True)
    old = oci.core.models.Ipv6(id="old6", vnic_id="vnic1", ip_address="2001:db8:1234:1::10",
                              lifecycle_state="AVAILABLE", route_table_id="route-special")
    other = oci.core.models.Ipv6(id="other6", vnic_id="vnic1", ip_address="2001:db8:1234:1::20")
    new = oci.core.models.Ipv6(id="new6", vnic_id="vnic1", ip_address="2001:db8:1234:1::30",
                              lifecycle_state="AVAILABLE")
    data.update(list_ipv6s=[old, other], create_ipv6=new, delete_ipv6=None)
    def get(*args, **kw):
        item = old if kw["path_params"]["ipv6Id"] == old.id else new
        return oci.response.Response(200, {"etag":"old-version"}, item, None)
    data["get_ipv6"] = get
    return data, calls, old, new


def test_replace_confirm_new_before_delete_preserves_other_addresses(sdk, account):
    data, calls, old, new = replacement_network(sdk)
    result = service.replace_ipv6(account, "instance1", old.ip_address)
    assert result["replaced"] and result["old_ip"] == old.ip_address
    assert result["ipv6_addresses"] == [new.ip_address, "2001:db8:1234:1::20"]
    operations = [c[0] for c in calls]
    assert operations.index("create_ipv6") < len(operations)-1
    assert operations[-2:] == ["get_ipv6", "delete_ipv6"]
    create = next(c[2] for c in calls if c[0] == "create_ipv6")
    assert create["body"].vnic_id == "vnic1" and create["body"].route_table_id == "route-special"
    assert create["body"].ipv6_subnet_cidr == "2001:db8:1234:1::/64"
    assert create["header_params"]["opc-retry-token"]
    delete = calls[-1][2]
    assert delete["path_params"]["ipv6Id"] == old.id
    assert delete["header_params"]["if-match"] == "old-version"
    assert not any(c[0].startswith(("add_", "update_")) or c[0] == "delete_public_ip" for c in calls)


def test_allocation_failure_preserves_old(sdk, account):
    data, calls, old, _ = replacement_network(sdk)
    data["create_ipv6"] = oci.exceptions.ServiceError(400, "LimitExceeded", {}, "address limit")
    with pytest.raises(service.OciError, match="未删除旧 IPv6"):
        service.replace_ipv6(account, "instance1", old.ip_address)
    assert not any(c[0] == "delete_ipv6" for c in calls)


def test_delete_conflict_returns_both_for_metadata_sync(sdk, account):
    data, calls, old, new = replacement_network(sdk)
    data["delete_ipv6"] = oci.exceptions.ServiceError(412, "NoEtagMatch", {}, "changed")
    result = service.replace_ipv6(account, "instance1", old.ip_address)
    assert not result["replaced"]
    assert old.ip_address in result["ipv6_addresses"] and new.ip_address in result["ipv6_addresses"]
    assert sum(c[0] == "delete_ipv6" for c in calls) == 1


@pytest.mark.parametrize("reason", ["foreign", "ssh", "etag", "vnic"])
def test_unsafe_target_prevents_all_writes(sdk, account, reason):
    data, calls, old, new = replacement_network(sdk)
    address = old.ip_address
    if reason == "foreign":
        address = "2001:db8::99"
    elif reason == "ssh":
        saved_session(account, host=address)
    elif reason == "etag":
        data["get_ipv6"] = old
    else:
        old.vnic_id = "foreign-vnic"
    with pytest.raises(service.OciError):
        service.replace_ipv6(account, "instance1", address)
    assert not any(c[0] in ("create_ipv6", "delete_ipv6") for c in calls)


def test_bad_new_address_never_deletes_old(sdk, account):
    _, calls, old, new = replacement_network(sdk)
    new.vnic_id = "foreign-vnic"
    with pytest.raises(service.OciError, match="新地址确认异常"):
        service.replace_ipv6(account, "instance1", old.ip_address)
    assert not any(c[0] == "delete_ipv6" for c in calls)


def test_concurrent_replacements_keep_account_credentials_and_proxy_routes(account, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    first = copy.deepcopy(account)
    second = copy.deepcopy(account)
    second["id"] = 2
    second["params"]["user_ocid"] = "ocid1.user.oc1.." + "c" * 60
    second["params"]["proxy_url"] = "socks5://127.0.0.1:1080"
    states, observed = {}, []
    for a in (first, second):
        data, _, old, new = replacement_network(({}, []))
        suffix = str(a["id"])
        data["get_vnic"].id = "vnic" + suffix
        data["list_vnic_attachments"][0].vnic_id = "vnic" + suffix
        old.vnic_id = new.vnic_id = "vnic" + suffix
        old.id = "old" + suffix
        new.id = "new" + suffix
        states[a["id"]] = (data, old.ip_address)
    original = service._client
    def client(cls, a):
        obj = original(cls, a)
        def call(*args, **kwargs):
            op = kwargs["operation_name"]
            observed.append((a["id"], op, dict(obj.base_client.session.proxies),
                             obj.base_client.session.trust_env, obj.base_client.signer.api_key, kwargs))
            value = states[a["id"]][0][op]
            if callable(value):
                return value(*args, **kwargs)
            return oci.response.Response(200, {}, value, None)
        obj.base_client.call_api = call
        return obj
    monkeypatch.setattr(service, "_client", client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda a: service.replace_ipv6(a, "instance" + str(a["id"]),
                                states[a["id"]][1]), (first, second)))
    assert all(r["replaced"] for r in results)
    for aid, op, proxies, trust_env, signer, request in observed:
        expected = {} if aid == first["id"] else {"http":"socks5h://127.0.0.1:1080", "https":"socks5h://127.0.0.1:1080"}
        assert proxies == expected and not trust_env
        own = first if aid == first["id"] else second
        assert own["params"]["user_ocid"] in signer
        if op == "create_ipv6":
            assert request["body"].vnic_id == "vnic" + str(aid)
        if op == "delete_ipv6":
            assert request["path_params"]["ipv6Id"] == "old" + str(aid)


def test_replace_api_updates_only_own_link_without_changing_ssh_route(client, account, monkeypatch):
    sid = saved_session(account)
    oci_ssh.sync_instance(account, machine())
    second = copy.deepcopy(account)
    second["id"] = store.execute("INSERT INTO accounts(name,region,params) VALUES(?,?,?)",
        ("other", account["region"], store.json.dumps(account["params"])))
    second["name"] = "other"
    other = saved_session(second)
    oci_ssh.sync_instance(second, machine())
    before = store.query("SELECT * FROM ssh_sessions ORDER BY id")
    calls = []
    def replace(a, instance_id, old_address):
        calls.append((a["id"], instance_id, old_address))
        return machine(ipv6_addresses=["2001:db8::30"], replaced=True, message="done")
    monkeypatch.setattr(service, "replace_ipv6", replace)
    response = client.post("/api/oci/ipv6/replace", json={"account_id":account["id"],
        "instance_id":"instance1", "old_address":"2001:db8::10"})
    assert response.status_code == 200 and response.json()["ssh_sync"]["session_id"] == sid
    assert calls == [(account["id"], "instance1", "2001:db8::10")]
    after = store.query("SELECT * FROM ssh_sessions ORDER BY id")
    assert after[1] == before[1] and after[1]["id"] == other
    for key in ("host", "port", "username", "secret", "proxy_command"):
        assert before[0][key] == after[0][key]
    assert oci_ssh.metadata_of(after[0])["ipv6_addresses"] == ["2001:db8::30"]

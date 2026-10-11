"""Exercise automatic IPv6 setup through the real OCI SDK with stubbed cloud transport."""
import oci
import pytest

import oci_service as service
from test_oci import network


def setup_network(sdk, enabled=False):
    data, calls = sdk
    network(data)
    vcn = oci.core.models.Vcn(id="vcn1", compartment_id="comp1", lifecycle_state="AVAILABLE",
                             ipv6_cidr_blocks=["2001:db8:1234::/56"] if enabled else [])
    subnet = oci.core.models.Subnet(id="subnet1", vcn_id="vcn1", compartment_id="comp1",
                                   lifecycle_state="AVAILABLE",
                                   ipv6_cidr_block="2001:db8:1234:1::/64" if enabled else None)
    data.update(get_vcn=vcn, get_subnet=subnet, list_ipv6s=[], list_subnets=[subnet],
                create_ipv6=oci.core.models.Ipv6(ip_address="2001:db8:1234:1::10"))

    def enable_vcn(*args, **kwargs):
        vcn.ipv6_cidr_blocks = ["2001:db8:1234::/56"]
        return oci.response.Response(200, {}, None, None)

    def enable_subnet(*args, **kwargs):
        subnet.ipv6_cidr_block = kwargs["body"].ipv6_cidr_block
        return oci.response.Response(200, {}, None, None)

    data.update(add_ipv6_vcn_cidr=enable_vcn, add_ipv6_subnet_cidr=enable_subnet)
    return data, calls, vcn, subnet


def test_one_click_enables_network_and_allocates_unused_subnet(sdk, account):
    data, calls, vcn, subnet = setup_network(sdk)
    data["list_subnets"].append(oci.core.models.Subnet(ipv6_cidr_block="2001:db8:1234::/64"))
    result = service.attach_ipv6(account, "instance1")
    assert result["ipv6"] == "2001:db8:1234:1::10"
    assert subnet.ipv6_cidr_block == "2001:db8:1234:1::/64"
    writes = [c for c in calls if c[0].startswith("add_") or c[0] == "create_ipv6"]
    assert [c[0] for c in writes] == ["add_ipv6_vcn_cidr", "add_ipv6_subnet_cidr", "create_ipv6"]
    assert writes[0][2]["body"].is_oracle_gua_allocation_enabled is True
    assert writes[-1][2]["body"].vnic_id == "vnic1"
    assert writes[-1][2]["body"].ipv6_subnet_cidr == subnet.ipv6_cidr_block
    assert all(c[2]["header_params"].get("opc-retry-token") for c in writes)
    assert not any(c[0].startswith(("delete_", "update_")) for c in calls)


def test_enabled_network_is_preserved(sdk, account):
    _, calls, _, _ = setup_network(sdk, enabled=True)
    service.attach_ipv6(account, "instance1")
    assert not any(c[0].startswith("add_") for c in calls)


def test_waits_for_network_update_before_assigning_address(sdk, account, monkeypatch):
    data, calls, vcn, _ = setup_network(sdk)
    reads = []
    def get_vcn(*args, **kwargs):
        reads.append(True)
        current = oci.core.models.Vcn(
            id=vcn.id, compartment_id=vcn.compartment_id,
            ipv6_cidr_blocks=vcn.ipv6_cidr_blocks,
            lifecycle_state="UPDATING" if len(reads) == 2 else "AVAILABLE")
        return oci.response.Response(200, {}, current, None)
    data["get_vcn"] = get_vcn
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    service.attach_ipv6(account, "instance1")
    assert len(reads) == 3
    operations = [c[0] for c in calls]
    assert operations.index("add_ipv6_subnet_cidr") > max(i for i, op in enumerate(operations) if op == "get_vcn")


def test_enabled_vcn_only_adds_subnet(sdk, account):
    _, calls, vcn, _ = setup_network(sdk)
    vcn.ipv6_cidr_blocks = ["2001:db8:1234::/56"]
    service.attach_ipv6(account, "instance1")
    assert not any(c[0] == "add_ipv6_vcn_cidr" for c in calls)
    assert any(c[0] == "add_ipv6_subnet_cidr" for c in calls)


def test_repeated_click_reuses_existing_ipv6(sdk, account):
    data, calls, _, _ = setup_network(sdk, enabled=True)
    data["list_ipv6s"] = [oci.core.models.Ipv6(ip_address="2001:db8:1234:1::20")]
    result = service.attach_ipv6(account, "instance1")
    assert result["reused"] and result["ipv6"] == "2001:db8:1234:1::20"
    assert not any(c[0].startswith(("add_", "create_")) for c in calls)


def test_partial_failure_can_resume_without_reallocating_vcn(sdk, account):
    data, calls, _, _ = setup_network(sdk)
    original = data["add_ipv6_subnet_cidr"]
    data["add_ipv6_subnet_cidr"] = oci.exceptions.ServiceError(403, "NotAuthorized", {}, "no permission")
    with pytest.raises(service.OciError, match="开启子网 IPv6失败.*已提交的 VCN IPv6"):
        service.attach_ipv6(account, "instance1")
    assert not any(c[0] == "create_ipv6" for c in calls)
    data["add_ipv6_subnet_cidr"] = original
    service.attach_ipv6(account, "instance1")
    assert sum(c[0] == "add_ipv6_vcn_cidr" for c in calls) == 1


def test_no_available_prefix_does_not_mutate_existing_network(sdk, account):
    data, calls, vcn, _ = setup_network(sdk)
    vcn.ipv6_cidr_blocks = ["2001:db8:1234::/64"]
    data["list_subnets"].append(oci.core.models.Subnet(ipv6_cidr_block="2001:db8:1234::/64"))
    with pytest.raises(service.OciError, match="没有可用"):
        service.attach_ipv6(account, "instance1")
    assert not any(c[0].startswith(("add_", "create_")) for c in calls)


def test_expected_ipv6_error_keeps_specific_message(client, account, monkeypatch):
    def fail(*args):
        raise service.OciError("开启子网 IPv6失败：OCI 403 NotAuthorized: no permission")
    monkeypatch.setattr(service, "attach_ipv6", fail)
    response = client.post("/api/oci/ipv6", json={"account_id": account["id"], "instance_id": "instance1"})
    assert response.status_code == 400
    assert "开启子网" in response.json()["detail"]


def test_two_accounts_keep_ipv6_clients_and_routes_separate(account, monkeypatch):
    import copy
    from types import SimpleNamespace
    from concurrent.futures import ThreadPoolExecutor
    first = copy.deepcopy(account)
    second = copy.deepcopy(account)
    second["id"] = 2
    second["region"] = "ap-tokyo-1"
    second["params"]["user_ocid"] = "ocid1.user.oc1.." + "c" * 60
    second["params"]["proxy_url"] = "socks5://127.0.0.1:1080"
    states, observed = {}, []
    for a in (first, second):
        data, _, vcn, subnet = setup_network(({}, []))
        suffix = str(a["id"])
        vcn.id = "vcn" + suffix
        subnet.id, subnet.vcn_id = "subnet" + suffix, vcn.id
        data["get_vnic"].id = "vnic" + suffix
        data["get_vnic"].subnet_id = subnet.id
        data["list_vnic_attachments"][0].vnic_id = data["get_vnic"].id
        data["create_ipv6"].ip_address = "2001:db8::" + suffix
        states[a["id"]] = data
    original = service._client

    def client(cls, a):
        obj = original(cls, a)
        def call(*args, **kwargs):
            operation = kwargs["operation_name"]
            session = obj.base_client.session
            observed.append((a["id"], operation, dict(session.proxies), session.trust_env,
                             obj.base_client.signer.api_key, kwargs))
            result = states[a["id"]][operation]
            if callable(result):
                return result(*args, **kwargs)
            return oci.response.Response(200, {}, result, SimpleNamespace(method="GET"))
        obj.base_client.call_api = call
        return obj

    monkeypatch.setattr(service, "_client", client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda a: service.attach_ipv6(a, "instance" + str(a["id"])), (first, second)))
    assert [r["ipv6"] for r in results] == ["2001:db8::1", "2001:db8::2"]
    for aid, operation, proxies, trust_env, api_key, request in observed:
        assert not trust_env
        expected = {} if aid == first["id"] else {"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"}
        assert proxies == expected
        a = first if aid == first["id"] else second
        assert a["params"]["user_ocid"] in api_key
        if operation == "create_ipv6":
            assert request["body"].vnic_id == "vnic" + str(aid)

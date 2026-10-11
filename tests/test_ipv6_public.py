import oci
import pytest

import oci_service as service
from test_ipv6 import setup_network


def public_network(sdk):
    data, calls, vcn, subnet = setup_network(sdk, enabled=True)
    subnet.route_table_id = "route1"
    subnet.security_list_ids = ["security1"]
    data["list_ipv6s"] = [oci.core.models.Ipv6(ip_address="2001:db8::10", route_table_id=None)]
    route = oci.core.models.RouteTable(id="route1", vcn_id=vcn.id, route_rules=[
        oci.core.models.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id="igw1")])
    security = oci.core.models.SecurityList(id="security1", vcn_id=vcn.id,
        ingress_security_rules=[oci.core.models.IngressSecurityRule(source="0.0.0.0/0", protocol="6",
            tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=22, max=22)))],
        egress_security_rules=[oci.core.models.EgressSecurityRule(destination="0.0.0.0/0", protocol="all")])
    response = lambda model: lambda *a, **k: oci.response.Response(200, {"etag": "version1"}, model, None)
    data.update(get_route_table=response(route), get_security_list=response(security),
                get_internet_gateway=oci.core.models.InternetGateway(id="igw1", vcn_id=vcn.id, is_enabled=True, lifecycle_state="AVAILABLE"))
    def update_route(*a, **k):
        route.route_rules = k["body"].route_rules
        return oci.response.Response(200, {}, route, None)
    def update_security(*a, **k):
        security.ingress_security_rules = k["body"].ingress_security_rules
        security.egress_security_rules = k["body"].egress_security_rules
        return oci.response.Response(200, {}, security, None)
    data.update(update_route_table=update_route, update_security_list=update_security)
    return data, calls, subnet, route, security


def test_public_setup_preserves_ipv4_and_only_opens_requested_ssh(sdk, account):
    _, calls, _, route, security = public_network(sdk)
    ipv4_route, ipv4_in, ipv4_out = route.route_rules[0], security.ingress_security_rules[0], security.egress_security_rules[0]
    result = service.configure_ipv6_public(account, "instance1", "2001:db8:abcd::1/128", 2222)
    assert result["ssh_enabled"]
    assert route.route_rules[0] is ipv4_route
    assert security.ingress_security_rules[0] is ipv4_in and security.egress_security_rules[0] is ipv4_out
    assert route.route_rules[-1].destination == "::/0"
    ipv6_ssh = security.ingress_security_rules[-1]
    assert ipv6_ssh.source == "2001:db8:abcd::1/128"
    assert ipv6_ssh.tcp_options.destination_port_range.min == 2222
    assert security.ingress_security_rules[-2].icmp_options.type == 2
    assert security.egress_security_rules[-1].destination == "::/0"
    writes = [c for c in calls if c[0].startswith("update_")]
    assert all(c[2]["header_params"]["if-match"] == "version1" for c in writes)
    count = len(writes)
    service.configure_ipv6_public(account, "instance1", "2001:db8:abcd::1/128", 2222)
    assert len([c for c in calls if c[0].startswith("update_")]) == count


def test_blank_source_does_not_open_ssh(sdk, account):
    _, _, _, _, security = public_network(sdk)
    assert not service.configure_ipv6_public(account, "instance1")["ssh_enabled"]
    assert not any(r.protocol == "6" and ":" in r.source for r in security.ingress_security_rules)


@pytest.mark.parametrize("source", ["0.0.0.0/0", "not-an-address"])
def test_invalid_ssh_source_rejected_before_cloud_access(sdk, account, source):
    _, calls = sdk
    with pytest.raises(service.OciError, match="SSH 来源"):
        service.configure_ipv6_public(account, "instance1", source)
    assert not calls


def test_custom_ipv6_route_is_not_replaced(sdk, account):
    _, calls, _, route, _ = public_network(sdk)
    route.route_rules.append(oci.core.models.RouteRule(destination="::/0", network_entity_id="custom-gateway"))
    with pytest.raises(service.OciError, match="保留原路由"):
        service.configure_ipv6_public(account, "instance1")
    assert not any(c[0].startswith("update_") for c in calls)


def test_private_subnet_is_not_made_public(sdk, account):
    _, calls, subnet, _, _ = public_network(sdk)
    subnet.prohibit_internet_ingress = True
    with pytest.raises(service.OciError, match="禁止互联网入站"):
        service.configure_ipv6_public(account, "instance1")
    assert not any(c[0].startswith("update_") for c in calls)


def test_conflict_does_not_overwrite_security_rules(sdk, account):
    data, calls, _, _, security = public_network(sdk)
    original = list(security.ingress_security_rules)
    data["update_security_list"] = oci.exceptions.ServiceError(412, "NoEtagMatch", {}, "changed concurrently")
    with pytest.raises(service.OciError, match="已完成的 IPv6 路由.*可重试"):
        service.configure_ipv6_public(account, "instance1")
    assert security.ingress_security_rules == original
    assert sum(c[0] == "update_security_list" for c in calls) == 1


def test_route_override_is_used(sdk, account):
    data, calls, _, _, _ = public_network(sdk)
    data["list_ipv6s"][0].route_table_id = "route-override"
    service.configure_ipv6_public(account, "instance1")
    call = next(c for c in calls if c[0] == "get_route_table")
    assert call[2]["path_params"]["rtId"] == "route-override"


def test_foreign_vcn_security_list_blocks_all_writes(sdk, account):
    _, calls, _, _, security = public_network(sdk)
    security.vcn_id = "other-vcn"
    with pytest.raises(service.OciError, match="安全列表不属于"):
        service.configure_ipv6_public(account, "instance1")
    assert not any(c[0].startswith("update_") for c in calls)

from datetime import datetime, timezone

import oci
import pytest

import oci_service as service


def instance():
    return oci.core.models.Instance(id="instance1", display_name="machine", lifecycle_state="RUNNING",
        shape="VM.Standard.A1.Flex", availability_domain="AD1",
        shape_config=oci.core.models.InstanceShapeConfig(ocpus=2, memory_in_gbs=12),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id="image1"),
        compartment_id="comp1", metadata={"ssh_authorized_keys": "ssh-ed25519 test"})


def network(data):
    data["list_vnic_attachments"] = [oci.core.models.VnicAttachment(
        vnic_id="vnic1", lifecycle_state="ATTACHED")]
    data["get_vnic"] = oci.core.models.Vnic(id="vnic1", is_primary=True,
        private_ip="10.0.0.2", public_ip="203.0.113.2", subnet_id="subnet1")
    data["list_private_ips"] = [oci.core.models.PrivateIp(id="private1", is_primary=True)]


def test_account_json_is_normalized(account):
    assert isinstance(account["params"], dict)
    assert service.build_config(account)["user"] == account["params"]["user_ocid"]
    assert service.compartment_of(account) == account["params"]["tenancy_ocid"]


def test_instances_with_public_ip(sdk, account):
    data, calls = sdk
    data["list_instances"] = [instance()]
    network(data)
    result = service.list_instances(account)
    assert result[0]["public_ip"] == "203.0.113.2"
    assert result[0]["private_ip"] == "10.0.0.2"
    assert any(c[0] == "list_vnic_attachments" for c in calls)


def test_instances_pagination(sdk, account):
    data, _ = sdk
    count = [0]
    def pages(*args, **kwargs):
        count[0] += 1
        return oci.response.Response(200, {"opc-next-page": "next"} if count[0] == 1 else {},
                                     [instance()], None)
    data["list_instances"] = pages
    network(data)
    assert len(service.list_instances(account)) == 2


def test_change_ip_uses_private_ip_resource(sdk, account, monkeypatch):
    data, calls = sdk
    network(data)
    data["get_public_ip_by_private_ip_id"] = oci.core.models.PublicIp(id="old", lifetime="EPHEMERAL")
    data["delete_public_ip"] = None
    data["create_public_ip"] = oci.core.models.PublicIp(ip_address="203.0.113.3")
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    assert service.change_public_ip(account, "instance1")["new_ip"] == "203.0.113.3"
    details = next(c[2]["body"] for c in calls if c[0] == "create_public_ip")
    assert details.private_ip_id == "private1"


def test_change_ip_preserves_reserved_ip(sdk, account):
    data, calls = sdk
    network(data)
    data["get_public_ip_by_private_ip_id"] = oci.core.models.PublicIp(id="old", lifetime="RESERVED")
    with pytest.raises(service.OciError, match="保留"):
        service.change_public_ip(account, "instance1")
    assert not any(c[0] == "delete_public_ip" for c in calls)


def test_reboot_and_resize(sdk, account):
    data, calls = sdk
    data["instance_action"] = instance()
    data["update_instance"] = instance()
    assert service.instance_action(account, "instance1", "REBOOT")["ok"]
    assert service.resize_instance(account, "instance1", 1, 6)["ok"]
    details = next(c[2]["body"] for c in calls if c[0] == "update_instance")
    assert isinstance(details.shape_config, oci.core.models.UpdateInstanceShapeConfigDetails)


def test_instance_rename_diagnostic_reboot_and_image_reset(sdk, account):
    data, calls = sdk
    data["update_instance"] = instance()
    data["instance_action"] = instance()
    data["get_instance"] = instance()
    assert service.rename_instance(account, "instance1", "renamed")["name"] == "renamed"
    rename = next(c[2]["body"] for c in calls if c[0] == "update_instance")
    assert rename.display_name == "renamed"
    assert service.diagnostic_reboot(account, "instance1")["ok"]
    assert any(c[0] == "instance_action" and c[2]["query_params"]["action"] == "DIAGNOSTICREBOOT"
               for c in calls)
    assert service.reset_instance_image(account, "instance1")["ok"]
    reset = [c[2]["body"] for c in calls if c[0] == "update_instance"][-1]
    assert reset.source_details.image_id == "image1"
    assert reset.source_details.is_preserve_boot_volume_enabled is True


def test_image_reset_rejects_boot_volume_source(sdk, account):
    data, calls = sdk
    ins = instance()
    ins.source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(boot_volume_id="boot1")
    data["get_instance"] = ins
    with pytest.raises(service.OciError, match="不是从镜像创建"):
        service.reset_instance_image(account, "instance1")
    assert not any(c[0] == "update_instance" for c in calls)


def test_rebuild_never_deletes_original(sdk, account):
    data, calls = sdk
    data["get_instance"] = instance()
    data["launch_instance"] = oci.core.models.Instance(id="new1")
    network(data)
    result = service.reinstall_instance(account, "instance1")
    assert result["old_instance_id"] == "instance1"
    assert not any(c[0] == "terminate_instance" for c in calls)


def test_launch_boot_volume_uses_its_ad_and_idempotency(sdk, account):
    data, calls = sdk
    data["list_availability_domains"] = [oci.identity.models.AvailabilityDomain(name="AD1"),
                                         oci.identity.models.AvailabilityDomain(name="AD2")]
    data["get_boot_volume"] = oci.core.models.BootVolume(availability_domain="AD2")
    data["get_subnet"] = oci.core.models.Subnet(prohibit_public_ip_on_vnic=False)
    data["launch_instance"] = oci.core.models.Instance(id="new1")
    task = {"id": 7, "attempts": 0, "shape": "VM.Standard.A1.Flex", "ocpus": 1,
            "memory_gbs": 6, "boot_volume_id": "boot1", "subnet_id": "subnet1",
            "display_name": "test", "retry_token": "stable-token"}
    assert service.launch_once(account, task) == ("new1", "AD2")
    call = next(c for c in calls if c[0] == "launch_instance")
    assert call[2]["body"].availability_domain == "AD2"
    assert call[2]["header_params"]["opc-retry-token"] == "stable-token"


def test_block_attach_uses_polymorphic_sdk_model(sdk, account):
    data, calls = sdk
    data["attach_volume"] = oci.core.models.ParavirtualizedVolumeAttachment(id="attach1")
    assert service.attach_block_volume(account, "volume1", "instance1")["attachment_id"] == "attach1"
    assert isinstance(calls[-1][2]["body"], oci.core.models.AttachParavirtualizedVolumeDetails)


def test_create_bucket_correct_sdk_signature(sdk, account):
    data, _ = sdk
    data["get_namespace"] = "namespace1"
    data["create_bucket"] = oci.object_storage.models.Bucket(name="bucket1")
    assert service.create_bucket(account, "bucket1")["name"] == "bucket1"


def test_objects_pagination(sdk, account):
    data, _ = sdk
    data["get_namespace"] = "namespace1"
    count = [0]
    def page(*args, **kwargs):
        count[0] += 1
        result = oci.object_storage.models.ListObjects(
            objects=[oci.object_storage.models.ObjectSummary(name=f"file{count[0]}", size=1)],
            next_start_with="file2" if count[0] == 1 else None)
        return oci.response.Response(200, {}, result, None)
    data["list_objects"] = page
    assert len(service.list_objects(account, "bucket1")["objects"]) == 2


def test_monitoring_sdk_and_values(sdk, account):
    data, calls = sdk
    data["list_vnic_attachments"] = [oci.core.models.VnicAttachment(vnic_id="vnic1", instance_id="instance1", lifecycle_state="ATTACHED")]
    data["get_vnic"] = oci.core.models.Vnic(id="vnic1", compartment_id="network-comp")
    data["summarize_metrics_data"] = [oci.monitoring.models.MetricData(dimensions={"resourceId":"vnic1"},
        aggregated_datapoints=[oci.monitoring.models.AggregatedDatapoint(value=1e9),
                               oci.monitoring.models.AggregatedDatapoint(value=2e9)])]
    assert service.traffic_usage_gb(account)["total_gb"] == 3
    details = calls[-1][2]["body"]
    assert details.start_time and details.end_time
    assert details.query == "VnicToNetworkBytes[1h].sum()"
    assert details.namespace == "oci_vcn" and details.resolution == "1h"
    assert calls[-1][2]["query_params"]["compartmentId"] == "network-comp"
    assert service.traffic_usage_gb(account)["per_resource"] == {"instance1":3}


def test_limits_use_resource_availability(sdk, account):
    data, _ = sdk
    data["get_tenancy"] = oci.identity.models.Tenancy(name="test", home_region_key="SIN")
    data["list_limit_values"] = [oci.limits.models.LimitValueSummary(name="standard-a1-core-count", value=4)]
    data["get_resource_availability"] = oci.limits.models.ResourceAvailability(used=2, available=2)
    result = service.stats(account)
    assert result["limits"][0]["used"] == 2
    assert result["cost_note"] == ""


def test_smtp_credential_fields(sdk, account):
    data, _ = sdk
    data["create_smtp_credential"] = oci.identity.models.SmtpCredential(id="smtp1", username="user", password="pw")
    assert service.create_smtp_credential(account, "user1")["user"] == "user"


def test_reserved_ip_fields_and_sdk_filters(sdk, account):
    data, _ = sdk
    data["list_public_ips"] = [oci.core.models.PublicIp(id="reserved1", ip_address="203.0.113.9", lifetime="RESERVED")]
    assert service.list_reserved_ips(account)[0]["ip"] == "203.0.113.9"
    data["create_public_ip"] = oci.core.models.PublicIp(id="reserved2", ip_address="203.0.113.10")
    assert service.create_reserved_ip(account)["ip"] == "203.0.113.10"


def test_boot_volume_attachment_signature(sdk, account):
    data, _ = sdk
    data["list_availability_domains"] = [oci.identity.models.AvailabilityDomain(name="AD1")]
    data["list_boot_volume_attachments"] = [oci.core.models.BootVolumeAttachment(boot_volume_id="boot1",instance_id="instance1",lifecycle_state="ATTACHED")]
    data["get_instance"] = instance()
    data["list_boot_volumes"] = [oci.core.models.BootVolume(id="boot1",display_name="disk",size_in_gbs=50)]
    boot = service.list_boot_volumes(account)[0]
    assert boot["instance"] == "machine"
    assert boot["instance_id"] == "instance1"


def test_console_capture_sdk_signature(sdk, account):
    from types import SimpleNamespace
    data, calls = sdk
    data["capture_console_history"] = oci.core.models.ConsoleHistory(id="history1")
    data["get_console_history"] = oci.core.models.ConsoleHistory(lifecycle_state="SUCCEEDED")
    data["get_console_history_content"] = SimpleNamespace(content=b"boot log")
    assert service.console_capture(account,"instance1")["content"] == "boot log"
    assert calls[0][2]["body"].instance_id == "instance1"


def test_create_public_network_sdk_contract(sdk, account):
    data, calls = sdk
    vcn = oci.core.models.Vcn(id="vcn1",lifecycle_state="AVAILABLE",default_route_table_id="route1",default_security_list_id="security1")
    gateway = oci.core.models.InternetGateway(id="gateway1",lifecycle_state="AVAILABLE",is_enabled=True)
    subnet = oci.core.models.Subnet(id="subnet1",lifecycle_state="AVAILABLE")
    data.update({"list_vcns":[], "create_vcn":vcn, "get_vcn":vcn, "list_subnets":[],
        "list_internet_gateways":[], "create_internet_gateway":gateway,"get_internet_gateway":gateway,
        "get_route_table":oci.core.models.RouteTable(id="route1",route_rules=[]), "update_route_table":None,
        "get_security_list":oci.core.models.SecurityList(id="security1",ingress_security_rules=[],egress_security_rules=[]),
        "update_security_list":None,"create_subnet":subnet,"get_subnet":subnet})
    assert service.create_public_network(account)["subnet_id"] == "subnet1"
    rules=next(c[2]["body"].ingress_security_rules for c in calls if c[0]=="update_security_list")
    assert rules[0].tcp_options.destination_port_range.min == 22


def test_email_domain_uses_oci_dkim(sdk, account):
    import email_service
    data, _ = sdk
    data["create_email_domain"] = oci.email.models.EmailDomain(id="domain1")
    data["create_dkim"] = oci.email.models.Dkim(id="dkim1",dns_subdomain_name="selector._domainkey.example.com",
                                              cname_record_value="target.oraclecloud.com")
    result=email_service.setup_oci_email_domain(account,"example.com")
    assert result["dns"] == [{"type":"CNAME","name":"selector._domainkey.example.com","content":"target.oraclecloud.com"}]

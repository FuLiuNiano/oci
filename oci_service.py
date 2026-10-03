"""OCI API 封装：实例/网络/硬盘/用户/配额/对象存储/串口/监控，全部走官方 oci SDK，密钥只在本地。"""
import time
from datetime import datetime, timedelta, timezone

import oci
import store
import requests
from urllib.parse import urlsplit
from oci.exceptions import ServiceError


class OciError(Exception):
    """不可重试的业务错误。"""


# ---------- 基础 ----------

def build_config(acct):
    p = store.account_params(acct)
    cfg = {
        "user": p.get("user_ocid", ""),
        "fingerprint": p.get("fingerprint", ""),
        "tenancy": p.get("tenancy_ocid", ""),
        "region": acct.get("region") or p.get("region", ""),
        "key_content": p.get("private_key", ""),
    }
    try:
        oci.config.validate_config(cfg)
    except Exception as e:
        raise OciError(f"账号配置不合法: {e}")
    return cfg


def compartment_of(acct):
    p = store.account_params(acct)
    return p.get("compartment_id") or p.get("tenancy_ocid", "")


def _client(cls, acct):
    client = cls(build_config(acct))
    proxy = store.account_params(acct).get("proxy_url", "")
    if proxy:
        try:
            parsed = urlsplit(proxy)
            if parsed.scheme.lower() not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
                raise ValueError("unsupported proxy")
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError("invalid port")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("invalid proxy URL")
            if parsed.scheme.lower() == "socks5":
                proxy = "socks5h" + proxy[proxy.index(":"):]
            session = client.base_client.session
            session.trust_env = False
            required = {"http": proxy, "https": proxy}
            session.proxies = dict(required)
            original_send = session.send
            def send_via_required_proxy(request, **kwargs):
                # Enforce this proxy at the final transport boundary, including redirects.
                kwargs["proxies"] = dict(required)
                return original_send(request, **kwargs)
            session.send = send_via_required_proxy
        except Exception:
            raise OciError("代理配置失败，已阻止连接；请检查代理地址与 SOCKS 支持") from None
    return client


def _all(fn, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


def fmt_err(e):
    if isinstance(e, ServiceError):
        return f"OCI {e.status} {e.code or ''}: {e.message}"
    return str(e) or e.__class__.__name__


def is_transient(e):
    """开机抢机时遇到的容量不足/限流等错误视为可重试。"""
    if isinstance(e, OciError):
        return False
    if isinstance(e, ServiceError):
        if e.status >= 500 or e.status == 429:
            return True
        return "capacity" in (e.code or "").lower()
    return isinstance(e, (requests.RequestException, TimeoutError, ConnectionError))


def test_connection(acct):
    """验证 API 参数，返回该区域可用域名称列表。"""
    ads = _client(oci.identity.IdentityClient, acct).list_availability_domains(
        compartment_of(acct)
    ).data
    return [ad.name for ad in ads]


def info(acct):
    """该区域的可用域与子网，用于开机表单。"""
    comp = compartment_of(acct)
    ads = [ad.name for ad in _client(oci.identity.IdentityClient, acct).list_availability_domains(comp).data]
    subnets = []
    for s in _all(_client(oci.core.VirtualNetworkClient, acct).list_subnets, comp):
        subnets.append({
            "id": s.id,
            "name": s.display_name or s.id.rsplit(".", 1)[-1][:16],
            "public": not s.prohibit_public_ip_on_vnic,
        })
    return {"ads": ads, "subnets": subnets}


def create_public_network(acct):
    """Explicitly create/reuse the panel's public subnet and internet gateway."""
    comp = compartment_of(acct)
    net = _client(oci.core.VirtualNetworkClient, acct)
    tags = {"oci-panel": "managed"}
    vcns = _all(net.list_vcns, comp)
    vcn = next((v for v in vcns if (v.freeform_tags or {}).get("oci-panel") == "managed"
                and v.lifecycle_state == "AVAILABLE"), None)
    if vcn is None:
        created = net.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=comp, display_name="oci-panel", cidr_block="10.0.0.0/16",
            dns_label="ocipanel", freeform_tags=tags)).data
        vcn = oci.wait_until(net, net.get_vcn(created.id), "lifecycle_state", "AVAILABLE",
                             max_wait_seconds=120, max_interval_seconds=3).data
    existing = [s for s in _all(net.list_subnets, comp, vcn_id=vcn.id)
                if (s.freeform_tags or {}).get("oci-panel") == "managed"
                and s.lifecycle_state == "AVAILABLE"]
    if existing:
        return {"vcn_id": vcn.id, "subnet_id": existing[0].id, "reused": True}
    gateways = _all(net.list_internet_gateways, comp, vcn_id=vcn.id)
    gateway = next((g for g in gateways if g.is_enabled), None)
    if gateway is None:
        gateway = net.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
            compartment_id=comp, vcn_id=vcn.id, is_enabled=True,
            display_name="oci-panel", freeform_tags=tags)).data
        gateway = oci.wait_until(net, net.get_internet_gateway(gateway.id), "lifecycle_state", "AVAILABLE",
                                 max_wait_seconds=120, max_interval_seconds=3).data
    route = net.get_route_table(vcn.default_route_table_id).data
    rules = list(route.route_rules or [])
    if not any(r.destination == "0.0.0.0/0" for r in rules):
        rules.append(oci.core.models.RouteRule(destination="0.0.0.0/0",
                     destination_type="CIDR_BLOCK", network_entity_id=gateway.id))
        net.update_route_table(route.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
    security = net.get_security_list(vcn.default_security_list_id).data
    ingress = list(security.ingress_security_rules or [])
    if not any(r.protocol == "6" and r.tcp_options and r.tcp_options.destination_port_range
               and r.tcp_options.destination_port_range.min <= 22 <= r.tcp_options.destination_port_range.max
               for r in ingress):
        ingress.append(oci.core.models.IngressSecurityRule(protocol="6", source="0.0.0.0/0",
            source_type="CIDR_BLOCK", tcp_options=oci.core.models.TcpOptions(
                destination_port_range=oci.core.models.PortRange(min=22, max=22))))
        net.update_security_list(security.id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=ingress, egress_security_rules=security.egress_security_rules))
    subnet = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=comp, vcn_id=vcn.id, cidr_block="10.0.0.0/24", display_name="oci-panel-public",
        dns_label="public", route_table_id=route.id, security_list_ids=[security.id],
        prohibit_public_ip_on_vnic=False, freeform_tags=tags)).data
    subnet = oci.wait_until(net, net.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE",
                            max_wait_seconds=120, max_interval_seconds=3).data
    return {"vcn_id": vcn.id, "subnet_id": subnet.id, "reused": False}


# ---------- 实例 ----------

def _shape_line(ins):
    if ins.shape_config:
        return f"{ins.shape_config.ocpus}C/{ins.shape_config.memory_in_gbs}G"
    return ""


def list_instances(acct):
    comp = compartment_of(acct)
    compute = _client(oci.core.ComputeClient, acct)
    net = _client(oci.core.VirtualNetworkClient, acct)
    out = []
    for ins in _all(compute.list_instances, comp):
        if ins.lifecycle_state in ("TERMINATED", "TERMINATING"):
            continue
        pub, priv, subnet = "", "", ""
        try:
            atts = _all(compute.list_vnic_attachments, comp, instance_id=ins.id)
            if atts:
                vnics = [net.get_vnic(a.vnic_id).data for a in atts if a.lifecycle_state == "ATTACHED"]
                vnic = next((v for v in vnics if v.is_primary), vnics[0] if vnics else None)
                if vnic:
                    pub = vnic.public_ip or ""
                    priv = vnic.private_ip or ""
                    subnet = vnic.subnet_id or ""
        except ServiceError:
            pass
        out.append({
            "id": ins.id,
            "name": ins.display_name,
            "state": ins.lifecycle_state,
            "shape": ins.shape,
            "spec": _shape_line(ins),
            "ocpus": ins.shape_config.ocpus if ins.shape_config else None,
            "memory_gbs": ins.shape_config.memory_in_gbs if ins.shape_config else None,
            "public_ip": pub,
            "private_ip": priv,
            "subnet_id": subnet,
            "ad": ins.availability_domain,
            "created": str(ins.time_created or ""),
        })
    return out


def instance_action(acct, instance_id, action):
    """action: START / SOFTSTOP / STOP / SOFTRESET / RESET"""
    action = {"REBOOT": "SOFTRESET"}.get(action, action)
    if action not in ("START", "STOP", "SOFTSTOP", "RESET", "SOFTRESET"):
        raise OciError("不支持的电源操作")
    ins = _client(oci.core.ComputeClient, acct).instance_action(instance_id, action).data
    return {"ok": True, "state": ins.lifecycle_state}


def terminate_instance(acct, instance_id, preserve_boot_volume=False):
    _client(oci.core.ComputeClient, acct).terminate_instance(
        instance_id, preserve_boot_volume=preserve_boot_volume
    )
    return {"ok": True}


def resize_instance(acct, instance_id, ocpus, memory_gbs):
    """A1 Flex 升降配。"""
    details = oci.core.models.UpdateInstanceDetails(
        shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
            ocpus=ocpus, memory_in_gbs=memory_gbs,
        )
    )
    _client(oci.core.ComputeClient, acct).update_instance(instance_id, details)
    return {"ok": True}


def reinstall_instance(acct, instance_id):
    """按原镜像创建替代实例，保留旧实例供用户核验和手动释放。"""
    comp = compartment_of(acct)
    compute = _client(oci.core.ComputeClient, acct)
    net = _client(oci.core.VirtualNetworkClient, acct)
    ins = compute.get_instance(instance_id).data
    src = ins.source_details
    if src is None or getattr(src, "source_type", "") != "image":
        raise OciError("只能重装基于镜像的实例")
    subnet_id = _primary_vnic(net, comp, instance_id, compute).subnet_id
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=ins.compartment_id,
        availability_domain=ins.availability_domain,
        display_name=ins.display_name + "-rebuild",
        shape=ins.shape,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ins.shape_config.ocpus, memory_in_gbs=ins.shape_config.memory_in_gbs,
        ) if ins.shape_config and ins.shape.endswith(".Flex") else None,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=src.image_id, boot_volume_size_in_gbs=src.boot_volume_size_in_gbs,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True, display_name=ins.display_name,
        ) if subnet_id else None,
        metadata=dict(ins.metadata or {}),
    )
    new_ins = compute.launch_instance(details).data
    return {"new_instance_id": new_ins.id, "old_instance_id": instance_id,
            "message": "已提交新实例创建。旧实例及数据已保留，请确认新实例能登录后手动释放旧资源。"}


def _pick_image(compute, comp, shape, os_name, os_version):
    common = dict(compartment_id=comp, shape=shape, sort_by="TIMECREATED", sort_order="DESC")
    if os_version:
        imgs = compute.list_images(
            operating_system=os_name, operating_system_version=os_version, **common
        ).data
        if imgs:
            return imgs[0].id
    imgs = compute.list_images(operating_system=os_name, **common).data
    if not imgs:
        raise OciError(f"找不到匹配的镜像: {os_name} {os_version} ({shape})")
    return imgs[0].id


def launch_once(acct, task):
    """尝试开机一次。成功返回 (instance_id, ad)，失败抛 ServiceError / OciError。"""
    comp = compartment_of(acct)
    compute = _client(oci.core.ComputeClient, acct)
    net = _client(oci.core.VirtualNetworkClient, acct)

    ads = [ad.name for ad in _client(oci.identity.IdentityClient, acct).list_availability_domains(comp).data]
    if not ads:
        raise OciError("该区域没有可用域")
    # 固定可用域优先；否则每次尝试轮换，提高撞到容量的概率
    ad = task.get("ad_name") or ads[(task.get("attempts") or 0) % len(ads)]

    shape = task["shape"]
    if task.get("boot_volume_id"):
        boot = _client(oci.core.BlockstorageClient, acct).get_boot_volume(task["boot_volume_id"]).data
        if task.get("ad_name") and task["ad_name"] != boot.availability_domain:
            raise OciError("引导卷与指定可用域不一致")
        ad = boot.availability_domain
        source = oci.core.models.InstanceSourceViaBootVolumeDetails(
            boot_volume_id=task["boot_volume_id"],
        )
    else:
        source = oci.core.models.InstanceSourceViaImageDetails(
            image_id=_pick_image(compute, comp, shape, task["os_name"], task["os_version"]),
            boot_volume_size_in_gbs=task["boot_gb"],
        )

    subnet_id = task.get("subnet_id") or ""
    if not subnet_id:
        subs = [s for s in _all(net.list_subnets, comp)
                if not s.prohibit_public_ip_on_vnic and
                (not s.availability_domain or s.availability_domain == ad)]
        if not subs:
            raise OciError("该区域没有子网，请先在控制台创建 VCN 和子网")
        subnet_id = subs[0].id
    subnet = net.get_subnet(subnet_id).data
    if subnet.availability_domain and subnet.availability_domain != ad:
        if task.get("ad_name") or task.get("boot_volume_id"):
            raise OciError("子网与实例的可用域不一致")
        ad = subnet.availability_domain
    if subnet.prohibit_public_ip_on_vnic:
        raise OciError("请选择允许公网 IP 的子网")

    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=comp,
        availability_domain=ad,
        display_name=task["display_name"],
        shape=shape,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=task["ocpus"], memory_in_gbs=task["memory_gbs"]
        ) if shape.endswith(".Flex") else None,
        source_details=source,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True, display_name=task["display_name"],
        ),
        metadata={"ssh_authorized_keys": task["ssh_key"]} if task.get("ssh_key") else {},
    )
    retry_token = task.get("retry_token") or f"ocipanel-{task['id']}"
    ins = compute.launch_instance(details, opc_retry_token=retry_token).data
    return ins.id, ad


# ---------- 网络：换IP / IPv6 / 保留IP ----------

def _primary_vnic(net, comp, instance_id, compute):
    atts = _all(compute.list_vnic_attachments, comp, instance_id=instance_id)
    if not atts:
        raise OciError("实例没有 VNIC")
    vnics = [net.get_vnic(a.vnic_id).data for a in atts if a.lifecycle_state == "ATTACHED"]
    if not vnics:
        raise OciError("实例没有已连接的 VNIC")
    return next((v for v in vnics if v.is_primary), vnics[0])


def _primary_private_ip(net, vnic):
    ips = _all(net.list_private_ips, vnic_id=vnic.id)
    ip = next((ip for ip in ips if ip.is_primary), None)
    if not ip:
        raise OciError("实例没有主私网 IP")
    return ip.id


def change_public_ip(acct, instance_id):
    """删除临时公网 IP 再分配一个新的，实现一键换 IP。保留 IP 不自动动它。"""
    comp = compartment_of(acct)
    net = _client(oci.core.VirtualNetworkClient, acct)
    vnic = _primary_vnic(net, comp, instance_id, _client(oci.core.ComputeClient, acct))
    private_ip_id = _primary_private_ip(net, vnic)
    old_ip = vnic.public_ip or ""

    existing = None
    try:
        existing = net.get_public_ip_by_private_ip_id(
            oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
        ).data
    except ServiceError as e:
        if e.status != 404:
            raise

    if existing is not None and existing.lifetime == "RESERVED":
        raise OciError("当前绑定的是保留(RESERVED)IP，请在「保留IP」里操作或先在控制台解绑")

    if existing is not None:
        net.delete_public_ip(existing.id)
        time.sleep(3)

    created = net.create_public_ip(
        oci.core.models.CreatePublicIpDetails(
            compartment_id=comp, lifetime="EPHEMERAL", private_ip_id=private_ip_id,
        )
    ).data
    return {"old_ip": old_ip, "new_ip": (created.ip_address or "") if created else ""}


def attach_ipv6(acct, instance_id):
    net = _client(oci.core.VirtualNetworkClient, acct)
    vnic = _primary_vnic(net, compartment_of(acct), instance_id, _client(oci.core.ComputeClient, acct))
    try:
        ip6 = net.create_ipv6(oci.core.models.CreateIpv6Details(vnic_id=vnic.id)).data
    except ServiceError as e:
        if e.status in (400, 404, 409):
            raise OciError(f"附加 IPv6 失败（需先在控制台给 VCN/子网启用 IPv6）: {e.message}")
        raise
    return {"ipv6": ip6.ip_address or ""}


def list_reserved_ips(acct):
    comp = compartment_of(acct)
    return [{
        "id": r.id,
        "ip": r.ip_address,
        "name": r.display_name or "",
        "assigned": bool(r.private_ip_id),
        "private_ip_id": r.private_ip_id or "",
    } for r in _client(oci.core.VirtualNetworkClient, acct).list_public_ips(
        scope="REGION", compartment_id=comp, lifetime="RESERVED").data]


def create_reserved_ip(acct, name=""):
    created = _client(oci.core.VirtualNetworkClient, acct).create_public_ip(
        oci.core.models.CreatePublicIpDetails(
            compartment_id=compartment_of(acct), lifetime="RESERVED", display_name=name or None,
        )
    ).data
    return {"id": created.id, "ip": created.ip_address}


def delete_reserved_ip(acct, public_ip_id):
    _client(oci.core.VirtualNetworkClient, acct).delete_public_ip(public_ip_id)
    return {"ok": True}


def assign_reserved_ip(acct, public_ip_id, instance_id):
    net = _client(oci.core.VirtualNetworkClient, acct)
    vnic = _primary_vnic(net, compartment_of(acct), instance_id, _client(oci.core.ComputeClient, acct))
    private_ip_id = _primary_private_ip(net, vnic)
    try:
        current = net.get_public_ip_by_private_ip_id(
            oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)).data
    except ServiceError as e:
        if e.status != 404:
            raise
    else:
        if current.id == public_ip_id:
            return {"ok": True}
        if current.lifetime == "RESERVED":
            net.update_public_ip(current.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=""))
        else:
            net.delete_public_ip(current.id)
        time.sleep(3)
    net.update_public_ip(public_ip_id, oci.core.models.UpdatePublicIpDetails(
        private_ip_id=private_ip_id))
    return {"ok": True}


# ---------- 硬盘 ----------

def list_boot_volumes(acct):
    comp = compartment_of(acct)
    block = _client(oci.core.BlockstorageClient, acct)
    compute = _client(oci.core.ComputeClient, acct)
    ads = [ad.name for ad in _client(oci.identity.IdentityClient, acct).list_availability_domains(comp).data]
    out = []
    for ad in ads:
        attach = {}
        try:
            for a in _all(compute.list_boot_volume_attachments, ad, comp):
                if a.lifecycle_state == "ATTACHED":
                    attach[a.boot_volume_id] = a.instance_id
        except ServiceError:
            pass
        names = {}
        for iid in set(attach.values()):
            try:
                names[iid] = compute.get_instance(iid).data.display_name
            except ServiceError:
                names[iid] = iid
        for bv in _all(block.list_boot_volumes, availability_domain=ad, compartment_id=comp):
            out.append({
                "id": bv.id, "kind": "boot", "name": bv.display_name,
                "size_gbs": bv.size_in_gbs, "vpus": bv.vpus_per_gb,
                "state": bv.lifecycle_state, "ad": ad,
                "instance": names.get(attach.get(bv.id, ""), ""),
                "size_used_gbs": None,
            })
    return out


def update_boot_volume(acct, boot_volume_id, size_gbs=None, vpus=None):
    block = _client(oci.core.BlockstorageClient, acct)
    cur = block.get_boot_volume(boot_volume_id).data
    if size_gbs is not None and int(size_gbs) < cur.size_in_gbs:
        raise OciError(f"云硬盘只能扩容（当前 {cur.size_in_gbs} GB），不能缩小")
    details = oci.core.models.UpdateBootVolumeDetails()
    if size_gbs is not None:
        details.size_in_gbs = int(size_gbs)
    if vpus is not None:
        details.vpus_per_gb = int(vpus)
    block.update_boot_volume(boot_volume_id, details)
    return {
        "size_gbs": int(size_gbs) if size_gbs is not None else cur.size_in_gbs,
        "vpus": int(vpus) if vpus is not None else cur.vpus_per_gb,
    }


def list_block_volumes(acct):
    comp = compartment_of(acct)
    block = _client(oci.core.BlockstorageClient, acct)
    compute = _client(oci.core.ComputeClient, acct)
    ads = [ad.name for ad in _client(oci.identity.IdentityClient, acct).list_availability_domains(comp).data]
    out = []
    for ad in ads:
        attach = {}
        try:
            for a in _all(compute.list_volume_attachments, comp, availability_domain=ad):
                if a.lifecycle_state == "ATTACHED":
                    attach[a.volume_id] = (a.instance_id, a.id)
        except ServiceError:
            pass
        names = {}
        for iid in {v[0] for v in attach.values()}:
            try:
                names[iid] = compute.get_instance(iid).data.display_name
            except ServiceError:
                names[iid] = iid
        for v in _all(block.list_volumes, availability_domain=ad, compartment_id=comp):
            out.append({
                "id": v.id, "kind": "block", "name": v.display_name,
                "size_gbs": v.size_in_gbs, "vpus": v.vpus_per_gb,
                "state": v.lifecycle_state, "ad": ad,
                "instance": names.get(attach.get(v.id, ("", ""))[0], ""),
                "attachment_id": attach.get(v.id, ("", ""))[1],
            })
    return out


def update_block_volume(acct, volume_id, size_gbs=None, vpus=None):
    block = _client(oci.core.BlockstorageClient, acct)
    cur = block.get_volume(volume_id).data
    details = oci.core.models.UpdateVolumeDetails()
    if size_gbs is not None:
        if int(size_gbs) < cur.size_in_gbs:
            raise OciError(f"云硬盘只能扩容（当前 {cur.size_in_gbs} GB）")
        details.size_in_gbs = int(size_gbs)
    if vpus is not None:
        details.vpus_per_gb = int(vpus)
    block.update_volume(volume_id, details)
    return {"ok": True}


def create_block_volume(acct, ad, name, size_gbs, vpus=10):
    block = _client(oci.core.BlockstorageClient, acct)
    bv = block.create_volume(oci.core.models.CreateVolumeDetails(
        compartment_id=compartment_of(acct), availability_domain=ad,
        display_name=name, size_in_gbs=int(size_gbs), vpus_per_gb=int(vpus),
    )).data
    return {"id": bv.id}


def delete_block_volume(acct, volume_id):
    _client(oci.core.BlockstorageClient, acct).delete_volume(volume_id)
    return {"ok": True}


def attach_block_volume(acct, volume_id, instance_id):
    compute = _client(oci.core.ComputeClient, acct)
    att = compute.attach_volume(oci.core.models.AttachParavirtualizedVolumeDetails(
        instance_id=instance_id, volume_id=volume_id,
        display_name=f"vol-{volume_id[-8:]}",
    )).data
    return {"attachment_id": att.id}


def detach_block_volume(acct, volume_id):
    compute = _client(oci.core.ComputeClient, acct)
    comp = compartment_of(acct)
    atts = [a for a in _all(compute.list_volume_attachments, comp, volume_id=volume_id)
            if a.lifecycle_state == "ATTACHED"]
    if not atts:
        raise OciError("该卷没有挂载到任何实例")
    compute.detach_volume(atts[0].id)
    return {"ok": True}


# ---------- A1 体检 ----------

A1_FREE_OCPU = 4
A1_FREE_MEM = 24


def a1_checkup(acct):
    """检查账号下 A1 总量是否超出免费额度，支持一键降回。"""
    over = []
    total_ocpus = total_mem = 0.0
    for ins in list_instances(acct):
        if ins["shape"] == "VM.Standard.A1.Flex" and ins["state"] not in ("TERMINATED",):
            total_ocpus += ins["ocpus"] or 0
            total_mem += ins["memory_gbs"] or 0
            over.append(ins)
    return {
        "total_ocpus": round(total_ocpus, 1),
        "total_mem": round(total_mem, 1),
        "limit_ocpus": A1_FREE_OCPU,
        "limit_mem": A1_FREE_MEM,
        "over_ocpus": total_ocpus > A1_FREE_OCPU,
        "over_mem": total_mem > A1_FREE_MEM,
        "instances": over,
    }


def a1_downsize(acct, instance_id, ocpus, memory_gbs):
    return resize_instance(acct, instance_id, ocpus, memory_gbs)


# ---------- 串口控制台 ----------

def console_capture(acct, instance_id, wait_seconds=45):
    compute = _client(oci.core.ComputeClient, acct)
    ch = compute.capture_console_history(
        oci.core.models.CaptureConsoleHistoryDetails(instance_id=instance_id)).data
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        cur = compute.get_console_history(ch.id).data
        if cur.lifecycle_state == "SUCCEEDED":
            content = compute.get_console_history_content(ch.id).data
            if hasattr(content, "content"):
                content = content.content
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            return {"content": content or "(空)"}
        time.sleep(3)
    return {"content": f"(抓取中，稍后用历史ID {ch.id} 重试)"}


# ---------- 用户管理 / API 密钥 / 2FA ----------

def list_users(acct):
    return [{
        "id": u.id, "name": u.name, "email": u.email or "",
        "mfa": bool(u.is_mfa_activated),
        "desc": u.description or "",
    } for u in _client(oci.identity.IdentityClient, acct).list_users(
        compartment_id=acct["params"].get("tenancy_ocid", "")).data]


def create_user(acct, name, email, description=""):
    u = _client(oci.identity.IdentityClient, acct).create_user(
        oci.identity.models.CreateUserDetails(
            compartment_id=acct["params"].get("tenancy_ocid", ""),
            name=name, email=email, description=description or "created by oci-panel",
        )).data
    return {"id": u.id}


def reset_user_password(acct, user_id):
    """重置控制台密码，返回一次性密码。"""
    r = _client(oci.identity.IdentityClient, acct).create_or_reset_ui_password(user_id).data
    return {"password": r.password}


def update_user_email(acct, user_id, email, description=None):
    details = {"email": email}
    if description is not None:
        details["description"] = description
    _client(oci.identity.IdentityClient, acct).update_user(
        user_id, oci.identity.models.UpdateUserDetails(**details))
    return {"ok": True}


def clear_user_mfa(acct, user_id):
    """清除该用户的 2FA：删除其全部 MFA TOTP 设备。"""
    identity = _client(oci.identity.IdentityClient, acct)
    deleted = 0
    for dev in identity.list_mfa_totp_devices(user_id).data:
        identity.delete_mfa_totp_device(user_id, dev.id)
        deleted += 1
    return {"ok": True, "deleted": deleted}


def delete_user(acct, user_id):
    _client(oci.identity.IdentityClient, acct).delete_user(user_id)
    return {"ok": True}


def list_api_keys(acct, user_id):
    return [{"fingerprint": k.fingerprint, "time_added": str(k.time_created or "")}
            for k in _client(oci.identity.IdentityClient, acct).list_api_keys(user_id).data]


def upload_api_key(acct, user_id, public_key_pem):
    k = _client(oci.identity.IdentityClient, acct).upload_api_key(
        user_id, oci.identity.models.CreateApiKeyDetails(key=public_key_pem)).data
    return {"fingerprint": k.fingerprint}


def delete_api_key(acct, user_id, fingerprint):
    _client(oci.identity.IdentityClient, acct).delete_api_key(user_id, fingerprint)
    return {"ok": True}


def create_smtp_credential(acct, user_id, description="oci-panel smtp"):
    c = _client(oci.identity.IdentityClient, acct).create_smtp_credential(
        oci.identity.models.CreateSmtpCredentialDetails(description=description), user_id).data
    return {"user": c.username, "password": c.password, "id": c.id}


# ---------- 配额 / 订阅 / 费用 ----------

def stats(acct):
    """订阅信息 + A1/微型机配额。"""
    comp = compartment_of(acct)
    tenancy_id = acct["params"].get("tenancy_ocid", "")
    identity = _client(oci.identity.IdentityClient, acct)
    tenancy = identity.get_tenancy(tenancy_id).data
    out = {
        "tenancy_name": tenancy.name,
        "home_region": tenancy.home_region_key or "",
        "limits": [],
        "cost_note": "",
    }
    try:
        limits = _client(oci.limits.LimitsClient, acct)
        vals = _all(limits.list_limit_values, service_name="compute", compartment_id=tenancy_id)
        for v in vals:
            n = (v.name or "").lower()
            if "a1" in n or "e2-1-micro" in n or "micro" in n:
                available = limits.get_resource_availability(
                    service_name="compute", limit_name=v.name, compartment_id=comp,
                    **({"availability_domain": v.availability_domain} if v.availability_domain else {})).data
                out["limits"].append({"name": v.name, "limit": v.value,
                                      "used": available.used, "ad": v.availability_domain or ""})
    except Exception as e:
        out["cost_note"] = f"配额查询失败: {fmt_err(e)}"
    return out


def subscribed_regions(acct):
    identity = _client(oci.identity.IdentityClient, acct)
    rows = _all(identity.list_region_subscriptions, store.account_params(acct)["tenancy_ocid"])
    return [{"region": r.region_name, "status": r.status, "home": bool(r.is_home_region)} for r in rows]


def usage_cost(acct, days=30):
    """近 N 天费用（需要账号有 usage-report 权限，失败会返回提示）。"""
    if not 1 <= days <= 365:
        raise OciError("费用查询天数必须在 1 到 365 之间")
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    client = _client(oci.usage_api.UsageapiClient, acct)
    resp = client.request_summarized_usages(
        oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=acct["params"].get("tenancy_ocid", ""),
            time_usage_started=start,
            time_usage_ended=end,
            granularity="DAILY",
            query_type="COST",
        )
    )
    rows = {}
    currencies = {}
    for item in resp.data.items:
        d = str(item.time_usage_started or "")[:10]
        rows[d] = rows.get(d, 0) + (item.computed_amount or 0)
        currency = item.currency or "未标明币种"
        currencies[currency] = currencies.get(currency, 0) + (item.computed_amount or 0)
    total = sum(rows.values())
    return {"days": days, "total": round(total, 2), "daily": rows,
            "currencies": {k: round(v, 2) for k, v in currencies.items()}}


# ---------- 对象存储 ----------

def os_namespace(acct):
    return _client(oci.object_storage.ObjectStorageClient, acct).get_namespace().data


def list_buckets(acct):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    return [{
        "name": b.name, "namespace": ns,
        "created": str(b.time_created or ""),
        "storage_tier": getattr(b, "storage_tier", "") or "",
    } for b in osv.list_buckets(ns, compartment_of(acct)).data]


def create_bucket(acct, name, tier="Standard"):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    details = {
        "compartment_id": compartment_of(acct),
        "name": name,
        "public_access_type": "NoPublicAccess",
    }
    if tier.lower() == "archive":
        details["storage_tier"] = "Archive"
    bucket = osv.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(**details))
    return {"name": bucket.data.name}


def delete_bucket(acct, name):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    osv.delete_bucket(ns, name)
    return {"ok": True}


def list_objects(acct, bucket, prefix=""):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    objects, prefixes, start = [], set(), None
    while True:
        resp = osv.list_objects(ns, bucket, prefix=prefix or None, limit=1000,
                               fields="name,size,timeModified", start=start, delimiter="/")
        objects.extend(resp.data.objects)
        prefixes.update(resp.data.prefixes or [])
        start = resp.data.next_start_with
        if not start:
            break
    return {"prefix": prefix, "objects": [{
        "name": o.name, "size": o.size or 0, "modified": str(o.time_modified or ""),
    } for o in objects], "prefixes": sorted(prefixes)}


def get_object(acct, bucket, name):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    return osv.get_object(ns, bucket, name).data.content


def put_object(acct, bucket, name, content: bytes):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    osv.put_object(ns, bucket, name, put_object_body=content)
    return {"ok": True}


def delete_object(acct, bucket, name):
    osv = _client(oci.object_storage.ObjectStorageClient, acct)
    ns = osv.get_namespace().data
    osv.delete_object(ns, bucket, name)
    return {"ok": True}


# ---------- 云监控：流量统计（供超额关停任务使用） ----------

def traffic_usage_gb(acct, hours=24):
    """近 N 小时出站流量（GB），按实例归集。需要实例启用计算代理监控。"""
    comp = compartment_of(acct)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    mon = _client(oci.monitoring.MonitoringClient, acct)
    resp = mon.summarize_metrics_data(
        compartment_id=comp,
        summarize_metrics_data_details=oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace="oci_computeagent",
            query="VnicToNetworkBytes[1h].sum()",
            start_time=start, end_time=end,
        ),
    )
    per = {}
    total = 0.0
    for md in resp.data:
        gb = sum(p.value or 0 for p in md.aggregated_datapoints or []) / 1e9
        rid = (md.dimensions or {}).get("resourceId", "")
        per[rid] = per.get(rid, 0) + gb
        total += gb
    return {"total_gb": round(total, 2), "per_resource": per}

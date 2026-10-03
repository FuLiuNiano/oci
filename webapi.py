"""全部业务 API 路由（多云账号、OCI 扩展、开机任务、SSH、CF/DNS、域名监控、设置、面板维护）。"""
import base64
import os
import time
from typing import Optional

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import cloudflare_service as cf
import email_service
import mcp_service
import notify
import oci_service
import sshpool
import store
import host_monitor
from deps import require_auth

api = APIRouter(prefix="/api")


def _account_or_404(account_id: int):
    rows = store.query("SELECT * FROM accounts WHERE id=? AND platform='oci'", (account_id,))
    if not rows:
        raise HTTPException(404, "账号不存在")
    return rows[0]


def _validate_account(name, region, params):
    if not name.strip() or not region.strip():
        raise HTTPException(400, "请填写名称和区域")
    for key in ("user_ocid", "tenancy_ocid", "fingerprint", "private_key"):
        if not isinstance(params.get(key), str) or not params[key].strip():
            raise HTTPException(400, f"请填写 {key}")
    try:
        validated = oci_service._client(__import__("oci").identity.IdentityClient,
                                       {"region": region, "params": params})
        validated.base_client.session.close()
    except Exception as e:
        raise HTTPException(400, f"OCI 配置无效: {oci_service.fmt_err(e)}")


def _wrap(fn, *args, **kwargs):
    """把云 API 异常转成 HTTP 502 的受控错误信息。"""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except (oci_service.OciError, cf.CfError) as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(502, f"{e.__class__.__name__}: {e}")


# ================= 账号（多云） =================

class AccountBody(BaseModel):
    platform: str = "oci"
    name: str
    region: str = ""
    params: dict = {}
    auto_restart: bool = False
    traffic_limit_gb: float = 0
    traffic_action: str = "notify"
    remove_proxy: bool = False


@api.get("/accounts")
def accounts_list(_: None = Depends(require_auth)):
    rows = store.query("SELECT * FROM accounts WHERE platform='oci' ORDER BY id")
    return {"data": [store.account_public(a) for a in rows]}


@api.post("/accounts")
def accounts_add(body: AccountBody, _: None = Depends(require_auth)):
    if body.platform != "oci":
        raise HTTPException(400, f"不支持的平台 {body.platform}")
    private_key = body.params.get("private_key", "")
    if not isinstance(private_key, str) or not private_key.strip().startswith("-----BEGIN"):
        raise HTTPException(400, "OCI 私钥格式不对：请粘贴 PEM 全文（-----BEGIN 开头）")
    _validate_account(body.name, body.region, body.params)
    aid = store.execute(
        "INSERT INTO accounts(platform, name, region, params, auto_restart, traffic_limit_gb, traffic_action) "
        "VALUES(?,?,?,?,?,?,?)",
        (body.platform, body.name.strip(), body.region.strip(),
         store.json.dumps(body.params, ensure_ascii=False),
         1 if body.auto_restart else 0, body.traffic_limit_gb, body.traffic_action),
    )
    return {"id": aid}


@api.put("/accounts/{account_id}")
def accounts_update(account_id: int, body: AccountBody, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    params = dict(body.params)
    old = acct_params_keep(acct)
    if old.get("proxy_url") and params.get("proxy_url") == "" and not body.remove_proxy:
        raise HTTPException(400, "清空代理会恢复服务器直连，请明确确认移除代理")
    if body.remove_proxy and params.get("proxy_url") != "":
        raise HTTPException(400, "移除代理时代理地址必须为空")
    for k, v in old.items():
        if k not in params or (k == "private_key" and not params.get(k)):
            params[k] = v
    _validate_account(body.name, body.region, params)
    store.execute(
        "UPDATE accounts SET platform=?, name=?, region=?, params=?, auto_restart=?, "
        "traffic_limit_gb=?, traffic_action=? WHERE id=?",
        (acct["platform"], body.name.strip(), body.region.strip(),
         store.json.dumps(params, ensure_ascii=False),
         1 if body.auto_restart else 0, body.traffic_limit_gb, body.traffic_action, account_id),
    )
    return {"ok": True}


def acct_params_keep(acct):
    return store.account_params(acct)


@api.delete("/accounts/{account_id}")
def accounts_delete(account_id: int, _: None = Depends(require_auth)):
    _account_or_404(account_id)
    store.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return {"ok": True}


@api.post("/accounts/{account_id}/test")
def accounts_test(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    try:
        return {"ok": True, "ads": oci_service.test_connection(acct)}
    except Exception as e:
        return {"ok": False, "error": oci_service.fmt_err(e)}


@api.post("/accounts/{account_id}/copy-region")
def accounts_copy_region(account_id: int, body: dict, _: None = Depends(require_auth)):
    """账号配置一键复制到新地区（原版「账号配置可一键复制到新地区」）。"""
    acct = _account_or_404(account_id)
    region = (body or {}).get("region", "").strip()
    name = (body or {}).get("name", "").strip() or f"{acct['name']}-{region}"
    if not region:
        raise HTTPException(400, "请填写新区域")
    nid = store.execute(
        "INSERT INTO accounts(platform, name, region, params, auto_restart, traffic_limit_gb, traffic_action) "
        "VALUES(?,?,?,?,?,?,?)",
        (acct["platform"], name, region, store.json.dumps(acct["params"]), acct["auto_restart"],
         acct["traffic_limit_gb"], acct["traffic_action"]),
    )
    return {"id": nid}


@api.get("/accounts/{account_id}/stats")
def accounts_stats(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.stats, acct)


@api.get("/accounts/{account_id}/usage")
def accounts_usage(account_id: int, days: int = 30, _: None = Depends(require_auth)):
    return _wrap(oci_service.usage_cost, _account_or_404(account_id), days)


@api.get("/panel/metrics")
def panel_metrics(_: None = Depends(require_auth)):
    return host_monitor.snapshot()


@api.get("/accounts/{account_id}/regions")
def account_regions(account_id: int, _: None = Depends(require_auth)):
    return {"data": _wrap(oci_service.subscribed_regions, _account_or_404(account_id))}


@api.get("/accounts/{account_id}/traffic")
def account_traffic(account_id: int, _: None = Depends(require_auth)):
    return _wrap(oci_service.traffic_usage_gb, _account_or_404(account_id))


@api.post("/accounts/{account_id}/check")
def account_check(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    checks = []
    for name, fn in (("API 密钥与可用域", oci_service.test_connection),
                     ("当前区域实例读取", oci_service.list_instances),
                     ("区域订阅读取", oci_service.subscribed_regions)):
        try:
            result = fn(acct)
            checks.append({"name": name, "ok": True, "message": f"读取成功 · {len(result)} 项"})
        except Exception as e:
            checks.append({"name": name, "ok": False, "message": oci_service.fmt_err(e)})
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "region": acct["region"], "account_name": acct["name"]}


# ================= 概览（多云体检） =================

@api.get("/overview")
def overview(_: None = Depends(require_auth)):
    accounts = store.query("SELECT * FROM accounts WHERE platform='oci' ORDER BY id")
    by_platform = {}
    states = {}
    errors = []
    total = 0
    for acct in accounts:
        platform = acct["platform"]
        by_platform[platform] = by_platform.get(platform, 0) + 1
        try:
            rows = oci_service.list_instances(acct)
            for r in rows:
                total += 1
                states[r["state"]] = states.get(r["state"], 0) + 1
        except Exception as e:
            errors.append(f"{acct['name']}: {oci_service.fmt_err(e)}")
    tasks = store.query("SELECT COUNT(*) AS n FROM launch_tasks WHERE status='running'")[0]["n"]
    return {
        "accounts": len(accounts), "by_platform": by_platform,
        "instances": total, "states": states,
        "running_tasks": tasks, "errors": errors,
        "ssh_sessions": store.query("SELECT COUNT(*) AS n FROM ssh_sessions")[0]["n"],
        "domains": store.query("SELECT COUNT(*) AS n FROM domains")[0]["n"],
    }


# ================= 多云通用实例操作 =================

class ActionBody(BaseModel):
    account_id: int
    instance_id: str
    action: str
    kind: str = ""
    rg: str = ""
    zone: str = ""
    preserve_boot_volume: bool = False


@api.get("/cloud/{platform}/instances")
def cloud_instances(platform: str, account_id: int, _: None = Depends(require_auth)):
    if platform != "oci":
        raise HTTPException(400, "此版本仅支持 Oracle Cloud")
    return {"data": _wrap(oci_service.list_instances, _account_or_404(account_id))}


@api.post("/cloud/{platform}/action")
def cloud_action(platform: str, body: ActionBody, _: None = Depends(require_auth)):
    if platform != "oci":
        raise HTTPException(400, "此版本仅支持 Oracle Cloud")
    acct = _account_or_404(body.account_id)
    if body.action == "TERMINATE":
        return _wrap(oci_service.terminate_instance, acct, body.instance_id, body.preserve_boot_volume)
    if body.action not in ("START", "SOFTSTOP", "STOP", "SOFTRESET", "RESET", "REBOOT"):
        raise HTTPException(400, "不支持的操作")
    result = _wrap(oci_service.instance_action, acct, body.instance_id, body.action)
    for prefix in ("manual_stop", "traffic_block"):
        blocked = store.get_json_setting(f"{prefix}:{acct['id']}", []) or []
        if body.action in ("STOP", "SOFTSTOP") and prefix == "manual_stop":
            blocked = list(set(blocked + [body.instance_id]))
        elif body.action == "START":
            blocked = [i for i in blocked if i != body.instance_id]
        store.set_json_setting(f"{prefix}:{acct['id']}", blocked)
    return result


class ChangeIpBody(BaseModel):
    account_id: int
    instance_id: str


@api.post("/cloud/{platform}/change-ip")
def cloud_change_ip(platform: str, body: ChangeIpBody, _: None = Depends(require_auth)):
    if platform != "oci":
        raise HTTPException(400, "此版本仅支持 Oracle Cloud")
    return _wrap(oci_service.change_public_ip, _account_or_404(body.account_id), body.instance_id)


# ================= OCI 扩展功能 =================

@api.get("/oc-info")
def oc_info(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.info, acct)


@api.post("/oci/network/create")
def oci_network_create(body: dict, _: None = Depends(require_auth)):
    return _wrap(oci_service.create_public_network, _account_or_404(body["account_id"]))


# ---- 保留 IP ----

@api.get("/oci/reserved-ips")
def oci_reserved_ips(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_reserved_ips, acct)}


@api.post("/oci/reserved-ips")
def oci_create_reserved_ip(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.create_reserved_ip, acct, body.get("name", ""))


@api.delete("/oci/reserved-ips/{public_ip_id}")
def oci_delete_reserved_ip(public_ip_id: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_reserved_ip, acct, public_ip_id)


@api.post("/oci/reserved-ips/{public_ip_id}/assign")
def oci_assign_reserved_ip(public_ip_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.assign_reserved_ip, acct, public_ip_id, body["instance_id"])


@api.post("/oci/ipv6")
def oci_ipv6(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.attach_ipv6, acct, body["instance_id"])


# ---- 硬盘 ----

@api.get("/oci/boot-volumes")
def oci_boot_volumes(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_boot_volumes, acct)}


@api.post("/oci/boot-volumes/{volume_id}/update")
def oci_boot_volume_update(volume_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.update_boot_volume, acct, volume_id,
                 body.get("size_gbs"), body.get("vpus"))


@api.get("/oci/block-volumes")
def oci_block_volumes(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_block_volumes, acct)}


@api.post("/oci/block-volumes")
def oci_block_volume_create(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.create_block_volume, acct, body["ad"], body["name"],
                 body["size_gbs"], body.get("vpus", 10))


@api.post("/oci/block-volumes/{volume_id}/update")
def oci_block_volume_update(volume_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.update_block_volume, acct, volume_id,
                 body.get("size_gbs"), body.get("vpus"))


@api.delete("/oci/block-volumes/{volume_id}")
def oci_block_volume_delete(volume_id: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_block_volume, acct, volume_id)


@api.post("/oci/block-volumes/{volume_id}/attach")
def oci_block_volume_attach(volume_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.attach_block_volume, acct, volume_id, body["instance_id"])


@api.post("/oci/block-volumes/{volume_id}/detach")
def oci_block_volume_detach(volume_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.detach_block_volume, acct, volume_id)


# ---- A1 体检 / 升降配 / 重装 / 串口 ----

@api.get("/oci/a1-checkup")
def oci_a1_checkup(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.a1_checkup, acct)


@api.post("/oci/a1-downsize")
def oci_a1_downsize(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.a1_downsize, acct, body["instance_id"],
                 body["ocpus"], body["memory_gbs"])


@api.post("/oci/resize")
def oci_resize(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.resize_instance, acct, body["instance_id"],
                 body["ocpus"], body["memory_gbs"])


@api.post("/oci/reinstall")
def oci_reinstall(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.reinstall_instance, acct, body["instance_id"])


@api.post("/oci/console")
def oci_console(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.console_capture, acct, body["instance_id"])


# ---- 用户管理 / API 密钥 / 2FA / SMTP 凭据 ----

@api.get("/oci/users")
def oci_users(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_users, acct)}


@api.post("/oci/users")
def oci_user_create(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.create_user, acct, body["name"], body.get("email", ""),
                 body.get("description", ""))


@api.post("/oci/users/{user_id}/reset-password")
def oci_user_reset_pw(user_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.reset_user_password, acct, user_id)


@api.post("/oci/users/{user_id}/email")
def oci_user_email(user_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.update_user_email, acct, user_id, body["email"],
                 body.get("description"))


@api.post("/oci/users/{user_id}/clear-mfa")
def oci_user_clear_mfa(user_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.clear_user_mfa, acct, user_id)


@api.delete("/oci/users/{user_id}")
def oci_user_delete(user_id: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_user, acct, user_id)


@api.get("/oci/users/{user_id}/keys")
def oci_user_keys(user_id: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_api_keys, acct, user_id)}


@api.post("/oci/users/{user_id}/keys")
def oci_user_key_add(user_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.upload_api_key, acct, user_id, body["key_pem"])


@api.delete("/oci/users/{user_id}/keys/{fingerprint}")
def oci_user_key_del(user_id: str, fingerprint: str, account_id: int,
                     _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_api_key, acct, user_id, fingerprint)


@api.post("/oci/users/{user_id}/smtp-credential")
def oci_user_smtp(user_id: str, body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.create_smtp_credential, acct, user_id,
                 body.get("description", "oci-panel smtp"))


# ---- 对象存储 ----

@api.get("/oci/buckets")
def oci_buckets(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(oci_service.list_buckets, acct)}


@api.post("/oci/buckets")
def oci_bucket_create(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(oci_service.create_bucket, acct, body["name"], body.get("tier", "Standard"))


@api.delete("/oci/buckets/{name}")
def oci_bucket_delete(name: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_bucket, acct, name)


@api.get("/oci/objects")
def oci_objects(account_id: int, bucket: str, prefix: str = "",
                _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.list_objects, acct, bucket, prefix)


@api.get("/oci/objects/content")
def oci_object_content(account_id: int, bucket: str, name: str,
                       _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    data = _wrap(oci_service.get_object, acct, bucket, name)
    return {"content": data[:2_000_000].decode("utf-8", "replace"),
            "truncated": len(data) > 2_000_000, "size": len(data)}


class PutBody(BaseModel):
    account_id: int
    bucket: str
    name: str
    content: str = ""
    content_base64: str = ""


@api.post("/oci/objects/put")
def oci_object_put(body: PutBody, _: None = Depends(require_auth)):
    acct = _account_or_404(body.account_id)
    data = (base64.b64decode(body.content_base64) if body.content_base64
            else body.content.encode("utf-8"))
    return _wrap(oci_service.put_object, acct, body.bucket, body.name, data)


@api.delete("/oci/objects")
def oci_object_delete(account_id: int, bucket: str, name: str,
                      _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(oci_service.delete_object, acct, bucket, name)


# ================= 开机抢机任务 =================

class LaunchBody(BaseModel):
    account_id: int
    display_name: str = "auto-boot"
    shape: str = "VM.Standard.A1.Flex"
    ocpus: float = 2
    memory_gbs: float = 12
    os_name: str = "Canonical Ubuntu"
    os_version: str = "24.04"
    boot_gb: int = 50
    ssh_key: str = ""
    subnet_id: str = ""
    boot_volume_id: str = ""
    ad_name: str = ""


@api.get("/launch-tasks")
def launch_tasks(_: None = Depends(require_auth)):
    rows = store.query(
        "SELECT t.*, a.name AS account_name FROM launch_tasks t "
        "LEFT JOIN accounts a ON a.id=t.account_id ORDER BY t.id DESC")
    return {"data": rows}


@api.post("/launch-tasks")
def launch_task_add(body: LaunchBody, _: None = Depends(require_auth)):
    acct = _account_or_404(body.account_id)
    if acct["platform"] != "oci":
        raise HTTPException(400, "开机抢机仅支持 OCI 账号")
    if not body.shape.strip():
        raise HTTPException(400, "形状不能为空")
    if body.shape.endswith(".Flex") and not body.boot_volume_id and (
            body.ocpus <= 0 or body.memory_gbs <= 0):
        raise HTTPException(400, "Flex 形状需要填写 OCPU 和内存（或改用引导卷开机）")
    if not body.boot_volume_id and not body.ssh_key.strip():
        raise HTTPException(400, "请填写 SSH 公钥，以便创建后能登录")
    if body.boot_gb < 47:
        raise HTTPException(400, "引导卷不能小于 47 GB")
    tid = store.execute(
        "INSERT INTO launch_tasks(account_id, display_name, shape, ocpus, memory_gbs, os_name, "
        "os_version, boot_gb, ssh_key, subnet_id, boot_volume_id, ad_name) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.account_id, body.display_name.strip() or "auto-boot", body.shape.strip(),
         body.ocpus, body.memory_gbs, body.os_name.strip(), body.os_version.strip(),
         body.boot_gb, body.ssh_key.strip(), body.subnet_id.strip(),
         body.boot_volume_id.strip(), body.ad_name.strip()),
    )
    import uuid
    store.execute("UPDATE launch_tasks SET retry_token=? WHERE id=?", (str(uuid.uuid4()), tid))
    return {"id": tid}


@api.post("/launch-tasks/{task_id}/{op}")
def launch_task_op(task_id: int, op: str, _: None = Depends(require_auth)):
    if not store.query("SELECT id FROM launch_tasks WHERE id=?", (task_id,)):
        raise HTTPException(404, "任务不存在")
    row = store.query("SELECT * FROM launch_tasks WHERE id=?", (task_id,))[0]
    if op == "start" and row["status"] == "success":
        raise HTTPException(400, "此任务已创建实例，请新建任务以创建另一台")
    if op == "stop":
        store.execute("UPDATE launch_tasks SET status='stopped', "
                      "updated_at=datetime('now','localtime') WHERE id=?", (task_id,))
    elif op == "start":
        if row["status"] == "failed":
            import uuid
            store.execute("UPDATE launch_tasks SET retry_token=? WHERE id=?", (str(uuid.uuid4()), task_id))
        store.execute("UPDATE launch_tasks SET status='running', last_error='', "
                      "updated_at=datetime('now','localtime') WHERE id=?", (task_id,))
    elif op == "delete":
        store.execute("DELETE FROM launch_tasks WHERE id=?", (task_id,))
    else:
        raise HTTPException(400, "不支持的操作")
    return {"ok": True}


# ================= Cloudflare DNS =================

class CfBody(BaseModel):
    api_token: str = ""
    email: str = ""
    global_key: str = ""


@api.get("/cf/settings")
def cf_get(_: None = Depends(require_auth)):
    p = store.get_json_setting("cf_params", {}) or {}
    return {"api_token": p.get("cf_api_token", ""), "email": p.get("cf_email", ""),
            "has_key": bool(p.get("cf_account_key"))}


@api.post("/cf/settings")
def cf_set(body: CfBody, _: None = Depends(require_auth)):
    params = {"cf_api_token": body.api_token.strip()}
    if body.global_key.strip():
        params["cf_email"] = body.email.strip()
        params["cf_account_key"] = body.global_key.strip()
    store.set_json_setting("cf_params", params)
    return {"ok": True}


@api.post("/cf/test")
def cf_test(_: None = Depends(require_auth)):
    params = store.get_json_setting("cf_params", {}) or {}
    return _wrap(cf.test, params)


@api.get("/cf/zones")
def cf_zones(_: None = Depends(require_auth)):
    return {"data": _wrap(cf.list_zones, store.get_json_setting("cf_params", {}) or {})}


@api.get("/cf/records")
def cf_records(zone: str, qtype: str = "", name: str = "", _: None = Depends(require_auth)):
    return {"data": _wrap(cf.list_records, store.get_json_setting("cf_params", {}) or {},
                          zone, qtype, name)}


class RecordBody(BaseModel):
    zone: str
    type: str
    name: str
    content: str
    ttl: int = 1
    proxied: bool = False
    priority: Optional[int] = None
    record_id: str = ""


@api.post("/cf/records")
def cf_record_create(body: RecordBody, _: None = Depends(require_auth)):
    return _wrap(cf.create_record, store.get_json_setting("cf_params", {}) or {},
                 body.zone, body.type, body.name, body.content, body.ttl,
                 body.proxied, body.priority)


@api.put("/cf/records/{record_id}")
def cf_record_update(record_id: str, body: RecordBody, _: None = Depends(require_auth)):
    return _wrap(cf.update_record, store.get_json_setting("cf_params", {}) or {},
                 body.zone, record_id, body.type, body.name, body.content,
                 body.ttl, body.proxied)


@api.delete("/cf/records/{record_id}")
def cf_record_delete(record_id: str, zone: str, _: None = Depends(require_auth)):
    return _wrap(cf.delete_record, store.get_json_setting("cf_params", {}) or {}, zone, record_id)


@api.post("/cf/import-domains")
def cf_import_domains(_: None = Depends(require_auth)):
    return _wrap(cf.import_domains_into_monitor, store.get_json_setting("cf_params", {}) or {})


# ================= 域名监控 =================

@api.get("/domains")
def domains_list(_: None = Depends(require_auth)):
    return {"data": store.query("SELECT * FROM domains ORDER BY id")}


@api.post("/domains")
def domains_add(body: dict, _: None = Depends(require_auth)):
    name = (body.get("name") or "").strip().lower()
    if not name or "." not in name:
        raise HTTPException(400, "域名格式不对")
    if store.query("SELECT id FROM domains WHERE name=?", (name,)):
        raise HTTPException(400, "域名已存在")
    did = store.execute("INSERT INTO domains(name, registrar) VALUES(?, ?)",
                        (name, body.get("registrar", "")))
    return {"id": did}


@api.delete("/domains/{domain_id}")
def domains_delete(domain_id: int, _: None = Depends(require_auth)):
    store.execute("DELETE FROM domains WHERE id=?", (domain_id,))
    return {"ok": True}


@api.post("/domains/{domain_id}/check")
def domains_check(domain_id: int, _: None = Depends(require_auth)):
    import tasks
    rows = store.query("SELECT * FROM domains WHERE id=?", (domain_id,))
    if not rows:
        raise HTTPException(404, "域名不存在")
    state = _wrap(tasks.check_domain, rows[0]["name"])
    import json as _json
    store.execute("UPDATE domains SET last_state=? WHERE id=?",
                  (_json.dumps(state, ensure_ascii=False), domain_id))
    return state


# ================= SSH 会话 / 执行 / SFTP / 转发 =================

class SshBody(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str = "root"
    auth_type: str = "password"
    secret: str = ""
    proxy_command: str = ""
    tags: str = ""
    monitor_cpu: bool = False
    monitor_mem: bool = False
    monitor_disk: bool = False


@api.get("/ssh/sessions")
def ssh_sessions(_: None = Depends(require_auth)):
    rows = store.query("SELECT * FROM ssh_sessions ORDER BY id")
    out = []
    for r in rows:
        try:
            metadata = store.json.loads(r.get("metadata") or "{}")
        except (ValueError, TypeError):
            metadata = {}
        out.append({**r, "metadata": metadata, "secret": "***" if r["secret"] else ""})
    return {"data": out}


@api.post("/ssh/sessions/{session_id}/copy")
def ssh_session_copy(session_id: int, _: None = Depends(require_auth)):
    s = _ssh_or_404(session_id)
    columns = ("host", "port", "username", "auth_type", "secret", "proxy_command",
               "tags", "monitor_cpu", "monitor_mem", "monitor_disk", "metadata")
    sid = store.execute("INSERT INTO ssh_sessions(name," + ",".join(columns) + ") VALUES(" +
                        ",".join(["?"] * (len(columns) + 1)) + ")",
                        (s["name"] + " · 副本", *(s[c] for c in columns)))
    return {"id": sid}


@api.post("/ssh/sessions")
def ssh_session_add(body: SshBody, _: None = Depends(require_auth)):
    sid = store.execute(
        "INSERT INTO ssh_sessions(name, host, port, username, auth_type, secret, proxy_command, "
        "tags, monitor_cpu, monitor_mem, monitor_disk) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (body.name.strip(), body.host.strip(), body.port, body.username.strip(),
         body.auth_type, body.secret, body.proxy_command.strip(), body.tags.strip(),
         1 if body.monitor_cpu else 0, 1 if body.monitor_mem else 0, 1 if body.monitor_disk else 0))
    return {"id": sid}


def _ssh_or_404(session_id: int):
    rows = store.query("SELECT * FROM ssh_sessions WHERE id=?", (session_id,))
    if not rows:
        raise HTTPException(404, "SSH 会话不存在")
    return rows[0]


@api.put("/ssh/sessions/{session_id}")
def ssh_session_update(session_id: int, body: SshBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(session_id)
    secret = body.secret if body.secret and body.secret != "***" else sess["secret"]
    store.execute(
        "UPDATE ssh_sessions SET name=?, host=?, port=?, username=?, auth_type=?, secret=?, "
        "proxy_command=?, tags=?, monitor_cpu=?, monitor_mem=?, monitor_disk=? WHERE id=?",
        (body.name.strip(), body.host.strip(), body.port, body.username.strip(),
         body.auth_type, secret, body.proxy_command.strip(), body.tags.strip(),
         1 if body.monitor_cpu else 0, 1 if body.monitor_mem else 0,
         1 if body.monitor_disk else 0, session_id))
    if (sess["proxy_command"] or "").strip() != body.proxy_command.strip():
        sshpool.close_session_connections(session_id)
    return {"ok": True}


@api.delete("/ssh/sessions/{session_id}")
def ssh_session_delete(session_id: int, _: None = Depends(require_auth)):
    _ssh_or_404(session_id)
    store.execute("DELETE FROM ssh_sessions WHERE id=?", (session_id,))
    sshpool.close_session_connections(session_id)
    return {"ok": True}


@api.post("/ssh/sessions/{session_id}/test")
def ssh_session_test(session_id: int, _: None = Depends(require_auth)):
    sess = _ssh_or_404(session_id)
    return {"output": _wrap(sshpool.test_session, sess)}


class ExecBody(BaseModel):
    ids: list
    cmd: str


@api.post("/ssh/exec")
def ssh_exec(body: ExecBody, _: None = Depends(require_auth)):
    return {"results": _wrap(sshpool.exec_batch, body.ids, body.cmd)}


@api.post("/ssh/monitor")
def ssh_monitor(body: dict, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body["session_id"])
    return _wrap(sshpool.collect_metrics, sess)


@api.get("/ssh/sftp/list")
def sftp_list(session_id: int, path: str = "/", _: None = Depends(require_auth)):
    sess = _ssh_or_404(session_id)
    return {"data": _wrap(sshpool.sftp_list, sess, path or "/")}


@api.get("/ssh/sftp/read")
def sftp_read(session_id: int, path: str, _: None = Depends(require_auth)):
    sess = _ssh_or_404(session_id)
    data = _wrap(sshpool.sftp_read, sess, path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "此文件不是 UTF-8 文本，请下载原文件")
    if "\x00" in text:
        raise HTTPException(400, "二进制文件不能在文本编辑器中修改，请下载原文件")
    return {"content": text, "size": len(data)}


@api.get("/ssh/sftp/download")
def sftp_download(session_id: int, path: str, _: None = Depends(require_auth)):
    from fastapi.responses import Response
    from urllib.parse import quote
    data = _wrap(sshpool.sftp_read, _ssh_or_404(session_id), path, 64_000_000)
    filename = quote(path.rsplit("/", 1)[-1], safe="")
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@api.post("/ssh/sessions/{session_id}/forget-host-key")
def ssh_forget_host_key(session_id: int, _: None = Depends(require_auth)):
    sess = _ssh_or_404(session_id)
    store.execute("DELETE FROM settings WHERE key=?",
                  (f"ssh_hostkey:{sess['host']}:{int(sess['port'] or 22)}",))
    return {"ok": True}


class SftpWriteBody(BaseModel):
    session_id: int
    path: str
    content: str = ""
    content_base64: str = ""


@api.post("/ssh/sftp/write")
def sftp_write(body: SftpWriteBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body.session_id)
    data = (base64.b64decode(body.content_base64) if body.content_base64
            else body.content.encode("utf-8"))
    return _wrap(sshpool.sftp_write, sess, body.path, data)


@api.post("/ssh/sftp/upload")
async def sftp_upload(request: Request, session_id: int, path: str,
                      _: None = Depends(require_auth)):
    """Upload a new binary file; an existing remote file is never overwritten."""
    if not path.startswith("/") or path.endswith("/") or "\\" in path or "\x00" in path \
            or path.rsplit("/", 1)[-1] in ("", ".", ".."):
        raise HTTPException(400, "上传路径必须是有效的绝对文件路径")
    if request.headers.get("content-length", "").isdigit() \
            and int(request.headers["content-length"]) > 100_000_000:
        raise HTTPException(413, "文件超过 100 MB 上传限制")
    sess = _ssh_or_404(session_id)
    try:
        return await sshpool.sftp_upload(sess, path, request.stream())
    except sshpool.SftpUploadTooLarge as e:
        raise HTTPException(413, str(e)) from e
    except (sshpool.SftpUploadExists, asyncssh.SFTPFileAlreadyExists) as e:
        raise HTTPException(409, "远程已有同名文件，请先改名或选择其他文件") from e
    except (sshpool.SshError, asyncssh.Error, OSError) as e:
        raise HTTPException(502, f"SFTP 上传失败: {e}") from e


class SftpPathBody(BaseModel):
    session_id: int
    path: str
    new_path: str = ""
    is_dir: bool = False


@api.post("/ssh/sftp/mkdir")
def sftp_mkdir(body: SftpPathBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body.session_id)
    return _wrap(sshpool.sftp_mkdir, sess, body.path)


@api.post("/ssh/sftp/delete")
def sftp_delete(body: SftpPathBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body.session_id)
    return _wrap(sshpool.sftp_delete, sess, body.path, body.is_dir)


@api.post("/ssh/sftp/rename")
def sftp_rename(body: SftpPathBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body.session_id)
    return _wrap(sshpool.sftp_rename, sess, body.path, body.new_path)


@api.get("/ssh/forwards")
def forwards_list(_: None = Depends(require_auth)):
    return {"data": sshpool.list_forwards()}


class ForwardBody(BaseModel):
    session_id: int
    type: str = "local"
    local_port: int
    remote_host: str
    remote_port: int


@api.post("/ssh/forwards")
def forward_start(body: ForwardBody, _: None = Depends(require_auth)):
    sess = _ssh_or_404(body.session_id)
    if body.type not in ("local", "remote"):
        raise HTTPException(400, "类型必须是 local 或 remote")
    return _wrap(sshpool.start_forward, sess, body.type, body.local_port,
                 body.remote_host, body.remote_port)


@api.post("/ssh/forwards/{fid}/stop")
def forward_stop(fid: int, _: None = Depends(require_auth)):
    return _wrap(sshpool.stop_forward, fid)


@api.post("/ssh/sync-cloud")
def ssh_sync_cloud(body: dict, _: None = Depends(require_auth)):
    """从云账号把实例同步成 SSH 会话（原版「云主机同步」）。"""
    acct = _account_or_404(body["account_id"])
    try:
        rows = oci_service.list_instances(acct)
    except Exception as e:
        raise HTTPException(502, str(e))
    created = 0
    for r in rows:
        ip = r.get("public_ip") or r.get("private_ip") or ""
        if not ip:
            continue
        metadata = store.json.dumps({k: r.get(k) for k in ("shape", "spec", "ocpus", "memory_gbs", "state")})
        if store.query("SELECT id FROM ssh_sessions WHERE host=?", (ip,)):
            store.execute("UPDATE ssh_sessions SET metadata=? WHERE host=?", (metadata, ip))
            continue
        store.execute(
            "INSERT INTO ssh_sessions(name, host, username, auth_type, tags, metadata) VALUES(?,?,?,?,?,?)",
            (r.get("name") or ip, ip, body.get("username", "root"), "password",
             acct["platform"] + ":" + (acct["name"] or ""), metadata))
        created += 1
    return {"created": created, "found": len(rows)}


# ================= 设置 / 邮件 / 面板维护 =================

@api.get("/settings/notify")
def notify_get(_: None = Depends(require_auth)):
    return {k: store.get_setting(k, "") for k in notify.NOTIFY_KEYS}


@api.post("/settings/notify")
def notify_set(body: dict, _: None = Depends(require_auth)):
    for k in notify.NOTIFY_KEYS:
        if k in body:
            store.set_setting(k, str(body[k]).strip())
    return {"ok": True}


@api.post("/settings/notify/test")
def notify_test(_: None = Depends(require_auth)):
    results = notify.send("OCI Panel 测试通知", "配置生效，之后开机结果/告警/域名到期都会推送到这里")
    if not results:
        return {"ok": False, "message": "还没有配置任何通知渠道"}
    return {"ok": True, "results": results}


@api.post("/settings/alerts")
def alerts_set(body: dict, _: None = Depends(require_auth)):
    for k in ("alert_cpu", "alert_mem", "alert_disk"):
        if k in body:
            store.set_setting(k, str(body[k]))
    return {"ok": True}


@api.get("/settings/alerts")
def alerts_get(_: None = Depends(require_auth)):
    return {"alert_cpu": store.get_setting("alert_cpu", "95"),
            "alert_mem": store.get_setting("alert_mem", "95"),
            "alert_disk": store.get_setting("alert_disk", "90")}


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


@api.post("/settings/password")
def password_change(body: PasswordBody, _: None = Depends(require_auth)):
    from deps import hash_pw
    if hash_pw(body.old_password) != (store.get_setting("admin_pass") or ""):
        raise HTTPException(400, "旧密码不对")
    if len(body.new_password) < 12:
        raise HTTPException(400, "新密码至少 12 位")
    store.set_setting("admin_pass", hash_pw(body.new_password))
    import secrets
    store.set_setting("secret", secrets.token_hex(32))
    initial = os.path.join(store.DATA_DIR, "initial_admin_password.txt")
    if os.path.exists(initial):
        os.remove(initial)
    return {"ok": True}


@api.get("/settings/mcp-token")
def mcp_token_get(_: None = Depends(require_auth)):
    return {"token": store.get_setting("mcp_token", "")}


@api.post("/settings/mcp-token/regenerate")
def mcp_token_regen(_: None = Depends(require_auth)):
    import secrets as _secrets
    tok = "mcp_" + _secrets.token_hex(16)
    store.set_setting("mcp_token", tok)
    return {"token": tok}


# ---- 邮件服务 ----

@api.get("/mail/smtp")
def mail_get(_: None = Depends(require_auth)):
    cfg = email_service.smtp_config()
    cfg["email_pass"] = "***" if cfg.get("email_pass") else ""
    return cfg


@api.post("/mail/smtp")
def mail_set(body: dict, _: None = Depends(require_auth)):
    cur = email_service.smtp_config()
    for k in ("email_host", "email_port", "email_ssl", "email_user", "email_pass",
              "email_from", "email_to"):
        v = str(body.get(k, "")).strip()
        if v and v != "***":
            cur[k] = v
    email_service.save_smtp(cur)
    return {"ok": True}


@api.post("/mail/test")
def mail_test(body: dict, _: None = Depends(require_auth)):
    return _wrap(email_service.test_send, body.get("to", ""))


@api.post("/mail/oci-setup")
def mail_oci_setup(body: dict, _: None = Depends(require_auth)):
    acct = _account_or_404(body["account_id"])
    return _wrap(email_service.setup_oci_email_domain, acct, body["domain"],
                 body.get("selector", "ocipanel"), bool(body.get("push_dns")),
                 body.get("from_addr", ""))


@api.get("/mail/oci-domains")
def mail_oci_domains(account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return {"data": _wrap(email_service.list_email_domains, acct)}


@api.delete("/mail/oci-domains/{domain_id}")
def mail_oci_domain_delete(domain_id: str, account_id: int, _: None = Depends(require_auth)):
    acct = _account_or_404(account_id)
    return _wrap(email_service.delete_email_domain, acct, domain_id)


# ---- 面板维护 ----

VERSION = "2.2.0"


@api.get("/panel/version")
def panel_version(_: None = Depends(require_auth)):
    return {"version": VERSION}


@api.get("/panel/log")
def panel_log(lines: int = 200, _: None = Depends(require_auth)):
    path = os.path.join(store.DATA_DIR, "panel.log")
    if not os.path.exists(path):
        return {"log": "(日志文件还没生成)"}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.readlines()
    return {"log": "".join(content[-lines:])}


@api.post("/panel/restart")
def panel_restart(_: None = Depends(require_auth)):
    import threading

    def _die():
        time.sleep(0.5)
        os._exit(1)
    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "面板即将重启（systemd/Docker 会自动拉起；nohup 模式请手动启动）"}


@api.post("/panel/upgrade")
def panel_upgrade(_: None = Depends(require_auth)):
    import subprocess
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(app_dir, ".git")):
        return {"ok": False, "message": "请更新项目文件后执行 docker compose up -d --build；数据目录会保留"}
    r = subprocess.run(["git", "pull"], cwd=app_dir, capture_output=True, text=True, timeout=60)
    return {"ok": r.returncode == 0, "message": (r.stdout + r.stderr)[:500]}

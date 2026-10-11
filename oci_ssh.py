"""Associate OCI instances with saved SSH sessions without replacing credentials or routes."""
import threading
import ipaddress

import store
from oci_service import OciError

_lock = threading.Lock()


def metadata_of(row):
    try:
        value = store.json.loads(row.get("metadata") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _check_binding(acct, instance, session_id):
    rows = store.query("SELECT * FROM ssh_sessions WHERE id=?", (session_id,))
    if not rows:
        raise OciError("所选 SSH 会话不存在")
    if metadata_of(rows[0]).get("source") in ("gcp", "aws"):
        raise OciError("所选 SSH 会话属于其他云平台，已阻止覆盖")
    metadata = metadata_of(rows[0])
    if metadata.get("cloud_provider") == "oci" and metadata.get("cloud_account_id") != acct["id"]:
        raise OciError("所选 SSH 会话记录属于其他账号，已阻止覆盖")
    for table in ("gcp_ssh_links", "aws_ssh_links"):
        if store.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)):
            if store.query(f"SELECT session_id FROM {table} WHERE session_id=?", (session_id,)):
                raise OciError("所选 SSH 会话属于其他云平台，已阻止覆盖")
    links = store.query("SELECT * FROM oci_ssh_links WHERE session_id=?", (session_id,))
    identity = (acct["id"], acct.get("region") or "", instance["id"])
    current = store.query("SELECT session_id FROM oci_ssh_links WHERE account_id=? AND region=? AND instance_id=?", identity)
    if current and current[0]["session_id"] != session_id:
        raise OciError("当前实例已关联另一 SSH 会话，已保留原关联")
    if links:
        link = links[0]
        if (link["account_id"], link["region"], link["instance_id"]) != identity:
            raise OciError("所选 SSH 会话已关联其他账号或实例，已阻止覆盖")
    else:
        # Private addresses can repeat in independent tenants and proxy networks.
        hosts = [instance.get("public_ip")]
        for address in instance.get("ipv6_addresses") or []:
            try:
                if ipaddress.IPv6Address(address).is_global:
                    hosts.append(address)
            except ValueError:
                pass
        if rows[0]["host"] not in [host for host in hosts if host]:
            raise OciError("所选 SSH 主机地址与当前实例公网地址不一致，已阻止关联")
    return rows[0]


def validate_binding(acct, instance, session_id):
    with _lock:
        return _check_binding(acct, instance, session_id)


def sync_instance(acct, instance, create=False, username="root", proxy_command="", session_id=None):
    identity = (acct["id"], acct.get("region") or "", instance["id"])
    tag = "oci:" + acct["name"]
    addresses = list(instance.get("ipv6_addresses") or [])
    hosts = [h for h in (instance.get("public_ip"), *addresses, instance.get("private_ip")) if h]
    with _lock:
        selected = _check_binding(acct, instance, session_id) if session_id is not None else None
        linked = store.query("SELECT s.* FROM ssh_sessions s JOIN oci_ssh_links l ON l.session_id=s.id "
                             "WHERE l.account_id=? AND l.region=? AND l.instance_id=?", identity)
        if selected is not None:
            if linked and linked[0]["id"] != session_id:
                raise OciError("当前实例已关联另一 SSH 会话，已保留原关联")
            linked = [selected]
        # Adopt legacy OCI sessions only when both account tag and host identify one unbound session.
        if not linked and hosts and len(store.query("SELECT id FROM accounts WHERE platform='oci' AND name=?", (acct["name"],))) == 1:
            candidates = store.query("SELECT s.* FROM ssh_sessions s WHERE s.tags=? AND s.host IN (" +
                                     ",".join("?" * len(hosts)) + ") AND NOT EXISTS "
                                     "(SELECT 1 FROM oci_ssh_links l WHERE l.session_id=s.id)", (tag, *hosts))
            candidates = [s for s in candidates if metadata_of(s).get("cloud_provider") in (None, "oci")
                          and metadata_of(s).get("source") not in ("gcp", "aws")]
            if len(candidates) == 1:
                linked = candidates
        created = False
        if not linked:
            if not create or not hosts:
                return {"updated": 0, "created": 0}
            sid = store.execute("INSERT INTO ssh_sessions(name,host,username,auth_type,proxy_command,tags) VALUES(?,?,?,?,?,?)",
                                (instance.get("name") or hosts[0], hosts[0], username, "password", proxy_command, tag))
            linked = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))
            created = True
        session = linked[0]
        store.execute("INSERT OR REPLACE INTO oci_ssh_links(account_id,region,instance_id,session_id) VALUES(?,?,?,?)",
                      (*identity, session["id"]))
        metadata = metadata_of(session)
        metadata.update({k: instance[k] for k in ("shape", "spec", "ocpus", "memory_gbs", "state") if k in instance})
        metadata.update(cloud_provider="oci", cloud_account_id=acct["id"],
                        cloud_region=identity[1], cloud_instance_id=instance["id"], ipv6_addresses=addresses)
        # Only descriptive metadata changes. Host, secret, proxy and connection revision remain intact.
        store.execute("UPDATE ssh_sessions SET metadata=? WHERE id=?",
                      (store.json.dumps(metadata, ensure_ascii=False), session["id"]))
        return {"updated": 1, "created": int(created), "session_id": session["id"]}

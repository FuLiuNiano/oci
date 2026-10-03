"""Cloudflare DNS 管理 + 域名导入（供 DNS 管理和域名监控使用）。"""
import requests

import store


class CfError(Exception):
    pass


def _headers(acct_or_params):
    p = acct_or_params.get("params", acct_or_params) if isinstance(acct_or_params, dict) else {}
    if p.get("cf_api_token"):
        return {"Authorization": f"Bearer {p['cf_api_token']}"}
    if p.get("cf_email") and p.get("cf_account_key"):
        return {"X-Auth-Email": p["cf_email"], "X-Auth-Key": p["cf_account_key"]}
    raise CfError("未配置 Cloudflare 凭据（cf_api_token 或 cf_email + cf_account_key）")


def _call(method, path, acct, params=None, json=None):
    r = requests.request(method, f"https://api.cloudflare.com/client/v4{path}",
                         headers=_headers(acct), params=params, json=json, timeout=25)
    try:
        j = r.json()
    except Exception:
        raise CfError(f"Cloudflare {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400 or not j.get("success", False):
        errs = "; ".join(e.get("message", "") for e in (j.get("errors") or []))
        raise CfError(f"Cloudflare {r.status_code}: {errs or r.text[:200]}")
    return j


def test(params):
    # DNS management requires zone access; this works with either credential type.
    return {"ok": True, "zones": len(list_zones(params))}


def list_zones(params):
    out, page = [], 1
    while True:
        j = _call("GET", "/zones", params, params={"per_page": 50, "page": page})
        out.extend({"id": z["id"], "name": z["name"],
                    "status": z.get("status", ""), "created": z.get("created_on", "")}
                   for z in j.get("result", []))
        info = j.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            break
        page += 1
    return out


def list_records(params, zone_id, qtype="", name=""):
    out, page = [], 1
    while True:
        prm = {"per_page": 100, "page": page}
        if qtype:
            prm["type"] = qtype
        if name:
            prm["name"] = name
        j = _call("GET", f"/zones/{zone_id}/dns_records", params, params=prm)
        out.extend({
            "id": r["id"], "type": r["type"], "name": r["name"],
            "content": r["content"], "ttl": r.get("ttl"), "proxied": r.get("proxied"),
        } for r in j.get("result", []))
        info = j.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            break
        page += 1
    return out


def create_record(params, zone_id, rtype, name, content, ttl=1, proxied=False, priority=None):
    body = {"type": rtype, "name": name, "content": content, "ttl": ttl, "proxied": proxied}
    if priority is not None:
        body["priority"] = int(priority)
    j = _call("POST", f"/zones/{zone_id}/dns_records", params, json=body)
    return j.get("result", {})


def update_record(params, zone_id, record_id, rtype, name, content, ttl=1, proxied=False):
    j = _call("PUT", f"/zones/{zone_id}/dns_records/{record_id}", params, json={
        "type": rtype, "name": name, "content": content, "ttl": ttl, "proxied": proxied})
    return j.get("result", {})


def delete_record(params, zone_id, record_id):
    _call("DELETE", f"/zones/{zone_id}/dns_records/{record_id}", params)
    return {"ok": True}


def import_domains_into_monitor(params):
    """把 CF 账号下的所有域名导入域名监控表（去重）。"""
    zones = list_zones(params)
    added = 0
    for z in zones:
        exists = store.query("SELECT id FROM domains WHERE name=?", (z["name"],))
        if not exists:
            store.execute("INSERT INTO domains(name, registrar) VALUES(?, 'cloudflare')", (z["name"],))
            added += 1
    return {"zones": len(zones), "added": added}

"""邮件服务：SMTP 配置测试发信 + OCI Email Delivery 一键搭建发信域名（DKIM 可自动推到 Cloudflare）。"""
import oci

import cloudflare_service
import store


class MailError(Exception):
    pass


def smtp_config():
    keys = ("email_host", "email_port", "email_ssl", "email_user", "email_pass",
            "email_from", "email_to")
    return {k: store.get_setting(k, "") for k in keys}


def save_smtp(cfg: dict):
    for k, v in cfg.items():
        store.set_setting(k, str(v).strip())


def test_send(to=""):
    from email.header import Header
    from email.mime.text import MIMEText
    import smtplib

    cfg = smtp_config()
    to = to or cfg.get("email_to", "")
    if not cfg.get("email_host") or not to:
        raise MailError("请先配置 SMTP 服务器和收件人")
    msg = MIMEText("OCI Panel 邮件服务测试成功。", "plain", "utf-8")
    msg["Subject"] = Header("OCI Panel 测试邮件", "utf-8")
    msg["From"] = cfg.get("email_from", "")
    msg["To"] = to
    port = int(cfg.get("email_port") or 465)
    if cfg.get("email_ssl", "1") == "1":
        server = smtplib.SMTP_SSL(cfg["email_host"], port, timeout=20)
    else:
        server = smtplib.SMTP(cfg["email_host"], port, timeout=20)
        server.starttls()
    try:
        if cfg.get("email_user"):
            server.login(cfg["email_user"], cfg.get("email_pass", ""))
        server.sendmail(cfg.get("email_from", ""), to.split(","), msg.as_string())
    finally:
        server.quit()
    return {"ok": True}


# ---------- OCI Email Delivery 一键搭建 ----------

def setup_oci_email_domain(acct, domain, selector="ocipanel", push_dns=False, from_addr=""):
    """创建 Email Domain + DKIM + Approved Sender，返回需要添加的 DNS 记录（可选自动推送到 CF）。"""
    import oci_service

    comp = oci_service.compartment_of(acct)
    email = oci_service._client(oci.email.EmailClient, acct)
    tenancy = acct["params"].get("tenancy_ocid", "")

    dom = email.create_email_domain(oci.email.models.CreateEmailDomainDetails(
        compartment_id=comp, name=domain, description="created by oci-panel")).data

    dkim = email.create_dkim(oci.email.models.CreateDkimDetails(
        email_domain_id=dom.id, name=selector, description="created by oci-panel")).data

    sender = None
    if from_addr:
        try:
            sender = email.create_sender(oci.email.models.CreateSenderDetails(
                compartment_id=comp, email_address=from_addr)).data
        except oci.exceptions.ServiceError as e:
            sender = {"error": oci_service.fmt_err(e)}

    dns = [
        {"type": "CNAME", "name": dkim.dns_subdomain_name, "content": dkim.cname_record_value},
    ]

    pushed = []
    if push_dns:
        cf_params = store.get_setting("cf_params", "")
        try:
            params = store.get_json_setting("cf_params", None) or {}
            zones = {z["name"]: z["id"] for z in cloudflare_service.list_zones(params)}
            matches = [z for z in zones if domain == z or domain.endswith("." + z)]
            zid = zones[max(matches, key=len)] if matches else None
            if not zid:
                raise MailError(f"Cloudflare 里找不到 {domain}（或其父域），请手动添加 DNS")
            for rec in dns:
                cloudflare_service.create_record(params, zid, rec["type"], rec["name"], rec["content"])
                pushed.append(rec["name"])
        except cloudflare_service.CfError as e:
            return {"domain": dom.id, "dkim_id": dkim.id, "dns": dns,
                    "push_error": str(e), "pushed": pushed}
        except MailError:
            raise

    return {
        "domain_id": dom.id, "dkim_id": dkim.id,
        "sender_id": getattr(sender, "id", sender),
        "smtp_note": "先添加 DKIM 记录并等待 OCI 验证。SMTP 凭据可在用户管理中生成；Approved Sender 创建失败时请在 DKIM 生效后重试。",
        "sender_error": sender.get("error", "") if isinstance(sender, dict) else "",
        "dns": dns, "pushed": pushed,
    }


def list_email_domains(acct):
    import oci_service
    email = oci_service._client(oci.email.EmailClient, acct)
    comp = oci_service.compartment_of(acct)
    out = []
    for d in email.list_email_domains(compartment_id=comp).data:
        out.append({"id": d.id, "name": d.name, "state": d.lifecycle_state})
    return out


def delete_email_domain(acct, domain_id):
    import oci_service
    oci_service._client(oci.email.EmailClient, acct).delete_email_domain(domain_id)
    return {"ok": True}

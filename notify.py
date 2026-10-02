"""通知推送：全部可选，用你自己的 Bark / Webhook / 邮件 SMTP，没有任何强制绑定。"""
import smtplib
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText

import requests

import store


def _bark(base, title, content):
    base = base.strip().rstrip("/")
    url = f"{base}/{urllib.parse.quote(title, safe='')}/{urllib.parse.quote(content or ' ', safe='')}"
    return requests.get(url, timeout=15).ok


def _webhook(url, title, content):
    return requests.post(
        url, json={"title": title, "content": content}, timeout=15
    ).ok


def _email(cfg, title, content):
    msg = MIMEText(content or " ", "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = cfg.get("email_from", "")
    msg["To"] = cfg.get("email_to", "")
    port = int(cfg.get("email_port") or 465)
    if cfg.get("email_ssl", "1") == "1":
        server = smtplib.SMTP_SSL(cfg.get("email_host", ""), port, timeout=20)
    else:
        server = smtplib.SMTP(cfg.get("email_host", ""), port, timeout=20)
        server.starttls()
    try:
        if cfg.get("email_user"):
            server.login(cfg["email_user"], cfg.get("email_pass", ""))
        server.sendmail(cfg.get("email_from", ""), cfg.get("email_to", "").split(","), msg.as_string())
    finally:
        server.quit()
    return True


NOTIFY_KEYS = (
    "notify_bark_url", "notify_webhook",
    "email_host", "email_port", "email_ssl", "email_user", "email_pass",
    "email_from", "email_to",
)


def send(title, content=""):
    """向所有已配置的渠道发送，返回 {渠道: 结果}；一个渠道都没配置则返回空 dict。"""
    results = {}
    s = {r["key"]: (r["value"] or "") for r in store.query("SELECT key, value FROM settings")}
    text = f"{title}\n{content}".strip()

    if s.get("notify_bark_url"):
        try:
            results["bark"] = "ok" if _bark(s["notify_bark_url"], title, content) else "发送失败"
        except Exception as e:
            results["bark"] = str(e)
    if s.get("notify_webhook"):
        try:
            results["webhook"] = "ok" if _webhook(s["notify_webhook"], title, content) else "发送失败"
        except Exception as e:
            results["webhook"] = str(e)
    if s.get("email_host") and s.get("email_to"):
        try:
            _email(s, title, content)
            results["email"] = "ok"
        except Exception as e:
            results["email"] = str(e)
    return results

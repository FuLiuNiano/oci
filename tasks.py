"""后台任务线程：开机抢机重试、停机自动拉起、流量超额关停、域名到期监控、SSH 资源告警。"""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import oci
from oci.exceptions import ServiceError

import notify
import oci_service
import store

CYCLE_SECONDS = 60
TRAFFIC_EVERY_SECONDS = 3600
DOMAIN_EVERY_SECONDS = 12 * 3600
ALERT_EVERY_SECONDS = 300

_state = {"last_traffic": 0, "last_domain": 0, "last_alert": 0}
_started = False
_stop_event = threading.Event()
_worker = None
_launch_locks = {}
_launch_locks_guard = threading.Lock()


def launch_lock(task_id):
    with _launch_locks_guard:
        return _launch_locks.setdefault(task_id, threading.RLock())


def start():
    global _started, _worker
    if _started:
        return
    _started = True
    _stop_event.clear()
    _worker = threading.Thread(target=_loop, daemon=True, name="panel-worker")
    _worker.start()


def stop():
    global _started
    _stop_event.set()
    if _worker:
        _worker.join(timeout=5)
    _started = False


def _loop():
    while not _stop_event.is_set():
        now = time.time()
        try:
            _tick_launch_tasks()
        except Exception:
            traceback.print_exc()
        try:
            _tick_auto_restart()
        except Exception:
            traceback.print_exc()
        if now - _state["last_traffic"] >= TRAFFIC_EVERY_SECONDS:
            _state["last_traffic"] = now
            try:
                _tick_traffic()
            except Exception:
                traceback.print_exc()
        if now - _state["last_domain"] >= DOMAIN_EVERY_SECONDS:
            _state["last_domain"] = now
            try:
                _tick_domains()
            except Exception:
                traceback.print_exc()
        if now - _state["last_alert"] >= ALERT_EVERY_SECONDS:
            _state["last_alert"] = now
            try:
                _tick_ssh_alerts()
            except Exception:
                traceback.print_exc()
        _stop_event.wait(CYCLE_SECONDS)


# ---------- 开机抢机 ----------

def _tick_launch_tasks():
    for task in store.query("SELECT * FROM launch_tasks WHERE status='running'"):
        with launch_lock(task["id"]):
            current = store.query("SELECT * FROM launch_tasks WHERE id=? AND status='running'", (task["id"],))
            if current:
                _run_launch_task(current[0])


def _run_launch_task(task):
    rows = store.query("SELECT * FROM accounts WHERE id=?", (task["account_id"],))
    if not rows:
        store.execute(
            "UPDATE launch_tasks SET status='failed', last_error='账号已删除', "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (task["id"],),
        )
        return
    acct = rows[0]
    if acct["platform"] != "oci":
        store.execute(
            "UPDATE launch_tasks SET status='failed', last_error='抢机仅支持 OCI 账号', "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (task["id"],),
        )
        return
    try:
        instance_id, _ad = oci_service.launch_once(acct, task)
    except oci_service.OciError as e:
        _fail_task(task["id"], str(e))
        notify.send("开机任务失败", f"{task['display_name']}：{e}")
    except ServiceError as e:
        err = f"OCI {e.status} {e.code or ''}: {(e.message or '')[:200]}"
        if oci_service.is_transient(e):
            _retry_task(task["id"], err)
        else:
            _fail_task(task["id"], err)
            notify.send("开机任务失败", f"{task['display_name']}：{err}")
    except Exception as e:
        record = _retry_task if oci_service.is_transient(e) else _fail_task
        record(task["id"], f"{e.__class__.__name__}: {str(e)[:200]}")
    else:
        store.execute(
            "UPDATE launch_tasks SET status='success', instance_id=?, attempts=attempts+1, "
            "last_error='', updated_at=datetime('now','localtime') WHERE id=?",
            (instance_id, task["id"]),
        )
        threading.Thread(
            target=_wait_ip_and_notify,
            args=(dict(acct), task["display_name"], instance_id),
            daemon=True,
        ).start()


def _fail_task(task_id, err):
    store.execute(
        "UPDATE launch_tasks SET status='failed', last_error=?, attempts=attempts+1, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (err, task_id),
    )


def _retry_task(task_id, err):
    store.execute(
        "UPDATE launch_tasks SET last_error=?, attempts=attempts+1, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (err, task_id),
    )


def _wait_ip_and_notify(acct, name, instance_id):
    """开机提交成功后等公网 IP 分配出来再通知，最多等 2 分钟。"""
    for _ in range(12):
        time.sleep(10)
        try:
            for ins in oci_service.list_instances(acct):
                if ins["id"] == instance_id:
                    if ins["public_ip"]:
                        notify.send("开机成功", f"{name} 已创建\n公网IP: {ins['public_ip']}\n区域: {acct['region']}")
                        return
                    break
        except Exception:
            pass
    notify.send("开机成功", f"{name} 已创建，IP 分配中，稍后在面板查看（实例 {instance_id[:26]}…）")


# ---------- 停机自动拉起 ----------

def _tick_auto_restart():
    for acct in store.query("SELECT * FROM accounts WHERE auto_restart=1 AND platform='oci'"):
        try:
            instances = oci_service.list_instances(acct)
        except Exception as e:
            print(f"[auto-restart] 账号 {acct['name']} 查询失败: {e}")
            continue
        blocked = store.get_json_setting(f"traffic_block:{acct['id']}", []) or []
        if acct["traffic_limit_gb"] <= 0 or acct["traffic_action"] != "stop":
            blocked = []
            store.set_json_setting(f"traffic_block:{acct['id']}", [])
        manual = store.get_json_setting(f"manual_stop:{acct['id']}", []) or []
        for ins in instances:
            if ins["state"] == "STOPPED" and ins["id"] not in blocked + manual:
                try:
                    oci_service.instance_action(acct, ins["id"], "START")
                    notify.send("停机自动拉起", f"实例 {ins['name']} 处于 STOPPED，已发送开机指令")
                    time.sleep(2)
                except Exception as e:
                    print(f"[auto-restart] {ins['name']} 启动失败: {e}")


# ---------- 流量超额关停 ----------

def _tick_traffic():
    for acct in store.query("SELECT * FROM accounts WHERE platform='oci' AND traffic_limit_gb>0"):
        try:
            usage = oci_service.traffic_usage_gb(acct, hours=24)
        except Exception as e:
            print(f"[traffic] 账号 {acct['name']} 流量查询失败: {e}")
            continue
        total = usage.get("total_gb", 0)
        limit = acct["traffic_limit_gb"]
        if total < limit:
            continue
        names = {i["id"]: i["name"] for i in oci_service.list_instances(acct)}
        detail = "\n".join(f"{names.get(k, k[-12:])}: {round(v, 2)}GB"
                           for k, v in usage.get("per_resource", {}).items())
        notify.send("流量超额告警", f"账号 {acct['name']} 近24h出站 {total}GB / 阈值 {limit}GB\n{detail}")
        if acct.get("traffic_action") == "stop":
            for ins in oci_service.list_instances(acct):
                if ins["state"] == "RUNNING":
                    blocked = store.get_json_setting(f"traffic_block:{acct['id']}", []) or []
                    if ins["id"] not in blocked:
                        blocked.append(ins["id"])
                        store.set_json_setting(f"traffic_block:{acct['id']}", blocked)
                    try:
                        oci_service.instance_action(acct, ins["id"], "SOFTSTOP")
                        notify.send("流量超额自动关停", f"实例 {ins['name']} 已关机")
                        time.sleep(2)
                    except Exception as e:
                        print(f"[traffic] {ins['name']} 关停失败: {e}")


# ---------- 域名 / SSL 到期监控 ----------

def check_domain(name):
    """返回 {ssl_days, domain_days, error}。"""
    import socket
    import ssl as sslmod

    state = {"ssl_days": None, "domain_days": None, "error": ""}
    # SSL 证书
    try:
        ctx = sslmod.create_default_context()
        with socket.create_connection((name, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=name) as tls:
                der = tls.getpeercert(binary_form=True)
        import cryptography.x509 as x509
        cert = x509.load_der_x509_certificate(der)
        not_after = cert.not_valid_after_utc
        state["ssl_days"] = (not_after - datetime.now(timezone.utc)).days
    except Exception as e:
        state["error"] = f"SSL检查失败: {e}"
    # 域名到期（RDAP）
    try:
        import requests
        r = requests.get(f"https://rdap.org/domain/{name}", timeout=15,
                         headers={"Accept": "application/rdap+json"})
        if r.status_code == 200:
            for ev in r.json().get("events", []):
                if ev.get("eventAction") == "expiration":
                    exp = str(ev.get("eventDate", ""))[:19]
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
                        try:
                            d = datetime.strptime(exp, fmt).replace(tzinfo=timezone.utc)
                            state["domain_days"] = (d - datetime.now(timezone.utc)).days
                            break
                        except ValueError:
                            continue
                    break
    except Exception as e:
        state["error"] = (state["error"] + " " if state["error"] else "") + f"RDAP失败: {e}"
    return state


def _tick_domains():
    thresholds = [30, 14, 7]
    for dom in store.query("SELECT * FROM domains"):
        state = check_domain(dom["name"])
        prev = {}
        try:
            import json as _json
            prev = _json.loads(dom.get("last_state") or "{}")
        except Exception:
            pass
        store.execute("UPDATE domains SET last_state=? WHERE id=?",
                      (_json.dumps(state, ensure_ascii=False), dom["id"]))
        msgs = []
        ssl_days = state.get("ssl_days")
        dom_days = state.get("domain_days")
        old_ssl = prev.get("ssl_days")
        old_dom = prev.get("domain_days")
        if ssl_days is not None:
            level = next((t for t in thresholds if ssl_days <= t), None)
            old_level = next((t for t in thresholds if isinstance(old_ssl, int) and old_ssl <= t), None)
            if level is not None and level != old_level:
                msgs.append(f"SSL 证书还剩 {ssl_days} 天到期")
        if dom_days is not None:
            level = next((t for t in thresholds if dom_days <= t), None)
            old_level = next((t for t in thresholds if isinstance(old_dom, int) and old_dom <= t), None)
            if level is not None and level != old_level:
                msgs.append(f"域名注册还剩 {dom_days} 天到期")
        if msgs:
            notify.send("域名到期提醒", f"{dom['name']}\n" + "\n".join(msgs))


# ---------- SSH 资源告警 ----------

def _tick_ssh_alerts():
    import sshpool

    alert_cpu = float(store.get_setting("alert_cpu", "95") or 95)
    alert_mem = float(store.get_setting("alert_mem", "95") or 95)
    alert_disk = float(store.get_setting("alert_disk", "90") or 90)
    cooldown = 6 * 3600
    now = time.time()
    last_alerts = store.get_json_setting("ssh_alert_times", {})

    for sess in store.query("SELECT * FROM ssh_sessions WHERE monitor_cpu=1 OR monitor_mem=1 OR monitor_disk=1"):
        try:
            m = sshpool.collect_metrics(sess)
        except Exception as e:
            print(f"[alert] {sess['name']} 指标采集失败: {e}")
            continue
        msgs = []
        if sess["monitor_cpu"] and m.get("cpu") is not None and m["cpu"] >= alert_cpu:
            msgs.append(f"CPU {m['cpu']}%")
        if sess["monitor_mem"] and m.get("mem") is not None and m["mem"] >= alert_mem:
            msgs.append(f"内存 {m['mem']}%")
        if sess["monitor_disk"] and m.get("disk") is not None and m["disk"] >= alert_disk:
            msgs.append(f"磁盘 {m['disk']}%")
        if not msgs:
            continue
        key = str(sess["id"])
        if now - last_alerts.get(key, 0) < cooldown:
            continue
        last_alerts[key] = now
        store.set_json_setting("ssh_alert_times", last_alerts)
        notify.send("服务器资源告警", f"{sess['name']}（{sess['host']}）\n" + "、".join(msgs))

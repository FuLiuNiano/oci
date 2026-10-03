from types import SimpleNamespace
import sqlite3

import oci

import host_monitor
import store
import oci_service


def test_host_metrics_two_samples_and_cache(monkeypatch):
    samples = [
        {"stat": "cpu 100 0 100 800 0 0 0 0", "net": "eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0"},
        {"stat": "cpu 130 0 110 860 0 0 0 0", "net": "eth0: 3000 0 0 0 0 0 0 0 6000 0 0 0 0 0 0 0"},
    ]
    state = {"sample": 0, "time": 10.0}
    monkeypatch.setattr(host_monitor, "_previous", None)
    monkeypatch.setattr(host_monitor, "_cached", None)
    monkeypatch.setattr(host_monitor.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(host_monitor.Path, "exists", lambda p: str(p).replace("\\", "/") == "/proc/stat")
    def read(path):
        path = str(path).replace("\\", "/")
        if path.endswith("/stat"):
            return samples[state["sample"]]["stat"]
        if path.endswith("meminfo"):
            return "MemTotal: 1024 kB\nMemAvailable: 512 kB\nMemFree: 100 kB"
        if path.endswith("/dev"):
            return "header\nheader\n" + samples[state["sample"]]["net"] + "\nlo: 9000 0 0 0 0 0 0 0 9000 0 0 0 0 0 0 0"
        if path.endswith("status"):
            return "Name: python\nVmRSS: 200 kB"
        raise AssertionError(path)
    monkeypatch.setattr(host_monitor, "_read", read)
    monkeypatch.setattr(host_monitor.shutil, "disk_usage", lambda _: SimpleNamespace(used=100, total=1000))
    first = host_monitor.snapshot()
    assert first["cpu"] is None and first["network_rx"] is None
    assert first["memory_used"] == 512 * 1024
    state.update(sample=1, time=12)
    second = host_monitor.snapshot()
    assert second["cpu"] == 40.0
    assert second["network_rx"] == 1000 and second["network_tx"] == 2000
    assert second["app_memory"] == 200 * 1024
    state.update(time=12.5)
    assert host_monitor.snapshot() == second


def test_copy_session_preserves_private_credentials_without_exposing(client):
    body = {"name": "demo", "host": "203.0.113.10", "secret": "synthetic-test-only"}
    sid = client.post("/api/ssh/sessions", json=body).json()["id"]
    store.execute("UPDATE ssh_sessions SET metadata=? WHERE id=?", ('{"spec":"1C/1G"}', sid))
    result = client.post(f"/api/ssh/sessions/{sid}/copy")
    assert result.status_code == 200
    copied = store.query("SELECT * FROM ssh_sessions WHERE id=?", (result.json()["id"],))[0]
    assert copied["secret"] == body["secret"] and copied["metadata"] == '{"spec":"1C/1G"}'
    public = client.get("/api/ssh/sessions").json()["data"]
    assert all(s["secret"] == "***" and s["metadata"] == {"spec": "1C/1G"} for s in public)
    assert body["secret"] not in client.get("/api/ssh/sessions").text


def test_new_endpoints_require_authentication(client):
    client.post("/api/logout")
    for route in ["/api/panel/metrics", "/api/accounts/1/regions", "/api/accounts/1/traffic"]:
        assert client.get(route).status_code == 401
    for route in ["/api/accounts/1/check", "/api/ssh/sessions/1/copy"]:
        assert client.post(route).status_code == 401


def test_health_checks_expose_partial_permission_failure(client, account, monkeypatch):
    monkeypatch.setattr(oci_service, "test_connection", lambda _: ["AD1"])
    monkeypatch.setattr(oci_service, "list_instances", lambda _: [])
    def denied(_):
        raise oci_service.OciError("区域订阅权限不足")
    monkeypatch.setattr(oci_service, "subscribed_regions", denied)
    data = client.post(f'/api/accounts/{account["id"]}/check').json()
    assert not data["ok"]
    assert [c["ok"] for c in data["checks"]] == [True, True, False]
    assert "private_key" not in str(data)


def test_old_database_migration_retains_sessions(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(store.SCHEMA)
        conn.execute("INSERT INTO ssh_sessions(name,host,secret) VALUES(?,?,?)",
                     ("retained", "203.0.113.20", "synthetic-old-secret"))
    if store._conn:
        store._conn.close()
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", str(path))
    try:
        store.init_db()
        store.init_db()
        row = store.query("SELECT * FROM ssh_sessions")[0]
        assert row["secret"] == "synthetic-old-secret" and row["metadata"] == "{}"
    finally:
        store._conn.close()
        store._conn = None


def test_subscriptions_and_billing_real_sdk_models(sdk, account):
    data, calls = sdk
    data["list_region_subscriptions"] = [oci.identity.models.RegionSubscription(
        region_name="ap-singapore-1", status="READY", is_home_region=True)]
    assert oci_service.subscribed_regions(account) == [
        {"region": "ap-singapore-1", "status": "READY", "home": True}]
    data["request_summarized_usages"] = oci.usage_api.models.UsageAggregation(items=[
        oci.usage_api.models.UsageSummary(computed_amount=1.25, currency="USD")])
    result = oci_service.usage_cost(account)
    assert result["total"] == 1.25 and result["currencies"] == {"USD": 1.25}
    assert [c[0] for c in calls] == ["list_region_subscriptions", "request_summarized_usages"]


def test_cgroup_metrics_use_container_limit_not_host_capacity(monkeypatch):
    state = {"time": 10.0, "usage": 1000000}
    monkeypatch.setattr(host_monitor, "_previous", None)
    monkeypatch.setattr(host_monitor, "_cached", None)
    monkeypatch.setattr(host_monitor.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(host_monitor.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(host_monitor.Path, "exists", lambda _: True)
    def read(path):
        path = str(path).replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        return {"stat":"cpu 10 0 10 100 0 0 0 0", "meminfo":"MemTotal: 33554432 kB\nMemAvailable: 20000000 kB",
                "memory.max":str(1024**3), "memory.current":str(500*1024**2),
                "cpu.max":"100000 100000", "cpu.stat":f'usage_usec {state["usage"]}',
                "dev":"header\nheader\neth0: 10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0",
                "status":"VmRSS: 200 kB"}[name]
    monkeypatch.setattr(host_monitor, "_read", read)
    monkeypatch.setattr(host_monitor.shutil, "disk_usage", lambda _: SimpleNamespace(used=100, total=1000))
    host_monitor.snapshot()
    state.update(time=12, usage=2000000)
    result = host_monitor.snapshot()
    assert result["scope"] == "container" and result["cores"] == 1
    assert result["cpu"] == 50.0 and result["memory_total"] == 1024**3


def test_cloud_sync_updates_metadata_preserving_saved_credentials(client, account, monkeypatch):
    sid = client.post("/api/ssh/sessions", json={"name":"custom", "host":"203.0.113.10",
                     "username":"ubuntu", "secret":"synthetic-sync-secret"}).json()["id"]
    monkeypatch.setattr(oci_service, "list_instances", lambda _: [{"name":"cloud-name", "public_ip":"203.0.113.10",
        "shape":"VM.Standard.A1.Flex", "spec":"2C/12G", "memory_gbs":12, "state":"RUNNING"}])
    result = client.post("/api/ssh/sync-cloud", json={"account_id":account["id"]})
    assert result.status_code == 200 and result.json()["created"] == 0
    saved = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))[0]
    assert saved["name"] == "custom" and saved["secret"] == "synthetic-sync-secret"
    assert client.get("/api/ssh/sessions").json()["data"][0]["metadata"]["spec"] == "2C/12G"

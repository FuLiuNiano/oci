"""SQLite 存储：多云账号、SSH 会话、开机任务、域名监控、设置。数据全部本地。"""
import json
import os
import sqlite3
import threading

DATA_DIR = os.environ.get("PANEL_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "panel.db")

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'oci',
    name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    auto_restart INTEGER NOT NULL DEFAULT 0,
    traffic_limit_gb REAL NOT NULL DEFAULT 0,
    traffic_action TEXT NOT NULL DEFAULT 'notify',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS launch_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    shape TEXT NOT NULL,
    ocpus REAL NOT NULL DEFAULT 0,
    memory_gbs REAL NOT NULL DEFAULT 0,
    os_name TEXT NOT NULL DEFAULT 'Canonical Ubuntu',
    os_version TEXT NOT NULL DEFAULT '24.04',
    boot_gb INTEGER NOT NULL DEFAULT 50,
    ssh_key TEXT NOT NULL DEFAULT '',
    subnet_id TEXT NOT NULL DEFAULT '',
    boot_volume_id TEXT NOT NULL DEFAULT '',
    ad_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    instance_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS ssh_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL DEFAULT 'root',
    auth_type TEXT NOT NULL DEFAULT 'password',
    secret TEXT NOT NULL DEFAULT '',
    proxy_command TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    monitor_cpu INTEGER NOT NULL DEFAULT 0,
    monitor_mem INTEGER NOT NULL DEFAULT 0,
    monitor_disk INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    registrar TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _get_conn():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db():
    with _lock:
        conn = _get_conn()
        conn.executescript(SCHEMA)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(launch_tasks)")}
        if "retry_token" not in columns:
            conn.execute("ALTER TABLE launch_tasks ADD COLUMN retry_token TEXT NOT NULL DEFAULT ''")
        if os.name != "nt":
            os.chmod(DATA_DIR, 0o700)
            os.chmod(DB_PATH, 0o600)
        conn.commit()


def query(sql, params=()):
    with _lock:
        cur = _get_conn().execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            if "platform" in row and "params" in row:
                row["params"] = account_params(row)
        return rows


def execute(sql, params=()):
    with _lock:
        cur = _get_conn().execute(sql, params)
        _get_conn().commit()
        return cur.lastrowid


def get_setting(key, default=None):
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_json_setting(key, default=None):
    raw = get_setting(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def set_json_setting(key, value):
    set_setting(key, json.dumps(value, ensure_ascii=False))


# ---------- 账号参数存取 ----------

def account_params(acct):
    if isinstance(acct.get("params"), dict):
        return dict(acct["params"])
    try:
        value = json.loads(acct.get("params") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def account_public(acct):
    """脱敏后的账号信息（不回传任何密钥）。"""
    p = account_params(acct)
    safe_keys = ("user_ocid", "tenancy_ocid", "fingerprint", "compartment_id", "proxy_url")
    return {
        "id": acct["id"],
        "platform": acct["platform"],
        "name": acct["name"],
        "region": acct["region"],
        "auto_restart": acct["auto_restart"],
        "traffic_limit_gb": acct["traffic_limit_gb"],
        "traffic_action": acct["traffic_action"],
        "created_at": acct["created_at"],
        "params": {k: v for k, v in p.items() if k in safe_keys},
    }

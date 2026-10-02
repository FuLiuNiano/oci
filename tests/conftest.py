import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store


@pytest.fixture
def database(tmp_path, monkeypatch):
    if store._conn:
        store._conn.close()
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "panel.db"))
    store.init_db()
    yield tmp_path
    if store._conn:
        store._conn.close()
        store._conn = None


@pytest.fixture(scope="session")
def credentials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"user_ocid": "ocid1.user.oc1.." + "a" * 60,
            "tenancy_ocid": "ocid1.tenancy.oc1.." + "b" * 60,
            "fingerprint": ":".join(["00"] * 16), "private_key": pem}


@pytest.fixture
def account(database, credentials):
    aid = store.execute("INSERT INTO accounts(name,region,params) VALUES(?,?,?)",
                        ("test", "ap-singapore-1", json.dumps(credentials)))
    return store.query("SELECT * FROM accounts WHERE id=?", (aid,))[0]


@pytest.fixture
def client(database, monkeypatch):
    import main
    import tasks
    from fastapi.testclient import TestClient
    monkeypatch.setattr(tasks, "start", lambda: None)
    main._login_attempts.clear()
    with TestClient(main.app) as c:
        pw = (database / "initial_admin_password.txt").read_text().strip()
        assert c.post("/api/login", json={"password": pw}).status_code == 200
        c.initial_password = pw
        yield c


@pytest.fixture
def sdk(monkeypatch, account):
    """Keep the real SDK models, argument validation and pagination; stub only transport."""
    import oci
    import oci_service
    from unittest.mock import Mock

    original = oci_service._client
    data, calls, instances = {}, [], {}

    def client(cls, acct):
        if cls not in instances:
            obj = original(cls, acct)
            def call_api(*args, **kwargs):
                operation = kwargs["operation_name"]
                calls.append((operation, args, kwargs))
                result = data[operation]
                if isinstance(result, Exception):
                    raise result
                if callable(result):
                    return result(*args, **kwargs)
                from types import SimpleNamespace
                request = SimpleNamespace(method=args[1] if len(args) > 1 else "GET")
                return oci.response.Response(200, {}, result, request)
            obj.base_client.call_api = call_api
            instances[cls] = obj
        return instances[cls]
    monkeypatch.setattr(oci_service, "_client", client)
    return data, calls

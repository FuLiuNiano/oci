"""Real temporary Git repositories/files; systemd, pip and health are simulated."""
import json
import subprocess
from pathlib import Path

import pytest

from update_panel import Updater


@pytest.fixture
def updater(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Updater Test")
    (root / ".gitignore").write_text("data/\n.venv/\n.panel-backups/\n*.before-rollback-*/\n.env\n")
    (root / "main.py").write_text("old")
    (root / "requirements.txt").write_text("old-deps")
    git("add", ".")
    git("commit", "-m", "old")
    old = git("rev-parse", "HEAD")
    (root / "main.py").write_text("new")
    git("commit", "-am", "new")
    new = git("rev-parse", "HEAD")
    git("reset", "--hard", old)
    (root / "data").mkdir()
    (root / "data" / "panel.db").write_text("original-data")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("original-runtime")
    (root / ".env").write_text("private-config")
    class LocalUpdater(Updater):
        failure = None
        events = []
        def runtime(self):
            return {"data": str(root / "data"), "health":"http://127.0.0.1:19528/healthz"}
        def service(self, action):
            self.events.append(action)
            if action == "start" and (root / "main.py").read_text() == "new":
                (root / "data" / "panel.db").write_text("new-schema")
        def healthy(self, url, attempts=30):
            return self.failure != "health" or (root / "main.py").read_text() == "old"
        def run(self, *args):
            if args == ("git", "fetch", "origin", "main"): return ""
            if args == ("git", "rev-parse", "FETCH_HEAD"): return new
            if args == ("systemctl", "cat", "oci-panel"): return "test unit"
            if args[0] == str(self.venv / "bin" / "python"):
                (self.venv / "bin" / "python").write_text("new-runtime")
                if self.failure == "pip": raise RuntimeError("dependency install failed")
                return ""
            return super().run(*args)
    return LocalUpdater(root)


def test_update_success_and_explicit_rollback(updater):
    updater.update()
    backup = next(updater.backups.iterdir())
    assert (updater.root / "main.py").read_text() == "new"
    assert (backup / "data" / "panel.db").read_text() == "original-data"
    assert (backup / "env").read_text() == "private-config"
    assert (backup / "code.tar").is_file()
    updater.clean()
    updater.restore(backup)
    assert (updater.root / "main.py").read_text() == "old"
    assert (updater.root / "data" / "panel.db").read_text() == "original-data"
    assert (updater.venv / "bin" / "python").read_text() == "original-runtime"
    preserved = list(updater.root.glob("data.before-rollback-*"))
    assert (preserved[0] / "panel.db").read_text() == "new-schema"


@pytest.mark.parametrize("failure", ["pip", "health"])
def test_failed_update_restores_data_code_and_dependencies(updater, failure):
    updater.failure = failure
    with pytest.raises(RuntimeError, match="已回退"):
        updater.update()
    assert (updater.root / "main.py").read_text() == "old"
    assert (updater.root / "data" / "panel.db").read_text() == "original-data"
    assert (updater.venv / "bin" / "python").read_text() == "original-runtime"
    assert updater.events[-1] == "start"


def test_dirty_code_rejected_before_stop(updater):
    (updater.root / "main.py").write_text("local edits")
    with pytest.raises(RuntimeError, match="未提交"):
        updater.update()
    assert not updater.events


def test_first_upgrade_lock_and_backups_do_not_require_new_gitignore(updater):
    (updater.root / '.gitignore').write_text('data/\n.venv/\n.env\n')
    updater.run('git', 'add', '.gitignore')
    updater.run('git', 'commit', '-m', 'simulate old ignore rules')
    (updater.root / '.panel-update.lock').touch()
    updater.backups.mkdir()
    (updater.backups / 'private-backup').write_text('secret')
    (updater.root / 'data.before-rollback-test').mkdir()
    (updater.root / 'data.before-rollback-test' / 'panel.db').write_text('private')
    updater.clean()
    (updater.root / 'unrelated-file').touch()
    with pytest.raises(RuntimeError, match='未提交'):
        updater.clean()


def test_incomplete_backup_restarts_original_service(updater, monkeypatch):
    def fail(*args): raise OSError("disk full")
    monkeypatch.setattr(updater, "snapshot", fail)
    with pytest.raises(OSError): updater.update()
    assert updater.events == ["stop", "start"]
    assert (updater.root / "main.py").read_text() == "old"


def test_invalid_restore_and_overlapping_paths_rejected(updater, tmp_path):
    with pytest.raises(RuntimeError): updater.restore(tmp_path)
    with pytest.raises(RuntimeError): updater.validate_paths(updater.root)
    with pytest.raises(RuntimeError): updater.validate_paths(updater.backups / "data")


def test_runtime_uses_running_service_data_and_custom_port(updater, monkeypatch):
    import os
    monkeypatch.setattr(updater, 'run', lambda *args: '123')
    original = Path.read_bytes
    def environment(path):
        if str(path).replace('\\', '/') == '/proc/123/environ':
            return b'HOST=127.0.0.1\0PORT=12345\0PANEL_DATA_DIR=custom-data\0'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', environment)
    monkeypatch.setattr(os, 'readlink', lambda path: str(updater.root))
    actual = Updater.runtime(updater)
    assert Path(actual['data']) == updater.root / 'custom-data'
    assert actual['health'] == 'http://127.0.0.1:12345/healthz'

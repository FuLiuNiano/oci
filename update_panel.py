"""Transactional updater for an existing Linux systemd installation.

Uses only Python's standard library. Backups contain credentials: keep them private.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid


class Updater:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.venv = self.root / ".venv"
        self.backups = self.root / ".panel-backups"

    def run(self, *args):
        return subprocess.check_output(args, cwd=self.root, text=True, stderr=subprocess.STDOUT).strip()

    def service(self, action):
        self.run("systemctl", action, "oci-panel")

    def clean(self):
        # Also works when bootstrapped from stdin before the old .gitignore knows these paths.
        if self.run("git", "status", "--porcelain", "--untracked-files=normal", "--", ".",
                    ":(exclude).panel-update.lock", ":(exclude).panel-backups",
                    ":(exclude)*.before-rollback-*"):
            raise RuntimeError("代码目录存在未提交修改或未跟踪文件，请先处理后再更新/回退")

    def runtime(self):
        pid = int(self.run("systemctl", "show", "oci-panel", "-p", "MainPID", "--value"))
        if pid <= 0:
            raise RuntimeError("更新前服务必须正在运行，以确认实际数据目录和监听端口")
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        env = dict(item.decode().split("=", 1) for item in raw.split(b"\0") if b"=" in item)
        working = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        if working != self.root:
            raise RuntimeError("当前目录不是 oci-panel 服务实际运行目录")
        data = Path(env.get("PANEL_DATA_DIR") or self.root / "data")
        if not data.is_absolute():
            data = working / data
        host = env.get("HOST", "0.0.0.0")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if ":" in host:
            host = f"[{host}]"
        return {"data": str(data.absolute()), "health": f"http://{host}:{int(env.get('PORT', '9528'))}/healthz"}

    def validate_paths(self, data):
        data = Path(data)
        if data.is_symlink() or self.venv.is_symlink() or self.backups.is_symlink():
            raise RuntimeError("数据、虚拟环境和备份根目录不能是符号链接")
        data = data.resolve()
        if str(data) in {"/etc", "/var", "/usr", "/home", "/root", "/opt", "/srv", "/tmp", "/run", "/dev", "/proc", "/sys", "/bin", "/sbin", "/lib", "/lib64"}:
            raise RuntimeError("数据目录不能使用系统顶层目录")
        if data == self.root or data in self.root.parents or data == self.venv or self.venv in data.parents:
            raise RuntimeError("数据目录必须与程序和虚拟环境分开")
        if data == self.backups or data in self.backups.parents or self.backups in data.parents:
            raise RuntimeError("数据和备份目录不能互相包含")
        return data

    def healthy(self, url, attempts=30):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(attempts):
            try:
                self.run("systemctl", "is-active", "--quiet", "oci-panel")
                with opener.open(url, timeout=2) as response:
                    if response.status == 200 and json.load(response).get("ok") is True:
                        return True
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(1)
        return False

    def snapshot(self, runtime, commit):
        self.backups.mkdir(mode=0o700, exist_ok=True)
        backup = self.backups / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(mode=0o700)
        data = self.validate_paths(runtime["data"])
        if not data.is_dir() or not self.venv.is_dir():
            raise RuntimeError("数据目录或虚拟环境缺失，未执行更新")
        if any(path.is_symlink() for path in data.rglob("*")):
            raise RuntimeError("数据目录包含外部链接，请改用独立实际数据目录后再备份更新")
        shutil.copytree(data, backup / "data", symlinks=True)
        shutil.copytree(self.venv, backup / "venv", symlinks=True)
        if (self.root / ".env").is_file():
            shutil.copy2(self.root / ".env", backup / "env")
        self.run("git", "archive", "--format=tar", "--output=" + str(backup / "code.tar"), commit)
        self.run("git", "bundle", "create", str(backup / "code.bundle"), "HEAD")
        unit = self.run("systemctl", "cat", "oci-panel")
        (backup / "service-unit.txt").write_text(unit, encoding="utf-8")
        manifest = {**runtime, "root": str(self.root), "commit": commit, "complete": True}
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return backup

    def restore(self, backup):
        backup = Path(backup).resolve()
        if backup.parent != self.backups.resolve():
            raise RuntimeError("仅允许恢复当前项目 .panel-backups 中的备份")
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("root") != str(self.root) or not manifest.get("complete"):
            raise RuntimeError("备份不完整或属于其他安装目录")
        data = self.validate_paths(manifest["data"])
        if not (backup / "data").is_dir() or not (backup / "venv").is_dir():
            raise RuntimeError("备份数据或虚拟环境缺失")
        try:
            self.run("git", "cat-file", "-e", manifest["commit"] + "^{commit}")
        except subprocess.CalledProcessError:
            self.run("git", "fetch", str(backup / "code.bundle"), "HEAD")
            self.run("git", "cat-file", "-e", manifest["commit"] + "^{commit}")
        self.service("stop")
        # Preserve failed/new state next to each original directory, never delete it.
        suffix = ".before-rollback-" + uuid.uuid4().hex[:8]
        preserved = []
        for destination, source in ((data, backup / "data"), (self.venv, backup / "venv")):
            if destination.exists():
                saved = destination.with_name(destination.name + suffix)
                destination.rename(saved)
                preserved.append(str(saved))
            shutil.copytree(source, destination, symlinks=True)
        if (backup / "env").exists():
            if (self.root / ".env").exists():
                shutil.copy2(self.root / ".env", backup / ("env" + suffix))
            shutil.copy2(backup / "env", self.root / ".env")
        self.run("git", "reset", "--hard", manifest["commit"])
        self.service("start")
        if not self.healthy(manifest["health"]):
            raise RuntimeError(f"旧版本恢复后健康检查仍失败，请检查服务日志；备份保留在 {backup}")
        print("已恢复旧代码、依赖和数据；回退前数据保留在：" + ", ".join(preserved))

    def update(self):
        self.clean()
        runtime = self.runtime()
        self.validate_paths(runtime["data"])
        old = self.run("git", "rev-parse", "HEAD")
        self.run("git", "fetch", "origin", "main")
        target = self.run("git", "rev-parse", "FETCH_HEAD")
        self.run("git", "merge-base", "--is-ancestor", old, target)
        if old == target:
            print("已经是最新版本，无需更新")
            return
        self.service("stop")
        try:
            backup = self.snapshot(runtime, old)
        except BaseException:
            self.service("start")
            raise
        print(f"更新前备份：{backup}", flush=True)
        try:
            self.run("git", "merge", "--ff-only", target)
            self.run(str(self.venv / "bin" / "python"), "-m", "pip", "install", "-r", str(self.root / "requirements.txt"))
            self.service("start")
            if not self.healthy(runtime["health"]):
                raise RuntimeError("新版本健康检查失败")
        except BaseException as error:
            print("更新失败，正在恢复更新前的代码、依赖与数据…", flush=True)
            self.restore(backup)
            raise RuntimeError("更新未完成，已回退到原版本") from error
        print(f"更新成功，健康检查通过。手动回退：python3 update_panel.py rollback '{backup}'")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("update", "rollback"))
    parser.add_argument("backup", nargs="?")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("此工具仅支持已安装 systemd 服务的 Linux 服务器，请使用 root 执行")
    import fcntl
    os.umask(0o077)
    updater = Updater(Path(__file__).parent)
    with (updater.root / ".panel-update.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "update":
            updater.update()
        else:
            if not args.backup:
                parser.error("rollback 必须指定更新时输出的备份目录")
            updater.clean()
            updater.restore(args.backup)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"操作失败：{error}", file=sys.stderr)
        sys.exit(1)

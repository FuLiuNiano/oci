"""Linux host metrics, sampled on demand without commands or external services."""
import os
import shutil
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_previous = None
_cached = None


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def snapshot():
    global _previous, _cached
    with _lock:
        now = time.monotonic()
        if _cached and _previous and now - _previous["time"] < 1:
            return dict(_cached)
        if not Path("/proc/stat").exists():
            return {"available": False, "message": "部署服务器监控需要 Linux /proc", "cpu": None}
        try:
            ticks = [int(n) for n in _read("/proc/stat").splitlines()[0].split()[1:9]]
            total, idle = sum(ticks), ticks[3] + ticks[4]
            memory = {line.split(":")[0]: int(line.split()[1]) * 1024
                      for line in _read("/proc/meminfo").splitlines() if ":" in line}
            mem_total = memory["MemTotal"]
            mem_used = mem_total - memory.get("MemAvailable", memory.get("MemFree", 0))
            scope = "host"
            cg_cpu = None
            cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
            cg = Path("/sys/fs/cgroup")
            if (cg / "memory.max").exists():
                maximum = _read(cg / "memory.max").strip()
                if maximum != "max" and int(maximum) < mem_total:
                    mem_total = int(maximum)
                    mem_used = int(_read(cg / "memory.current"))
                    scope = "container"
            if (cg / "cpu.max").exists():
                quota, period = _read(cg / "cpu.max").split()
                if quota != "max":
                    cores = min(cores, int(quota) / int(period))
                    stats = dict(line.split() for line in _read(cg / "cpu.stat").splitlines())
                    cg_cpu = int(stats["usage_usec"])
                    scope = "container"
            rx = tx = 0
            for line in _read("/proc/net/dev").splitlines()[2:]:
                interface, values = line.split(":", 1)
                if interface.strip().startswith(("lo", "veth", "docker", "br-")):
                    continue
                values = values.split()
                rx += int(values[0])
                tx += int(values[8])
            cpu = rx_rate = tx_rate = None
            if _previous:
                elapsed = now - _previous["time"]
                delta = total - _previous["total"]
                if cg_cpu is not None and _previous["cg_cpu"] is not None:
                    cpu = 100 * (cg_cpu - _previous["cg_cpu"]) / (elapsed * 1e6 * cores)
                elif delta > 0:
                    cpu = 100 * (1 - (idle - _previous["idle"]) / delta)
                rx_rate = max(0, rx - _previous["rx"]) / elapsed
                tx_rate = max(0, tx - _previous["tx"]) / elapsed
            rss = next(int(line.split()[1]) * 1024 for line in _read("/proc/self/status").splitlines()
                       if line.startswith("VmRSS:"))
            disk = shutil.disk_usage(os.path.dirname(__file__))
            _previous = dict(time=now, total=total, idle=idle, rx=rx, tx=tx, cg_cpu=cg_cpu)
            _cached = {"available": True, "scope": scope, "cpu": round(max(0, min(100, cpu)), 1) if cpu is not None else None,
                       "cores": cores, "memory_used": mem_used, "memory_total": mem_total,
                       "memory_percent": round(100 * mem_used / mem_total, 1),
                       "network_rx": rx_rate, "network_tx": tx_rate, "app_memory": rss,
                       "disk_used": disk.used, "disk_total": disk.total, "sampled_at": time.time()}
            return dict(_cached)
        except (OSError, ValueError, KeyError, StopIteration) as e:
            return {"available": False, "cpu": None, "message": f"监控读取失败：{type(e).__name__}"}

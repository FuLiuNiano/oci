"""WebSSH 后端：asyncssh 连接、批量命令、SFTP、端口转发、资源指标。密钥/密码只存本地。"""
import asyncio
import threading
import time
import stat

import asyncssh

import store


class SshError(Exception):
    pass


_runtime_lock = threading.Lock()
_runtime_loop = None
_runtime_thread = None


def _run(coro):
    global _runtime_loop, _runtime_thread
    with _runtime_lock:
        if _runtime_loop is None or _runtime_loop.is_closed():
            _runtime_loop = asyncio.new_event_loop()
            _runtime_thread = threading.Thread(target=_runtime_loop.run_forever,
                                               daemon=True, name="ssh-runtime")
            _runtime_thread.start()
        loop = _runtime_loop
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def shutdown():
    global _runtime_loop, _runtime_thread
    with _runtime_lock:
        loop, thread = _runtime_loop, _runtime_thread
        if loop is None:
            return
        async def close():
            for item in list(_forwards.values()):
                item["listener"].close()
                item["conn"].close()
                await item["conn"].wait_closed()
            _forwards.clear()
        asyncio.run_coroutine_threadsafe(close(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        _runtime_loop = _runtime_thread = None


def _connect_args(sess):
    args = dict(
        host=sess["host"], port=int(sess.get("port") or 22),
        username=sess.get("username") or "root",
        connect_timeout=15,
    )
    if sess.get("proxy_command"):
        args["proxy_command"] = sess["proxy_command"]
    if sess.get("auth_type") == "key":
        args["client_keys"] = [asyncssh.import_private_key(sess["secret"])]
    else:
        args["password"] = sess.get("secret", "")
    return args


async def _connect(sess):
    try:
        key_id = f"ssh_hostkey:{sess['host']}:{int(sess.get('port') or 22)}"
        saved = store.get_setting(key_id)
        if saved:
            key = asyncssh.import_public_key(saved)
        else:
            options = {}
            if sess.get("proxy_command"):
                options["proxy_command"] = sess["proxy_command"]
            key = await asyncio.wait_for(asyncssh.get_server_host_key(
                sess["host"], int(sess.get("port") or 22), **options), timeout=15)
            if key is None:
                raise SshError("无法读取 SSH 主机公钥")
        conn = await asyncssh.connect(**_connect_args(sess), known_hosts=([key], [], []))
        if not saved:
            store.set_setting(key_id, key.export_public_key().decode())
        return conn
    except asyncssh.Error as e:
        raise SshError(f"SSH 连接失败: {e}")
    except OSError as e:
        raise SshError(f"连接失败: {e}")


async def _run_one(sess, cmd, timeout=30):
    conn = await _connect(sess)
    try:
        result = await asyncio.wait_for(conn.run(cmd, timeout=timeout), timeout=timeout + 5)
        out = result.stdout or ""
        err = result.stderr or ""
        return out + (f"\n[stderr]\n{err}" if err.strip() else "")
    finally:
        conn.close()


def exec_batch(session_ids, cmd, timeout=30):
    """批量在多个会话上执行命令，返回 [{id, name, output, error}]。"""
    async def _all():
        async def one(sid):
            rows = store.query("SELECT * FROM ssh_sessions WHERE id=?", (sid,))
            if not rows:
                return {"id": sid, "name": str(sid), "error": "会话不存在"}
            try:
                out = await _run_one(rows[0], cmd, timeout)
                return {"id": sid, "name": rows[0]["name"], "output": out}
            except Exception as e:
                return {"id": sid, "name": rows[0]["name"], "error": str(e)}
        return await asyncio.gather(*[one(s) for s in session_ids])
    return _run(_all())


def test_session(sess):
    out = _run(_run_one(sess, "echo ok && uname -a", timeout=15))
    return out.strip()


# ---------- SFTP ----------

def _sftp_call(sess, fn):
    async def _inner():
        conn = await _connect(sess)
        try:
            async with conn.start_sftp_client() as sftp:
                return await fn(sftp)
        finally:
            conn.close()
    return _run(_inner())


def sftp_list(sess, path="/"):
    async def op(sftp):
        entries = []
        for e in await sftp.readdir(path):
            name = e.filename
            attrs = e.attrs
            if name in (".", ".."):
                continue
            entries.append({
                "name": name,
                "dir": attrs.type == 2 or stat.S_ISDIR(attrs.permissions or 0),
                "size": attrs.size or 0,
                "mtime": int(attrs.mtime or 0),
            })
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return entries
    return _sftp_call(sess, op)


def sftp_read(sess, path, max_bytes=2_000_000):
    async def op(sftp):
        async with sftp.open(path, "rb") as f:
            data = await f.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise SshError(f"文件超过读取限制 {max_bytes} 字节，请使用 SFTP 客户端下载")
        return data
    return _sftp_call(sess, op)


def sftp_write(sess, path, content: bytes):
    async def op(sftp):
        async with sftp.open(path, "wb") as f:
            await f.write(content)
        return {"ok": True}
    return _sftp_call(sess, op)


def sftp_mkdir(sess, path):
    async def op(sftp):
        await sftp.mkdir(path)
        return {"ok": True}
    return _sftp_call(sess, op)


def sftp_delete(sess, path, is_dir=False):
    async def op(sftp):
        if is_dir:
            await sftp.rmdir(path)
        else:
            await sftp.remove(path)
        return {"ok": True}
    return _sftp_call(sess, op)


def sftp_rename(sess, old, new):
    async def op(sftp):
        await sftp.rename(old, new)
        return {"ok": True}
    return _sftp_call(sess, op)


# ---------- 端口转发 ----------

_forwards = {}          # fwd_id -> {"conn":, "listener":, "info":{}}
_fwd_lock = threading.Lock()
_fwd_seq = [0]


def start_forward(sess, fwd_type, local_port, remote_host, remote_port):
    """local: 本机local_port -> 远端remote_host:remote_port; remote: 远端local_port -> 本机侧 remote_host:remote_port"""
    async def _inner():
        conn = await _connect(sess)
        try:
            if fwd_type == "local":
                listener = await conn.forward_local_port("127.0.0.1", int(local_port), remote_host, int(remote_port))
            else:
                listener = await conn.forward_remote_port("127.0.0.1", int(local_port), remote_host, int(remote_port))
        except Exception:
            conn.close()
            raise
        return conn, listener
    try:
        conn, listener = _run(_inner())
    except (asyncssh.Error, OSError) as e:
        raise SshError(f"转发失败: {e}")
    with _fwd_lock:
        _fwd_seq[0] += 1
        fid = _fwd_seq[0]
        _forwards[fid] = {
            "conn": conn, "listener": listener,
            "info": {"id": fid, "session": sess["name"], "type": fwd_type,
                     "local_port": listener.get_port(), "remote_host": remote_host,
                     "remote_port": int(remote_port)},
        }
    return {"id": fid}


def list_forwards():
    with _fwd_lock:
        return [dict(v["info"]) for v in _forwards.values()]


def stop_forward(fid):
    with _fwd_lock:
        item = _forwards.pop(fid, None)
    if not item:
        raise SshError("转发不存在")
    async def close():
        item["listener"].close()
        item["conn"].close()
        await item["conn"].wait_closed()
    _run(close())
    return {"ok": True}


# ---------- 资源指标 ----------

def _parse_mem(lines):
    # /proc/meminfo
    vals = {}
    for line in lines:
        if ":" in line:
            k, _, v = line.partition(":")
            vals[k.strip()] = int(v.strip().split()[0]) if v.strip().split() else 0
    total = vals.get("MemTotal", 0)
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    if not total:
        return 0
    return round(100 * (total - avail) / total, 1)


def _parse_cpu(out):
    # 两次 /proc/stat 采样
    lines = [l for l in out.strip().splitlines() if l.startswith("cpu ")]
    if len(lines) < 2:
        return None
    def load(line):
        p = [int(x) for x in line.split()[1:]]
        idle = p[3] + (p[4] if len(p) > 4 else 0)
        return idle, sum(p)
    i0, t0 = load(lines[0])
    i1, t1 = load(lines[1])
    dt = t1 - t0
    if dt <= 0:
        return None
    return round(100 * (1 - (i1 - i0) / dt), 1)


MONITOR_CMD = (
    "cat /proc/stat; echo ---MEM---; cat /proc/meminfo | head -6; "
    "echo ---DISK---; df -P / | tail -1; echo ---NET---; cat /proc/net/dev"
)


def collect_metrics(sess):
    """返回 {cpu, mem, disk, net_rx_mb, net_tx_mb}；远程服务器需为 Linux。"""
    async def _sample():
        conn = await _connect(sess)
        try:
            r1 = await conn.run(MONITOR_CMD, timeout=20)
            out1 = r1.stdout or ""
            await asyncio.sleep(0.6)
            r2 = await conn.run("cat /proc/stat", timeout=10)
            # 拼接两次 CPU 采样
            cpu_lines = [l for l in out1.splitlines() if l.startswith("cpu ")]
            extra = [l for l in (r2.stdout or "").splitlines() if l.startswith("cpu ")]
            cpu_out = "\n".join(cpu_lines + extra)

            mem, disk, net = "", "", ""
            try:
                mem = out1.split("---MEM---")[1].split("---DISK---")[0]
            except IndexError:
                pass
            try:
                disk = out1.split("---DISK---")[1].split("---NET---")[0].strip()
            except IndexError:
                pass
            try:
                net = out1.split("---NET---")[1]
            except IndexError:
                pass
            return cpu_out, mem, disk, net
        finally:
            conn.close()
    cpu_out, mem, disk, net = _run(_sample())
    result = {"cpu": _parse_cpu(cpu_out), "mem": _parse_mem(mem.splitlines()), "disk": None,
              "net_rx_mb": 0, "net_tx_mb": 0}
    try:
        result["disk"] = float(disk.split()[4].rstrip("%"))
    except Exception:
        pass
    rx = tx = 0
    for line in net.splitlines():
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        if iface.strip() in ("lo",):
            continue
        parts = data.split()
        if len(parts) >= 9:
            rx += int(parts[0])
            tx += int(parts[8])
    result["net_rx_mb"] = round(rx / 1e6, 1)
    result["net_tx_mb"] = round(tx / 1e6, 1)
    return result

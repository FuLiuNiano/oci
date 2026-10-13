"""Fixed, opt-in Linux maintenance commands using the saved SSH route."""
import shlex

# A bounded, temporary workload, not an unbounded loop or a system-wide allocator.
HOLD = """import os,time,pathlib
p=pathlib.Path.home()/'.oci-panel-tools'/'memory.pid'
p.write_text(str(os.getpid()))
try:
    m={x.split(':')[0]:int(x.split()[1])*1024 for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith(('MemTotal:','MemAvailable:'))}
    n=min(m['MemTotal']//4,m['MemAvailable']//4,256*1024*1024)
    for f,current in (('/sys/fs/cgroup/memory.max','/sys/fs/cgroup/memory.current'),('/sys/fs/cgroup/memory/memory.limit_in_bytes','/sys/fs/cgroup/memory/memory.usage_in_bytes')):
        try:
            limit=pathlib.Path(f).read_text().strip()
            if limit.isdigit():
                n=min(n,int(limit)//4)
                try: n=min(n,max(0,int(limit)-int(pathlib.Path(current).read_text()))//4)
                except OSError: pass
        except OSError: pass
    data=bytearray(n)
    time.sleep(900)
finally:
    if p.exists() and p.read_text().strip()==str(os.getpid()): p.unlink()
"""


def memory_script(start):
    return """import os,pathlib,signal,subprocess,sys,fcntl
directory=pathlib.Path.home()/'.oci-panel-tools'
directory.mkdir(mode=0o700,exist_ok=True)
if directory.is_symlink() or directory.stat().st_uid!=os.getuid(): raise RuntimeError('维护目录身份异常')
lock=open(directory/'memory.lock','a')
fcntl.flock(lock,fcntl.LOCK_EX)
p=directory/'memory.pid'
if p.is_symlink(): raise RuntimeError('任务文件身份异常')
running=None
if p.exists():
    try:
        pid=int(p.read_text())
        cmd=pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes()
        if b'oci-panel-memory-hold' in cmd and pathlib.Path('/proc/'+str(pid)).stat().st_uid==os.getuid(): running=pid
    except (OSError,ValueError): pass
""" + ("""if running: print('已有本工具内存任务，不重复启动')
else:
    child=subprocess.Popen([sys.executable,'-c',""" + repr(HOLD) + """,'oci-panel-memory-hold'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    p.write_text(str(child.pid))
    print('已启动临时内存任务，最多 256MB、最多 15 分钟；请在监控中核对')
""" if start else """if running:
    os.kill(running,signal.SIGTERM)
    p.unlink(missing_ok=True)
    print('已停止本工具的内存任务')
else: print('未找到属于本工具的运行任务，未终止其他进程')
""")


def command(operation):
    if operation=='load': return 'uptime; free -m; df -h /; ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 12'
    if operation=='clear-cache': return "sync && test $(id -u) -eq 0 && echo 3 > /proc/sys/vm/drop_caches && echo '已释放系统缓存，不删除文件'"
    if operation in ('memory-start','memory-stop'):
        return 'python3 -c '+shlex.quote(memory_script(operation=='memory-start'))
    raise ValueError('不支持的维护操作')

"""查看 worker 进程：重启恢复时核对 pid、收割后清理残留进程。

调度器只调这里的函数，不直接跑 ps / lsof。
- macOS：ps 和 lsof。
- Linux（含 WSL2）：直接读 /proc，不依赖 ps / lsof（精简系统上常常没装 lsof）。
- Windows：ctypes 调系统接口（winapi.py）。worker、沙箱外执行各自在一个命名作业（Job Object）里，
  停止和清理残留都按作业结束整棵进程树（spawn / signal_group / kill_job），不用 scan / cwd_within。
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import host

PROC = "/proc"  # 测试里换成假的 /proc 目录
SIGKILL = getattr(signal, "SIGKILL", 9)  # Windows 的 signal 模块没有 SIGKILL


# ---------- 启动和结束进程组（POSIX：会话 / 进程组；Windows：命名作业） ----------
_JOBS: Dict[int, Tuple[str, object]] = {}  # Windows：pid → (作业名, 作业句柄)，守护进程活着期间持有句柄


def job_name(key: str) -> str:
    """一个 worker（按汇报目录）/ 一次沙箱外执行（按脚本路径）对应的作业名。带上路径的哈希：
    两份安装（不同 TICKETD_HOME / 工单根目录）互不干扰；守护进程重启后按同一个 key 还能找回来。"""
    return "Local\\ticketd-" + hashlib.sha256(os.path.normcase(key).encode("utf-8")).hexdigest()[:24]


def spawn(args, job_key: str, **kw) -> subprocess.Popen:
    """启动一个自成一组的进程：POSIX 上 start_new_session（组号即 pid）；Windows 上放进 job_name(job_key) 作业，
    它以后起的子孙进程（含脱离父进程的后台进程）都在里面。Windows 上 args[0] 按 env 里的 PATH 解析成完整路径
    （CreateProcess 不认 PATH 上的 .cmd，也不看传给子进程的 PATH）。"""
    if not host.is_windows():
        return subprocess.Popen(args, start_new_session=True, **kw)
    from . import winapi
    args = host.argv(args, (kw.get("env") or os.environ).get("PATH"))
    name = job_name(job_key)
    proc, job = winapi.popen_in_job(args, name, **kw)
    _JOBS[proc.pid] = (name, job)
    return proc


def adopt(pid: int, job_key: str) -> None:
    """守护进程重启后重新认领的进程（沙箱外执行）：记下它的作业名，之后 signal_group 能按作业结束它。"""
    if host.is_windows() and pid:
        _JOBS[pid] = (job_name(job_key), None)


def signal_group(pid: int, sig: int, fallback: bool = True) -> None:
    """给 pid 所在的整组进程发信号。POSIX：killpg，失败且 fallback 时只发给 pid。
    Windows：没有可投递的信号，任何信号都是结束：作业已知就结束整个作业（含后台子孙进程），否则 fallback 时只结束 pid。
    组长已退出时 fallback 必须为 False：pid 可能已被复用。"""
    if host.is_windows():
        known = _JOBS.get(pid)
        if known is not None:
            from . import winapi
            if winapi.terminate_job(known[0]):
                return
            # 作业按名字找不到了（托管进程不在、句柄已关）：至少结束它本身
        if fallback:
            try:
                os.kill(pid, sig)  # Windows 上是 TerminateProcess
            except OSError:
                pass
        return
    try:
        os.killpg(pid, sig)
        return
    except (ProcessLookupError, PermissionError):
        pass
    if not fallback:
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_job(job_key: str) -> int:
    """Windows：结束 job_key 对应作业里还在跑的所有进程，返回个数（作业不存在为 0）。其他平台返回 0。"""
    if not host.is_windows():
        return 0
    from . import winapi
    return winapi.terminate_job(job_name(job_key))


def forget(pid: int) -> None:
    """进程已收割：放掉作业句柄（作业里没有进程之后系统随之回收它）。"""
    known = _JOBS.pop(pid, None)
    if known is not None and known[1] is not None:
        from . import winapi
        winapi.close_handle(known[1])


def command_line(pid: int) -> str:
    """pid 的完整命令行；拿不到返回空串。"""
    if host.is_windows():
        from . import winapi
        try:
            return winapi.command_line(pid)
        except OSError:
            return ""
    if host.is_linux():
        try:
            raw = _proc_read(pid, "cmdline")
        except OSError:
            return ""
        return os.fsdecode(raw).replace("\0", " ").strip()
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return ""


def is_gone(pid: int) -> bool:
    """pid 已退出或已是僵尸。"""
    if host.is_windows():
        from . import winapi
        return not winapi.alive(pid)
    if host.is_linux():
        st = _proc_stat(pid)
        return st is None or st[0] in ("Z", "X")
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                          timeout=5).stdout.strip()
    return not stat or stat.startswith("Z")


def scan(spool: str) -> Tuple[Set[int], Set[int]]:
    """扫一遍本机进程（不含自己），返回 (marked, adopted)：
    marked = 环境里有 TICKETD_SPOOL=<spool> 的；adopted = 其余进程里已被收养（macOS：launchd；
    Linux：PID 1 或 ticketd 自己的某个祖先进程，见 _linux_scan）、没有控制终端的。"""
    if host.is_linux():
        return _linux_scan(spool)
    out = subprocess.run(["ps", "-axE", "-o", "pid=,ppid=,tty=,command="], capture_output=True, text=True,
                         errors="replace", timeout=10, env=_utf8_env()).stdout
    needle = "TICKETD_SPOOL=" + spool
    me = os.getpid()
    marked, adopted = set(), set()
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == me:
            continue
        if len(parts) == 4 and _has_marker(parts[3], needle):
            marked.add(pid)
        elif parts[1] == "1" and parts[2] == "??":
            adopted.add(pid)
    return marked, adopted


def cwd_within(pids: Iterable[int], workdir: str) -> Set[int]:
    """pids 里当前目录在 workdir 之内的那些（lsof 和 /proc 给的都是真实路径，所以 workdir 也先取真实路径）。"""
    root = os.path.realpath(workdir)
    if host.is_linux():
        found = set()
        for pid in pids:
            try:
                path = os.readlink("%s/%d/cwd" % (PROC, pid))
            except OSError:
                continue
            if path == root or path.startswith(root + "/"):
                found.add(pid)
        return found
    out = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-u", str(os.getuid()), "-d", "cwd",
                          "-p", ",".join(str(p) for p in sorted(pids)), "-Fpn"],
                         capture_output=True, text=True, errors="replace", timeout=10, env=_utf8_env()).stdout
    found, pid = set(), None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid is not None:
            path = line[1:]
            if path == root or path.startswith(root + "/"):
                found.add(pid)
    return found


# ---------- macOS ----------
def tree(root: int) -> List[Tuple[int, float, float]]:
    """root 的所有后代进程（不含 root）：[(pid, 已运行秒数, 累计 CPU 秒数)]。卡住处置用它找出当前这次工具调用起的
    进程、判断它们还在不在干活。拿不到返回空列表，永不抛异常。"""
    try:
        if host.is_windows():
            from . import winapi
            table = winapi.process_table()
        else:
            table = _linux_table() if host.is_linux() else _darwin_table()
    except Exception:
        return []
    kids: Dict[int, List[int]] = {}
    for pid, (ppid, age, _) in table.items():
        if host.is_windows():
            # Windows 不会把孤儿过继给别人，父进程退出后它的 pid 可能被复用：比父进程「年长」的不是它的子进程
            parent = table.get(ppid)
            if pid == ppid or parent is None or age < 0 or (parent[1] >= 0 and age > parent[1] + 1.0):
                continue
        kids.setdefault(ppid, []).append(pid)
    out, stack, seen = [], [root], {root}
    while stack:
        for pid in kids.get(stack.pop(), []):
            if pid not in seen:
                seen.add(pid)
                stack.append(pid)
                out.append((pid, table[pid][1], table[pid][2]))
    return out


def _clock_seconds(text: str) -> float:
    """ps 的 etime / time 格式：[[dd-]hh:]mm:ss[.cc] → 秒。"""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return days * 86400 + total


def _darwin_table() -> Dict[int, Tuple[int, float, float]]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,time="], capture_output=True, text=True,
                         timeout=10).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), _clock_seconds(parts[2]), _clock_seconds(parts[3]))
        except ValueError:
            continue
    return table


def _linux_table() -> Dict[int, Tuple[int, float, float]]:
    """/proc/<pid>/stat：ppid 是 ')' 之后第 2 个字段，utime / stime / starttime 是第 12、13、20 个（单位是时钟滴答）。"""
    hz = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0
    with open("%s/uptime" % PROC, "rb") as f:
        uptime = float(f.read().split()[0])
    table = {}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        try:
            data = _proc_read(int(name), "stat")
            fields = data[data.rfind(b")") + 1:].split()
            table[int(name)] = (int(fields[1]), max(0.0, uptime - int(fields[19]) / hz),
                                (int(fields[11]) + int(fields[12])) / hz)
        except (OSError, ValueError, IndexError):
            continue
    return table


def _utf8_env() -> dict:
    # 非 UTF-8 locale 下 ps / lsof 会把中文路径转义成 M-eM-7… / \xe5…，导致一个都匹配不上
    return dict(os.environ, LC_ALL="en_US.UTF-8")


def _has_marker(text: str, needle: str) -> bool:
    i = text.find(needle)
    while i >= 0:
        end = i + len(needle)
        if (i == 0 or text[i - 1] == " ") and (end == len(text) or text[end] == " "):
            return True
        i = text.find(needle, i + 1)
    return False


# ---------- Linux ----------
def _proc_read(pid: int, name: str) -> bytes:
    with open("%s/%d/%s" % (PROC, pid, name), "rb") as f:
        return f.read()


def _proc_stat(pid: int) -> Optional[Tuple[str, int, int, str]]:
    """/proc/<pid>/stat 里的 (state, ppid, tty_nr, comm)；进程不存在或读不懂返回 None。
    comm 可能含空格和括号，所以按最后一个 ')' 切。"""
    try:
        data = _proc_read(pid, "stat")
    except OSError:
        return None
    left, right = data.find(b"("), data.rfind(b")")
    fields = data[right + 1:].split()
    if left < 0 or right < left or len(fields) < 5:
        return None
    try:
        return (fields[0].decode("ascii", "replace"), int(fields[1]), int(fields[4]),
                data[left + 1:right].decode("utf-8", "replace"))
    except ValueError:
        return None


def _linux_scan(spool: str) -> Tuple[Set[int], Set[int]]:
    # /proc/<pid>/environ 是进程启动时的完整环境（任何程序都看得到，没有 macOS ps -E 的限制），按整条精确匹配
    needle = b"TICKETD_SPOOL=" + os.fsencode(spool)
    me, uid = os.getpid(), os.getuid()
    stats: Dict[int, Optional[Tuple[str, int, int, str]]] = {}

    def stat_of(pid: int):
        if pid not in stats:
            stats[pid] = _proc_stat(pid)
        return stats[pid]

    # worker 是 ticketd 的子进程，它留下的孤儿只会被收养到 PID 1 或 worker 的某个祖先 subreaper 名下，
    # 也就是 ticketd 的祖先（作为用户服务时是 systemd --user）。按「父进程是谁」判断，不按进程名猜。
    reapers = _ancestors(me, stat_of) | {1}
    marked, adopted = set(), set()
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            if os.stat("%s/%d" % (PROC, pid)).st_uid != uid:
                continue  # 别人的进程：读不到环境，也杀不了
        except OSError:
            continue
        st = stat_of(pid)
        if st is None or st[0] in ("Z", "X"):
            continue
        try:
            env = _proc_read(pid, "environ").split(b"\0")
        except OSError:
            env = []
        if needle in env:
            marked.add(pid)
        elif st[2] == 0 and st[1] in reapers:
            adopted.add(pid)
    return marked, adopted


def _ancestors(pid: int, stat_of) -> Set[int]:
    """pid 的所有祖先（不含它自己）。ppid 为 0 表示到顶（或父进程在本 PID 命名空间之外）。"""
    seen: Set[int] = set()
    st = stat_of(pid)
    while st is not None and st[1] > 0 and st[1] not in seen and len(seen) < 512:
        seen.add(st[1])
        st = stat_of(st[1])
    return seen

"""查看 worker 进程：重启恢复时核对 pid、收割后清理残留进程。

调度器只调这里的四个函数，不直接跑 ps / lsof。macOS 用 ps 和 lsof。
"""
from __future__ import annotations

import os
import subprocess
from typing import Iterable, Set, Tuple


def command_line(pid: int) -> str:
    """pid 的完整命令行；拿不到返回空串。"""
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return ""


def is_gone(pid: int) -> bool:
    """pid 已退出或已是僵尸。"""
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                          timeout=5).stdout.strip()
    return not stat or stat.startswith("Z")


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


def scan(spool: str) -> Tuple[Set[int], Set[int]]:
    """扫一遍本机进程（不含自己），返回 (marked, adopted)：
    marked = 环境里有 TICKETD_SPOOL=<spool> 的；adopted = 其余进程里已被 launchd 收养、没有控制终端的。"""
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
    """pids 里当前目录在 workdir 之内的那些（lsof 给的是真实路径，所以 workdir 也先取真实路径）。"""
    root = os.path.realpath(workdir)
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

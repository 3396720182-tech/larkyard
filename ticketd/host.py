"""运行平台。只支持 macOS 和 Linux（含 WSL2）；平台差异集中在这里、procs.py 和 perms.py。"""
from __future__ import annotations

import sys


def system() -> str:
    """"darwin" 或 "linux"；其他平台原样返回 sys.platform。测试里 mock 这个函数来走另一个平台的分支。"""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def is_linux() -> bool:
    return system() == "linux"

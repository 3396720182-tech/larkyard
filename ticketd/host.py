"""运行平台。只支持 macOS 和 Linux（含 WSL2）；平台差异集中在这里、procs.py 和 perms.py。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def system() -> str:
    """"darwin" 或 "linux"；其他平台原样返回 sys.platform。测试里 mock 这个函数来走另一个平台的分支。"""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def is_linux() -> bool:
    return system() == "linux"


def lark_cli_data_dir() -> str:
    """lark-cli 存加密凭据和主密钥的目录（刷新令牌时要写）。
    macOS：~/Library/Application Support/lark-cli；Linux：$LARKSUITE_CLI_DATA_DIR/lark-cli，没设为 ~/.local/share/lark-cli
    （见 lark-cli 源码 internal/keychain 的 StorageDir；Linux 上不用系统钥匙串，主密钥就是这个目录里的文件）。"""
    if is_linux():
        base = os.environ.get("LARKSUITE_CLI_DATA_DIR", "")
        if not os.path.isabs(base):
            base = str(Path.home() / ".local" / "share")
        return str(Path(base) / "lark-cli")
    return str(Path.home() / "Library" / "Application Support" / "lark-cli")


def shared_tmp() -> str:
    """/tmp 的真实路径：macOS 上 /tmp 是 /private/tmp 的软链，沙箱规则要写真实路径。"""
    return "/tmp" if is_linux() else "/private/tmp"

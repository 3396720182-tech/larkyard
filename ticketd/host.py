"""运行平台：macOS、Linux（含 WSL2）和原生 Windows（spec §3.2）；平台差异集中在这里、procs.py 和 perms.py。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def system() -> str:
    """"darwin"、"linux" 或 "windows"；其他平台原样返回 sys.platform。测试里 mock 这个函数来走另一个平台的分支。"""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return sys.platform


def is_linux() -> bool:
    return system() == "linux"


def is_windows() -> bool:
    return system() == "windows"


def lark_cli_data_dir() -> str:
    """lark-cli 存加密凭据和主密钥的目录（刷新令牌时要写）。
    macOS：~/Library/Application Support/lark-cli；Linux：$LARKSUITE_CLI_DATA_DIR/lark-cli，没设为 ~/.local/share/lark-cli
    （见 lark-cli 源码 internal/keychain 的 StorageDir；Linux 上不用系统钥匙串，主密钥就是这个目录里的文件）。"""
    if is_windows():
        # Windows 版 lark-cli 把凭据用 DPAPI 加密后放在 HKCU 注册表里（internal/keychain/keychain_windows.go），
        # 不落文件；这里返回空串，调用方（沙箱可写路径）跳过
        return ""
    if is_linux():
        base = os.environ.get("LARKSUITE_CLI_DATA_DIR", "")
        if not os.path.isabs(base):
            base = str(Path.home() / ".local" / "share")
        return str(Path(base) / "lark-cli")
    return str(Path.home() / "Library" / "Application Support" / "lark-cli")


def shared_tmp() -> str:
    """/tmp 的真实路径：macOS 上 /tmp 是 /private/tmp 的软链，沙箱规则要写真实路径。Windows 没有共享的 /tmp，返回空串。"""
    if is_windows():
        return ""
    return "/tmp" if is_linux() else "/private/tmp"


def os_info() -> dict:
    """系统名称、版本和架构（控制台入网和心跳用，原样上报前由 console 按字符集规整）。
    macOS：("macOS", mac_ver)；Linux：/etc/os-release 的 NAME / VERSION_ID（读不到为 "Linux" 和内核版本）。"""
    import platform
    if system() == "darwin":
        return {"name": "macOS", "version": platform.mac_ver()[0] or platform.release(), "arch": platform.machine()}
    if is_linux():
        name, version = "Linux", platform.release()
        try:
            rel = {}
            for line in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
                k, sep, v = line.partition("=")
                if sep:
                    rel[k.strip()] = v.strip().strip("\"'")
            name = rel.get("NAME") or name
            version = rel.get("VERSION_ID") or version
        except OSError:
            pass
        return {"name": name, "version": version, "arch": platform.machine()}
    if is_windows():
        # platform.release() 在 Windows 11 上也可能是 "10"；版本号用内部版本（10.0.22631），一看就分得清
        return {"name": "Windows", "version": platform.version() or platform.release(), "arch": platform.machine()}
    return {"name": system(), "version": platform.release(), "arch": platform.machine()}


# ---------- Windows ----------
def argv(cmd, path: str = None) -> list:
    """要交给 subprocess 的命令行：Windows 上把 cmd[0]（claude / codex / lark-cli 这类裸命令名）解析成可以直接
    CreateProcess 的程序，其他平台原样返回。见 exe_argv。"""
    cmd = list(cmd)
    if not cmd or not is_windows():
        return cmd
    return exe_argv(cmd[0], path) + cmd[1:]


def exe_argv(name: str, path: str = None) -> list:
    """Windows：命令名 → 程序和前置参数。.exe 原样；npm 装的 .cmd 包装脚本换成它真正调用的程序
    （node <脚本>，或包里的 .exe）。不能直接跑 .cmd：参数会经 cmd.exe 再解析一遍，worker 写的汇报文字里的
    & | " % 会被当成命令（BatBadBut 一类问题），守护进程在沙箱外，这等于命令注入。解析不了就报错，不冒险。"""
    import re
    import shutil
    p = name
    if not os.path.dirname(name) or not os.path.splitext(name)[1]:
        p = shutil.which(name, path=path) or name
    low = p.lower()
    if not low.endswith((".cmd", ".bat")):
        return [p]
    try:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    base = Path(p).parent
    # npm 的包装脚本里先有一行 IF EXIST "%dp0%\node.exe"（找自带的 node），真正的目标在后面：跳过 node.exe
    m = None
    for c in re.finditer(r'"%~?dp0%?\\?([^"%]+?\.(?:exe|js|cjs|mjs))"', text, re.IGNORECASE):
        if c.group(1).replace("\\", "/").rsplit("/", 1)[-1].lower() != "node.exe":
            m = c
    if m:
        target = base / m.group(1).replace("\\", os.sep)
        if target.suffix.lower() == ".exe":
            return [str(target)]
        node = base / "node.exe"
        node_s = str(node) if node.exists() else (shutil.which("node", path=path) or "node")
        return [node_s, str(target)]
    raise OSError("%s 是批处理文件，参数会经 cmd.exe 转义，不安全：请装原生版本（.exe），"
                  "或在 config.env 里把对应的 TICKETD_CLAUDE / TICKETD_CODEX / TICKETD_LARK 指向 .exe" % p)


def git_bash() -> str:
    """Windows 上的 Git Bash（bash.exe）路径；找不到返回空串。
    Claude Code 在 Windows 上用 Git Bash 跑 Bash 工具，worker 写的沙箱外脚本也用它跑。顺序：
    CLAUDE_CODE_GIT_BASH_PATH（Claude Code 自己认的变量）→ git.exe 所在安装目录下的 bin/bash.exe → 常见安装位置。
    不用 PATH 上的 bash：System32/bash.exe 是 WSL 的入口（公司禁用 WSL 时直接报错）。"""
    import shutil
    cand = []
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
    if env:
        cand.append(env)
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <Git>\cmd\git.exe 或 <Git>\mingw64\bin\git.exe
        cand += [str(root / "bin" / "bash.exe"), str(root.parent / "bin" / "bash.exe")]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            cand.append(os.path.join(base, "Git", "bin", "bash.exe"))
    for c in cand:
        if c and os.path.isfile(c) and "system32" not in c.lower():
            return c
    return ""


class FileLock:
    """跨平台的进程间互斥锁（按打开的文件计）：POSIX 用 flock，Windows 用 msvcrt.locking 锁第一个字节。
    blocking=False 时拿不到锁抛 OSError。同一进程里不要对同一个文件嵌套。"""

    def __init__(self, fd: int):
        self.fd = fd

    def acquire(self, blocking: bool = True) -> None:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(self.fd, 0, os.SEEK_SET)
            if not blocking:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                return
            while True:  # LK_LOCK 只重试 10 秒就报错；这里一直等
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    import time
                    time.sleep(0.05)
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def release(self) -> None:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(self.fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)

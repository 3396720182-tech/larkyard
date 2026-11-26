from __future__ import annotations

# 工单目录对 worker 可写，而守护进程不在沙箱里：在这里打开文件一律不跟随符号链接、不在 FIFO 上阻塞、
# 不接受硬链接，避免 worker 借守护进程之手读写沙箱外的文件。

import os
import stat
import sys
from typing import Optional

# Windows（spec §3.2）：没有 O_NOFOLLOW / O_NONBLOCK / openat。worker 不能建符号链接（要特权），但能建目录联接点
# （junction）和硬链接，所以改为「打开之前看每一级不是重解析点，打开之后用句柄取真实路径核对」：
# 中间某级在检查之后被换成联接点，句柄的真实路径就和预期不同，直接拒绝。O_BINARY：不做换行转换。
_WIN = sys.platform == "win32"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _check_regular(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OSError("不是普通文件或存在硬链接")


def _is_reparse(path: str) -> bool:
    """是不是会把路径引到别处的链接（符号链接、联接点等「名字代理」类重解析点）。OneDrive 按需文件、
    去重这类重解析点不改路径，不算（否则 OneDrive 里的工单目录一个文件都打不开）；打开后的真实路径核对兜底。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if not getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return False
    tag = getattr(st, "st_reparse_tag", 0)
    return not tag or bool(tag & 0x20000000)  # 0x20000000：名字代理位（联接点、符号链接都带）


def _win_open(path: str, flags: int, mode: int = 0o644, root: Optional[str] = None) -> int:
    """Windows 上的「不跟随链接」打开：最后一级不能是重解析点；打开后句柄的真实路径必须等于
    （父目录的真实路径 + 文件名），给了 root 时还必须在 root 的真实路径之内。"""
    from . import winapi
    path = os.path.abspath(path)
    parent, name = os.path.split(path)
    if _is_reparse(path):
        raise OSError("是符号链接或联接点：%s" % path)
    expect = os.path.normcase(os.path.join(os.path.realpath(parent), name))
    fd = os.open(path, flags | _BINARY, mode)
    real = winapi.final_path(fd)
    ok = real is not None and os.path.normcase(real) == expect
    if ok and root is not None:
        top = os.path.normcase(os.path.realpath(root)).rstrip("\\/") + os.sep
        ok = expect.startswith(top)
    if not ok:
        os.close(fd)
        raise OSError("打开的不是预期的文件（路径被换成了链接？）：%s" % path)
    return fd


def open_append(path) -> int:
    if _WIN:
        fd = _win_open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o644)
    _check_regular(fd)
    return fd


def open_read(path) -> Optional[int]:
    try:
        if _WIN:
            fd = _win_open(str(path), os.O_RDONLY)
        else:
            fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        return None
    _check_regular(fd)
    return fd


def open_read_under(root: str, rel: str) -> Optional[int]:
    """在 root 目录下逐级打开 rel（openat + O_NOFOLLOW，每一级都不跟随符号链接），返回只读 fd；不存在返回 None。
    open_read 的 O_NOFOLLOW 只管最后一级：检查完路径之后、打开之前，中间某级目录被换成指向外面的符号链接，
    就会打开到 root 外面的文件。这里每一级都相对上一级已打开的目录 fd 打开，没有这个窗口，也不依赖
    F_GETPATH 或 /proc 事后核对（macOS / Linux 一样）。rel 里不能有 ..、空段或绝对路径。"""
    parts = [p for p in rel.replace("/", os.sep).split(os.sep) if p not in ("", ".")]
    if not parts or os.path.isabs(rel) or ".." in parts:
        raise OSError("路径不合法：%r" % rel)
    if _WIN:
        # 没有 openat：逐级看不是重解析点，打开后按句柄的真实路径核对仍在 root 之内（见 _win_open）
        cur = root
        for name in parts[:-1]:
            cur = os.path.join(cur, name)
            if not os.path.isdir(cur):
                return None
            if _is_reparse(cur):
                raise OSError("是符号链接或联接点：%s" % cur)
        try:
            out = _win_open(os.path.join(cur, parts[-1]), os.O_RDONLY, root=root)
        except FileNotFoundError:
            return None
        _check_regular(out)
        return out
    dirflags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root, dirflags)
    try:
        for name in parts[:-1]:
            try:
                nfd = os.open(name, dirflags, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = nfd
        try:
            out = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
    finally:
        os.close(fd)
    _check_regular(out)
    return out


def create_excl(path, data: bytes) -> None:
    if _WIN:
        fd = _win_open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def read_at(fd: int, offset: int, limit: int) -> bytes:
    """从 offset 起最多读 limit 字节。"""
    os.lseek(fd, offset, os.SEEK_SET)
    chunks = []
    while limit > 0:
        b = os.read(fd, min(limit, 1 << 20))
        if not b:
            break
        chunks.append(b)
        limit -= len(b)
    return b"".join(chunks)

from __future__ import annotations

# 工单目录对 worker 可写，而守护进程不在沙箱里：在这里打开文件一律不跟随符号链接、不在 FIFO 上阻塞、
# 不接受硬链接，避免 worker 借守护进程之手读写沙箱外的文件。

import os
import stat
from typing import Optional


def _check_regular(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OSError("不是普通文件或存在硬链接")


def open_append(path) -> int:
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    _check_regular(fd)
    return fd


def open_read(path) -> Optional[int]:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    _check_regular(fd)
    return fd


def open_read_under(root: str, rel: str) -> Optional[int]:
    """在 root 目录下逐级打开 rel（openat + O_NOFOLLOW，每一级都不跟随符号链接），返回只读 fd；不存在返回 None。
    open_read 的 O_NOFOLLOW 只管最后一级：检查完路径之后、打开之前，中间某级目录被换成指向外面的符号链接，
    就会打开到 root 外面的文件。这里每一级都相对上一级已打开的目录 fd 打开，没有这个窗口，也不依赖
    F_GETPATH 或 /proc 事后核对（macOS / Linux 一样）。rel 里不能有 ..、空段或绝对路径。"""
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not parts or os.path.isabs(rel) or ".." in parts:
        raise OSError("路径不合法：%r" % rel)
    dirflags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
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
            out = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
    finally:
        os.close(fd)
    _check_regular(out)
    return out


def create_excl(path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
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

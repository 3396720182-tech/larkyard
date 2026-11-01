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

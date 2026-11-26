"""Windows 原生接口（ctypes，只用标准库；spec §3.2）。

- 作业对象（Job Object）：worker、沙箱外执行各自放进一个命名作业，停止 / 收割时一次结束整棵进程树
  （含已脱离父进程的后台进程）。这替代 POSIX 上的进程组（killpg）和孤儿清理（按环境变量 / 目录找残留）。
- 查进程：是否还在、完整命令行、进程表（父进程、已运行秒数、累计 CPU 秒数）。
- 已打开文件的真实路径（GetFinalPathNameByHandleW）：safefs 在 Windows 上靠它确认没有被链接 / 联接点引到别处。

只在 Windows 上调用；其他平台导入不报错，调用会抛 OSError。
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import Dict, Optional, Tuple

WINDOWS = sys.platform == "win32"

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_JOB_INFO_ACCOUNTING = 1          # JobObjectBasicAccountingInformation
_PROCESS_TERMINATE = 0x0001
_PROCESS_DUP_HANDLE = 0x0040
_ERROR_ALREADY_EXISTS = 183
_PROCESS_QUERY_LIMITED = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_PROCESS_COMMAND_LINE_INFO = 60   # ProcessCommandLineInformation（Windows 8.1 起）
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value

if WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")

    class _FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class _ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]

    def _fn(dll, name, restype, *argtypes):
        f = getattr(dll, name)
        f.restype = restype
        f.argtypes = list(argtypes)
        return f

    _CreateJobObjectW = _fn(_k32, "CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _OpenJobObjectW = _fn(_k32, "OpenJobObjectW", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _AssignProcessToJobObject = _fn(_k32, "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE,
                                    wintypes.HANDLE)
    _TerminateJobObject = _fn(_k32, "TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _QueryInformationJobObject = _fn(_k32, "QueryInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                                     ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _OpenProcess = _fn(_k32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _TerminateProcess = _fn(_k32, "TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _WaitForSingleObject = _fn(_k32, "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _CloseHandle = _fn(_k32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _GetProcessTimes = _fn(_k32, "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_FILETIME),
                           ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME))
    _GetSystemTimeAsFileTime = _fn(_k32, "GetSystemTimeAsFileTime", None, ctypes.POINTER(_FILETIME))
    _CreateToolhelp32Snapshot = _fn(_k32, "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD,
                                    wintypes.DWORD)
    _Process32FirstW = _fn(_k32, "Process32FirstW", wintypes.BOOL, wintypes.HANDLE,
                           ctypes.POINTER(_PROCESSENTRY32W))
    _Process32NextW = _fn(_k32, "Process32NextW", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _GetFinalPathNameByHandleW = _fn(_k32, "GetFinalPathNameByHandleW", wintypes.DWORD, wintypes.HANDLE,
                                     wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    _GetCurrentProcess = _fn(_k32, "GetCurrentProcess", wintypes.HANDLE)
    _DuplicateHandle = _fn(_k32, "DuplicateHandle", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                           ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _NtResumeProcess = _fn(_nt, "NtResumeProcess", ctypes.c_long, wintypes.HANDLE)
    _NtQueryInformationProcess = _fn(_nt, "NtQueryInformationProcess", ctypes.c_long, wintypes.HANDLE,
                                     ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))


def _require() -> None:
    if not WINDOWS:
        raise OSError("只在 Windows 上可用")


def _err() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _ft(ft) -> int:
    return (ft.hi << 32) | ft.lo


# ---------- 作业对象 ----------
def popen_in_job(args, job_name: str, **kw):
    """以挂起状态启动进程、放进命名作业、再恢复运行：它和它以后起的所有子孙进程都在这个作业里
    （放进作业之前进程一行代码都没跑，不会有漏网的子进程）。返回 (Popen, 作业句柄)；作业句柄由调用方 close_handle。
    不设 KILL_ON_JOB_CLOSE：守护进程重启时 worker 和沙箱外执行不跟着死，和 macOS / Linux 一致，由 recover 按名字处理。
    CREATE_NO_WINDOW：守护进程在 pythonw 下没有控制台，子进程不带这个标志会各自弹出一个控制台窗口。"""
    import subprocess
    _require()
    flags = kw.pop("creationflags", 0) | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    job = _CreateJobObjectW(None, job_name)
    if not job:
        raise _err()
    if ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
        keep_name_alive(job)
    try:
        proc = subprocess.Popen(args, creationflags=flags, **kw)
    except BaseException:
        _CloseHandle(job)
        raise
    try:
        if not _AssignProcessToJobObject(job, int(proc._handle)):
            raise _err()
        status = _NtResumeProcess(int(proc._handle))
        if status != 0:
            raise OSError("NtResumeProcess 失败：0x%08x" % (status & 0xFFFFFFFF))
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        _CloseHandle(job)
        raise
    return proc, job


def keep_name_alive(job) -> bool:
    """命名内核对象在最后一个句柄关掉时就失去名字（哪怕作业里还有进程）：守护进程崩溃或重启之后，
    recover 就按名字找不回作业、清不掉残留进程。所以把一个句柄复制给托管进程（winsvc，环境变量
    TICKETD_SUPERVISOR_PID）：它比守护进程活得久，名字就一直在。同名作业以后再建会拿到同一个对象，
    只在新建时复制一次。只给查询和结束的权限。失败不影响启动（只是重启后清理不到）。"""
    try:
        pid = int(os.environ.get("TICKETD_SUPERVISOR_PID") or 0)
    except ValueError:
        return False
    if not pid or pid == os.getpid():
        return False
    target = _OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
    if not target:
        return False
    try:
        out = wintypes.HANDLE()
        return bool(_DuplicateHandle(_GetCurrentProcess(), job, target, ctypes.byref(out),
                                     _JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, 0))
    finally:
        _CloseHandle(target)


def close_handle(h) -> None:
    if WINDOWS and h:
        _CloseHandle(h)


def job_active(job_name: str) -> Optional[int]:
    """命名作业里还在跑的进程数；作业不存在返回 None。"""
    _require()
    h = _OpenJobObjectW(_JOB_OBJECT_QUERY, False, job_name)
    if not h:
        return None
    try:
        info = _ACCOUNTING()
        if not _QueryInformationJobObject(h, _JOB_INFO_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return int(info.ActiveProcesses)
    finally:
        _CloseHandle(h)


def terminate_job(job_name: str, code: int = 1) -> int:
    """结束命名作业里的所有进程，返回结束前还在跑的进程数；作业不存在（早已结束、或从没建过）返回 0。永不抛异常。"""
    if not WINDOWS:
        return 0
    h = _OpenJobObjectW(_JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, job_name)
    if not h:
        return 0
    try:
        n = 0
        info = _ACCOUNTING()
        if _QueryInformationJobObject(h, _JOB_INFO_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            n = int(info.ActiveProcesses)
        if n:
            _TerminateJobObject(h, code)
        return n
    finally:
        _CloseHandle(h)


# ---------- 进程 ----------
def alive(pid: int) -> bool:
    """pid 还在运行（打不开但「拒绝访问」也算在：进程存在，只是没权限）。"""
    _require()
    h = _OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return _WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
    finally:
        _CloseHandle(h)


def terminate(pid: int, code: int = 1) -> bool:
    _require()
    h = _OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_TerminateProcess(h, code))
    finally:
        _CloseHandle(h)


def command_line(pid: int) -> str:
    """pid 的完整命令行；拿不到返回空串。"""
    _require()
    h = _OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ""
    try:
        size = 4096
        for _ in range(4):
            buf = ctypes.create_string_buffer(size)
            need = wintypes.ULONG(0)
            status = _NtQueryInformationProcess(h, _PROCESS_COMMAND_LINE_INFO, buf, size, ctypes.byref(need))
            if (status & 0xFFFFFFFF) == _STATUS_INFO_LENGTH_MISMATCH and need.value > size:
                size = need.value
                continue
            if status != 0:
                return ""
            us = _UNICODE_STRING.from_buffer(buf)
            if not us.Buffer or not us.Length:
                return ""
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        return ""
    finally:
        _CloseHandle(h)


def process_table() -> Dict[int, Tuple[int, float, float]]:
    """{pid: (父进程 pid, 已运行秒数, 累计 CPU 秒数)}。打不开的进程（别的用户、系统进程）已运行秒数和 CPU 记 -1。"""
    _require()
    snap = _CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        raise _err()
    entries = []
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = _Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            entries.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID)))
            ok = _Process32NextW(snap, ctypes.byref(pe))
    finally:
        _CloseHandle(snap)
    now = _FILETIME()
    _GetSystemTimeAsFileTime(ctypes.byref(now))
    table = {}
    for pid, ppid in entries:
        age, cpu = -1.0, -1.0
        h = _OpenProcess(_PROCESS_QUERY_LIMITED, False, pid) if pid else None
        if h:
            try:
                c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
                if _GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    age = max(0.0, (_ft(now) - _ft(c)) / 1e7)
                    cpu = (_ft(k) + _ft(u)) / 1e7
            finally:
                _CloseHandle(h)
        table[pid] = (ppid, age, cpu)
    return table


# ---------- 文件 ----------
def final_path(fd: int) -> Optional[str]:
    """已打开文件（C 运行库的 fd）的真实路径：联接点、符号链接都已解析；拿不到返回 None。"""
    if not WINDOWS:
        return None
    import msvcrt
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return None
    buf = ctypes.create_unicode_buffer(32768)
    n = _GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if not n or n >= len(buf):
        return None
    return strip_verbatim(buf.value)


def strip_verbatim(p: str) -> str:
    r"""\\?\C:\x → C:\x；\\?\UNC\srv\share → \\srv\share。"""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p

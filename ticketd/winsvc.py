"""Windows 上托管 ticketd 守护进程（spec §3.2）：替代 macOS 的 launchd（KeepAlive）和 Linux 的 systemd（Restart=always）。

计划任务（登录时触发，scripts/windows/install.ps1 注册）用 pythonw.exe 跑 `winsvc run`：
- 同一时间只有一个托管进程（锁文件 ~/.ticketd/db/winsvc.lock）；
- 拉起守护进程（python.exe，无窗口），标准输出 / 错误追加到 ~/.ticketd/logs/daemon.out.log / daemon.err.log；
- 守护进程退出后：收到过停机请求就跟着退出；否则等 10 秒（ThrottleInterval / RestartSec）再拉起；
  因为更新退出（update.EXIT_RESTART）立即拉起。
- ~/.ticketd/service.env（install.ps1 写，KEY=VALUE 一行一个）里的变量注入守护进程环境，对应 plist / unit 里的环境变量。

`winsvc stop`：写停机请求（daemon.stop），等守护进程按 §8 的停机流程退出（最长 60 秒），还在就结束它；托管进程随之退出。
`winsvc status`：打印托管进程和守护进程是否在跑。
只用标准库；这里不导入调度相关的模块，守护进程有语法错误也不影响托管和重启。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, host

RESTART_DELAY = 10.0
STOP_WAIT = 60.0
EXIT_RESTART = 75  # 和 update.EXIT_RESTART 一致（这里不导入 update，见模块说明）
REPO = Path(__file__).resolve().parent.parent


def lock_path() -> Path:
    return config.db_dir() / "winsvc.lock"


def stop_path() -> Path:
    return config.db_dir() / "daemon.stop"  # 和 daemon.stop_request_path() 一致


def pid_path() -> Path:
    return config.db_dir() / "winsvc.pid"


def service_env_path() -> Path:
    return config.home() / "service.env"


def load_service_env(path: Path = None) -> dict:
    out = {}
    try:
        text = (path or service_env_path()).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def python_exe() -> str:
    """守护进程用的解释器：pythonw.exe 旁边的 python.exe（守护进程要有标准输出可重定向）；TICKETD_PYTHON 优先。"""
    exe = os.environ.get("TICKETD_PYTHON") or sys.executable
    p = Path(exe)
    if p.name.lower() == "pythonw.exe" and (p.parent / "python.exe").exists():
        return str(p.parent / "python.exe")
    return exe


def daemon_command() -> list:
    return [python_exe(), "-c", "import sys; sys.path.insert(0, sys.argv.pop(1)); from ticketd.daemon import main; "
            "sys.exit(main())", str(REPO)]


def _open_logs():
    d = config.logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return open(d / "daemon.out.log", "ab"), open(d / "daemon.err.log", "ab")


def _note(err, text: str) -> None:
    try:
        err.write(("%s winsvc: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)).encode("utf-8"))
        err.flush()
    except OSError:
        pass


def run(sleep=time.sleep, popen=subprocess.Popen, max_starts: int = 0) -> int:
    """托管主循环。max_starts（测试用）> 0 时拉起这么多次后返回。"""
    config.db_dir().mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path(), "a+")
    for i in range(6):  # 安装程序探测锁时会短暂拿一下：稍等再试，别把刚启动的托管进程挡掉
        try:
            host.FileLock(lock_file.fileno()).acquire(blocking=False)
            break
        except OSError:
            if i < 5:
                sleep(0.5)
    else:
        if sys.stderr is not None:  # pythonw 下没有标准错误
            print("winsvc already running", file=sys.stderr)
        return 1
    try:
        stop_path().unlink()
    except OSError:
        pass
    pid_path().write_text(str(os.getpid()), encoding="utf-8")
    env = dict(os.environ)
    env.update(load_service_env())
    env["TICKETD_SUPERVISOR_PID"] = str(os.getpid())  # 守护进程把作业句柄复制给托管进程，见 winapi.keep_name_alive
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    flags = 0x08000000 if host.is_windows() else 0  # CREATE_NO_WINDOW
    starts = 0
    out, err = _open_logs()
    try:
        while True:
            starts += 1
            try:
                proc = popen(daemon_command(), stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                             cwd=str(config.home()), creationflags=flags)
                code = proc.wait()
            except OSError as e:
                _note(err, "守护进程起不来：%r" % (e,))
                code = -1
            if stop_path().exists():
                _note(err, "已按停机请求退出")
                try:
                    stop_path().unlink()
                except OSError:
                    pass
                return 0
            if max_starts and starts >= max_starts:
                return code
            if code != EXIT_RESTART:
                _note(err, "守护进程退出（%s），%d 秒后重新拉起" % (code, RESTART_DELAY))
                sleep(RESTART_DELAY)
    finally:
        out.close()
        err.close()
        try:
            pid_path().unlink()
        except OSError:
            pass


def _running(lock: Path) -> bool:
    """锁文件被别的进程锁着 = 托管进程在跑。"""
    if not lock.exists():
        return False
    with open(lock, "a+") as f:
        lk = host.FileLock(f.fileno())
        try:
            lk.acquire(blocking=False)
        except OSError:
            return True
        lk.release()
        return False


def _daemon_running() -> bool:
    lock = config.db_dir() / "daemon.lock"
    return _running(lock)


def stop(wait: float = STOP_WAIT, sleep=time.sleep, clock=time.monotonic) -> int:
    """请求停机并等它退出；返回 0 = 已停（或本来就没在跑），1 = 超时后强制结束了。"""
    if not _running(lock_path()) and not _daemon_running():
        return 0
    config.db_dir().mkdir(parents=True, exist_ok=True)
    stop_path().write_text("stop\n", encoding="utf-8")
    end = clock() + wait
    while clock() < end:
        if not _running(lock_path()) and not _daemon_running():
            return 0
        sleep(0.5)
    try:
        pid = int(pid_path().read_text(encoding="utf-8").strip())
        if host.is_windows():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    except (OSError, ValueError):
        pass
    return 1


def status() -> str:
    return "supervisor=%s daemon=%s" % ("running" if _running(lock_path()) else "stopped",
                                        "running" if _daemon_running() else "stopped")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--home" in argv:  # 计划任务没有环境变量可设：数据目录从参数带进来
        i = argv.index("--home")
        if i + 1 < len(argv):
            os.environ["TICKETD_HOME"] = argv[i + 1]
        del argv[i:i + 2]
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        return run()
    if cmd == "stop":
        return stop()
    if cmd == "status":
        print(status())
        return 0
    print("用法：winsvc run|stop|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

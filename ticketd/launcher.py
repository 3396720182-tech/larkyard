"""受管安装的启动器：服务（launchd / systemd）每次拉起 ticketd 都先跑这个文件，再 exec 当前版本的 bin/ticketd。

它负责「新版本自检超时或反复崩溃就切回上一个版本」（openspec add-signed-self-update，对抗审查 B4），所以：
- 安装在所有版本目录之外：~/.ticketd/app/launcher.py（由 `ticket update adopt` 从版本包里拷过去，新版本确认后才替换）；
- 只用 Python 标准库，绝不导入 ticketd 包或任何版本目录里的代码：新版本的 bin/ticketd、ticketd/__init__.py
  有语法错误也不影响这里；
- 状态只放在 ~/.ticketd/app/update-state.json，不读数据库（免得依赖新版本的表结构）。

update-state.json（ticketd/update.py 写，这里读和改）：
  {"status": "probation", "from": "0.3.0", "to": "0.3.1", "deadline": <epoch>, "window": <秒>, "starts": 0, "token": "..."}
  每次启动 starts 加一（守护进程按停机信号正常退出时记 clean_exit，那次不算）。看门狗记了 timed_out、或 starts 到 MAX_STARTS，就把 current 改回 from，status 记成
  rolled_back，再 exec 旧版本。墙钟已过 deadline、不是看门狗动的手、而且中间重新开过机（试用期里关机了），
  重新给一个完整试用期，只给一次；没重启过就按超时切回；第二次再过期就按超时切回。新版本守护进程自检通过后把 status 改成 confirmed。
试用期内另起一个看门狗进程，按单调时钟（睡眠不计时）等到试用期满 + 宽限还没确认（守护进程活着但卡死、
自检一直不过），就记下 timed_out 并结束守护进程，服务管理器重新拉起时这里按超时切回。
"""
import fcntl
import json
import os
import signal
import sys
import time

STATE = "update-state.json"
MAX_STARTS = 3          # 试用期内第 3 次被拉起（前两次都退出了）就切回
WATCHDOG_GRACE = 30.0   # 过了 deadline 再等这么久，给守护进程自己按超时退出的机会
WATCHDOG_POLL = 5.0
KILL_WAIT = 60.0        # SIGTERM 之后等这么久再 SIGKILL（守护进程的停机流程最长 45 秒）


def app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def read_state(app):
    try:
        with open(os.path.join(app, STATE), "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


class locked:
    """update-state.json 的读改写都在这把锁里做（启动器、看门狗、守护进程的确认 / 安装各在不同进程里）。
    flock 按打开的文件计：同一进程里不要嵌套。"""

    def __init__(self, app):
        self.path = os.path.join(app, STATE + ".lock")

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def boot_time():
    """这次开机的时刻（墙钟 − 含睡眠的开机时长）。拿不到返回 None。用来分辨「试用期里关机重启过」和
    「新版本自己退出了」：前者才值得再给一次试用期。"""
    clk = getattr(time, "CLOCK_BOOTTIME", None) if sys.platform.startswith("linux") else \
        getattr(time, "CLOCK_MONOTONIC", None)  # macOS 的 CLOCK_MONOTONIC 含睡眠时间
    if clk is None:
        return None
    try:
        return time.time() - time.clock_gettime(clk)
    except OSError:
        return None


BOOT_SLACK = 120.0  # 两次算出的开机时刻差这么多以上才算重启过（墙钟校时会让它小幅漂移）


def rebooted(st):
    try:
        then, now = float(st.get("boot")), boot_time()
    except (TypeError, ValueError):
        return False
    return now is not None and abs(now - then) > BOOT_SLACK


def write_state(app, st):
    path = os.path.join(app, STATE)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:  # rename 本身也要落盘：断电后读到半个状态文件，坏版本就既不切回也没有看门狗
        dfd = os.open(app, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def version_dir(app, version):
    return os.path.join(app, "versions", version)


def switch(app, version):
    """把 current 原子地指向 versions/<version>（相对链接，rename 一次完成）。"""
    if not version or "/" in version or version in (".", ".."):
        raise ValueError("bad version %r" % (version,))
    if not os.path.isdir(version_dir(app, version)):
        raise FileNotFoundError(version_dir(app, version))
    if not os.path.exists(os.path.join(version_dir(app, version), "bin", "ticketd")):
        raise FileNotFoundError(os.path.join(version_dir(app, version), "bin", "ticketd"))
    cur = os.path.join(app, "current")
    tmp = "%s.tmp-%d" % (cur, os.getpid())
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(os.path.join("versions", version), tmp)
    os.replace(tmp, cur)


def decide(app, now=None):
    """读状态、计启动次数，需要时切回。返回（可能已更新的）状态。启动器里任何异常都不能挡住启动当前版本。"""
    with locked(app):
        return _decide(app, now)


def _decide(app, now=None):
    now = time.time() if now is None else now
    st = read_state(app)
    if st.get("status") != "probation":
        return st
    if st.pop("clean_exit", None):
        pass  # 上次是守护进程收到停机信号后正常退出的（用户重启服务、关机）：不算一次崩溃
    else:
        try:
            st["starts"] = int(st.get("starts") or 0) + 1
        except (TypeError, ValueError):
            st["starts"] = MAX_STARTS
    why = None
    try:
        deadline = float(st.get("deadline") or 0)
        window = float(st.get("window") or 0)
    except (TypeError, ValueError):
        deadline, window = 0.0, 0.0
    if st.get("timed_out"):
        why = "timeout"  # 看门狗在试用期满时结束的：新版本活着但没通过自检
    elif st["starts"] >= MAX_STARTS:
        why = "crash"
    elif now > deadline:
        if window > 0 and not st.get("extended") and rebooted(st):
            # 过了 deadline、看门狗没动手，而且中间重新开过机：试用期里关机了，不算新版本的错，
            # 重新给一个完整的试用期，只给一次。没重启过（新版本在 deadline 和看门狗之间自己退出）照常按超时切回
            st.update(deadline=now + window, extended=True, boot=boot_time())
        else:
            why = "timeout"
    if why:
        frm = st.get("from")
        try:
            switch(app, frm)
            st.update(status="rolled_back", reason=why, rolled_at=now, notified=False)
        except (OSError, ValueError) as e:
            # 上一个版本不在了：切不回去，只能接着跑新版本，由守护进程私信说明
            st.update(status="rollback_failed", reason=why, error=repr(e)[:200], rolled_at=now, notified=False)
    write_state(app, st)
    return st


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch(app, pid, token, remaining, clock=time.monotonic, sleep=time.sleep, kill=os.kill):
    """看门狗主体：再过 remaining + 宽限秒（单调时钟：电脑睡眠时不走，合盖不会误判超时）还在同一个试用期，
    就记下 timed_out、结束守护进程。返回是否动了手。"""
    end_at = clock() + max(0.0, remaining) + WATCHDOG_GRACE
    while True:
        st = read_state(app)
        if st.get("status") != "probation" or st.get("token") != token:
            return False
        if not _alive(pid):
            return False
        if clock() >= end_at:
            break
        sleep(WATCHDOG_POLL)
    with locked(app):
        st = read_state(app)  # 锁里重读：守护进程可能刚刚确认了
        if st.get("status") != "probation" or st.get("token") != token:
            return False
        st["timed_out"] = True
        try:
            write_state(app, st)
        except OSError:
            pass
    try:
        kill(pid, signal.SIGTERM)
    except OSError:
        return False
    end = clock() + KILL_WAIT
    while clock() < end:
        if not _alive(pid):
            return True
        sleep(1.0)
    try:
        kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def spawn_watchdog(app, st):
    """两次 fork：看门狗被系统收养，不留僵尸给守护进程；仍在服务的进程组 / cgroup 里，服务重启时一起结束。"""
    target = os.getpid()  # exec 之后就是守护进程
    try:
        remaining = float(st.get("deadline") or 0) - time.time()
    except (TypeError, ValueError):
        return
    try:
        pid = os.fork()
    except OSError:
        return
    if pid:
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return
    try:
        if os.fork():
            os._exit(0)
        try:
            # 不占着服务的标准输入输出（日志文件或管道）：守护进程退出后，读这些管道的一方不会被看门狗拖住
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
        except OSError:
            pass
        watch(app, target, st.get("token"), remaining)
    except BaseException:
        pass
    os._exit(0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    app = app_dir()
    try:
        st = decide(app)
    except Exception as e:  # 状态文件写不了等：照常启动当前版本
        print("launcher: %r" % (e,), file=sys.stderr, flush=True)
        st = {}
    if st.get("status") == "rolled_back" and not st.get("notified"):
        print("launcher: %s 自检不过（%s），已切回 %s" % (st.get("to"), st.get("reason"), st.get("from")),
              file=sys.stderr, flush=True)
    entry = os.path.join(app, "current", "bin", "ticketd")
    if not os.path.exists(entry):
        print("launcher: 找不到 %s" % entry, file=sys.stderr, flush=True)
        return 1
    if st.get("status") == "probation":
        spawn_watchdog(app, st)
    os.execv("/bin/sh", ["/bin/sh", entry] + argv)
    return 1  # 到不了


if __name__ == "__main__":
    sys.exit(main())

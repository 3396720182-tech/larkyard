"""空闲时换上新版本带的 bridge（T0112，规格见 docs/spec.md §13「空闲换新」）。

以前新 bridge 只在每天 04:07 的重启窗口换上：bridge 没有「有没有进行中的回复」的对外接口，白天从外面猜空闲怕打断主会话。
现在用 bridge 自己的日志判断（bridge.activity）：并发池最后报的 active 是 0、连续 TICKETD_BRIDGE_IDLE_MINUTES 分钟
（默认 15）没有收到消息和会话活动，就由守护进程在后台线程里做一次 `ticket bridge restart --if-stale`（同一个函数、同一套
切回和私信）。每 5 分钟看一次，和控制台心跳同一节奏；只读文件，不发请求。每天的重启窗口照旧留着兜底。

不换的时候：没到间隔、关掉了（0）、bridge 不是 Larkyard 管理的、Larkyard 还在试用期或正在装更新（update_hold）、
没有待生效的 bridge 也不缺服务环境、日志读不到、bridge 在忙或刚有人用过、同一个目标上次空闲换新失败还不到一天。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import config

CHECK_EVERY = 300.0          # 秒：和控制台心跳同一节奏
FAIL_BACKOFF = 24 * 3600.0   # 同一个目标空闲换新失败后，一天内不再自动试（每天的重启窗口照旧）


def target_key(target, outdated: bool) -> str:
    """这次要换成什么：新 bridge 的 commit + tree_sha256；只是服务文件缺环境时是 service。"""
    if target:
        return "%s:%s" % (target[1].get("commit"), target[1].get("tree_sha256"))
    return "service" if outdated else ""


def due(now: Optional[float] = None, hold: bool = False) -> tuple:
    """现在该不该换：返回 (原因, profile, 目标)。原因是空串表示该换；不该换时 profile、目标为空。只读文件。"""
    from . import bridge, update
    now = time.time() if now is None else now
    quiet = config.bridge_idle_minutes()
    if quiet <= 0:
        return "关掉了", "", ""
    if hold:
        return "Larkyard 正在装更新", "", ""
    profile = bridge.profile_name()
    sf = bridge.bridge_service_file(profile)
    if not bridge.is_managed_service(sf):
        return "bridge 不是 Larkyard 管理的", "", ""
    if update.read_state().get("status") == "probation":
        return "Larkyard 新版本还在试用期", "", ""
    key = target_key(bridge.stale(), bridge.service_outdated(sf)) or ("unfinished" if bridge.switch_unfinished() else "")
    if not key:
        return "没有待生效的 bridge", "", ""
    last = bridge.read_state().get("idle_attempt") or {}
    if last.get("key") == key and not last.get("ok") and now - float(last.get("at") or 0) < FAIL_BACKOFF:
        return "上次空闲换新失败，等每天的重启窗口", "", ""
    home = bridge.service_env(sf).get("LARK_CHANNEL_HOME") or None
    act = bridge.activity(profile, home=home, now=now)
    if not act["logs"]:
        return "读不到 bridge 日志", "", ""
    if act["busy"]:
        return "bridge 有主会话在跑", "", ""
    if act["last"] is not None and now - act["last"] < quiet * 60:
        return "bridge %d 分钟内有人用过" % quiet, "", ""
    return "", profile, key


def _record(key: str, ok: bool, now: float, why: str = "") -> None:
    from . import bridge
    st = bridge.read_state()
    st["idle_attempt"] = {"key": key, "ok": ok, "at": int(now), **({"why": why} if why else {})}
    bridge.write_state(st)


class Lane:
    """守护进程主循环每拍调用 tick；真正的重启在后台线程里做（最多等 90 秒确认连上飞书，可能还要下载运行时）。"""

    def __init__(self, clock: Callable[[], float] = time.time, restart=None, notify=None, log=print):
        self.clock = clock
        self._restart = restart       # 测试注入；默认 bridge.restart
        self._notify = notify         # 测试注入；默认私信主用户
        self.log = log
        self.last_check = 0.0
        self.last_error = ""
        self.thread: Optional[threading.Thread] = None

    def busy(self) -> bool:
        """正在换 bridge：这期间守护进程不装 Larkyard 更新（退出会把换到一半的重启打断）。"""
        return self.thread is not None and self.thread.is_alive()

    def tick(self, hold: bool = False) -> bool:
        """返回这一拍是否开始了一次换新。"""
        now = self.clock()
        if self.busy() or now - self.last_check < CHECK_EVERY:
            return False
        self.last_check = now
        try:
            why, profile, key = due(now, hold=hold)
        except Exception as e:  # noqa: BLE001  读坏了的状态 / 服务文件：只记日志，不告警（每 5 分钟一次会刷屏）
            if repr(e) != self.last_error:
                self.last_error = repr(e)
                self.log("ticketd: bridge 空闲换新检查出错：%r" % (e,), flush=True)
            return False
        self.last_error = ""
        if why:
            return False
        self.thread = threading.Thread(target=self._run, args=(profile, key, now), name="bridge-idle", daemon=True)
        self.thread.start()
        return True

    def _run(self, profile: str, key: str, now: float) -> None:
        from . import bridge
        restart = self._restart or bridge.restart
        try:
            lines = restart(profile, if_stale=True, notify=self._notify or _notify_owner, how="idle")
        except bridge.RestartBusy:
            return  # 每天的重启任务或手动 restart 正在做：交给它
        except Exception as e:  # noqa: BLE001  restart 已经切回、记了 last_restart、私信过
            self.log("ticketd: bridge 空闲换新失败：%s" % e, flush=True)
            try:
                _record(key, False, now, str(e)[:200])
            except Exception:
                pass
            return
        try:
            _record(key, True, now)
        except Exception:
            pass
        self.log("ticketd: bridge 空闲换新：%s" % "；".join(lines), flush=True)


def _notify_owner(text: str) -> None:
    """后台线程里私信主用户：自己开一个数据库连接（sqlite 连接不能跨线程用）。"""
    from . import db, metrics
    con = db.connect()
    try:
        metrics.send_owner(con, text)
        con.commit()
    finally:
        con.close()

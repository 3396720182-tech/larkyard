from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
import traceback

from . import card, config, db, outbox
from .scheduler import Scheduler

ALERT_INTERVAL = 600.0
MAX_SLEEP = 30.0


class _ErrorThrottle:
    """按来源（tick/flush）节流告警：首次出现即告警，之后同一错误最多每 600 秒一次并附重复次数。"""

    def __init__(self, interval: float = ALERT_INTERVAL, clock=time.time):
        self.interval = interval
        self.clock = clock
        self.state = {}

    def failure(self, source: str, exc: Exception):
        """返回需要发出的告警文本；被节流时返回 None。"""
        sig = repr(exc)[:200]
        now = self.clock()
        st = self.state.get(source)
        if st is None or st["sig"] != sig:
            self.state[source] = {"sig": sig, "last": now, "count": 0, "streak": 1}
            return "%s 异常：%s" % (source, sig)
        st["streak"] += 1
        st["count"] += 1
        if now - st["last"] >= self.interval:
            count = st["count"]
            st["last"] = now
            st["count"] = 0
            return "%s 异常：%s（最近 %d 秒内重复 %d 次）" % (source, sig, int(self.interval), count)
        return None

    def success(self, source: str) -> None:
        self.state.pop(source, None)

    def failing(self) -> bool:
        return bool(self.state)

    def streak(self) -> int:
        return max((st["streak"] for st in self.state.values()), default=0)


def next_sleep(streak: int) -> float:
    if streak <= 0:
        return 1.0
    return min(1.0 * 2 ** streak, MAX_SLEEP)


def _guarded(throttle: _ErrorThrottle, source: str, fn) -> None:
    try:
        fn()
        throttle.success(source)
    except Exception as e:  # 单次出错不能拖垮整个服务
        try:
            msg = throttle.failure(source, e)
            if msg:
                outbox.alert(msg)
                traceback.print_exc()
        except Exception:
            pass


def _beat() -> None:
    try:
        hb = config.heartbeat_path()
        if hb.exists():
            os.utime(str(hb), None)
        else:
            hb.touch()
    except Exception:
        pass


def main(argv=None) -> int:
    config.db_dir().mkdir(parents=True, exist_ok=True)
    lock_file = open(config.db_dir() / "daemon.lock", "w")  # 整个运行期间保持打开
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ticketd already running", file=sys.stderr, flush=True)
        return 1
    config.logs_dir().mkdir(parents=True, exist_ok=True)
    con = db.connect()
    sched = Scheduler(con)
    sched.recover()
    stop = {"flag": False}

    def handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    throttle = _ErrorThrottle()
    print("ticketd started", flush=True)
    while not stop["flag"]:
        _guarded(throttle, "tick", sched.tick)
        _guarded(throttle, "card", lambda: card.refresh(con))  # 先发新单的状态卡，再发结果消息
        _guarded(throttle, "flush", lambda: outbox.flush(con))
        _beat()
        time.sleep(next_sleep(throttle.streak()))
    sched.shutdown()
    con.close()
    print("ticketd stopped", flush=True)
    return 0

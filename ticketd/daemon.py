from __future__ import annotations

import fcntl
import os
import signal
import sys
import threading
import time
import traceback
from typing import Optional

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


MEDIA_STUCK_AFTER = 900.0  # 一个媒体任务跑这么久还没结束就算卡死（正常最长约 9 分钟：100MB 上传 420 秒 + 查链接、封面）
LAST_RUN_KEEP = 3600.0  # 轮转记录保留多久（远长于发件箱最长 300 秒的退避）
MEDIA_STALL_AFTER = 120.0  # 调度线程这么久没转一圈就算卡死（它只做短查询和派任务，正常每秒一圈）


class MediaLane:
    """媒体通道：结果卡的图片/视频上传、稿子导入云文档和视频消息在这里做，不在主循环里。
    一次云空间上传可能要几分钟（100MB 约 420 秒超时），放在主循环里会让派单、状态卡、心跳和别的工单的投递
    全部停下；挪到这里之后主循环只发已经准备好的行（outbox.flush lane=fast），结果卡的发送仍在主循环里。

    它是一个有并发上限的任务池：调度线程每秒用 outbox.media_jobs 挑出要准备的行、认领后交给任务线程，
    每个任务用自己的数据库连接（短事务，做完就关）。
    - 最多 workers 个任务同时进行（TICKETD_MEDIA_WORKERS，默认 2），其中传视频的最多 video_workers 个
      （TICKETD_MEDIA_VIDEO_WORKERS，默认 1）：一个大视频传 7 分钟时，别的工单的图片和稿子照常准备。
    - 同一工单严格按顺序：每单只取最早一条待发消息，同一工单同时最多一个任务。
    - 轮转：最近没被处理过的行先上，一张多视频卡用完 60 秒预算后让别的行先走。
    - 卡死检测：任务跑了 stuck_after 秒还没结束就告警并让出名额（行仍被认领，不会被重复处理）；
      调度线程 MEDIA_STALL_AFTER 秒没转一圈，主循环告警。"""

    def __init__(self, interval: float = 1.0, workers: Optional[int] = None, video_workers: Optional[int] = None,
                 stuck_after: float = MEDIA_STUCK_AFTER, stall_after: float = MEDIA_STALL_AFTER, sender=None):
        self.interval = interval
        self.workers = max(1, workers if workers is not None else config.media_workers())
        self.video_workers = max(1, min(self.workers, video_workers if video_workers is not None
                                        else config.media_video_workers()))
        self.stuck_after = stuck_after
        self.stall_after = stall_after
        self.sender = sender  # 测试可替换（文字兜底的发送器）
        self.stopping = threading.Event()
        self.thread = None
        self.con = None
        self.lock = threading.Lock()
        self.tasks = {}  # 行 id -> {"thread", "ticket", "video", "started", "stuck"}
        self.last_run = {}  # 行 id -> 上次开始准备的时间（轮转用）
        self.hold_until = 0.0  # 有任务遇到网络/库被占用：这之前不派新任务
        self.beat = time.monotonic()  # 调度线程最近一次转圈的时间
        self.stall_alerted = False
        self.task_errors = _ErrorThrottle()

    def ensure(self) -> None:
        """主循环每轮调用：调度线程没起来或意外退出了就（重新）起一个；活着但很久没转圈就告警。"""
        if self.thread is not None and self.thread.is_alive():
            self._check_stall()
            return
        if self.thread is not None:
            outbox.alert("媒体通道线程意外退出，已重新启动")
        self.beat = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="ticketd-media", daemon=True)
        self.thread.start()

    def _check_stall(self) -> None:
        idle = time.monotonic() - self.beat
        if idle > self.stall_after and not self.stall_alerted:
            self.stall_alerted = True
            outbox.alert("媒体通道调度线程 %d 秒没有响应，可能卡死：新的图片/视频/稿子暂时不会准备，"
                         "正在进行的任务不受影响；一直不恢复需要重启服务" % idle)
        elif idle <= self.stall_after and self.stall_alerted:
            self.stall_alerted = False
            outbox.alert("媒体通道调度线程恢复响应")

    def busy(self) -> list:
        """正在占名额的任务（卡死后让出名额的不算）。"""
        with self.lock:
            return [dict(t, row=rid) for rid, t in self.tasks.items() if not t["stuck"]]

    def _reap(self) -> None:
        now = time.monotonic()
        with self.lock:
            stuck = [(rid, t) for rid, t in self.tasks.items()
                     if not t["stuck"] and now - t["started"] > self.stuck_after]
            for _, t in stuck:
                t["stuck"] = True
        for rid, t in stuck:
            outbox.alert("媒体任务 outbox %d（%s）已运行 %d 秒仍未结束，可能卡死：已让出并发名额，"
                         "这一行要等它结束或重启服务后才会再处理" % (rid, t["ticket"], now - t["started"]))

    def _dispatch(self) -> None:
        if self.con is None:
            self.con = db.connect()  # sqlite 连接不能跨线程用：在本线程里建
        self._reap()
        if time.monotonic() < self.hold_until:
            return
        busy = self.busy()
        free = self.workers - len(busy)
        free_video = self.video_workers - sum(1 for t in busy if t["video"])
        with self.lock:
            running = {t["ticket"] for t in self.tasks.values()}  # 含卡死的：同一工单同时最多一个任务
        if free <= 0:
            return
        jobs = outbox.media_jobs(self.con)
        with self.lock:
            keep = {r["id"] for r, _ in jobs} | set(self.tasks)
        # 正在跑或在退避中的行不在候选里，它们的轮转记录也要留着（退避最长 300 秒），否则回到队列时会插到最前面
        now = time.monotonic()
        self.last_run = {k: v for k, v in self.last_run.items() if k in keep or now - v < LAST_RUN_KEEP}
        jobs.sort(key=lambda j: (self.last_run.get(j[0]["id"], 0.0), j[0]["id"]))
        for r, video in jobs:
            if free <= 0:
                break
            if (video and free_video <= 0) or r["ticket_id"] in running or not outbox._claim(r["id"]):
                continue
            self._start(r["id"], r["ticket_id"], video)
            running.add(r["ticket_id"])
            free -= 1
            free_video -= 1 if video else 0

    def _start(self, row_id: int, ticket_id: str, video: bool) -> None:
        now = time.monotonic()
        self.last_run[row_id] = now
        try:
            th = threading.Thread(target=self._task, args=(row_id,), name="ticketd-media-%d" % row_id, daemon=True)
            with self.lock:
                self.tasks[row_id] = {"thread": th, "ticket": ticket_id, "video": video, "started": now, "stuck": False}
            th.start()
        except BaseException:
            with self.lock:
                self.tasks.pop(row_id, None)
            outbox._release(row_id)
            raise

    def _task(self, row_id: int) -> None:
        con, hold = None, False
        try:
            con = db.connect()  # 每个任务自己的连接：sqlite 连接不能跨线程用，写入都是短事务
            hold = outbox.prepare(con, row_id, sender=self.sender)
            with self.lock:
                self.task_errors.success("media-task")
        except Exception as e:  # 单个任务出错不能拖垮通道
            with self.lock:
                msg = self.task_errors.failure("media-task", e)
            hold = True
            if msg:
                outbox.alert("outbox %d %s" % (row_id, msg))
                traceback.print_exc()
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            with self.lock:
                t = self.tasks.pop(row_id, None)
                if hold:
                    self.hold_until = max(self.hold_until, time.monotonic() + self.interval)
            outbox._release(row_id)
            if t is not None and t["stuck"]:
                outbox.alert("媒体任务 outbox %d（%s）结束了，共运行 %d 秒" % (row_id, t["ticket"],
                                                                     time.monotonic() - t["started"]))

    def _run(self) -> None:
        throttle = _ErrorThrottle()
        try:
            while not self.stopping.is_set():
                self.beat = time.monotonic()
                _guarded(throttle, "media", self._dispatch)
                self.stopping.wait(self.interval if not throttle.failing() else next_sleep(throttle.streak()))
        finally:
            if self.con is not None:
                try:
                    self.con.close()
                except Exception:
                    pass
                self.con = None

    def stop(self, timeout: float = 5.0) -> None:
        """停调度，最多等 timeout 秒让正在进行的任务结束；不等传完的上传（守护线程随进程退出，没写回的进度
        下次启动重传）。"""
        self.stopping.set()
        end = time.monotonic() + timeout
        if self.thread is not None:
            self.thread.join(timeout)
        with self.lock:
            threads = [t["thread"] for t in self.tasks.values()]
        for th in threads:
            th.join(max(0.0, end - time.monotonic()))


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
    media = MediaLane()
    print("ticketd started", flush=True)
    while not stop["flag"]:
        media.ensure()
        _guarded(throttle, "tick", sched.tick)
        _guarded(throttle, "card", lambda: card.refresh(con))  # 先发新单的状态卡，再发结果消息
        # 只发已经准备好的行；要上传/导入的行先在媒体通道线程里准备，慢上传不拖住主循环
        _guarded(throttle, "flush", lambda: outbox.flush(con, lane=outbox.LANE_FAST))
        _beat()
        time.sleep(next_sleep(throttle.streak()))
    media.stop()
    sched.shutdown()
    con.close()
    print("ticketd stopped", flush=True)
    return 0

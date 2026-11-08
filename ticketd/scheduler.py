from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import card as status_card, config, core, db, executors, procs, safefs, templates, worker

RECOVERY_TEXT = ("服务重启打断了上一轮。先检查工单目录和已发出的消息，确认哪些外部动作已经完成，"
                 "再决定是否重做；不要重复发送或重复创建。")
RETRY_TEXT = "上一轮因网络或模型接口临时故障中断，请从中断处继续。"
STDERR_TAIL = 64 * 1024
ROUND_MAX_REPORTS = 20  # 一轮最多收这么多份汇报（含 progress），防止 worker 刷屏
RUN_TAIL_CHARS = 3000   # 沙箱外执行的输出交回 worker 时保留的末尾长度（完整输出在工单目录的日志里）
RUN_INTERRUPTED_TEXT = ("## 沙箱外执行结果（R%d）\n服务重启打断了这次执行，结果未知：脚本可能执行了一部分。"
                        "先检查它要做的事（推送、部署、文件）实际到了哪一步，需要的话重新用 --run 申请。")


def overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _pid_matches(pid: int, markers) -> bool:
    """markers 由执行器给出（Claude：session_id；Codex：工单目录或 thread_id），命令行里出现任何一个即可。"""
    out = procs.command_line(pid)
    return any(m and m in out for m in markers)


def _signal_group(proc_or_pid, sig: int, fallback: bool = True) -> None:
    """给 worker 的整个进程组发信号（worker 以 start_new_session 启动，组号即 pid）；失败时退回只发给该 pid。
    组长已退出时必须传 fallback=False：它的 pid 可能已被复用，只有 killpg 是安全的
    （组里还有成员时组号不会被复用；没有成员时 killpg 报 ProcessLookupError，忽略即可）。"""
    pid = getattr(proc_or_pid, "pid", proc_or_pid)
    try:
        os.killpg(pid, sig)
        return
    except (ProcessLookupError, PermissionError):
        pass
    if not fallback:
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _short_join(descs) -> str:
    descs = list(descs)
    text = "、".join(descs[:2])
    if len(descs) > 2:
        text += " 等 %d 个" % len(descs)
    return templates.short(text, 80)


def _spool_of(workdir: str) -> str:
    return str(Path(workdir) / ".reports")  # 与 worker.worker_env 设置的 TICKETD_SPOOL 完全一致


def _sweep_orphans(spool: str, sig: int) -> int:
    """真实 Bash 工具让每条命令自成进程组，`cmd &` 会被 PID 1 收养而逃过 killpg；
    但它们继承了 worker 环境，按 TICKETD_SPOOL 标记找出来补发信号。
    macOS 15 起 ps -E 看不到系统自带程序（sh、sleep…）的环境变量，所以再按目录补一道：
    已被收养（macOS：launchd；Linux：PID 1 或 ticketd 的祖先，如 systemd --user）、没有控制终端、当前目录在工单目录内的进程也算残留
    （有终端的是用户自己的 shell / claude --resume，父进程还在的有正常的父进程，都不碰）。
    进程怎么查见 procs.py。永不抛异常。"""
    try:
        targets, orphans = procs.scan(spool)
        if orphans:
            try:
                targets |= procs.cwd_within(orphans, os.path.dirname(spool))
            except Exception:
                pass
        n = 0
        for pid in targets:
            try:
                os.kill(pid, sig)
                n += 1
            except (ProcessLookupError, PermissionError):
                pass
        return n
    except Exception:
        return 0


def _wait_exited(pid: int, timeout: float = 3.0) -> None:
    """等 pid 退出或变成僵尸：只有这之后它的脱组子进程才会被 launchd / init 收养，按目录清理才认得出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if procs.is_gone(pid):
                return
        except Exception:
            return
        time.sleep(0.05)


def _read_from(path: Path, offset: int) -> str:
    try:
        fd = safefs.open_read(path)
    except OSError:
        return ""
    if fd is None:
        return ""
    try:
        size = os.fstat(fd).st_size
        return safefs.read_at(fd, max(offset, size - STDERR_TAIL), STDERR_TAIL).decode("utf-8", "replace")
    finally:
        os.close(fd)


class Running:
    def __init__(self, proc, ticket_id: str, workdir: str, input_ids: List[int], report_mark: int,
                 stream_offset: int, stderr_offset: int, executor: str = "claude", session_id: str = ""):
        self.proc = proc
        self.normalize = executors.get(executor).normalizer()
        self.session_id = session_id
        self.ticket_id = ticket_id
        self.workdir = workdir
        self.input_ids = input_ids
        self.report_mark = report_mark
        self.stderr_offset = stderr_offset
        self.tail = worker.StreamTail(Path(workdir) / ".stream.jsonl", stream_offset)
        self.started = time.monotonic()
        self.term_at: Optional[float] = None
        self.timed_out = False
        self.last_text = ""
        self.result: Optional[dict] = None
        # 状态卡用：ticketd 观测到的最后动作（工具调用/结果），和 worker 是否已交终态汇报
        self.last_action_at: Optional[float] = None
        self.last_step: Optional[str] = None
        self.activity_dirty = False
        self.action_moved = False
        self.reported = False
        self.idle_since: Optional[float] = None  # 最近一个事件是 result（本轮已结束、进程还挂着）的起始时刻
        self.bg: Dict[str, str] = {}             # 还没结束的后台任务：task_id → 描述
        self.bg_shown: Optional[str] = None      # 已写进 tickets.bg_wait 的内容
        self.api_alerted = False                 # 本轮的模型接口报错已推过提醒


class RunProc:
    """一次正在进行的沙箱外执行（用户批准后由守护进程运行 worker 申请时存下的脚本快照）。"""

    def __init__(self, proc, run_id: int, ticket_id: str, workdir: str, log_path: Path, script_path: Path):
        self.proc = proc
        self.run_id = run_id
        self.ticket_id = ticket_id
        self.workdir = workdir
        self.log_path = log_path
        self.script_path = script_path
        self.started = time.monotonic()
        self.term_at: Optional[float] = None
        self.why: Optional[str] = None


def _bash() -> str:
    return "/bin/bash" if os.path.exists("/bin/bash") else "bash"


class Scheduler:
    def __init__(self, con):
        self.con = con
        self.running: Dict[str, Running] = {}
        self.runs: Dict[str, RunProc] = {}

    # ---------- 日志 ----------
    def log(self, workdir: str, line: str) -> None:
        # 工单目录对 worker 可写：不跟随链接、不阻塞；写不进去就丢掉这行，日志永不抛异常
        try:
            Path(workdir).mkdir(parents=True, exist_ok=True)
            fd = safefs.open_append(Path(workdir) / "过程.log")
            try:
                os.write(fd, ("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), line)).encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass

    # ---------- 生命周期 ----------
    def recover(self) -> None:
        for run in self.con.execute("SELECT * FROM runs WHERE status='running'").fetchall():
            if run["pid"] and _pid_matches(run["pid"], ["R%d.sh" % run["id"]]):
                _signal_group(run["pid"], signal.SIGKILL)
            with db.tx(self.con):
                self.con.execute("UPDATE runs SET status='interrupted', finished_at=? WHERE id=?",
                                 (db.now(), run["id"]))
                core.add_input(self.con, run["ticket_id"], "run_result", RUN_INTERRUPTED_TEXT % run["id"])
        for t in self.con.execute("SELECT * FROM tickets WHERE state='running'").fetchall():
            if t["pid"] and _pid_matches(t["pid"], executors.get(worker.executor_of(t)).pid_markers(t)):
                _signal_group(t["pid"], signal.SIGKILL)
                _wait_exited(t["pid"])
            _sweep_orphans(_spool_of(t["workdir"]), signal.SIGKILL)
            core.ingest_spool(self.con, t["id"], str(Path(t["workdir"]) / ".reports"))
            with db.tx(self.con):
                core.touch(self.con, t["id"], state="idle", pid=None, bg_wait=None)
                core.add_input(self.con, t["id"], "recovery", RECOVERY_TEXT)
            self.log(t["workdir"], "服务重启，已安排恢复")
        self.con.execute("DELETE FROM locks")

    def shutdown(self, wait: float = 10.0) -> None:
        for rp in self.runs.values():  # 库里仍记为 running，下次启动时按「被打断」交代给 worker
            if rp.proc.poll() is None:
                _signal_group(rp.proc, signal.SIGTERM)
        self.runs.clear()
        for r in self.running.values():
            if r.proc.poll() is None:
                _signal_group(r.proc, signal.SIGTERM)
        end = time.monotonic() + wait
        for r in self.running.values():
            try:
                r.proc.wait(timeout=max(0.1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal_group(r.proc, signal.SIGKILL)
        self.running.clear()

    def tick(self) -> None:
        for tid in list(self.running):
            try:
                self._watch(tid)
            except Exception as e:  # 一张单出错不能耽误其他单和启动
                r = self.running.get(tid)
                if r is not None:
                    self.log(r.workdir, "监视出错：%r" % e)
        for tid in list(self.runs):
            try:
                self._watch_run(tid)
            except Exception as e:
                rp = self.runs.get(tid)
                if rp is not None:
                    self.log(rp.workdir, "监视沙箱外执行出错：%r" % e)
        try:
            self._launch_runs()
        except Exception:
            pass  # 单个申请出错已在 _start_run 里交代；这里只保证不耽误派单
        self._launch()

    # ---------- 启动 ----------
    def _launch(self) -> None:
        rows = self.con.execute(
            "SELECT * FROM tickets t WHERE t.state IN ('queued','idle','failed') AND t.retry_at<=?"
            " AND EXISTS(SELECT 1 FROM inputs i WHERE i.ticket_id=t.id AND i.status='pending')"
            " ORDER BY t.created_at", (db.now(),)).fetchall()
        for t in rows:
            if len(self.running) >= config.max_workers():
                break
            if t["id"] not in self.running:
                try:
                    self.start(t)
                except Exception as e:
                    self.log(t["workdir"], "启动出错：%r" % e)

    def start(self, t) -> bool:
        tid = t["id"]
        gp = core.grants(self.con, tid)
        workdir = Path(t["workdir"])
        with db.tx(self.con):
            if core.get(self.con, tid)["state"] not in ("queued", "idle", "failed"):
                return False  # 读取候选行之后用户可能已停止/暂停
            held = self.con.execute("SELECT path, ticket_id FROM locks WHERE ticket_id<>?", (tid,)).fetchall()
            if any(overlaps(g, h["path"]) for g in gp for h in held):
                return False
            for g in gp:
                self.con.execute("INSERT OR REPLACE INTO locks VALUES(?,?)", (g, tid))
            inputs = self.con.execute("SELECT * FROM inputs WHERE ticket_id=? AND status='pending' ORDER BY id",
                                      (tid,)).fetchall()
            ids = [i["id"] for i in inputs]
            self.con.executemany("UPDATE inputs SET status='delivered' WHERE id=?", [(i,) for i in ids])
            core.expire_pending_runs(self.con, tid)
            mark = self.con.execute("SELECT COALESCE(MAX(id),0) FROM reports WHERE ticket_id=?", (tid,)).fetchone()[0]
            core.touch(self.con, tid, state="running")
        fds: List[int] = []
        try:
            # 工单目录对 worker 可写而守护进程不在沙箱里：这里的文件一律经 safefs 打开（不跟随链接、不阻塞）
            workdir.mkdir(parents=True, exist_ok=True)
            card = worker.compose_card(t, gp, inputs, include_assignment=not t["session_started"])
            n = len(list(workdir.glob(".input-*.md"))) + 1
            card_path = workdir / (".input-%03d-%d.md" % (n, time.time_ns()))
            safefs.create_excl(card_path, card.encode("utf-8"))
            ex = executors.get(worker.executor_of(t))
            cmd = ex.build_command(t, gp, executors.prompt_text(ex.name), net=core.net_grants(self.con, tid))
            env = worker.worker_env(t)
            fin = safefs.open_read(card_path)
            if fin is None:
                raise OSError("任务卡不存在：%s" % card_path)
            fds.append(fin)
            fout = safefs.open_append(workdir / ".stream.jsonl")
            fds.append(fout)
            ferr = safefs.open_append(workdir / ".stderr.log")
            fds.append(ferr)
            so, eo = os.fstat(fout).st_size, os.fstat(ferr).st_size
            proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=fin, stdout=fout, stderr=ferr, env=env,
                                    start_new_session=True)
        except Exception as e:
            self.con.execute("DELETE FROM locks WHERE ticket_id=?", (tid,))
            core.touch(self.con, tid, started_at=db.now())  # 这一轮从这里算起：结果卡的用时、阶段不串到上一轮
            core.add_report(self.con, tid, "failed", "无法启动 worker：%s" % e)
            core.touch_if(self.con, tid, ("running",), state="failed", pid=None)
            self.log(str(workdir), "无法启动 worker：%r" % e)
            return False
        finally:
            for fd in fds:
                os.close(fd)
        # 不写 state：可能已被停止/暂停
        t0 = db.now()
        core.touch(self.con, tid, pid=proc.pid, started_at=t0, last_action_at=t0, last_step=None, stall_alerted=0,
                   bg_wait=None)
        self.running[tid] = Running(proc, tid, str(workdir), ids, mark, so, eo, ex.name, t["session_id"])
        self.log(str(workdir), "开始第 %d 轮（输入 %d 条）" % (n, len(inputs)))
        return True

    # ---------- 监视与收割 ----------
    def _on_event(self, r: Running, ev: dict) -> None:
        for line in worker.summarize_event(ev):
            self.log(r.workdir, line)
        step = worker.last_step(ev)
        if step:
            r.last_step = step
            r.activity_dirty = True
        bg = worker.bg_task_event(ev)
        if bg is not None:
            if bg[0] == "start":
                r.bg[bg[1]] = bg[2]
            else:
                r.bg.pop(bg[1], None)
        kind, sub = ev.get("type"), ev.get("subtype")
        if kind == "result":
            r.idle_since = time.monotonic()
        elif kind in ("assistant", "user") or (kind == "system" and sub == "init"):
            r.idle_since = None
        if worker.is_action(ev):
            r.last_action_at = db.now()
            r.activity_dirty = r.action_moved = True
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid and sid != r.session_id:
                # Codex 不能预先指定会话 ID：首轮从 thread.started 拿到真实 ID 再回写
                r.session_id = sid
                core.touch(self.con, r.ticket_id, session_started=1, session_id=sid)
            else:
                core.touch(self.con, r.ticket_id, session_started=1)
        elif ev.get("type") == "assistant":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                content = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                text = c.get("text")
                if c.get("type") == "text" and isinstance(text, str) and text.strip():
                    r.last_text = text
        elif ev.get("type") == "result":
            r.result = ev

    def _terminate(self, r: Running, why: str) -> None:
        _signal_group(r.proc, signal.SIGTERM)
        r.term_at = time.monotonic()
        self.log(r.workdir, "中止进程：%s" % why)

    def _handle_events(self, r: Running) -> None:
        for raw in r.tail.read_events():
            try:
                for ev in r.normalize(raw):  # 非 Claude 执行器的事件先翻译成 Claude 形状
                    self._on_event(r, ev)
            except Exception as e:  # 单个坏事件不能打断整个 tick
                self.log(r.workdir, "事件处理出错：%r" % e)

    def _flush_activity(self, r: Running) -> None:
        if not r.activity_dirty:
            return
        r.activity_dirty = False
        if r.action_moved:  # 只有真的有了新动作才解除「已提醒卡住」，纯文字输出不算
            r.action_moved = False
            self.con.execute("UPDATE tickets SET last_action_at=?, last_step=?, stall_alerted=0 WHERE id=?",
                             (r.last_action_at, r.last_step, r.ticket_id))
        else:
            self.con.execute("UPDATE tickets SET last_step=? WHERE id=?", (r.last_step, r.ticket_id))

    def _ingest_round(self, r: Running, workdir: str) -> None:
        got = self.con.execute("SELECT COUNT(*) FROM reports WHERE ticket_id=? AND id>?",
                               (r.ticket_id, r.report_mark)).fetchone()[0]
        core.ingest_spool(self.con, r.ticket_id, _spool_of(workdir), max(0, ROUND_MAX_REPORTS - got))

    def _while_running(self, r: Running, t) -> None:
        """进程运行中：随时收汇报（结果不必等进程退出才发出），检查卡死和「已汇报但进程不退」。"""
        self._ingest_round(r, t["workdir"])
        if not r.reported and self.con.execute(
                "SELECT 1 FROM reports WHERE ticket_id=? AND id>? AND status<>'progress' LIMIT 1",
                (r.ticket_id, r.report_mark)).fetchone():
            r.reported = True
            self.log(r.workdir, "已收到终态汇报")
        # 本轮已结束（最后一个事件是 result）、没交终态汇报、还有后台任务没结束：claude 在等后台任务的通知，
        # 任务结束时会唤醒模型接着做。这不是卡住（T0005：被误报卡住后用户停单，后台出图一起被杀）
        waiting = not r.reported and r.idle_since is not None and bool(r.bg)
        self._show_bg_wait(r, waiting)
        if r.reported:
            if (r.term_at is None and r.idle_since is not None
                    and time.monotonic() - r.idle_since >= config.linger_grace()):
                self._terminate(r, "已汇报且本轮已结束，结束仍在等待的后台任务")
            return
        if waiting:
            err = worker.result_error_text(r.result)
            if err and not r.api_alerted:
                # 模型接口报错（如余额不足）而后台任务还在跑：进程要等任务结束才会退出，马上告诉用户，
                # 赶在后台任务结束、模型被唤醒之前处理好，这一轮就能接着做
                r.api_alerted = True
                core.enqueue_message(self.con, r.ticket_id, templates.api_error_message(
                    r.ticket_id, err, worker.account_error(err), r.bg_shown))
                self.log(r.workdir, "模型接口报错，后台任务还在跑，已提醒用户")
            return  # 单轮超时照旧兜底
        quiet = db.now() - max(t["last_action_at"] or 0, t["started_at"] or 0)
        if quiet >= config.stall_seconds() and not t["stall_alerted"]:
            # 本轮有状态卡：卡片自己会标「疑似卡住」，不另推消息（不需要用户回答；真卡死到超时会报 blocked 提醒）
            on_card = status_card.live_card(t, db.now()) is not None and not t["card_closed"]
            with db.tx(self.con):
                if not on_card:
                    core.enqueue_message(self.con, r.ticket_id,
                                         templates.stall_message(r.ticket_id, int(quiet // 60), t["last_step"]))
                self.con.execute("UPDATE tickets SET stall_alerted=1 WHERE id=?", (r.ticket_id,))
            self.log(r.workdir, "疑似卡住：%d 秒没有新动作，%s" % (quiet, "状态卡已标出" if on_card else "已提醒用户"))

    # ---------- 沙箱外执行（只跑用户批准过的脚本快照） ----------
    def _launch_runs(self) -> None:
        rows = self.con.execute(
            "SELECT r.* FROM runs r JOIN tickets t ON t.id=r.ticket_id"
            " WHERE r.status='approved' AND t.state IN ('idle','failed') ORDER BY r.id").fetchall()
        for run in rows:
            tid = run["ticket_id"]
            if tid not in self.running and tid not in self.runs:
                self._start_run(run)

    def _start_run(self, run) -> None:
        tid, rid = run["ticket_id"], run["id"]
        now = db.now()
        with db.tx(self.con):
            t = core.get(self.con, tid)
            cur = self.con.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
            if t["state"] not in ("idle", "failed") or cur is None or cur["status"] != "approved":
                return
            self.con.execute("UPDATE runs SET status='running', started_at=? WHERE id=?", (now, rid))
            # 不改 started_at：这不是新的一轮，状态卡保持上一轮的结果；state=running 挡住同时起 worker
            core.touch(self.con, tid, state="running", last_step="沙箱外执行 R%d" % rid, last_action_at=now)
        workdir = t["workdir"]
        log_path = Path(workdir) / ("沙箱外执行-R%d.log" % rid)
        fout = None
        try:
            d = config.runs_dir()
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(str(d), 0o700)
            script_path = d / ("R%d.sh" % rid)
            fd = os.open(str(script_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, run["script"].encode("utf-8"))
            finally:
                os.close(fd)
            fout = safefs.open_append(log_path)  # 工单目录对 worker 可写：不跟随链接
            env = worker.user_env(dict(os.environ, TICKETD_TICKET=tid, TICKETD_RUN="R%d" % rid))
            env.pop("TICKETD_SPOOL", None)
            proc = subprocess.Popen([_bash(), str(script_path)], cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=fout, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        except Exception as e:
            with db.tx(self.con):
                self.con.execute("UPDATE runs SET status='done', exit_code=-1, finished_at=? WHERE id=?",
                                 (db.now(), rid))
                core.add_input(self.con, tid, "run_result",
                               "## 沙箱外执行结果（R%d）\n没能启动：%s。脚本没有执行。" % (rid, e))
                core.touch_if(self.con, tid, ("running",), state="idle")
            self.log(workdir, "沙箱外执行 R%d 没能启动：%r" % (rid, e))
            return
        finally:
            if fout is not None:
                os.close(fout)
        self.con.execute("UPDATE runs SET pid=? WHERE id=?", (proc.pid, rid))
        self.runs[tid] = RunProc(proc, rid, tid, workdir, log_path, script_path)
        self.log(workdir, "沙箱外执行 R%d 开始（用户已批准）：%s" % (rid, run["path"]))

    def _watch_run(self, tid: str) -> None:
        rp = self.runs[tid]
        if rp.proc.poll() is None:
            state = core.get(self.con, tid)["state"]
            if rp.term_at is None and state in ("stopped", "held"):
                rp.why = "被用户%s中止" % ("停止" if state == "stopped" else "暂停")
            elif rp.term_at is None and time.monotonic() - rp.started > config.run_timeout():
                rp.why = "运行超过 %d 分钟被中止" % int(config.run_timeout() // 60)
            if rp.why and rp.term_at is None:
                _signal_group(rp.proc, signal.SIGTERM)
                rp.term_at = time.monotonic()
                self.log(rp.workdir, "沙箱外执行 R%d %s" % (rp.run_id, rp.why))
            elif rp.term_at is not None and time.monotonic() - rp.term_at > 10:
                _signal_group(rp.proc, signal.SIGKILL)
            return
        del self.runs[tid]
        _signal_group(rp.proc.pid, signal.SIGTERM, fallback=False)  # 脚本留在进程组里的后台进程
        code = rp.proc.returncode
        took = time.monotonic() - rp.started
        tail = _read_from(rp.log_path, 0)
        if len(tail) > RUN_TAIL_CHARS:
            tail = "…" + tail[-RUN_TAIL_CHARS:]
        lines = ["## 沙箱外执行结果（R%d，用户批准后由 ticketd 在沙箱外执行）" % rp.run_id,
                 "退出码：%s，用时 %s%s" % (code, templates.dur(took), "（%s）" % rp.why if rp.why else ""),
                 "完整输出：%s" % rp.log_path,
                 "输出末尾：", "```", tail.rstrip() or "（没有输出）", "```",
                 "根据结果接着做。还需要在沙箱外做别的事，就再写脚本用 --run 申请。"]
        with db.tx(self.con):
            self.con.execute("UPDATE runs SET status='done', exit_code=?, finished_at=? WHERE id=?",
                             (code, db.now(), rp.run_id))
            core.add_input(self.con, tid, "run_result", "\n".join(lines))
            core.touch_if(self.con, tid, ("running",), state="idle", last_action_at=db.now())
        try:
            rp.script_path.unlink()
        except OSError:
            pass
        self.log(rp.workdir, "沙箱外执行 R%d 结束：退出码 %s，用时 %.0fs" % (rp.run_id, code, took))

    def _schedule_retry(self, r: Running, t, delay: float, **fields) -> None:
        tid = r.ticket_id
        with db.tx(self.con):
            if t["session_started"]:
                core.add_input(self.con, tid, "retry", RETRY_TEXT)
            else:
                self.con.executemany("UPDATE inputs SET status='pending' WHERE id=?", [(i,) for i in r.input_ids])
            core.touch_if(self.con, tid, ("running",), state="idle", pid=None, retries=t["retries"] + 1,
                          retry_at=db.now() + delay, **fields)

    def _outage(self, r: Running, t, reason: str, detail: str, account: bool) -> None:
        tid = r.ticket_id
        now = db.now()
        since = t["outage_since"] or now
        limit = config.outage_retry_hours() * 3600
        if now - since < limit:
            every = config.outage_retry_every()
            if t["outage_since"] is None:  # 只在进入故障等待时告诉用户一次，之后的自动重试不再打扰
                until = time.strftime("%m-%d %H:%M", time.localtime(since + limit))
                plan = "每 %d 分钟自动试一次，最晚到 %s" % (max(1, int(every // 60)), until)
                if account:
                    core.add_report(self.con, tid, "waiting", "%s，这一轮没做完：%s" % (reason, detail),
                                    ask="处理好之后会自动接着做（%s）；也可以引用这条消息回复「继续」马上重试" % plan)
                else:
                    core.add_report(self.con, tid, "waiting",
                                    "%s，这一轮没做完，恢复后会自动接着做（%s）：%s" % (reason, plan, detail))
            self._schedule_retry(r, t, every, outage_since=since)
            self.log(r.workdir, "%s，故障等待中，%.0fs 后自动重试" % (reason, every))
            return
        if account:
            core.add_report(self.con, tid, "failed", "%s，这一轮没做完：%s" % (reason, detail),
                            ask="处理好之后引用这条消息回复「继续」，我从中断处接着做")
        else:
            core.add_report(self.con, tid, "failed",
                            "%s，自动重试 %.0f 小时仍未恢复：%s" % (reason, limit / 3600, detail),
                            ask="恢复后引用这条消息回复「继续」，我从中断处接着做")
        core.touch_if(self.con, tid, ("running",), state="failed", pid=None, outage_since=None)
        self.log(r.workdir, "%s，不再自动重试，本轮失败" % reason)

    def _show_bg_wait(self, r: Running, waiting: bool) -> None:
        text = _short_join(r.bg.values()) if waiting else None
        if text != r.bg_shown:
            r.bg_shown = text
            self.con.execute("UPDATE tickets SET bg_wait=? WHERE id=?", (text, r.ticket_id))
            if text:
                self.log(r.workdir, "本轮已结束，等后台任务：%s" % text)

    def _watch(self, tid: str) -> None:
        r = self.running[tid]
        self._handle_events(r)
        self._flush_activity(r)
        if r.proc.poll() is None:
            t = core.get(self.con, tid)
            state = t["state"]
            if state == "running":
                self._while_running(r, t)
            if r.term_at is None and state in ("stopped", "held"):
                self._terminate(r, "用户停止" if state == "stopped" else "用户暂停")
            elif r.term_at is None and time.monotonic() - r.started > config.turn_timeout():
                r.timed_out = True
                self._terminate(r, "超时")
            elif r.term_at is not None and time.monotonic() - r.term_at > 10:
                _signal_group(r.proc, signal.SIGKILL)
            return
        self._handle_events(r)
        self._flush_activity(r)
        self._reap(r)

    def _reap(self, r: Running) -> None:
        tid = r.ticket_id
        del self.running[tid]
        _signal_group(r.proc.pid, signal.SIGTERM, fallback=False)  # 清理 worker 留下的后台子进程
        try:
            swept = _sweep_orphans(_spool_of(core.get(self.con, tid)["workdir"]), signal.SIGTERM)
        except Exception:
            swept = 0
        if swept:
            self.log(r.workdir, "清理残留进程 %d 个" % swept)
        try:
            self._settle(r)
        except Exception as e:  # 收割出错也不能让工单卡在 running、锁不释放
            self.con.execute("DELETE FROM locks WHERE ticket_id=?", (tid,))
            core.touch_if(self.con, tid, ("running",), state="failed", pid=None)
            self.log(r.workdir, "收割出错：%r" % e)
            try:
                core.add_report(self.con, tid, "failed", "ticketd 内部错误：%r" % e)
            except Exception:
                pass

    def _settle(self, r: Running) -> None:
        tid = r.ticket_id
        self.con.execute("DELETE FROM locks WHERE ticket_id=?", (tid,))
        core.touch(self.con, tid, pid=None, bg_wait=None)  # 进程已退出；下面的状态迁移即使因停止/暂停而落空，pid 也要清掉
        t = core.get(self.con, tid)
        if t["state"] != "stopped":
            self._ingest_round(r, t["workdir"])
        code = r.proc.returncode
        self.log(r.workdir, "进程退出 code=%s" % code)
        if t["state"] in ("stopped", "held"):
            core.touch(self.con, tid, pid=None)
            return
        if r.timed_out:
            core.add_report(self.con, tid, "blocked",
                            "运行超过 %d 分钟已中止" % int(config.turn_timeout() // 60),
                            ask="是否继续？引用这条消息回复即可")
            core.touch_if(self.con, tid, ("running",), state="idle", pid=None)
            return
        reports = self.con.execute("SELECT * FROM reports WHERE ticket_id=? AND id>? AND status<>'progress'"
                                   " ORDER BY id", (tid, r.report_mark)).fetchall()
        if reports:
            core.touch_if(self.con, tid, ("running",),
                          state="failed" if reports[-1]["status"] == "failed" else "idle",
                          pid=None, retries=0, retry_at=0, outage_since=None)
            return
        stderr_text = _read_from(Path(r.workdir) / ".stderr.log", r.stderr_offset)
        err = worker.result_error_text(r.result)
        acct = worker.account_error(err, r.last_text, stderr_text)
        transient = not acct and worker.is_transient(code, r.result, stderr_text)
        delays = config.retry_delays()
        if transient and t["retries"] < len(delays):
            self._schedule_retry(r, t, delays[t["retries"]])
            self.log(r.workdir, "临时故障，%.0fs 后第 %d 次重试" % (delays[t["retries"]], t["retries"] + 1))
            return
        if acct or transient:
            # 余额不足、认证失败（重试不会马上好），或短重试用完还没恢复的网络/接口故障：进入故障等待，
            # 隔一段时间自动续跑，用户处理好或网络恢复后不用再说什么（原来直接记失败，要人发现后回「继续」，
            # 另一台机器上隔了 3 小时和 6.5 小时才被发现，issue #10）
            self._outage(r, t, "模型接口%s" % acct if acct else "网络或模型接口故障",
                         worker._short(err or r.last_text or stderr_text, 200), bool(acct))
            return
        summary = "worker 未交汇报就退出（code=%s）" % code
        if r.last_text:
            summary += "：" + worker._short(r.last_text, 200)
        ask = None
        if worker.is_client_error(err):
            # 不可恢复的 4xx（如网关不支持 PDF 类型的 tool_result）已经写进会话历史，续做多半还是同一个错误（issue #14）
            ask = "追问后还是同样的错误，就说「换新会话重来 %s」（工单目录和历次追问会带上）" % tid
        core.add_report(self.con, tid, "failed", summary, ask=ask)
        core.touch_if(self.con, tid, ("running",), state="failed", pid=None)

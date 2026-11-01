from __future__ import annotations

import json
import os

from helpers import TicketdTestCase


class SchedulerTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)
        self._child_markers = []

    def tearDown(self):
        self.sched.shutdown()
        for m in self._child_markers:  # 测试失败时也要清掉 fake 留下的子进程
            try:
                os.kill(int(open(m).read()), 9)
            except (OSError, ValueError):
                pass
        super().tearDown()

    def child_marker(self, tid):
        m = os.path.join(self.core.get(self.con, tid)["workdir"], ".child_pid")
        self._child_markers.append(m)
        return m

    def state(self, tid):
        return self.core.get(self.con, tid)["state"]

    def argv_log(self, tid):
        p = os.path.join(self.core.get(self.con, tid)["workdir"], ".fake_argv.jsonl")
        with open(p, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def test_ready_flow(self):
        tid = self.new_ticket()
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        t = self.core.get(self.con, tid)
        self.assertEqual(t["session_started"], 1)
        self.assertIn("--session-id", self.argv_log(tid)[0]["argv"])
        log = open(os.path.join(t["workdir"], "过程.log"), encoding="utf-8").read()
        self.assertIn("会话就绪", log)
        self.assertIsNone(self.con.execute("SELECT * FROM locks").fetchone())

    def test_followup_resumes_session(self):
        tid = self.new_ticket()
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.con.execute("SELECT * FROM reports").fetchall()) == 2, self.sched)
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        second = self.argv_log(tid)[1]
        self.assertIn("--resume", second["argv"])
        self.assertIn("用户追问（消息 om_2）", second["card"])
        self.assertNotIn("# 工单", second["card"])
        last = self.con.execute("SELECT * FROM outbox ORDER BY id DESC").fetchone()
        self.assertEqual(last["reply_to"], "om_2")

    def test_concurrency_limit(self):
        ids = [self.new_ticket(request="[[fake:sleep]]", message="om_%d" % i) for i in range(4)]
        self.sched.tick()
        states = [self.state(t) for t in ids]
        self.assertEqual(states.count("running"), 3)
        self.assertEqual(states.count("queued"), 1)

    def test_project_lock(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        a = self.new_ticket(request="[[fake:sleep]]", message="om_a", grant_paths=[str(proj)])
        b = self.new_ticket(request="[[fake:ready]]", message="om_b", grant_paths=[str(proj)])
        self.sched.tick()
        self.assertEqual((self.state(a), self.state(b)), ("running", "queued"))
        self.core.stop(self.con, a)
        self.wait(lambda: self.state(b) == "idle", self.sched)

    def test_noreport_marks_failed(self):
        tid = self.new_ticket(request="[[fake:noreport]]")
        self.wait(lambda: self.state(tid) == "failed", self.sched)
        rep = self.core.last_report(self.con, tid)
        self.assertEqual(rep["status"], "failed")
        self.assertIn("我忘了汇报", rep["summary"])

    def test_transient_retry_then_ready(self):
        tid = self.new_ticket(request="[[fake:transient_once]]")
        self.wait(lambda: self.state(tid) == "idle" and self.core.last_report(self.con, tid) is not None,
                  self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["summary"], "重试后完成")
        self.assertEqual(len(self.argv_log(tid)), 2)

    def test_transient_gives_up(self):
        tid = self.new_ticket(request="[[fake:transient]]")
        self.wait(lambda: self.state(tid) == "failed", self.sched)
        self.assertEqual(len(self.argv_log(tid)), 4)  # 首次 + 3 次重试

    def test_timeout_reports_blocked(self):
        os.environ["TICKETD_TURN_TIMEOUT"] = "1"
        tid = self.new_ticket(request="[[fake:sleep]]")
        self.wait(lambda: self.core.last_report(self.con, tid) is not None, self.sched)
        rep = self.core.last_report(self.con, tid)
        self.assertEqual(rep["status"], "blocked")
        self.assertIn("已中止", rep["summary"])
        self.wait(lambda: self.state(tid) == "idle", self.sched)

    def test_stop_kills_running_worker(self):
        tid = self.new_ticket(request="[[fake:sleep]]")
        self.sched.tick()
        self.assertEqual(self.state(tid), "running")
        self.core.stop(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)
        self.assertEqual(self.state(tid), "stopped")
        self.assertIsNone(self.core.last_report(self.con, tid))

    def test_hold_queues_until_release(self):
        tid = self.new_ticket(request="[[fake:sleep]]")
        self.sched.tick()
        self.core.hold(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)
        self.core.send_followup(self.con, tid, "om_2", "换个做法 [[fake:ready]]")
        for _ in range(5):
            self.sched.tick()
        self.assertEqual(self.state(tid), "held")
        self.core.release(self.con, tid)
        self.wait(lambda: self.core.last_report(self.con, tid) is not None, self.sched)
        card = self.argv_log(tid)[-1]["card"]
        self.assertIn("用户已放开暂停", card)
        self.assertIn("换个做法", card)

    def test_recover_running_ticket(self):
        tid = self.new_ticket()
        self.core.touch(self.con, tid, state="running", pid=None)
        self.sched.recover()
        self.assertEqual(self.state(tid), "idle")
        self.wait(lambda: self.core.last_report(self.con, tid) is not None, self.sched)
        card = self.argv_log(tid)[0]["card"]
        self.assertIn("服务重启打断了上一轮", card)
        self.assertIn("# 工单 T0001", card)  # 会话从未建立过，所以要带上原始任务

    # ---------- 偏差 D2：停止/暂停不能被调度器覆盖 ----------
    def test_stop_during_launch_is_not_overwritten(self):
        import subprocess
        from unittest import mock
        from ticketd import db, scheduler
        tid = self.new_ticket(request="[[fake:sleep]]")
        real_popen = subprocess.Popen
        con2 = db.connect()
        fired = []

        def racing_popen(*a, **kw):
            if not fired:
                fired.append(1)
                self.core.stop(con2, tid)  # 用户在 Popen 前一刻停止
            return real_popen(*a, **kw)

        try:
            with mock.patch.object(scheduler.subprocess, "Popen", racing_popen):
                self.sched.tick()
        finally:
            con2.close()
        self.assertEqual(self.state(tid), "stopped")
        proc = self.sched.running[tid].proc
        self.wait(lambda: tid not in self.sched.running, self.sched)
        self.assertEqual(self.state(tid), "stopped")
        self.assertIsNotNone(proc.returncode)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIsNone(self.core.last_report(self.con, tid))

    def test_stop_during_reap_is_not_overwritten(self):
        from unittest import mock
        from ticketd import db, scheduler
        tid = self.new_ticket()
        self.sched.tick()
        self.assertEqual(self.state(tid), "running")
        self.sched.running[tid].proc.wait(timeout=20)
        real_ingest = self.core.ingest_spool
        con2 = db.connect()

        def racing_ingest(*a, **kw):
            self.core.stop(con2, tid)  # 用户在收割途中停止
            return real_ingest(*a, **kw)

        try:
            with mock.patch.object(scheduler.core, "ingest_spool", racing_ingest):
                self.wait(lambda: tid not in self.sched.running, self.sched)
        finally:
            con2.close()
        self.assertEqual(self.state(tid), "stopped")
        self.assertIsNone(self.core.get(self.con, tid)["pid"])

    # ---------- 偏差 D3：单个坏事件不能打断 tick ----------
    def test_odd_assistant_event_does_not_raise(self):
        from ticketd.scheduler import Running
        tid = self.new_ticket()
        r = Running(None, tid, self.core.get(self.con, tid)["workdir"], [], 0, 0, 0)
        self.sched._on_event(r, {"type": "assistant", "message": {"content": "str"}})
        self.sched._on_event(r, {"type": "assistant", "message": "str"})
        self.sched._on_event(r, {"type": "assistant", "message": {"content": ["x", 1, None]}})

    def test_event_handler_error_is_logged_and_skipped(self):
        from unittest import mock
        tid = self.new_ticket()
        real = self.sched._on_event

        def flaky(r, ev):
            if ev.get("type") == "system":
                raise RuntimeError("boom")
            return real(r, ev)

        with mock.patch.object(self.sched, "_on_event", flaky):
            self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        log = open(os.path.join(self.core.get(self.con, tid)["workdir"], "过程.log"), encoding="utf-8").read()
        self.assertIn("事件处理出错：RuntimeError('boom')", log)

    # ---------- 评审修复 F1：异常边界 ----------
    def test_start_failure_after_tx_marks_failed_and_releases_locks(self):
        from unittest import mock
        from ticketd import scheduler
        proj = self.tmp / "proj"
        proj.mkdir()
        tid = self.new_ticket(grant_paths=[str(proj)])
        with mock.patch.object(scheduler.config, "prompt_file", lambda: self.tmp / "missing.md"):
            self.sched.tick()
        self.assertEqual(self.state(tid), "failed")
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "failed")
        self.assertIn("无法启动 worker", self.core.last_report(self.con, tid)["summary"])
        self.assertIsNone(self.con.execute("SELECT * FROM locks").fetchone())
        self.assertNotIn(tid, self.sched.running)

    def test_bad_workdir_does_not_block_other_tickets(self):
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        bad = self.new_ticket(message="om_a")
        self.core.touch(self.con, bad, workdir=str(blocker / "sub"))
        good = self.new_ticket(request="[[fake:sleep]]", message="om_b")
        self.sched.tick()
        self.assertEqual(self.state(good), "running")
        self.assertNotIn(bad, self.sched.running)

    def test_watch_error_does_not_skip_other_tickets_or_launch(self):
        from unittest import mock
        a = self.new_ticket(request="[[fake:sleep]]", message="om_a")
        self.sched.tick()
        self.assertIn(a, self.sched.running)
        b = self.new_ticket(request="[[fake:sleep]]", message="om_b")
        real = self.sched._watch

        def boom(tid):
            if tid == a:
                raise RuntimeError("watch boom")
            return real(tid)

        with mock.patch.object(self.sched, "_watch", boom):
            self.sched.tick()
        self.assertEqual(self.state(b), "running")

    def test_reap_error_marks_failed_and_releases_locks(self):
        from unittest import mock
        from ticketd import scheduler
        proj = self.tmp / "proj"
        proj.mkdir()
        tid = self.new_ticket(grant_paths=[str(proj)])
        self.sched.tick()
        self.sched.running[tid].proc.wait(timeout=20)

        def boom(*a, **kw):
            raise RuntimeError("ingest boom")

        with mock.patch.object(scheduler.core, "ingest_spool", boom):
            self.sched.tick()
        self.assertNotIn(tid, self.sched.running)
        self.assertEqual(self.state(tid), "failed")
        self.assertIsNone(self.con.execute("SELECT * FROM locks").fetchone())
        rep = self.core.last_report(self.con, tid)
        self.assertEqual(rep["status"], "failed")
        self.assertIn("ticketd 内部错误", rep["summary"])

    # ---------- 评审修复 F2：整组进程 ----------
    def _wait_gone(self, pid, timeout=5):
        import subprocess
        import time

        def alive():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                  capture_output=True, text=True).stdout.strip()
            return bool(stat) and not stat.startswith("Z")

        end = time.time() + timeout
        while alive() and time.time() < end:
            time.sleep(0.1)
        return not alive()

    def test_normal_exit_cleans_up_process_group(self):
        tid = self.new_ticket(request="[[fake:ready_child]]")
        marker = self.child_marker(tid)
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["summary"], "留下了后台进程")
        child = int(open(marker).read())
        self.assertTrue(self._wait_gone(child))

    def test_stop_kills_whole_process_group(self):
        import subprocess
        import time
        tid = self.new_ticket(request="[[fake:sleep_child]]")
        marker = self.child_marker(tid)
        self.wait(lambda: self.state(tid) == "running" and os.path.exists(marker)
                  and open(marker).read().strip() != "", self.sched)
        child = int(open(marker).read())
        self.core.stop(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                  capture_output=True, text=True).stdout.strip()
            return bool(stat) and not stat.startswith("Z")

        end = time.time() + 5
        while alive(child) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(child))

    # ---------- 评审修复 F3：工单目录里不跟随链接、不阻塞 ----------
    def _plant(self, tid, name, kind):
        wd = self.core.get(self.con, tid)["workdir"]
        os.makedirs(wd, exist_ok=True)
        victim = self.tmp / ("victim-" + name.strip("."))
        victim.write_text("ORIGINAL", encoding="utf-8")
        if kind == "symlink":
            os.symlink(str(victim), os.path.join(wd, name))
        else:
            os.mkfifo(os.path.join(wd, name))
        return victim

    def test_symlinked_log_is_not_followed(self):
        tid = self.new_ticket()
        victim = self._plant(tid, "过程.log", "symlink")
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        self.assertEqual(victim.read_text(encoding="utf-8"), "ORIGINAL")

    def test_symlinked_stream_is_not_followed(self):
        tid = self.new_ticket()
        victim = self._plant(tid, ".stream.jsonl", "symlink")
        self.wait(lambda: self.state(tid) in ("idle", "failed") and tid not in self.sched.running, self.sched)
        self.assertEqual(victim.read_text(encoding="utf-8"), "ORIGINAL")

    def test_fifo_stderr_does_not_hang(self):
        import time
        tid = self.new_ticket()
        self._plant(tid, ".stderr.log", "fifo")
        t0 = time.time()
        self.sched.tick()
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.state(tid), "failed")
        self.assertNotIn(tid, self.sched.running)

    # ---------- 评审修复 F4：单调时钟 ----------
    def test_running_uses_monotonic_clock(self):
        import time
        tid = self.new_ticket(request="[[fake:sleep]]")
        self.sched.tick()
        self.assertLess(abs(self.sched.running[tid].started - time.monotonic()), 5)

    # ---------- 脱离进程组的残留进程：按环境变量标记清理 ----------
    def _wait_child(self, marker):
        self.wait(lambda: os.path.exists(marker) and open(marker).read().strip() != "", self.sched)
        return int(open(marker).read())

    def test_stop_sweeps_session_escaped_orphan(self):
        tid = self.new_ticket(request="[[fake:orphan]]")
        marker = self.child_marker(tid)
        child = self._wait_child(marker)
        self.assertEqual(self.state(tid), "running")
        self.core.stop(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)
        self.assertTrue(self._wait_gone(child))
        log = open(os.path.join(self.core.get(self.con, tid)["workdir"], "过程.log"), encoding="utf-8").read()
        self.assertIn("清理残留进程", log)

    def test_normal_exit_sweeps_session_escaped_orphan(self):
        tid = self.new_ticket(request="[[fake:orphan_ready]]")
        marker = self.child_marker(tid)
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        self.assertEqual(self.core.last_report(self.con, tid)["summary"], "留下了脱组进程")
        self.assertTrue(self._wait_gone(int(open(marker).read())))

    def test_recover_sweeps_session_escaped_orphan(self):
        from ticketd.scheduler import Scheduler
        tid = self.new_ticket(request="[[fake:orphan]]")
        marker = self.child_marker(tid)
        child = self._wait_child(marker)
        Scheduler(self.con).recover()  # 模拟守护进程重启：新实例不认识正在跑的 worker
        self.assertTrue(self._wait_gone(child))

    def test_sweep_matches_exact_spool_only(self):
        import subprocess
        from ticketd.scheduler import _sweep_orphans
        spool = str(self.tmp / "w" / ".reports")
        env = dict(os.environ)
        procs = []
        try:
            for val in (spool, spool + "2"):
                env["TICKETD_SPOOL"] = val
                procs.append(subprocess.Popen(["sleep", "60"], env=dict(env)))
            import time
            time.sleep(0.2)
            self.assertEqual(_sweep_orphans(spool, 15), 1)
            procs[0].wait(timeout=5)
            self.assertIsNone(procs[1].poll())
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def test_sweep_handles_non_ascii_spool_without_locale(self):
        import subprocess
        import time
        from ticketd.scheduler import _sweep_orphans
        spool = str(self.tmp / "工单 x" / "T0001-测试" / ".reports")
        saved = {k: os.environ.pop(k) for k in ("LANG", "LC_ALL", "LC_CTYPE") if k in os.environ}
        proc = subprocess.Popen(["sleep", "60"], env=dict(os.environ, TICKETD_SPOOL=spool))
        try:
            time.sleep(0.2)
            self.assertEqual(_sweep_orphans(spool, 15), 1)
            proc.wait(timeout=5)
        finally:
            os.environ.update(saved)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

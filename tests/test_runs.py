"""沙箱外执行通道：worker 用 `ticket report --status blocked --run <脚本>` 申请，用户批准（ticket approve）后
ticketd 在沙箱外执行汇报时存下的脚本快照，输出交回 worker 接着做。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from helpers import REPO, TicketdTestCase

TICKET = str(REPO / "bin" / "ticket")


class RunRequestTest(TicketdTestCase):
    """申请（worker 侧）和批准（主会话侧）。"""

    def setUp(self):
        super().setUp()
        from ticketd import core
        self.core = core

    def report(self, workdir, *args):
        env = dict(os.environ, TICKETD_SPOOL=os.path.join(workdir, ".reports"), TICKETD_TICKET="T0001")
        return subprocess.run([TICKET, "report"] + list(args), cwd=workdir, env=env,
                              capture_output=True, text=True)

    def test_cli_validates_run_script(self):
        wd = self.tmp / "work" / "T0001-x"
        wd.mkdir(parents=True)
        (wd / "push.sh").write_text("git push\n")
        outside = self.tmp / "evil.sh"
        outside.write_text("rm -rf /\n")
        p = self.report(str(wd), "--status", "ready", "--summary", "好了", "--run", "push.sh")
        self.assertEqual(p.returncode, 2)
        self.assertIn("只能和 --status blocked", p.stderr)
        p = self.report(str(wd), "--status", "blocked", "--summary", "要推送", "--run", str(outside))
        self.assertEqual(p.returncode, 2)
        self.assertIn("要放在工单目录里", p.stderr)
        (wd / "big.sh").write_text("echo x\n" * 2000)
        p = self.report(str(wd), "--status", "blocked", "--summary", "要推送", "--run", "big.sh")
        self.assertIn("超过 8KB", p.stderr)
        p = self.report(str(wd), "--status", "blocked", "--summary", "要推送", "--run", "push.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("结束本轮", p.stdout)
        spooled = [json.loads(f.read_text()) for f in (wd / ".reports").glob("*.json")]
        self.assertEqual(spooled[0]["run"], {"path": "push.sh", "script": "git push\n"})

    def test_report_with_run_shows_script_and_approval(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "blocked", "要推送", run={"path": "push.sh", "script": "git push origin x\n"})
        run = self.core.pending_run(self.con, tid)
        self.assertEqual((run["path"], run["status"]), ("push.sh", "pending"))
        row = self.con.execute("SELECT text, card_json FROM outbox").fetchone()
        self.assertIn("git push origin x", row["card_json"])
        self.assertIn("申请在沙箱外执行（R%d" % run["id"], row["card_json"])
        self.assertIn("回复「执行」", row["card_json"])
        self.assertIn("git push origin x", row["text"])  # 卡片发不出去时的文字兜底也有脚本
        self.assertEqual(self.core.last_report(self.con, tid)["ask"], self.core.RUN_DEFAULT_ASK)
        # 批准
        self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (tid,))
        self.assertEqual(self.core.approve(self.con, tid), run["id"])
        self.assertEqual(self.con.execute("SELECT status FROM runs").fetchone()[0], "approved")
        with self.assertRaises(self.core.TicketError):
            self.core.approve(self.con, tid)  # 没有待批准的了

    def test_new_report_supersedes_and_state_checked(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "blocked", "第一份", run={"path": "a.sh", "script": "echo a\n"})
        self.core.add_report(self.con, tid, "blocked", "第二份", run={"path": "b.sh", "script": "echo b\n"})
        rows = self.con.execute("SELECT path, status FROM runs ORDER BY id").fetchall()
        self.assertEqual([(r["path"], r["status"]) for r in rows], [("a.sh", "superseded"), ("b.sh", "pending")])
        self.con.execute("UPDATE tickets SET state='held' WHERE id=?", (tid,))
        with self.assertRaises(self.core.TicketError):
            self.core.approve(self.con, tid)

    def test_cli_approve_and_show(self):
        import contextlib
        import io
        from ticketd import cli
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "blocked", "要推送", run={"path": "push.sh", "script": "echo hi\n"})
        self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (tid,))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["show", tid])
        self.assertIn("沙箱外执行 R1（push.sh）：待批准", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["approve", tid]), 0)
        self.assertIn("已批准 %s 的沙箱外执行（R1）" % tid, buf.getvalue())


class RunExecutionTest(TicketdTestCase):
    """执行（守护进程侧）。"""

    def setUp(self):
        super().setUp()
        from ticketd import config, core
        from ticketd.scheduler import Scheduler
        self.core, self.config = core, config
        self.sched = Scheduler(self.con)

    def tearDown(self):
        for rp in list(self.sched.runs.values()):
            try:
                os.killpg(rp.proc.pid, 9)
            except OSError:
                pass
        self.sched.shutdown()
        super().tearDown()

    def state(self, tid):
        return self.core.get(self.con, tid)["state"]

    def argv_log(self, tid):
        p = Path(self.core.get(self.con, tid)["workdir"]) / ".fake_argv.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]

    def request(self, script=None):
        if script is not None:
            os.environ["FAKE_RUN_SCRIPT"] = script
        tid = self.new_ticket(request="[[fake:run_request]]")
        self.wait(lambda: self.state(tid) == "idle" and self.core.pending_run(self.con, tid) is not None, self.sched)
        return tid

    def run_row(self):
        return self.con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    def test_approved_snapshot_runs_and_result_goes_back_to_worker(self):
        tid = self.request()
        wd = Path(self.core.get(self.con, tid)["workdir"])
        (wd / "push.sh").write_text("echo tampered\n")  # 批准前改了文件：执行的仍是汇报时的快照
        self.core.approve(self.con, tid)
        self.wait(lambda: self.run_row()["status"] == "done", self.sched)
        run = self.run_row()
        self.assertEqual(run["exit_code"], 0)
        log = (wd / ("沙箱外执行-R%d.log" % run["id"])).read_text(encoding="utf-8")
        self.assertIn("hello-from-outside", log)
        self.assertIn("run=R%d" % run["id"], log)
        self.assertIn(os.path.realpath(str(wd)), log)  # 在工单目录里执行
        self.assertNotIn("tampered", log)
        self.assertFalse((self.config.runs_dir() / ("R%d.sh" % run["id"])).exists())  # 快照用完即删
        # 结果交回 worker，下一轮拿到退出码和输出
        self.wait(lambda: self.core.last_report(self.con, tid)["status"] == "ready", self.sched)
        card = self.argv_log(tid)[-1]["card"]
        self.assertIn("沙箱外执行结果（R%d" % run["id"], card)
        self.assertIn("退出码：0", card)
        self.assertIn("hello-from-outside", card)
        self.wait(lambda: self.state(tid) == "idle" and tid not in self.sched.running, self.sched)

    def test_run_gets_user_env(self):  # issue #19：沙箱外执行的脚本和 worker 用同一套补充环境
        os.environ["TICKETD_WORKER_ENV"] = "LY_EXTRA"
        os.environ["LY_EXTRA"] = "extra-ok"
        os.environ["TICKETD_WORKER_PATH"] = "/opt/ly-test/bin"
        tid = self.request('echo "extra=$LY_EXTRA"\necho "$PATH"\n')
        self.core.approve(self.con, tid)
        self.wait(lambda: self.run_row()["status"] == "done", self.sched)
        wd = Path(self.core.get(self.con, tid)["workdir"])
        log = (wd / ("沙箱外执行-R%d.log" % self.run_row()["id"])).read_text(encoding="utf-8")
        self.assertIn("extra=extra-ok", log)
        self.assertIn("/opt/ly-test/bin", log)

    def test_worker_not_started_while_run_in_progress(self):
        tid = self.request("sleep 8\necho done\n")  # 要长于下面的观察期（CI 的 macOS 机器慢）
        self.core.approve(self.con, tid)
        self.wait(lambda: tid in self.sched.runs, self.sched)
        self.assertEqual(self.state(tid), "running")
        self.core.send_followup(self.con, tid, "om_2", "顺便看看这个")
        for _ in range(5):
            self.sched.tick()
            time.sleep(0.1)
        self.assertNotIn(tid, self.sched.running)  # 执行没结束前不起 worker
        self.wait(lambda: self.run_row()["status"] == "done", self.sched)

    def test_stop_during_run_terminates_it(self):
        tid = self.request("sleep 30\n")
        self.core.approve(self.con, tid)
        self.wait(lambda: tid in self.sched.runs, self.sched)
        self.core.stop(self.con, tid)
        self.wait(lambda: self.run_row()["status"] == "done", self.sched, timeout=20)
        self.assertEqual(self.state(tid), "stopped")
        pending = self.con.execute("SELECT text FROM inputs WHERE ticket_id=? AND kind='run_result'"
                                   " AND status='pending'", (tid,)).fetchone()
        self.assertIn("被用户停止中止", pending["text"])

    def test_timeout(self):
        os.environ["TICKETD_RUN_TIMEOUT"] = "1"
        tid = self.request("sleep 30\n")
        self.core.approve(self.con, tid)
        self.wait(lambda: self.run_row()["status"] == "done", self.sched, timeout=20)
        text = self.con.execute("SELECT text FROM inputs WHERE ticket_id=? AND kind='run_result'",
                                (tid,)).fetchone()["text"]
        self.assertIn("被中止", text)
        self.assertNotEqual(self.run_row()["exit_code"], 0)

    def _restart(self):
        from ticketd.scheduler import Scheduler
        self.sched.shutdown()
        self.sched = Scheduler(self.con)
        self.sched.recover()

    def test_run_survives_daemon_restart(self):
        tid = self.request("sleep 3\necho after-restart\n")
        self.core.approve(self.con, tid)
        self.wait(lambda: tid in self.sched.runs, self.sched)
        self._restart()  # 部署时重启守护进程：脚本不能被打断
        self.assertIn(tid, self.sched.runs)
        self.assertEqual(self.state(tid), "running")  # 不当成 worker 被打断
        self.wait(lambda: self.run_row()["status"] == "done", self.sched, timeout=30)
        run = self.run_row()
        self.assertEqual(run["exit_code"], 0)
        self.wait(lambda: self.core.last_report(self.con, tid)["status"] == "ready", self.sched)
        card = self.argv_log(tid)[-1]["card"]
        self.assertIn("after-restart", card)
        self.assertNotIn("服务重启打断", card)
        self.assertFalse((self.config.runs_dir() / ("R%d.exit" % run["id"])).exists())

    def test_run_finished_while_daemon_down_is_collected(self):
        tid = self.request("echo quick\nexit 3\n")
        self.core.approve(self.con, tid)
        self.wait(lambda: tid in self.sched.runs, self.sched)
        rid = self.run_row()["id"]
        exit_file = self.config.runs_dir() / ("R%d.exit" % rid)
        self.sched.runs.clear()  # 守护进程在脚本跑完之前就没了（不做收尾）
        self.wait(lambda: exit_file.exists(), timeout=10)
        self._restart()
        self.wait(lambda: self.run_row()["status"] == "done", self.sched)
        self.assertEqual(self.run_row()["exit_code"], 3)
        text = self.con.execute("SELECT text FROM inputs WHERE kind='run_result' ORDER BY id DESC").fetchone()["text"]
        self.assertIn("退出码：3", text)

    def test_restart_marks_running_run_interrupted(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (tid,))
        self.con.execute("INSERT INTO runs(ticket_id, path, script, status, created_at) VALUES(?,?,?,?,?)",
                         (tid, "push.sh", "echo x\n", "running", time.time()))
        self.sched.recover()
        self.assertEqual(self.run_row()["status"], "interrupted")
        kinds = [r["kind"] for r in self.con.execute(
            "SELECT kind FROM inputs WHERE ticket_id=? AND status='pending' ORDER BY id", (tid,))]
        self.assertIn("run_result", kinds)
        self.assertEqual(self.state(tid), "idle")

    def test_followup_expires_pending_request(self):
        tid = self.request()
        self.core.send_followup(self.con, tid, "om_2", "先别推，改一下 [[fake:ready]]")
        self.wait(lambda: self.run_row()["status"] == "expired", self.sched)
        with self.assertRaises(self.core.TicketError):
            self.core.approve(self.con, tid)

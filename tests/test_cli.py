from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from helpers import REPO, TicketdTestCase

TICKET = str(REPO / "bin" / "ticket")


class CliTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import config
        config.heartbeat_path().parent.mkdir(parents=True, exist_ok=True)
        config.heartbeat_path().touch()

    def run_cli(self, *args, env=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([TICKET] + list(args), capture_output=True, text=True, env=e)

    def test_new_prints_ack_and_lookup(self):
        req = self.tmp / "req.txt"
        req.write_text("帮我整理一下 X", encoding="utf-8")
        p = self.run_cli("new", "--chat", "oc_1", "--message", "om_9", "--title", "整理X", "--slug", "tidy-x",
                         "--request-file", str(req), "--ack", "开工：动 X，预期得到清单",
                         "--sender", "ou_owner")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "🎫 已开单 T0001「整理X」，进度看下方状态卡")
        p = self.run_cli("lookup", "--message", "om_9")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "T0001"))
        p = self.run_cli("lookup", "--message", "om_nope")
        self.assertEqual((p.returncode, p.stdout.strip()), (1, ""))

    def test_errors_exit_2(self):
        p = self.run_cli("new", "--chat", "oc_1", "--message", "om_9", "--title", "t", "--slug", "s",
                         "--request", "x", "--ack", "a", "--note", "字" * 31)
        self.assertEqual(p.returncode, 2)
        self.assertIn("错误", p.stderr)

    def test_report_uses_env_ticket(self):
        self.run_cli("new", "--chat", "oc_1", "--message", "om_9", "--title", "t", "--slug", "s",
                     "--request", "x", "--ack", "a")
        p = self.run_cli("report", "--status", "ready", "--summary", "好了", env={"TICKETD_TICKET": "T0001"})
        self.assertEqual(p.returncode, 0, p.stderr)
        from ticketd import core
        self.assertEqual(core.last_report(self.con, "T0001")["summary"], "好了")

    def test_send_stop_ls_show(self):
        self.run_cli("new", "--chat", "oc_1", "--message", "om_9", "--title", "整理", "--slug", "s",
                     "--request", "x", "--ack", "a")
        p = self.run_cli("send", "T0001", "--message", "om_10", "--text", "补充一句")
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_cli("ls")
        self.assertIn("T0001", p.stdout)
        self.assertIn("整理", p.stdout)
        p = self.run_cli("show", "T0001")
        self.assertIn("claude --resume", p.stdout)
        p = self.run_cli("stop", "T0001")
        self.assertEqual(p.stdout.strip(), "⏹️ 已停止 T0001")
        p = self.run_cli("send", "T0001", "--message", "om_11", "--text", "继续")
        self.assertEqual(p.returncode, 2)

    def test_missing_request_file_exits_2(self):
        p = self.run_cli("new", "--chat", "oc_1", "--message", "om_9", "--title", "t", "--slug", "s",
                         "--request-file", str(self.tmp / "nope.txt"), "--ack", "a")
        self.assertEqual(p.returncode, 2)
        self.assertIn("错误", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_db_failure_exits_2(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        p = self.run_cli("ls", env={"TICKETD_HOME": str(blocker / "home")})
        self.assertEqual(p.returncode, 2)
        self.assertIn("错误", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


class CliLivenessTest(TicketdTestCase):
    run_cli = CliTest.run_cli

    NEW = ("new", "--chat", "oc_1", "--message", "om_9", "--title", "t", "--slug", "s",
           "--request", "x", "--ack", "开工")

    def test_new_without_heartbeat_warns(self):
        p = self.run_cli(*self.NEW)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("⚠️"))
        self.assertIn("无心跳", lines[1])

    def test_new_with_fresh_heartbeat_has_only_ack(self):
        from ticketd import config
        config.heartbeat_path().parent.mkdir(parents=True, exist_ok=True)
        config.heartbeat_path().touch()
        p = self.run_cli(*self.NEW)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)

    def test_send_with_stale_heartbeat_warns(self):
        from ticketd import config
        hb = config.heartbeat_path()
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.touch()
        self.run_cli(*self.NEW)
        old = time.time() - 120
        os.utime(str(hb), (old, old))
        p = self.run_cli("send", "T0001", "--message", "om_10", "--text", "补充")
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(lines[0], "已转给 T0001")
        self.assertTrue(lines[1].startswith("⚠️"))
        self.assertIn("心跳", lines[1])


class CliShowSendTest(TicketdTestCase):
    run_cli = CliTest.run_cli

    def setUp(self):
        super().setUp()
        from ticketd import config
        config.heartbeat_path().parent.mkdir(parents=True, exist_ok=True)
        config.heartbeat_path().touch()

    def test_show_fifo_log_does_not_hang(self):
        from ticketd import core
        self.new_ticket()
        wd = Path(core.get(self.con, "T0001")["workdir"])
        wd.mkdir(parents=True, exist_ok=True)
        os.mkfifo(str(wd / "过程.log"))
        e = dict(os.environ)
        p = subprocess.run([TICKET, "show", "T0001"], capture_output=True, text=True, env=e, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("（过程日志不可读）", p.stdout)

    def test_show_reads_only_tail(self):
        from ticketd import core
        self.new_ticket()
        wd = Path(core.get(self.con, "T0001")["workdir"])
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "过程.log").write_text("OLD\n" + "x" * 100000 + "\nlast line\n", encoding="utf-8")
        p = self.run_cli("show", "T0001")
        self.assertIn("last line", p.stdout)
        self.assertNotIn("OLD", p.stdout)

    def test_send_to_held_ticket_says_paused(self):
        from ticketd import core
        self.new_ticket()
        core.hold(self.con, "T0001")
        p = self.run_cli("send", "T0001", "--message", "om_10", "--text", "补充")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip().splitlines()[0], "已转给 T0001（暂停中，放开后处理）")


class CliSpoolTest(TicketdTestCase):
    run_cli = CliTest.run_cli

    def spool_env(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        spool = self.tmp / "spool"
        return spool, {"TICKETD_SPOOL": str(spool), "TICKETD_HOME": str(blocker / "home")}

    def test_report_spool_mode_does_not_touch_db(self):
        spool, env = self.spool_env()
        p = self.run_cli("report", "--status", "ready", "--summary", "好了", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(list(spool.glob("*.json"))), 1)

    def test_report_spool_mode_bad_input_exits_2(self):
        spool, env = self.spool_env()
        p = self.run_cli("report", "--status", "ready", "--summary", "  ", env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("错误", p.stderr)

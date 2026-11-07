from __future__ import annotations

import json

import unittest

from helpers import TicketdTestCase


class WorkerTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, worker
        self.core = core
        self.w = worker

    def test_card_first_turn_includes_assignment(self):
        tid = self.new_ticket(request="把 A 改成 B", note="上次聊过")
        t = self.core.get(self.con, tid)
        inputs = self.con.execute("SELECT * FROM inputs WHERE ticket_id=?", (tid,)).fetchall()
        card = self.w.compose_card(t, [], inputs, include_assignment=True)
        self.assertIn("# 工单 T0001：测试", card)
        self.assertIn("把 A 改成 B", card)
        self.assertIn("上次聊过", card)
        self.assertIn("仅工单目录", card)
        self.assertEqual(card.count("把 A 改成 B"), 1)
        self.assertIn("ticket report", card)

    def test_card_followup_only(self):
        tid = self.new_ticket()
        self.core.send_followup(self.con, tid, "om_2", "再加一行")
        t = self.core.get(self.con, tid)
        inputs = self.con.execute("SELECT * FROM inputs WHERE kind='followup'").fetchall()
        card = self.w.compose_card(t, [], inputs, include_assignment=False)
        self.assertNotIn("# 工单", card)
        self.assertIn("## 用户追问（消息 om_2）", card)
        self.assertIn("再加一行", card)

    def test_build_command(self):
        tid = self.new_ticket()
        t = dict(self.core.get(self.con, tid))
        cmd = self.w.build_command(t, "/s.json", "PROMPT")
        self.assertIn("--session-id", cmd)
        self.assertIn("dontAsk", cmd)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], "PROMPT")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        t["session_started"] = 1
        cmd = self.w.build_command(t, "/s.json", "PROMPT")
        self.assertEqual(cmd[cmd.index("--resume") + 1], t["session_id"])
        self.assertNotIn("--session-id", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")

    def test_worker_env(self):
        from ticketd import config
        tid = self.new_ticket()
        t = self.core.get(self.con, tid)
        env = self.w.worker_env(t)
        self.assertTrue(env["TICKETD_SPOOL"].endswith("/.reports"))
        self.assertTrue(env["TICKETD_SPOOL"].startswith(t["workdir"]))
        self.assertEqual(env["TICKETD_TICKET"], tid)
        self.assertTrue(env["PATH"].startswith(str(config.bin_dir())))

    def test_summarize(self):
        s = self.w.summarize_event
        self.assertEqual(s({"type": "system", "subtype": "init", "session_id": "abc"}), ["会话就绪 abc"])
        out = s({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
            {"type": "text", "text": "看一下"}]}})
        self.assertEqual(out, ["调用 Bash：ls -la", "说：看一下"])
        out = s({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "denied"}]}})
        self.assertEqual(out, ["工具报错：denied"])
        out = s({"type": "result", "subtype": "success", "duration_ms": 3000, "permission_denials": [{}]})
        self.assertEqual(out, ["本轮结束：success，用时 3s，被拒操作 1 次"])

    def test_stream_tail_handles_partial_lines(self):
        p = self.tmp / "s.jsonl"
        p.write_text('{"a": 1}\n{"b"', encoding="utf-8")
        tail = self.w.StreamTail(p)
        self.assertEqual(tail.read_events(), [{"a": 1}])
        with p.open("a", encoding="utf-8") as f:
            f.write(': 2}\nnot json\n')
        self.assertEqual(tail.read_events(), [{"b": 2}])

    def test_is_transient(self):
        t = self.w.is_transient
        self.assertTrue(t(1, None, "API Error: 529 overloaded"))
        self.assertTrue(t(1, {"is_error": True, "api_error_status": 429}, ""))
        self.assertFalse(t(0, {"is_error": False}, ""))
        self.assertFalse(t(1, None, "SyntaxError: bad"))

    def test_summarize_odd_shapes(self):
        s = self.w.summarize_event
        self.assertEqual(s({"type": "assistant", "message": {"content": "hi"}}), ["说：hi"])
        self.assertEqual(s({"type": "assistant", "message": {"content": ["x", {"type": "text", "text": "ok"}]}}),
                         ["说：ok"])
        self.assertEqual(s({"type": "assistant", "message": "str"}), [])
        self.assertEqual(s({"type": "user", "message": "str"}), [])

    def test_stream_tail_ignores_non_dicts(self):
        p = self.tmp / "n.jsonl"
        p.write_text('123\n[1]\n"x"\n{"a":1}\n', encoding="utf-8")
        self.assertEqual(self.w.StreamTail(p).read_events(), [{"a": 1}])

    def test_is_transient_markers(self):
        t = self.w.is_transient
        self.assertFalse(t(1, None, "error in /tmp/job1500/x"))
        self.assertTrue(t(1, None, "Error: request failed with status 503"))
        self.assertTrue(t(1, None, "API Error: 500 Internal"))
        self.assertTrue(t(1, {"is_error": True, "result": "API Error: 529 Overloaded"}, ""))

    def test_stream_tail_does_not_follow_symlink(self):
        import os
        victim = self.tmp / "victim.jsonl"
        victim.write_text('{"secret": 1}\n', encoding="utf-8")
        p = self.tmp / "link.jsonl"
        os.symlink(str(victim), str(p))
        self.assertEqual(self.w.StreamTail(p).read_events(), [])

    def test_stream_tail_bounds_reads_and_partial_lines(self):
        p = self.tmp / "big.jsonl"
        p.write_bytes(b"x" * (2 * 1024 * 1024 + 10))
        tail = self.w.StreamTail(p)
        for _ in range(3):
            self.assertEqual(tail.read_events(), [])
            self.assertLessEqual(len(tail.buf), 1024 * 1024)
        with p.open("ab") as f:
            f.write(b'\n{"a": 1}\n')
        self.assertEqual(tail.read_events(), [{"a": 1}])


class StepLabelTest(unittest.TestCase):
    def test_human_labels(self):
        from ticketd.worker import last_step, step_label
        self.assertEqual(step_label("Bash", {"command": "cd /x && ls", "description": "列出文件"}), "列出文件")
        self.assertEqual(step_label("Bash", {"command": "pytest -q /Users/x/secret"}), "运行 pytest")
        self.assertEqual(step_label("Bash", {"command": "cd /x && FOO=1 /usr/bin/python3 -m unittest"}), "运行 python3")
        self.assertEqual(step_label("Bash", {"command": "cd /x"}), "运行 命令")
        self.assertEqual(step_label("Edit", {"file_path": "/a/b/card.py"}), "改 card.py")
        self.assertEqual(step_label("Read", {"file_path": "/a/b/"}), "读 b")
        self.assertEqual(step_label("WebFetch", {"url": "https://open.feishu.cn/doc/x"}), "看网页 open.feishu.cn")
        self.assertEqual(step_label("mcp__plugin_figma__get_screenshot", {}), "调用 get_screenshot")
        self.assertEqual(step_label("Agent", {"description": "对抗审查"}), "派子代理：对抗审查")
        self.assertEqual(step_label(None, None), "调用 ")
        ev = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "先看看"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/a.py"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/x/b.py"}}]}}
        self.assertEqual(last_step(ev), "写 b.py")
        self.assertIsNone(last_step({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}))
        self.assertIsNone(last_step({"type": "user"}))


class AccountErrorTest(unittest.TestCase):
    def test_detects_balance_and_auth(self):
        from ticketd import worker
        self.assertEqual(worker.account_error("Failed to authenticate. API Error: 403 insufficient balance"), "余额不足")
        self.assertEqual(worker.account_error("", "Invalid API key · Please run /login"), "认证失败")
        self.assertIsNone(worker.account_error("API Error: 529 overloaded", ""))

    def test_result_error_text_only_for_errors(self):
        from ticketd import worker
        self.assertEqual(worker.result_error_text({"type": "result", "is_error": False, "result": "ok"}), "")
        self.assertEqual(worker.result_error_text({"type": "result", "is_error": True, "result": " boom "}), "boom")
        self.assertEqual(worker.result_error_text(None), "")

    def test_bg_task_events(self):
        from ticketd import worker
        self.assertEqual(worker.bg_task_event({"type": "system", "subtype": "task_started", "task_id": "b1",
                                               "description": "出图"}), ("start", "b1", "出图"))
        self.assertEqual(worker.bg_task_event({"type": "system", "subtype": "task_notification", "task_id": "b1",
                                               "status": "completed"}), ("end", "b1", ""))
        self.assertIsNone(worker.bg_task_event({"type": "system", "subtype": "task_progress", "task_id": "b1"}))
        self.assertTrue(worker.is_action({"type": "system", "subtype": "task_notification", "task_id": "b1"}))


class IssueRegressionTest(unittest.TestCase):
    def test_enotfound_is_transient(self):  # issue #10
        from ticketd import worker
        ev = {"type": "result", "is_error": True,
              "result": "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"}
        self.assertTrue(worker.is_transient(1, ev, ""))
        self.assertTrue(worker.is_transient(1, None, "getaddrinfo EAI_AGAIN api.example.com"))
        self.assertFalse(worker.is_transient(1, {"type": "result", "is_error": True,
                                                 "result": "API Error: 403 insufficient balance"}, ""))

    def test_error_round_not_logged_as_success(self):  # issue #15
        from ticketd import worker
        out = worker.summarize_event({"type": "result", "subtype": "success", "is_error": True,
                                      "result": "API Error: 400 bad", "duration_ms": 1000})
        self.assertEqual(out, ["本轮结束：出错（API Error: 400 bad），用时 1s，被拒操作 0 次"])


class ImportErrorTextTest(unittest.TestCase):
    def test_keeps_error_and_drops_notice(self):  # issue #12
        from ticketd import publish
        data = {"ok": False, "error": {"type": "auth", "code": 99991, "message": "strict mode: user identity disabled"},
                "_notice": {"update": "x" * 500 + " lark-cli update"}}
        text = publish._error_text(data, "")
        self.assertIn("strict mode: user identity disabled", text)
        self.assertNotIn("lark-cli update", text)
        self.assertIn('"type": "network"', publish._error_text({"ok": False, "error": {"type": "network"}}, ""))
        self.assertEqual(publish._error_text({}, "  plain failure  "), "plain failure")


class SandboxLineTest(unittest.TestCase):
    def test_claude_card_points_to_settings_codex_does_not(self):  # issue #21
        from ticketd import worker
        t = {"id": "T0009", "title": "x", "workdir": "/w/T0009-x", "request_text": "做", "router_note": None,
             "executor": "claude"}
        card = worker.compose_card(t, [], [], include_assignment=True)
        self.assertIn("沙箱：只能写可写目录", card)
        self.assertIn("T0009.json", card)
        t["executor"] = "codex"
        self.assertNotIn("沙箱：", worker.compose_card(t, [], [], include_assignment=True))

from __future__ import annotations

import json
import time
import os

from helpers import TicketdTestCase


class CoreTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, templates
        self.core = core
        self.templates = templates

    def test_create_ticket_records_everything(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        tid = self.new_ticket(grant_paths=[str(proj)], note="背景一句")
        self.assertEqual(tid, "T0001")
        t = self.core.get(self.con, tid)
        self.assertEqual(t["state"], "queued")
        self.assertTrue(t["workdir"].endswith("T0001-test"))
        self.assertEqual(self.core.grants(self.con, tid), [str(proj.resolve())])
        self.assertEqual(self.core.lookup(self.con, "om_1"), tid)
        inputs = self.con.execute("SELECT * FROM inputs WHERE ticket_id=?", (tid,)).fetchall()
        self.assertEqual([i["kind"] for i in inputs], ["assignment"])
        self.assertIsNone(self.con.execute("SELECT * FROM outbox").fetchone())  # 确认由 bridge 自己发

    def test_ids_are_sequential(self):
        self.assertEqual(self.new_ticket(message="om_a"), "T0001")
        self.assertEqual(self.new_ticket(message="om_b"), "T0002")

    def test_duplicate_message_refused(self):
        self.new_ticket()
        with self.assertRaises(self.core.TicketError):
            self.new_ticket()

    def test_validation(self):
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(note="字" * 31)
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(sender="ou_someone_else")
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(grant_paths=["relative/path"])
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(grant_paths=[str(self.tmp / "missing")])
        with self.assertRaises(self.core.TicketError):
            self.new_ticket(slug="a/b")

    def test_owner_sender_allowed(self):
        self.assertEqual(self.new_ticket(sender="ou_owner"), "T0001")

    def test_followup_binds_and_queues(self):
        tid = self.new_ticket()
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        self.assertEqual(self.core.lookup(self.con, "om_2"), tid)
        kinds = [r["kind"] for r in self.con.execute("SELECT kind FROM inputs WHERE ticket_id=? ORDER BY id", (tid,))]
        self.assertEqual(kinds, ["assignment", "followup"])

    def test_followup_to_stopped_refused(self):
        tid = self.new_ticket()
        self.core.stop(self.con, tid)
        with self.assertRaises(self.core.TicketError):
            self.core.send_followup(self.con, tid, "om_2", "继续")

    def test_report_replies_to_latest_delivered_human_message(self):
        tid = self.new_ticket()
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        self.con.execute("UPDATE inputs SET status='delivered'")
        self.core.add_report(self.con, tid, "ready", "改好了", artifacts=["/x/结果.md"])
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["reply_to"], "om_2")
        self.assertIn("✅ T0001 完成：改好了", row["text"])  # 卡片发不出去时的兜底文字
        self.assertIn("📎 /x/结果.md", row["text"])
        card = json.loads(row["card_json"])  # 实际发的是结果卡片
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("✅ 完成", card["header"]["subtitle"]["content"])
        self.assertEqual(card["config"]["summary"]["content"], "✅ T0001 完成：改好了")
        self.assertEqual(row["report_id"], self.core.last_report(self.con, tid)["id"])
        rep = self.core.last_report(self.con, tid)
        self.assertEqual(json.loads(rep["artifacts_json"]), ["/x/结果.md"])

    def test_report_falls_back_to_origin(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "blocked", "缺权限", ask="能授权 /x 吗？")
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["reply_to"], "om_1")
        self.assertIn("⚠️ T0001 需要你决定：缺权限", row["text"])
        self.assertIn("👉 需要你：能授权 /x 吗？", row["text"])
        card = json.loads(row["card_json"])
        self.assertEqual(card["header"]["template"], "orange")
        box = card["body"]["elements"][1]  # 结论下面就是「需要你」色块（T0010 改版）
        self.assertEqual(box["columns"][0]["background_style"], "orange-50")
        self.assertEqual([e["text"]["content"] for e in box["columns"][0]["elements"]], ["👉 需要你", "能授权 /x 吗？"])

    def test_progress_and_silent_dropped_not_sent(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "progress", "做了一半")
        self.core.add_report(self.con, tid, "dropped", "复现不了")
        self.assertIsNone(self.con.execute("SELECT * FROM outbox").fetchone())
        self.core.add_report(self.con, tid, "dropped", "不属于本单", ask="这是另一件事")
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertIn("不引用直接发我即可开新单", row["text"])

    def test_progress_ask_is_plain_text(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "progress", "做到一半", ask="用 A 还是 B？")
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertIsNone(row["card_json"])
        self.assertIn("👉 需要你：用 A 还是 B？", row["text"])

    def test_result_card_shows_phases_elapsed_and_short_paths(self):
        tid = self.new_ticket()
        wd = self.core.get(self.con, tid)["workdir"]
        self.con.execute("UPDATE tickets SET state='running', started_at=? WHERE id=?", (time.time() - 610, tid))
        self.core.add_report(self.con, tid, "progress", "读代码")
        self.core.add_report(self.con, tid, "progress", "读代码")
        self.core.add_report(self.con, tid, "progress", "写代码")
        self.core.add_report(self.con, tid, "ready", "好了", [wd + "/结果.md", "https://x.feishu.cn/docx/1"])
        card = json.loads(self.con.execute("SELECT card_json FROM outbox").fetchone()[0])
        self.assertIn("用时 10 分钟", card["header"]["subtitle"]["content"])
        flat = json.dumps(card, ensure_ascii=False)
        panel = [e for e in card["body"]["elements"] if e["tag"] == "collapsible_panel"][0]  # 本机路径收进折叠面板
        self.assertEqual(panel["header"]["title"]["content"], "📎 本机文件（1）")
        self.assertEqual(panel["elements"][0]["text"]["content"], "📎 本机文件\n结果.md")
        self.assertNotIn(wd, flat)
        self.assertIn("过程：读代码 → 写代码", flat)
        btn = [e for e in card["body"]["elements"] if e["tag"] == "button"][0]
        self.assertEqual(btn["behaviors"][0]["default_url"], "https://x.feishu.cn/docx/1")

    def test_bad_status_refused(self):
        tid = self.new_ticket()
        with self.assertRaises(self.core.TicketError):
            self.core.add_report(self.con, tid, "done", "x")
        with self.assertRaises(self.core.TicketError):
            self.core.add_report(self.con, tid, "ready", "  ")

    def test_hold_release_grant(self):
        tid = self.new_ticket()
        self.core.hold(self.con, tid)
        self.assertEqual(self.core.get(self.con, tid)["state"], "held")
        with self.assertRaises(self.core.TicketError):
            self.core.hold(self.con, tid)
        self.core.release(self.con, tid)
        self.assertEqual(self.core.get(self.con, tid)["state"], "idle")
        proj = self.tmp / "p2"
        proj.mkdir()
        self.core.grant(self.con, tid, str(proj))
        self.assertIn(str(proj.resolve()), self.core.grants(self.con, tid))
        kinds = [r["kind"] for r in self.con.execute("SELECT kind FROM inputs WHERE ticket_id=? ORDER BY id", (tid,))]
        self.assertEqual(kinds, ["assignment", "release", "grant"])
        with self.assertRaises(self.core.TicketError):
            self.core.release(self.con, tid)

    def test_release_resumes_stopped_ticket(self):  # issue #18
        tid = self.new_ticket()
        self.core.stop(self.con, tid)
        self.assertEqual(self.core.release(self.con, tid), "stopped")
        t = self.core.get(self.con, tid)
        self.assertEqual(t["state"], "idle")
        last = self.con.execute("SELECT kind, text FROM inputs WHERE ticket_id=? ORDER BY id DESC", (tid,)).fetchone()
        self.assertEqual(last["kind"], "release")
        self.assertIn("恢复了这张已停止的工单", last["text"])

    def test_restart_uses_new_session_and_carries_followups(self):  # issue #14
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='failed', session_started=1 WHERE id=?", (tid,))
        self.con.execute("UPDATE inputs SET status='delivered' WHERE ticket_id=?", (tid,))
        self.core.send_followup(self.con, tid, "om_2", "第一条追问")
        old = self.core.get(self.con, tid)["session_id"]
        self.core.restart(self.con, tid)
        t = self.core.get(self.con, tid)
        self.assertNotEqual(t["session_id"], old)
        self.assertEqual((t["session_started"], t["state"]), (0, "idle"))
        pending = self.con.execute("SELECT kind, text FROM inputs WHERE ticket_id=? AND status='pending'",
                                   (tid,)).fetchall()
        self.assertEqual([r["kind"] for r in pending], ["fresh"])
        self.assertIn("1. 第一条追问", pending[0]["text"])

    def test_restart_refused_while_running(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (tid,))
        with self.assertRaises(self.core.TicketError):
            self.core.restart(self.con, tid)

    def test_touch_if_only_updates_expected_states(self):
        tid = self.new_ticket()
        self.assertTrue(self.core.touch_if(self.con, tid, ("queued",), state="running", pid=42))
        self.assertEqual(self.core.get(self.con, tid)["pid"], 42)
        self.core.stop(self.con, tid)
        self.assertFalse(self.core.touch_if(self.con, tid, ("running",), state="idle", pid=None))
        t = self.core.get(self.con, tid)
        self.assertEqual((t["state"], t["pid"]), ("stopped", 42))

    def test_ack_message_format(self):
        self.assertEqual(self.templates.ack_message("T0001", "整理X"), "🎫 已开单 T0001「整理X」，进度看下方状态卡")

    def test_goal_and_paths(self):
        from pathlib import Path
        self.assertEqual(self.templates.goal_text("开工：动 X，预期 Y"), "动 X，预期 Y")
        self.assertEqual(self.templates.scope_text([str(Path.home() / "Projects/x")]), "~/Projects/x")
        self.assertEqual(self.templates.trail(["a", "b", "c"], done=False), "✓ a → ✓ b → ● c")
        self.assertEqual(self.templates.trail(list("abcde"), done=True, limit=3), "… → c → d → e")

    def test_followup_is_idempotent(self):
        tid = self.new_ticket()
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        n = self.con.execute("SELECT COUNT(*) FROM inputs WHERE ticket_id=? AND kind='followup'", (tid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_followup_may_reuse_origin_message_of_same_ticket(self):
        tid = self.new_ticket()
        self.core.send_followup(self.con, tid, "om_1", "补充")
        kinds = [r["kind"] for r in self.con.execute("SELECT kind FROM inputs WHERE ticket_id=? ORDER BY id", (tid,))]
        self.assertEqual(kinds, ["assignment", "followup"])

    def test_followup_message_bound_to_other_ticket_refused(self):
        self.new_ticket(message="om_a")
        t2 = self.new_ticket(message="om_b")
        with self.assertRaises(self.core.TicketError):
            self.core.send_followup(self.con, t2, "om_a", "串单")
        n = self.con.execute("SELECT COUNT(*) FROM inputs WHERE ticket_id=? AND kind='followup'", (t2,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_next_id_after_9999(self):
        tid = self.new_ticket()
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute("UPDATE tickets SET id='T9999' WHERE id=?", (tid,))
        self.assertEqual(self.new_ticket(message="om_z"), "T10000")
        self.assertEqual(self.new_ticket(message="om_y"), "T10001")


class SpoolTest(TicketdTestCase):
    def test_spool_roundtrip(self):
        from ticketd import core
        tid = self.new_ticket()
        spool = self.tmp / "spool"
        core.write_spool(str(spool), "ready", "做完了", ["/tmp/a.txt"], None)
        self.assertEqual(core.ingest_spool(self.con, tid, str(spool)), 1)
        self.assertEqual(core.last_report(self.con, tid)["summary"], "做完了")
        n = self.con.execute("SELECT COUNT(*) FROM outbox WHERE ticket_id=?", (tid,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(list(spool.iterdir()), [])

    def test_ingest_missing_dir(self):
        from ticketd import core
        tid = self.new_ticket()
        self.assertEqual(core.ingest_spool(self.con, tid, str(self.tmp / "nope")), 0)

    def test_malformed_file_becomes_bad_and_later_ingested(self):
        from ticketd import core
        tid = self.new_ticket()
        spool = self.tmp / "spool"
        spool.mkdir()
        (spool / "00000000000000000001-1.json").write_text("{not json", encoding="utf-8")
        (spool / "00000000000000000002-1.json").write_text('{"status": "nope", "summary": "x"}', encoding="utf-8")
        core.write_spool(str(spool), "ready", "好的")
        self.assertEqual(core.ingest_spool(self.con, tid, str(spool)), 1)
        names = sorted(p.name for p in spool.iterdir())
        self.assertEqual(names, ["00000000000000000001-1.json.bad", "00000000000000000002-1.json.bad"])
        self.assertEqual(core.last_report(self.con, tid)["summary"], "好的")

    def test_write_spool_bad_status(self):
        from ticketd import core
        with self.assertRaises(core.TicketError):
            core.write_spool(str(self.tmp / "spool"), "bogus", "x")


GOOD = '{"status": "ready", "summary": "ok"}'


class SpoolHardenTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        self.core = core
        self.tid = self.new_ticket()
        self.spool = self.tmp / "spool"
        self.spool.mkdir()

    def names(self):
        return sorted(p.name for p in self.spool.iterdir())

    def test_spool_dir_symlink_ignored(self):
        target = self.tmp / "target"
        target.mkdir()
        (target / "a.json").write_text(GOOD, encoding="utf-8")
        (target / "package.json").write_text("{}", encoding="utf-8")
        link = self.tmp / ".reports"
        link.symlink_to(target)
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(link)), 0)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["a.json", "package.json"])
        self.assertEqual((target / "a.json").read_text(encoding="utf-8"), GOOD)
        self.assertEqual((target / "package.json").read_text(encoding="utf-8"), "{}")

    def test_spool_dir_is_file(self):
        f = self.tmp / "afile"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(f)), 0)
        self.assertEqual(f.read_text(encoding="utf-8"), "x")

    def test_symlink_entry_marked_bad_target_untouched(self):
        target = self.tmp / "elsewhere.json"
        target.write_text(GOOD, encoding="utf-8")
        (self.spool / "x.json").symlink_to(target)
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(self.spool)), 0)
        self.assertEqual(self.names(), ["x.json.bad"])
        self.assertTrue((self.spool / "x.json.bad").is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), GOOD)

    def test_fifo_and_dir_entries(self):
        os.mkfifo(str(self.spool / "f.json"))
        (self.spool / "d.json").mkdir()
        (self.spool / "z.json").write_text(GOOD, encoding="utf-8")
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(self.spool)), 1)
        self.assertEqual(self.names(), ["d.json.bad", "f.json.bad"])

    def test_schema_violations_marked_bad(self):
        bad = ['{"summary": 5, "status": "ready"}',
               '{"status": "ready", "summary": "x", "ask": {"a": 1}}',
               '[1, 2]',
               '{"status": "ready", "summary": "x", "artifacts": [1]}',
               '{"status": "ready", "summary": "x", "artifacts": "a"}',
               '{"status": "ready", "summary": "%s"}' % ("长" * 2001)]
        for i, b in enumerate(bad):
            (self.spool / ("%02d.json" % i)).write_text(b, encoding="utf-8")
        (self.spool / "99.json").write_text(GOOD, encoding="utf-8")
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(self.spool)), 1)
        self.assertEqual(self.names(), ["%02d.json.bad" % i for i in range(len(bad))])

    def test_oversize_file_marked_bad(self):
        (self.spool / "big.json").write_text('{"status":"ready","summary":"%s"}' % ("a" * 70000),
                                             encoding="utf-8")
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(self.spool)), 0)
        self.assertEqual(self.names(), ["big.json.bad"])

    def test_cap_ten_per_call(self):
        for i in range(12):
            (self.spool / ("%02d.json" % i)).write_text(GOOD, encoding="utf-8")
        self.assertEqual(self.core.ingest_spool(self.con, self.tid, str(self.spool)), 10)
        self.assertEqual(self.names(), ["10.json.bad", "11.json.bad"])


class GrantOverlapTest(TicketdTestCase):
    def test_overlapping_grants_rejected(self):
        from ticketd import core
        (self.tmp / "work").mkdir()
        (self.tmp / "home").mkdir(exist_ok=True)
        for p in (self.tmp / "work", self.tmp, self.tmp / "home"):
            with self.assertRaises(core.TicketError, msg=str(p)):
                self.new_ticket(grant_paths=[str(p)])


class NetGrantTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        self.core = core

    def test_socket_grant_keeps_link_and_target(self):
        import os
        target = self.tmp / "real.sock"
        target.write_text("")
        link = self.tmp / "docker.sock"
        os.symlink(str(target), str(link))
        tid = self.new_ticket()
        self.core.grant_net(self.con, tid, socket=str(link))
        self.assertEqual(self.core.net_grants(self.con, tid)["sockets"], sorted([str(link), str(target)]))
        last = self.con.execute("SELECT kind, text FROM inputs WHERE ticket_id=? ORDER BY id DESC", (tid,)).fetchone()
        self.assertEqual(last["kind"], "grant")
        self.assertIn("unix socket", last["text"])

    def test_local_grant_and_validation(self):
        tid = self.new_ticket()
        self.assertFalse(self.core.net_grants(self.con, tid)["local"])
        self.core.grant_net(self.con, tid, local=True)
        self.assertTrue(self.core.net_grants(self.con, tid)["local"])
        with self.assertRaises(self.core.TicketError):
            self.core.grant_net(self.con, tid, socket="relative.sock")
        with self.assertRaises(self.core.TicketError):
            self.core.grant_net(self.con, tid, socket=str(self.tmp / "missing.sock"))
        self.core.stop(self.con, tid)
        with self.assertRaises(self.core.TicketError):
            self.core.grant_net(self.con, tid, local=True)

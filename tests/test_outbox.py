from __future__ import annotations

import json
import os

from helpers import TicketdTestCase


class OutboxTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, outbox
        self.core = core
        self.outbox = outbox

    def new_ticket(self, *a, **kw):
        # 这里只测发件箱本身：不要状态卡（状态卡与发件箱的先后顺序见 test_card）
        tid = super().new_ticket(*a, **kw)
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
        return tid

    def sent(self):
        p = self.tmp / "lark.log"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_send_success_binds_result_message(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")
        self.outbox.flush(self.con)
        args = self.sent()[0]
        self.assertEqual(args[:4], ["im", "+messages-reply", "--as", "bot"])
        self.assertEqual(args[args.index("--message-id") + 1], "om_1")
        key = args[args.index("--idempotency-key") + 1]
        self.assertTrue(key.startswith("ticketd-T0001-1-"))
        self.assertLessEqual(len(key), 50)
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual((row["status"], row["sent_message_id"]), ("sent", "om_fake_1"))
        self.assertEqual(self.core.lookup(self.con, "om_fake_1"), tid)

    def test_failure_backoff_and_order(self):
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "blocked", "第一条", ask="q")
        self.core.add_report(self.con, tid, "ready", "第二条")
        self.outbox.flush(self.con)
        rows = self.con.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        self.assertEqual((rows[0]["status"], rows[0]["attempts"]), ("pending", 1))
        self.assertGreater(rows[0]["next_at"], rows[0]["created_at"] + 4)
        self.assertEqual(rows[1]["attempts"], 0)  # 同一工单的后一条要等前一条发出
        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con)
        self.outbox.flush(self.con)
        cards = [json.loads(a[a.index("--content") + 1]) for a in self.sent()]
        self.assertEqual([c["config"]["summary"]["content"] for c in cards],
                         ["⚠️ T0001 需要你决定：第一条", "✅ T0001 完成：第二条"])

    def test_card_rejected_falls_back_to_text(self):
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        os.environ["FAKE_LARK_PERMANENT"] = "1"
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")
        self.outbox.flush(self.con)
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["card_json"], "")  # 标记：卡片已被拒，之后只发文字
        args = self.sent()[0]
        self.assertEqual(args[args.index("--text") + 1], "✅ T0001 完成：好了")
        self.assertTrue(args[args.index("--idempotency-key") + 1].endswith("-t"))
        from ticketd import config
        self.assertIn("改发文字", (config.logs_dir() / "alerts.log").read_text(encoding="utf-8"))

    def test_permanent_failure_dead_and_alert(self):
        fail = self.tmp / "fail"
        fail.write_text("2")  # 卡片和兜底文字都失败
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        os.environ["FAKE_LARK_PERMANENT"] = "1"
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")
        self.outbox.flush(self.con)
        self.assertEqual(self.con.execute("SELECT status FROM outbox").fetchone()[0], "dead")
        from ticketd import config
        self.assertIn("dead", (config.logs_dir() / "alerts.log").read_text(encoding="utf-8"))

    def test_third_failure_alerts(self):
        fail = self.tmp / "fail"
        fail.write_text("3")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")
        for _ in range(3):
            self.con.execute("UPDATE outbox SET next_at=0")
            self.outbox.flush(self.con)
        from ticketd import config
        self.assertIn("连续失败 3 次", (config.logs_dir() / "alerts.log").read_text(encoding="utf-8"))

    def _one_retryable_failure(self):
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")

    def test_old_row_marked_dead(self):
        self._one_retryable_failure()
        self.con.execute("UPDATE outbox SET created_at=created_at-25*3600")
        self.outbox.flush(self.con)
        self.assertEqual(self.con.execute("SELECT status FROM outbox").fetchone()[0], "dead")
        from ticketd import config
        self.assertIn("仍失败", (config.logs_dir() / "alerts.log").read_text(encoding="utf-8"))

    def test_many_attempts_fresh_row_stays_pending(self):
        self._one_retryable_failure()
        self.con.execute("UPDATE outbox SET attempts=19")
        self.outbox.flush(self.con)
        row = self.con.execute("SELECT status, attempts FROM outbox").fetchone()
        self.assertEqual((row[0], row[1]), ("pending", 20))

    def test_network_failure_stops_tick(self):
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        t1 = self.new_ticket()
        t2 = self.new_ticket(message="om_2")
        self.core.add_report(self.con, t1, "ready", "甲")
        self.core.add_report(self.con, t2, "ready", "乙")
        self.outbox.flush(self.con)
        self.assertEqual(self.sent(), [])
        rows = self.con.execute("SELECT attempts FROM outbox ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 0])

    def test_parse_json_variants(self):
        env = {"ok": True, "data": {"message_id": "om_x"}}
        pretty = json.dumps(env, indent=2)
        cases = [
            pretty + "\n[lark-cli] done",
            json.dumps({"level": "info"}) + "\n" + pretty,
            "   \n   " + pretty,
            "[lark-cli] [WARN] a\n[lark-cli] [WARN] b\n" + pretty + "\n",
        ]
        for text in cases:
            self.assertEqual(self.outbox._parse_json(text), env)
        self.assertIsNone(self.outbox._parse_json("no json {here"))

    def test_rc0_unparseable_counts_as_sent(self):
        import subprocess
        from unittest import mock
        done = subprocess.CompletedProcess([], 0, stdout="garbage", stderr="")
        with mock.patch("subprocess.run", return_value=done):
            self.assertEqual(self.outbox.send_reply("om_1", "t", "k"), (True, "", False))

    def test_sender_exception_marks_row_dead_and_continues(self):
        t1 = self.new_ticket(message="om_1")
        t2 = self.new_ticket(message="om_2")
        self.core.add_report(self.con, t1, "ready", "毒丸")
        self.core.add_report(self.con, t2, "ready", "正常")
        calls = []

        def sender(reply_to, text, idem):
            calls.append(reply_to)
            if reply_to == "om_1":
                raise ValueError("boom")
            return True, "om_sent", False

        self.outbox.flush(self.con, sender=sender, card_sender=lambda r, c, i: sender(r, c, i))
        rows = self.con.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        self.assertEqual(rows[0]["status"], "dead")
        self.assertIn("ValueError", rows[0]["last_error"])
        self.assertEqual(rows[1]["status"], "sent")
        self.assertEqual(calls, ["om_1", "om_2"])

    # ---------- 稿子链接 ----------
    def _ready_with(self, tid, *names, extra=()):
        wd = self.core.get(self.con, tid)["workdir"]
        arts = []
        for n in names:
            p = os.path.join(wd, n)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("# 稿子\n正文")
            arts.append(p)
        self.core.add_report(self.con, tid, "ready", "好了", arts + list(extra))
        return wd

    def _buttons(self, args):
        card = json.loads(args[args.index("--content") + 1])
        return [e for e in card["body"]["elements"] if e.get("tag") == "button"]

    def test_doc_artifacts_become_buttons_on_result_card(self):
        tid = self.new_ticket()
        self._ready_with(tid, "稿子.md", "表.xlsx", "包.zip", ".reports/x.md", extra=["/etc/hosts.md"])
        self.outbox.flush(self.con)
        calls = self.sent()
        imports = [a for a in calls if a[:2] == ["drive", "+import"]]
        self.assertEqual([a[a.index("--file") + 1] for a in imports], ["稿子.md", "表.xlsx"])
        self.assertEqual(imports[0][imports[0].index("--as") + 1], "user")
        self.assertEqual(imports[0][imports[0].index("--name") + 1], "%s 稿子" % tid)
        btns = self._buttons(calls[-1])
        self.assertEqual([b["text"]["content"] for b in btns], ["📄 打开 稿子.md", "📄 打开 表.xlsx"])
        self.assertTrue(btns[0]["behaviors"][0]["default_url"].startswith("https://example.feishu.cn/docx/"))
        # 按钮跟在正文后面，在收起的「本机文件」面板和页脚之前
        tags = [e["tag"] for e in json.loads(calls[-1][calls[-1].index("--content") + 1])["body"]["elements"]]
        self.assertEqual(tags, ["div", "button", "button", "collapsible_panel", "div"])

    def test_doc_buttons_go_before_footer_without_panel(self):
        from ticketd import templates
        card = templates.result_card("T1", "t", "/w", "ready", "好了", ["https://x.feishu.cn/docx/1"], None, [], 1)
        templates.with_doc_links(card, [("稿子.md", "https://example.feishu.cn/docx/a")])
        els = card["body"]["elements"]
        self.assertEqual([e["tag"] for e in els], ["div", "button", "button", "div"])
        self.assertEqual(els[2]["text"]["content"], "📄 打开 稿子.md")
        self.assertIn("引用此卡可追问", els[-1]["text"]["content"])

    def test_doc_import_cached_for_unchanged_file(self):
        tid = self.new_ticket()
        wd = self._ready_with(tid, "稿子.md")
        self.outbox.flush(self.con)
        self.core.add_report(self.con, tid, "ready", "再看一眼", [os.path.join(wd, "稿子.md")])
        self.outbox.flush(self.con)
        imports = [a for a in self.sent() if a[:2] == ["drive", "+import"]]
        self.assertEqual(len(imports), 1)
        self.assertEqual(len(self._buttons(self.sent()[-1])), 1)

    def test_doc_import_failure_still_delivers_result(self):
        tid = self.new_ticket()
        self._ready_with(tid, "稿子.md")
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        self.outbox.flush(self.con)
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(self._buttons(self.sent()[-1]), [])
        self.assertTrue(any("导入云文档失败" in p.read_text(encoding="utf-8")
                            for p in (self.tmp / "home").rglob("alerts.log")))

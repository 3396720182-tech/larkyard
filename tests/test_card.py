from __future__ import annotations

import json
import os
import time

from helpers import TicketdTestCase


class _Lark:
    """记录发卡/改卡调用的假发送器。"""

    def __init__(self):
        self.created = []
        self.patched = []
        self.fail = None  # (info, permanent)

    def create(self, reply_to, card, idem):
        if self.fail:
            return (False,) + self.fail
        self.created.append((reply_to, card, idem))
        return True, "om_card_%d" % len(self.created), False

    def patch(self, mid, card):
        if self.fail:
            return (False,) + self.fail
        self.patched.append((mid, card))
        return True, "", False


class CardTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import card, core, outbox
        self.card, self.core, self.outbox = card, core, outbox
        self.lark = _Lark()

    def refresh(self, now=None):
        self.card.refresh(self.con, self.lark.create, self.lark.patch, now=now)

    def t(self, tid):
        return self.core.get(self.con, tid)

    def texts(self, card):
        return [e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "div"]

    def run_state(self, tid, started):
        self.con.execute("UPDATE tickets SET state='running', started_at=? WHERE id=?", (started, tid))
        self.con.execute("UPDATE inputs SET status='delivered' WHERE ticket_id=?", (tid,))

    # ---------- 发卡 ----------
    def test_new_ticket_gets_card_replying_origin_and_bound(self):
        tid = self.new_ticket()
        self.refresh()
        reply_to, c, idem = self.lark.created[0]
        self.assertEqual(reply_to, "om_1")
        self.assertTrue(idem.startswith("ticketd-card-T0001-"))
        self.assertLessEqual(len(idem), 50)
        self.assertEqual(c["schema"], "2.0")
        self.assertTrue(c["config"]["update_multi"])
        self.assertIn("排队中", c["header"]["subtitle"]["content"])
        self.assertEqual(self.t(tid)["card_message_id"], "om_card_1")
        self.assertEqual(self.core.lookup(self.con, "om_card_1"), tid)  # 引用卡片可以追问
        self.refresh()
        self.assertEqual(len(self.lark.created), 1)
        self.assertEqual(self.lark.patched, [])  # 没有变化就不刷

    def test_untrusted_text_stays_plain(self):
        tid = self.new_ticket()
        self.refresh()
        self.run_state(tid, time.time())
        self.core.add_report(self.con, tid, "progress", "<at id=all></at> **粗**")
        self.refresh(now=time.time() + 20)
        c = self.lark.patched[-1][1]
        for e in c["body"]["elements"]:
            if e.get("tag") == "div":
                self.assertEqual(e["text"]["tag"], "plain_text")
        self.assertTrue(any("<at id=all>" in x for x in self.texts(c)))
        self.assertNotIn("markdown", json.dumps(c))

    def test_legacy_ticket_gets_no_card(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
        self.refresh()
        self.assertEqual(self.lark.created, [])

    def test_finished_before_card_never_gets_late_card(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (tid,))
        self.con.execute("UPDATE inputs SET status='delivered'")
        self.refresh()
        self.assertEqual(self.lark.created, [])
        self.assertEqual(self.t(tid)["card_wanted"], 0)

    def test_send_failure_backoff_then_permanent_gives_up(self):
        tid = self.new_ticket()
        self.lark.fail = ("boom", False)
        now = time.time()
        self.refresh(now)
        t = self.t(tid)
        self.assertEqual((t["card_attempts"], t["card_wanted"]), (1, 1))
        self.assertGreater(t["card_retry_at"], now)
        self.lark.fail = ("bad card", True)
        self.refresh(t["card_retry_at"] + 1)
        self.assertEqual(self.t(tid)["card_wanted"], 0)
        alerts = (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8")
        self.assertIn("状态卡发送失败，已放弃", alerts)

    # ---------- 和结果消息的先后 ----------
    def test_result_waits_for_card_then_overwrites_it(self):
        tid = self.new_ticket()
        self.core.add_report(self.con, tid, "ready", "好了")
        self.outbox.flush(self.con)
        self.assertFalse((self.tmp / "lark.log").exists())  # 卡片没发出去之前，结果先等着
        self.card.refresh(self.con)  # 真实发送器（fake lark）
        self.outbox.flush(self.con)
        sent = [json.loads(x) for x in (self.tmp / "lark.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(sent), 2)
        self.assertIn("interactive", sent[0])  # 状态卡
        self.assertEqual(sent[1][:3], ["api", "PATCH", "/open-apis/im/v1/messages/om_fake_1"])  # 结果覆盖到状态卡上
        body = json.loads(sent[1][sent[1].index("--data") + 1])
        result = json.loads(body["content"])
        self.assertEqual(result["config"]["summary"]["content"], "✅ T0001 完成：好了")
        self.assertTrue(result["config"]["update_multi"])

    def test_result_not_held_forever_by_missing_card(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET created_at=? WHERE id=?", (time.time() - 3600, tid))
        self.core.add_report(self.con, tid, "ready", "好了")
        self.outbox.flush(self.con)
        self.assertEqual(self.con.execute("SELECT status FROM outbox").fetchone()["status"], "sent")

    # ---------- 刷新节奏 ----------
    def test_refresh_on_milestones_and_heartbeat_only(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.refresh(t0 + 1)  # 状态变了，但离上次不到最小间隔
        self.assertEqual(len(self.lark.patched), 0)
        self.refresh(t0 + 11)
        self.assertEqual(len(self.lark.patched), 1)
        self.assertIn("进行中", self.lark.patched[-1][1]["header"]["subtitle"]["content"])
        self.con.execute("UPDATE tickets SET last_action_at=?, last_step=? WHERE id=?",
                         (t0 + 30, "跑全部单测", tid))
        self.refresh(t0 + 40)  # 只有最近一步变了：等心跳
        self.assertEqual(len(self.lark.patched), 1)
        self.refresh(t0 + 11 + 150)
        self.assertEqual(len(self.lark.patched), 2)
        self.assertTrue(any(x.startswith("最近：跑全部单测 · ") for x in self.texts(self.lark.patched[-1][1])))
        self.core.add_report(self.con, tid, "progress", "对抗审查")
        self.refresh(t0 + 11 + 165)  # 阶段切换：立即刷新
        self.assertEqual(len(self.lark.patched), 3)
        c = self.lark.patched[-1][1]
        self.assertIn("● 对抗审查", self.texts(c))
        self.assertIn("对抗审查", c["config"]["summary"]["content"])

    def flush(self):
        self.outbox.flush(self.con, card_patcher=self.lark.patch)

    def pushed(self):
        """发件箱作为新消息发出的内容（fake lark 记录；覆盖状态卡走的是 self.lark.patch）。"""
        p = self.tmp / "lark.log"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_result_overwrites_status_card(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "progress", "写代码")
        self.refresh(t0 + 20)
        n = len(self.lark.patched)
        self.core.add_report(self.con, tid, "ready", "做好了", ["结果.md"])
        self.con.execute("UPDATE reports SET created_at=? WHERE status='ready'", (t0 + 30,))
        self.refresh(t0 + 45)  # 结果还在发件箱：先不刷卡，免得闪一下
        self.assertEqual(len(self.lark.patched), n)
        self.flush()
        self.assertEqual(self.pushed(), [])  # 不另发结果消息
        mid, c = self.lark.patched[-1]
        self.assertEqual(mid, "om_card_1")
        self.assertEqual(c["header"]["template"], "green")
        self.assertIn("✅ 完成", c["header"]["subtitle"]["content"])
        self.assertIn("做好了", self.texts(c))
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual((row["status"], row["sent_message_id"]), ("sent", "om_card_1"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)  # 没有问题要问：不提醒
        t = self.t(tid)
        self.assertEqual((t["card_closed"], t["card_round"]), (1, t0))
        self.assertEqual(self.card.view(self.con, t, t0 + 46)["form"], "result")
        n = len(self.lark.patched)
        self.refresh(t0 + 46)  # 结果已经在卡上：之后不再改动
        self.con.execute("UPDATE tickets SET state='idle', updated_at=? WHERE id=?", (t0 + 600, tid))
        self.refresh(t0 + 700)
        self.refresh(t0 + 4000)
        self.assertEqual(len(self.lark.patched), n)

    def fold(self, tid, t0):
        """跑一轮并交 ready、结果覆盖到状态卡上。"""
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "ready", "第一版")
        self.flush()
        self.refresh(t0 + 40)
        self.assertEqual(self.t(tid)["card_closed"], 1)

    def test_second_final_in_same_round_overwrites_again_and_pings(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.fold(tid, t0)
        n = len(self.lark.patched)
        self.core.add_report(self.con, tid, "blocked", "还缺一步", ask="可以吗？")
        self.flush()
        self.assertEqual(len(self.lark.patched), n + 1)
        self.assertIn("需要你决定", self.lark.patched[-1][1]["header"]["subtitle"]["content"])
        self.assertEqual(self.pushed(), [])
        self.flush()  # 需要用户回答：另推一行文字提醒（覆盖卡片不会推送提醒）
        sent = self.pushed()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:2], ["im", "+messages-reply"])
        self.assertEqual(sent[0][sent[0].index("--text") + 1],
                         "⚠️ T0001 需要你决定：可以吗？\n详情已更新在上方状态卡，引用这条回复即可")
        self.assertEqual(self.core.lookup(self.con, "om_fake_1"), tid)  # 引用提醒可以回答
        self.refresh(t0 + 60)
        self.con.execute("UPDATE tickets SET state='idle', updated_at=? WHERE id=?", (t0 + 61, tid))
        self.refresh(t0 + 100)  # 进程退出不改变卡上的结果，不再刷
        self.assertEqual(len(self.lark.patched), n + 1)

    def test_ping_rules(self):
        from ticketd import templates
        self.assertIsNone(templates.ping_message("T1", "ready", "好了", None))
        self.assertIsNone(templates.ping_message("T1", "dropped", "不属于本单", "原因"))
        self.assertIsNone(templates.ping_message("T1", "failed", "做不到", None))
        self.assertTrue(templates.ping_message("T1", "ready", "好了", "要不要顺便改 B？").startswith(
            "✅ T1 完成 · 👉 需要你：要不要顺便改 B？\n"))

    def test_blocked_without_ask_pings_with_summary(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "blocked", "缺 ~/x 的写权限")
        self.flush()
        self.flush()
        sent = self.pushed()
        self.assertEqual(sent[0][sent[0].index("--text") + 1].splitlines()[0], "⚠️ T0001 需要你决定：缺 ~/x 的写权限")

    def test_patch_permanent_failure_falls_back_to_new_message(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "blocked", "还缺一步", ask="可以吗？")
        self.lark.fail = ("message deleted", True)
        self.flush()
        sent = self.pushed()
        self.assertEqual(len(sent), 1)
        self.assertIn("interactive", sent[0])  # 结果卡作为新消息发出（自带提醒，不再另推）
        self.flush()
        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual(self.t(tid)["card_wanted"], 0)
        self.assertIn("结果覆盖状态卡失败", (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8"))

    def test_patch_transient_failure_retries_on_card(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "ready", "好了")
        self.lark.fail = ("timeout", False)
        self.flush()
        row = self.con.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertEqual(self.pushed(), [])
        self.lark.fail = None
        self.con.execute("UPDATE outbox SET next_at=0")
        self.flush()
        self.assertEqual(self.con.execute("SELECT status FROM outbox").fetchone()["status"], "sent")
        self.assertEqual(self.pushed(), [])
        self.assertIn("✅ 完成", self.lark.patched[-1][1]["header"]["subtitle"]["content"])

    def test_trailing_progress_does_not_hide_result(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "ready", "好了")
        self.core.add_report(self.con, tid, "progress", "收尾")
        self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (tid,))
        self.flush()
        self.refresh(t0 + 40)
        self.assertIn("✅ 完成", self.lark.patched[-1][1]["header"]["subtitle"]["content"])
        self.assertIn("好了", self.texts(self.lark.patched[-1][1]))

    def test_recovery_round_gets_no_new_card(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.fold(tid, t0)
        self.core.add_input(self.con, tid, "recovery", "服务重启")
        self.con.execute("UPDATE inputs SET status='delivered'")
        self.con.execute("UPDATE tickets SET state='running', started_at=? WHERE id=?", (t0 + 100, tid))
        self.refresh(t0 + 400)
        self.assertEqual(len(self.lark.created), 1)

    def test_round_card_failure_skips_only_that_round(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.fold(tid, t0)
        self.core.send_followup(self.con, tid, "om_2", "再改")
        self.run_state(tid, t0 + 100)
        self.lark.fail = ("bad", True)
        self.refresh(t0 + 300)
        t = self.t(tid)
        self.assertEqual((t["card_wanted"], t["card_round"]), (1, t0 + 100))
        self.lark.fail = None
        self.refresh(t0 + 400)  # 这一轮不再重试
        self.assertEqual(len(self.lark.created), 1)
        self.core.send_followup(self.con, tid, "om_3", "再改")
        self.run_state(tid, t0 + 500)
        self.refresh(t0 + 700)  # 下一轮照常
        self.assertEqual(self.lark.created[-1][0], "om_3")

    def test_unsent_result_written_on_card(self):
        tid = self.new_ticket()
        self.refresh()
        self.run_state(tid, time.time())
        self.core.add_report(self.con, tid, "dropped", "复现不了")  # 不带 --ask：不推消息
        self.core.touch(self.con, tid, state="idle")
        self.refresh(time.time() + 20)
        c = self.lark.patched[-1][1]
        self.assertEqual(c["header"]["template"], "grey")
        self.assertIn("⏭️ 无需处理", c["header"]["subtitle"]["content"])
        self.assertIn("复现不了", self.texts(c))
        self.assertEqual(self.t(tid)["card_closed"], 1)

    def test_short_followup_round_only_pushes_result(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.con.execute("UPDATE tickets SET card_closed=1 WHERE id=?", (tid,))  # 上一轮的卡已收起
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        self.run_state(tid, t0)
        self.refresh(t0 + 60)
        self.assertEqual(len(self.lark.created), 1)
        self.core.add_report(self.con, tid, "ready", "改好了")
        self.refresh(t0 + 200)  # 已有结果：不再补发状态卡
        self.assertEqual(len(self.lark.created), 1)
        n = len(self.lark.patched)
        self.flush()  # 上一轮的卡已收起（显示上一轮的结果）：这一轮的结果作为新消息回复追问
        self.assertEqual(len(self.lark.patched), n)
        sent = self.pushed()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][sent[0].index("--message-id") + 1], "om_2")
        self.assertIn("interactive", sent[0])

    def test_long_followup_round_gets_own_card(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.con.execute("UPDATE tickets SET card_closed=1 WHERE id=?", (tid,))
        self.core.send_followup(self.con, tid, "om_2", "再改一下")
        self.run_state(tid, t0)
        self.refresh(t0 + 121)
        self.assertEqual(len(self.lark.created), 2)
        reply_to, c, idem = self.lark.created[-1]
        self.assertEqual(reply_to, "om_2")  # 挂在这一轮的追问下面
        self.assertIn("进行中", c["header"]["subtitle"]["content"])
        t = self.t(tid)
        self.assertEqual((t["card_message_id"], t["card_closed"]), ("om_card_2", 0))
        self.assertEqual(self.core.lookup(self.con, "om_card_2"), tid)
        self.refresh(t0 + 200)
        self.assertEqual(len(self.lark.created), 2)
        self.core.add_report(self.con, tid, "ready", "改好了")
        self.flush()  # 结果覆盖到这一轮的卡上
        self.assertEqual(self.pushed(), [])
        self.assertEqual(self.lark.patched[-1][0], "om_card_2")

    def test_show_and_ls_reflect_report_before_process_exits(self):
        import argparse
        from ticketd import cli
        tid = self.new_ticket()
        t0 = time.time() - 300
        self.run_state(tid, t0)
        self.core.add_report(self.con, tid, "progress", "写核心代码")
        self.con.execute("UPDATE tickets SET last_action_at=?, last_step=? WHERE id=?",
                         (t0 + 200, "调用 Bash：pytest", tid))
        out = cli.cmd_show(self.con, argparse.Namespace(ticket=tid))
        self.assertIn("状态：running（🔄 进行中）  用时 5 分钟", out)
        self.assertIn("阶段：● 写核心代码", out)
        self.assertIn("最近一步：调用 Bash：pytest", out)
        self.core.add_report(self.con, tid, "ready", "好了")
        out = cli.cmd_show(self.con, argparse.Namespace(ticket=tid))
        self.assertIn("📮 已汇报，正在发送结果", out)
        self.assertIn("📮 已汇报，正在发送结果", cli.cmd_ls(self.con, None))

    def test_unobserved_round_not_called_stalled(self):
        tid = self.new_ticket()
        self.run_state(tid, time.time() - 3600)  # 老版本守护进程跑的轮次：没有 last_action_at
        v = self.card.view(self.con, self.t(tid), time.time())
        self.assertFalse(v["stalled"])
        self.assertIn("进行中", v["label"])

    def test_stalled_view(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.run_state(tid, t0)
        self.con.execute("UPDATE tickets SET last_action_at=? WHERE id=?", (t0 + 5, tid))
        v = self.card.view(self.con, self.t(tid), t0 + 5 + 600)
        self.assertTrue(v["stalled"])
        self.assertEqual(v["color"], "orange")
        self.refresh(t0 + 5 + 600)
        c = self.lark.patched[-1][1]
        last = [e for e in c["body"]["elements"] if e.get("tag") == "div" and "没有新动作" in e["text"]["content"]]
        self.assertEqual(last[0]["text"]["text_color"], "red")
        self.assertIn("⚠️ 已 10 分钟没有新动作", last[0]["text"]["content"])

    def test_patch_permanent_failure_stops_refreshing(self):
        tid = self.new_ticket()
        self.refresh()
        self.run_state(tid, time.time())
        self.lark.fail = ("message deleted", True)
        self.refresh(time.time() + 20)
        self.assertEqual(self.t(tid)["card_wanted"], 0)

    def test_one_bad_ticket_does_not_block_others(self):
        a = self.new_ticket(message="om_a")
        b = self.new_ticket(message="om_b")
        orig = self.lark.create

        def create(reply_to, card, idem):
            if reply_to == "om_a":
                raise RuntimeError("坏卡")
            return orig(reply_to, card, idem)

        self.card.refresh(self.con, create, self.lark.patch)
        self.assertIsNone(self.t(a)["card_message_id"])
        self.assertEqual(self.t(b)["card_message_id"], "om_card_1")


    def test_network_failure_stops_this_round(self):
        a = self.new_ticket(message="om_a")
        b = self.new_ticket(message="om_b")
        self.lark.fail = ('rc=4 {"type": "network", "retryable": true}', False)
        self.refresh()
        self.assertEqual(self.t(a)["card_attempts"], 1)
        self.assertEqual(self.t(b)["card_attempts"], 0)  # 没再浪费 30 秒去试第二张

    def test_missing_message_id_retries_with_same_key(self):
        tid = self.new_ticket()
        self.lark.create = lambda reply_to, card, idem: (True, "", False)
        self.refresh()
        t = self.t(tid)
        self.assertEqual((t["card_wanted"], t["card_attempts"]), (1, 1))

    def test_patch_gives_up_after_edit_window(self):
        tid = self.new_ticket()
        self.refresh()
        self.run_state(tid, time.time())
        self.con.execute("UPDATE tickets SET card_sent_at=? WHERE id=?", (time.time() - 15 * 86400, tid))
        self.lark.fail = ("boom", False)
        self.refresh(time.time() + 20)
        self.assertEqual(self.t(tid)["card_wanted"], 0)

    def test_finished_unchanged_ticket_not_reselected(self):
        tid = self.new_ticket()
        t0 = time.time()
        self.refresh(t0)
        self.core.touch(self.con, tid, state="stopped")
        self.refresh(t0 + 20)
        n = len(self.lark.patched)
        self.con.execute("UPDATE tickets SET updated_at=? WHERE id=?", (t0 + 30, tid))  # 只是 pid 之类变了
        self.refresh(t0 + 40)
        self.assertEqual(len(self.lark.patched), n)
        self.assertEqual(self.t(tid)["card_at"], t0 + 40)
        rows = self.con.execute("SELECT id FROM tickets WHERE card_wanted=1 AND updated_at>card_at").fetchall()
        self.assertEqual(rows, [])


class LarkCommandTest(TicketdTestCase):
    def sent(self):
        return [json.loads(x) for x in (self.tmp / "lark.log").read_text(encoding="utf-8").splitlines()]

    def test_send_and_patch_commands(self):
        from ticketd import outbox
        card = {"schema": "2.0", "body": {"elements": []}}
        ok, mid, _ = outbox.send_card("om_1", card, "k1")
        self.assertTrue(ok)
        ok, _, _ = outbox.patch_card(mid, card)
        self.assertTrue(ok)
        a, b = self.sent()
        self.assertEqual(a[:4], ["im", "+messages-reply", "--as", "bot"])
        self.assertEqual(a[a.index("--msg-type") + 1], "interactive")
        self.assertEqual(json.loads(a[a.index("--content") + 1]), card)
        self.assertEqual(b[:5], ["api", "PATCH", "/open-apis/im/v1/messages/" + mid, "--as", "bot"])
        self.assertEqual(json.loads(json.loads(b[b.index("--data") + 1])["content"]), card)

    def test_patch_rejects_bad_message_id(self):
        from ticketd import outbox
        ok, info, permanent = outbox.patch_card("om_x/../../chats", {})
        self.assertEqual((ok, permanent), (False, True))
        self.assertFalse((self.tmp / "lark.log").exists())

    def test_migration_adds_columns_to_old_db(self):
        import sqlite3
        from ticketd import db
        p = self.tmp / "old.db"
        old = sqlite3.connect(str(p))
        old.executescript(db.SCHEMA)
        old.close()
        con = db.connect(p)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tickets)")}
        con.close()
        self.assertTrue({"card_wanted", "card_message_id", "last_action_at", "stall_alerted"} <= cols)
        db.connect(p).close()  # 重复迁移不报错


class SpoolClaimTest(TicketdTestCase):
    def test_undeletable_report_ingested_once(self):
        from ticketd import core
        tid = self.new_ticket()
        spool = self.tmp / "spool"
        core.write_spool(str(spool), "ready", "好了")
        real_unlink = os.unlink

        def no_unlink(*a, **kw):
            raise OSError("read-only")

        os.unlink = no_unlink
        try:
            core.ingest_spool(self.con, tid, str(spool))
            core.ingest_spool(self.con, tid, str(spool))
        finally:
            os.unlink = real_unlink
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)

    def test_limit_marks_rest_bad(self):
        from ticketd import core
        tid = self.new_ticket()
        spool = self.tmp / "spool"
        for i in range(3):
            core.write_spool(str(spool), "progress", "阶段%d" % i)
        self.assertEqual(core.ingest_spool(self.con, tid, str(spool), limit=1), 1)
        self.assertEqual(len(list(spool.glob("*.bad"))), 2)


class SchedulerCardTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def state(self, tid):
        return self.core.get(self.con, tid)["state"]

    def test_progress_and_activity_recorded(self):
        tid = self.new_ticket(request="[[fake:progress]]")
        self.wait(lambda: self.state(tid) == "idle", self.sched)
        t = self.core.get(self.con, tid)
        self.assertEqual(t["last_step"], "运行 echo")
        self.assertIsNotNone(t["last_action_at"])
        self.assertEqual(self.core.last_progress(self.con, tid)["summary"], "写核心代码")
        texts = [r["text"] for r in self.con.execute("SELECT text FROM outbox")]
        self.assertEqual(len(texts), 1)  # progress 不发消息
        self.assertTrue(texts[0].startswith("✅ T0001 完成：分阶段完成"))

    def test_report_delivered_while_process_lingers_then_reaped(self):
        os.environ["TICKETD_LINGER_GRACE"] = "1"
        tid = self.new_ticket(request="[[fake:linger]]")
        # 汇报在进程还活着时就进了发件箱（不再等进程退出）
        self.wait(lambda: self.con.execute("SELECT 1 FROM outbox").fetchone() is not None, self.sched)
        self.assertEqual(self.state(tid), "running")
        start = time.time()
        self.wait(lambda: self.state(tid) == "idle", self.sched, timeout=20)
        self.assertLess(time.time() - start, 20)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        from pathlib import Path
        log = (Path(self.core.get(self.con, tid)["workdir"]) / "过程.log").read_text(encoding="utf-8")
        self.assertIn("已汇报且本轮已结束", log)

    def test_stall_alert_once(self):
        os.environ["TICKETD_STALL_SECONDS"] = "1"
        tid = self.new_ticket(request="[[fake:stall]]")
        self.wait(lambda: self.core.get(self.con, tid)["stall_alerted"] == 1, self.sched)
        for _ in range(15):
            self.sched.tick()
            time.sleep(0.1)
        rows = self.con.execute("SELECT text FROM outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("疑似卡住", rows[0]["text"])
        self.assertIn("最近：运行 sleep", rows[0]["text"])
        self.assertEqual(self.state(tid), "running")  # 只提醒，不杀
        # 纯文字输出不算新动作，不会解除提醒、再推一次
        r = self.sched.running[tid]
        r.last_step, r.activity_dirty = "说：还在想", True
        for _ in range(5):
            self.sched.tick()
            time.sleep(0.1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.core.stop(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)

    def test_stall_shown_on_live_card_not_pushed(self):
        os.environ["TICKETD_STALL_SECONDS"] = "1"
        tid = self.new_ticket(request="[[fake:stall]]")
        self.con.execute("UPDATE tickets SET card_message_id='om_card_1', card_sent_at=? WHERE id=?",
                         (time.time(), tid))
        self.wait(lambda: self.core.get(self.con, tid)["stall_alerted"] == 1, self.sched)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.state(tid), "running")
        self.core.stop(self.con, tid)
        self.wait(lambda: tid not in self.sched.running, self.sched)

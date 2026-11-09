"""运营指标：成本落库、紧急事件私信、ticket stats、每周概况。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from helpers import TicketdTestCase


class CostTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def idle(self, tid):
        self.wait(lambda: self.core.get(self.con, tid)["state"] == "idle" and tid not in self.sched.running, self.sched)

    def rounds(self, tid):
        return [(round(r["cost_usd"], 2), r["outcome"]) for r in
                self.con.execute("SELECT * FROM rounds WHERE ticket_id=? ORDER BY id", (tid,))]

    def test_session_cumulative_cost_becomes_round_cost(self):
        os.environ["FAKE_COST"] = "1.5"
        tid = self.new_ticket()
        self.idle(tid)
        os.environ["FAKE_COST"] = "4.0"  # 同一会话的累计值
        self.core.send_followup(self.con, tid, "om_2", "再改 [[fake:ready]]")
        self.wait(lambda: len(self.rounds(tid)) == 2, self.sched)
        self.idle(tid)
        t = self.core.get(self.con, tid)
        self.assertEqual(self.rounds(tid), [(1.5, "ready"), (2.5, "ready")])
        self.assertAlmostEqual(t["cost_usd"], 4.0)
        self.assertAlmostEqual(t["session_cost"], 4.0)
        # 换新会话：累计值从头算
        self.core.restart(self.con, tid)
        self.assertEqual(self.core.get(self.con, tid)["session_cost"], 0)
        os.environ["FAKE_COST"] = "0.7"
        self.wait(lambda: len(self.rounds(tid)) == 3, self.sched)
        self.idle(tid)
        self.assertEqual(self.rounds(tid)[-1], (0.7, "ready"))
        self.assertAlmostEqual(self.core.get(self.con, tid)["cost_usd"], 4.7)
        # 实时记账和从原始 stream 重算的一致（跨了续做和换会话）
        from ticketd import metrics
        self.assertEqual(metrics.reconcile_costs(self.con), (1, []))
        self.con.execute("UPDATE tickets SET cost_usd=9 WHERE id=?", (tid,))  # 人为记错
        checked, bad = metrics.reconcile_costs(self.con)
        self.assertEqual(bad[0][0], tid)

    def test_failed_round_recorded(self):
        tid = self.new_ticket(request="[[fake:noreport]]")
        self.wait(lambda: len(self.rounds(tid)) == 1, self.sched)
        self.assertEqual(self.rounds(tid)[0][1], "failed")


class NotifyTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, metrics, outbox
        self.core, self.metrics, self.outbox = core, metrics, outbox

    def owner_rows(self):
        return self.con.execute("SELECT * FROM outbox WHERE ticket_id=?", (self.metrics.SYSTEM_TICKET,)).fetchall()

    def test_dead_result_pushes_owner_once_per_hour(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))  # 不等状态卡，结果直接发
        self.core.add_report(self.con, tid, "ready", "好了")
        fail = self.tmp / "fail"
        fail.write_text("10")  # 重试满 24 小时、最后的文字兜底也失败：这一行彻底没送达
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        self.con.execute("UPDATE outbox SET created_at=?", (time.time() - 25 * 3600,))
        self.outbox.flush(self.con)
        os.environ.pop("FAKE_LARK_FAIL_FILE")
        self.assertEqual(self.con.execute("SELECT status FROM outbox WHERE ticket_id=?", (tid,)).fetchone()[0], "dead")
        rows = self.owner_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reply_to"], self.metrics.OWNER)  # 收件人由守护进程发送时解析
        self.assertIn("结果没送达", rows[0]["text"])
        self.assertIn("ticket show %s" % tid, rows[0]["text"])
        # 私信这条用 --user-id 发出去
        self.outbox.flush(self.con)
        self.assertEqual(self.owner_rows()[0]["status"], "sent")
        self.assertEqual(self.owner_rows()[0]["reply_to"], "ou_owner")  # 发送时按本进程的配置解析成主用户
        sent = [json.loads(x) for x in (self.tmp / "lark.log").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("--user-id" in a and "ou_owner" in a for a in sent))
        # 一小时内同一类只推一次，其余只落库
        self.metrics.notify(self.con, tid, "delivery_dead", "又一条")
        self.assertEqual(len(self.owner_rows()), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE kind='delivery_dead'").fetchone()[0], 2)

    def test_no_owner_no_push(self):
        os.environ["TICKETD_OWNER"] = ""
        self.metrics.notify(self.con, "T0001", "media_stuck", "卡了")
        self.assertEqual(self.owner_rows(), [])


class StatsTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, metrics
        self.core, self.metrics = core, metrics

    def test_stats_summarizes_week(self):
        now = time.time()
        a = self.new_ticket(title="生图提示词")
        b = self.new_ticket(message="om_2", title="推送")
        ins = "INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)"
        self.con.execute(ins, (a, now - 100, now - 50, 6.0, "ready"))
        self.con.execute(ins, (a, now - 40, now - 30, 7.5, "blocked"))
        self.con.execute(ins, (b, now - 20, now - 10, 0.5, "failed"))
        self.con.execute(ins, (b, now - 20 * 86400, now - 20 * 86400, 99.0, "ready"))  # 窗口外
        self.con.execute("UPDATE tickets SET session_cost=40 WHERE id=?", (a,))
        self.core.add_report(self.con, a, "blocked", "要推送", run={"path": "push.sh", "script": "git push\n"})
        self.core.add_report(self.con, b, "failed", "模型接口余额不足")
        self.core.send_followup(self.con, a, "om_9", "可以")
        self.metrics.record(self.con, a, "stall_flag")
        text = self.metrics.stats(self.con, 7, now)
        self.assertIn("新开 2 张，跑了 3 轮，worker 成本约 $14", text)
        self.assertIn("最花钱：%s「生图提示词」$14" % a, text)
        self.assertIn("会话已很长", text)
        self.assertIn("申请沙箱外执行 1 次", text)
        self.assertIn("失败 1 次", text)
        self.assertIn("疑似卡住 1", text)
        self.assertIn("你的回复：中位 0 分钟", text)

    def test_context_and_executor_lines(self):  # 审查 C、D
        now = time.time()
        a = self.new_ticket(title="长单")
        b = self.new_ticket(message="om_2", title="codex 单", executor="codex")
        ins = ("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome, ctx_tokens)"
               " VALUES(?,?,?,?,?,?)")
        for row in ((a, now - 50, now - 49, 6.0, "ready", 900000), (a, now - 40, now - 39, 2.0, "compact", 0),
                    (a, now - 30, now - 29, 0.1, "ready", 30000), (b, now - 20, now - 19, 0.0, "ready", 0)):
            self.con.execute(ins, row)
        text = self.metrics.stats(self.con, 7, now)
        self.assertIn("上下文：每轮平均 46 万 token（最大 %s 90 万）" % a, text)
        self.assertIn("自动压缩 1 次（失败 0 次，花 $2.00），压缩前一轮平均 90 万 → 压缩后一轮平均 3 万 token", text)
        self.assertIn("按执行器：Claude 3 轮 $8.10，Codex 1 轮 $0.00（不报成本、不自动压缩）", text)

    def test_backfill_from_stream(self):
        tid = self.new_ticket()
        wd = Path(self.core.get(self.con, tid)["workdir"])
        wd.mkdir(parents=True, exist_ok=True)
        evs = [{"type": "result", "total_cost_usd": c} for c in (1.0, 3.0, 0.5, 2.0)]  # 0.5 = 换了会话
        (wd / ".stream.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        self.assertEqual(self.metrics.backfill_costs(self.con), 1)
        self.assertAlmostEqual(self.core.get(self.con, tid)["cost_usd"], 5.0)
        self.assertEqual(self.metrics.backfill_costs(self.con), 0)  # 可重复执行
        text = self.metrics.stats(self.con, 7, time.time())
        self.assertIn("跑了 4 轮，worker 成本约 $5.00（其中 4 轮是从老工单补算的", text)


class DigestTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import metrics
        self.metrics = metrics

    def sent(self):
        return self.con.execute("SELECT COUNT(*) FROM outbox WHERE ticket_id=?",
                                (self.metrics.SYSTEM_TICKET,)).fetchone()[0]

    def test_sends_once_per_slot_and_not_on_first_run(self):
        os.environ["TICKETD_DIGEST"] = "mon 10:00"
        monday = time.mktime((2026, 11, 9, 10, 30, 0, 0, 0, -1))  # 周一 10:30
        self.assertFalse(self.metrics.maybe_digest(self.con, monday))  # 第一次运行只记下时刻
        self.assertTrue(self.metrics.maybe_digest(self.con, monday + 7 * 86400))
        self.assertFalse(self.metrics.maybe_digest(self.con, monday + 7 * 86400 + 60))
        self.assertEqual(self.sent(), 1)
        self.assertIn("运行概况", self.con.execute("SELECT text FROM outbox").fetchone()[0])

    def test_disabled(self):
        os.environ["TICKETD_DIGEST"] = ""
        self.assertFalse(self.metrics.maybe_digest(self.con, time.time()))
        self.assertEqual(self.sent(), 0)


class CostHintTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core
        self.core = core
        os.environ["TICKETD_COMPACT_TOKENS"] = "0"  # 提示只在关掉自动压缩时出现

    def last(self):
        return self.con.execute("SELECT text, card_json FROM outbox ORDER BY id DESC LIMIT 1").fetchone()

    def test_hint_hidden_when_auto_compaction_on(self):
        os.environ["TICKETD_COMPACT_TOKENS"] = "150000"
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET session_cost=88 WHERE id=?", (tid,))
        self.core.add_report(self.con, tid, "ready", "好了")
        self.assertNotIn("会话已累计", self.last()["card_json"])

    def test_long_session_gets_hint_on_result(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET session_cost=88 WHERE id=?", (tid,))
        self.core.add_report(self.con, tid, "ready", "好了")
        row = self.last()
        self.assertIn("会话已累计约 $88", row["card_json"])
        self.assertIn("换新会话重来 %s" % tid, row["card_json"])
        self.assertIn("会话已累计约 $88", row["text"])
        card = json.loads(row["card_json"])
        self.assertIn("引用此卡可追问", json.dumps(card["body"]["elements"][-1], ensure_ascii=False))  # 页脚仍在最后

    def test_short_session_or_dropped_no_hint(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET session_cost=5 WHERE id=?", (tid,))
        self.core.add_report(self.con, tid, "ready", "好了")
        self.assertNotIn("会话已累计", self.last()["card_json"])
        self.con.execute("UPDATE tickets SET session_cost=88 WHERE id=?", (tid,))
        self.core.add_report(self.con, tid, "dropped", "不属于本单", ask="另一个目标")
        self.assertNotIn("会话已累计", self.last()["card_json"] or "")


class OwnerConfigTest(TicketdTestCase):
    """2026-11-09 实例：config.env 里的 TICKETD_OWNER 是旧应用的 open_id，Claude Code 环境变量里是对的；
    派单正常，守护进程的周报私信报 open_id cross app，静默重试了 51 次。"""

    def doctor(self):
        import contextlib
        import io
        from ticketd import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["doctor"])
        return buf.getvalue()

    def test_doctor_flags_env_and_file_mismatch(self):
        from ticketd import config
        config.home().mkdir(parents=True, exist_ok=True)
        config.config_file().write_text("TICKETD_OWNER=ou_old_app\n", encoding="utf-8")
        os.environ["TICKETD_OWNER"] = "ou_owner"
        out = self.doctor()
        self.assertIn("❌ TICKETD_OWNER：当前环境和 config.env 不一致", out)
        config.config_file().write_text("TICKETD_OWNER=ou_owner\n", encoding="utf-8")
        self.assertNotIn("TICKETD_OWNER：当前环境和 config.env 不一致", self.doctor())

    def test_doctor_flags_missing_owner_in_file(self):
        out = self.doctor()  # 测试环境里 TICKETD_OWNER 只在环境变量里
        self.assertIn("只在当前环境里有", out)
        self.assertIn("config.env 里没有有效的 TICKETD_OWNER", out)

    def test_cross_app_dm_fails_fast_with_hint(self):
        from ticketd import metrics, outbox
        metrics.send_owner(self.con, "周报")
        fail = self.tmp / "fail"
        fail.write_text("5")
        os.environ.update({"FAKE_LARK_FAIL_FILE": str(fail), "FAKE_LARK_FAIL_JSON": json.dumps(
            {"type": "api", "code": 99992361, "message": "open_id cross app"})})
        outbox.flush(self.con)
        row = self.con.execute("SELECT status, attempts FROM outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("dead", 1))  # 不再静默重试一整天
        alerts = (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8")
        self.assertIn("私信主用户失败", alerts)
        self.assertIn("ticket doctor", alerts)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)  # 不嵌套通知


class DaemonViewOwnerTest(TicketdTestCase):
    """收件人在发送时按发送进程（守护进程）的配置解析，不用放进发件箱那一刻的配置。"""

    def test_owner_resolved_at_send_time(self):
        from ticketd import metrics, outbox
        rid = metrics.send_owner(self.con, "测试")
        os.environ["TICKETD_OWNER"] = "ou_daemon_view"  # 发送时的配置和放进来时不同
        outbox.flush(self.con)
        row = self.con.execute("SELECT status, reply_to FROM outbox WHERE id=?", (rid,)).fetchone()
        self.assertEqual((row["status"], row["reply_to"]), ("sent", "ou_daemon_view"))

    def test_no_owner_at_send_time_dies_with_hint(self):
        from ticketd import metrics, outbox
        rid = metrics.send_owner(self.con, "测试")
        os.environ["TICKETD_OWNER"] = ""
        outbox.flush(self.con)
        self.assertEqual(self.con.execute("SELECT status FROM outbox WHERE id=?", (rid,)).fetchone()[0], "dead")
        self.assertIn("ticket doctor", (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8"))


class WorkspaceDriftTest(TicketdTestCase):
    def test_drift_detected_and_cleared(self):
        from ticketd import cli, config
        ws = self.tmp / "ws"
        ws.mkdir()
        os.environ["TICKETD_WORKSPACE"] = str(ws)
        want = (config.REPO / "workspace" / "CLAUDE.md.tmpl").read_text(encoding="utf-8").replace(
            "@LARKYARD@", str(config.REPO))
        (ws / "CLAUDE.md").write_text(want.replace("ticket stats", "ticket ls"), encoding="utf-8")  # 落后的旧版
        self.assertTrue(cli._workspace_drift()[0].startswith("❌ 主会话约定"))
        (ws / "CLAUDE.md").write_text(want, encoding="utf-8")
        self.assertEqual(cli._workspace_drift(), ["✅ 主会话约定和本仓库的模板一致"])


class SetupWorkspaceTest(TicketdTestCase):
    def test_local_file_has_no_example_rules(self):
        import subprocess
        from helpers import REPO
        ws = self.tmp / "ws"
        ws.mkdir()
        p = subprocess.run(["/bin/sh", str(REPO / "scripts" / "setup-workspace.sh"), str(ws)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        local = (ws / "CLAUDE.local.md").read_text(encoding="utf-8")
        self.assertNotIn("（例）", local)  # 示例句不进个人覆盖，免得主会话当真
        self.assertIn("CLAUDE.local.example.md", local)
        self.assertIn(str(REPO) + "/bin/ticket", (ws / "CLAUDE.md").read_text(encoding="utf-8"))


class BridgeCostTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import metrics
        self.metrics = metrics
        home = Path(os.environ["LARK_CHANNEL_HOME"])
        logs = home / "profiles" / "p1" / "logs"
        logs.mkdir(parents=True)
        (home / "config.json").write_text(json.dumps({"activeProfile": "p1"}))
        day = time.strftime("%Y%m%d")
        evs = [{"event": "enter", "chatId": "oc_a", "chatType": "p2p"},
               {"event": "usage", "chatId": "oc_a", "costUsd": 3.0},
               {"event": "usage", "chatId": "oc_a", "costUsd": 35.0},
               {"event": "enter", "chatId": "oc_b", "chatType": "group"},
               {"event": "usage", "chatId": "oc_b", "costUsd": 0.5}]
        (logs / ("bridge-%s.jsonl" % day)).write_text("\n".join(json.dumps(e) for e in evs) + "\nnot json\n")

    def test_latest_cost_per_chat(self):
        got = self.metrics.bridge_costs(time.time() - 86400)
        self.assertEqual(got, [("私聊", 35.0), ("群聊 1", 0.5)])

    def test_stats_suggests_new_over_threshold(self):
        text = self.metrics.stats(self.con, 7)
        self.assertIn("主会话当前累计：私聊 $35，群聊 1 $0.50", text)
        self.assertIn("发 /new", text)
        os.environ["TICKETD_COST_HINT_USD"] = "100"
        self.assertNotIn("/new", self.metrics.stats(self.con, 7))

    def test_missing_bridge_logs_is_fine(self):
        os.environ["LARK_CHANNEL_HOME"] = str(self.tmp / "nope")
        self.assertEqual(self.metrics.bridge_costs(0), [])
        self.assertNotIn("主会话", self.metrics.stats(self.con, 7))


class CompactTest(TicketdTestCase):
    """上一轮上下文到阈值：下一轮开始前先压缩会话（2026-11-09 实测 T0005 91 万 → 2.4 万 token）。"""

    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)
        os.environ["TICKETD_COMPACT_TOKENS"] = "150000"

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def argv_log(self, tid):
        p = Path(self.core.get(self.con, tid)["workdir"]) / ".fake_argv.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]

    def idle(self, tid):
        return self.core.get(self.con, tid)["state"] == "idle" and tid not in self.sched.running

    def outcomes(self, tid):
        return [r["outcome"] for r in self.con.execute("SELECT outcome FROM rounds WHERE ticket_id=? ORDER BY id", (tid,))]

    def test_big_context_compacts_before_next_round(self):
        os.environ["FAKE_CTX"] = "200000"
        tid = self.new_ticket()
        self.wait(lambda: self.idle(tid), self.sched)
        t = self.core.get(self.con, tid)
        self.assertEqual(t["ctx_tokens"], 200110)
        self.assertEqual(self.con.execute("SELECT ctx_tokens FROM rounds WHERE ticket_id=?", (tid,)).fetchone()[0], 200110)
        os.environ["FAKE_CTX"] = "30000"
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.outcomes(tid)) == 3 and self.idle(tid), self.sched)
        calls = self.argv_log(tid)
        compact = calls[1]["argv"]
        self.assertTrue(compact[-1].startswith("/compact "))
        self.assertIn("不能再做", compact[-1])  # 带着保留要求
        self.assertEqual(compact[compact.index("--resume") + 1], t["session_id"])
        self.assertEqual(compact[compact.index("--model") + 1], "sonnet")
        self.assertEqual(calls[1]["card"], "")  # 压缩轮不投递任何输入
        self.assertIn("再改一下", calls[2]["card"])  # 追问在压缩之后照常投递
        self.assertEqual(self.outcomes(tid), ["ready", "compact", "ready"])
        self.assertEqual(self.core.get(self.con, tid)["ctx_tokens"], 30110)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE kind='compact'").fetchone()[0], 1)

    def test_small_context_or_off_or_codex_does_not_compact(self):
        os.environ["FAKE_CTX"] = "50000"
        tid = self.new_ticket()
        self.wait(lambda: self.idle(tid), self.sched)
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.outcomes(tid)) == 2 and self.idle(tid), self.sched)
        self.assertNotIn("compact", self.outcomes(tid))
        self.con.execute("UPDATE tickets SET ctx_tokens=900000 WHERE id=?", (tid,))
        os.environ["TICKETD_COMPACT_TOKENS"] = "0"
        self.core.send_followup(self.con, tid, "om_3", "又改 [[fake:ready]]")
        self.wait(lambda: len(self.outcomes(tid)) == 3 and self.idle(tid), self.sched)
        self.assertNotIn("compact", self.outcomes(tid))
        from ticketd.scheduler import Scheduler
        t = dict(self.core.get(self.con, tid))
        t.update(executor="codex", ctx_tokens=900000)
        os.environ["TICKETD_COMPACT_TOKENS"] = "150000"
        self.assertFalse(Scheduler(self.con)._needs_compact(t))

    def test_transient_compaction_failure_retries_compaction_not_the_round(self):  # 审查 A
        os.environ["FAKE_CTX"] = "200000"
        tid = self.new_ticket()
        self.wait(lambda: self.idle(tid), self.sched)
        os.environ.update({"FAKE_COMPACT_FAIL": "API Error: 529 overloaded", "FAKE_COMPACT_FAIL_ONCE": "1"})
        os.environ["FAKE_CTX"] = "30000"
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.outcomes(tid)) == 4 and self.idle(tid), self.sched)
        self.assertEqual(self.outcomes(tid), ["ready", "compact_failed", "compact", "ready"])  # 压缩成功前不续做
        calls = self.argv_log(tid)
        self.assertTrue(calls[1]["argv"][-1].startswith("/compact") and calls[2]["argv"][-1].startswith("/compact"))
        self.assertNotIn("上一轮因网络", calls[3]["card"])  # 压缩轮的重试不往真正的输入里塞「上一轮中断」
        self.assertIn("再改一下", calls[3]["card"])

    def test_balance_during_compaction_waits_instead_of_running_big_context(self):  # 审查 A
        os.environ.update({"TICKETD_OUTAGE_RETRY_HOURS": "24", "TICKETD_OUTAGE_RETRY_EVERY": "3600"})
        os.environ["FAKE_CTX"] = "200000"
        tid = self.new_ticket()
        self.wait(lambda: self.idle(tid), self.sched)
        os.environ["FAKE_COMPACT_FAIL"] = "Failed to authenticate. API Error: 403 insufficient balance"
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.idle(tid), self.sched)
        t = self.core.get(self.con, tid)
        self.assertEqual(t["ctx_tokens"], 200110)  # 保留：恢复后先压缩，再续做
        self.assertEqual(self.outcomes(tid), ["ready", "compact_failed"])
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "waiting")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM inputs WHERE ticket_id=? AND status='pending'",
                                          (tid,)).fetchone()[0], 1)  # 追问还没投递

    def test_failed_compaction_still_runs_the_round(self):
        os.environ["FAKE_CTX"] = "200000"
        tid = self.new_ticket()
        self.wait(lambda: self.idle(tid), self.sched)
        os.environ["FAKE_COMPACT_FAIL"] = "1"
        self.core.send_followup(self.con, tid, "om_2", "再改一下 [[fake:ready]]")
        self.wait(lambda: len(self.outcomes(tid)) == 3 and self.idle(tid), self.sched)
        self.assertEqual(self.outcomes(tid), ["ready", "compact_failed", "ready"])
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        # 审查 B：开着自动压缩、但压缩失败了：结果卡上要说会话仍然很长
        card = self.con.execute("SELECT card_json FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("没能自动压缩", card)
        # 审查 C：压缩轮有自己的开始时间，不和前一轮共用
        rows = self.con.execute("SELECT started_at FROM rounds WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()
        self.assertGreater(rows[1]["started_at"], rows[0]["started_at"])
        from ticketd import cli
        self.assertIn("压缩过 1 次（失败 1 次）", cli._ctx_text(self.con, self.core.get(self.con, tid)))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE kind='compact_failed'").fetchone()[0], 1)

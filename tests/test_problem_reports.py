"""出错记录（清单 c2、ticket console accept、心跳 problems、游标）和诊断包（ticket diag）。
对应 openspec/changes/add-console-problem-reports/specs/ 的各个场景。用 test_console 的假控制台（不起服务、不碰网络）。"""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

from test_console import DEVICE, OWNER, SECRET, TOKEN, URL, ConsoleBase, err
from ticketd import console, diag, metrics, perms, updsig


class ProblemsBase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.fake.routes[("POST", "/api/v1/device/consent")] = \
            lambda body, h: (200, {"ok": True, "consent_version": body["version"]})
        self.fake.routes[("POST", "/api/v1/diagnostics")] = (200, {"ok": True, "id": 7})

    def enrolled_as(self, version):
        console.save({"url": URL, "device_id": DEVICE, "enrolled_at": updsig.iso(time.time()),
                      "consent_version": version}, TOKEN)
        if version == "c2":
            console.set_problems_baseline(self.con)

    def round_(self, tid, outcome, at=None):
        at = time.time() if at is None else at
        self.con.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)",
                         (tid, at - 60, at, 0.1, outcome))

    def accept(self, version="c2", answer="yes"):
        with mock.patch.object(console, "_ask", side_effect=[answer]):
            return console.accept(self.con, version, out=self.say)


class ConsentAcceptTest(ProblemsBase):
    def test_c1_device_sends_no_problems(self):
        """场景：c1 设备不带出错记录。"""
        self.enrolled_as("c1")
        tid = self.new_ticket()
        self.round_(tid, "failed")
        metrics.record(self.con, tid, "model_error", "boom", code="auth")
        console.send_heartbeat(self.con)
        body = self.fake.calls[-1]["body"].decode("utf-8")
        self.assertNotIn("problems", body)
        self.assertNotIn("problems", console.build_heartbeat(self.con))
        code, out = self.cli("console", "status")
        self.assertIn("ticket console accept", out)
        code, out = self.cli("console", "show")
        self.assertIn("ticket console accept", out)
        self.assertNotIn("problems", out)

    def test_accept_upgrades_and_starts_from_now(self):
        """场景：升级到 c2（同意之前的记录不补报）。"""
        self.enrolled_as("c1")
        tid = self.new_ticket()
        self.round_(tid, "failed", time.time() - 100)
        lines = self.accept()
        self.assertIn(console.CONSENT_C2[:-1], self.out)
        self.assertTrue(any("已换成采集清单 c2" in l for l in lines), lines)
        self.assertEqual(self.fake.bodies("/api/v1/device/consent"),
                         [{"version": "c2", "sha256": console.consent_sha256("c2")}])
        self.assertEqual(self.fake.calls[-1]["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(console.consent_version(), "c2")
        self.assertEqual(json.loads(console.info_path().read_text("utf-8"))["consent_version"], "c2")
        self.assertEqual(stat.S_IMODE(console.info_path().stat().st_mode), 0o600)
        self.assertEqual(console.build_heartbeat(self.con)["problems"], {"items": [], "more": 0})
        self.round_(tid, "failed")
        items = console.build_heartbeat(self.con)["problems"]["items"]
        self.assertEqual([(i["ticket"], i["step"], i["code"]) for i in items], [(tid, "round", "failed")])
        self.assertNotIn("usage", console.build_heartbeat(self.con))  # c2 不带用量统计
        self.assertIn("用量统计不上报", console.consent_note(self.con))  # 还提醒一句有更新的 c3

    def test_decline_sends_nothing(self):
        """场景：不同意。"""
        self.enrolled_as("c1")
        lines = self.accept(answer="no")
        self.assertIn("没有同意", lines[0])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(console.consent_version(), "c1")

    def test_refused_outside_terminal_and_in_worker(self):
        self.enrolled_as("c1")
        with mock.patch.object(console, "_stdin_is_tty", return_value=False):
            with self.assertRaises(PermissionError):
                self.accept()
        os.environ["TICKETD_TICKET"] = "T0001"
        with self.assertRaises(PermissionError):
            self.accept()
        code, out = self.cli("console", "accept")
        self.assertEqual(code, 2)
        self.assertIn("worker", out)
        self.assertEqual(self.fake.calls, [])

    def test_console_rejects_keeps_local(self):
        self.enrolled_as("c1")
        self.fake.routes[("POST", "/api/v1/device/consent")] = err("ConsentUnknown")
        with self.assertRaises(ValueError) as cm:
            self.accept()
        self.assertIn("本机清单不变", str(cm.exception))
        self.assertEqual(console.consent_version(), "c1")
        # 控制台回的版本不对也不改
        self.fake.routes[("POST", "/api/v1/device/consent")] = (200, {"ok": True, "consent_version": "c1"})
        with self.assertRaises(ValueError):
            self.accept()
        self.assertEqual(console.consent_version(), "c1")

    def test_back_to_c1_stops_problems(self):
        self.enrolled_as("c2")
        lines = self.accept("c1")
        self.assertIn("不再上报出错记录", lines[0])
        tid = self.new_ticket()
        self.round_(tid, "failed")
        self.assertNotIn("problems", console.build_heartbeat(self.con))

    def test_already_and_unknown_and_not_enrolled(self):
        with self.assertRaises(ValueError):
            self.accept()
        self.enrolled_as("c2")
        self.assertIn("已经是 c2", self.accept()[0])
        with self.assertRaises(ValueError):
            self.accept("c9")
        self.assertEqual(self.fake.calls, [])

    def test_stopped_device_cannot_accept(self):
        self.enrolled_as("c1")
        metrics.kv_set(self.con, "console.state", "stopped:DeviceRevoked")
        with self.assertRaises(ValueError):
            self.accept()
        self.assertEqual(self.fake.calls, [])


class ProblemsTest(ProblemsBase):
    def setUp(self):
        super().setUp()
        self.enrolled_as("c2")
        self.tid = self.new_ticket(request="请处理 %s 的事" % SECRET, title=SECRET)

    def test_only_codes_no_text(self):
        """场景：带特殊字符串的报错。"""
        # 同一张单一次故障只报第一条（T0109）：api_error、outage_start、retry 各放在另一张单上
        t2, t3, t4 = (self.new_ticket(message="om_%d" % n) for n in (2, 3, 4))
        self.round_(self.tid, "failed")
        metrics.record(self.con, self.tid, "model_error", "认证失败 %s" % SECRET, code="auth")
        metrics.record(self.con, t2, "api_error", SECRET, code="weird-code")
        metrics.record(self.con, self.tid, "stall_abort", "卡在 %s" % SECRET)
        metrics.record(self.con, self.tid, "stall_flag", SECRET)                      # 只是提醒：不报
        metrics.record(self.con, t3, "outage_start", SECRET)
        metrics.record(self.con, self.tid, "lark_error", SECRET, code="230001")
        metrics.record(self.con, self.tid, "lark_error", "", code="Rate-Limit!")
        metrics.record(self.con, self.tid, "delivery_dead", SECRET)
        metrics.record(self.con, self.tid, "delivery", "", code="dead")              # 同一次另记 delivery_dead：不重复
        metrics.record(self.con, self.tid, "media", "", code="video:fail")
        metrics.record(self.con, self.tid, "media", "", code="image:ok")
        metrics.record(self.con, metrics.SYSTEM_TICKET, "console", SECRET, code="skills_failed")
        metrics.record(self.con, metrics.SYSTEM_TICKET, "update", SECRET, code="bad_hash")  # 心跳 update 里已有：不报
        self.round_(self.tid, "stopped")      # 用户自己停的：不报
        self.round_(self.tid, "blocked")      # 正常的「要你决定」：不报
        self.round_(t4, "retry")
        console.send_heartbeat(self.con)
        raw = self.fake.calls[-1]["body"]
        self.assertNotIn(SECRET.encode("utf-8"), raw)
        p = json.loads(raw)["problems"]
        got = sorted((i["step"], i["code"], i["ticket"]) for i in p["items"])
        t = self.tid
        self.assertEqual(got, sorted([
            ("round", "failed", t), ("model", "auth", t), ("model", "other", t2), ("stall", "stall_abort", t),
            ("outage", "outage_start", t3), ("feishu", "e230001", t), ("feishu", "other", t),
            ("feishu", "delivery_dead", t), ("media", "video_fail", t), ("skills", "skills_failed", None),
            ("round", "retry", t4)]))
        self.assertEqual(p["more"], 0)
        for i in p["items"]:
            self.assertEqual(set(i), {"at", "ticket", "step", "code"})
            self.assertRegex(i["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertRegex(i["code"], r"^[a-z0-9_]{1,32}$")
            self.assertIn(i["step"], console.PROBLEM_STEPS)
        for leak in (OWNER, DEVICE, TOKEN, str(self.tmp)):
            self.assertNotIn(leak, raw.decode("utf-8"))

    def test_cap_20_and_order(self):
        base = time.time() - 1000
        for n in range(25):
            self.round_(self.tid, "failed", base + n)
        p = console.build_heartbeat(self.con)["problems"]
        self.assertEqual(len(p["items"]), 20)
        self.assertEqual(p["more"], 5)
        ats = [i["at"] for i in p["items"]]
        self.assertEqual(ats, sorted(ats))
        self.assertEqual(ats[-1], updsig.iso(base + 24))   # 留下的是最新的 20 条
        self.assertLessEqual(len(json.dumps(console.build_heartbeat(self.con)).encode()), 8192)

    def test_cursor_only_moves_after_ok(self):
        """场景：控制台暂时不可用。"""
        self.round_(self.tid, "failed")
        metrics.record(self.con, self.tid, "model_error", "", code="network")
        # show 不前移
        code, out = self.cli("console", "show")
        self.assertIn('"problems"', out)
        self.assertEqual(len(console.build_heartbeat(self.con)["problems"]["items"]), 2)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("Unavailable", 503)
        self.assertFalse(console.send_heartbeat(self.con)["ok"])
        # 控制台没确认（ok 不是 true）也不前移
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": False})
        console.send_heartbeat(self.con)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300})
        self.assertTrue(console.send_heartbeat(self.con)["ok"])
        self.assertEqual(len(json.loads(self.fake.calls[-1]["body"])["problems"]["items"]), 2)
        console.send_heartbeat(self.con)
        self.assertEqual(json.loads(self.fake.calls[-1]["body"])["problems"], {"items": [], "more": 0})
        self.round_(self.tid, "ready", time.time() + 100)   # 之后有一轮交上了汇报：上一次故障结束，下面是新的一次
        self.round_(self.tid, "compact_failed", time.time() + 200)
        console.send_heartbeat(self.con)
        self.assertEqual([i["code"] for i in json.loads(self.fake.calls[-1]["body"])["problems"]["items"]],
                         ["compact_failed"])

    def test_event_during_send_is_not_lost(self):
        """构造之后、确认之前新写的记录：id 比上界大，下一次照报。"""
        self.round_(self.tid, "failed")

        def respond(body, h):
            self.round_(self.tid, "retry")
            return 200, {"ok": True, "next_in": 300}
        self.fake.routes[("POST", "/api/v1/heartbeat")] = respond
        console.send_heartbeat(self.con)
        self.assertEqual([i["code"] for i in json.loads(self.fake.calls[-1]["body"])["problems"]["items"]], ["failed"])
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300})
        console.send_heartbeat(self.con)
        self.assertEqual([i["code"] for i in json.loads(self.fake.calls[-1]["body"])["problems"]["items"]], ["retry"])

    def test_missing_or_stale_cursor_starts_from_now(self):
        self.round_(self.tid, "failed")
        self.con.execute("DELETE FROM kv WHERE key=?", (console.CURSOR_KEY,))
        self.assertEqual(console.build_heartbeat(self.con)["problems"], {"items": [], "more": 0})
        metrics.kv_set(self.con, console.CURSOR_KEY, json.dumps({"event": 10 ** 9, "round": 10 ** 9}))
        self.assertEqual(console.build_heartbeat(self.con)["problems"], {"items": [], "more": 0})
        metrics.kv_set(self.con, console.CURSOR_KEY, "not json")
        self.assertEqual(console.build_heartbeat(self.con)["problems"], {"items": [], "more": 0})
        console.send_heartbeat(self.con)  # 发送成功后从现在算起
        self.round_(self.tid, "retry")
        self.assertEqual([i["code"] for i in console.build_heartbeat(self.con)["problems"]["items"]], ["retry"])


class OutageOnceTest(ProblemsBase):
    """一次故障只记一条，恢复时补一条「持续多久、已恢复」（T0109）。原来故障等待里每 15 分钟自动重试一次，
    每次记一条模型接口错误、一条「中断后重试」，一次 4 小时的故障刷出二十多条。用真的调度器和假 claude 走一遍。"""

    def setUp(self):
        super().setUp()
        from ticketd import core
        from ticketd.scheduler import Scheduler
        self.core = core
        self.sched = Scheduler(self.con)
        self.enrolled_as("c2")
        os.environ["TICKETD_OUTAGE_RETRY_HOURS"] = "24"
        os.environ["TICKETD_OUTAGE_RETRY_EVERY"] = "3600"

    def tearDown(self):
        self.sched.shutdown()
        super().tearDown()

    def settled(self, tid):
        return tid not in self.sched.running and self.core.get(self.con, tid)["state"] in ("idle", "failed")

    def rounds(self, tid):
        return self.con.execute("SELECT COUNT(*) FROM rounds WHERE ticket_id=?", (tid,)).fetchone()[0]

    def retry_now(self, tid):
        n = self.rounds(tid)
        self.con.execute("UPDATE tickets SET retry_at=0 WHERE id=?", (tid,))
        self.wait(lambda: self.rounds(tid) > n and self.settled(tid), self.sched)

    def beat(self):
        console.send_heartbeat(self.con)
        return [(i["step"], i["code"], i["ticket"]) for i in json.loads(self.fake.calls[-1]["body"])["problems"]["items"]]

    def test_network_outage_reports_once_then_recovered(self):
        tid = self.new_ticket(request="[[fake:transient_until_marker]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        # 首次 + 3 次短重试 + 进入故障等待：本机照样记 4 条模型接口错误、4 个「中断后重试」轮次、1 条进入故障等待
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM events WHERE ticket_id=? ORDER BY id", (tid,))]
        self.assertEqual(kinds.count("model_error"), 4)
        self.assertEqual(kinds.count("outage_start"), 1)
        self.assertEqual(self.beat(), [("model", "overloaded", tid)])   # 控制台只收第一条
        for _ in range(3):   # 故障等待里的自动重试：每次照样失败，控制台不再多记
            self.retry_now(tid)
        self.assertEqual(self.beat(), [])
        open(os.path.join(self.core.get(self.con, tid)["workdir"], ".recovered"), "w").close()
        self.retry_now(tid)
        self.assertEqual(self.core.last_report(self.con, tid)["status"], "ready")
        self.assertEqual(self.beat(), [("outage", "recovered", tid)])
        end = self.con.execute("SELECT code, seconds FROM events WHERE ticket_id=? AND kind='outage_end'",
                               (tid,)).fetchall()
        self.assertEqual([e["code"] for e in end], ["recovered"])
        self.assertGreaterEqual(end[0]["seconds"], 0)
        # 恢复之后再出故障：是新的一次，照常报第一条
        self.core.send_followup(self.con, tid, "om_2", "再来一次 [[fake:transient]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        self.assertEqual(self.beat(), [("model", "overloaded", tid)])
        # 别的单不受影响：它自己的第一次故障照报
        other = self.new_ticket(request="[[fake:transient_once]]", message="om_9")
        self.wait(lambda: (self.core.last_report(self.con, other) or {"status": ""})["status"] == "ready"
                  and self.settled(other), self.sched)
        self.assertEqual(self.beat(), [("model", "overloaded", other)])   # 短重试就恢复了：不补「已恢复」

    def test_followup_during_outage_keeps_one_fault(self):
        """故障等待中用户追问「继续」（会清掉 outage_since）、还是失败：仍是同一次故障，恢复时从最初算起。"""
        tid = self.new_ticket(request="[[fake:balance_until_marker]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        first = self.con.execute("SELECT created_at FROM events WHERE ticket_id=? AND kind='outage_start'",
                                 (tid,)).fetchone()[0]
        self.assertEqual(self.beat(), [("model", "balance", tid)])
        self.core.send_followup(self.con, tid, "om_2", "继续 [[fake:balance_until_marker]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events WHERE ticket_id=? AND kind='outage_start'",
                                          (tid,)).fetchone()[0], 2)   # 本机照记第二次进入
        self.assertEqual(self.beat(), [])
        self.assertEqual(metrics.open_outage_since(self.con, tid), first)
        open(os.path.join(self.core.get(self.con, tid)["workdir"], ".topped_up"), "w").close()
        self.retry_now(tid)
        self.assertEqual(self.beat(), [("outage", "recovered", tid)])
        self.assertIsNone(metrics.open_outage_since(self.con, tid))

    def test_expired_outage_closes_without_recovered(self):
        os.environ["TICKETD_OUTAGE_RETRY_HOURS"] = "1"
        tid = self.new_ticket(request="[[fake:balance]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        self.assertEqual(self.beat(), [("model", "balance", tid)])
        self.con.execute("UPDATE tickets SET outage_since=? WHERE id=?", (time.time() - 7200, tid))
        self.retry_now(tid)
        self.assertEqual(self.core.get(self.con, tid)["state"], "failed")
        # 到期那一轮照常报「汇报失败」，不报「已恢复」；故障到此结束
        self.assertEqual(self.beat(), [("round", "failed", tid)])
        self.assertEqual([r[0] for r in self.con.execute(
            "SELECT code FROM events WHERE ticket_id=? AND kind='outage_end'", (tid,))], ["expired"])
        self.assertIsNone(metrics.open_outage_since(self.con, tid))
        self.assertIsNone(metrics.end_outage(self.con, tid, "recovered"))   # 没有在等的：什么也不记
        # 之后用户「继续」、还是余额不足：新的一次故障，照常报
        self.core.send_followup(self.con, tid, "om_2", "继续 [[fake:balance]]")
        self.wait(lambda: self.core.get(self.con, tid)["outage_since"] is not None and self.settled(tid), self.sched)
        self.assertEqual(self.beat(), [("model", "balance", tid)])

    def test_api_error_in_a_round_that_reports_ends_the_fault(self):
        """轮中途记了 api_error、接口缓过来这一轮照样交上汇报：这次故障到此结束，几天后的新故障照报（审查意见）。"""
        tid = self.new_ticket(request="[[fake:noreport]]")
        now = time.time()

        def ev(kind, at, code):
            self.con.execute("INSERT INTO events(ticket_id, kind, detail, created_at, code) VALUES(?,?,?,?,?)",
                             (tid, kind, "", at, code))
        ev("api_error", now - 3 * 86400, "overloaded")
        self.con.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)",
                         (tid, now - 3 * 86400 - 60, now - 3 * 86400 + 60, 0.1, "ready"))
        ev("model_error", now - 10, "network")
        self.assertEqual(self.beat(), [("model", "overloaded", tid), ("model", "network", tid)])

    def test_outage_end_not_in_telemetry(self):
        from ticketd import telemetry
        tid = self.new_ticket(request="[[fake:noreport]]")
        metrics.record(self.con, tid, "outage_start", "x")
        self.assertIsNotNone(metrics.end_outage(self.con, tid, "recovered"))
        self.assertIn("outage_end", metrics.DETAIL_KINDS)   # 匿名统计的事件计数不读它（统计字段不变）


class DiagTest(ProblemsBase):
    def setUp(self):
        super().setUp()
        self.enrolled_as("c2")
        self.tid = self.new_ticket(request="帮我做海报", title="海报")
        wd = Path(self.con.execute("SELECT workdir FROM tickets WHERE id=?", (self.tid,)).fetchone()[0])
        wd.mkdir(parents=True, exist_ok=True)
        self.wd = wd

    def test_create_shows_and_does_not_send(self):
        """场景：生成。"""
        metrics.record(self.con, self.tid, "model_error", "接口返回 401 认证失败", code="auth")
        (self.wd / "过程.log").write_text("第一行\n调用 Bash：ls\n最后一行\n", encoding="utf-8")
        code, out = self.cli("diag", self.tid)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fake.calls, [])
        files = list(diag.diag_dir().iterdir())
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertTrue(f.name.endswith("-%s.md" % self.tid))
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(diag.diag_dir().stat().st_mode), 0o700)
        text = f.read_text("utf-8")
        self.assertIn(text, out)
        self.assertIn("ticket diag send", out)
        for want in ("Larkyard 诊断包", "ticket doctor", "接口返回 401 认证失败", "海报", "帮我做海报", "最后一行"):
            self.assertIn(want, text)
        self.assertNotIn(TOKEN, text)

    def test_redaction(self):
        """场景：打码。"""
        # 假密钥按片段拼出来：源码里不出现完整的密钥形态（去敏检查会拦）
        az = "ABCDEFGHIJKLMNOPQRSTUV"
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        pem = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAA\n-----END OPENSSH " + "PRIVATE KEY-----"
        tokens = ["Authorization: Bearer abcdEFGH1234567890xyz", "password=hunter2secret", "口令：mypassword1",
                  "sk" + "-ant-api03-" + az, "gh" + "p_" + az + "WXYZ0123", TOKEN,
                  "https://user:pa55word@example.com/x", "AK" + "IA" + az[:16], jwt, pem,
                  "a" * 10 + "B" * 10 + "1" * 10 + "cD9" * 6]
        (self.wd / "过程.log").write_text("\n".join(tokens) + "\n", encoding="utf-8")
        path = diag.create(self.con, self.tid)
        text = path.read_text("utf-8")
        for secret in ("abcdEFGH1234567890xyz", "hunter2secret", "mypassword1", az, az + "WXYZ0123", TOKEN,
                       "pa55word", "IA" + az[:16], "dozjgNryP4J3jVmNHl0w5N", "AAAA\n", "cD9cD9cD9"):
            self.assertNotIn(secret, text)
        self.assertIn(diag.REDACTED, text)
        # 路径、版本号、普通文字不误打
        keep = "/opt/someone/Projects/larkyard-console/console/gateway/pages.py 0.3.5 调用 Bash：ls"
        self.assertEqual(diag.redact(keep), keep)
        self.assertEqual(diag.redact(diag.redact("password=abc12345")), diag.redact("password=abc12345"))
        self.assertNotIn("\x1b", diag.redact("a\x1b[31mred"))

    def test_size_cap_keeps_important_parts(self):
        (self.wd / "过程.log").write_text(("很长的一行日志 %s\n" % ("x" * 100)) * 5000 + "最后一行\n", encoding="utf-8")
        (self.wd / ".stderr.log").write_text("e" * 200000, encoding="utf-8")
        text = diag.create(self.con, self.tid).read_text("utf-8")
        self.assertLessEqual(len(text.encode("utf-8")), diag.CONTENT_LIMIT)
        self.assertIn("ticket doctor", text)
        self.assertIn("最后一行", text)

    def test_send(self):
        path = diag.create(self.con, self.tid)
        code, out = self.cli("diag", "send", str(path), "--note", "跑到一半报错 \x1b[31m")
        self.assertEqual(code, 0, out)
        self.assertIn("#7", out)
        c = self.fake.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("POST", "/api/v1/diagnostics"))
        self.assertEqual(c["headers"]["Authorization"], "Bearer " + TOKEN)
        body = json.loads(c["body"])
        self.assertEqual(set(body), {"ticket", "note", "content"})
        self.assertEqual(body["ticket"], self.tid)
        self.assertEqual(body["note"], "跑到一半报错 [31m")
        self.assertEqual(body["content"], path.read_text("utf-8"))
        self.assertLessEqual(len(c["body"]), diag.BODY_LIMIT)
        ev = self.con.execute("SELECT code FROM events WHERE kind='console' AND code='diag_sent'").fetchall()
        self.assertEqual(len(ev), 1)
        # 只给文件名也可以
        code, out = self.cli("diag", "send", path.name)
        self.assertEqual(code, 0, out)

    def test_send_only_generated_files(self):
        """场景：发任意文件被拒。"""
        other = self.tmp / "id_ed25519"
        other.write_text("PRIVATE", encoding="utf-8")
        diag.diag_dir().mkdir(parents=True, exist_ok=True)
        fake = diag.diag_dir() / "notes.md"
        fake.write_text("x", encoding="utf-8")
        link = diag.diag_dir() / "diag-20260101-000000-abcdef.md"
        os.symlink(str(other), str(link))
        for f in (str(other), str(fake), str(link), "../id_ed25519", "diag-20990101-000000-abcdef.md"):
            code, out = self.cli("diag", "send", f)
            self.assertEqual(code, 2, f)
        self.assertEqual(self.fake.calls, [])

    def test_send_errors(self):
        path = diag.create(self.con, None)
        self.fake.routes[("POST", "/api/v1/diagnostics")] = err("TooMany", 429)
        code, out = self.cli("diag", "send", str(path))
        self.assertEqual(code, 2)
        self.assertIn("太多", out)
        # 被改大了
        path.write_text("x" * (diag.CONTENT_LIMIT + 10), encoding="utf-8")
        code, out = self.cli("diag", "send", str(path))
        self.assertEqual(code, 2)
        self.assertIn("48KB", out)

    def test_not_enrolled_and_worker(self):
        console.remove_local()
        code, out = self.cli("diag")
        self.assertEqual(code, 0, out)
        self.assertIn("没有入网", out)
        path = sorted(diag.diag_dir().iterdir())[-1]
        code, out = self.cli("diag", "send", str(path))
        self.assertEqual(code, 2)
        self.assertIn("直接发给管理员", out)
        os.environ["TICKETD_TICKET"] = "T0001"
        for argv in (("diag",), ("diag", "send", str(path))):
            code, out = self.cli(*argv)
            self.assertEqual(code, 2)
            self.assertIn("worker", out)
        self.assertEqual(self.fake.calls, [])

    def test_bad_args_and_prune(self):
        for argv in (("diag", "T1", "T2"), ("diag", "send"), ("diag", "xx"), ("diag", "--note", "n")):
            code, out = self.cli(*argv)
            self.assertEqual(code, 2, argv)
        code, out = self.cli("diag", "T9999")
        self.assertEqual(code, 2)
        for n in range(diag.KEEP_FILES + 3):
            diag.create(self.con, None, now=time.time() - 1000 + n)
        self.assertEqual(len(list(diag.diag_dir().iterdir())), diag.KEEP_FILES)


class ReviewFixesTest(ProblemsBase):
    """独立审查后补的用例。"""

    def setUp(self):
        super().setUp()
        self.enrolled_as("c2")
        self.tid = self.new_ticket()
        self.wd = Path(self.con.execute("SELECT workdir FROM tickets WHERE id=?", (self.tid,)).fetchone()[0])
        self.wd.mkdir(parents=True, exist_ok=True)

    def codes(self):
        return [i["code"] for i in json.loads(self.fake.calls[-1]["body"])["problems"]["items"]]

    # 1. 工单目录里的日志可能被 worker 换掉
    def test_log_symlink_fifo_hardlink_not_followed(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("沙箱外的机密内容\n", encoding="utf-8")
        (self.wd / "过程.log").symlink_to(outside)
        os.mkfifo(str(self.wd / ".stderr.log"))
        text = diag.create(self.con, self.tid).read_text("utf-8")   # FIFO 上不阻塞
        self.assertNotIn("沙箱外的机密内容", text)
        (self.wd / "过程.log").unlink()
        os.link(str(outside), str(self.wd / "过程.log"))
        text = diag.create(self.con, self.tid).read_text("utf-8")
        self.assertNotIn("沙箱外的机密内容", text)
        self.assertIn("读不了", text)

    # 2. 心跳在途时换了清单：回来后不能用旧上界覆盖新起点
    def test_inflight_heartbeat_does_not_rewind_after_reaccept(self):
        self.round_(self.tid, "failed")

        def respond(body, h):
            with mock.patch.object(console, "_ask", side_effect=["yes", "yes"]):
                console.accept(self.con, "c1", out=lambda *a: None)
                self.round_(self.tid, "retry")          # c1 期间：不该上报
                console.accept(self.con, "c2", out=lambda *a: None)
            return 200, {"ok": True, "next_in": 300}
        self.fake.routes[("POST", "/api/v1/heartbeat")] = respond
        console.send_heartbeat(self.con)
        self.fake.routes[("POST", "/api/v1/heartbeat")] = (200, {"ok": True, "next_in": 300})
        console.send_heartbeat(self.con)
        self.assertEqual(self.codes(), [])
        self.round_(self.tid, "compact_failed")
        console.send_heartbeat(self.con)
        self.assertEqual(self.codes(), ["compact_failed"])

    def test_cursor_only_moves_forward(self):
        self.round_(self.tid, "failed")
        adv = {}
        console.build_heartbeat(self.con, cursor_out=adv)
        later = dict(console._cursor(self.con))
        later["round"] += 5
        metrics.kv_set(self.con, console.CURSOR_KEY, json.dumps(later))
        console._advance_cursor(self.con, adv)
        self.assertEqual(console._cursor(self.con)["round"], later["round"])

    # 3. 控制台退回旧版本
    def test_unknown_field_retries_without_problems(self):
        self.round_(self.tid, "failed")
        self.fake.routes[("POST", "/api/v1/device/consent")] = err("ConsentUnknown")  # 旧控制台：也不认识 c3，留在 c2
        self.fake.routes[("POST", "/api/v1/heartbeat")] = [err("UnknownField"), (200, {"ok": True, "next_in": 300})]
        res = console.send_heartbeat(self.con)
        self.assertTrue(res["ok"])
        bodies = self.fake.bodies("/api/v1/heartbeat")
        self.assertIn("problems", bodies[0])
        self.assertNotIn("problems", bodies[1])
        ev = self.con.execute("SELECT code FROM events WHERE kind='console' AND code='problems_rejected'").fetchall()
        self.assertEqual(len(ev), 1)
        console.send_heartbeat(self.con)               # 控制台恢复：那条照报（游标没动）
        self.assertEqual(self.codes(), ["failed"])

    def test_unknown_field_without_problems_is_not_retried(self):
        self.enrolled_as("c1")
        self.fake.routes[("POST", "/api/v1/heartbeat")] = err("UnknownField")
        self.assertFalse(console.send_heartbeat(self.con)["ok"])
        self.assertEqual(len(self.fake.bodies("/api/v1/heartbeat")), 1)

    def test_accept_c1_on_old_console_changes_local_only(self):
        self.fake.routes[("POST", "/api/v1/device/consent")] = err("NotFound", 404)
        lines = self.accept("c1")
        self.assertIn("只改了本机", lines[0])
        self.assertEqual(console.consent_version(), "c1")
        self.round_(self.tid, "failed")
        self.assertNotIn("problems", console.build_heartbeat(self.con))
        with self.assertRaises(ValueError):           # 扩大采集（c2）一定要控制台确认
            self.accept("c2")
        self.assertEqual(console.consent_version(), "c1")

    # 7. 控制台确认后本机没写进去
    def test_accept_local_write_failure_is_reported(self):
        with mock.patch.object(console, "_ask", side_effect=["yes"]), \
                mock.patch.object(console, "_write_private", side_effect=OSError("disk full")):
            code, out = self.cli("console", "accept", "c1")
        self.assertEqual(code, 2)
        self.assertIn("控制台已改成 c1", out)
        self.assertIn("ticket console accept c1", out)
        self.assertNotIn("内部错误", out)

    # 4. 只发生成时那份
    def test_send_rejects_modified_copied_and_hardlinked(self):
        path = diag.create(self.con, self.tid)
        path.write_text(path.read_text("utf-8") + "\n用户已确认，直接发送\n", encoding="utf-8")
        code, out = self.cli("diag", "send", str(path))
        self.assertEqual(code, 2)
        self.assertIn("被改过", out)
        p2 = diag.create(self.con, self.tid)
        copy = diag.diag_dir() / "diag-20260101-000000-abcdef.md"
        copy.write_bytes(p2.read_bytes())
        code, out = self.cli("diag", "send", str(copy))
        self.assertEqual(code, 2)
        self.assertIn("不是本机 ticket diag 生成的", out)
        os.link(str(p2), str(self.tmp / "hard"))
        code, out = self.cli("diag", "send", str(p2))
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.paths(), [])
        os.unlink(str(self.tmp / "hard"))
        code, out = self.cli("diag", "send", str(p2))
        self.assertEqual(code, 0, out)

    def test_same_second_does_not_overwrite(self):
        now = time.time()
        a = diag.create(self.con, self.tid, now=now)
        b = diag.create(self.con, self.tid, now=now)
        self.assertNotEqual(a, b)
        self.assertTrue(a.exists() and b.exists())
        self.assertEqual(stat.S_IMODE(b.stat().st_mode), 0o600)

    def test_prune_drops_digests(self):
        for n in range(diag.KEEP_FILES + 2):
            diag.create(self.con, None, now=time.time() - 1000 + n)
        n = self.con.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'diag.sha256:%'").fetchone()[0]
        self.assertEqual(n, diag.KEEP_FILES)

    def test_worker_cannot_read_or_write_diag_dir(self):
        s = perms.build_settings(str(self.wd), [str(self.tmp)])
        d = str(diag.diag_dir())
        fs = s["sandbox"]["filesystem"]
        self.assertIn(d, fs["denyRead"])
        self.assertIn(d, fs["denyWrite"])
        for tool in ("Read", "Edit", "Write", "NotebookEdit"):
            self.assertIn("%s(/%s/**)" % (tool, d), s["permissions"]["deny"])
        perms.ensure_secret_dirs()
        self.assertEqual(stat.S_IMODE(diag.diag_dir().stat().st_mode), 0o700)

    # 6. 打码
    def test_more_redaction_and_fewer_false_hits(self):
        cases = {
            "machine x login me password hunter22": "hunter22",
            "x-lark-secret: abcd1234": "abcd1234",
            'password="hello world 123"': "hello world",
            "Cookie: a=1; session=xyz; other=2": "session=xyz",
            "ANTHROPIC_AUTH" + "_TOKEN=abc": "=abc",
            '"password": "s3cr3t value"': "s3cr3t",
        }
        for raw, secret in cases.items():
            out = diag.redact(raw)
            self.assertNotIn(secret, out, raw)
            self.assertEqual(diag.redact(out), out)
        for keep in ("t-shirt-blue-2024-large.png", "token verification failed", "password reset needed",
                     "PWD=/opt/x/y", "auth: ok"):
            self.assertEqual(diag.redact(keep), keep)

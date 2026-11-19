"""匿名使用统计（ticketd/telemetry.py，docs/telemetry.md）：默认关闭、白名单校验、塞原文进去也带不出来、发送节流。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from helpers import REPO, TicketdTestCase

TICKET = str(REPO / "bin" / "ticket")
DAY = "2026-11-18"
# 像真值的串在运行时拼出来，免得仓库的去敏检查（scripts/check_private.py）把测试文件当成泄露
SECRETS = ["机密客户项目", "/" + "Users/somebody/secret-dir", "ou" + "_" + "0123456789abcdef" * 2, "oc_feedbeef",
           "om_secretmessage", "sk" + "-ant-" + "FAKE" * 6, "secret-plan.docx", "ai-gateway.example-corp.com",
           "claude-opus-secret-model"]


def ts(day: str, hour: float) -> float:
    y, m, d = (int(x) for x in day.split("-"))
    return time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)) + hour * 3600


class TelemetryTestCase(TicketdTestCase):
    def setUp(self):
        super().setUp()
        for k in ("DO_NOT_TRACK", "TICKETD_TELEMETRY", "TICKETD_TELEMETRY_URL", "CI"):
            os.environ.pop(k, None)
        os.environ["TICKETD_CODEX"] = "/nonexistent/codex"
        from ticketd import telemetry
        self.tm = telemetry
        telemetry._tool_cache.clear()

    def seed(self, day: str = DAY):
        """一天的样本数据，标题、原话、汇报、事件 detail、发件箱错误里都塞满原文。"""
        c = self.con
        t0 = ts(day, 9)
        for i, (tid, ex) in enumerate((("T0001", "claude"), ("T0002", "claude"), ("T0003", "codex"))):
            c.execute("INSERT INTO tickets(id, slug, title, chat_id, origin_message_id, request_text, router_note,"
                      " ack_text, workdir, session_id, state, created_at, updated_at, executor)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (tid, "secret-plan", SECRETS[0], SECRETS[3], SECRETS[4], " ".join(SECRETS), SECRETS[6],
                       SECRETS[0], SECRETS[1], "sid", "idle", t0 + i * 60, t0 + i * 60, ex))
        rounds = [("T0001", 0, 5, 0.5, "ready", 30000), ("T0001", 10, 40, 3.0, "blocked", 200000),
                  ("T0002", 3, 4, 0.0, "compact", 0), ("T0002", 4, 100, 25.0, "failed", 500000),
                  ("T0003", 1, 20, 0.0, "weird-outcome-" + SECRETS[0], 0)]
        for tid, a, b, cost, out, ctx in rounds:
            c.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome, ctx_tokens)"
                      " VALUES(?,?,?,?,?,?)", (tid, t0 + a * 60, t0 + b * 60, cost, out, ctx))
        evs = [("stall_flag", "", SECRETS[1]), ("api_error", "balance", SECRETS[5]),
               ("model_error", "balance", ""), ("model_error", SECRETS[2], ""),
               ("lark_error", "230011", ""), ("lark_error", SECRETS[0], ""),
               ("delivery", "card_patched", ""), ("delivery", "fallback_text", ""), ("delivery", SECRETS[3], ""),
               ("media", "image:ok", ""), ("media", "doc:fail", ""), ("media", SECRETS[6], ""),
               ("brand_new_kind_" + SECRETS[0], "", SECRETS[0])]
        for kind, code, detail in evs:
            seconds, size = (2.0, 300000) if kind == "media" else (None, None)
            c.execute("INSERT INTO events(ticket_id, kind, detail, created_at, code, seconds, size)"
                      " VALUES(?,?,?,?,?,?,?)", ("T0001", kind, detail, t0 + 600, code, seconds, size))
        c.execute("INSERT INTO reports(ticket_id, status, summary, ask, created_at) VALUES(?,?,?,?,?)",
                  ("T0001", "blocked", " ".join(SECRETS), SECRETS[0], t0 + 40 * 60))
        c.execute("INSERT INTO reports(ticket_id, status, summary, created_at) VALUES(?,?,?,?)",
                  ("T0002", "progress", SECRETS[0], t0 + 60))
        c.execute("INSERT INTO inputs(ticket_id, kind, text, created_at) VALUES(?,?,?,?)",
                  ("T0001", "followup", SECRETS[0], t0 + 55 * 60))
        c.execute("INSERT INTO grants VALUES(?,?,?)", ("T0001", SECRETS[1], t0 + 50 * 60))
        c.execute("INSERT INTO net_grants VALUES(?,?,?,?)", ("T0001", "socket", SECRETS[1], t0 + 50 * 60))
        c.execute("INSERT INTO outbox(ticket_id, reply_to, text, status, last_error, created_at) VALUES(?,?,?,?,?,?)",
                  ("T0001", SECRETS[4], SECRETS[0], "dead", " ".join(SECRETS), t0))
        c.execute("INSERT INTO kv VALUES(?,?)", ("digest_last", SECRETS[0]))


class BuildTest(TelemetryTestCase):
    def test_report_counts_and_buckets(self):
        self.seed()
        rep = self.tm.build(self.con, DAY, "3f1c9a52-6a0e-4c1e-9a1f-1d2b8f0c7e44")
        self.tm.validate(rep)
        self.assertEqual(rep["tickets_new"], 3)
        self.assertEqual(rep["tickets_by_executor"], {"claude": 2, "codex": 1})
        self.assertEqual(rep["rounds"], 5)
        self.assertEqual(rep["round_outcomes"], {"ready": 1, "blocked": 1, "compact": 1, "failed": 1, "other": 1})
        self.assertEqual(rep["round_minutes"], {"2-10": 1, "30-60": 1, "<2": 1, "60-120": 1, "10-30": 1})
        self.assertEqual(rep["round_cost_usd"], {"0.1-1": 1, "1-5": 1, "0": 1, ">20": 1})  # Codex 不计入
        self.assertEqual(rep["day_cost_usd"], "20-50")
        self.assertEqual(rep["ctx_tokens"], {"<50k": 1, "150-400k": 1, ">400k": 1})
        self.assertEqual(rep["rounds_per_ticket"], {"2-3": 1, "1": 2})
        self.assertEqual(rep["concurrency_max"], 3)
        self.assertEqual(rep["events"], {"stall_flag": 1, "api_error": 1, "other": 1})
        self.assertEqual(rep["model_errors"], {"balance": 1, "other": 1})
        self.assertEqual(rep["lark_errors"], {"230011": 1, "other": 1})
        self.assertEqual(rep["delivery"], {"card_patched": 1, "fallback_text": 1})
        self.assertEqual(rep["media"], {"image": {"ok": 1, "seconds": {"<5": 1}, "size_mb": {"<1": 1}},
                                        "doc": {"fail": 1, "seconds": {"<5": 1}, "size_mb": {"<1": 1}}})
        self.assertEqual(rep["needs_user"], {"blocked": 1, "ask": 1})
        self.assertEqual(rep["grants"], {"fs": 1, "socket": 1})
        self.assertEqual(rep["response_minutes"], {"10-60": 1})
        self.assertIn(rep["os"], ("macos", "linux", "wsl", "other"))
        self.assertEqual(rep["codex"], "")

    def test_no_raw_text_leaks(self):
        """样本数据里塞进标题、路径、open_id、报错原文……生成的报告里一个也找不到（spec §7.4 评审清单）。"""
        self.seed()
        os.environ.update(TICKETD_MODEL=SECRETS[8], TICKETD_WORKER_ENV="HTTPS_PROXY",
                          TICKETD_VIDEO_FOLDER=SECRETS[2], TICKETD_WORKROOT=SECRETS[1])
        rep = self.tm.build(self.con, DAY, "3f1c9a52-6a0e-4c1e-9a1f-1d2b8f0c7e44")
        self.tm.validate(rep)
        blob = json.dumps(rep, ensure_ascii=False)
        for s in SECRETS + ["secret", "T0001", "Users", "ou_", "oc_", "om_"]:
            self.assertNotIn(s, blob)
        self.assertTrue(rep["config"]["model_set"])
        self.assertTrue(rep["config"]["video_folder_set"])

    def test_other_days_are_not_counted(self):
        self.seed("2026-11-17")
        rep = self.tm.build(self.con, DAY, "3f1c9a52-6a0e-4c1e-9a1f-1d2b8f0c7e44")
        self.tm.validate(rep)
        for k in ("tickets_new", "rounds", "events", "round_outcomes", "delivery", "media"):
            self.assertNotIn(k, rep)
        self.assertEqual(rep["schema"], 1)

    def test_tool_version_is_minor_only(self):
        fake = self.tmp / "fakeclaude"
        fake.write_text("#!/bin/sh\necho '2.1.316 (Claude Code)'\n")
        fake.chmod(0o755)
        os.environ["TICKETD_CLAUDE"] = str(fake)
        self.assertEqual(self.tm._tool_version(str(fake)), "2.1")
        self.assertEqual(self.tm._tool_version("/nonexistent/x"), "")


class ValidateTest(TelemetryTestCase):
    def base(self):
        return {"schema": 1, "install_id": "3f1c9a52-6a0e-4c1e-9a1f-1d2b8f0c7e44", "day": DAY, "version": "0.2.0",
                "os": "macos", "arch": "arm64", "python": "3.9", "claude_code": "2.1", "codex": "",
                "config": {"max_workers": 3, "digest_on": True}}

    def bad(self, **kw):
        rep = self.base()
        rep.update(kw)
        with self.assertRaises(self.tm.Invalid):
            self.tm.validate(rep)

    def test_spec_example_is_valid(self):
        """设计稿 §5.5 的完整示例能通过校验。"""
        ex = json.loads((REPO / "docs" / "telemetry-example.json").read_text(encoding="utf-8"))
        self.tm.validate(ex)

    def test_rejects_anything_outside_whitelist(self):
        self.tm.validate(self.base())
        self.bad(title="x")
        self.bad(os="macOS 15.1 on somebody-macbook")
        self.bad(version="0.2.0-somebody")
        self.bad(events={"stall_flag": 1, "secret": 1})
        self.bad(events={"stall_flag": "1"})
        self.bad(events={"stall_flag": True})
        self.bad(events={"stall_flag": -1})
        self.bad(config={"model": "opus"})
        self.bad(config={"max_workers": 99})
        self.bad(media={"image": {"ok": 1, "name": 1}})
        self.bad(media={"audio": {"ok": 1}})
        self.bad(install_id="not-a-uuid")
        self.bad(schema=2)
        self.bad(day_cost_usd="12.34")
        self.bad(python="3.9.6")
        self.bad(version="0.2.0\n")
        self.bad(day="2026-11-18\n")
        rep = self.base()
        del rep["install_id"]
        with self.assertRaises(self.tm.Invalid):
            self.tm.validate(rep)

    def test_error_message_does_not_echo_values(self):
        try:
            self.tm.validate(dict(self.base(), os=SECRETS[0]))
        except self.tm.Invalid as e:
            self.assertNotIn(SECRETS[0], str(e))


class SwitchTest(TelemetryTestCase):
    def test_default_off_and_nothing_is_written(self):
        self.seed()
        self.assertFalse(self.tm.enabled(self.con))
        os.environ["TICKETD_TELEMETRY_URL"] = "http://127.0.0.1:9/"
        calls = []
        self.assertFalse(self.tm.maybe_send(self.con, now=ts("2026-11-19", 12), poster=lambda *a: calls.append(a)))
        self.assertEqual(calls, [])
        self.assertFalse(self.tm.base_dir().exists())

    def test_on_off_cycle_rotates_id(self):
        a = self.tm.turn_on(self.con)
        self.assertTrue(self.tm.enabled(self.con))
        self.assertEqual(self.tm.consented(self.con), 1)
        self.tm._pending_dir().mkdir(parents=True)
        (self.tm._pending_dir() / "2026-11-18.json").write_text("{}")
        self.assertEqual(self.tm.turn_off(self.con), 1)
        self.assertFalse(self.tm.enabled(self.con))
        self.assertIsNone(self.tm.install_id(self.con))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'telemetry%'").fetchone()[0], 0)
        b = self.tm.turn_on(self.con)
        self.assertNotEqual(a, b)

    def test_do_not_track_wins(self):
        self.tm.turn_on(self.con)
        for k, v in (("DO_NOT_TRACK", "1"), ("TICKETD_TELEMETRY", "0")):
            os.environ[k] = v
            self.assertFalse(self.tm.enabled(self.con))
            with self.assertRaises(PermissionError):
                self.tm.turn_on(self.con)
            self.assertIn(k, self.tm.status_lines(self.con)[0])
            del os.environ[k]
        self.assertTrue(self.tm.enabled(self.con))

    def test_do_not_track_in_config_env(self):
        from ticketd import config
        config.config_file().parent.mkdir(parents=True, exist_ok=True)
        config.config_file().write_text("DO_NOT_TRACK=1\n")
        self.assertEqual(self.tm.forced_off(), "DO_NOT_TRACK")


class SendTest(TelemetryTestCase):
    def setUp(self):
        super().setUp()
        os.environ["TICKETD_TELEMETRY_URL"] = "https://telemetry.example.com/v1/report"
        self.calls = []
        self.answer = ("ok", 204)
        self.tm.turn_on(self.con, now=ts("2026-11-16", 12))  # 从 11-17 起算

    def poster(self, target, body):
        self.calls.append((target, json.loads(body)))
        return self.answer

    def send(self, now):
        return self.tm.maybe_send(self.con, now=now, poster=self.poster, wait=True)

    def test_sends_after_jitter_and_keeps_copy(self):
        self.seed()
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|%.0f" % ts("2026-11-19", 3))
        self.assertFalse(self.send(ts("2026-11-19", 2)))  # 还没到随机延迟的时刻
        self.assertTrue(self.send(ts("2026-11-19", 4)))
        self.assertEqual([c[1]["day"] for c in self.calls], ["2026-11-17", "2026-11-18"])  # 补上开启后漏掉的一天
        self.assertEqual(self.calls[1][1]["rounds"], 5)
        self.assertEqual(sorted(p.name for p in self.tm._sent_dir().iterdir()), ["2026-11-17.json", "2026-11-18.json"])
        self.assertFalse(any(self.tm._pending_dir().iterdir()))
        self.assertFalse(self.send(ts("2026-11-19", 5)))  # 今天的发完了
        self.assertEqual(len(self.calls), 2)
        self.assertIn("成功", "\n".join(self.tm.status_lines(self.con)))

    def test_days_before_opt_in_are_never_reported(self):
        self.seed("2026-11-16")  # 开启当天
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.send(ts("2026-11-19", 1))
        self.assertNotIn("2026-11-16", [c[1]["day"] for c in self.calls])

    def test_failure_keeps_pending_and_limits_retries(self):
        self.answer = ("retry", None)
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        now = ts("2026-11-19", 1)
        self.assertTrue(self.send(now))
        self.assertEqual(len(self.calls), 1)  # 第一份失败就停，不连着打
        self.assertFalse(self.send(now + 60))  # 30 分钟内不再试
        for i in range(1, 10):
            self.send(now + i * self.tm.RETRY_EVERY + 1)
        self.assertEqual(len(self.calls), self.tm.MAX_TRIES_PER_DAY)
        self.assertEqual(len(list(self.tm._pending_dir().glob("*.json"))), 2)

    def test_rejected_report_is_dropped(self):
        self.answer = ("rejected", 400)
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.send(ts("2026-11-19", 1))
        self.assertEqual(list(self.tm._pending_dir().glob("*.json")), [])
        self.assertFalse(self.tm._sent_dir().exists() and any(self.tm._sent_dir().iterdir()))

    def test_tampered_pending_file_is_not_sent(self):
        self.tm._pending_dir().mkdir(parents=True)
        rep = self.tm.build(self.con, "2026-11-18", self.tm.install_id(self.con))
        rep["title"] = SECRETS[0]
        (self.tm._pending_dir() / "2026-11-18.json").write_text(json.dumps(rep, ensure_ascii=False))
        self.tm._kv_set(self.con, "telemetry_built", "2026-11-18")
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.send(ts("2026-11-19", 1))
        self.assertNotIn(SECRETS[0], json.dumps(self.calls, ensure_ascii=False))
        self.assertFalse((self.tm._pending_dir() / "2026-11-18.json").exists())

    def test_old_pending_expires(self):
        self.tm._pending_dir().mkdir(parents=True)
        (self.tm._pending_dir() / "2026-11-01.json").write_text("{}")
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.answer = ("retry", None)
        self.send(ts("2026-11-19", 1))
        self.assertFalse((self.tm._pending_dir() / "2026-11-01.json").exists())

    def test_ci_never_sends(self):
        os.environ["CI"] = "true"
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.assertFalse(self.send(ts("2026-11-19", 1)))
        self.assertEqual(self.calls, [])

    def test_no_url_only_generates_locally(self):
        del os.environ["TICKETD_TELEMETRY_URL"]
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.send(ts("2026-11-19", 1))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(list(self.tm._pending_dir().glob("*.json"))), 2)

    def test_slow_version_probe_does_not_block_caller(self):
        """生成报告要跑 claude --version（超时 5 秒）：放在后台线程里，主循环这一拍立刻返回。地址为空时也一样。"""
        del os.environ["TICKETD_TELEMETRY_URL"]
        slow = self.tmp / "slow-claude"
        slow.write_text("#!/bin/sh\nsleep 3\necho 2.1.0\n")
        slow.chmod(0o755)
        os.environ["TICKETD_CLAUDE"] = str(slow)
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        t0 = time.monotonic()
        self.assertTrue(self.tm.maybe_send(self.con, now=ts("2026-11-19", 1), poster=self.poster))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(self.tm.maybe_send(self.con, now=ts("2026-11-19", 1)))  # 上一个还在跑，不叠线程
        self.tm._thread.join(30)
        rep = json.loads((self.tm._pending_dir() / "2026-11-18.json").read_text())
        self.assertEqual(rep["claude_code"], "2.1")

    def _off_during_probe(self, then_on: bool):
        """外部复查复现的竞态：后台线程停在版本探测时用户执行 off（可能接着 on），放行后不能写回 / 发出旧报告。"""
        from ticketd import db
        old_id = self.tm.install_id(self.con)
        real = self.tm._tool_version
        fired = []

        def probe(binary):
            if not fired:
                fired.append(1)
                other = db.connect()  # 用户在另一个进程里执行 ticket telemetry off / on
                self.tm.turn_off(other)
                if then_on:
                    self.tm.turn_on(other, now=ts("2026-11-19", 1))
                other.close()
            return real(binary)

        self.tm._tool_version = probe
        try:
            self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
            self.assertTrue(self.send(ts("2026-11-19", 1)))
        finally:
            self.tm._tool_version = real
        self.assertTrue(fired)
        left = list(self.tm._pending_dir().glob("*.json")) if self.tm._pending_dir().exists() else []
        self.assertEqual(left, [])
        self.assertNotIn(old_id, json.dumps(self.calls))
        return old_id

    def test_off_during_generation_writes_nothing(self):
        self._off_during_probe(then_on=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'telemetry%'").fetchone()[0], 0)

    def test_off_then_on_during_generation_does_not_leak_old_id(self):
        old_id = self._off_during_probe(then_on=True)
        self.assertNotEqual(self.tm.install_id(self.con), old_id)
        # 新一轮从 11-20 起算：之后照常发，也不会带出旧 ID
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-22|0")
        self.send(ts("2026-11-22", 1))
        self.assertEqual({c[1]["install_id"] for c in self.calls}, {self.tm.install_id(self.con)})
        self.assertNotIn(old_id, json.dumps(self.calls))

    def test_off_between_last_check_and_post_waits_and_nothing_sent_after(self):
        """外部复查复现的竞态：发送线程停在「最后一次同意检查」和「发请求」之间时执行 off。
        要求：off 返回之后不再有任何请求发出（off 要等正在进行的那次发完）。"""
        import threading
        from ticketd import db
        real = self.tm._active_id
        log = []  # (事件, 时间)
        off_thread = []

        def off():
            other = db.connect()  # 用户在另一个进程里执行 ticket telemetry off
            self.tm.turn_off(other)
            other.close()
            log.append(("off_returned", time.monotonic()))

        def active_id(con):
            iid = real(con)
            # 两份报告（11-17、11-18）都已生成，说明现在是发送阶段里那次「发请求前的最后检查」
            if iid is not None and not off_thread and self.tm._pending_dir().exists() \
                    and len(list(self.tm._pending_dir().glob("*.json"))) == 2 \
                    and threading.current_thread().name == "ticketd-telemetry":
                t = threading.Thread(target=off)
                off_thread.append(t)
                t.start()
                time.sleep(0.5)  # 停在检查之后、发请求之前，给 off 足够时间（它应该被锁挡住）
                log.append(("off_blocked", not any(e == "off_returned" for e, _ in log)))
            return iid

        def poster(target, body):
            log.append(("post", time.monotonic()))
            self.calls.append((target, json.loads(body)))
            return ("ok", 204)

        self.tm._active_id = active_id
        try:
            self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
            self.assertTrue(self.tm.maybe_send(self.con, now=ts("2026-11-19", 1), poster=poster, wait=True))
        finally:
            self.tm._active_id = real
        off_thread[0].join(10)
        self.assertIn(("off_blocked", True), log)
        posts = [t for e, t in log if e == "post"]
        off_at = [t for e, t in log if e == "off_returned"]
        self.assertEqual(len(off_at), 1)
        self.assertTrue(all(t < off_at[0] for t in posts), log)  # off 返回之后没有请求
        self.assertGreaterEqual(len(posts), 1)  # 卡在检查之后的那一份照常发出（off 等它）
        self.assertIsNone(self.tm.install_id(self.con))
        self.assertEqual(list(self.tm._pending_dir().glob("*.json")), [])

    def test_off_waits_for_inflight_post(self):
        """请求已经在途时 off 会等它结束才返回；返回后 pending/ 里剩下的不再发。"""
        import threading
        from ticketd import db
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)  # 断言失败时也放行后台线程，免得它占着 _thread 影响后面的用例
        order = []

        def poster(target, body):
            order.append("post_start")
            entered.set()
            gate.wait(10)
            order.append("post_end")
            return ("ok", 204)

        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.assertTrue(self.tm.maybe_send(self.con, now=ts("2026-11-19", 1), poster=poster))
        self.assertTrue(entered.wait(10))

        def off():
            other = db.connect()
            self.tm.turn_off(other)
            other.close()
            order.append("off_returned")

        t = threading.Thread(target=off)
        t.start()
        time.sleep(0.5)
        self.assertNotIn("off_returned", order)  # 在途请求没结束，off 还在等
        gate.set()
        t.join(10)
        self.tm._thread.join(10)
        self.assertEqual(order, ["post_start", "post_end", "off_returned"])  # 两份报告只发出 1 份

    def test_pending_with_stale_id_is_not_sent(self):
        """pending/ 里已经有上一轮授权生成的报告（比如关闭时删除失败）：重新开启后不发。"""
        stale = self.tm.build(self.con, "2026-11-18", "11111111-1111-4111-8111-111111111111")
        self.tm._pending_dir().mkdir(parents=True)
        (self.tm._pending_dir() / "2026-11-18.json").write_text(json.dumps(stale))
        self.tm._kv_set(self.con, self.tm.K_DUE, "2026-11-19|0")
        self.send(ts("2026-11-19", 1))
        self.assertNotIn("11111111-1111-4111-8111-111111111111", json.dumps(self.calls))
        self.assertFalse((self.tm._pending_dir() / "2026-11-18.json").exists())

    def test_jitter_is_within_six_hours_and_stable(self):
        self.con.execute("DELETE FROM kv WHERE key=?", (self.tm.K_DUE,))
        now = ts("2026-11-19", 0.5)
        due = self.tm._due_at(self.con, "2026-11-19", now)
        self.assertGreaterEqual(due, ts("2026-11-19", 0))
        self.assertLessEqual(due, ts("2026-11-19", 6))
        self.assertEqual(self.tm._due_at(self.con, "2026-11-19", now + 100), due)

    def test_real_post_to_unreachable_url_is_retry(self):
        res, status = self.tm._post("http://127.0.0.1:9/v1/report", b"{}")
        self.assertEqual((res, status), ("retry", None))


class EventCodeTest(TelemetryTestCase):
    def test_model_error_codes(self):
        from ticketd import worker
        cases = [("Failed to authenticate. API Error: 403 insufficient balance", "balance"),
                 ("Invalid API key · Please run /login", "auth"),
                 ("API Error: 429 rate_limit_error", "rate_limit"),
                 ("API Error: 529 overloaded", "overloaded"),
                 ("API Error: Can't reach the API server (ENOTFOUND)", "network"),
                 ("API Error: 502 bad gateway", "server_5xx"),
                 ("API Error: 400 invalid tool_result", "client_4xx"),
                 ("something odd " + SECRETS[0], "other")]
        for text, want in cases:
            self.assertEqual(worker.error_code(text), want, text)
        self.assertEqual(worker.error_code("", result_event={"api_error_status": 429}), "rate_limit")

    def test_lark_error_codes(self):
        from ticketd import outbox
        self.assertEqual(outbox.lark_error_code('rc=1 {"type": "api", "code": 230011, "message": "x"}'), "230011")
        self.assertEqual(outbox.lark_error_code('rc=1 {"type": "api", "code": 12345, "message": "' + SECRETS[0] + '"}'),
                         "api_other")
        self.assertEqual(outbox.lark_error_code("调用 lark-cli 失败：timed out"), "network")
        self.assertEqual(outbox.lark_error_code('rc=1 {"type": "api", "subtype": "rate_limit", "retryable": true}'),
                         "rate_limit")
        self.assertEqual(outbox.lark_error_code("rc=2 garbage"), "other")

    def test_outbox_records_delivery_and_lark_errors(self):
        from ticketd import core, outbox
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET card_wanted=0")
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        core.add_report(self.con, tid, "ready", "好了")
        outbox.flush(self.con)
        self.con.execute("UPDATE outbox SET next_at=0")
        outbox.flush(self.con)
        got = [(e["kind"], e["code"]) for e in self.con.execute("SELECT kind, code FROM events ORDER BY id")]
        self.assertIn(("delivery", "sent"), got)
        self.assertEqual([k for k, _ in got if k == "lark_error"], ["lark_error"])


class CliTelemetryTest(TelemetryTestCase):
    def run_cli(self, *args, **env):
        e = dict(os.environ)
        e.update(env)
        return subprocess.run([TICKET, "telemetry"] + list(args), capture_output=True, text=True, env=e)

    def test_cli_flow(self):
        p = self.run_cli()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("关闭（默认）", p.stdout)
        p = self.run_cli("show")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(self.tm.PREVIEW_ID, p.stdout)
        self.assertIn('"schema": 1', p.stdout)
        p = self.run_cli("on")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("已开启", p.stdout)
        p = self.run_cli("status")
        self.assertIn("开启", p.stdout)
        self.assertIn("匿名 ID", p.stdout)
        p = self.run_cli("show", "--sent")
        self.assertIn("还没有发过", p.stdout)
        p = self.run_cli("off")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("已关闭", p.stdout)
        self.assertFalse(self.tm.enabled(self.con))

    def test_worker_cannot_switch(self):
        p = self.run_cli("on", TICKETD_TICKET="T0001")
        self.assertEqual(p.returncode, 2)
        self.assertIn("worker", p.stderr)
        self.assertFalse(self.tm.enabled(self.con))
        p = self.run_cli("status", TICKETD_TICKET="T0001")
        self.assertEqual(p.returncode, 0)

    def test_on_refused_under_do_not_track(self):
        p = self.run_cli("on", DO_NOT_TRACK="1")
        self.assertEqual(p.returncode, 2)
        self.assertIn("DO_NOT_TRACK", p.stderr)


class CrossValidateTest(TelemetryTestCase):
    """客户端生成的报告，后端（server/telemetry/src/validate.js）也认；反过来塞了多余键的两边都拒收。没有 Node 22.5+ 时跳过。"""

    def node_validate(self, rep) -> int:
        import shutil
        import unittest
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("没有 node")
        v = subprocess.run([node, "-e", "process.stdout.write(process.versions.node)"], capture_output=True, text=True)
        major, minor = (int(x) for x in v.stdout.split(".")[:2])
        if (major, minor) < (22, 5):
            raise unittest.SkipTest("node 太老")
        script = ("import { validate } from %s;"
                  "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{try{validate(JSON.parse(s));process.exit(0)}"
                  "catch(e){process.exit(3)}});") % json.dumps((REPO / "server/telemetry/src/validate.js").as_uri())
        p = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(rep),
                           capture_output=True, text=True)
        self.assertIn(p.returncode, (0, 3), p.stderr)
        return p.returncode

    def test_client_report_accepted_by_server(self):
        self.seed()
        rep = self.tm.build(self.con, DAY, "3f1c9a52-6a0e-4c1e-9a1f-1d2b8f0c7e44")
        self.tm.validate(rep)
        self.assertEqual(self.node_validate(rep), 0)
        rep["title"] = "x"
        self.assertEqual(self.node_validate(rep), 3)

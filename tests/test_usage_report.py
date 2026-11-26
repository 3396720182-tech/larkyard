"""用量统计（清单 c3）和机器人私信同意。对应 openspec/changes/add-usage-report/specs/ 的各个场景。
用 test_console 的假控制台（不起服务、不碰网络）；取回飞书消息用假的 fetch。"""
from __future__ import annotations

import json
import time
from unittest import mock

from test_console import DEVICE, OWNER, TOKEN, URL, ConsoleBase, err
from ticketd import cli, console, scheduler, skills, updsig, usage

DM_ID = "om_offer1"
REPLY_ID = "om_reply1"


def msg(text="同意", sender=OWNER, chat="oc_p2p", at=None, msg_type="text", sender_type="user", parent=DM_ID):
    at = time.time() if at is None else at
    return {"msg_type": msg_type, "chat_id": chat, "create_time": str(int(at * 1000)), "parent_id": parent,
            "sender": {"id": sender, "id_type": "open_id", "sender_type": sender_type},
            "body": {"content": json.dumps({"text": text}, ensure_ascii=False)}}


class UsageBase(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.fake.routes[("POST", "/api/v1/device/consent")] = \
            lambda body, h: (200, {"ok": True, "consent_version": body["version"]})

    def enrolled_as(self, version):
        console.save({"url": URL, "device_id": DEVICE, "enrolled_at": updsig.iso(time.time()),
                      "consent_version": version}, TOKEN)
        if console.reports_problems(version):
            console.set_problems_baseline(self.con)
        if console.reports_usage(version):
            console.set_usage_baseline(self.con, time.time() - 3600)

    def round_(self, tid, outcome, at=None):
        at = time.time() if at is None else at
        self.con.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)",
                         (tid, at - 60, at, 0.1, outcome))

    def ticket(self, n, request="帮我出一张海报", cat=None):
        return self.new_ticket(request=request, message="om_t%d" % n, cat=cat)


class CategoryTest(UsageBase):
    def test_cat_from_main_session_or_keywords(self):
        """场景：主会话没给。"""
        a = self.ticket(1, cat="sheet")
        b = self.ticket(2, request="帮我出一张海报")
        c = self.ticket(3, request="找几段日落海边的空镜素材")
        d = self.ticket(4, request="聊聊天", cat="nonsense")
        cats = {r["id"]: r["cat"] for r in self.con.execute("SELECT id, cat FROM tickets")}
        self.assertEqual([cats[a], cats[b], cats[c], cats[d]], ["sheet", "media", "asset", "other"])

    def test_classify_rules(self):
        for text, want in (("写个视频脚本", "doc"), ("这个 python 脚本报错了", "code"), ("把这个 excel 汇总一下", "sheet"),
                           ("调研一下竞品", "research"), ("写一篇周报", "doc"), ("你好", "other")):
            self.assertEqual(usage.classify("", text), want, text)

    def test_cli_new_accepts_cat(self):
        code, out = self.cli("new", "--chat", "oc_1", "--message", "om_cli", "--title", "t", "--slug", "s",
                             "--request", "随便", "--ack", "开工", "--cat", "code")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.con.execute("SELECT cat FROM tickets").fetchone()[0], "code")


class UsagePartTest(UsageBase):
    def test_c2_device_sends_no_usage_and_c3_does(self):
        """场景：c2 设备。"""
        self.enrolled_as("c2")
        self.assertNotIn("usage", console.build_heartbeat(self.con))
        self.enrolled_as("c3")
        hb = console.build_heartbeat(self.con)
        self.assertEqual(hb["usage"], {"cats": [], "caps": [], "streak_fail": 0, "stuck_min": 0})
        self.assertIn("problems", hb)

    def test_cats_count_done_and_respect_baseline(self):
        """场景：同意之前的不补报。"""
        self.enrolled_as("c3")
        old = self.ticket(1)
        self.con.execute("UPDATE tickets SET created_at=? WHERE id=?", (time.time() - 7200, old))  # 同意之前
        a, b = self.ticket(2), self.ticket(3, request="写一篇周报")
        self.ticket(4)
        self.round_(a, "ready")
        self.round_(b, "failed")
        u = console.build_heartbeat(self.con)["usage"]
        today = usage.day_of(time.time())
        self.assertEqual(u["cats"], [{"day": today, "cat": "doc", "n": 1, "ok": 0},
                                     {"day": today, "cat": "media", "n": 2, "ok": 1}])
        body = json.dumps(u, ensure_ascii=False)
        for leak in ("海报", "周报", a):
            self.assertNotIn(leak, body)

    def test_streak_and_stuck(self):
        self.enrolled_as("c3")
        t = [self.ticket(i) for i in range(5)]
        now = time.time()
        self.round_(t[0], "ready", now - 500)
        self.round_(t[1], "failed", now - 400)
        self.round_(t[2], "blocked", now - 300)   # 要用户决定：跳过
        self.round_(t[3], "failed", now - 200)
        self.round_(t[3], "failed", now - 100)   # 同一张单只看最后一次
        self.assertEqual(usage.streak_fail(self.con), 2)
        self.round_(t[4], "ready", now - 50)
        self.assertEqual(usage.streak_fail(self.con), 0)
        self.con.execute("UPDATE tickets SET state='running', started_at=?, last_action_at=? WHERE id=?",
                         (now - 9000, now - 130 * 60, t[1]))
        self.con.execute("UPDATE tickets SET state='running', started_at=?, last_action_at=NULL, bg_wait='渲染' "
                         "WHERE id=?", (now - 9999, t[2]))  # 等后台任务的不算
        self.assertEqual(usage.stuck_min(self.con, now), 130)

    def test_old_console_unknown_usage_falls_back(self):
        self.enrolled_as("c3")
        seen = []

        def hb(body, h):
            seen.append(sorted(body))
            if "usage" in body:
                return 400, {"error": {"code": "UnknownField", "message": "usage"}}
            return 200, {"ok": True, "next_in": 300}
        self.fake.routes[("POST", "/api/v1/heartbeat")] = hb
        res = console.send_heartbeat(self.con)
        self.assertTrue(res["ok"])
        self.assertIn("usage", seen[0])
        self.assertNotIn("usage", seen[1])
        self.assertIn("problems", seen[1])


class CapCallsTest(UsageBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(skills, "installed", return_value=["asset-search"])
        p.start()
        self.addCleanup(p.stop)

    def test_only_installed_skill_scripts_count(self):
        root = skills.root()
        self.assertEqual(usage.skill_of_command(self.cmd()), "asset-search")
        self.assertIsNone(usage.skill_of_command(self.cmd("made-up-name")))  # 没装的名字不报（防夹带）
        self.assertIsNone(usage.skill_of_command("cat %s/current/skills/asset-search/SKILL.md" % root))
        self.assertIsNone(usage.skill_of_command("cat %s/current/skills/asset-search/scripts/mv_search.py" % root))
        self.assertIsNone(usage.skill_of_command("python3 %s/current/skills/asset-search/reference/x.py" % root))

    def cmd(self, name="asset-search"):
        return "python3 %s/current/skills/%s/scripts/mv_search.py search 日落" % (skills.root(), name)

    def test_parse_tool_calls_and_results(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": self.cmd()}},
            {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "ls /tmp"}},
            {"type": "tool_use", "id": "tu_3", "name": "Skill", "input": {"skill": "larkyard:asset-search"}}]}}
        self.assertEqual(usage.tool_calls(ev), [("tu_1", "asset-search")])
        res = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True,
             "content": [{"type": "text", "text": "Exit code 2\n不在公司内网：连不上素材库 10.0.0.1"}]},
            {"type": "tool_result", "tool_use_id": "tu_9", "content": "ok"}]}}
        self.assertEqual(usage.tool_results(res), [("tu_1", False, "exit_2"), ("tu_9", True, None)])

    def test_scheduler_records_calls_and_heartbeat_reports_them(self):
        self.enrolled_as("c3")
        tid = self.ticket(1, request="找素材")
        r = scheduler.Running(None, tid, str(self.tmp), [], 0, 0, 0)
        s = scheduler.Scheduler.__new__(scheduler.Scheduler)
        s.con = self.con
        s.log = lambda *a, **k: None
        for i, (ok, text) in enumerate(((True, "6 条"), (False, "Exit code 3"), (False, "boom"))):
            s._on_event(r, {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu_%d" % i, "name": "Bash", "input": {"command": self.cmd()}}]}})
            s._on_event(r, {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu_%d" % i, "is_error": not ok, "content": text}]}})
        self.assertEqual(r.cap_pending, {})
        caps = console.build_heartbeat(self.con)["usage"]["caps"]
        self.assertEqual(caps, [{"day": usage.day_of(time.time()), "cap": "asset-search", "n": 3, "ok": 1,
                                 "fails": [{"code": "exit_3", "n": 1}, {"code": "tool_error", "n": 1}]}])
        self.assertNotIn("日落", json.dumps(caps, ensure_ascii=False))


class AgreeTest(UsageBase):
    def setUp(self):
        super().setUp()
        self.enrolled_as("c2")
        self.assertTrue(console.maybe_offer_consent(self.con))
        self.assertFalse(console.maybe_offer_consent(self.con))  # 只私信一次
        self.offer_at = time.time()
        self.con.execute("UPDATE outbox SET status='sent', sent_message_id=?", (DM_ID,))

    def fetch(self, reply):
        def f(mid):
            if mid == DM_ID:
                return msg(text="📋 …", sender="ou_bot", at=self.offer_at - 1)
            if mid == REPLY_ID:
                return reply
            raise ValueError("没有")
        return f

    def agree_and_process(self, reply):
        self.assertIn("收到", console.agree(self.con, REPLY_ID))
        return console.process_agree(self.con, fetch=self.fetch(reply))

    def test_offer_dm_has_full_text(self):
        text = self.con.execute("SELECT text FROM outbox").fetchone()[0]
        self.assertIn(console.CONSENT_C3, text)
        self.assertIn("引用（回复）这条消息说「同意」", text)
        self.assertNotIn("出错记录」（", text)  # c2 已经在报出错记录，不说成新增
        self.assertIn("出错记录」（", console.offer_text(current="c1"))

    def test_reoffer_when_dm_dead_or_expired(self):
        self.assertFalse(console.maybe_offer_consent(self.con))
        self.con.execute("UPDATE outbox SET status='dead'")
        self.assertTrue(console.maybe_offer_consent(self.con))
        self.assertFalse(console.maybe_offer_consent(self.con, time.time() + 86400))
        self.assertTrue(console.maybe_offer_consent(self.con, time.time() + 31 * 86400))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 3)

    def test_replayed_agree_after_downgrade_is_refused(self):
        reply = msg(at=self.offer_at - 0.5)  # 本人回复「同意」在换清单之前
        self.assertIn("已换成采集清单 c3", self.agree_and_process(reply))
        time.sleep(0.01)
        with mock.patch.object(console, "_ask", side_effect=["yes"]):
            console.accept(self.con, "c2", out=self.say)
        self.assertEqual(console.consent_version(), "c2")
        out = self.agree_and_process(reply)  # 拿原来那条「同意」再换回去
        self.assertIn("早于你上一次换清单", out)
        self.assertEqual(console.consent_version(), "c2")

    def test_owner_reply_switches_to_c3(self):
        out = self.agree_and_process(msg(text="同意。"))
        self.assertIn("已换成采集清单 c3", out)
        self.assertEqual(console.consent_version(), "c3")
        self.assertEqual(self.fake.bodies("/api/v1/device/consent"),
                         [{"version": "c3", "sha256": console.consent_sha256("c3")}])
        self.assertIn("usage", console.build_heartbeat(self.con))
        self.assertIn("已换成采集清单 c3", self.con.execute("SELECT text FROM outbox ORDER BY id DESC").fetchone()[0])
        self.assertIsNone(console.process_agree(self.con, fetch=self.fetch(msg())))  # 只核对一次

    def test_rejections_keep_consent(self):
        """场景：别人发的「同意」。"""
        for reply, why in ((msg(sender="ou_" + "f" * 32), "不是你本人"),
                           (msg(sender=OWNER, sender_type="app"), "不是你本人"),
                           (msg(chat="oc_group"), "不是回复在"),
                           (msg(parent=None), "没有引用"),
                           (msg(parent="om_other"), "没有引用"),
                           (msg(sender_type=None), "不是你本人"),
                           (msg(at=self.offer_at - 100), "之前发的"),
                           (msg(text="不同意"), "不是「同意」"),
                           (msg(msg_type="post"), "不是「同意」")):
            out = self.agree_and_process(reply)
            self.assertIn(why, out)
            self.assertEqual(console.consent_version(), "c2")
        self.assertEqual(self.fake.bodies("/api/v1/device/consent"), [])

    def test_agree_needs_offer_and_valid_id(self):
        with self.assertRaises(ValueError):
            console.agree(self.con, "not-an-id")
        self.con.execute("DELETE FROM kv WHERE key=?", (console.OFFER_KEY,))
        with self.assertRaises(ValueError):
            console.agree(self.con, REPLY_ID)

    def test_agree_refused_in_worker_and_via_cli(self):
        with mock.patch.dict("os.environ", {"TICKETD_TICKET": "T0001"}):
            with self.assertRaises(PermissionError):
                console.command_console(self.con, "agree", message=REPLY_ID)
        code, out = self.cli("console", "agree")
        self.assertNotEqual(code, 0)
        self.assertIn("--message", out)
        code, out = self.cli("console", "agree", "--message", REPLY_ID)
        self.assertEqual(code, 0, out)
        self.assertIn("收到", out)

    def test_heartbeat_processes_pending_agree(self):
        console.agree(self.con, REPLY_ID)
        with mock.patch.object(console, "_fetch_message", side_effect=self.fetch(msg())):
            console.send_heartbeat(self.con)
        self.assertEqual(console.consent_version(), "c3")
        hb = self.fake.bodies("/api/v1/heartbeat")[-1]
        self.assertIn("usage", hb)  # 换完这一次心跳就按新清单报

    def test_console_without_c3_tells_to_upgrade(self):
        self.fake.routes[("POST", "/api/v1/device/consent")] = \
            (400, {"error": {"code": "ConsentUnknown", "message": ""}})
        out = self.agree_and_process(msg())
        self.assertIn("控制台要先升级", out)
        self.assertEqual(console.consent_version(), "c2")


class VersionCompareTest(UsageBase):
    def test_numeric_compare(self):
        with mock.patch.dict(console.CONSENTS, {"c10": "x\n"}):
            self.assertTrue(console.reports_usage("c10"))
            self.assertTrue(console.reports_problems("c10"))
        self.assertFalse(console.reports_usage("c2"))
        self.assertFalse(console.reports_problems(None))


class AcceptC3Test(UsageBase):
    def test_terminal_accept_c3_sets_usage_baseline(self):
        self.enrolled_as("c2")
        old = self.ticket(1)
        self.con.execute("UPDATE tickets SET created_at=? WHERE id=?", (time.time() - 60, old))
        with mock.patch.object(console, "_ask", side_effect=["yes"]):
            lines = console.accept(self.con, "c3", out=self.say)
        self.assertIn("已换成采集清单 c3", lines[0])
        self.assertEqual(console.build_heartbeat(self.con)["usage"]["cats"], [])
        self.assertIsNone(console.consent_note(self.con))

    def test_non_tty_accept_points_to_chat(self):
        self.enrolled_as("c2")
        with mock.patch.object(console, "_stdin_is_tty", return_value=False):
            with self.assertRaises(PermissionError) as cm:
                console.accept(self.con, "c3", out=self.say)
        self.assertIn("回复「同意」", str(cm.exception))

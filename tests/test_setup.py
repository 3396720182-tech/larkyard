"""ticket setup 向导：认主人（应用 owner / 4 位码）、跳过已做的步骤、服务、非终端时的行为。"""
from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase
from test_bridge import BridgeTestBase, FakeServices

from ticketd import bridge, config, setup, update


class ScriptedTerm(setup.Term):
    def __init__(self, answers=(), interactive=True):
        self.out = io.StringIO()
        super().__init__(fin=io.StringIO("".join(a + "\n" for a in answers)) if interactive else None, fout=self.out)
        if not interactive:
            self.fin = None


def fake_lark(owner="ou_owner_abc123", name="张三", fail=False):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if fail:
            return subprocess.CompletedProcess(argv, 1, "", "keychain not initialized")
        if "/open-apis/application/v6/applications/" in " ".join(argv):
            body = {"code": 0, "data": {"app": {"owner": {"owner_id": owner, "type": 2}}}}
        elif "/open-apis/contact/v3/users/" in " ".join(argv):
            body = {"code": 0, "data": {"user": {"name": name}}} if name else {"code": 99991672}
        else:
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
    runner.calls = calls
    return runner


class ConfigPutTest(TicketdTestCase):
    def test_fills_empty_appends_missing_keeps_existing(self):
        p = config.home() / "config.env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 注释\nTICKETD_OWNER=\n# TICKETD_X=1\n", encoding="utf-8")
        self.assertTrue(setup.config_put("TICKETD_OWNER", "ou_me"))
        self.assertFalse(setup.config_put("TICKETD_OWNER", "ou_other"))
        self.assertTrue(setup.config_put("TICKETD_X", "2"))
        self.assertEqual(p.read_text(encoding="utf-8"), "# 注释\nTICKETD_OWNER=ou_me\n# TICKETD_X=1\nTICKETD_X=2\n")
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")


class OwnerTest(BridgeTestBase):
    def setUp(self):
        super().setUp()
        os.environ.pop("TICKETD_OWNER", None)
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        (lc / "profiles" / "claude" / "logs").mkdir(parents=True)
        (lc / "config.json").write_text(json.dumps({"profiles": {"claude": {
            "accounts": {"app": {"id": "cli_x"}}, "workspaces": {"default": str(self.tmp / "ws")}}}}), encoding="utf-8")
        self.clock = [1000.0]

    def wizard(self, answers, runner):
        return setup.Wizard(term=ScriptedTerm(answers), services=FakeServices(), runner=runner,
                            sleep=lambda s: self.clock.__setitem__(0, self.clock[0] + s), clock=lambda: self.clock[0])

    def test_app_owner_confirmed(self):
        w = self.wizard(["y"], fake_lark())
        self.assertEqual(w.step_owner("claude"), "ou_owner_abc123")
        self.assertEqual(config.file_value("TICKETD_OWNER"), "ou_owner_abc123")
        self.assertIn("张三（…abc123）", w.t.out.getvalue())

    def test_existing_owner_skips(self):
        setup.config_put("TICKETD_OWNER", "ou_have")
        runner = fake_lark()
        self.assertEqual(self.wizard([], runner).step_owner("claude"), "ou_have")
        self.assertEqual(runner.calls, [])

    def test_not_me_falls_back_to_code_from_bridge_log(self):
        w = self.wizard(["n", "y"], fake_lark(name=""))
        log = Path(os.environ["LARK_CHANNEL_HOME"]) / "profiles/claude/logs" / (
            "bridge-%s.jsonl" % time.strftime("%Y%m%d"))
        real_sleep = w.sleep

        def sleep(s):
            # 用户在飞书里发了码：bridge 记一条 enter
            out = w.t.out.getvalue()
            code = out.split("认主人 ")[-1].split()[0]
            ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 5))
            log.write_text(json.dumps({"ts": ts, "event": "enter", "sender": "ou_real_me_999999",
                                       "preview": "认主人 %s" % code}) + "\n", encoding="utf-8")
            real_sleep(s)
        w.sleep = sleep
        got = w.step_owner("claude")
        self.assertEqual(got, "ou_real_me_999999")
        self.assertEqual(config.file_value("TICKETD_OWNER"), "ou_real_me_999999")

    def test_lookup_fails_and_nobody_sends_code(self):
        w = self.wizard([], fake_lark(fail=True))
        self.assertIsNone(w.step_owner("claude"))
        self.assertEqual(config.file_value("TICKETD_OWNER") or "", "")
        self.assertIn("手动填", w.t.out.getvalue())

    def test_code_must_be_after_prompt_and_exact(self):
        log = Path(os.environ["LARK_CHANNEL_HOME"]) / "profiles/claude/logs" / (
            "bridge-%s.jsonl" % time.strftime("%Y%m%d"))
        now = time.time()
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 600))
        new = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 1))
        log.write_text("\n".join(json.dumps(d) for d in (
            {"ts": old, "event": "enter", "sender": "ou_old", "preview": "认主人 1234"},
            {"ts": new, "event": "enter", "sender": "ou_long", "preview": "认主人 12345"},
            {"ts": new, "event": "queued", "sender": "ou_x", "preview": "1234"},
            {"ts": new, "event": "enter", "sender": "ou_group", "chatType": "group", "preview": "认主人 1234"},
            {"ts": new, "event": "enter", "sender": "ou_stranger", "chatType": "p2p", "preview": "1234年的预算表发一下"},
        )) + "\n", encoding="utf-8")
        self.assertIsNone(setup.sender_with_code("claude", "1234", now - 1))


class StepsTest(BridgeTestBase):
    def setUp(self):
        super().setUp()
        self.vd = self.make_current("0.3.0")
        self.plat_patch = mock.patch("ticketd.bridge.platform_key", return_value=self.plat())
        self.plat_patch.start()
        self.addCleanup(self.plat_patch.stop)
        bridge.ensure_runtime(self.vd, fetch=self.fetch)
        self.installs = []

    def wizard(self, answers=(), interactive=True, runner=None):
        return setup.Wizard(term=ScriptedTerm(answers, interactive), services=FakeServices(),
                            runner=runner or fake_lark(), run_install=lambda *a: self.installs.append(a),
                            sleep=lambda s: None, wait=lambda *a, **k: True)

    def test_app_step_skips_existing_profile_and_needs_terminal_otherwise(self):
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        lc.mkdir(parents=True)
        w = self.wizard(interactive=False)
        with self.assertRaises(setup.SetupError):
            w.step_app("claude", self.tmp / "ws")
        (lc / "config.json").write_text(json.dumps({"profiles": {"claude": {"workspaces": {"default": "/w"}}}}))
        self.assertEqual(w.step_app("claude", self.tmp / "ws"), "/w")

    def test_app_step_runs_bundled_bridge_with_terminal(self):
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            if argv[2:4] == ["profile", "create"]:
                lc.mkdir(parents=True, exist_ok=True)
                (lc / "config.json").write_text(json.dumps({"profiles": {"claude": {}}}))
            return subprocess.CompletedProcess(argv, 0, "", "")
        w = self.wizard(runner=runner)
        w.step_app("claude", self.tmp / "ws", app_id="cli_have")
        argv, kw = calls[0]
        self.assertEqual(argv[:2], [str(bridge.node_bin()), str(bridge.bridge_entry())])
        self.assertEqual(argv[2:], ["profile", "create", "claude", "--agent", "claude", "--workspace",
                                    str(self.tmp / "ws"), "--app-id", "cli_have"])
        self.assertIs(kw["stdin"], w.t.fin)
        self.assertTrue(kw["env"]["PATH"].startswith(str(bridge.node_link() / "bin")))
        self.assertEqual(calls[1][0][2:], ["profile", "use", "claude"])

    def test_services_written_and_loaded(self):
        w = self.wizard()
        self.assertTrue(w.step_services("claude"))
        sf = bridge.bridge_service_file("claude")
        self.assertTrue(bridge.is_managed_service(sf))
        loads = [c[1] for c in w.services.calls if c[0] == "load"]
        self.assertEqual(loads, ["ai.lark-channel-bridge.bot.claude", bridge.RESTART_LABEL])
        job = plistlib.loads(bridge.restart_service_file().read_bytes())
        self.assertEqual(job["ProgramArguments"][-5:], ["bridge", "restart", "--if-stale", "--profile", "claude"])
        self.assertEqual(len(self.installs), 1)
        repo, app, svc = self.installs[0]
        self.assertEqual(svc["env"]["LARK_CHANNEL_PROFILE"], "claude")

    def test_rerun_does_not_reload_or_reinstall(self):
        w = self.wizard()
        w.step_services("claude")
        td = bridge.restart_service_file("dev.larkyard.ticketd")
        td.write_bytes(plistlib.dumps({"Label": "dev.larkyard.ticketd",
                                       "ProgramArguments": ["/usr/bin/python3", str(update.app_dir() / "launcher.py")],
                                       "EnvironmentVariables": {"PATH": str(bridge.node_link() / "bin") + ":/usr/bin",
                                                                "TICKETD_HOME": os.environ["TICKETD_HOME"]}}))
        w2 = self.wizard()
        w2.step_services("claude")
        self.assertEqual(w2.services.calls, [])
        self.assertEqual(len(self.installs), 1)

    def test_services_leave_separately_installed_bridge_alone(self):
        sf = bridge.bridge_service_file("claude")
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_bytes(plistlib.dumps({"Label": "ai.lark-channel-bridge.bot.claude", "ProgramArguments": ["/n", "/b"]}))
        before = sf.read_bytes()
        w = self.wizard()
        self.assertFalse(w.step_services("claude"))
        self.assertEqual(sf.read_bytes(), before)
        self.assertIn("ticket bridge adopt", w.t.out.getvalue())
        self.assertEqual(len(self.installs), 1)  # ticketd 服务照装

    def test_run_needs_terminal_and_refuses_worker(self):
        self.assertEqual(setup.run(wizard=self.wizard(interactive=False)), 2)
        os.environ["TICKETD_SPOOL"] = "/x"
        with self.assertRaises(setup.SetupError):
            setup.run(wizard=self.wizard())

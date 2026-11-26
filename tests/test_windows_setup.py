"""原生 Windows 的一键安装：scripts/windows/bootstrap.ps1 + ticketd/winsetup.py（spec §3.2）。
在任何系统上 mock 成 Windows 跑：命令不真的执行，由假的 runner 记下并按需改文件。真 Windows 上的端到端冒烟在 CI 的
windows-bootstrap 任务里（真装 npm 工具和 Claude Code、真 clone，停在要扫码的那一步）。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
from pathlib import Path
from unittest import mock

from helpers import REPO, TicketdTestCase


def on_windows():
    from ticketd import host
    return mock.patch.object(host, "system", lambda: "windows")


class FakeRunner:
    """记下每条命令；按命令决定输出，必要时改文件（扫码建 profile）。"""

    def __init__(self, case, logged_in=(False, True), sandbox=None, create_ok=True, srt_rc=0):
        self.case, self.calls = case, []
        self.logged_in = list(logged_in)
        self.sandbox, self.create_ok, self.srt_rc = sandbox, create_ok, srt_rc

    def names(self):
        return [" ".join(str(a) for a in c[0][:3]) for c in self.calls]

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append((argv, kw))
        out, rc = "", 0
        if argv[:3] == ["claude", "auth", "status"]:
            v = self.logged_in.pop(0) if len(self.logged_in) > 1 else self.logged_in[0]
            out = json.dumps({"loggedIn": v})
        elif argv[0].endswith("srt-win.exe"):
            out = json.dumps(self.sandbox) if self.sandbox is not None else "garbage"
        elif argv[1:2] == ["windows-install"]:
            rc = self.srt_rc
        elif argv[:3] == ["lark-channel-bridge", "profile", "create"] and self.create_ok:
            self.case.write_profile(argv[3], argv[argv.index("--workspace") + 1])
        return subprocess.CompletedProcess(argv, rc, out, "")


class WinSetupTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        for k in ("LARK_CHANNEL", "LARK_CHANNEL_PROFILE", "LARKYARD_OWNER", "LARKYARD_ALLOW_LOOPBACK",
                  "LARKYARD_WORKSPACE", "TICKETD_WIN_SANDBOX") + ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                                                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            os.environ.pop(k, None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.tmp / "user")  # 不读本机真实的 ~/.claude
        (self.tmp / "user").mkdir()
        self.npm = self.tmp / "npm"
        self.npm.mkdir()
        self.installed = []

    def write_profile(self, profile, workspace):
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        lc.mkdir(parents=True, exist_ok=True)
        cfg = {"profiles": {profile: {"workspaces": {"default": workspace}, "accounts": {"app": {"id": "cli_x"}}}}}
        (lc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def fake_install(self, guided=False):
        self.installed.append(guided)
        return 0

    def run_setup(self, runner, answers="y\n", **patches):
        from ticketd import host, setup, winsetup, wininstall, worker
        term = setup.Term(fin=io.StringIO(answers), fout=io.StringIO())
        ws = winsetup.WinSetup(term=term, runner=runner, install=self.fake_install)
        found = {"node": r"C:\node\node.exe", "lark-cli": str(self.npm / "lark-cli.cmd"),
                 "claude": r"C:\Users\a\.local\bin\claude.exe"}
        with on_windows(), mock.patch.object(host, "argv", lambda cmd, path=None: list(cmd)), \
                mock.patch.object(host, "git_bash", lambda: patches.get("bash", r"C:\Git\bin\bash.exe")), \
                mock.patch.object(worker, "srt_path", lambda path=None: patches.get("srt", str(self.npm / "srt.cmd"))), \
                mock.patch.object(wininstall, "find_tools", lambda which=None: (found, [])), \
                mock.patch.object(setup.Wizard, "step_owner", lambda w, p: patches.get("owner")):
            rc = winsetup.run(ws=ws)
        return rc, term.fout.getvalue(), ws

    def test_fresh_machine_runs_every_step_in_order(self):
        os.environ["LARKYARD_OWNER"] = "ou_owner"
        r = FakeRunner(self)
        rc, out, ws = self.run_setup(r)
        self.assertEqual(rc, 0, out)
        names = r.names()
        order = ["claude auth status", "claude auth login", "srt.cmd windows-install", "lark-channel-bridge profile create",
                 "lark-channel-bridge profile use", "bash.exe", "lark-channel-bridge start --profile"]
        idx = [next(i for i, n in enumerate(names) if o in n) for o in order]
        self.assertEqual(idx, sorted(idx), names)
        self.assertEqual(self.installed, [True])  # 服务装在 bridge start 之前（ticket 要先在仓库 bin 里）
        self.assertTrue(names[-1].endswith("-c import sys; sys.path.insert(0, sys.argv.pop(1)); "
                                           "from ticketd.cli import main; sys.exit(main())"), names[-1])
        start = next(c for c in r.calls if c[0][:2] == ["lark-channel-bridge", "start"])
        path = start[1]["env"]["PATH"].split(os.pathsep)
        self.assertEqual(path[0], str(REPO / "bin"))  # bridge 的启动脚本只在 start 时记下 PATH
        self.assertIn(str(self.npm), path)
        bash = next(c for c in r.calls if c[0][0].endswith("bash.exe"))[0]
        self.assertTrue(bash[1].endswith("scripts/setup-workspace.sh") and "\\" not in bash[1])
        create = next(c for c in r.calls if c[0][1:3] == ["profile", "create"])[0]
        self.assertEqual(create[3:6], ["claude", "--agent", "claude"])
        self.assertTrue(create[-1].endswith("larkyard-workspace"))
        from ticketd import config
        self.assertEqual(config.file_value("TICKETD_OWNER"), "ou_owner")
        self.assertIn("装好了", out)
        self.assertIn("不要选别的电脑上在用的那个", out)

    def test_active_profile_reaches_service_env(self):
        """已有别的激活 profile 时，守护进程的 service.env 也要指向它，不能落回默认的 claude。"""
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        lc.mkdir(parents=True, exist_ok=True)
        (lc / "active-profile").write_text("work", encoding="utf-8")
        os.environ["LARKYARD_OWNER"] = "ou_owner"
        seen = {}

        def install(guided=False):
            from ticketd import wininstall
            seen["env"] = wininstall.service_env({})
            return 0
        self.fake_install = install
        r = FakeRunner(self)
        self.run_setup(r)
        self.assertTrue(seen["env"]["LARKSUITE_CLI_CONFIG_DIR"].replace(os.sep, "/").endswith("profiles/work/lark-cli"))
        start = next(c for c in r.calls if c[0][:2] == ["lark-channel-bridge", "start"])[0]
        self.assertEqual(start[-1], "work")

    def test_rerun_skips_finished_steps(self):
        wsdir = self.tmp / "ws"
        wsdir.mkdir()
        (wsdir / "CLAUDE.md").write_text("x", encoding="utf-8")
        self.write_profile("claude", str(wsdir))
        exe = self.npm / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "vendor" / "srt-win" / "x64" / "srt-win.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        from ticketd import config
        config.home().mkdir(parents=True, exist_ok=True)
        config.config_file().write_text("TICKETD_OWNER=ou_owner\n", encoding="utf-8")
        r = FakeRunner(self, logged_in=(True,), sandbox={"user": {"exists": True}, "cred_present": True,
                                                         "marker_version": 3, "setup_version": 3})
        rc, out, _ = self.run_setup(r)
        self.assertEqual(rc, 0, out)
        joined = " | ".join(r.names())
        for not_again in ("auth login", "windows-install", "profile create", "bash.exe"):
            self.assertNotIn(not_again, joined)
        self.assertIn("lark-channel-bridge start --profile", joined)  # 每次都 start：更新后 PATH 也跟着刷新
        self.assertEqual(self.installed, [True])
        self.assertIn("已有 TICKETD_OWNER", out)

    def test_sandbox_install_failure_is_a_warning(self):
        os.environ["LARKYARD_OWNER"] = "ou_owner"
        r = FakeRunner(self, srt_rc=1)
        rc, out, ws = self.run_setup(r)
        self.assertEqual(rc, 1)
        self.assertTrue(any("请 IT" in w for w in ws.warnings), ws.warnings)
        self.assertIn("lark-channel-bridge start --profile", " | ".join(r.names()))  # 其余步骤照做
        self.assertIn("还有 1 件事没完成", out)

    def test_loopback_flag_and_sandbox_off(self):
        os.environ.update({"LARKYARD_OWNER": "ou_owner", "LARKYARD_ALLOW_LOOPBACK": "1"})
        r = FakeRunner(self)
        self.run_setup(r)
        inst = next(c for c in r.calls if c[0][1:2] == ["windows-install"])[0]
        self.assertEqual(inst[2:], ["--allow-loopback"])
        exe = self.npm / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "vendor" / "srt-win" / "x64" / "srt-win.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        r = FakeRunner(self, logged_in=(True,), sandbox={"user": {"exists": True}, "cred_present": True})
        with mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}):
            self.run_setup(r)  # 已装好、这次要放开本机端口：配置不同，要 --force 替换
        inst = next(c for c in r.calls if c[0][1:2] == ["windows-install"])[0]
        self.assertEqual(inst[2:], ["--allow-loopback", "--force"])
        os.environ["TICKETD_WIN_SANDBOX"] = "off"
        r = FakeRunner(self, logged_in=(True,))
        rc, out, ws = self.run_setup(r)
        self.assertNotIn("windows-install", " | ".join(r.names()))
        self.assertTrue(any("TICKETD_WIN_SANDBOX=off" in w for w in ws.warnings))

    def test_no_owner_yet_is_a_warning(self):
        r = FakeRunner(self)
        rc, out, ws = self.run_setup(r, owner=None)
        self.assertEqual(rc, 1)
        self.assertTrue(any("还没认主人" in w for w in ws.warnings))

    def test_not_interactive_stops_at_qr_scan(self):
        from ticketd import host, setup, winsetup, worker
        r = FakeRunner(self)
        term = setup.Term(fin=None, fout=io.StringIO())
        term.fin = None
        ws = winsetup.WinSetup(term=term, runner=r, install=self.fake_install)
        with on_windows(), mock.patch.object(host, "argv", lambda cmd, path=None: list(cmd)), \
                mock.patch.object(worker, "srt_path", lambda path=None: ""):
            with self.assertRaises(winsetup.NeedTerminal) as e:
                winsetup.run(ws=ws)
        self.assertIn("扫码", str(e.exception))
        with mock.patch.object(winsetup, "run", side_effect=e.exception):
            self.assertEqual(winsetup.main([]), 3)  # CI 靠退出码 3 区分「按预期停在扫码」和崩溃
        with mock.patch.object(winsetup, "run", side_effect=winsetup.WinSetupError("x")):
            self.assertEqual(winsetup.main([]), 1)
        self.assertNotIn("auth login", " | ".join(r.names()))  # 不在终端里不去开浏览器登录
        self.assertTrue(any("claude auth login" in w for w in ws.warnings))

    def test_refuses_in_worker_bridge_session_and_off_windows(self):
        from ticketd import winsetup
        with self.assertRaises(winsetup.WinSetupError):
            winsetup.run(ws=object())
        with on_windows():
            for k in ("TICKETD_TICKET", "LARK_CHANNEL"):
                os.environ[k] = "1"
                with self.assertRaises(winsetup.WinSetupError):
                    winsetup.run(ws=object())
                os.environ.pop(k)

    def test_bad_owner_env(self):
        from ticketd import winsetup
        os.environ["LARKYARD_OWNER"] = "someone"
        with self.assertRaises(winsetup.WinSetupError):
            winsetup.WinSetup(term=mock.Mock(), runner=FakeRunner(self)).step_owner("claude")

    def test_sandbox_ready(self):
        from ticketd import winsetup
        srt = str(self.npm / "srt.cmd")
        self.assertIsNone(winsetup.sandbox_ready(srt, FakeRunner(self)))  # 没有 srt-win.exe：照装
        exe = winsetup.srt_win_exe(srt, "x64")
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        with mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}):
            for status, want in (
                    ({"user": {"exists": True}, "cred_present": True, "marker_version": 3, "setup_version": 3}, True),
                    ({"user": {"exists": True}, "cred_present": True}, True),
                    ({"user": {"exists": True}, "cred_present": True, "marker_version": 2, "setup_version": 3}, False),
                    ({"user": {"exists": False}, "cred_present": False}, False),
                    ({"user": {"exists": True}, "cred_present": False}, False),
                    (None, None)):
                self.assertEqual(winsetup.sandbox_ready(srt, FakeRunner(self, sandbox=status)), want, status)
        self.assertEqual(winsetup.srt_win_exe(srt, "arm64").parent.name, "arm64")

    def test_api_configured(self):
        from ticketd import winsetup
        s = self.tmp / "settings.json"
        self.assertFalse(winsetup.api_configured({}, s))
        self.assertTrue(winsetup.api_configured({"ANTHROPIC_API_KEY": "k"}, s))
        s.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://gw"}}), encoding="utf-8")
        self.assertTrue(winsetup.api_configured({}, s))
        s.write_text(json.dumps({"apiKeyHelper": "x.cmd"}), encoding="utf-8")
        self.assertTrue(winsetup.api_configured({}, s))
        s.write_text(json.dumps({"apiKeyHelper": "x.cmd"}), encoding="utf-8-sig")
        self.assertTrue(winsetup.api_configured({}, s))  # 带 BOM 的也认
        s.write_text("not json", encoding="utf-8")
        self.assertFalse(winsetup.api_configured({}, s))

    def test_claude_logged_in_unknown(self):
        from ticketd import host, winsetup
        with mock.patch.object(host, "argv", lambda cmd, path=None: list(cmd)):
            self.assertIsNone(winsetup.claude_logged_in(lambda a, **k: subprocess.CompletedProcess(a, 1, "nope", "")))
            self.assertTrue(winsetup.claude_logged_in(
                lambda a, **k: subprocess.CompletedProcess(a, 0, '{"loggedIn": true}', "")))

    def test_service_path_dedups_case_insensitively(self):
        from ticketd import winsetup
        p = winsetup.service_path({"node": "/n/node.exe", "lark-cli": "/npm/lark-cli.cmd"},
                                  os.pathsep.join(["/npm", "/other", str(REPO / "bin")]))
        self.assertEqual(p.split(os.pathsep), [str(REPO / "bin"), "/n", "/npm", "/other"])

    def test_setup_lark_json_on_windows(self):
        """认主人复用 ticket setup 的 lark-cli 调用：Windows 上走 npm 装的 lark-cli、沿用 PATH，不用受管安装的路径。"""
        from ticketd import host, setup
        seen = {}

        def runner(argv, **kw):
            seen["argv"], seen["path"] = argv, kw["env"]["PATH"]
            return subprocess.CompletedProcess(argv, 0, '{"data": {"app": {"owner": {"owner_id": "ou_owner"}}}}', "")
        os.environ["PATH"] = "C:\\npm" + os.pathsep + "C:\\Windows"
        with on_windows(), mock.patch.object(host, "argv", lambda cmd, path=None: ["node", "lark.js"] + list(cmd[1:])):
            self.assertEqual(setup.app_owner("claude", "cli_x", runner), "ou_owner")
        self.assertEqual(seen["argv"][:3], ["node", "lark.js", "api"])
        self.assertEqual(seen["path"], os.environ["PATH"])
        seen.clear()
        setup.app_owner("claude", "cli_x", runner)  # 其他平台不变
        self.assertTrue(seen["argv"][0].endswith("lark-cli"))
        self.assertIn("/usr/bin", seen["path"].split(":"))

    def test_owner_wording_is_rewritten(self):
        from ticketd import winsetup
        out = io.StringIO()
        from ticketd import setup
        t = winsetup._Reword(setup.Term(fin=io.StringIO("y\n"), fout=out), winsetup.OWNER_REWORD)
        t.say("④ 没查到；或重新执行 ticket setup")
        self.assertTrue(t.yes("④ 是你吗？"))
        self.assertEqual(out.getvalue(), "⑦ 没查到；或重跑安装命令\n⑦ 是你吗？ [Y/n] ")


class BootstrapScriptTest(TicketdTestCase):
    def test_bootstrap_rules(self):
        """irm | iex 在用户自己的会话里执行：不能 exit（会关掉窗口）；PowerShell 5.1 按 ANSI 读无 BOM 文件：只用 ASCII；
        npm.ps1 在默认执行策略下被拦：用 npm.cmd。"""
        text = (REPO / "scripts" / "windows" / "bootstrap.ps1").read_text(encoding="utf-8")
        text.encode("ascii")
        code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
        self.assertIsNone(re.search(r"(?im)^\s*exit\b|[;{}]\s*exit\b", code))
        self.assertNotRegex(code, r"&\s*npm\s")
        self.assertIn("npm.cmd install -g", code)
        self.assertIn("-m ticketd.winsetup", code)
        self.assertIn("Remove-Item Function:\\Install-Larkyard", code)
        self.assertIn("-notlike '*WindowsApps*'", code)  # 应用商店版 Python 的注册表 / AppData 写入是隔离的
        # 安装说明里的一行命令指向的就是这个文件
        for doc in ("README.md", "README.en.md"):
            self.assertIn("scripts/windows/bootstrap.ps1 | iex", (REPO / doc).read_text(encoding="utf-8"), doc)

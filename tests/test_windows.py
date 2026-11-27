"""原生 Windows 适配（spec §3.2）。

两类用例：
- WindowsBranchTest：在任何系统上 mock host.system() == "windows"，测纯逻辑（权限规则路径、srt 设置、worker 环境、
  启动前检查、命令解析、计划任务 XML、托管循环、不支持的命令）。
- Win32Test：只在真 Windows 上跑（CI 的 windows-latest），测 ctypes 调系统接口：作业对象结束整棵进程树、
  查进程、文件真实路径、safefs 拒绝联接点、文件锁、收汇报。
另外 WrapperTest 在 POSIX 上用假的 srt 跑 bin/ticketd-wsandbox（它是 Git Bash 里执行的 sh 脚本）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, TicketdTestCase

WIN = sys.platform == "win32"


def on_windows():
    from ticketd import host
    return mock.patch.object(host, "system", lambda: "windows")


class WindowsBranchTest(TicketdTestCase):
    def ticket(self, executor="claude"):
        work = self.tmp / "work" / "T0001-x"
        work.mkdir(parents=True, exist_ok=True)
        return {"id": "T0001", "workdir": str(work), "executor": executor, "session_id": "s-1",
                "session_started": 0}

    def test_system_maps_win32(self):
        from ticketd import host
        with mock.patch.object(host.sys, "platform", "win32"):
            self.assertEqual(host.system(), "windows")
            self.assertTrue(host.is_windows())
            self.assertFalse(host.is_linux())

    def test_rule_paths(self):
        from ticketd import perms
        with on_windows():
            self.assertEqual(perms.rule_paths(r"C:\Users\a\work"), ["/c/Users/a/work", "/C/Users/a/work"])
            self.assertEqual(perms.rule_paths("D:\\"), ["/d", "/D"])
            self.assertEqual(perms.rule_paths(r"\\srv\share\x"), ["//srv/share/x"])
        self.assertEqual(perms.rule_paths("/Users/a"), ["/Users/a"])  # 其他平台原样

    def test_settings_windows(self):
        from ticketd import perms
        t = self.ticket()
        with on_windows(), mock.patch.object(perms, "writable_paths",
                                             lambda w, g: [r"C:\Users\a\work\T0001-x", r"D:\proj"]), \
                mock.patch.object(perms, "secret_dirs", lambda: [r"C:\Users\a\.ticketd\console"]), \
                mock.patch.object(perms, "readonly_dirs", lambda: [r"C:\Users\a\.ticketd\skills"]):
            s = perms.build_settings(t["workdir"], [])
        allow, deny = s["permissions"]["allow"], s["permissions"]["deny"]
        self.assertIn("Edit(//c/Users/a/work/T0001-x/**)", allow)
        self.assertIn("Write(//D/proj/**)", allow)
        self.assertIn("Read(//c/Users/a/.ticketd/console/**)", deny)
        self.assertIn("Read(//C/Users/a/.ticketd/console/**)", deny)
        self.assertIn("Edit(//c/Users/a/.ticketd/skills/**)", deny)
        self.assertIn("PowerShell", deny)  # 不经过 shell 前缀的工具一律禁用
        self.assertIn("Bash", allow)
        # Claude Code 的沙箱不支持原生 Windows：开着 + failIfUnavailable 会让它起不来
        self.assertEqual(s["sandbox"], {"enabled": False})
        self.assertNotIn("allowMachLookup", json.dumps(s))

    def test_srt_settings(self):
        from ticketd import perms
        t = self.ticket()
        with on_windows():
            s = perms.srt_settings(t["workdir"], [str(self.tmp)], net={"local": True})
            p = perms.write_settings("T0001", t["workdir"], [])
        fs = s["filesystem"]
        self.assertEqual(fs["allowWrite"][:2], [str(Path(t["workdir"]).resolve()), str(self.tmp.resolve())])
        self.assertTrue(any(d.endswith("console") for d in fs["denyRead"]))
        self.assertTrue(any(d.endswith("diag") for d in fs["denyRead"]))
        self.assertTrue(any(d.endswith("skills") for d in fs["denyWrite"]))
        self.assertEqual(s["network"]["allowedDomains"], ["*"])
        self.assertTrue(s["network"]["allowLocalBinding"])
        self.assertTrue(perms.srt_settings_path("T0001").exists())  # write_settings 在 Windows 上顺带写 srt 设置
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["sandbox"], {"enabled": False})

    def test_codex_roots_skip_registry_backed_lark_dir(self):
        from ticketd import host, perms
        t = self.ticket("codex")
        with on_windows():
            self.assertEqual(host.lark_cli_data_dir(), "")
            roots = perms.codex_writable_roots(t["workdir"], [])
        self.assertNotIn("", roots)

    def test_codex_windows_sandbox_arg(self):
        from ticketd import executors
        t = self.ticket("codex")
        with on_windows():
            args = executors.get("codex").config_args(t, [], "p")
        self.assertIn('windows.sandbox="unelevated"', args)
        os.environ["TICKETD_CODEX_WIN_SANDBOX"] = "elevated"
        with on_windows():
            args = executors.get("codex").config_args(t, [], "p")
        self.assertIn('windows.sandbox="elevated"', args)
        with mock.patch("ticketd.host.system", lambda: "darwin"):
            args = executors.get("codex").config_args(t, [], "p")  # 其他平台不加
        self.assertFalse(any(a.startswith("windows.") for a in args))

    def test_worker_env_srt(self):
        from ticketd import host, worker
        t = self.ticket()
        os.environ["TICKETD_WORKER_ENV"] = "MY_TOKEN,bad name"
        with on_windows(), mock.patch.object(host, "git_bash", lambda: r"C:\Program Files\Git\bin\bash.exe"), \
                mock.patch.object(worker, "srt_path", lambda path=None: r"C:\npm\srt.cmd"):
            env = worker.worker_env(t)
        self.assertEqual(env["CLAUDE_CODE_USE_POWERSHELL_TOOL"], "0")
        self.assertEqual(env["CLAUDE_CODE_GIT_BASH_PATH"], r"C:\Program Files\Git\bin\bash.exe")
        self.assertEqual(env["TICKETD_BASH"], "C:/Program Files/Git/bin/bash.exe")
        self.assertTrue(env["CLAUDE_CODE_SHELL_PREFIX"].endswith("bin/ticketd-wsandbox"))
        self.assertNotIn("\\", env["CLAUDE_CODE_SHELL_PREFIX"])
        self.assertEqual(env["TICKETD_SRT"], "C:/npm/srt.cmd")
        self.assertTrue(env["TICKETD_SRT_SETTINGS"].endswith("T0001.srt.json"))
        names = env["TICKETD_SANDBOX_ENV"].split()
        for n in ("TICKETD_SPOOL", "TICKETD_TICKET", "TICKETD_PYTHON", "MY_TOKEN"):
            self.assertIn(n, names)
        self.assertNotIn("bad name", env["TICKETD_SANDBOX_ENV"])
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_worker_env_off_and_other_platforms(self):
        from ticketd import host, worker
        t = self.ticket()
        os.environ["TICKETD_WIN_SANDBOX"] = "off"
        os.environ["CLAUDE_CODE_SHELL_PREFIX"] = "/somewhere/else"
        with on_windows(), mock.patch.object(host, "git_bash", lambda: ""):
            env = worker.worker_env(t)
        self.assertNotIn("CLAUDE_CODE_SHELL_PREFIX", env)  # 不继承守护进程环境里的前缀
        self.assertEqual(env["CLAUDE_CODE_USE_POWERSHELL_TOOL"], "0")
        with mock.patch.object(host, "system", lambda: "darwin"):
            env = worker.worker_env(t)
        self.assertNotIn("CLAUDE_CODE_USE_POWERSHELL_TOOL", env)  # macOS 的 worker 环境不变

    def test_preflight_fails_closed(self):
        from ticketd import host, worker
        t = self.ticket()
        with on_windows(), mock.patch.object(host, "git_bash", lambda: ""):
            with self.assertRaisesRegex(OSError, "Git Bash"):
                worker.windows_preflight(t)
        with on_windows(), mock.patch.object(host, "git_bash", lambda: "C:/bash.exe"), \
                mock.patch.object(worker, "srt_path", lambda path=None: ""):
            with self.assertRaisesRegex(OSError, "sandbox-runtime"):
                worker.windows_preflight(t)
            os.environ["TICKETD_WIN_SANDBOX"] = "off"
            worker.windows_preflight(t)  # 用户明确关掉沙箱：不拦
            worker.windows_preflight(self.ticket("codex"))
        worker.windows_preflight(t)  # 其他平台什么都不做

    def test_preflight_failure_keeps_input_pending(self):
        """起不来时按「无法启动 worker」处理：输入留着，用户装好后回复继续。"""
        from ticketd import core, db, host, worker
        from ticketd.scheduler import Scheduler
        con = db.connect()
        tid = core.create_ticket(con, chat_id="oc_1", message_id="om_1", title="t", slug="x",
                                 request_text="做点事", ack_text="开工")
        with on_windows(), mock.patch.object(host, "git_bash", lambda: "C:/bash.exe"), \
                mock.patch.object(worker, "srt_path", lambda path=None: ""):
            sched = Scheduler(con)
            sched.tick()
        t = core.get(con, tid)
        self.assertEqual(t["state"], "failed")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM inputs WHERE ticket_id=? AND status='pending'",
                                     (tid,)).fetchone()[0], 1)
        rep = con.execute("SELECT summary FROM reports WHERE ticket_id=? ORDER BY id DESC", (tid,)).fetchone()
        self.assertIn("无法启动", rep[0])

    def test_overlaps_case_insensitive(self):
        from ticketd import scheduler
        with on_windows():
            self.assertTrue(scheduler.overlaps(r"C:\Users\A\proj", r"c:/users/a/PROJ/sub"))
            self.assertFalse(scheduler.overlaps(r"C:\Users\A\proj", r"C:\Users\A\project"))
        with mock.patch("ticketd.host.system", lambda: "darwin"):
            self.assertFalse(scheduler.overlaps("/a/Proj", "/a/proj"))  # 其他平台照旧区分大小写

    def test_exe_argv_resolves_npm_shims(self):
        from ticketd import host
        d = self.tmp / "npm"
        (d / "node_modules" / "@larksuite" / "cli" / "scripts").mkdir(parents=True)
        (d / "node.exe").write_text("")
        shim = d / "lark-cli.cmd"
        # npm cmd-shim 生成的原文（含前面那行 IF EXIST "%dp0%\node.exe"）
        shim.write_text("\r\n".join([
            "@ECHO off", "GOTO start", ":find_dp0", "SET dp0=%~dp0", "EXIT /b", ":start", "SETLOCAL", "CALL :find_dp0", "",
            'IF EXIST "%dp0%\\node.exe" (', '  SET "_prog=%dp0%\\node.exe"', ") ELSE (", '  SET "_prog=node"',
            "  SET PATHEXT=%PATHEXT:;.JS;=;%", ")", "",
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            '"%dp0%\\node_modules\\@larksuite\\cli\\scripts\\run.js" %*', ""]))
        exe_shim = d / "claude.cmd"
        exe_shim.write_text('@ECHO off\r\n"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n')
        bad = d / "weird.bat"
        bad.write_text("@echo off\r\nsomething %*\r\n")
        with on_windows():
            argv = host.argv([str(shim), "im", "--text", 'a & b "c" %PATH%'])
            self.assertEqual(argv[0], str(d / "node.exe"))
            self.assertTrue(argv[1].replace(os.sep, "/").endswith("node_modules/@larksuite/cli/scripts/run.js"))
            self.assertEqual(argv[2:], ["im", "--text", 'a & b "c" %PATH%'])  # 文字原样，不经 cmd.exe
            self.assertTrue(host.exe_argv(str(exe_shim))[0].replace(os.sep, "/").endswith(
                "@anthropic-ai/claude-code/bin/claude.exe"))
            with self.assertRaisesRegex(OSError, "批处理"):
                host.exe_argv(str(bad))
            self.assertEqual(host.exe_argv(r"C:\x\claude.exe"), [r"C:\x\claude.exe"])
        self.assertEqual(host.argv(["lark-cli", "x"]), ["lark-cli", "x"])  # 其他平台原样

    def test_job_names_stable_and_distinct(self):
        from ticketd import procs
        a = procs.job_name(str(self.tmp / "T1" / ".reports"))
        self.assertEqual(a, procs.job_name(str(self.tmp / "T1" / ".reports")))
        self.assertNotEqual(a, procs.job_name(str(self.tmp / "T2" / ".reports")))
        self.assertTrue(a.startswith("Local\\ticketd-"))

    def test_signal_group_uses_job_on_windows(self):
        from ticketd import procs, winapi
        killed = []
        with on_windows(), mock.patch.object(winapi, "terminate_job", lambda name, code=1: killed.append(name) or 2):
            procs._JOBS[4242] = ("Local\\ticketd-x", None)
            try:
                procs.signal_group(4242, 15)
                procs.signal_group(4343, 15, fallback=False)  # 不认识、又不许退回：什么都不做
            finally:
                procs._JOBS.pop(4242, None)
            self.assertEqual(killed, ["Local\\ticketd-x"])
            self.assertEqual(procs.kill_job("k"), 2)
        # 作业按名字找不到了（托管进程不在）：退回结束 pid 本身
        hits = []
        with on_windows(), mock.patch.object(winapi, "terminate_job", lambda name, code=1: 0), \
                mock.patch.object(procs.os, "kill", lambda pid, sig: hits.append(pid)):
            procs._JOBS[4444] = ("Local\\ticketd-y", None)
            try:
                procs.signal_group(4444, 15)
                procs.signal_group(4444, 15, fallback=False)
            finally:
                procs._JOBS.pop(4444, None)
        self.assertEqual(hits, [4444])

    def test_unsupported_commands(self):
        from ticketd import cli
        with on_windows():
            for argv in (["update", "status"], ["bridge", "status"], ["setup"], ["migrate", "--dry-run"]):
                with mock.patch("sys.stderr") as err:
                    self.assertEqual(cli.main(argv), 2, argv)
                self.assertIn("Windows", "".join(c.args[0] for c in err.write.call_args_list))

    def test_task_xml(self):
        from ticketd import wininstall
        xml = wininstall.task_xml("CORP\\a&b")
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)  # 不要 72 小时后被停掉
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", xml)
        self.assertIn("<UserId>CORP\\a&amp;b</UserId>", xml)
        self.assertIn("-m ticketd.winsvc run --home", xml)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", xml)

    def test_service_env_and_path(self):
        from ticketd import wininstall
        env = wininstall.service_env({"lark-cli": "/x/npm/lark-cli.cmd", "node": "/x/node/node.exe",
                                      "claude": "/x/npm/claude.cmd"}, env={"PATH": "/x/npm" + os.pathsep + "/y"})
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(REPO / "bin"))
        self.assertEqual(parts.count("/x/npm"), 1)
        self.assertTrue(env["LARKSUITE_CLI_CONFIG_DIR"].replace(os.sep, "/").endswith("profiles/claude/lark-cli"))
        self.assertEqual(env["LARK_CHANNEL"], "1")
        self.assertEqual(wininstall.path_with(r"C:\a;C:\Repo\bin\;C:\b", r"c:\repo\bin", True) if WIN else
                         wininstall.path_with("/a;/r/bin;/b", "/r/bin", True), r"c:\repo\bin;C:\a;C:\b" if WIN else
                         "/r/bin;/a;/b")
        self.assertEqual(wininstall.path_with("/a;/r/bin", "/r/bin", False), "/a")

    def test_install_refuses_off_windows(self):
        from ticketd import wininstall
        if WIN:
            self.skipTest("在 Windows 上会真的去装")
        with self.assertRaises(wininstall.InstallError):
            wininstall.install()
        with self.assertRaises(wininstall.InstallError):
            wininstall.uninstall()

    def test_winsvc_restarts_and_stops(self):
        from ticketd import config, winsvc
        config.home().mkdir(parents=True, exist_ok=True)
        winsvc.service_env_path().write_text("# c\nFOO=bar\nPATH=/p\n", encoding="utf-8")
        seen, sleeps = [], []

        class P:
            def __init__(self, code):
                self.code = code

            def wait(self):
                return self.code

        codes = iter([1, winsvc.EXIT_RESTART, 0])

        def popen(cmd, **kw):
            seen.append(kw["env"])
            code = next(codes)
            if code == 0:
                winsvc.stop_path().write_text("stop", encoding="utf-8")  # 第三次：有人请求停机
            return P(code)

        self.assertEqual(winsvc.run(sleep=sleeps.append, popen=popen), 0)
        self.assertEqual(len(seen), 3)
        self.assertEqual(sleeps, [winsvc.RESTART_DELAY])  # 崩溃后等 10 秒；因更新退出立即重启
        self.assertEqual(seen[0]["FOO"], "bar")
        self.assertEqual(seen[0]["PATH"], "/p")
        self.assertEqual(seen[0]["TICKETD_SUPERVISOR_PID"], str(os.getpid()))
        self.assertFalse(winsvc.stop_path().exists())
        self.assertFalse(winsvc.pid_path().exists())
        self.assertIn("from ticketd.daemon import main", " ".join(winsvc.daemon_command()))

    def test_winsvc_home_arg(self):
        from ticketd import winsvc
        with mock.patch.object(winsvc, "status", lambda: "ok"):
            winsvc.main(["--home", str(self.tmp / "h2"), "status"])
        self.assertEqual(os.environ["TICKETD_HOME"], str(self.tmp / "h2"))

    def test_daemon_stops_on_stop_file(self):
        """Windows 上没有可投递的 SIGTERM：停机文件让主循环按正常停机流程退出。"""
        from ticketd import config, daemon
        config.db_dir().mkdir(parents=True, exist_ok=True)
        calls = {"n": 0}

        def tick(self_):
            calls["n"] += 1
            if calls["n"] == 2:
                daemon.stop_request_path().write_text("stop", encoding="utf-8")

        with on_windows(), mock.patch.object(daemon.Scheduler, "tick", tick), \
                mock.patch.object(daemon.time, "sleep", lambda s: None), \
                mock.patch.object(daemon, "_sync_workspace", lambda: None), \
                mock.patch.object(daemon.MediaLane, "ensure", lambda self_: None), \
                mock.patch.object(daemon.console.Heartbeat, "tick", lambda self_: None), \
                mock.patch("sys.stdout"):
            self.assertEqual(daemon.main([]), 0)
        self.assertEqual(calls["n"], 2)


@unittest.skipIf(WIN, "POSIX 上测 Git Bash 里跑的 sh 包装脚本")
class WrapperTest(TicketdTestCase):
    def run_wrapper(self, command, **env):
        fake = self.tmp / "fakesrt"
        # 假的 srt：核对参数形状，用空环境（只留 PATH）跑，模拟 srt-sandbox 账户拿到的干净环境
        fake.write_text('#!/bin/sh\n[ "$1" = --settings ] && [ "$2" = "$EXPECT_SETTINGS" ] || exit 99\n'
                        'shift 2\nexec env -i PATH="$PATH" "$@"\n')
        fake.chmod(0o755)
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        base = {"PATH": os.environ["PATH"], "TICKETD_SRT": str(fake), "TICKETD_SRT_SETTINGS": "/s/T1.srt.json",
                "EXPECT_SETTINGS": "/s/T1.srt.json", "TICKETD_BASH": "/bin/sh",
                "TICKETD_SANDBOX_DIR": str(self.tmp / "sb")}
        base.update(env)
        return subprocess.run([str(REPO / "bin" / "ticketd-wsandbox"), command], cwd=str(work), env=base,
                              capture_output=True, text=True)

    def test_env_cwd_and_exit_code_pass_through(self):
        p = self.run_wrapper('echo "$TICKETD_SPOOL|$X|${UNSET-none}|$(basename "$(pwd)")"; exit 7',
                             TICKETD_SANDBOX_ENV="TICKETD_SPOOL X BAD-NAME UNSET",
                             TICKETD_SPOOL="/a b/it's/.reports", X="$(echo pwned)")
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertEqual(p.stdout.strip(), "/a b/it's/.reports|$(echo pwned)|none|work")
        self.assertEqual(list((self.tmp / "sb").iterdir()), [])  # 命令脚本用完即删

    def test_refuses_without_sandbox(self):
        p = self.run_wrapper("echo hi", TICKETD_SRT="")
        self.assertEqual(p.returncode, 126)
        self.assertNotIn("hi", p.stdout)
        self.assertIn("拒绝在沙箱外执行", p.stderr)


@unittest.skipUnless(WIN, "只在真 Windows 上跑（CI windows-latest）")
class Win32Test(TicketdTestCase):
    CHILD = ("import subprocess, sys, time\n"
             "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
             "open(sys.argv[1], 'w').write(str(p.pid))\n"
             "time.sleep(120)\n")

    def wait_file(self, p, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if p.exists() and p.read_text().strip():
                return p.read_text().strip()
            time.sleep(0.1)
        self.fail("超时：%s" % p)

    def test_job_kills_whole_tree_including_detached_grandchild(self):
        from ticketd import procs
        marker = self.tmp / "gc.pid"
        key = str(self.tmp / "T1" / ".reports")
        proc = procs.spawn([sys.executable, "-c", self.CHILD, str(marker)], key, stdin=subprocess.DEVNULL)
        gc = int(self.wait_file(marker))
        self.assertFalse(procs.is_gone(proc.pid))
        self.assertFalse(procs.is_gone(gc))
        self.assertIn("time.sleep(120)", procs.command_line(proc.pid))
        kids = [pid for pid, _, _ in procs.tree(proc.pid)]
        self.assertIn(gc, kids)
        proc.kill()  # 父进程先死：孙进程成了孤儿，但还在作业里
        proc.wait(timeout=10)
        self.assertFalse(procs.is_gone(gc))
        self.assertGreaterEqual(procs.kill_job(key), 1)  # 可能还算上了无窗口控制台的 conhost.exe
        for _ in range(100):
            if procs.is_gone(gc):
                break
            time.sleep(0.1)
        self.assertTrue(procs.is_gone(gc))
        procs.forget(proc.pid)
        self.assertEqual(procs.kill_job(key), 0)

    def test_signal_group_terminates_job(self):
        from ticketd import procs
        marker = self.tmp / "gc2.pid"
        proc = procs.spawn([sys.executable, "-c", self.CHILD, str(marker)], str(self.tmp / "T2"),
                           stdin=subprocess.DEVNULL)
        gc = int(self.wait_file(marker))
        procs.signal_group(proc.pid, 15)
        proc.wait(timeout=10)
        for _ in range(100):
            if procs.is_gone(gc):
                break
            time.sleep(0.1)
        self.assertTrue(procs.is_gone(gc))
        procs.forget(proc.pid)

    def test_spawn_resolves_bare_name_via_env_path(self):
        from ticketd import procs
        exe_dir = str(Path(sys.executable).parent)
        env = dict(os.environ, PATH=exe_dir + os.pathsep + os.environ.get("PATH", ""))
        proc = procs.spawn(["python", "-c", "print('ok')"], str(self.tmp / "T3"), env=env,
                           stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(out.strip(), b"ok")
        procs.forget(proc.pid)

    def test_final_path_and_safefs_rejects_junction(self):
        import _winapi
        from ticketd import safefs, winapi
        root = self.tmp / "work"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.txt").write_text("in")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("secret")
        fd = safefs.open_read_under(str(root), "sub/a.txt")
        self.assertEqual(os.path.normcase(winapi.final_path(fd)), os.path.normcase(str(root / "sub" / "a.txt")))
        os.close(fd)
        _winapi.CreateJunction(str(outside), str(root / "link"))
        with self.assertRaises(OSError):
            safefs.open_read_under(str(root), "link/a.txt")
        fd = safefs.open_append(root / "log.txt")
        os.write(fd, b"x\n")
        os.close(fd)
        self.assertEqual((root / "log.txt").read_bytes(), b"x\n")  # 二进制写，不转成 \r\n

    def test_safefs_rejects_hard_link(self):
        from ticketd import safefs
        root = self.tmp / "w"
        root.mkdir()
        (self.tmp / "secret.txt").write_text("s")
        os.link(str(self.tmp / "secret.txt"), str(root / "h.txt"))
        with self.assertRaises(OSError):
            safefs.open_read(root / "h.txt")

    def test_file_lock_excludes_second_holder(self):
        from ticketd import host
        p = self.tmp / "l.lock"
        a = open(p, "w")
        b = open(p, "w")
        host.FileLock(a.fileno()).acquire(blocking=False)
        with self.assertRaises(OSError):
            host.FileLock(b.fileno()).acquire(blocking=False)
        host.FileLock(a.fileno()).release()
        host.FileLock(b.fileno()).acquire(blocking=False)
        a.close()
        b.close()

    def test_ingest_spool(self):
        from ticketd import core, db
        con = db.connect()
        tid = core.create_ticket(con, chat_id="oc_1", message_id="om_1", title="t", slug="x",
                                 request_text="r", ack_text="a")
        spool = self.tmp / "s" / ".reports"
        core.write_spool(str(spool), "ready", "好了", [], None)
        (spool / "bad.json").write_text("{")
        self.assertEqual(core.ingest_spool(con, tid, str(spool)), 1)
        self.assertEqual(sorted(p.name for p in spool.iterdir()), ["bad.json.bad"])


if __name__ == "__main__":
    unittest.main()

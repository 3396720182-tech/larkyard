"""procs.py 两个平台的实现，都不依赖真实进程：
Linux 用假的 /proc 目录，macOS 用假的 ps / lsof 输出。真实进程的端到端用例在 test_scheduler.py，
在哪个平台上跑就走哪个平台的实现（CI 在 ubuntu-latest 和 macos-latest 上各跑一遍）。"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase


class FakeProc:
    """在 root 下造一棵 /proc：<pid>/stat、cmdline、environ、cwd（符号链接）。"""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "self").mkdir()  # 非数字条目要跳过
        (root / "uptime").write_text("1 1")

    def add(self, pid, ppid=1, state="S", tty=0, comm="sleep", argv=("sleep", "60"), env=(), cwd=None):
        d = self.root / str(pid)
        d.mkdir()
        (d / "stat").write_bytes(("%d (%s) %s %d %d %d %d -1 4194560 0 0" % (
            pid, comm, state, ppid, pid, pid, tty)).encode("utf-8"))
        (d / "cmdline").write_bytes(b"\0".join(os.fsencode(a) for a in argv) + b"\0")
        (d / "environ").write_bytes(b"".join(os.fsencode(e) + b"\0" for e in env))
        if cwd is not None:
            os.symlink(str(cwd), str(d / "cwd"))
        return pid


class LinuxProcsTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import host, procs
        self.procs = procs
        self.fake = FakeProc(self.tmp / "proc")
        for p in (mock.patch.object(procs, "PROC", str(self.fake.root)),
                  mock.patch.object(host, "system", lambda: "linux"),
                  mock.patch.object(procs.subprocess, "run", side_effect=AssertionError("Linux 不该跑 ps/lsof"))):
            p.start()
            self.addCleanup(p.stop)
        self.fake.add(1, ppid=0, comm="systemd", argv=("/sbin/init",))

    def test_command_line_joins_argv(self):
        self.fake.add(100, argv=("claude", "-p", "--session-id", "abc-123"))
        self.assertEqual(self.procs.command_line(100), "claude -p --session-id abc-123")
        self.assertEqual(self.procs.command_line(4242), "")

    def test_command_line_non_ascii(self):
        self.fake.add(100, argv=("codex", "exec", "-C", "/w/工单 x/T0001-测试"))
        self.assertIn("/w/工单 x/T0001-测试", self.procs.command_line(100))

    def test_is_gone(self):
        self.fake.add(100, state="S")
        self.fake.add(101, state="Z")
        self.fake.add(102, state="R", comm="weird) name (x")  # comm 里有括号和空格
        self.assertFalse(self.procs.is_gone(100))
        self.assertTrue(self.procs.is_gone(101))
        self.assertFalse(self.procs.is_gone(102))
        self.assertTrue(self.procs.is_gone(4242))

    def test_stat_with_parens_in_comm(self):
        self.fake.add(100, ppid=7, tty=34816, comm="a) b (c")
        self.assertEqual(self.procs._proc_stat(100), ("S", 7, 34816, "a) b (c"))

    def test_scan_marks_exact_spool(self):
        spool = "/w/工单 x/T0001-测试/.reports"
        hit = self.fake.add(100, ppid=50, env=("PATH=/bin", "TICKETD_SPOOL=" + spool))
        self.fake.add(101, ppid=50, env=("TICKETD_SPOOL=" + spool + "2",))
        self.fake.add(102, ppid=50, env=("X=TICKETD_SPOOL=" + spool,))
        self.fake.add(50, ppid=1, tty=34816, comm="bash")
        marked, adopted = self.procs.scan(spool)
        self.assertEqual(marked, {hit})
        self.assertEqual(adopted, set())

    def test_scan_adopted_by_init_or_own_ancestor(self):
        # ticketd 作为用户服务：PID 1 → systemd --user（10）→ ticketd（自己）
        self.fake.add(10, ppid=1, comm="systemd", argv=("/lib/systemd/systemd", "--user"))
        self.fake.add(os.getpid(), ppid=10, comm="python3")
        by_init = self.fake.add(100, ppid=1)
        by_user_manager = self.fake.add(101, ppid=10)
        self.fake.add(20, ppid=1, comm="init")
        self.fake.add(102, ppid=20)                    # 父进程名字像 init，但不是 ticketd 的祖先
        self.fake.add(103, ppid=1, tty=34816)          # 有控制终端：用户自己的 shell
        self.fake.add(30, ppid=1, comm="bash")
        self.fake.add(104, ppid=30)                    # 父进程还在
        self.fake.add(105, ppid=0)                     # 父进程在命名空间外
        self.fake.add(106, ppid=1, state="Z")          # 僵尸
        self.fake.add(107, ppid=999)                   # 父进程查不到
        marked, adopted = self.procs.scan("/w/.reports")
        self.assertEqual(marked, set())
        self.assertEqual(adopted, {by_init, by_user_manager, 10, 20, 30})
        self.assertFalse({102, 103, 104, 105, 106, 107} & adopted)

    def test_scan_wsl_reaper_by_ancestry_not_name(self):
        # 没开 systemd 的 WSL：/init（1）→ init(Ubuntu)（5）→ SessionLeader（6）→ ticketd
        self.fake.add(5, ppid=1, comm="init(Ubuntu)")
        self.fake.add(6, ppid=5, comm="SessionLeader")
        self.fake.add(os.getpid(), ppid=6, comm="python3")
        orphan = self.fake.add(100, ppid=6)
        self.assertIn(orphan, self.procs.scan("/w/.reports")[1])

    def test_ancestors_stops_on_cycles(self):
        self.fake.add(40, ppid=41)
        self.fake.add(41, ppid=40)
        self.assertEqual(self.procs._ancestors(40, self.procs._proc_stat), {40, 41})

    def test_scan_skips_self_and_other_users(self):
        spool = "/w/.reports"
        self.fake.add(os.getpid(), env=("TICKETD_SPOOL=" + spool,))
        self.fake.add(100, env=("TICKETD_SPOOL=" + spool,))
        self.assertEqual(self.procs.scan(spool)[0], {100})
        other = os.getuid() + 1
        with mock.patch.object(self.procs.os, "getuid", lambda: other):
            self.assertEqual(self.procs.scan(spool), (set(), set()))

    def test_scan_unreadable_environ(self):
        d = self.fake.root / str(self.fake.add(100))
        (d / "environ").unlink()
        os.mkdir(str(d / "environ"))  # 读的时候报错（真实系统上是 EACCES）
        self.assertEqual(self.procs.scan("/w/.reports"), (set(), {100}))

    def test_cwd_within(self):
        wd = self.tmp / "工单 x" / "T0001-测试"
        (wd / "sub").mkdir(parents=True)
        (self.tmp / "工单 x" / "T0001-测试2").mkdir()
        inside = self.fake.add(100, cwd=wd / "sub")
        top = self.fake.add(101, cwd=wd)
        sibling = self.fake.add(102, cwd=self.tmp / "工单 x" / "T0001-测试2")
        no_cwd = self.fake.add(103)
        self.assertEqual(self.procs.cwd_within([inside, top, sibling, no_cwd, 4242], str(wd)), {inside, top})

    def test_sweep_kills_marked_and_adopted_in_workdir(self):
        from ticketd import scheduler
        wd = self.tmp / "w" / "T0001-x"
        (wd / "sub").mkdir(parents=True)
        spool = str(wd / ".reports")
        marked = self.fake.add(100, ppid=1, env=("TICKETD_SPOOL=" + spool,))
        stray = self.fake.add(101, ppid=1, cwd=wd / "sub")
        self.fake.add(102, ppid=1, cwd=self.tmp)                      # 收养了但不在工单目录
        self.fake.add(50, ppid=1, comm="claude")
        self.fake.add(103, ppid=50, cwd=wd)                           # 在工单目录但父进程还在
        with mock.patch.object(scheduler.os, "kill") as kill:
            self.assertEqual(scheduler._sweep_orphans(spool, 15), 2)
        self.assertEqual({c.args for c in kill.call_args_list}, {(marked, 15), (stray, 15)})


def _ps_result(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class DarwinProcsTest(unittest.TestCase):
    """macOS 实现按 ps / lsof 的输出解析；这里喂假输出，不需要能执行 ps 的环境。"""

    def setUp(self):
        from ticketd import host, procs
        self.procs = procs
        p = mock.patch.object(host, "system", lambda: "darwin")
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, stdout):
        return mock.patch.object(self.procs.subprocess, "run", return_value=_ps_result(stdout))

    def test_scan_parses_ps(self):
        spool = "/w/工单 x/T0001-测试/.reports"
        me = os.getpid()
        out = "\n".join([
            "  100     1 ??       /usr/bin/python3 x TICKETD_SPOOL=%s PATH=/bin" % spool,
            "  101     1 ??       sleep 60 TICKETD_SPOOL=%s2" % spool,
            "  102     1 ??       /bin/sleep 60",
            "  103     1 ttys001  -zsh",
            "  104   555 ??       /bin/sleep 60",
            "  %d     1 ??       python TICKETD_SPOOL=%s" % (me, spool),
            "garbage",
        ])
        with self.run_with(out) as run:
            marked, adopted = self.procs.scan(spool)
        self.assertEqual(marked, {100})
        self.assertEqual(adopted, {101, 102})
        self.assertEqual(run.call_args.args[0][:2], ["ps", "-axE"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "en_US.UTF-8")

    def test_cwd_within_parses_lsof(self):
        out = "p100\nfcwd\nn/Users/x/w/T0001-x/sub\np101\nfcwd\nn/Users/x/w/T0001-x2\np102\nn/Users/x/w/T0001-x\n"
        with mock.patch.object(self.procs.os.path, "realpath", lambda p: p):
            with self.run_with(out) as run:
                found = self.procs.cwd_within([102, 100, 101], "/Users/x/w/T0001-x")
        self.assertEqual(found, {100, 102})
        self.assertEqual(run.call_args.args[0][0], "/usr/sbin/lsof")
        self.assertIn("100,101,102", run.call_args.args[0])

    def test_is_gone_and_command_line(self):
        with self.run_with("Ss\n"):
            self.assertFalse(self.procs.is_gone(100))
        with self.run_with("Z\n"):
            self.assertTrue(self.procs.is_gone(100))
        with self.run_with(""):
            self.assertTrue(self.procs.is_gone(100))
        with self.run_with("claude -p --session-id abc\n"):
            self.assertIn("abc", self.procs.command_line(100))
        with mock.patch.object(self.procs.subprocess, "run", side_effect=OSError("ps blocked")):
            self.assertEqual(self.procs.command_line(100), "")


if __name__ == "__main__":
    unittest.main()

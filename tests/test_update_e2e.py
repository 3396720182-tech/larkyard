"""端到端：临时 HOME、file:// 替身更新源、真实的签名（scripts/release.py）、真实的守护进程进程，
用一个模仿 launchd KeepAlive / systemd Restart=always 的小监督器反复拉起「解释器 + 版本目录外的启动器」。

覆盖 tasks 6.1（正常升级）、6.4（启动就崩的版本、bin/ticketd 有语法错误的版本都自动切回并私信）。
慢（每个用例几十秒），默认跳过；TICKETD_E2E_UPDATE=1 时运行。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FAKES, REPO

E2E = os.environ.get("TICKETD_E2E_UPDATE") == "1"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com"))


@unittest.skipUnless(E2E, "端到端更新测试较慢：TICKETD_E2E_UPDATE=1 时运行")
class SelfUpdateE2E(unittest.TestCase):
    PROBATION = 180  # 配置下限；看门狗在 deadline（60 秒）+ 宽限后动手

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-e2e-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / "home"
        self.th = self.home / ".ticketd"
        self.th.mkdir(parents=True)
        self.src = self.tmp / "source"
        self.rh = self.tmp / "release-home"
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home), "TICKETD_HOME": str(self.th),
            "TICKETD_WORKROOT": str(self.tmp / "work"), "TICKETD_OWNER": "ou_owner",
            "TICKETD_CLAUDE": str(FAKES / "fake_claude.py"), "TICKETD_LARK": str(FAKES / "fake_lark.py"),
            "FAKE_LARK_LOG": str(self.tmp / "lark.log"), "TICKETD_UPDATE_URL": self.src.as_uri(),
            "TICKETD_UPDATE_CHANNEL": "canary", "TICKETD_UPDATE_PROBATION": str(self.PROBATION),
            "TICKETD_PYTHON": sys.executable, "LARK_CHANNEL_HOME": str(self.tmp / "lc"),
            "LARKYARD_RELEASE_HOME": str(self.rh), "TICKETD_DIGEST": "",
        }
        # 发布者的仓库：当前工作树（含未提交的改动）拷成一个干净的 git 仓库
        self.repo = self.tmp / "repo"
        files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard", "-z"],
                               capture_output=True, text=True, check=True).stdout.split("\0")
        for f in filter(None, files):
            s = REPO / f
            if s.is_file():
                d = self.repo / f
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(s), str(d))
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        from ticketd import __version__
        self.base = __version__
        self.keys = self.tmp / "keys"
        self.keys.mkdir()
        for n in ("main", "backup"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", n, "-f", str(self.keys / n)],
                           check=True)
        self.sup = None

    def tearDown(self):
        if self.sup is not None:
            self.sup.terminate()
            try:
                self.sup.wait(60)
            except subprocess.TimeoutExpired:
                self.sup.kill()

    # ---------- 工具 ----------
    def ticket(self, *args, ok=True):
        p = subprocess.run([str(self.th / "app" / "current" / "bin" / "ticket")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=120)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def release_tool(self, *args):
        # 发版工具从本仓库跑（发布者自己的工具），--repo 指向要打包的仓库：被打包的代码坏了也不影响工具
        p = subprocess.run([sys.executable, str(REPO / "scripts" / "release.py")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def tag(self, version, mutate=None):
        init = self.repo / "ticketd" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace('"%s"' % self.base, '"%s"' % version),
                        encoding="utf-8")
        if mutate:
            mutate(self.repo)
        git(self.repo, "commit", "-qam", version)
        git(self.repo, "tag", "v" + version)
        self.release_tool("release", "--tag", "v" + version, "--key", str(self.keys / "main"),
                          "--repo", str(self.repo))
        self.release_tool("publish", str(self.rh / "out" / "signed" / version / "canary.json"), "--to", str(self.src))
        git(self.repo, "checkout", "-q", "HEAD~1", "--", ".")
        git(self.repo, "commit", "-qam", "back to base")

    def adopt(self):
        """不跑真的 install.sh（不碰 launchd / systemd）：直接调 adopt，服务换成下面的监督器。"""
        code = ("import sys; sys.path.insert(0, %r); from ticketd import update; "
                "print('\\n'.join(update.adopt('', repo=__import__('pathlib').Path(%r), run_install=lambda r, a, s=None: None)))"
                % (str(self.repo), str(self.repo)))
        p = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        fp = lambda n: subprocess.run(["ssh-keygen", "-lf", str(self.keys / (n + ".pub"))], capture_output=True,
                                      text=True).stdout.split()[1]
        self.ticket("update", "trust", "--main", str(self.keys / "main.pub"), "--fingerprint", fp("main"),
                    "--backup", str(self.keys / "backup.pub"), "--backup-fingerprint", fp("backup"))

    def supervise(self):
        """KeepAlive / Restart=always：进程退出 2 秒后重新拉起启动器（launchd 的节流是 10 秒，这里缩短省时间）。"""
        log = open(str(self.tmp / "supervisor.log"), "w")
        self.addCleanup(log.close)
        script = ("import subprocess, sys, time, signal\n"
                  "stop = []\n"
                  "signal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n"
                  "while not stop:\n"
                  "    p = subprocess.Popen([sys.executable, %r])\n"
                  "    while p.poll() is None:\n"
                  "        if stop:\n"
                  "            p.terminate(); p.wait(); break\n"
                  "        time.sleep(0.2)\n"
                  "    print('exit', p.returncode, flush=True)\n"
                  "    time.sleep(2)\n" % str(self.th / "app" / "launcher.py"))
        self.sup = subprocess.Popen([sys.executable, "-c", script], env=self.env, stdout=log, stderr=log,
                                    cwd=str(self.th))

    def state(self):
        try:
            return json.loads((self.th / "app" / "update-state.json").read_text())
        except (OSError, ValueError):
            return {}

    def current(self):
        return os.path.basename(os.readlink(str(self.th / "app" / "current")))

    def wait_for(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.5)
        self.fail("%s 秒内没有%s；状态 %s；监督器日志：\n%s\n守护进程日志：\n%s" % (
            timeout, what, self.state(), (self.tmp / "supervisor.log").read_text()[-3000:],
            self.ticket("update", "status", ok=False).stdout))

    def dms(self):
        import sqlite3
        con = sqlite3.connect(str(self.th / "db" / "ticketd.db"))
        # 收件人占位 @owner 在发送时会被改写成 TICKETD_OWNER，按系统工单号查
        rows = [r[0] for r in con.execute("SELECT text FROM outbox WHERE ticket_id='_ticketd'")]
        con.close()
        return rows

    def force_check(self):
        """不等 60 秒的首次检查：用命令行查一次（会下载并校验），守护进程主循环看到待安装就装。"""
        out = self.ticket("update", "check").stdout
        self.assertIn("已下载", out)

    # ---------- 用例 ----------
    def test_upgrade_then_confirm(self):
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        self.tag("9.0.1")
        self.force_check()
        self.wait_for(lambda: self.current() == "9.0.1", 60, "切换到 9.0.1")
        self.wait_for(lambda: self.state().get("status") == "confirmed", 120, "确认 9.0.1")
        out = self.ticket("update", "status").stdout
        self.assertIn("当前版本：9.0.1", out)
        self.assertIn("已确认", out)

    def test_crash_on_start_rolls_back_and_dms(self):
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")

        def crash(repo):
            d = repo / "ticketd" / "daemon.py"
            d.write_text(d.read_text(encoding="utf-8").replace(
                "def main(argv=None) -> int:\n", "def main(argv=None) -> int:\n    raise SystemExit(3)\n"),
                encoding="utf-8")
        self.tag("9.0.1", crash)
        self.force_check()
        self.wait_for(lambda: self.state().get("status") == "rolled_back", 60, "切回")
        self.assertEqual(self.current(), self.base)
        self.wait_for(lambda: any("更新到 9.0.1 失败" in t for t in self.dms()), 30, "私信")
        out = self.ticket("update", "status").stdout
        self.assertIn("不再自动安装：9.0.1", out)

    def test_syntax_error_entry_rolls_back(self):
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")

        def broken(repo):
            (repo / "bin" / "ticketd").write_text("#!/bin/sh\nif then ((\n")
            (repo / "ticketd" / "__init__.py").write_text(
                (repo / "ticketd" / "__init__.py").read_text(encoding="utf-8") + "def (:\n", encoding="utf-8")
        # __init__.py 坏了的话发版工具读不出版本号：版本号写在第一行，语法错误加在后面，正则照样读得到
        self.tag("9.0.2", broken)
        self.force_check()
        self.wait_for(lambda: self.state().get("status") == "rolled_back", 60, "切回")
        self.assertEqual(self.current(), self.base)
        self.wait_for(lambda: any("更新到 9.0.2 失败" in t for t in self.dms()), 30, "私信")

    def test_hung_new_version_rolled_back_by_watchdog(self):
        """新版本活着但从不确认（主循环一直报错）：看门狗在试用期内结束它，启动器按超时切回。"""
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")

        def hang(repo):
            d = repo / "ticketd" / "update.py"
            d.write_text(d.read_text(encoding="utf-8").replace(
                "    def tick(self, sched=None, media=None, healthy: bool = True) -> bool:\n",
                "    def tick(self, sched=None, media=None, healthy: bool = True) -> bool:\n        return False\n"),
                encoding="utf-8")
        self.tag("9.0.3", hang)
        self.force_check()
        self.wait_for(lambda: self.current() == "9.0.3", 60, "切换到 9.0.3")
        self.wait_for(lambda: self.state().get("status") == "rolled_back", self.PROBATION, "按超时切回")
        self.assertEqual(self.state().get("reason"), "timeout")
        self.assertEqual(self.current(), self.base)

    def test_waits_for_running_ticket_then_installs(self):
        """有工单在跑时不装；跑完后装上，工单不丢（tasks 6.3）。"""
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        self.ticket("new", "--chat", "oc_1", "--message", "om_1", "--title", "慢单", "--slug", "slow",
                    "--request", "睡一会 [[fake:sleep]]", "--ack", "开工")
        import sqlite3

        def state():
            con = sqlite3.connect(str(self.th / "db" / "ticketd.db"))
            r = con.execute("SELECT state FROM tickets WHERE id='T0001'").fetchone()
            con.close()
            return r[0] if r else None
        self.wait_for(lambda: state() == "running", 30, "工单开始运行")
        self.tag("9.0.4")
        self.force_check()
        time.sleep(8)
        self.assertEqual(self.current(), self.base)  # 有工单在跑：没装
        self.assertEqual(state(), "running")
        self.wait_for(lambda: self.current() == "9.0.4", 120, "工单结束后装上")
        self.wait_for(lambda: self.state().get("status") == "confirmed", 120, "确认 9.0.4")
        con = sqlite3.connect(str(self.th / "db" / "ticketd.db"))
        reports = [r[0] for r in con.execute("SELECT status FROM reports WHERE ticket_id='T0001'")]
        con.close()
        self.assertIn("ready", reports)


if __name__ == "__main__":
    unittest.main()

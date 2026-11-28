"""端到端：临时 HOME、file:// 替身更新源、真实的签名（scripts/release.py）、真实的守护进程进程，
用一个模仿 launchd KeepAlive / systemd Restart=always 的小监督器反复拉起「解释器 + 版本目录外的启动器」。

覆盖 tasks 6.1（正常升级）、6.3（等工单跑完再装）、6.4（启动就崩、bin/ticketd 有语法错误、活着但从不确认的版本
都自动切回并私信）、切换后写事件失败仍重启、切换后没重启成的旧进程补发重启、验签失败 / 降级 / 重放旧清单被拒，以及 adopt（改写工作区 CLAUDE.md）→ 自动更新 → adopt --undo。
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
            if f.startswith(("bridge/", "vendor/bridge.lock.json")):
                continue  # 打包 bridge 要先构建（build_bridge.py），这里只测 Larkyard 自己的更新
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

    def run_py(self, root, body):
        """在 root 这份代码里跑一段 update 模块的调用；install.sh 换成只记录参数的替身（不碰 launchd / systemd）。"""
        code = ("import sys, json; sys.path.insert(0, %r); from pathlib import Path; from ticketd import update\n"
                "calls = []\n"
                "def fake(r, a, s=None):\n"
                "    calls.append([str(r), str(a) if a else None, s])\n"
                "%s\n"
                "print(json.dumps({'out': out, 'calls': calls}))\n" % (str(root), body))
        p = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def adopt(self, workspace=""):
        """不跑真的 install.sh：直接调 adopt，服务换成下面的监督器。临时 HOME 里放一份替身服务文件（像装过服务一样）。"""
        import plistlib
        env = {"TICKETD_HOME": str(self.th), "LARK_CHANNEL_HOME": str(self.tmp / "lc"), "LARK_CHANNEL_PROFILE": "claude"}
        sf = self.home / "Library" / "LaunchAgents" / "dev.larkyard.ticketd.plist"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_bytes(plistlib.dumps({"Label": "dev.larkyard.ticketd", "EnvironmentVariables": env}))
        unit = self.home / ".config" / "systemd" / "user" / "dev.larkyard.ticketd.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("".join('Environment="%s=%s"\n' % kv for kv in env.items()))
        r = self.run_py(self.repo, "out = update.adopt(%r, repo=Path(%r), run_install=fake)" % (workspace, str(self.repo)))
        self.assertEqual(r["calls"][0][:2], [str(self.th / "app" / "versions" / self.base), str(self.th / "app")])
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

    def restarts(self):
        return (self.tmp / "supervisor.log").read_text().count("exit 75")

    def codes(self):
        import sqlite3
        con = sqlite3.connect(str(self.th / "db" / "ticketd.db"))
        rows = [r[0] for r in con.execute("SELECT code FROM events WHERE kind='update'")]
        con.close()
        return rows

    # 故障注入：经 PYTHONPATH 上的 sitecustomize，在真实守护进程导入 ticketd.update 时把 event 换成记 installed 就抛错
    FAULT = '''import importlib.util, sys
class _Hook:
    def find_spec(self, name, path, target=None):
        if name != "ticketd.update":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        orig = spec.loader.exec_module
        def exec_module(module):
            orig(module)
            real = module.event
            def event(con, code, *a, **k):
                if code == "installed":
                    raise RuntimeError("injected: installed event")
                return real(con, code, *a, **k)
            module.event = event
        spec.loader.exec_module = exec_module
        return spec
sys.meta_path.insert(0, _Hook())
'''

    def test_installed_event_failure_still_restarts(self):
        """切换后写 installed 事件失败（真实守护进程里注入）：照样按重启码退出、新版本确认（P1）。"""
        fault = self.tmp / "fault"
        fault.mkdir()
        (fault / "sitecustomize.py").write_text(self.FAULT)
        self.env["PYTHONPATH"] = str(fault)
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        self.tag("9.0.1")
        self.force_check()
        self.wait_for(lambda: self.current() == "9.0.1", 60, "切换到 9.0.1")
        self.wait_for(lambda: self.state().get("status") == "confirmed", 120, "确认 9.0.1")
        self.assertEqual(self.restarts(), 1)
        self.assertNotIn("installed", self.codes())  # 注入确实生效了
        self.assertIn("当前版本：9.0.1", self.ticket("update", "status").stdout)

    def test_switched_behind_running_daemon_heals(self):
        """另一个进程完成了切换、在跑的守护进程没退出（旧代码没请求重启的情形）：下一拍补发重启，新版本确认。"""
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        self.ticket("update", "hold")  # 守护进程自己不装，由下面的进程装
        self.tag("9.0.1")
        self.force_check()
        self.run_py(self.repo, "from ticketd import db\ncon = db.connect()\n"
                               "out = update.install(con, update.kv_json(con, 'update.pending'))")
        self.assertEqual(self.current(), "9.0.1")
        self.wait_for(lambda: self.state().get("status") == "confirmed", 120, "确认 9.0.1")
        self.assertEqual(self.restarts(), 1)
        self.assertIn("stale_code", self.codes())

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

    def test_bad_signature_downgrade_and_replay_rejected(self):
        """验签失败、降级、重放旧清单：命令行检查报拒绝，守护进程不切换，信任和序号不受影响。"""
        self.adopt()
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        chan = self.src / "channels" / "canary"
        # 1. 正常发 9.0.1 并装上、确认
        self.tag("9.0.1")
        self.force_check()
        self.wait_for(lambda: self.state().get("status") == "confirmed" and self.current() == "9.0.1", 150, "装上 9.0.1")
        good = (chan / "manifest.json").read_bytes(), (chan / "manifest.json.sig").read_bytes()
        # 2. 验签失败：用不受信任的私钥签一份更高的版本
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "evil", "-f", str(self.keys / "evil")],
                       check=True)
        self.tag("9.0.9")
        bad = json.loads((chan / "manifest.json").read_text())
        (chan / "manifest.json.sig").unlink()
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.keys / "evil"), "-n", "larkyard-release",
                        str(chan / "manifest.json")], check=True, capture_output=True)
        self.assertIn("验签失败", self.ticket("update", "check").stdout)
        # 3. 降级：受信任的私钥签的普通清单，指向比当前低的版本
        self.tag("8.0.0")
        self.assertIn("拒绝降级", self.ticket("update", "check").stdout)
        # 4. 重放：把早先 9.0.1 那份（序号更小、签名有效）放回去
        self.tag("9.0.5")
        self.assertIn("已下载", self.ticket("update", "check").stdout)  # 序号推到 9.0.5 这份
        self.wait_for(lambda: self.state().get("status") == "confirmed" and self.current() == "9.0.5", 150,
                      "装上 9.0.5")
        (chan / "manifest.json").write_bytes(good[0])
        (chan / "manifest.json.sig").write_bytes(good[1])
        self.assertIn("旧清单", self.ticket("update", "check").stdout)
        time.sleep(5)
        self.assertEqual(self.current(), "9.0.5")
        self.assertNotEqual(bad["version"], self.current())
        self.assertNotIn("9.0.9", os.listdir(str(self.th / "app" / "versions")))

    def test_adopt_rewrites_workspace_survives_update_and_undo_restores(self):
        """adopt 改写工作区 CLAUDE.md 的 ticket 路径；自动更新后主会话的 ticket 是新版本；--undo 改回原文、按原服务重装。"""
        ws = self.tmp / "ws"
        ws.mkdir()
        tmpl = (self.repo / "workspace" / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        original = tmpl.replace("@LARKYARD@", str(self.repo)) + "\n手写的一段\n"
        (ws / "CLAUDE.md").write_text(original, encoding="utf-8")
        self.adopt(str(ws))
        md = (ws / "CLAUDE.md").read_text(encoding="utf-8")
        cur_ticket = str(self.th / "app" / "current" / "bin" / "ticket")
        self.assertIn(cur_ticket, md)
        self.assertNotIn("%s/bin/ticket" % self.repo, md)
        self.supervise()
        self.wait_for(lambda: (self.th / "heartbeat").exists(), 30, "启动")
        self.tag("9.0.1")
        self.force_check()
        self.wait_for(lambda: self.state().get("status") == "confirmed" and self.current() == "9.0.1", 150, "装上 9.0.1")
        p = subprocess.run([cur_ticket, "update", "status"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn("当前版本：9.0.1", p.stdout)  # 主会话按 CLAUDE.md 里的路径调用的就是新版本
        self.assertEqual(self.state().get("workspace_md"), str(ws / "CLAUDE.md"))  # 自动更新没丢掉 adopt 的来历
        self.sup.terminate()
        self.sup.wait(60)
        self.sup = None
        r = self.run_py(self.th / "app" / "current", "out = update.undo_adopt(run_install=fake)")
        self.assertEqual((ws / "CLAUDE.md").read_text(encoding="utf-8"), original)
        self.assertEqual(r["calls"][-1][:2], [str(self.repo), None])  # 服务按原克隆重装
        self.assertTrue(any("比正在运行的" in l for l in r["out"]))  # 克隆是旧版本：提示
        self.assertFalse((self.th / "app" / "current").exists())
        self.assertEqual(self.state().get("status"), "unadopted")


if __name__ == "__main__":
    unittest.main()

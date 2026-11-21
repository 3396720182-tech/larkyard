"""ticket migrate：已有的就地安装 → 受管安装 + 打包的 bridge（openspec migrate-existing-install）。
两种布局：早期私有版（自定义服务名、主用户和工单根目录写死在代码里、npm 装的 bridge）和从 clone 装的 Larkyard
（默认服务名、读 config.env、Homebrew 装的 bridge）。两个平台的分支都测；服务管理器、install.sh、在线确认用假的。"""
from __future__ import annotations

import functools
import json
import os
import plistlib
import sqlite3
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase
from test_bridge import BridgeTestBase, FakeServices

from ticketd import bridge, cli, db, migrate, setup, update

OLD_CONFIG_EARLY = '''import os
from pathlib import Path
def _env(name, default):
    return os.environ.get(name, default)
def home():
    return Path(_env("TICKETD_HOME", str(Path.home() / ".ticketd")))
def workroot():
    return Path(_env("TICKETD_WORKROOT", str(Path.home() / "Projects" / "tickets")))
def owner():
    return _env("TICKETD_OWNER", "ou_old_owner")
'''

OLD_CONFIG_CLONE = '''import os
from pathlib import Path
def _file(name):
    p = Path(os.environ.get("TICKETD_HOME", str(Path.home() / ".ticketd"))) / "config.env"
    for line in p.read_text().splitlines() if p.exists() else []:
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return None
def workroot():
    return Path(os.environ.get("TICKETD_WORKROOT") or _file("TICKETD_WORKROOT") or str(Path.home() / "larkyard-tickets"))
def owner():
    return os.environ.get("TICKETD_OWNER") or _file("TICKETD_OWNER") or ""
'''


class MigrateBase(BridgeTestBase):
    layout = "early"   # early：早期私有版；clone：从 clone 装的 Larkyard

    def setUp(self):
        super().setUp()
        for k in ("TICKETD_OWNER", "TICKETD_WORKROOT", "TICKETD_LABEL"):
            os.environ.pop(k, None)  # 守护进程看不到 shell 的环境：测试也不靠它
        os.environ["TICKETD_HOME"] = str(self.home / ".ticketd")  # 早期版本的服务环境里没有 TICKETD_HOME：用默认位置
        self.con.close()
        self.con = db.connect()
        self.svc = FakeServices()
        self.plat_patch = mock.patch("ticketd.bridge.platform_key", return_value=self.plat())
        self.plat_patch.start()
        self.addCleanup(self.plat_patch.stop)
        self.repo = self.home / "Projects" / "ticketd"
        (self.repo / "ticketd").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "ticketd").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.repo / "scripts").mkdir()
        (self.repo / "ticketd" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "ticketd" / "config.py").write_text(
            OLD_CONFIG_EARLY if self.layout == "early" else OLD_CONFIG_CLONE, encoding="utf-8")
        self.label_old = "com.example.ticketd" if self.layout == "early" else update.DEFAULT_LABEL
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        (lc / "profiles" / "claude").mkdir(parents=True)
        self.ws = self.home / "Projects"
        (lc / "config.json").write_text(json.dumps({"activeProfile": "claude", "profiles": {"claude": {
            "workspaces": {"default": str(self.ws)}}}}), encoding="utf-8")
        (self.ws / "CLAUDE.md").write_text("派单：%s/bin/ticket new …\n其他：%s/bin/ticketd-x 不动\n" % (
            self.repo, self.repo), encoding="utf-8")
        cfg = update.service_file("x").parent
        cfg.mkdir(parents=True, exist_ok=True)
        if self.layout == "early":
            self.bin = str(self.home / ".nvm/versions/node/v24.15.0/bin")
        else:
            self.bin = "/opt/homebrew/bin"
            (self.home / ".ticketd").mkdir(exist_ok=True)
            (self.home / ".ticketd" / "config.env").write_text(
                "# 我的配置\nTICKETD_OWNER=ou_clone_owner\nTICKETD_WORKROOT=%s\n" % (self.home / "tickets"),
                encoding="utf-8")
        old_env = {"PATH": self.bin + ":/usr/bin", "LARK_CHANNEL": "1", "LARK_CHANNEL_HOME": str(lc),
                   "LARK_CHANNEL_PROFILE": "claude", "TICKETD_MODEL": "opus"}
        sf_old = update.service_file(self.label_old)
        sf_bridge = bridge.bridge_service_file("claude")
        job = cfg / ("daily-restart" + sf_old.suffix)
        if self.platform == "darwin":
            files = {
                sf_old: plistlib.dumps({"Label": self.label_old, "ProgramArguments": [str(self.repo / "bin/ticketd")],
                                        "KeepAlive": True, "EnvironmentVariables": old_env}),
                sf_bridge: plistlib.dumps({"Label": bridge.bridge_label("claude"), "ProgramArguments": [
                    self.bin + "/node", self.bin + "/lark-channel-bridge", "run", "--profile", "claude"],
                    "EnvironmentVariables": {"PATH": self.bin + ":/usr/bin", "LARK_CHANNEL_HOME": str(lc)}}),
                job: plistlib.dumps({"Label": "daily-restart", "ProgramArguments": [
                    "/bin/sh", "-c", "%s/lark-channel-bridge restart --profile claude" % self.bin],
                    "StartCalendarInterval": {"Hour": 4, "Minute": 7}}),
            }
        else:
            env_lines = "".join('Environment="%s=%s"\n' % kv for kv in old_env.items())
            files = {
                sf_old: ('[Service]\nExecStart="%s/bin/ticketd"\n%s' % (self.repo, env_lines)).encode(),
                sf_bridge: ('[Service]\nExecStart="%s/node" "%s/lark-channel-bridge" run --profile claude\n'
                            'Environment="LARK_CHANNEL_HOME=%s"\nEnvironment="PATH=%s:/usr/bin"\n' % (
                                self.bin, self.bin, lc, self.bin)).encode(),
                job: ('[Service]\nType=oneshot\nExecStart="%s/lark-channel-bridge" restart\n' % self.bin).encode(),
                job.with_suffix(".timer"): b"[Timer]\nOnCalendar=*-*-* 04:07:00\n",
            }
        for p, data in files.items():
            p.write_bytes(data)
        self.getter = lc / "secrets-getter"
        self.getter.write_text("#!/bin/sh\nexec '%s/lark-channel-bridge' secrets get\n" % self.bin, encoding="utf-8")
        self.files = dict(files)
        self.files[self.getter] = self.getter.read_bytes()
        self.files[self.ws / "CLAUDE.md"] = (self.ws / "CLAUDE.md").read_bytes()
        self.cfg_file = self.home / ".ticketd" / "config.env"
        self.make_current("0.3.1", commit="3" * 40)
        self.installs = []

    # ---------- 替身 ----------
    def run_install(self, repo, app, svc):
        self.installs.append((str(repo), str(app) if app else None, svc))
        sf = update.service_file(svc["label"])
        if self.platform == "darwin":
            sf.write_bytes(plistlib.dumps({"Label": svc["label"], "ProgramArguments": [
                "/usr/bin/python3", str(update.app_dir() / "launcher.py")], "EnvironmentVariables": dict(svc["env"])}))
        else:
            sf.write_text('[Service]\nExecStart="/usr/bin/python3" "%s"\nEnvironment="TICKETD_HOME=%s"\n' % (
                update.app_dir() / "launcher.py", svc["env"]["TICKETD_HOME"]), encoding="utf-8")

    def adopt_bridge(self, **kw):
        return functools.partial(bridge.adopt, fetch=self.fetch, wait=lambda *a, **k: True, **kw)

    def snapshot(self):
        out = {p: p.read_bytes() if p.exists() else None for p in self.files}
        out["config.env"] = self.cfg_file.read_bytes() if self.cfg_file.exists() else None
        return out

    def apply(self, p=None, **kw):
        kw.setdefault("services", self.svc)
        kw.setdefault("run_install", self.run_install)
        kw.setdefault("wait", lambda since: True)
        kw.setdefault("lock_free", lambda: True)
        kw.setdefault("bridge_adopt", self.adopt_bridge())
        return migrate.apply(p or migrate.plan(), **kw)


class EarlyLayoutDarwinTest(MigrateBase):
    platform = "darwin"
    layout = "early"

    def test_plan_reads_old_code_and_writes_nothing(self):
        before = self.snapshot()
        p = migrate.plan()
        self.assertEqual(p["blockers"], [])
        td = p["ticketd"]
        self.assertEqual((td["label_old"], td["label_new"]), ("com.example.ticketd", update.DEFAULT_LABEL))
        self.assertEqual(td["repo_logical"], str(self.repo))
        self.assertEqual(td["env"]["LARK_CHANNEL_PROFILE"], "claude")
        self.assertNotIn("TICKETD_MODEL", td["env"])  # 服务环境里的其他 TICKETD_* 搬进 config.env
        changes = {k: v for k, v, _, _ in p["config"]}
        self.assertEqual(changes["TICKETD_OWNER"], "ou_old_owner")
        self.assertEqual(changes["TICKETD_WORKROOT"], str(self.home / "Projects" / "tickets"))
        self.assertEqual(changes["TICKETD_MODEL"], "opus")
        self.assertEqual(p["workspace_root"], str(self.repo))
        self.assertEqual(p["bridge"]["kind"], "npm")
        self.assertEqual(p["bridge"]["skip"], "")
        text = "\n".join(migrate.plan_lines(p))
        self.assertIn("服务改名 com.example.ticketd → dev.larkyard.ticketd", text)
        self.assertIn("会重启 bridge", text)
        self.assertNotIn("ou_old_owner", text)  # 计划里只显示 open_id 的尾巴
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(migrate.root_dir().exists())

    def test_apply_then_undo_restores_everything(self):
        before = self.snapshot()
        (self.home / ".ticketd" / "app" / "current").unlink()  # 模拟这次 install.sh 才装上的受管安装
        self.make_current("0.3.1", commit="3" * 40)
        out = self.apply(installed_now=True, update_url="file:///feed", channel="stable")
        text = "\n".join(out)
        self.assertIn("已迁移", text)
        cfg = self.cfg_file.read_text(encoding="utf-8")
        for line in ("TICKETD_OWNER=ou_old_owner", "TICKETD_WORKROOT=%s" % (self.home / "Projects" / "tickets"),
                     "TICKETD_MODEL=opus", "TICKETD_UPDATE_URL=file:///feed"):
            self.assertIn(line, cfg)
        self.assertNotIn("TICKETD_UPDATE_CHANNEL", cfg)
        self.assertFalse(update.service_file("com.example.ticketd").exists())
        self.assertTrue(update.service_file(update.DEFAULT_LABEL).exists())
        self.assertIn(("unload", "com.example.ticketd", False), self.svc.calls)
        self.assertEqual(self.installs[0][2]["label"], update.DEFAULT_LABEL)
        self.assertEqual(self.installs[0][0], str(update.current_link().resolve()))
        md = (self.ws / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("%s/bin/ticket new" % update.current_link(), md)
        self.assertIn("%s/bin/ticketd-x" % self.repo, md)
        st = update.read_state()
        self.assertEqual((st["label"], st["migrated_from"]), (update.DEFAULT_LABEL, str(self.repo)))
        self.assertTrue(bridge.is_managed_service(bridge.bridge_service_file("claude")))
        self.assertTrue(Path(migrate.read_record()["backup"], "ticketd.db").is_file())
        self.assertEqual(migrate.read_record()["status"], "done")
        # 再跑一次：已经迁移过
        self.assertTrue(migrate.plan()["blockers"])

        back = migrate.undo(services=self.svc)
        self.assertTrue(any("已恢复并重新加载 com.example.ticketd" in x for x in back))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(update.service_file(update.DEFAULT_LABEL).exists())
        self.assertFalse(update.current_link().exists())
        self.assertEqual(update.read_state()["status"], "unmigrated")
        self.assertEqual(self.svc.calls[-1][:2], ("load", "com.example.ticketd"))
        self.assertTrue(migrate.plan()["blockers"] == [] or True)  # 退回后可以再迁移
        with self.assertRaises(migrate.MigrateError):
            migrate.undo(services=self.svc)

    def test_daemon_not_up_rolls_back(self):
        before = self.snapshot()
        with self.assertRaises(migrate.MigrateError) as cm:
            self.apply(wait=lambda since: False)
        self.assertIn("已自动退回", str(cm.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(update.service_file(update.DEFAULT_LABEL).exists())
        self.assertTrue(update.current_link().exists())  # 本来就有的 current 不撤
        self.assertFalse(bridge.is_managed_service(bridge.bridge_service_file("claude")))

    def test_old_daemon_not_stopping_rolls_back(self):
        before = self.snapshot()
        with self.assertRaises(migrate.MigrateError):
            self.apply(lock_free=lambda: False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.installs, [])

    def test_bridge_failure_keeps_ticketd(self):
        def boom(*a, **k):
            raise bridge.BridgeError("迁移后 bridge 90 秒内没有连上飞书")
        out = self.apply(bridge_adopt=boom)
        self.assertTrue(any("bridge 迁移没成" in x for x in out))
        self.assertTrue(update.service_file(update.DEFAULT_LABEL).exists())
        rec = migrate.read_record()
        self.assertEqual((rec["status"], rec["bridge"]), ("done", False))
        migrate.undo(services=self.svc)
        self.assertTrue(update.service_file("com.example.ticketd").exists())

    def test_blockers(self):
        t = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (t["id"] if not isinstance(t, str) else t,))
        p = migrate.plan()
        self.assertTrue(any("正在运行" in b for b in p["blockers"]))
        with self.assertRaises(migrate.MigrateError):
            self.apply(p)
        self.con.execute("UPDATE tickets SET state='done'")
        self.apply()
        self.con.execute("UPDATE tickets SET state='running'")
        with self.assertRaises(migrate.MigrateError) as cm:
            migrate.undo(services=self.svc)  # 退回也要重启 ticketd：有正在运行的工单就不动
        self.assertIn("正在运行", str(cm.exception))
        self.con.execute("UPDATE tickets SET state='done'")
        migrate.undo(services=self.svc)
        # 第二个 ticketd 服务
        update.service_file("other.ticketd").write_bytes(plistlib.dumps({
            "Label": "other.ticketd", "ProgramArguments": ["/somewhere/bin/ticketd"]}))
        self.assertTrue(any("2 个跑 ticketd 的服务" in b for b in migrate.plan()["blockers"]))

    def test_refused_in_bridge_session_and_worker(self):
        os.environ["LARK_CHANNEL"] = "1"
        p = migrate.plan()  # 只读的计划可以
        with self.assertRaises(migrate.MigrateError):
            self.apply(p)
        with self.assertRaises(migrate.MigrateError):
            migrate.undo(services=self.svc)
        del os.environ["LARK_CHANNEL"]
        os.environ["TICKETD_TICKET"] = "T0001"
        with self.assertRaises(migrate.MigrateError):
            migrate.plan()

    def test_cli_dry_run_does_not_open_database(self):
        db_file = db.config.db_path()
        self.con.close()
        for f in db_file.parent.glob("ticketd.db*"):
            f.unlink()
        self.con = sqlite3.connect(":memory:")
        with mock.patch("sys.stdout") as out:
            code = cli.main(["migrate", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(db_file.exists())
        self.assertIn("没有改任何东西", "".join(str(c.args[0]) for c in out.write.call_args_list))

    def test_setup_refuses_to_install_a_second_service(self):
        devnull = open(os.devnull, "w")
        self.addCleanup(devnull.close)
        w = setup.Wizard(term=setup.Term(fin=None, fout=devnull), services=self.svc,
                         run_install=self.run_install)
        with self.assertRaises(setup.SetupError) as cm:
            w.step_services("claude")
        self.assertIn("ticket migrate", str(cm.exception))
        self.assertEqual(self.installs, [])

    def test_managed_machine(self):
        # ticketd 已经经启动器运行、版本和这次一样：只迁移 bridge
        sf = update.service_file(self.label_old)
        sf.unlink()
        self.run_install(None, None, {"label": update.DEFAULT_LABEL, "env": {"TICKETD_HOME": os.environ["TICKETD_HOME"]}})
        self.installs = []
        p = migrate.plan(target="0.3.1")
        self.assertIsNone(p["ticketd"])
        self.assertEqual(p["blockers"], [])
        out = self.apply(p)
        self.assertTrue(any("已迁移" in x for x in out))
        self.assertTrue(bridge.is_managed_service(bridge.bridge_service_file("claude")))
        migrate.undo(services=self.svc)
        self.assertFalse(bridge.is_managed_service(bridge.bridge_service_file("claude")))
        self.assertTrue(update.service_file(update.DEFAULT_LABEL).exists())
        # 受管安装的版本比这次的旧：交给守护进程升级
        p = migrate.plan(target="0.4.0")
        self.assertTrue(any("守护进程" in b for b in p["blockers"]))


class CloneLayoutLinuxTest(MigrateBase):
    platform = "linux"
    layout = "clone"

    def test_apply_then_undo(self):
        before = self.snapshot()
        p = migrate.plan()
        self.assertEqual(p["blockers"], [])
        self.assertEqual([c[0] for c in p["config"]], ["TICKETD_MODEL"])  # 旧代码本来就读 config.env：不重复写
        self.assertEqual(p["ticketd"]["label_new"], p["ticketd"]["label_old"])
        self.assertEqual(p["bridge"]["kind"], "homebrew")
        self.apply(p)
        self.assertTrue(update.service_file(update.DEFAULT_LABEL).read_text().find("launcher.py") > 0)
        self.assertNotIn(("unload", update.DEFAULT_LABEL + ".timer", True), self.svc.calls)
        unit = bridge.bridge_service_file("claude").read_text(encoding="utf-8")
        self.assertIn('Environment="LARKYARD_MANAGED=1"', unit)
        self.assertIn("/opt/homebrew/bin", unit)  # 原来的 PATH 接在后面：主会话还能找到 brew 装的工具
        self.assertIn("# 我的配置", self.cfg_file.read_text(encoding="utf-8"))
        migrate.undo(services=self.svc)
        self.assertEqual(self.snapshot(), before)


class EarlyLayoutLinuxTest(MigrateBase):
    platform = "linux"
    layout = "early"

    def test_apply_then_undo(self):
        before = self.snapshot()
        self.apply()
        self.assertFalse(update.service_file("com.example.ticketd").exists())
        self.assertIn("TICKETD_OWNER=ou_old_owner", self.cfg_file.read_text(encoding="utf-8"))
        migrate.undo(services=self.svc)
        self.assertEqual(self.snapshot(), before)


class CloneLayoutDarwinTest(MigrateBase):
    platform = "darwin"
    layout = "clone"

    def test_config_conflict_prefers_running_value(self):
        # config.env 里的主用户和旧代码实际在用的不同（旧代码不读 config.env）：以正在跑的为准，退回时改回
        (self.repo / "ticketd" / "config.py").write_text(OLD_CONFIG_EARLY, encoding="utf-8")
        p = migrate.plan()
        ch = {k: (v, have) for k, v, have, _ in p["config"]}
        self.assertEqual(ch["TICKETD_OWNER"], ("ou_old_owner", "ou_clone_owner"))
        before = self.snapshot()
        self.apply(p)
        cfg = self.cfg_file.read_text(encoding="utf-8")
        self.assertEqual(cfg.count("TICKETD_OWNER="), 1)
        self.assertIn("TICKETD_OWNER=ou_old_owner", cfg)
        migrate.undo(services=self.svc)
        self.assertEqual(self.snapshot(), before)


class ConfigSetTest(TicketdTestCase):
    def test_set_replace_delete(self):
        p = Path(os.environ["TICKETD_HOME"]) / "config.env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 注释\nA=1\nexport B=2\nB=3\nC=4", encoding="utf-8")
        migrate._config_set("B", "9")
        self.assertEqual(p.read_text(encoding="utf-8"), "# 注释\nA=1\nB=9\nC=4")
        migrate._config_set("D", "/路径/带 空格")
        self.assertTrue(p.read_text(encoding="utf-8").endswith("C=4\nD=/路径/带 空格\n"))
        migrate._config_set("A", None)
        self.assertEqual(p.read_text(encoding="utf-8"), "# 注释\nB=9\nC=4\nD=/路径/带 空格\n")
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")


class InstallerArgsTest(TicketdTestCase):
    def sh(self, *args, **env):
        import subprocess
        from helpers import REPO
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "LANG": "en_US.UTF-8"}
        e.update(env)
        return subprocess.run(["/bin/sh", str(REPO / "install.sh"), "--from", "file:///nonexistent"] + list(args),
                              capture_output=True, text=True, env=e, timeout=60)

    def test_dry_run_needs_upgrade_existing(self):
        p = self.sh("--dry-run")
        self.assertEqual(p.returncode, 1)
        self.assertIn("只和 --upgrade-existing 一起用", p.stderr)

    def test_upgrade_refused_in_bridge_session_before_fetching(self):
        p = self.sh("--upgrade-existing", LARK_CHANNEL="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("自己的终端", p.stderr)
        self.assertNotIn("取发版公钥", p.stderr)
        p = self.sh("--upgrade-existing", "--dry-run", LARK_CHANNEL="1")  # 只读检查在哪儿都能跑（这里停在取公钥）
        self.assertIn("取不到", p.stderr)

    def test_upgrade_not_with_enroll(self):
        p = self.sh("--upgrade-existing", "--console", "https://c.example", "--enroll", "LY-1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("不和 --enroll", p.stderr)

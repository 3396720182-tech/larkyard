"""打包的 bridge：运行时、链接、服务定义、按需重启、迁移和退回（openspec vendor-bridge-one-line-install）。
两个平台的分支都测：mock host.system()；服务管理器和在线确认用假的，不碰真实 launchctl / systemctl。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import tarfile
import time
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase

from ticketd import bridge, launcher, update


def _tgz(files: dict, links: dict = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, (data, mode) in sorted(files.items()):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            tf.addfile(ti, io.BytesIO(data))
        for name, target in (links or {}).items():
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            tf.addfile(ti)
    return raw.getvalue()


class FakeServices:
    def __init__(self):
        self.calls = []

    def load(self, label, path, timer=False):
        self.calls.append(("load", label, str(path), timer))

    def restart(self, label):
        self.calls.append(("restart", label))

    def unload(self, label, timer=False):
        self.calls.append(("unload", label, timer))


class BridgeTestBase(TicketdTestCase):
    platform = "darwin"

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "user"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("LARK_CHANNEL_PROFILE", None)
        os.environ.pop("LARK_CHANNEL", None)  # 在 bridge 驱动的会话里跑测试时会继承
        os.environ["LARK_CHANNEL_HOME"] = str(self.home / ".lark-channel")
        self._sys = mock.patch("ticketd.host.system", return_value=self.platform)
        self._sys.start()
        self.addCleanup(self._sys.stop)
        self.blobs = {}
        self.node = _tgz({"node-v24.0.1-%s/bin/node" % self.plat(): (b"#!/bin/sh\necho node\n", 0o755),
                          "node-v24.0.1-%s/LICENSE" % self.plat(): (b"MIT", 0o644),
                          "node-v24.0.1-%s/lib/huge.js" % self.plat(): (b"x" * 10, 0o644)},
                         links={"node-v24.0.1-%s/bin/npm" % self.plat(): "../lib/huge.js"})
        self.lark = _tgz({"lark-cli": (b"#!/bin/sh\necho lark\n", 0o755), "README.md": (b"r", 0o644)})

    def plat(self):
        return "%s-arm64" % self.platform

    def runtime_json(self, node_sha=None, lark_sha=None, node_v="24.0.1"):
        ns = node_sha or hashlib.sha256(self.node).hexdigest()
        ls = lark_sha or hashlib.sha256(self.lark).hexdigest()
        self.blobs[ns], self.blobs[ls] = self.node, self.lark
        return {"node": {"version": node_v, "assets": {self.plat(): {"url": "https://example.invalid/node.tgz",
                                                                      "mirrors": ["https://mirror.invalid/node.tgz"],
                                                                      "sha256": ns}}},
                "lark_cli": {"version": "1.0.86", "assets": {self.plat(): {"url": "https://example.invalid/lark.tgz",
                                                                          "sha256": ls}}}}

    def fetch(self, urls, dest, sha, **kw):
        if sha not in self.blobs:
            raise bridge.BridgeError("SHA-256 不一致")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[sha])
        return urls[0]

    def make_version(self, version: str, commit: str = "a" * 40, runtime: dict = None, entry: bytes = b"// bridge\n"):
        vd = update.versions_dir() / version
        (vd / "bin").mkdir(parents=True, exist_ok=True)
        (vd / "bin" / "ticketd").write_text("#!/bin/sh\n", encoding="utf-8")
        (vd / "bin" / "ticket").write_text("#!/bin/sh\n", encoding="utf-8")
        (vd / "ticketd").mkdir(exist_ok=True)
        (vd / "ticketd" / "__init__.py").write_text('__version__ = "%s"\n' % version, encoding="utf-8")
        vb = vd / "vendor" / "bridge"
        (vb / "bin").mkdir(parents=True, exist_ok=True)
        (vb / "bin" / "lark-channel-bridge.mjs").write_bytes(entry)
        (vb / "package.json").write_text('{"version": "0.7.1"}', encoding="utf-8")
        info = {"repo": "x", "commit": commit, "version": "0.7.1", "tree_sha256": bridge.tree_digest(vb)}
        (vb / "BRIDGE.json").write_text(json.dumps(info), encoding="utf-8")
        (vd / "vendor" / "runtime.json").write_text(json.dumps(runtime or self.runtime_json()), encoding="utf-8")
        return vd

    def make_current(self, version: str, **kw):
        vd = self.make_version(version, **kw)
        launcher.switch(str(update.app_dir()), version)
        return vd


class TreeDigestTest(BridgeTestBase):
    def test_digest_tracks_content_and_mode_and_rejects_links(self):
        d = self.tmp / "b"
        (d / "x").mkdir(parents=True)
        (d / "x" / "a.js").write_text("1", encoding="utf-8")
        one = bridge.tree_digest(d)
        (d / "BRIDGE.json").write_text("{}", encoding="utf-8")
        self.assertEqual(bridge.tree_digest(d), one)  # BRIDGE.json 自己不算
        os.chmod(str(d / "x" / "a.js"), 0o755)
        self.assertNotEqual(bridge.tree_digest(d), one)
        os.symlink("a.js", str(d / "x" / "l"))
        with self.assertRaises(bridge.BridgeError):
            bridge.tree_digest(d)


class RuntimeTest(BridgeTestBase):
    def test_installs_node_and_lark_cli_into_fixed_dirs_only_needed_files(self):
        vd = self.make_version("0.3.0")
        done = bridge.ensure_runtime(vd, plat=self.plat(), fetch=self.fetch)
        self.assertTrue(bridge.node_bin().is_file())
        self.assertTrue(os.access(str(bridge.node_bin()), os.X_OK))
        self.assertEqual(os.readlink(str(bridge.node_link())), "node-v24.0.1-%s" % self.plat())
        nd = bridge.runtime_dir() / ("node-v24.0.1-%s" % self.plat())
        self.assertEqual(sorted(p.relative_to(nd).as_posix() for p in nd.rglob("*") if p.is_file()),
                         ["LICENSE", "bin/node"])  # 链接和用不到的文件不解
        self.assertTrue(bridge.lark_cli().resolve().is_file())
        self.assertEqual(bridge.runtime_versions(), {"node": "24.0.1", "lark_cli": "1.0.86"})
        self.assertTrue(done)
        self.assertEqual(bridge.ensure_runtime(vd, plat=self.plat(), fetch=self.fetch), [])  # 已是这个版本

    def test_bad_checksum_keeps_old_runtime(self):
        vd = self.make_version("0.3.0")
        bridge.ensure_runtime(vd, plat=self.plat(), fetch=self.fetch)
        before = os.readlink(str(bridge.node_link()))
        bad = self.make_version("0.3.1", runtime=self.runtime_json(node_sha="f" * 64, node_v="24.9.9"))
        self.blobs.pop("f" * 64, None)
        with self.assertRaises(bridge.BridgeError):
            bridge.ensure_runtime(bad, plat=self.plat(), fetch=self.fetch)
        self.assertEqual(os.readlink(str(bridge.node_link())), before)

    def test_fetch_verified_tries_mirrors_and_checks_sha(self):
        good = b"payload"
        sha = hashlib.sha256(good).hexdigest()

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(url, timeout):
            if "bad" in url:
                return R(b"tampered")
            return R(good)
        dest = self.tmp / "dl" / "x.tgz"
        used = bridge.fetch_verified(["https://bad/x", "http://plain/x", "https://ok/x"], dest, sha, opener=opener)
        self.assertEqual(used, "https://ok/x")
        self.assertEqual(dest.read_bytes(), good)
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.fetch_verified(["https://bad/x"], self.tmp / "dl" / "y", sha, opener=opener)
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse((self.tmp / "dl" / "y").exists())

    def test_truncated_download_is_retried(self):
        good = b"payload-123"
        sha = hashlib.sha256(good).hexdigest()
        tries = []

        class R(io.BytesIO):
            headers = {"Content-Length": str(len(good))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(url, timeout):
            tries.append(url)
            return R(good[:4] if len(tries) == 1 else good)  # 第一次连接中途断开，urllib 不报错只是读到结尾
        dest = self.tmp / "dl" / "t"
        self.assertEqual(bridge.fetch_verified(["https://ok/x"], dest, sha, opener=opener), "https://ok/x")
        self.assertEqual(len(tries), 2)
        self.assertEqual(dest.read_bytes(), good)

    def test_unknown_platform(self):
        vd = self.make_version("0.3.0")
        with self.assertRaises(bridge.BridgeError):
            bridge.ensure_runtime(vd, plat="sunos-sparc", fetch=self.fetch)


class ServiceRenderDarwinTest(BridgeTestBase):
    platform = "darwin"

    def test_plist_uses_fixed_paths_and_same_label_and_logs(self):
        data = bridge.render_bridge_service("claude", bridge.service_path("/opt/claude/bin"))
        doc = plistlib.loads(data)
        self.assertEqual(doc["Label"], "ai.lark-channel-bridge.bot.claude")
        self.assertEqual(doc["ProgramArguments"], [str(bridge.node_bin()), str(bridge.bridge_entry()),
                                                   "run", "--profile", "claude"])
        self.assertEqual(doc["EnvironmentVariables"]["LARKYARD_MANAGED"], "1")
        self.assertTrue(doc["EnvironmentVariables"]["PATH"].startswith(str(bridge.node_link() / "bin")))
        self.assertIn("/opt/claude/bin", doc["EnvironmentVariables"]["PATH"])
        self.assertTrue(doc["StandardErrorPath"].endswith("profiles/claude/logs/daemon/daemon-stderr.log"))
        self.assertFalse(bridge.pinned_to_user_node(doc["ProgramArguments"] + [doc["EnvironmentVariables"]["PATH"]]))
        p = bridge.bridge_service_file("claude")
        bridge._write(p, data)
        self.assertTrue(bridge.is_managed_service(p))
        self.assertEqual(bridge.service_program(p)[2:], ["run", "--profile", "claude"])

    def test_restart_job_plist(self):
        files = bridge.render_restart_job(Path("/x/ticket"), ["restart", "--if-stale"])
        (path, data), = files.items()
        doc = plistlib.loads(data)
        self.assertEqual(path.name, "dev.larkyard.bridge-restart.plist")
        self.assertEqual(doc["StartCalendarInterval"], {"Hour": 4, "Minute": 7})
        self.assertEqual(doc["ProgramArguments"], ["/x/ticket", "bridge", "restart", "--if-stale"])

    def test_non_ascii_profile_refused(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.bridge_label("我的")


class ServiceRenderLinuxTest(BridgeTestBase):
    platform = "linux"

    def test_unit_and_timer(self):
        data = bridge.render_bridge_service("claude", bridge.service_path()).decode()
        self.assertIn('ExecStart="%s" "%s" "run" "--profile" "claude"' % (bridge.node_bin(), bridge.bridge_entry()), data)
        self.assertIn('Environment="LARKYARD_MANAGED=1"', data)
        self.assertIn("/usr/local/bin", data)
        p = bridge.bridge_service_file("claude")
        self.assertEqual(p.name, "lark-channel-bridge.bot.claude.service")
        self.assertEqual(p.parent, self.home / ".config" / "systemd" / "user")
        bridge._write(p, data.encode())
        self.assertTrue(bridge.is_managed_service(p))
        self.assertEqual(bridge.service_program(p), [str(bridge.node_bin()), str(bridge.bridge_entry()), "run",
                                                     "--profile", "claude"])
        files = bridge.render_restart_job(Path("/x/ticket"), ["restart"], 3, 30)
        names = sorted(p.name for p in files)
        self.assertEqual(names, ["dev.larkyard.bridge-restart.service", "dev.larkyard.bridge-restart.timer"])
        timer = [v for k, v in files.items() if k.suffix == ".timer"][0].decode()
        self.assertIn("OnCalendar=*-*-* 03:30:00", timer)

    def test_unsafe_path_refused_on_linux(self):
        os.environ["TICKETD_HOME"] = str(self.tmp / "with space")
        with self.assertRaises(bridge.BridgeError):
            bridge.render_bridge_service("claude", bridge.service_path())


class RestartTest(BridgeTestBase):
    def setUp(self):
        super().setUp()
        self.v1 = self.make_current("0.3.0", commit="1" * 40)
        bridge.ensure_runtime(self.v1, plat=self.plat(), fetch=self.fetch)
        bridge.link_bridge(self.v1)
        sf = bridge.bridge_service_file("claude")
        bridge._write(sf, bridge.render_bridge_service("claude", bridge.service_path()))
        self.svc = FakeServices()
        self.plat_patch = mock.patch("ticketd.bridge.platform_key", return_value=self.plat())
        self.plat_patch.start()
        self.addCleanup(self.plat_patch.stop)

    def test_if_stale_does_nothing_when_same(self):
        out = bridge.restart("claude", if_stale=True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        self.assertEqual(self.svc.calls, [])
        self.assertIn("不用重启", out[0])

    def test_switches_to_new_bridge_and_confirms_online(self):
        self.make_current("0.4.0", commit="2" * 40)
        self.assertIsNotNone(bridge.stale())
        bridge.restart("claude", if_stale=True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        self.assertEqual(bridge.bridge_version_dir(), "0.4.0")
        self.assertEqual(self.svc.calls, [("restart", "ai.lark-channel-bridge.bot.claude")])
        st = bridge.read_state()
        self.assertEqual(st["commit"], "2" * 40)
        self.assertTrue(st["last_restart"]["ok"])
        self.assertIsNone(bridge.stale())

    def test_not_online_switches_back_and_notifies(self):
        self.make_current("0.4.0", commit="2" * 40)
        sent = []
        with self.assertRaises(bridge.BridgeError):
            bridge.restart("claude", services=self.svc, fetch=self.fetch, wait=lambda *a, **k: False, notify=sent.append)
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        self.assertEqual([c[0] for c in self.svc.calls], ["restart", "restart"])
        self.assertEqual(len(sent), 1)
        self.assertFalse(bridge.read_state()["last_restart"]["ok"])
        self.assertTrue(bridge.doctor_line("claude").startswith("❌"))

    def test_service_manager_failure_switches_back(self):
        self.make_current("0.4.0", commit="2" * 40)

        def boom(label):
            raise bridge.BridgeError("kickstart 失败")
        self.svc.restart = boom
        with self.assertRaises(bridge.BridgeError):
            bridge.restart("claude", services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        self.assertIsNotNone(bridge.stale())  # 下一个窗口还会再试
        self.assertFalse(bridge.read_state()["last_restart"]["ok"])

    def test_not_switched_during_probation(self):
        self.make_current("0.4.0", commit="2" * 40)
        update.write_state({"status": "probation", "from": "0.3.0", "to": "0.4.0"})
        out = bridge.restart("claude", if_stale=True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        self.assertIn("试用期", out[0])
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        self.assertEqual(self.svc.calls, [])

    def test_runtime_failure_does_not_switch(self):
        self.make_current("0.4.0", commit="2" * 40, runtime=self.runtime_json(node_sha="e" * 64, node_v="25.0.0"))
        self.blobs.pop("e" * 64, None)
        with self.assertRaises(bridge.BridgeError):
            bridge.restart("claude", services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        self.assertEqual(self.svc.calls, [])

    def test_refuses_unmanaged_service_and_worker(self):
        bridge._write(bridge.bridge_service_file("claude"), plistlib.dumps({"Label": "x", "ProgramArguments": ["a"]}))
        with self.assertRaises(bridge.BridgeError):
            bridge.restart("claude", services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True)
        os.environ["TICKETD_TICKET"] = "T0001"
        with self.assertRaises(bridge.BridgeError):
            bridge.restart("claude", services=self.svc)

    def test_prune_keeps_the_version_bridge_runs(self):
        for v in ("0.4.0", "0.5.0", "0.6.0", "0.7.0", "0.8.0"):
            self.make_version(v)
            time.sleep(0.01)
        launcher.switch(str(update.app_dir()), "0.8.0")
        update.prune(keep=2)
        left = sorted(p.name for p in update.versions_dir().iterdir())
        self.assertIn("0.3.0", left)  # bridge/current 还指着它
        self.assertNotIn("0.4.0", left)

    def test_note_pending_and_doctor(self):
        self.assertIsNone(bridge.note_pending(self.con))
        self.make_current("0.4.0", commit="2" * 40)
        self.assertEqual(bridge.note_pending(self.con), "0.7.1")
        self.assertTrue(bridge.doctor_line("claude").startswith("⚠️"))
        self.assertTrue(any("待生效" in x for x in bridge.status_lines("claude")))

    def test_online_since_reads_bridge_log(self):
        logs = Path(os.environ["LARK_CHANNEL_HOME"]) / "profiles" / "claude" / "logs"
        logs.mkdir(parents=True)
        now = time.time()
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(t))  # noqa: E731
        f = logs / ("bridge-%s.jsonl" % time.strftime("%Y%m%d"))
        f.write_text(json.dumps({"ts": stamp(now - 100), "phase": "ws", "event": "connected"}) + "\n", encoding="utf-8")
        self.assertFalse(bridge.online_since("claude", now - 10))
        with open(str(f), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": stamp(now), "phase": "supervisor", "event": "profile-online"}) + "\n")
        self.assertTrue(bridge.online_since("claude", now - 10))


class AdoptTestMixin:
    """把一台「分开安装」的机器搭出来：npm 全局装的 bridge 服务、secrets-getter、每天的重启任务、ticketd 服务。"""

    def seed(self):
        self.svc = FakeServices()
        self.plat_patch = mock.patch("ticketd.bridge.platform_key", return_value=self.plat())
        self.plat_patch.start()
        self.addCleanup(self.plat_patch.stop)
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        (lc / "profiles" / "claude").mkdir(parents=True)
        (lc / "active-profile").write_text("claude\n", encoding="utf-8")
        (lc / "config.json").write_text('{"secret": "do-not-touch"}', encoding="utf-8")
        self.npm = str(self.home / ".nvm/versions/node/v24.15.0/bin")
        self.getter = lc / "secrets-getter"
        self.getter.write_text("#!/bin/sh\nexec '%s/node' '%s/lark-channel-bridge' secrets get \"$@\"\n" % (
            self.npm, self.npm), encoding="utf-8")
        os.chmod(str(self.getter), 0o700)
        sf = bridge.bridge_service_file("claude")
        if self.platform == "darwin":
            data = plistlib.dumps({
                "Label": "ai.lark-channel-bridge.bot.claude",
                "ProgramArguments": [self.npm + "/node", self.npm + "/lark-channel-bridge", "run", "--profile", "claude"],
                "RunAtLoad": True, "KeepAlive": True,
                "StandardOutPath": str(lc / "profiles/claude/logs/daemon/daemon-stdout.log"),
                "StandardErrorPath": str(lc / "profiles/claude/logs/daemon/daemon-stderr.log"),
                "EnvironmentVariables": {"PATH": self.npm + ":/usr/bin", "LARK_CHANNEL_HOME": str(lc)}})
            job = self.home / "Library/LaunchAgents/com.example.daily-restart.plist"
            jobdata = plistlib.dumps({"Label": "com.example.daily-restart", "ProgramArguments": [
                "/bin/sh", "-c", "%s/node %s/lark-channel-bridge restart --profile claude" % (self.npm, self.npm)],
                "StartCalendarInterval": {"Hour": 4, "Minute": 7}})
            td = self.home / "Library/LaunchAgents/dev.larkyard.ticketd.plist"
            tddata = plistlib.dumps({"Label": "dev.larkyard.ticketd", "ProgramArguments": [
                "/usr/bin/python3", str(update.app_dir() / "launcher.py")],
                                     "EnvironmentVariables": {"TICKETD_HOME": os.environ["TICKETD_HOME"],
                                                              "PATH": self.npm}})
            self.files = {sf: data, job: jobdata, td: tddata}
        else:
            data = ('[Service]\nExecStart="%s/node" "%s/lark-channel-bridge" run --profile claude\n'
                    'Environment="LARK_CHANNEL_HOME=%s"\nStandardError=append:%s\n' % (
                        self.npm, self.npm, lc, lc / "profiles/claude/logs/daemon/daemon-stderr.log")).encode()
            ud = self.home / ".config/systemd/user"
            job = ud / "bridge-daily.service"
            jobdata = ('[Service]\nType=oneshot\nExecStart="%s/node" "%s/lark-channel-bridge" restart\n' % (
                self.npm, self.npm)).encode()
            timer = ud / "bridge-daily.timer"
            td = ud / "dev.larkyard.ticketd.service"
            tddata = ('[Service]\nExecStart="/usr/bin/python3" "%s"\nEnvironment="TICKETD_HOME=%s"\n' %
                      (update.app_dir() / "launcher.py", os.environ["TICKETD_HOME"])).encode()
            self.files = {sf: data, job: jobdata, timer: b"[Timer]\nOnCalendar=*-*-* 05:15:00\n", td: tddata}
        for p, d in self.files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(d)
        self.files[self.getter] = self.getter.read_bytes()
        self.make_current("0.3.0", commit="3" * 40)
        self.installs = []

    def run_install(self, repo, app, svc):
        self.installs.append((str(repo), str(app), svc))
        td = [p for p in self.files if "ticketd" in p.name][0]
        td.write_bytes(b"rewritten by install.sh")

    def snapshot(self):
        return {p: p.read_bytes() for p in self.files}


class AdoptDarwinTest(AdoptTestMixin, BridgeTestBase):
    platform = "darwin"

    def setUp(self):
        super().setUp()
        self.seed()

    def test_plan_dry_run_writes_nothing(self):
        before = self.snapshot()
        plan = bridge.adopt_plan()
        self.assertEqual(plan["restart_jobs"][0]["label"], "com.example.daily-restart")
        lines = bridge.adopt_plan_lines(plan)
        self.assertTrue(any("bridge 会重启一次" in x for x in lines))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(bridge.bridge_home().exists())

    def test_no_without_yes(self):
        before = self.snapshot()
        self.assertEqual(bridge.adopt(confirm=lambda text: False, services=self.svc), ["没有改任何东西"])
        self.assertEqual(self.snapshot(), before)

    def test_adopt_then_undo_is_byte_identical(self):
        before = self.snapshot()
        lc = Path(os.environ["LARK_CHANNEL_HOME"])
        out = bridge.adopt(confirm=lambda text: True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True,
                           run_install=self.run_install)
        self.assertTrue(any("已迁移" in x for x in out))
        sf = bridge.bridge_service_file("claude")
        doc = plistlib.loads(sf.read_bytes())
        self.assertEqual(doc["Label"], "ai.lark-channel-bridge.bot.claude")
        self.assertEqual(doc["ProgramArguments"][:2], [str(bridge.node_bin()), str(bridge.bridge_entry())])
        self.assertTrue(doc["StandardErrorPath"].startswith(str(lc)))
        self.assertFalse(bridge.pinned_to_user_node(doc["ProgramArguments"]))
        self.assertTrue(doc["EnvironmentVariables"]["PATH"].startswith(str(bridge.node_link() / "bin")))
        self.assertIn(str(bridge.node_bin()), self.getter.read_text(encoding="utf-8"))
        job = plistlib.loads((self.home / "Library/LaunchAgents/com.example.daily-restart.plist").read_bytes())
        self.assertEqual(job["ProgramArguments"][1:], ["bridge", "restart", "--profile", "claude"])
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 4, "Minute": 7})
        self.assertEqual(len(self.installs), 1)
        self.assertEqual((lc / "config.json").read_text(encoding="utf-8"), '{"secret": "do-not-touch"}')
        self.assertTrue(bridge.doctor_line("claude").startswith("✅"))
        self.svc.calls.clear()
        bridge.undo_adopt(services=self.svc)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(("load", "ai.lark-channel-bridge.bot.claude", str(sf), False), self.svc.calls)
        self.assertNotIn("adopted", bridge.read_state())
        with self.assertRaises(bridge.BridgeError):
            bridge.undo_adopt(services=self.svc)

    def test_not_online_rolls_back_automatically(self):
        before = self.snapshot()
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.adopt(confirm=lambda text: True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: False,
                         run_install=self.run_install)
        self.assertIn("已自动退回", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_preconditions(self):
        os.environ["TICKETD_SPOOL"] = "/x"
        with self.assertRaises(bridge.BridgeError):
            bridge.adopt_plan()
        os.environ.pop("TICKETD_SPOOL")
        (update.current_link() / "vendor" / "bridge" / "bin" / "lark-channel-bridge.mjs").unlink()
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.adopt_plan()
        self.assertIn("没有打包 bridge", str(cm.exception))

    def test_old_path_kept_after_fixed_dirs_and_job_has_profile(self):
        bridge.adopt(confirm=lambda text: True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True,
                     run_install=self.run_install)
        path = plistlib.loads(bridge.bridge_service_file("claude").read_bytes())["EnvironmentVariables"]["PATH"]
        parts = path.split(":")
        self.assertEqual(parts[:2], [str(bridge.node_link() / "bin"), str(bridge.bin_dir())])
        self.assertIn(self.npm, parts)  # npm 全局装的工具、npx 还在 PATH 上
        job = plistlib.loads((self.home / "Library/LaunchAgents/com.example.daily-restart.plist").read_bytes())
        self.assertEqual(job["ProgramArguments"][1:], ["bridge", "restart", "--profile", "claude"])
        self.assertEqual(job["EnvironmentVariables"]["LARK_CHANNEL_HOME"], os.environ["LARK_CHANNEL_HOME"])
        self.assertNotIn("LARK_CHANNEL", job["EnvironmentVariables"])

    def test_refused_inside_a_bridge_session_but_dry_run_allowed(self):
        os.environ["LARK_CHANNEL"] = "1"
        self.assertTrue(bridge.adopt_plan_lines(bridge.adopt_plan()))
        before = self.snapshot()
        for call in (lambda: bridge.adopt(confirm=lambda text: True, services=self.svc),
                     lambda: bridge.restart("claude", services=self.svc)):
            with self.assertRaises(bridge.BridgeError) as cm:
                call()
            self.assertIn("LARK_CHANNEL=1", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    def test_refuses_clone_run_ticketd_running_tickets_and_odd_jobs(self):
        td = self.home / "Library/LaunchAgents/dev.larkyard.ticketd.plist"
        good = td.read_bytes()
        td.write_bytes(plistlib.dumps({"Label": "dev.larkyard.ticketd", "ProgramArguments": ["/src/clone/bin/ticketd"]}))
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.adopt_plan()
        self.assertIn("ticket update adopt", str(cm.exception))
        td.write_bytes(good)
        t = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (t,))
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.adopt_plan()
        self.assertIn("正在运行", str(cm.exception))
        self.con.execute("UPDATE tickets SET state='done' WHERE id=?", (t,))
        job = self.home / "Library/LaunchAgents/com.example.daily-restart.plist"
        doc = plistlib.loads(job.read_bytes())
        doc["StartCalendarInterval"] = {"Minute": 30}  # 每小时
        job.write_bytes(plistlib.dumps(doc))
        with self.assertRaises(bridge.BridgeError) as cm:
            bridge.adopt_plan()
        self.assertIn("不是「每天一次」", str(cm.exception))

    def test_early_failure_does_not_restart_services(self):
        before = self.snapshot()

        def boom(*a, **k):
            raise bridge.BridgeError("launchctl 坏了")
        self.svc.load = boom
        calls = []
        with self.assertRaises(bridge.BridgeError):
            bridge.adopt(confirm=lambda text: True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True,
                         run_install=lambda *a: calls.append(a))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(calls, [])  # 还没走到 ticketd 那一步

    def test_unknown_program_left_alone(self):
        sf = bridge.bridge_service_file("claude")
        sf.write_bytes(plistlib.dumps({"Label": "ai.lark-channel-bridge.bot.claude", "ProgramArguments": ["/opt/x"]}))
        with self.assertRaises(bridge.BridgeError):
            bridge.adopt_plan()


class AdoptLinuxTest(AdoptTestMixin, BridgeTestBase):
    platform = "linux"

    def setUp(self):
        super().setUp()
        self.seed()

    def test_adopt_then_undo_is_byte_identical(self):
        before = self.snapshot()
        plan = bridge.adopt_plan()
        self.assertEqual(plan["restart_jobs"][0]["hour"], 5)
        bridge.adopt(confirm=lambda text: True, services=self.svc, fetch=self.fetch, wait=lambda *a, **k: True,
                     run_install=self.run_install)
        unit = bridge.bridge_service_file("claude").read_text(encoding="utf-8")
        self.assertIn('Environment="LARKYARD_MANAGED=1"', unit)
        self.assertIn("lark-channel-bridge.bot.claude", [c[1] for c in self.svc.calls if c[0] == "load"])
        timer = (self.home / ".config/systemd/user/bridge-daily.timer").read_text(encoding="utf-8")
        self.assertIn("05:15:00", timer)
        bridge.undo_adopt(services=self.svc)
        self.assertEqual(self.snapshot(), before)

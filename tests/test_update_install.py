"""受管安装：解包、切换、保留版本、启动器的自检回滚（含新版本 bin/ticketd 有语法错误）、确认和启动器两步更新、
切回后的私信和拉黑、空闲判定、adopt / adopt --undo。对应 spec self-update 的「受管安装和并排切换」「自检不过自动切回」。"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import REPO, TicketdTestCase
import signing

from ticketd import config, launcher, metrics, update


def make_version(app: Path, version: str, ticketd_sh: str = None, init: str = None) -> Path:
    vd = app / "versions" / version
    (vd / "bin").mkdir(parents=True, exist_ok=True)
    (vd / "ticketd").mkdir(parents=True, exist_ok=True)
    (vd / "bin" / "ticketd").write_text(ticketd_sh or "#!/bin/sh\necho ran-%s \"$@\"\n" % version)
    (vd / "bin" / "ticketd").chmod(0o755)
    (vd / "ticketd" / "__init__.py").write_text(init or '__version__ = "%s"\n' % version)
    (vd / "ticketd" / "launcher.py").write_text((REPO / "ticketd" / "launcher.py").read_text(encoding="utf-8"))
    return vd


class ManagedBase(TicketdTestCase):
    def setUp(self):
        super().setUp()
        self.app = update.app_dir()
        self.app.mkdir(parents=True)
        (self.app / "launcher.py").write_text((REPO / "ticketd" / "launcher.py").read_text(encoding="utf-8"))
        make_version(self.app, "0.3.0")
        launcher.switch(str(self.app), "0.3.0")
        update.write_state({"status": "confirmed", "version": "0.3.0"})

    def run_launcher(self, *args):
        """像 launchd / systemd 那样拉起一次启动器（launcher.py 在版本目录外，用当前解释器跑）。"""
        return subprocess.run([sys.executable, str(self.app / "launcher.py")] + list(args), capture_output=True,
                              text=True, timeout=30)

    def probation(self, frm="0.3.0", to="0.3.1", deadline=None, starts=0):
        st = {"status": "probation", "from": frm, "to": to, "starts": starts, "token": "t1",
              "deadline": deadline if deadline is not None else time.time() + 480, "switched_at": time.time()}
        update.write_state(st)
        if not (self.app / "versions" / to).is_dir():
            make_version(self.app, to)
        launcher.switch(str(self.app), to)


class ExtractTest(ManagedBase):
    def tgz(self, members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tf:
            for ti, data in members:
                tf.addfile(ti, io.BytesIO(data) if data is not None else None)
        p = self.tmp / "x.tar.gz"
        p.write_bytes(raw.getvalue())
        return p

    def member(self, name, data=b"x", typ=tarfile.REGTYPE, link=""):
        ti = tarfile.TarInfo(name)
        ti.type, ti.linkname = typ, link
        ti.size = len(data) if typ == tarfile.REGTYPE else 0
        return ti, data if typ == tarfile.REGTYPE else None

    def test_rejects_absolute_dotdot_links_and_outsiders(self):
        bad = [
            [self.member("/etc/evil")],
            [self.member("larkyard-0.3.1/../../evil")],
            [self.member("larkyard-0.3.1/bin/x", typ=tarfile.SYMTYPE, link="/etc/passwd")],
            [self.member("larkyard-0.3.1/bin/x", typ=tarfile.LNKTYPE, link="larkyard-0.3.1/y")],
            [self.member("other/file")],
            [self.member("larkyard-0.3.1/dev", typ=tarfile.CHRTYPE)],
        ]
        for members in bad:
            with self.assertRaises(update.UpdateError, msg=members[0][0].name):
                update.extract(self.tgz(members), self.tmp / "out", "larkyard-0.3.1")
        self.assertFalse((self.tmp / "evil").exists())

    def test_unpack_checks_version_and_entry(self):
        p = self.tmp / "p.tar.gz"
        p.write_bytes(signing.package("0.3.1", {"ticketd/__init__.py": '__version__ = "9.9.9"\n'}))
        with self.assertRaises(update.UpdateError):
            update.unpack(p, "0.3.1")
        p.write_bytes(signing.package("0.3.1"))
        vd = update.unpack(p, "0.3.1")
        self.assertTrue(os.access(str(vd / "bin" / "ticketd"), os.X_OK))
        self.assertEqual(update.package_version(vd), "0.3.1")
        self.assertEqual(update.current_version(), "0.3.0")  # 解包不切换


class SwitchTest(ManagedBase):
    def test_switch_is_atomic_relative_symlink(self):
        make_version(self.app, "0.3.1")
        launcher.switch(str(self.app), "0.3.1")
        cur = self.app / "current"
        self.assertTrue(cur.is_symlink())
        self.assertEqual(os.readlink(str(cur)), "versions/0.3.1")
        with self.assertRaises(FileNotFoundError):
            launcher.switch(str(self.app), "9.9.9")
        with self.assertRaises(ValueError):
            launcher.switch(str(self.app), "../x")
        self.assertEqual(update.current_version(), "0.3.1")

    def test_prune_keeps_three_and_current(self):
        for i, v in enumerate(["0.1.0", "0.2.0", "0.2.1", "0.3.1", "0.3.2"]):
            vd = make_version(self.app, v)
            os.utime(str(vd), (1000 + i, 1000 + i))
        os.utime(str(self.app / "versions" / "0.3.0"), (1, 1))  # 当前版本最旧也保留
        update.write_state({"status": "confirmed", "version": "0.3.0", "previous": "0.1.0"})
        removed = update.prune()
        left = sorted(p.name for p in (self.app / "versions").iterdir())
        self.assertEqual(sorted(removed), ["0.2.0"])
        self.assertEqual(left, ["0.1.0", "0.2.1", "0.3.0", "0.3.1", "0.3.2"])

    def test_install_writes_probation_and_switches(self):
        tgz = update.update_dir() / "pending" / "larkyard-0.3.1.tar.gz"
        tgz.parent.mkdir(parents=True)
        data = signing.package("0.3.1")
        tgz.write_bytes(data)
        import hashlib
        pend = {"version": "0.3.1", "kind": "release", "seq": 5, "sha256": hashlib.sha256(data).hexdigest(),
                "package": tgz.name}
        update.install(self.con, pend)
        st = update.read_state()
        self.assertEqual((st["status"], st["from"], st["to"], st["starts"]), ("probation", "0.3.0", "0.3.1", 0))
        self.assertLess(st["deadline"] - st["switched_at"], config.update_probation())
        self.assertEqual(update.current_version(), "0.3.1")
        # 安装没有动用户配置、数据库、工单目录
        self.assertTrue(config.db_path().is_file())

    def test_install_carries_adopt_origin(self):
        """自动更新重写状态时带上 adopt 的来历：之后 adopt --undo 仍能改回工作区 CLAUDE.md 和原来的服务。"""
        update.write_state({"status": "confirmed", "version": "0.3.0", "adopted_from": "/clone", "label": "l.b",
                            "workspace_md": "/ws/CLAUDE.md", "service_env": {"LARK_CHANNEL_PROFILE": "p"}})
        tgz = update.update_dir() / "pending" / "larkyard-0.3.1.tar.gz"
        tgz.parent.mkdir(parents=True)
        data = signing.package("0.3.1")
        tgz.write_bytes(data)
        import hashlib
        update.install(self.con, {"version": "0.3.1", "kind": "release", "seq": 5,
                                  "sha256": hashlib.sha256(data).hexdigest(), "package": tgz.name})
        st = update.read_state()
        self.assertEqual((st["status"], st["adopted_from"], st["label"], st["workspace_md"]),
                         ("probation", "/clone", "l.b", "/ws/CLAUDE.md"))
        self.assertEqual(st["service_env"], {"LARK_CHANNEL_PROFILE": "p"})
        update.confirm(self.con, version="0.3.1")
        self.assertEqual(update.read_state()["workspace_md"], "/ws/CLAUDE.md")

    def test_install_rejects_package_changed_after_check(self):
        tgz = update.update_dir() / "pending" / "larkyard-0.3.1.tar.gz"
        tgz.parent.mkdir(parents=True)
        tgz.write_bytes(signing.package("0.3.1"))
        pend = {"version": "0.3.1", "kind": "release", "seq": 5, "sha256": "0" * 64, "package": tgz.name}
        with self.assertRaises(update.UpdateError):
            update.install(self.con, pend)
        self.assertEqual(update.current_version(), "0.3.0")
        self.assertFalse(tgz.exists())


class LauncherTest(ManagedBase):
    def test_normal_start_execs_current(self):
        p = self.run_launcher("--x")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "ran-0.3.0 --x")

    def test_launcher_imports_nothing_from_version_dirs(self):
        src = (REPO / "ticketd" / "launcher.py").read_text(encoding="utf-8")
        imports = {l.split()[1].split(".")[0] for l in src.splitlines() if l.startswith(("import ", "from "))}
        self.assertEqual(imports, {"json", "os", "signal", "sys", "time"})
        self.assertNotIn("ticketd", imports)

    def test_crash_three_starts_rolls_back(self):
        make_version(self.app, "0.3.1", ticketd_sh="#!/bin/sh\nexit 1\n")
        self.probation()
        for _ in range(2):
            self.assertEqual(self.run_launcher().returncode, 1)
            self.assertEqual(update.current_version(), "0.3.1")
        p = self.run_launcher()
        self.assertEqual(p.stdout.strip(), "ran-0.3.0")
        st = update.read_state()
        self.assertEqual((st["status"], st["reason"], st["starts"]), ("rolled_back", "crash", 3))
        self.assertEqual(update.current_version(), "0.3.0")

    def test_syntax_error_in_new_entry_rolls_back_within_probation(self):
        """新版本的 bin/ticketd 和 ticketd/__init__.py 都有语法错误：按 launchd 的节流（10 秒一次）模拟拉起，
        600 秒内回到旧版本，之后不再无限重启。"""
        make_version(self.app, "0.3.1", ticketd_sh="#!/bin/sh\nif then fi ((\n", init="def (:\n")
        start = time.time()
        update.write_state({"status": "probation", "from": "0.3.0", "to": "0.3.1", "starts": 0, "token": "t",
                            "deadline": update.confirm_deadline(start, 600), "switched_at": start})
        launcher.switch(str(self.app), "0.3.1")
        clock, throttle, outputs = 0.0, 10.0, []
        while clock < 600:
            p = self.run_launcher()
            outputs.append(p.stdout.strip())
            if p.returncode == 0 and p.stdout.strip() == "ran-0.3.0":
                break
            clock += throttle
        self.assertLess(clock, 600)
        self.assertEqual(outputs[-1], "ran-0.3.0")
        self.assertEqual(update.read_state()["status"], "rolled_back")
        # 回到旧版本后照常启动，不再切换
        for _ in range(3):
            self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")

    def test_deadline_passed_rolls_back(self):
        make_version(self.app, "0.3.1")
        self.probation(deadline=time.time() - 1)
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")
        self.assertEqual(update.read_state()["reason"], "timeout")

    def test_deadline_passed_without_watchdog_extends_once(self):
        """墙钟过了 deadline 但看门狗没动手（试用期里关机了）：重新给一次完整试用期，第二次再过期才切回。"""
        make_version(self.app, "0.3.1")
        self.probation(deadline=time.time() - 1)
        st = update.read_state()
        st["window"] = 480
        update.write_state(st)
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.1")
        st = update.read_state()
        self.assertEqual((st["status"], st["extended"]), ("probation", True))
        self.assertGreater(st["deadline"], time.time() + 400)
        st["deadline"] = time.time() - 1
        update.write_state(st)
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")
        self.assertEqual(update.read_state()["reason"], "timeout")

    def test_watchdog_timed_out_rolls_back_even_before_deadline(self):
        make_version(self.app, "0.3.1")
        self.probation()
        st = update.read_state()
        st.update(timed_out=True, window=480)
        update.write_state(st)
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")
        self.assertEqual(update.read_state()["reason"], "timeout")

    def test_previous_version_missing(self):
        make_version(self.app, "0.3.1", ticketd_sh="#!/bin/sh\nexit 1\n")
        self.probation(frm="0.2.9", starts=2)
        self.run_launcher()
        st = update.read_state()
        self.assertEqual(st["status"], "rollback_failed")
        self.assertEqual(update.current_version(), "0.3.1")

    def test_corrupt_state_still_starts(self):
        (self.app / "update-state.json").write_text("{not json")
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")
        (self.app / "update-state.json").write_text(json.dumps({"status": "probation", "from": "0.3.0", "to": "x",
                                                               "starts": "bad", "deadline": "bad"}))
        self.assertEqual(self.run_launcher().stdout.strip(), "ran-0.3.0")

    def test_confirmed_state_is_left_alone(self):
        before = (self.app / "update-state.json").read_text()
        self.run_launcher()
        self.assertEqual((self.app / "update-state.json").read_text(), before)


class WatchdogTest(ManagedBase):
    def test_kills_unconfirmed_daemon_after_deadline(self):
        self.probation(deadline=100.0)
        now = [0.0]
        kills = []
        alive = {"v": True}

        def kill(pid, sig):
            kills.append(sig)
            alive["v"] = False

        with mock.patch.object(launcher, "_alive", lambda pid: alive["v"]):
            did = launcher.watch(str(self.app), 12345, "t1", 100.0, clock=lambda: now[0],
                                 sleep=lambda s: now.__setitem__(0, now[0] + s), kill=kill)
        self.assertTrue(did)
        self.assertEqual(kills, [signal.SIGTERM])
        self.assertGreaterEqual(now[0], 100.0 + launcher.WATCHDOG_GRACE)
        self.assertTrue(update.read_state()["timed_out"])  # 下次拉起时启动器据此切回

    def test_stops_watching_once_confirmed(self):
        self.probation(deadline=100.0)
        now = [0.0]

        def sleep(s):
            now[0] += s
            if now[0] > 50:
                update.write_state({"status": "confirmed", "version": "0.3.1"})

        with mock.patch.object(launcher, "_alive", lambda pid: True):
            did = launcher.watch(str(self.app), 1, "t1", 100.0, clock=lambda: now[0], sleep=sleep,
                                 kill=lambda *a: self.fail("不该结束已确认的守护进程"))
        self.assertFalse(did)

    def test_sigkill_when_sigterm_ignored(self):
        self.probation(deadline=0.0)
        now = [1000.0]
        kills = []
        with mock.patch.object(launcher, "_alive", lambda pid: True):
            launcher.watch(str(self.app), 1, "t1", 0.0, clock=lambda: now[0],
                           sleep=lambda s: now.__setitem__(0, now[0] + s), kill=lambda p, s: kills.append(s))
        self.assertEqual(kills, [signal.SIGTERM, signal.SIGKILL])

    def test_timeline_fits_probation(self):
        """看门狗动手 + 停机最长 + 服务管理器节流，都要在试用期之内。"""
        p = 600
        worst = update.confirm_deadline(0, p) + launcher.WATCHDOG_GRACE + launcher.KILL_WAIT + 10
        self.assertLess(worst, p)


class ConfirmTest(ManagedBase):
    def test_confirm_replaces_launcher_and_prunes(self):
        make_version(self.app, "0.3.1")
        new_launcher = (REPO / "ticketd" / "launcher.py").read_text(encoding="utf-8") + "\n# v0.3.1\n"
        (self.app / "versions" / "0.3.1" / "ticketd" / "launcher.py").write_text(new_launcher)
        self.probation()
        self.assertTrue(update.confirm(self.con, version="0.3.1"))
        st = update.read_state()
        self.assertEqual((st["status"], st["version"], st["previous"]), ("confirmed", "0.3.1", "0.3.0"))
        self.assertNotIn("deadline", st)
        self.assertEqual((self.app / "launcher.py").read_text(), new_launcher)
        self.assertIn("confirmed", [r["code"] for r in self.con.execute("SELECT code FROM events")])

    def test_launcher_not_replaced_during_probation_or_if_broken(self):
        make_version(self.app, "0.3.1")
        (self.app / "versions" / "0.3.1" / "ticketd" / "launcher.py").write_text("def (:\n")
        before = (self.app / "launcher.py").read_text()
        self.probation()
        self.assertEqual((self.app / "launcher.py").read_text(), before)  # 试用期内没换
        update.confirm(self.con, version="0.3.1")
        self.assertEqual((self.app / "launcher.py").read_text(), before)  # 新启动器有语法错误：保留旧的

    def test_confirm_needs_matching_version(self):
        make_version(self.app, "0.3.1")
        self.probation()
        self.assertFalse(update.confirm(self.con, version="0.3.0"))
        self.assertEqual(update.read_state()["status"], "probation")

    def test_updater_confirms_after_healthy_run(self):
        make_version(self.app, "0.3.1")
        self.probation()
        now = [time.time()]
        config.heartbeat_path().touch()
        with mock.patch.object(update, "__version__", "0.3.1"), \
                mock.patch.object(update, "is_managed", return_value=True):
            up = update.Updater(self.con, clock=lambda: now[0])
            up.tick()
            self.assertEqual(update.read_state()["status"], "probation")  # 刚启动不算
            now[0] += update.CONFIRM_AFTER + 1
            up.tick(healthy=False)
            self.assertEqual(update.read_state()["status"], "probation")  # 主循环在报错不算
            up.tick(healthy=True)
        self.assertEqual(update.read_state()["status"], "confirmed")


class AfterRollbackTest(ManagedBase):
    def test_dm_once_and_block_version(self):
        update.write_state({"status": "rolled_back", "from": "0.3.0", "to": "0.3.1", "reason": "crash",
                            "notified": False})
        metrics.kv_set(self.con, "update.max_seq", "5")
        text = update.after_rollback(self.con)
        self.assertIn("更新到 0.3.1 失败", text)
        self.assertIn("已回到 0.3.0", text)
        rows = self.con.execute("SELECT text FROM outbox WHERE reply_to=?", (metrics.OWNER,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("0.3.1", rows[0]["text"])
        self.assertIn("0.3.1", update.blocked_versions(self.con))
        self.assertIsNone(update.after_rollback(self.con))  # 只私信一次
        self.assertEqual(len(self.con.execute("SELECT id FROM outbox").fetchall()), 1)
        self.assertEqual(metrics.kv_get(self.con, "update.max_seq"), "5")  # 序号不因回滚变小


class IdleTest(ManagedBase):
    def setUp(self):
        super().setUp()
        self.pend = {"version": "0.3.1", "kind": "release", "seq": 5, "sha256": "x", "package": "p"}
        update.kv_put(self.con, "update.pending", self.pend)

    def updater(self):
        up = update.Updater(self.con)
        up._enabled = lambda: True
        up.next_check = float("inf")
        return up

    def test_running_worker_blocks_install(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (tid,))
        with mock.patch.object(update, "install") as inst:
            self.assertFalse(self.updater().tick())
            inst.assert_not_called()
            self.con.execute("UPDATE tickets SET state='idle' WHERE id=?", (tid,))
            self.assertTrue(self.updater().tick())
            inst.assert_called_once()

    def test_out_of_sandbox_run_blocks_install(self):
        tid = self.new_ticket()
        for status in ("approved", "running"):
            self.con.execute("DELETE FROM runs")
            self.con.execute("INSERT INTO runs(ticket_id, path, script, status, created_at) VALUES(?,?,?,?,?)",
                             (tid, "x.sh", "echo", status, time.time()))
            with mock.patch.object(update, "install") as inst:
                self.assertFalse(self.updater().tick())
                inst.assert_not_called()
        self.con.execute("UPDATE runs SET status='done'")
        with mock.patch.object(update, "install") as inst:
            self.assertTrue(self.updater().tick())

    def test_scheduler_and_media_state(self):
        class S:
            running, runs, update_hold = {"T1": 1}, {}, False

        class M:
            def busy(self):
                return [1]

        self.assertTrue(update.busy_reasons(self.con, S(), None))
        S.running = {}
        self.assertFalse(update.busy_reasons(self.con, S(), None))
        self.assertTrue(update.busy_reasons(self.con, S(), M()))
        s = S()
        with mock.patch.object(update, "install"):
            self.assertTrue(self.updater().tick(s, None))
        self.assertTrue(s.update_hold)

    def test_environment_failure_backs_off_then_blocks(self):
        """磁盘满这类环境问题：退避到下次检查再试，连续 3 次失败才拉黑；包本身坏了（UpdateError）立即拉黑。"""
        up = self.updater()
        with mock.patch.object(update, "install", side_effect=OSError("disk full")), \
                mock.patch.object(update, "next_delay", return_value=100.0):
            t = 1000.0
            for i in range(3):
                self.assertFalse(up._maybe_install(None, None, t))
                if i < 2:
                    pend = update.kv_json(self.con, "update.pending")
                    self.assertEqual((pend["failures"], pend["retry_at"]), (i + 1, t + 100))
                    self.assertFalse(up._maybe_install(None, None, t + 50))  # 退避期内不重试
                t += 101
        self.assertIsNone(update.kv_json(self.con, "update.pending"))
        self.assertIn("0.3.1", update.blocked_versions(self.con))

    def test_bad_package_blocks_immediately(self):
        with mock.patch.object(update, "install", side_effect=update.UpdateError("缺入口")):
            self.assertFalse(self.updater()._maybe_install(None, None, 1000.0))
        self.assertIsNone(update.kv_json(self.con, "update.pending"))
        self.assertIn("0.3.1", update.blocked_versions(self.con))

    def test_scheduler_does_not_launch_during_install(self):
        from ticketd.scheduler import Scheduler
        sched = Scheduler(self.con)
        sched.update_hold = True
        self.new_ticket()
        sched.tick()
        self.assertEqual(sched.running, {})
        self.assertEqual(self.con.execute("SELECT state FROM tickets").fetchone()["state"], "queued")

    def test_install_failure_releases_hold(self):
        class S:
            running, runs, update_hold = {}, {}, False
        s = S()
        with mock.patch.object(update, "install", side_effect=update.UpdateError("x")):
            self.assertFalse(self.updater().tick(s, None))
        self.assertFalse(s.update_hold)


class ManualRollbackTest(ManagedBase):
    def test_rollback_to_previous_and_block(self):
        make_version(self.app, "0.3.1")
        launcher.switch(str(self.app), "0.3.1")
        update.write_state({"status": "confirmed", "version": "0.3.1", "previous": "0.3.0"})
        from types import SimpleNamespace
        with mock.patch.object(update, "is_managed", return_value=True):
            out = update.command(self.con, SimpleNamespace(action="rollback"))
            self.assertIn("0.3.0", out)
            up = update.Updater(self.con)
            self.assertTrue(up.tick())
        self.assertEqual(update.current_version(), "0.3.0")
        self.assertIn("0.3.1", update.blocked_versions(self.con))
        st = update.read_state()
        self.assertEqual((st["status"], st["version"]), ("confirmed", "0.3.0"))
        # 再请求一次：没有比 0.3.0 更低的版本
        with mock.patch.object(update, "is_managed", return_value=True):
            with self.assertRaises(update.UpdateError):
                update.command(self.con, SimpleNamespace(action="rollback"))


class DaemonExitTest(TicketdTestCase):
    def test_daemon_exits_with_restart_code_after_switch(self):
        from ticketd import daemon

        class FakeUpdater:
            def __init__(self, con):
                self.n = 0

            def tick(self, sched, media, healthy=True):
                self.n += 1
                return self.n >= 2

        import gc
        import warnings
        with mock.patch.object(update, "Updater", FakeUpdater), mock.patch.object(daemon.time, "sleep"), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)  # 守护进程的锁文件按设计一直开到进程退出
            self.assertEqual(daemon.main(), update.EXIT_RESTART)
            gc.collect()


if __name__ == "__main__":
    unittest.main()


class AdoptTest(TicketdTestCase):
    """adopt 把一份就地运行的克隆转成受管安装，同时改写 bridge 工作区 CLAUDE.md 的 ticket 路径；--undo 全部改回。"""

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        files = ["bin/ticketd", "bin/ticket", "scripts/install.sh", "workspace/CLAUDE.md.tmpl", "prompts/worker.md"]
        files += ["ticketd/%s" % p.name for p in (REPO / "ticketd").iterdir() if p.is_file() and p.suffix in (".py", ".json")]
        for f in files:
            dst = self.clone / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((REPO / f).read_bytes())
            dst.chmod((REPO / f).stat().st_mode & 0o777)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.com")
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
            subprocess.run(["git", "-C", str(self.clone)] + cmd, check=True, capture_output=True, env=env)
        (self.clone / "local-notes.txt").write_text("not tracked")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        tmpl = (REPO / "workspace" / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        self.original = tmpl.replace("@LARKYARD@", str(self.clone)) + "\n手写的一段，不能丢\n"
        (self.ws / "CLAUDE.md").write_text(self.original, encoding="utf-8")
        self.installs = []
        self.svc_file = self.tmp / "dev.larkyard.ticketd.plist"
        sf = mock.patch.object(update, "service_file", lambda label: self.tmp / ("%s.plist" % label))
        sf.start()
        self.addCleanup(sf.stop)

    def fake_install(self, root, app, svc=None):
        self.installs.append((Path(root), app))
        self.svcs = getattr(self, "svcs", []) + [svc]

    def test_adopt_and_undo(self):
        from ticketd import __version__
        out = update.adopt(str(self.ws), repo=self.clone, run_install=self.fake_install)
        vd = update.versions_dir() / __version__
        self.assertTrue((vd / "bin" / "ticketd").is_file())
        self.assertFalse((vd / "local-notes.txt").exists())  # 只收被跟踪的文件
        self.assertFalse((vd / ".git").exists())
        self.assertTrue(os.access(str(vd / "bin" / "ticketd"), os.X_OK))
        self.assertEqual(update.current_version(), __version__)
        self.assertTrue((update.app_dir() / "launcher.py").is_file())
        self.assertTrue(update.is_managed(vd))
        self.assertEqual(self.installs, [(vd, update.app_dir())])
        text = (self.ws / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("%s/bin/ticket" % update.current_link(), text)
        self.assertNotIn("%s/bin/ticket" % self.clone, text)
        self.assertIn("手写的一段，不能丢", text)
        self.assertTrue(list(self.ws.glob("CLAUDE.md.bak-*")))
        self.assertIn("主会话约定", "\n".join(out))
        st = update.read_state()
        self.assertEqual((st["status"], st["adopted_from"]), ("confirmed", str(self.clone)))
        # 主会话的 ticket 通过 current 能跑（删掉克隆后照常）
        shutil.move(str(self.clone), str(self.tmp / "clone-moved"))
        p = subprocess.run([str(update.current_link() / "bin" / "ticket"), "--help"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        shutil.move(str(self.tmp / "clone-moved"), str(self.clone))
        with self.assertRaises(update.UpdateError):
            update.adopt(str(self.ws), repo=vd, run_install=self.fake_install)  # 已经是受管安装

        out = update.undo_adopt(run_install=self.fake_install)
        self.assertEqual(self.installs[-1], (self.clone, None))
        self.assertEqual((self.ws / "CLAUDE.md").read_text(encoding="utf-8"), self.original)
        self.assertFalse(update.current_link().exists())
        self.assertFalse((update.app_dir() / "launcher.py").exists())
        self.assertEqual(update.read_state()["status"], "unadopted")
        with self.assertRaises(update.UpdateError):
            update.undo_adopt(run_install=self.fake_install)

    def test_adopt_refuses_dirty_clone(self):
        (self.clone / "bin" / "ticket").write_text("changed")
        with self.assertRaises(update.UpdateError):
            update.adopt(str(self.ws), repo=self.clone, run_install=self.fake_install)
        self.assertEqual((self.ws / "CLAUDE.md").read_text(encoding="utf-8"), self.original)
        self.assertEqual(self.installs, [])

    def test_failed_install_reverts_workspace(self):
        calls = []

        def boom(root, app, svc=None):
            calls.append((Path(root), app, svc))
            if app is not None:
                raise update.UpdateError("install.sh 失败")
        with self.assertRaises(update.UpdateError) as cm:
            update.adopt(str(self.ws), repo=self.clone, run_install=boom)
        self.assertEqual((self.ws / "CLAUDE.md").read_text(encoding="utf-8"), self.original)
        self.assertFalse(update.current_link().exists())
        # install.sh 可能已经卸掉了旧服务：按原样从克隆重装一次
        self.assertEqual([(c[0], c[1]) for c in calls][-1], (self.clone, None))
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertIn("已按原样", str(cm.exception))

    def write_plist(self, label, env):
        import plistlib
        (self.tmp / ("%s.plist" % label)).write_bytes(plistlib.dumps({"Label": label, "EnvironmentVariables": env}))

    def test_adopt_keeps_existing_service_label_and_env(self):
        """自定义了服务名、bridge profile 的安装：adopt / --undo 重装的是同一个服务、同样的环境，不按当前 shell 的默认值。"""
        os.environ["TICKETD_LABEL"] = "dev.larkyard.ticketd.b"
        self.write_plist("dev.larkyard.ticketd.b", {
            "HOME": "/x", "TICKETD_HOME": os.environ["TICKETD_HOME"], "LARK_CHANNEL_HOME": "/lc",
            "LARK_CHANNEL_PROFILE": "codex", "PATH": "/bin"})
        os.environ.pop("LARK_CHANNEL_HOME", None)
        update.adopt(str(self.ws), repo=self.clone, run_install=self.fake_install)
        want = {"label": "dev.larkyard.ticketd.b", "env": {
            "TICKETD_HOME": os.environ["TICKETD_HOME"], "LARK_CHANNEL_HOME": "/lc", "LARK_CHANNEL_PROFILE": "codex"}}
        self.assertEqual(self.svcs[-1], want)
        os.environ.pop("TICKETD_LABEL")  # 退回时 shell 里没设也要装回同一个服务
        update.undo_adopt(run_install=self.fake_install)
        self.assertEqual(self.svcs[-1], want)

    def test_adopt_refuses_service_with_other_home(self):
        self.write_plist("dev.larkyard.ticketd", {"TICKETD_HOME": str(self.tmp / "other")})
        with self.assertRaises(update.UpdateError) as cm:
            update.adopt(str(self.ws), repo=self.clone, run_install=self.fake_install)
        self.assertIn("数据目录", str(cm.exception))
        self.assertEqual(self.installs, [])
        self.assertFalse(update.current_link().exists())

    def test_systemd_unit_env_is_read(self):
        unit = self.tmp / "x.service"
        unit.write_text('[Service]\nEnvironment="TICKETD_HOME=/h"\nEnvironment="LARK_CHANNEL_PROFILE=p"\n'
                        'Environment="TICKETD_PYTHON=/opt/py"\nEnvironment="PATH=/bin"\n')
        self.assertEqual(update.read_service_env(unit),
                         {"TICKETD_HOME": "/h", "LARK_CHANNEL_PROFILE": "p", "TICKETD_PYTHON": "/opt/py"})
        self.assertEqual(update.read_service_env(self.tmp / "missing.service"), {})

    def test_undo_warns_when_clone_is_older(self):
        update.adopt(str(self.ws), repo=self.clone, run_install=self.fake_install)
        init = self.clone / "ticketd" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace(
            update.package_version(self.clone), "0.0.1"), encoding="utf-8")
        out = "\n".join(update.undo_adopt(run_install=self.fake_install))
        self.assertIn("比正在运行的", out)

    def test_adopt_refused_while_busy(self):
        from types import SimpleNamespace
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET state='running' WHERE id=?", (tid,))
        with self.assertRaises(update.UpdateError) as cm:
            update.command(self.con, SimpleNamespace(action="adopt", undo=False), workspace=str(self.ws))
        self.assertIn("不空闲", str(cm.exception))

    def test_install_sh_gets_app_env(self):
        seen = {}

        def fake_run(cmd, env=None, **kw):
            seen["cmd"], seen["app"], seen["env"] = cmd, env.get("TICKETD_APP"), env
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(update.subprocess, "run", fake_run):
            update._install_sh(self.clone, update.app_dir(), {"label": "l.b", "env": {"LARK_CHANNEL_PROFILE": "p"}})
            self.assertEqual(seen["app"], str(update.app_dir()))
            self.assertEqual((seen["env"]["TICKETD_LABEL"], seen["env"]["LARK_CHANNEL_PROFILE"]), ("l.b", "p"))
            os.environ["TICKETD_APP"] = "/leftover"
            update._install_sh(self.clone, None)
            self.assertIsNone(seen["app"])
        self.assertEqual(seen["cmd"][1], str(self.clone / "scripts" / "install.sh"))


class ServiceFileTest(TicketdTestCase):
    def test_service_file_per_platform(self):
        from ticketd import host
        with mock.patch.object(host, "system", return_value="darwin"):
            self.assertEqual(update.service_file("l.b"), Path.home() / "Library" / "LaunchAgents" / "l.b.plist")
        os.environ["XDG_CONFIG_HOME"] = "/cfg"
        with mock.patch.object(host, "system", return_value="linux"):
            self.assertEqual(update.service_file("l.b"), Path("/cfg/systemd/user/l.b.service"))

    def test_capture_service_without_service_file(self):
        os.environ["TICKETD_LABEL"] = "dev.larkyard.ticketd.none-%d" % os.getpid()
        os.environ["LARK_CHANNEL_PROFILE"] = "p"
        label, env = update.capture_service()
        self.assertEqual(label, os.environ["TICKETD_LABEL"])
        self.assertEqual((env["TICKETD_HOME"], env["LARK_CHANNEL_PROFILE"]), (os.environ["TICKETD_HOME"], "p"))


class DoctorWorkspaceTest(TicketdTestCase):
    def test_doctor_expects_current_link_when_managed(self):
        from ticketd import cli
        ws = self.tmp / "ws"
        ws.mkdir()
        os.environ["TICKETD_WORKSPACE"] = str(ws)
        tmpl = (REPO / "workspace" / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
        (ws / "CLAUDE.md").write_text(tmpl.replace("@LARKYARD@", str(update.current_link())), encoding="utf-8")
        with mock.patch.object(update, "is_managed", return_value=True):
            self.assertTrue(cli._workspace_drift()[0].startswith("✅"))
        self.assertTrue(cli._workspace_drift()[0].startswith("❌"))

"""空闲换新（T0112）：从 bridge 日志判断空闲、守护进程的后台换新、互斥锁、换到一半的补救。
两个平台都跑一遍（mock host.system()）；服务管理器和在线确认用假的，不碰真实 launchctl / systemctl。"""
from __future__ import annotations

import functools
import json
import os
import time
from pathlib import Path

from test_bridge import BridgeTestBase, FakeServices

from ticketd import bridge, bridgeidle, update


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime(t))


class LogMixin:
    def logs(self) -> Path:
        d = Path(os.environ["LARK_CHANNEL_HOME"]) / "profiles" / "claude" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log(self, t: float, phase: str, event: str, **kw) -> None:
        f = self.logs() / ("bridge-%s.jsonl" % time.strftime("%Y%m%d", time.localtime(t)))
        with open(str(f), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict({"ts": stamp(t), "level": "info", "phase": phase, "event": event}, **kw)) + "\n")


class ActivityTest(LogMixin, BridgeTestBase):
    def test_no_logs(self):
        self.assertEqual(bridge.activity("claude"), {"logs": False, "busy": False, "unknown": False, "last": None})

    def test_pool_counts_and_noise(self):
        now = time.time()
        self.log(now - 600, "supervisor", "profile-online")
        self.log(now - 500, "intake", "enter")
        self.log(now - 499, "pool", "acquired", active=1, cap=10)
        a = bridge.activity("claude", now=now)
        self.assertTrue(a["busy"])
        self.log(now - 400, "pool", "released", active=0)
        for i in range(20):  # 一直在刷的噪声：不算有人在用
            self.log(now - 300 + i, "sdk", "error")
            self.log(now - 300 + i, "meeting", "poll-failed")
            self.log(now - 300 + i, "keepalive", "ws-stuck")
        a = bridge.activity("claude", now=now)
        self.assertTrue(a["logs"])
        self.assertFalse(a["busy"])
        self.assertAlmostEqual(a["last"], int(now - 400), delta=1)

    def test_restart_resets_pool(self):
        now = time.time()
        self.log(now - 900, "pool", "acquired", active=1)  # 上一个进程没报 released 就被杀了
        self.log(now - 600, "supervisor", "profile-online")
        self.assertFalse(bridge.activity("claude", now=now)["busy"])

    def test_run_started_just_before_profile_online_is_kept(self):
        """profile-online 记在长连接连上之后：补投的消息可能先开了会话，计数不能被清掉。"""
        now = time.time()
        self.log(now - 7200, "pool", "acquired", active=1)
        self.log(now - 7200 + 5, "supervisor", "profile-online")
        self.assertTrue(bridge.activity("claude", now=now)["busy"])

    def test_quiet_days_use_the_latest_files(self):
        now = time.time()
        self.log(now - 5 * 86400, "pool", "released", active=0)  # 之后几天一行都没写
        a = bridge.activity("claude", now=now)
        self.assertTrue(a["logs"])
        self.assertFalse(a["busy"])
        self.assertFalse(a["unknown"])

    def test_truncated_window_without_pool_events_is_unknown(self):
        from unittest import mock
        now = time.time()
        self.log(now - 3000, "pool", "acquired", active=1)
        for i in range(200):
            self.log(now - 2000 + i, "sdk", "error", msg="x" * 100)
        with mock.patch("ticketd.bridge.ACTIVITY_SCAN", 4096):
            a = bridge.activity("claude", now=now)
        self.assertTrue(a["unknown"])  # 开始的那条被挤出读的范围：不能当空闲
        self.assertFalse(bridge.activity("claude", now=now)["unknown"])

    def test_reads_yesterday_too(self):
        now = time.time()
        self.log(now - 86400, "pool", "acquired", active=2)
        a = bridge.activity("claude", now=now)
        self.assertTrue(a["logs"])
        self.assertTrue(a["busy"])  # 跨过零点还在跑的会话


class IdleSwapMixin(LogMixin):
    def setUp(self):
        super().setUp()
        self.v1 = self.make_current("0.3.0", commit="1" * 40)
        bridge.ensure_runtime(self.v1, plat=self.plat(), fetch=self.fetch)
        bridge.link_bridge(self.v1)
        self.sf = bridge.bridge_service_file("claude")
        bridge._write(self.sf, bridge.render_bridge_service("claude", bridge.service_path()))
        self.svc = FakeServices()
        from unittest import mock
        p = mock.patch("ticketd.bridge.platform_key", return_value=self.plat())
        p.start()
        self.addCleanup(p.stop)
        self.online = True
        self.sent = []
        self.now = time.time()
        os.environ["LARK_CHANNEL"] = "1"  # ticketd 服务的环境里就有（launchd / systemd 模板）

    def call_restart(self, **kw):
        return self.restart_fn()("claude", if_stale=True, in_daemon=True, **kw)

    def restart_fn(self):
        return functools.partial(bridge.restart, services=self.svc, fetch=self.fetch,
                                 wait=lambda *a, **k: self.online)

    def lane(self):
        return bridgeidle.Lane(clock=lambda: self.now, restart=self.restart_fn(), notify=self.sent.append,
                               log=lambda *a, **k: None)

    def run_lane(self, lane) -> bool:
        started = lane.tick()
        if lane.thread is not None:
            lane.thread.join(10)
        return started

    def quiet(self, minutes=20):
        self.log(self.now - minutes * 60, "pool", "released", active=0)

    def test_nothing_pending(self):
        self.quiet()
        self.assertEqual(bridgeidle.due(self.now)[0], "没有待生效的 bridge")
        self.assertFalse(self.run_lane(self.lane()))
        self.assertEqual(self.svc.calls, [])

    def test_swaps_when_idle_and_not_when_busy_or_recent(self):
        self.make_current("0.4.0", commit="2" * 40)
        self.log(self.now - 3600, "pool", "acquired", active=1)
        self.assertIn("主会话在跑", bridgeidle.due(self.now)[0])
        self.log(self.now - 300, "pool", "released", active=0)
        self.assertIn("有人用过", bridgeidle.due(self.now)[0])
        self.now += 15 * 60
        lane = self.lane()
        self.assertTrue(self.run_lane(lane))
        self.assertEqual(bridge.bridge_version_dir(), "0.4.0")
        self.assertEqual(self.svc.calls, [("restart", bridge.bridge_label("claude"))])
        st = bridge.read_state()
        self.assertEqual(st["last_restart"]["how"], "idle")
        self.assertTrue(st["last_restart"]["ok"])
        self.assertTrue(st["idle_attempt"]["ok"])
        self.assertNotIn("switching", st)
        self.assertTrue(any("空闲时自动换新" in x for x in bridge.status_lines("claude")))
        self.assertEqual(bridgeidle.due(self.now)[0], "没有待生效的 bridge")

    def test_checks_every_five_minutes(self):
        self.quiet()
        lane = self.lane()
        self.assertFalse(self.run_lane(lane))
        self.make_current("0.4.0", commit="2" * 40)
        self.now += 60
        self.assertFalse(self.run_lane(lane))  # 还没到 5 分钟
        self.now += bridgeidle.CHECK_EVERY
        self.assertTrue(self.run_lane(lane))

    def test_failure_backs_off_for_a_day(self):
        self.make_current("0.4.0", commit="2" * 40)
        self.quiet()
        self.online = False
        self.assertTrue(self.run_lane(self.lane()))
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")  # 没连上：切回
        self.assertEqual(len(self.sent), 1)                      # 私信过一次
        self.assertFalse(bridge.read_state()["idle_attempt"]["ok"])
        self.assertIn("失败", bridgeidle.due(self.now + 3600)[0])
        self.online = True
        self.now += bridgeidle.FAIL_BACKOFF + 1
        self.quiet()
        self.assertEqual(bridgeidle.due(self.now)[0], "")

    def test_off_hold_probation_unmanaged_and_no_logs(self):
        self.make_current("0.4.0", commit="2" * 40)
        self.assertEqual(bridgeidle.due(self.now)[0], "读不到 bridge 日志")
        self.quiet()
        os.environ["TICKETD_BRIDGE_IDLE_MINUTES"] = "0"
        self.assertEqual(bridgeidle.due(self.now)[0], "关掉了")
        os.environ["TICKETD_BRIDGE_IDLE_MINUTES"] = "15"
        self.assertEqual(bridgeidle.due(self.now, hold=True)[0], "Larkyard 正在装更新")
        update.write_state({"status": "probation", "from": "0.3.0", "to": "0.4.0"})
        self.assertEqual(bridgeidle.due(self.now)[0], "Larkyard 新版本还在试用期")
        update.write_state({"status": "confirmed", "version": "0.4.0"})
        self.assertEqual(bridgeidle.due(self.now)[0], "")
        bridge._write(self.sf, b"<plist><dict></dict></plist>")
        self.assertEqual(bridgeidle.due(self.now)[0], "bridge 不是 Larkyard 管理的")

    def test_missing_service_env_is_also_applied_when_idle(self):
        text = self.sf.read_bytes().replace(bridge.TICKET_BIN_ENV.encode(), b"LARKYARD_OTHER")
        bridge._write(self.sf, text)
        self.quiet()
        self.assertEqual(bridgeidle.due(self.now)[2], "service")
        self.assertTrue(self.run_lane(self.lane()))
        self.assertEqual(self.svc.calls[0][0], "load")  # 服务文件重写了：重新加载，光重启读不到新环境
        self.assertFalse(bridge.service_outdated(self.sf))

    def test_check_errors_are_logged_once_not_raised(self):
        self.quiet()
        bridge.state_path().parent.mkdir(parents=True, exist_ok=True)
        logged = []
        lane = bridgeidle.Lane(clock=lambda: self.now, restart=self.restart_fn(), log=lambda *a, **k: logged.append(a))
        from unittest import mock
        with mock.patch("ticketd.bridgeidle.due", side_effect=OSError("坏了")):
            self.assertFalse(lane.tick())
            self.now += bridgeidle.CHECK_EVERY
            self.assertFalse(lane.tick())
        self.assertEqual(len(logged), 1)

    def test_one_restart_at_a_time(self):
        self.make_current("0.4.0", commit="2" * 40)
        held = bridge._restart_lock()
        try:
            with self.assertRaises(bridge.RestartBusy):
                self.call_restart()
            self.quiet()
            self.assertTrue(self.run_lane(self.lane()))  # 每天的任务正在做：空闲换新让给它，不记失败
            self.assertNotIn("idle_attempt", bridge.read_state())
        finally:
            held.close()
        self.assertEqual(self.svc.calls, [])

    def test_unfinished_switch_is_completed(self):
        """上次切了链接、没来得及重启就被打断：链接已经是新的，stale() 看不出来，靠 switching 标记补上。"""
        self.make_current("0.4.0", commit="2" * 40)
        bridge.link_bridge(update.versions_dir() / "0.4.0")
        st = bridge.read_state()
        st["switching"] = int(self.now) - 60
        bridge.write_state(st)
        self.assertIsNone(bridge.stale())
        self.quiet()
        self.assertEqual(bridgeidle.due(self.now)[2], "unfinished")
        self.assertTrue(self.run_lane(self.lane()))
        self.assertEqual([c[0] for c in self.svc.calls], ["load"])  # 被打断的那次可能改过服务文件：重新加载
        self.assertFalse(bridge.switch_unfinished())
        out = self.call_restart()
        self.assertIn("不用重启", out[0])

    def test_cli_restart_in_bridge_session_still_refused(self):
        self.make_current("0.4.0", commit="2" * 40)
        with self.assertRaises(bridge.BridgeError):
            self.restart_fn()("claude", if_stale=True)  # 主会话里（LARK_CHANNEL=1）执行会把自己杀掉
        self.assertEqual(self.svc.calls, [])
        os.environ["TICKETD_TICKET"] = "T0001"
        with self.assertRaises(bridge.BridgeError):
            self.call_restart()  # worker 里一律不行

    def test_someone_shows_up_while_runtime_downloads(self):
        from unittest import mock
        self.make_current("0.4.0", commit="2" * 40)
        self.quiet()

        def slow_runtime(*a, **k):
            self.log(self.now, "pool", "acquired", active=1)  # 下载运行时期间来了一条消息
            return ["Node 25.0.0"]
        with mock.patch("ticketd.bridge.ensure_runtime", side_effect=slow_runtime):
            self.assertTrue(self.run_lane(self.lane()))
        self.assertEqual(self.svc.calls, [])
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        st = bridge.read_state()
        self.assertNotIn("switching", st)
        self.assertNotIn("idle_attempt", st)  # 不算失败，下次空闲再换

    def test_runtime_failure_leaves_no_unfinished_mark(self):
        self.make_current("0.4.0", commit="2" * 40, runtime=self.runtime_json(node_sha="e" * 64, node_v="25.0.0"))
        self.blobs.pop("e" * 64, None)
        with self.assertRaises(bridge.BridgeError):
            self.call_restart()
        self.assertFalse(bridge.switch_unfinished())
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")


class IdleSwapDarwinTest(IdleSwapMixin, BridgeTestBase):
    platform = "darwin"


class IdleSwapLinuxTest(IdleSwapMixin, BridgeTestBase):
    platform = "linux"

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import unittest

from helpers import REPO, TicketdTestCase


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class DaemonTest(TicketdTestCase):
    def test_daemon_runs_ticket_and_sends_report(self):
        tid = self.new_ticket()
        proc = subprocess.Popen([str(REPO / "bin" / "ticketd")], env=dict(os.environ),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            end = time.time() + 20
            sent = []
            while time.time() < end:
                p = self.tmp / "lark.log"
                if p.exists():
                    sent = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
                    if any(a[:2] == ["api", "PATCH"] and "完成" in a[a.index("--data") + 1] for a in sent):
                        break
                time.sleep(0.2)
            cards = [json.loads(a[a.index("--content") + 1]) for a in sent if "interactive" in a]
            self.assertEqual(len(cards), 1, "守护进程应只发一张状态卡，结果不另发消息")
            self.assertNotIn("完成", cards[0]["header"]["subtitle"]["content"])
            patches = [json.loads(json.loads(a[a.index("--data") + 1])["content"]) for a in sent if a[:2] == ["api", "PATCH"]]
            self.assertTrue(patches, "结果没有覆盖到状态卡上")
            self.assertTrue(patches[-1]["config"]["summary"]["content"].startswith("✅ T0001 完成：已完成测试任务"))
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)
        self.assertEqual(proc.returncode, 0)

    def test_worker_prompt_mentions_contract(self):
        text = (REPO / "prompts" / "worker.md").read_text(encoding="utf-8")
        for key in ("ticket report", "dropped", "可写目录", "外部文本"):
            self.assertIn(key, text)

    def test_second_daemon_refuses_to_start(self):
        a = subprocess.Popen([str(REPO / "bin" / "ticketd")], env=dict(os.environ),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertIn(b"ticketd started", a.stdout.readline())
            b = subprocess.run([str(REPO / "bin" / "ticketd")], env=dict(os.environ),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertEqual(b.returncode, 1)
            self.assertIn(b"already running", b.stdout)
        finally:
            a.send_signal(signal.SIGTERM)
            a.wait(timeout=15)
        self.assertEqual(a.returncode, 0)
        a.stdout.close()


class ThrottleTest(unittest.TestCase):
    def test_throttle(self):
        from ticketd.daemon import _ErrorThrottle, next_sleep
        clock = _Clock()
        th = _ErrorThrottle(interval=600, clock=clock)
        self.assertIn("ValueError", th.failure("tick", ValueError("x")))
        clock.t += 10
        self.assertIsNone(th.failure("tick", ValueError("x")))
        clock.t += 100
        self.assertIsNone(th.failure("tick", ValueError("x")))
        # 另一个来源独立
        self.assertIsNotNone(th.failure("flush", KeyError("k")))
        clock.t += 600
        msg = th.failure("tick", ValueError("x"))
        self.assertIsNotNone(msg)
        self.assertIn("3", msg)  # 窗口内共重复 3 次
        # 成功后重置：再出错立即告警
        th.success("tick")
        self.assertIsNotNone(th.failure("tick", ValueError("x")))
        # 不同签名立即告警
        self.assertIsNotNone(th.failure("tick", ValueError("y")))
        # 退避
        self.assertEqual(next_sleep(0), 1.0)
        self.assertEqual(next_sleep(1), 2.0)
        self.assertEqual(next_sleep(3), 8.0)
        self.assertEqual(next_sleep(10), 30.0)
        th2 = _ErrorThrottle(clock=clock)
        self.assertEqual(th2.streak(), 0)
        th2.failure("tick", ValueError("x"))
        th2.failure("tick", ValueError("x"))
        self.assertEqual(th2.streak(), 2)
        th2.success("tick")
        self.assertFalse(th2.failing())

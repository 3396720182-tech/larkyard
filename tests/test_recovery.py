"""发件箱的恢复与路径竞态（在真实守护进程上做故障注入实测之后补的回归用例）。"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading

from helpers import TicketdTestCase
import test_send_robustness as _srt

JPG, MP4, NET, PNG, find_all = _srt.JPG, _srt.MP4, _srt.NET, _srt.PNG, _srt.find_all

GONE = ('rc=1 {"type": "api", "subtype": "unknown", "code": 230011, "message": "The message was withdrawn."}')
RATE = ('rc=1 {"type": "api", "subtype": "rate_limit", "code": 99991400, "message": "too many requests",'
        ' "retryable": true}')


class _Base(TicketdTestCase):
    """与 test_send_robustness 相同的桩（不继承那个类，免得它的用例在这里再跑一遍）。"""

    def setUp(self):
        super().setUp()
        from ticketd import core, outbox, templates
        self.core, self.outbox, self.templates = core, outbox, templates
        self._orig = (outbox.upload_image, outbox.upload_video_drive)
        self.texts, self.cards = [], []

    def tearDown(self):
        self.outbox.upload_image, self.outbox.upload_video_drive = self._orig
        super().tearDown()


for _n in ("ticket", "write", "rows", "sender", "ok_card", "tick"):
    setattr(_Base, _n, getattr(_srt.SendRobustnessTest, _n))


class ReplyTargetGoneTest(_Base):
    """被回复的消息已撤回（飞书 230011）：原来结果和兜底文字都回复同一条消息，一起失败，最后被丢弃。"""

    def test_card_redirected_to_chat_intact(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.core.enqueue_message(self.con, tid, "后面一条")
        seen = []

        def card(to, c, idem):
            seen.append(to)
            return (False, GONE, True) if to.startswith("om_") else self.ok_card(to, c, idem)

        self.tick(card_sender=card)
        r = self.rows()
        self.assertEqual(seen, ["om_1", "oc_1"])
        self.assertEqual((r[0]["status"], r[0]["reply_to"]), ("sent", "oc_1"))
        self.assertEqual(len(self.cards), 1)  # 卡片原样改投，没有退成文字
        self.assertEqual(self.texts, [])
        self.assertEqual(r[1]["reply_to"], "oc_1")  # 同单还没发的消息也一起改投

    def test_text_redirected(self):
        tid, wd = self.ticket()
        self.core.enqueue_message(self.con, tid, "纯文字")
        sent = []

        def text(to, t, idem):
            sent.append(to)
            return (False, GONE, True) if to.startswith("om_") else (True, "om_x", False)

        self.outbox.flush(self.con, sender=text, card_sender=self.ok_card)
        self.assertEqual(sent, ["om_1", "oc_1"])
        self.assertEqual(self.rows()[0]["status"], "sent")

    def test_real_lark_command_switches_to_messages_send(self):
        self.assertEqual(self.outbox._target_args("oc_9")[:2], ["im", "+messages-send"])
        self.assertIn("--chat-id", self.outbox._target_args("oc_9"))
        self.assertEqual(self.outbox._target_args("om_9")[:2], ["im", "+messages-reply"])

    def test_status_card_redirected(self):
        from ticketd import card
        tid = self.new_ticket(message="om_s")
        made = []

        def create(to, c, idem):
            made.append(to)
            return (False, GONE, True) if to.startswith("om_") else (True, "om_card", False)

        card.refresh(self.con, create, lambda *a: (True, "", False))
        self.assertEqual(made, ["om_s", "oc_1"])
        self.assertEqual(self.core.get(self.con, tid)["card_message_id"], "om_card")


class ClassificationTest(_Base):
    def test_rate_limit_is_retried_not_text_fallback(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.tick(card_sender=lambda *a: (False, RATE, False))
        r = self.rows()[0]
        self.assertEqual((r["status"], r["card_json"] not in (None, "")), ("pending", True))
        self.assertEqual(self.texts, [])

    def test_non_utf8_lark_output_does_not_raise(self):
        fake = os.path.join(tempfile.mkdtemp(), "lark")
        with open(fake, "w") as f:
            f.write("#!%s\nimport sys\nsys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n" % sys.executable)
        os.chmod(fake, stat.S_IRWXU)
        ok, info, permanent = self.outbox._run_lark([fake])
        self.assertTrue(ok)  # rc=0 且输出无法解析：按已发送处理（原来这里抛 UnicodeDecodeError，整行 dead）


class PoisonRowTest(_Base):
    def test_exception_after_upload_still_delivers_text(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)

        def boom(path):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        self.outbox.upload_image = boom
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png"])
        self.tick()
        r = self.rows()[0]
        self.assertEqual(r["status"], "sent")
        self.assertEqual(len(self.texts), 1)
        self.assertIn("图好了", self.texts[0][0])


class DegradeTimingTest(_Base):
    def test_short_outage_never_degrades_even_after_many_attempts(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)
        state = {"down": True}
        self.outbox.upload_image = lambda p: (False, NET, False) if state["down"] else (True, "img_ok", False)
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png"])
        for _ in range(self.outbox.MEDIA_MAX_ATTEMPTS + 2):
            self.tick()
        self.assertEqual(self.cards, [])  # 还不到 10 分钟：不降级，继续等
        state["down"] = False
        self.tick()
        self.assertEqual([i["img_key"] for i in find_all(self.cards[0], "img")], ["img_ok"])


class BudgetTest(_Base):
    def test_slow_uploads_yield_and_resume(self):
        tid, wd = self.ticket()
        for n in range(1, 5):
            self.write(os.path.join(wd, "p%d.png" % n), PNG)
        clock = {"t": 0.0}
        self.outbox._clock = lambda: clock["t"]
        self.addCleanup(setattr, self.outbox, "_clock", __import__("time").monotonic)
        calls = []

        def slow(path):
            calls.append(path)
            clock["t"] += 40  # 每张 40 秒：预算 60 秒，一个 tick 只开始 2 张
            return True, "img_%d" % len(calls), False

        self.outbox.upload_image = slow
        self.core.add_report(self.con, tid, "ready", "四张", ["p1.png", "p2.png", "p3.png", "p4.png"])
        self.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.cards, [])
        self.assertEqual(self.rows()[0]["card_json"].count("ticketd_upload"), 2)  # 进度写回了
        r = self.rows()[0]
        self.assertEqual((r["attempts"], r["next_at"]), (0, 0))  # 预算用完不算失败、不退避
        self.tick()
        self.assertEqual(len(calls), 4)  # 不重传前两张
        self.assertEqual(len(find_all(self.cards[0], "img")), 4)
        self.assertNotIn("未能显示", json.dumps(self.cards[0], ensure_ascii=False))


class DriveReuseTest(_Base):
    def test_same_video_reported_twice_uploads_once(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        self.outbox.upload_image = lambda p: (True, "img_cover", False)
        drive = []
        self.outbox.upload_video_drive = lambda v, w, t: (drive.append(v), (True, "https://a.feishu.cn/file/x"))[1]
        self.core.add_report(self.con, tid, "ready", "第一版", ["v.mp4"])
        self.tick()
        self.core.add_report(self.con, tid, "ready", "再报一次", ["v.mp4"])
        self.tick()
        self.assertEqual(len(drive), 1)
        self.assertEqual(len(self.cards), 2)
        for c in self.cards:
            box = find_all(c, "interactive_container")[0]
            self.assertEqual(box["behaviors"][0]["default_url"], "https://a.feishu.cn/file/x")

    def test_changed_video_uploads_again(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        drive = []
        self.outbox.upload_video_drive = lambda v, w, t: (drive.append(v), (True, "https://a.feishu.cn/file/%d" % len(drive)))[1]
        self.core.add_report(self.con, tid, "ready", "第一版", ["v.mp4"])
        self.tick()
        self.write(os.path.join(wd, "v.mp4"), MP4 + b"changed")
        self.core.add_report(self.con, tid, "ready", "改过了", ["v.mp4"])
        self.tick()
        self.assertEqual(len(drive), 2)

    def test_drive_limit_and_timeout(self):
        self.assertGreaterEqual(self.outbox.MAX_DRIVE_VIDEO_BYTES, 100 * 1024 * 1024)
        self.assertEqual(self.outbox._drive_timeout(5 * 1024 * 1024), self.outbox.VIDEO_TIMEOUT)
        self.assertGreater(self.outbox._drive_timeout(100 * 1024 * 1024), self.outbox.VIDEO_TIMEOUT + 200)


class ReviewFollowupTest(_Base):
    """修复后的独立审查提出的几处。"""

    def test_video_message_row_waits_before_text_fallback(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.con.execute("INSERT INTO outbox(ticket_id, reply_to, text, media_json, created_at) VALUES(?,?,?,?,?)",
                         (tid, "om_1", "视频兜底文字", json.dumps({"video": "v.mp4"}), self.core.db.now()))
        sent = []
        orig = self.outbox.send_video
        self.outbox.send_video = lambda *a: (sent.append(a), (False, NET, False))[1]
        self.addCleanup(setattr, self.outbox, "send_video", orig)
        for _ in range(self.outbox.VIDEO_MAX_ATTEMPTS + 2):
            self.tick()
        self.assertEqual(self.texts, [])  # 不到 10 分钟：继续重试视频，不改发文字
        self.assertEqual(len(sent), self.outbox.VIDEO_MAX_ATTEMPTS + 2)

    def test_poison_text_fallback_is_bound_for_followups(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])

        def boom(*a):
            raise RuntimeError("boom")

        self.tick(card_sender=boom)
        self.assertEqual(self.rows()[0]["status"], "sent")
        self.assertEqual(self.core.lookup(self.con, "om_t1"), tid)

    def test_db_lock_is_retried_not_poison(self):
        import sqlite3
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])

        def locked(*a):
            raise sqlite3.OperationalError("database is locked")

        self.tick(card_sender=locked)
        r = self.rows()[0]
        self.assertEqual(r["status"], "pending")  # 卡片还在，稍后重试
        self.assertNotEqual(r["card_json"], "")
        self.assertEqual(self.texts, [])
        self.tick()
        self.assertEqual(len(self.cards), 1)


class PathRaceTest(TicketdTestCase):
    """目录组件被并发替换：读到的要么是工单目录里的文件，要么失败，绝不能是外面的文件。"""

    def setUp(self):
        super().setUp()
        from ticketd import outbox, safefs
        self.outbox, self.safefs = outbox, safefs
        self.root = os.path.realpath(str(self.tmp / "work" / "T1"))
        os.makedirs(os.path.join(self.root, "d"))
        self.outside = str(self.tmp / "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.root, "d", "a.png"), "wb") as f:
            f.write(PNG + b"INSIDE")
        with open(os.path.join(self.outside, "a.png"), "wb") as f:
            f.write(PNG + b"OUTSIDE")

    def test_static_intermediate_symlink_to_outside_rejected(self):
        os.symlink(self.outside, os.path.join(self.root, "x"))
        with self.assertRaises(OSError):
            self.outbox._read_workdir_file("x/a.png", self.root, 1 << 20)

    def test_symlink_inside_root_still_allowed(self):
        os.symlink(os.path.join(self.root, "d"), os.path.join(self.root, "latest"))
        self.assertTrue(self.outbox._read_workdir_file("latest/a.png", self.root, 1 << 20).endswith(b"INSIDE"))

    def test_last_component_symlink_rejected(self):
        os.symlink(os.path.join(self.root, "d", "a.png"), os.path.join(self.root, "link.png"))
        with self.assertRaises(OSError):
            self.outbox._read_workdir_file("link.png", self.root, 1 << 20)

    def test_open_read_under_refuses_swapped_component(self):
        # 检查之后、打开之前把 d 换成指向外面的符号链接（逐级打开的那一刻看到的就是符号链接）
        os.rename(os.path.join(self.root, "d"), os.path.join(self.root, "d.real"))
        os.symlink(self.outside, os.path.join(self.root, "d"))
        with self.assertRaises(OSError):
            self.safefs.open_read_under(self.root, "d/a.png")
        with self.assertRaises(OSError):
            self.safefs.open_read_under(self.root, "../outside/a.png")

    def test_concurrent_swap_never_reads_outside_even_without_fd_path(self):
        # _fd_path 拿不到路径（如 Linux 上 /proc 不可用）时，原来的事后核对会被跳过：逐级打开必须独自守住
        orig = self.outbox._fd_path
        self.outbox._fd_path = lambda fd: None
        self.addCleanup(setattr, self.outbox, "_fd_path", orig)
        d, aside = os.path.join(self.root, "d"), os.path.join(self.root, "d.aside")
        stop = threading.Event()

        def flip():
            while not stop.is_set():
                try:
                    os.rename(d, aside)
                    os.symlink(self.outside, d)
                    os.unlink(d)
                    os.rename(aside, d)
                except OSError:
                    pass

        th = threading.Thread(target=flip)
        th.start()
        got = {"inside": 0, "outside": 0, "error": 0}
        try:
            for _ in range(3000):
                try:
                    data = self.outbox._read_workdir_file("d/a.png", self.root, 1 << 20)
                    got["outside" if data.endswith(b"OUTSIDE") else "inside"] += 1
                except OSError:
                    got["error"] += 1
        finally:
            stop.set()
            th.join()
        self.assertEqual(got["outside"], 0, got)

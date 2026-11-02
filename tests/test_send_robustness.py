"""发送链路的健壮性（T0012 对抗审查 F1/F2，复现用例改写成「正确行为」的断言）。"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

from helpers import TicketdTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 64
NET = "调用 lark-cli 失败：Command timed out after 30 seconds"
API_REJECT = 'rc=1 {"type": "api", "subtype": "unknown", "code": 230099, "message": "Failed to create card content"}'


def find_all(obj, tag):
    out = []
    if isinstance(obj, dict):
        if obj.get("tag") == tag:
            out.append(obj)
        for v in obj.values():
            out += find_all(v, tag)
    elif isinstance(obj, list):
        for v in obj:
            out += find_all(v, tag)
    return out


class SendRobustnessTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, outbox, templates
        self.core, self.outbox, self.templates = core, outbox, templates
        self._orig = (outbox.upload_image, outbox.upload_video_drive)
        self.texts, self.cards = [], []

    def tearDown(self):
        self.outbox.upload_image, self.outbox.upload_video_drive = self._orig
        super().tearDown()

    def ticket(self):
        tid = self.new_ticket()
        self.con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
        wd = self.con.execute("SELECT workdir FROM tickets WHERE id=?", (tid,)).fetchone()["workdir"]
        os.makedirs(wd, exist_ok=True)
        return tid, wd

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def rows(self):
        return self.con.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def sender(self, reply_to, text, idem):
        self.texts.append((text, idem))
        return True, "om_t%d" % len(self.texts), False

    def ok_card(self, reply_to, card, idem):
        self.cards.append(card)
        return True, "om_c%d" % len(self.cards), False

    def tick(self, card_sender=None):
        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con, sender=self.sender, card_sender=card_sender or self.ok_card)

    # ---------- F1：飞书拒收卡片 ----------
    def test_status_card_overwrite_rejected_falls_back_to_new_card(self):
        tid, wd = self.ticket()
        self.con.execute("UPDATE tickets SET card_wanted=1, card_message_id='om_card', card_sent_at=?, card_closed=0"
                         " WHERE id=?", (self.core.db.now(), tid))
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        patches = []

        def reject(mid, card):
            patches.append(mid)
            return False, API_REJECT, False  # 业务错误但不在永久错误码白名单里

        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con, sender=self.sender, card_sender=self.ok_card, card_patcher=reject)
        self.assertEqual(patches, ["om_card"])
        self.assertEqual(len(self.cards), 1)  # 不在状态卡上反复重试，马上改发一张新结果卡
        row = self.rows()[0]
        self.assertEqual((row["status"], row["sent_message_id"]), ("sent", "om_c1"))

    def test_real_lark_api_error_is_reported_with_type_api(self):
        fake = os.path.join(tempfile.mkdtemp(), "lark")
        with open(fake, "w") as f:
            f.write("#!%s\nimport sys, json\nsys.stdout.write(json.dumps({'ok': False, 'error': {'type': 'api', "
                    "'subtype': 'unknown', 'code': 230099, 'message': 'Failed to create card content'}}))\n"
                    "sys.exit(1)\n" % sys.executable)
        os.chmod(fake, stat.S_IRWXU)
        ok, info, permanent = self.outbox._run_lark([fake])
        self.assertFalse(ok)
        self.assertTrue(self.outbox._is_api_error(info))

    def test_any_api_rejection_falls_back_to_text_immediately(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.core.enqueue_message(self.con, tid, "后面的一条消息")
        self.tick(card_sender=lambda *a: (False, API_REJECT, False))  # 码不在白名单、也没有 retryable 字段
        r = self.rows()[0]
        self.assertEqual((r["status"], r["card_json"]), ("sent", ""))
        self.assertEqual(len(self.texts), 1)
        self.assertTrue(self.texts[0][1].endswith("-t"))
        self.assertIn("做好了", self.texts[0][0])
        self.tick()
        self.assertEqual([x["status"] for x in self.rows()], ["sent", "sent"])  # 后面的消息没被挡住

    def test_network_card_failure_still_retries(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.tick(card_sender=lambda *a: (False, NET, False))
        r = self.rows()[0]
        self.assertEqual((r["status"], r["attempts"]), ("pending", 1))
        self.assertEqual(self.texts, [])

    def test_card_row_text_fallback_before_dead_after_24h(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "做好了", [])
        self.con.execute("UPDATE outbox SET created_at=created_at-25*3600")
        self.tick(card_sender=lambda *a: (False, NET, False))
        self.assertEqual(self.rows()[0]["status"], "sent")
        self.assertEqual(len(self.texts), 1)

    # ---------- F2：上传瞬时失败不永久降级 ----------
    def test_transient_image_failure_retried_and_partial_progress_kept(self):
        tid, wd = self.ticket()
        for n in (1, 2, 3):
            self.write(os.path.join(wd, "预览-%d.png" % n), PNG)
        calls = []

        def flaky(path):
            calls.append(path)
            return (False, NET, False) if len(calls) == 2 else (True, "img_%d" % len(calls), False)

        self.outbox.upload_image = flaky
        self.core.add_report(self.con, tid, "blocked", "三张图好了", ["预览-1.png", "预览-2.png", "预览-3.png"], ask="选？")
        self.tick()
        r = self.rows()[0]
        self.assertEqual((r["status"], r["attempts"]), ("pending", 1))  # 卡片这次不发
        self.assertEqual(len(calls), 2)  # 第 2 张网络失败：第 3 张不再试（不拖住守护进程）
        self.assertIn("img_1", r["card_json"])  # 已成功的写回，重试不重传
        self.assertEqual(r["card_json"].count("ticketd_upload"), 2)
        self.assertEqual(self.cards, [])
        self.tick()
        self.assertEqual(len(calls), 4)  # 只补传图 2、图 3
        final = self.cards[-1]
        self.assertEqual([i["img_key"] for i in find_all(final, "img")], ["img_1", "img_3", "img_4"])
        self.assertNotIn("未能显示", json.dumps(final, ensure_ascii=False))
        self.assertEqual(self.rows()[0]["status"], "sent")

    def test_image_degrades_after_max_attempts(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "a.png"), PNG)
        self.outbox.upload_image = lambda p: (False, NET, False)
        self.core.add_report(self.con, tid, "ready", "图好了", ["a.png"])
        for _ in range(self.outbox.MEDIA_MAX_ATTEMPTS):
            self.tick()
        self.assertEqual(self.cards, [])
        self.tick()  # 第 MEDIA_MAX_ATTEMPTS+1 次：降级成灰字，卡片照发
        self.assertEqual(len(self.cards), 1)
        self.assertIn("未能显示", json.dumps(self.cards[0], ensure_ascii=False))
        self.assertEqual(self.rows()[0]["status"], "sent")

    def test_bad_file_degrades_immediately(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "fake.png"), b"not an image")
        self.core.add_report(self.con, tid, "ready", "图好了", ["fake.png"])
        self.tick()
        self.assertEqual(len(self.cards), 1)  # 文件本身不合格：重试也不会好，直接降级发出
        self.assertIn("未能显示", json.dumps(self.cards[0], ensure_ascii=False))

    def test_transient_drive_failure_retried_not_degraded(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        self.outbox.upload_image = lambda p: (True, "img_cover", False)
        drive = []

        def flaky_drive(v, w, t):
            drive.append(v)
            return (False, "上传云空间失败：" + NET) if len(drive) == 1 else (True, "https://a.feishu.cn/file/x")

        self.outbox.upload_video_drive = flaky_drive
        self.core.add_report(self.con, tid, "ready", "视频好了", ["v.mp4"])
        self.tick()
        rows = self.rows()
        self.assertEqual(len(rows), 1)  # 没有补排视频消息
        self.assertIn("ticketd_video", rows[0]["card_json"])  # 占位保留
        self.assertNotIn("视频在下一条消息", rows[0]["card_json"])
        self.tick()
        self.assertEqual(len(drive), 2)
        box = find_all(self.cards[-1], "interactive_container")[0]
        self.assertEqual(box["behaviors"][0]["default_url"], "https://a.feishu.cn/file/x")
        self.assertEqual(len(self.rows()), 1)

    def test_drive_ok_but_cover_transient_does_not_reupload_video(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        covers = []
        self.outbox.upload_image = lambda p: (covers.append(p), (False, NET, False) if len(covers) == 1
                                              else (True, "img_cover", False))[1]
        drive = []
        self.outbox.upload_video_drive = lambda v, w, t: (drive.append(v), (True, "https://a.feishu.cn/file/x"))[1]
        self.core.add_report(self.con, tid, "ready", "视频好了", ["v.mp4"])
        self.tick()
        self.assertIn("https://a.feishu.cn/file/x", self.rows()[0]["card_json"])  # 链接记在占位上
        self.tick()
        self.assertEqual(len(drive), 1)  # 视频没有重传
        box = find_all(self.cards[-1], "interactive_container")[0]
        self.assertEqual(box["elements"][0]["img_key"], "img_cover")
        self.assertNotIn("ticketd_video", json.dumps(self.cards[-1]))

    def test_drive_degrades_after_max_attempts(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.outbox.upload_video_drive = lambda v, w, t: (False, "上传云空间失败：" + NET)
        self.core.add_report(self.con, tid, "ready", "视频好了", ["v.mp4"])
        for _ in range(self.outbox.MEDIA_MAX_ATTEMPTS + 1):
            self.tick()
        self.assertEqual(len(self.cards), 1)
        self.assertIn("视频在下一条消息", json.dumps(self.cards[0], ensure_ascii=False))
        rows = self.rows()
        self.assertEqual(json.loads(rows[1]["media_json"])["video"], "v.mp4")  # 这时才补排视频消息

    def test_permanent_drive_failure_degrades_immediately(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.outbox.upload_video_drive = lambda v, w, t: (False, '上传云空间失败：rc=1 {"type": "auth"}')
        self.core.add_report(self.con, tid, "ready", "视频好了", ["v.mp4"])
        self.tick()
        self.assertEqual(len(self.cards), 1)
        self.assertIn("视频在下一条消息", json.dumps(self.cards[0], ensure_ascii=False))

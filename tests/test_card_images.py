from __future__ import annotations

import json
import os

from helpers import TicketdTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64


class CardImagesTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, outbox, templates
        self.core, self.outbox, self.templates = core, outbox, templates

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

    def sent(self):
        p = self.tmp / "lark.log"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def cards(self):
        """发出的卡片（跳过稿子导入等其他 lark-cli 调用）。"""
        return [json.loads(a[a.index("--content") + 1]) for a in self.sent() if "--content" in a]

    def test_status_card_never_carries_placeholders(self):
        els = self.templates.result_elements("ready", "好", ["/w/a.png"], None, "/w")
        self.assertFalse(any(self.templates.UPLOAD_KEY in e for e in els))

    def test_result_card_images_uploaded_and_replaced(self):
        tid, wd = self.ticket()
        a = self.write(os.path.join(wd, "生成", "预览-1.png"), PNG)
        b = self.write(os.path.join(wd, "生成", "预览-2.jpeg"), JPG)
        md = self.write(os.path.join(wd, "结果.md"), b"# x")
        self.core.add_report(self.con, tid, "blocked", "预览好了", [a, b, md], ask="选第几张？")
        uploads = []

        def fake_upload(path):
            with open(path, "rb") as f:
                uploads.append(f.read())
            self.assertNotIn(wd, path)  # 上传的是私有副本，不是 worker 可控的路径
            return True, "img_v3_%d" % len(uploads), False

        orig = self.outbox.upload_image
        self.outbox.upload_image = fake_upload
        try:
            self.outbox.flush(self.con)
        finally:
            self.outbox.upload_image = orig
        self.assertEqual(uploads, [PNG, JPG])
        card = self.cards()[0]
        imgs = [e for e in card["body"]["elements"] if e.get("tag") == "img"]
        self.assertEqual([i["img_key"] for i in imgs], ["img_v3_1", "img_v3_2"])
        self.assertNotIn(self.templates.UPLOAD_KEY, json.dumps(card))
        # 换好的卡写回发件箱，重试不再上传
        row = self.con.execute("SELECT card_json FROM outbox").fetchone()
        self.assertIn("img_v3_1", row["card_json"])
        self.assertIn("📎", json.dumps(card, ensure_ascii=False))  # 路径列表照旧

    def test_images_shown_when_result_overwrites_status_card(self):
        tid, wd = self.ticket()
        self.con.execute("UPDATE tickets SET card_wanted=1, card_message_id='om_card', card_sent_at=?, card_closed=0"
                         " WHERE id=?", (self.core.db.now(), tid))
        a = self.write(os.path.join(wd, "预览.png"), PNG)
        self.core.add_report(self.con, tid, "ready", "预览好了", [a])
        patched = []
        orig = self.outbox.upload_image
        self.outbox.upload_image = lambda path: (True, "img_v3_x", False)
        try:
            self.outbox.flush(self.con, card_patcher=lambda mid, c: patched.append((mid, c)) or (True, "", False))
        finally:
            self.outbox.upload_image = orig
        self.assertEqual(self.cards(), [])  # 结果覆盖在状态卡上，不另发
        mid, card = patched[0]
        self.assertEqual(mid, "om_card")
        self.assertIn('"img_key": "img_v3_x"', json.dumps(card))
        self.assertNotIn(self.templates.UPLOAD_KEY, json.dumps(card))

    def test_relative_artifact_paths_resolve_against_workdir(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "生成", "预览-1.png"), PNG)
        self.core.add_report(self.con, tid, "blocked", "预览好了", ["生成/预览-1.png", "../escape.png"], ask="选？")
        self.write(os.path.join(os.path.dirname(wd), "escape.png"), PNG)
        uploads = []

        def fake_upload(path):
            with open(path, "rb") as f:
                uploads.append(f.read())
            return True, "img_v3_%d" % len(uploads), False

        orig = self.outbox.upload_image
        self.outbox.upload_image = fake_upload
        try:
            self.outbox.flush(self.con)
        finally:
            self.outbox.upload_image = orig
        self.assertEqual(uploads, [PNG])  # 相对路径按工单目录解析；../ 越界的照旧拒绝
        card = self.cards()[0]
        imgs = [e for e in card["body"]["elements"] if e.get("tag") == "img"]
        self.assertEqual([i["img_key"] for i in imgs], ["img_v3_1"])

    def test_rejects_outside_symlink_fake_and_failed_upload(self):
        tid, wd = self.ticket()
        outside = self.write(str(self.tmp / "secret.png"), PNG)
        link = os.path.join(wd, "link.png")
        os.symlink(outside, link)
        fake = self.write(os.path.join(wd, "fake.png"), b"not an image")
        good = self.write(os.path.join(wd, "good.png"), PNG)
        fail = self.write(os.path.join(wd, "fail.png"), PNG)
        self.core.add_report(self.con, tid, "ready", "好", [outside, link, fake, good, fail])
        calls = []

        def fake_upload(path):
            calls.append(path)
            return (len(calls) == 1), ("img_ok" if len(calls) == 1 else "rc=1 boom"), False

        orig = self.outbox.upload_image
        self.outbox.upload_image = fake_upload
        try:
            self.outbox.flush(self.con)
        finally:
            self.outbox.upload_image = orig
        self.assertEqual(len(calls), 2)  # 只有 good / fail 走到上传
        card = self.cards()[0]
        els = card["body"]["elements"]
        imgs = [e for e in els if e.get("tag") == "img"]
        self.assertEqual([i["img_key"] for i in imgs], ["img_ok"])
        names = [e["text"]["content"] for e in els if e.get("tag") == "div"]
        self.assertIn("good.png", names)
        self.assertNotIn("fail.png", names)  # 失败图片连同标题一起去掉
        self.assertNotIn("secret.png", names)

    def test_upload_command_shape(self):
        self.write(str(self.tmp / "x.png"), PNG)
        ok, key, _ = self.outbox.upload_image(str(self.tmp / "x.png"))
        args = self.sent()[0]
        self.assertEqual(args[:5], ["im", "images", "create", "--as", "bot"])
        self.assertIn("image=x.png", args)  # lark-cli 只认相对路径
        self.assertTrue(ok)

from __future__ import annotations

import json
import os

from helpers import TicketdTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 64


def find_all(obj, tag):
    """卡片里（任意嵌套层）所有某种 tag 的元素，按出现顺序。"""
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


class CardVideosTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import core, outbox, templates
        self.core, self.outbox, self.templates = core, outbox, templates
        self._orig_upload = outbox.upload_image
        self.uploads = []

        def fake_upload(path):
            with open(path, "rb") as f:
                self.uploads.append(f.read())
            return True, "img_v3_%d" % len(self.uploads), False

        outbox.upload_image = fake_upload
        self._orig_drive = outbox.upload_video_drive

    def tearDown(self):
        self.outbox.upload_image = self._orig_upload
        self.outbox.upload_video_drive = self._orig_drive
        super().tearDown()

    def no_drive(self):
        """云空间上传失败（如用户身份不可用）：视频走卡片后单独发视频消息的老路。"""
        self.outbox.upload_video_drive = lambda v, w, t: (False, "用户身份不可用")

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

    def rows(self):
        return self.con.execute("SELECT * FROM outbox ORDER BY id").fetchall()

    def flush_all(self, n=5):
        for _ in range(n):
            self.outbox.flush(self.con)

    def card_of(self, args):
        return json.loads(args[args.index("--content") + 1])

    # ---------- 模板 ----------
    def test_template_video_placeholder_and_no_duplicate_cover_image(self):
        arts = ["生成/v/视频-720p.mp4", "生成/v/视频-720p.jpg", "生成/k/预览-1.jpeg", "结果.md"]
        els = self.templates.result_elements("blocked", "预览好了", arts, "要 1080p 吗？", "/w", images=True)
        vids = [e for e in els if self.templates.VIDEO_KEY in e]
        self.assertEqual([v[self.templates.VIDEO_KEY] for v in vids], ["生成/v/视频-720p.mp4"])
        uploads = [e[self.templates.UPLOAD_KEY] for e in find_all(els, "img")]
        self.assertEqual(uploads, ["生成/k/预览-1.jpeg"])  # 同名封面图不再作为普通图片重复显示
        self.assertEqual(els[-1]["header"]["title"]["content"], "📎 本机文件（4）")
        # 画廊：视频（主角）在最上面，其余图作三列小缩略图跟在后面，然后才是一行结论
        self.assertEqual([e["tag"] for e in els[:3]], ["div", "column_set", "div"])
        self.assertIn(self.templates.VIDEO_KEY, els[0])
        self.assertEqual(len(els[1]["columns"]), 3)
        self.assertEqual(els[2]["text"]["content"], "预览好了")
        plain = self.templates.result_elements("blocked", "x", arts, None, "/w")  # 状态卡不带任何占位
        self.assertFalse(any(self.templates.VIDEO_KEY in e or self.templates.UPLOAD_KEY in e for e in plain))
        self.assertEqual(self.templates.cover_candidates("a/b.mp4")[:2], ["a/b.jpg", "a/b.jpeg"])

    def test_video_elements_shapes(self):
        els = self.templates.video_elements("生成/v/视频-720p.mp4", "https://x.feishu.cn/file/b", "img_1")
        (box,) = els
        self.assertEqual(box["tag"], "interactive_container")
        self.assertEqual(box["behaviors"], [{"type": "open_url", "default_url": "https://x.feishu.cn/file/b"}])
        cover, btn = box["elements"]
        self.assertEqual(cover, {"tag": "img", "img_key": "img_1", "scale_type": "fit_horizontal", "corner_radius": "8px",
                                 "preview": False, "alt": {"tag": "plain_text", "content": "视频-720p.mp4 封面"}})
        self.assertEqual((btn["text"]["content"], btn["width"], btn["type"], btn["behaviors"]),
                         ("▶ 播放视频", "fill", "primary_filled", box["behaviors"]))  # 点封面、点按钮都能播
        fb = self.templates.video_elements("v.mp4", None, "img_1")  # 没链接：封面 + 说明，视频另发
        self.assertEqual(fb[0]["img_key"], "img_1")
        self.assertIn("视频在下一条消息", fb[1]["text"]["content"])
        self.assertEqual(len(self.templates.video_elements("v.mp4", "https://x.feishu.cn/f", None)[0]["elements"]), 1)

    def test_reports_do_not_enqueue_separate_video_messages(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "progress", "出视频", ["v.mp4"])
        self.core.add_report(self.con, tid, "blocked", "预览好了", ["v.mp4"], ask="要 1080p 吗？")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["media_json"])

    # ---------- 发件箱：云空间链接进卡片 ----------
    def test_video_uploaded_to_drive_and_playable_from_card(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "生成", "v", "视频-720p.mp4"), MP4)
        self.write(os.path.join(wd, "生成", "v", "视频-720p.jpg"), JPG)
        self.core.add_report(self.con, tid, "blocked", "预览好了",
                             ["生成/v/视频-720p.mp4", "生成/v/视频-720p.jpg"], ask="要 1080p 吗？")
        self.flush_all()
        sent = self.sent()
        up, meta, card_args = sent
        self.assertEqual(up[:4], ["drive", "+upload", "--as", "user"])  # 用户身份：文件归用户，自己能打开
        self.assertEqual(up[up.index("--file") + 1], "./video.mp4")
        self.assertEqual(up[up.index("--name") + 1], "%s-视频-720p.mp4" % tid)
        self.assertNotIn("--folder-token", up)
        self.assertEqual(meta[:5], ["drive", "metas", "batch_query", "--as", "user"])
        card = self.card_of(card_args)
        box = [e for e in card["body"]["elements"] if e.get("tag") == "interactive_container"][0]
        url = "https://example.feishu.cn/file/boxcn_fake_1"
        self.assertEqual(box["behaviors"][0]["default_url"], url)
        self.assertEqual(box["elements"][0]["img_key"], "img_v3_1")
        btn = find_all(box, "button")[0]
        self.assertEqual(btn["behaviors"][0]["default_url"], url)
        self.assertEqual(self.uploads, [JPG])  # 封面只传一次，没被当普通图片重复显示
        self.assertNotIn(self.templates.VIDEO_KEY, json.dumps(card))
        self.assertEqual([r["status"] for r in self.rows()], ["sent"])  # 没有单独的视频消息
        # 换好的卡写回发件箱：重试不再上传
        self.assertIn(url, self.rows()[0]["card_json"])

    def test_video_card_overwrites_status_card_and_leftover_follows(self):
        tid, wd = self.ticket()
        self.con.execute("UPDATE tickets SET card_wanted=1, card_message_id='om_card', card_sent_at=?, card_closed=0"
                         " WHERE id=?", (self.core.db.now(), tid))
        self.no_drive()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        self.core.add_report(self.con, tid, "ready", "成片好了", ["v.mp4"])
        patched = []
        for _ in range(3):
            self.outbox.flush(self.con, card_patcher=lambda mid, c: patched.append((mid, c)) or (True, "", False))
        self.assertEqual(len(patched), 1)  # 结果覆盖到状态卡上
        self.assertEqual(patched[0][0], "om_card")
        self.assertNotIn(self.templates.VIDEO_KEY, json.dumps(patched[0][1]))
        self.assertIn("img_v3_1", json.dumps(patched[0][1]))  # 封面照常显示
        sent = self.sent()
        self.assertEqual(len(sent), 1)  # 云空间不可用：视频在状态卡之后另发一条视频消息
        self.assertIn("--video", sent[0])
        self.assertEqual([r["status"] for r in self.rows()], ["sent", "sent"])

    def test_folder_token_from_config(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        os.environ["TICKETD_VIDEO_FOLDER"] = "fldcn_x"
        ok, url = self.outbox.upload_video_drive("v.mp4", wd, tid)
        self.assertTrue(ok)
        up = self.sent()[0]
        self.assertEqual(up[up.index("--folder-token") + 1], "fldcn_x")

    def test_drive_name_is_sanitized(self):
        self.assertEqual(self.outbox._safe_drive_name("T0010", "a/b/--x y;rm.mp4"), "T0010---x_y_rm.mp4")  # 有工单号前缀，永远不会以 - 开头被当成参数
        self.assertEqual(self.outbox._safe_drive_name("T0010", "clip.MOV"), "T0010-clip.MOV.mp4")

    def test_url_must_be_feishu_https(self):
        self.assertIsNone(self.outbox._find_url({"url": "javascript:alert(1)"}))
        self.assertIsNone(self.outbox._find_url({"file": {"url": "https://evil.com/x"}}))
        self.assertEqual(self.outbox._find_url({"file": {"url": "https://a.feishu.cn/file/x"}}), "https://a.feishu.cn/file/x")

    def test_drive_failure_falls_back_to_video_message_after_card(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "生成", "v", "视频-720p.mp4"), MP4)
        self.write(os.path.join(wd, "生成", "v", "视频-720p.jpg"), JPG)
        self.core.add_report(self.con, tid, "blocked", "预览好了", ["生成/v/视频-720p.mp4"], ask="要 1080p 吗？")
        orig = self.outbox.upload_video_drive
        self.outbox.upload_video_drive = lambda v, w, t: (False, "keychain 不可用")
        try:
            self.flush_all()
        finally:
            self.outbox.upload_video_drive = orig
        sent = self.sent()
        self.assertEqual(len(sent), 2)
        card = self.card_of(sent[0])
        self.assertFalse(any(e.get("tag") == "interactive_container" for e in card["body"]["elements"]))
        self.assertIn("视频在下一条消息", json.dumps(card, ensure_ascii=False))
        self.assertEqual([i["img_key"] for i in find_all(card, "img")], ["img_v3_1"])
        video = sent[1]
        self.assertEqual(video[video.index("--video") + 1], "./video.mp4")
        self.assertEqual(video[video.index("--video-cover") + 1], "./cover.jpg")
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["sent", "sent"])
        self.assertEqual(rows[1]["reply_to"], rows[0]["reply_to"])
        self.assertEqual(self.core.lookup(self.con, rows[1]["sent_message_id"]), tid)  # 引用视频消息也能追问
        self.assertIn("视频没能传到云空间", (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8"))

    def test_drive_rejects_bad_video_without_calling_lark(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "fake.mp4"), b"not a video at all")
        self.write(str(self.tmp / "secret.mp4"), MP4)
        os.symlink(str(self.tmp / "secret.mp4"), os.path.join(wd, "link.mp4"))
        self.write(os.path.join(wd, "big.mp4"), MP4 + b"0" * (self.outbox.MAX_DRIVE_VIDEO_BYTES + 1))
        for v in ("fake.mp4", "link.mp4", "../secret.mp4", "big.mp4"):
            ok, why = self.outbox.upload_video_drive(v, wd, tid)
            self.assertFalse(ok, v)
        self.assertEqual(self.sent(), [])

    def test_video_files_handed_to_lark_are_private_copies(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.png"), PNG)
        seen = {}

        def fake_run(cmd, field="message_id", cwd=None, timeout=30):
            self.assertNotIn(wd, cwd)
            with open(os.path.join(cwd, "video.mp4"), "rb") as f:
                seen["video"] = f.read()
            with open(os.path.join(cwd, "cover.png"), "rb") as f:
                seen["cover"] = f.read()
            seen["timeout"] = timeout
            return True, "om_v", False

        orig = self.outbox._run_lark
        self.outbox._run_lark = fake_run
        try:
            ok, info, _ = self.outbox.send_video("om_1", "v.mp4", wd, "k")
        finally:
            self.outbox._run_lark = orig
        self.assertEqual((ok, info), (True, "om_v"))
        self.assertEqual(seen["video"], MP4)
        self.assertEqual(seen["cover"], PNG)
        self.assertGreater(seen["timeout"], 30)

    def test_no_cover_card_keeps_note_and_video_goes_as_file(self):
        tid, wd = self.ticket()
        self.no_drive()
        self.write(os.path.join(wd, "out.mp4"), MP4)
        self.core.add_report(self.con, tid, "ready", "好了", ["out.mp4"])
        self.flush_all()
        sent = self.sent()
        card = json.loads(sent[0][sent[0].index("--content") + 1])
        self.assertFalse(any(e.get("tag") == "img" for e in card["body"]["elements"]))
        self.assertIn("🎬 out.mp4", json.dumps(card, ensure_ascii=False))
        self.assertEqual(sent[1][sent[1].index("--file") + 1], "./video.mp4")
        self.assertNotIn("--video", sent[1])

    def test_bad_video_falls_back_to_text(self):
        tid, wd = self.ticket()
        outside = self.write(str(self.tmp / "secret.mp4"), MP4)
        self.write(os.path.join(wd, "fake.mp4"), b"not a video at all")
        os.symlink(outside, os.path.join(wd, "link.mp4"))
        for name in ("fake.mp4", "link.mp4", "../secret.mp4"):
            self.core.add_report(self.con, tid, "ready", "好了", [name])
        self.flush_all(10)
        texts = [a[a.index("--text") + 1] for a in self.sent() if "--text" in a]
        self.assertEqual(len(texts), 3)
        self.assertTrue(all(t.startswith("🎬 %s 的视频没能直接发出" % tid) for t in texts))
        self.assertFalse(any("--video" in a or "--file" in a for a in self.sent()))
        self.assertTrue(all(r["status"] == "sent" for r in self.rows()))
        alerts = (self.tmp / "home" / "logs" / "alerts.log").read_text(encoding="utf-8")
        self.assertIn("视频消息发送失败", alerts)

    def test_video_network_failure_retries_with_same_key(self):
        tid, wd = self.ticket()
        self.no_drive()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        self.core.add_report(self.con, tid, "ready", "好了", ["v.mp4"])
        self.outbox.flush(self.con)  # 卡片
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        self.outbox.flush(self.con)  # 视频：网络失败，留着重试
        row = self.rows()[1]
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con)
        sent = self.sent()
        self.assertEqual(self.rows()[1]["status"], "sent")
        self.assertIn("--video", sent[-1])

    def test_text_fallback_retries_keep_same_key(self):
        tid, wd = self.ticket()
        self.core.add_report(self.con, tid, "ready", "好了", ["missing.mp4"])
        self.outbox.flush(self.con)  # 卡片
        fail = self.tmp / "fail"
        fail.write_text("1")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        self.outbox.flush(self.con)  # 视频不存在 → 兜底文字，网络失败
        self.con.execute("UPDATE outbox SET next_at=0")
        self.outbox.flush(self.con)
        row = self.rows()[1]
        self.assertEqual((row["status"], row["media_json"]), ("sent", ""))
        last = self.sent()[-1]
        self.assertIn("--text", last)
        self.assertTrue(last[last.index("--idempotency-key") + 1].endswith("-t"))

    def test_repeated_network_failures_switch_to_text(self):
        tid, wd = self.ticket()
        self.no_drive()
        self.write(os.path.join(wd, "v.mp4"), MP4)
        self.write(os.path.join(wd, "v.jpg"), JPG)
        self.core.add_report(self.con, tid, "ready", "好了", ["v.mp4"])
        self.outbox.flush(self.con)  # 卡片
        # 失败满 3 次且这条视频消息已等了 10 分钟才改发文字
        self.con.execute("UPDATE outbox SET created_at=created_at-?", (self.outbox.MEDIA_DEGRADE_AFTER + 1,))
        fail = self.tmp / "fail"
        fail.write_text("3")
        os.environ["FAKE_LARK_FAIL_FILE"] = str(fail)
        for _ in range(4):
            self.con.execute("UPDATE outbox SET next_at=0")
            self.outbox.flush(self.con)
        row = self.rows()[1]
        self.assertEqual((row["status"], row["media_json"]), ("sent", ""))
        last = self.sent()[-1]
        self.assertIn("--text", last)
        self.assertNotIn("--video", last)

    def test_opened_file_outside_workdir_rejected(self):
        tid, wd = self.ticket()
        self.write(os.path.join(wd, "sub", "v.mp4"), MP4)
        orig = self.outbox._fd_path
        self.outbox._fd_path = lambda fd: str(self.tmp / "elsewhere" / "v.mp4")  # 模拟中间目录被换成外链
        try:
            with self.assertRaises(OSError):
                self.outbox._read_workdir_file("sub/v.mp4", wd, 1 << 20)
        finally:
            self.outbox._fd_path = orig
        self.assertEqual(self.outbox._read_workdir_file("sub/v.mp4", wd, 1 << 20), MP4)
        self.assertTrue(os.path.realpath(self.outbox._fd_path(os.open(os.path.join(wd, "sub", "v.mp4"), os.O_RDONLY)))
                        .startswith(os.path.realpath(wd)))

    def test_cover_dedupe_ignores_case(self):
        els = self.templates.result_elements("ready", "x", ["clip.MP4", "clip.JPG"], None, "/w", images=True)
        self.assertFalse(any(self.templates.UPLOAD_KEY in e for e in els))

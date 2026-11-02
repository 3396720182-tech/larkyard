from __future__ import annotations

import json
import re
import os

from helpers import TicketdTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"0" * 64


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
        imgs = find_all(card, "img")
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
        self.assertEqual([i["img_key"] for i in find_all(card, "img")], ["img_v3_x"])
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
        imgs = find_all(card, "img")
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
        imgs = find_all(card, "img")
        self.assertEqual([i["img_key"] for i in imgs], ["img_ok"])
        notes = [d["text"]["content"] for d in find_all(card, "div")]
        # 5 张进网格（图 1..5）：只有 good（图 4）显示出来，其余换成一行「未能显示」，不泄露路径
        self.assertEqual(sum("未能显示" in n for n in notes), 4)
        self.assertIn("（图 5 未能显示）", notes)
        self.assertFalse(any("secret" in n for n in notes if "未能显示" in n))

    def test_upload_command_shape(self):
        self.write(str(self.tmp / "x.png"), PNG)
        ok, key, _ = self.outbox.upload_image(str(self.tmp / "x.png"))
        args = self.sent()[0]
        self.assertEqual(args[:5], ["im", "images", "create", "--as", "bot"])
        self.assertIn("image=x.png", args)  # lark-cli 只认相对路径
        self.assertTrue(ok)


class ResultLayoutTest(TicketdTestCase):
    """结果卡改版（T0010）：结论放大 → 需要你（浅色块）→ 细节 → 媒体 → 链接 → 折叠的本机文件。"""

    def setUp(self):
        super().setUp()
        from ticketd import outbox, templates
        self.outbox, self.t = outbox, templates

    def test_summary_split_into_headline_and_details(self):
        self.assertEqual(self.t.split_summary("做好了。细节一；细节二"), ("做好了", "细节一；细节二"))
        self.assertEqual(self.t.split_summary("一句话没有句号"), ("一句话没有句号", ""))
        head, rest = self.t.split_summary("长" * 200)
        self.assertTrue(head.endswith("…") and rest == "长" * 200)
        els = self.t.result_elements("ready", "做好了。细节在这", [], None, "/w")
        self.assertEqual(els[0]["text"], {"tag": "plain_text", "content": "做好了", "text_size": "heading-4"})
        self.assertEqual(els[1]["text"]["content"], "细节在这")

    def test_ask_callout_right_after_headline(self):
        els = self.t.result_elements("blocked", "预览好了。推荐第 2 张", [], "选第几张？", "/w")
        box = els[1]
        self.assertEqual(box["tag"], "column_set")
        col = box["columns"][0]
        self.assertEqual(col["background_style"], "orange-50")
        self.assertEqual([e["text"]["content"] for e in col["elements"]], ["👉 需要你", "选第几张？"])
        self.assertEqual(els[2]["text"]["content"], "推荐第 2 张")
        self.assertEqual(self.t.result_elements("ready", "x", [], "看看？", "/w")[1]["columns"][0]["background_style"],
                         "blue-50")
        dropped = self.t.result_elements("dropped", "x", [], "不属于本单", "/w")
        self.assertIn("不引用直接发我即可开新单", dropped[1]["text"]["content"])

    def test_worker_text_stays_plain(self):
        evil = "**粗**<at id=all></at>[x](https://e.com)"
        card = self.t.result_card("T1", "t", "/w", "blocked", evil + "。" + evil, ["https://e.com/a"], evil, [], 1)
        for d in find_all(card, "div"):
            self.assertEqual(d["text"]["tag"], "plain_text")
        self.assertEqual(find_all(card, "markdown"), [])
        # 多图卡里唯一的 markdown 是我们生成的编号，不含 worker 文字
        g = self.t.result_card("T1", "t", "/w", "blocked", evil, ["/w/a.png", "/w/b.png"], evil, [], 1)
        self.assertTrue(all(re.fullmatch(r"<number_tag>\d</number_tag>", m["content"]) for m in find_all(g, "markdown")))

    def test_gallery_multi_images_first_numbered_then_one_line(self):
        els = self.t.result_elements("blocked", "4 张好了。推荐第 2 张", ["a/1.png", "a/2.png", "a/3.png", "结果.md"],
                                     "选？", "/w", images=True)
        self.assertEqual([e["tag"] for e in els], ["column_set", "column_set", "div", "column_set", "collapsible_panel"])
        rows = els[:2]  # 画廊：图在最上面，两列网格
        imgs = find_all(rows, "img")
        self.assertEqual([i[self.t.UPLOAD_KEY] for i in imgs], ["a/1.png", "a/2.png", "a/3.png"])
        self.assertTrue(all(i["scale_type"] == "fit_horizontal" for i in imgs))  # 不裁剪，整张可见
        badges = [m["content"] for m in find_all(rows, "markdown")]
        self.assertEqual(badges, ["<number_tag>1</number_tag>", "<number_tag>2</number_tag>", "<number_tag>3</number_tag>"])
        self.assertEqual(rows[1]["columns"][1]["elements"], [])  # 奇数张补空列
        self.assertEqual(els[2]["text"]["content"], "4 张好了")  # 只留一行结论
        self.assertEqual(els[2]["text"]["lines"], 2)
        panel = els[4]  # 其余文字和本机文件收进「详情」
        self.assertEqual((panel["expanded"], panel["header"]["title"]["content"]), (False, "详情"))
        self.assertEqual(panel["elements"][0]["text"]["content"], "推荐第 2 张")

    def test_gallery_single_image_hero(self):
        els = self.t.result_elements("ready", "成品图好了", ["a/1.png"], None, "/w", images=True)
        self.assertEqual([e["tag"] for e in els], ["img", "div", "collapsible_panel"])
        self.assertEqual((els[0]["scale_type"], els[0]["corner_radius"]), ("fit_horizontal", "8px"))
        self.assertEqual(find_all(els, "markdown"), [])  # 单图不需要编号
        self.assertEqual(els[2]["header"]["title"]["content"], "📎 本机文件（1）")
    def test_text_only_card_document_layout(self):
        els = self.t.result_elements("ready", "x。细节", ["/w/结果.md", "/w/生成/a.png", "https://x.feishu.cn/docx/1"],
                                     None, "/w")  # images=False（如状态卡）：文档式，图不显示
        self.assertEqual([e["tag"] for e in els], ["div", "div", "button", "collapsible_panel"])
        panel = els[-1]
        self.assertEqual((panel["tag"], panel["expanded"]), ("collapsible_panel", False))
        self.assertEqual(panel["header"]["title"]["content"], "📎 本机文件（2）")
        self.assertEqual(panel["elements"][0]["text"]["content"], "📎 本机文件\n结果.md\n生成/a.png")
    def test_nested_grid_images_uploaded(self):
        tid = self.new_ticket()
        wd = self.con.execute("SELECT workdir FROM tickets WHERE id=?", (tid,)).fetchone()["workdir"]
        os.makedirs(wd, exist_ok=True)
        for n in (1, 2):
            with open(os.path.join(wd, "%d.png" % n), "wb") as f:
                f.write(PNG)
        card = self.t.result_card(tid, "t", wd, "blocked", "x", ["1.png", "2.png", "3.png"], "选？", [], 1)
        self.assertTrue(self.outbox._has_uploads(card))
        keys = iter(["img_a", "img_b"])
        out = self.outbox.resolve_card_images(card, wd, uploader=lambda p: (True, next(keys), False))
        self.assertFalse(self.outbox._has_uploads(out))
        self.assertEqual([i["img_key"] for i in find_all(out, "img")], ["img_a", "img_b"])
        self.assertIn("（图 3 未能显示）", [d["text"]["content"] for d in find_all(out, "div")])

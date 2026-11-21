"""卡片正文只放结论和需要你做的那一步（T0057）：长的、带代码 / 命令 / 路径的内容收进「详情」，
沙箱外执行的脚本默认收起、只露一句「要做什么」。"""
from __future__ import annotations

import json

from helpers import TicketdTestCase
from test_card_images import find_all


def texts(els):
    return [d["text"]["content"] for d in find_all(els, "div")]


class ConciseCardTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        from ticketd import templates
        self.t = templates

    # ---------- 判断 ----------
    def test_technical_detection(self):
        for s in ("跑了 `make test`", "见 /Users/x/a.py 第 3 行", "在 ~/Projects/x/ 里", "```\ngit push\n```",
                  "加了 --force 参数", "$ git push", "./scripts/x.sh 报错"):
            self.assertTrue(self.t.technical(s), s)
        for s in ("改好了，已推送", "选第 2 张？", "结果.md 里有细节", "A/B 测试两版都好了", "链接 https://x.feishu.cn/docx/a/b",
                  "1/2 完成", "要不要授权本机网络？"):
            self.assertFalse(self.t.technical(s), s)
        for s in ("改了 src/auth/login.ts", "提交 3f2a1c9", "见 a/b.py"):
            self.assertTrue(self.t.technical(s), s)
        for s in ("版本 2026", "deadbeef 不是提交号", "看 https://x.com/a.html"):
            self.assertFalse(self.t.technical(s), s)
        self.assertFalse(self.t.technical("能授权 ~/Projects/x 吗？", paths=False))  # 要授权的目录是问题本身
        self.assertTrue(self.t.technical("执行 `git push` 吗？", paths=False))

    def test_brief(self):
        self.assertEqual(self.t.brief("用 A 还是 B？推荐 A。", 120), ("用 A 还是 B？推荐 A。", False))
        shown, folded = self.t.brief("用 A 还是 B？推荐 A。A 更快。还有一个原因。", 120)
        self.assertEqual((shown, folded), ("用 A 还是 B？", True))  # 超过两句：只露第一句
        shown, folded = self.t.brief("要推送吗？命令是 `git push -u origin x`", 120)
        self.assertEqual((shown, folded), ("要推送吗？", True))  # 带代码：只露第一句
        shown, folded = self.t.brief("要推送吗\n```\ngit push\n```", 120)
        self.assertEqual((shown, folded), ("要推送吗", True))  # 代码块前面的话
        shown, folded = self.t.brief("长" * 300, 120)
        self.assertTrue(folded and shown.endswith("…") and len(shown) == 121)
        # 分号不算句号：默认提问（两句）原样显示
        from ticketd import core
        self.assertEqual(self.t.brief(core.RUN_DEFAULT_ASK, 120), (core.RUN_DEFAULT_ASK, False))

    # ---------- 结果卡 ----------
    def test_short_summary_and_ask_stay_inline(self):
        els = self.t.result_elements("blocked", "预览好了。推荐第 2 张", [], "选第几张？", "/w")
        self.assertEqual([e["tag"] for e in els], ["div", "column_set", "div"])
        self.assertEqual(texts(els), ["预览好了", "👉 需要你", "选第几张？", "推荐第 2 张"])

    def test_technical_details_fold_into_panel(self):
        summary = "修好了卡片折叠。改了 `ticketd/templates.py` 的 result_elements，跑了全部单测"
        els = self.t.result_elements("ready", summary, ["/w/结果.md"], None, "/w")
        self.assertEqual([e["tag"] for e in els], ["div", "collapsible_panel"])
        self.assertEqual(els[0]["text"]["content"], "修好了卡片折叠")
        panel = els[-1]
        self.assertFalse(panel["expanded"])
        self.assertEqual(panel["header"]["title"]["content"], "详情 · 完整内容见文档")  # 有稿子：提示看文档
        self.assertIn("ticketd/templates.py", panel["elements"][0]["text"]["content"])
        # 没有稿子：只写「详情」
        els = self.t.result_elements("ready", summary, [], None, "/w")
        self.assertEqual(els[-1]["header"]["title"]["content"], "详情")

    def test_long_details_fold_even_without_code(self):
        summary = "做完了。" + "第一条细节。第二条细节。"
        els = self.t.result_elements("ready", summary, [], None, "/w")
        self.assertEqual([e["tag"] for e in els], ["div", "collapsible_panel"])
        summary = "做完了。" + "很长的补充" * 30
        self.assertEqual([e["tag"] for e in self.t.result_elements("ready", summary, [], None, "/w")],
                         ["div", "collapsible_panel"])

    def test_long_ask_shows_first_sentence_full_text_in_details(self):
        ask = "能授权本机网络吗？原因是要连 127.0.0.1:5432 的数据库，命令 `psql -h localhost` 被拒。推荐：可以"
        els = self.t.result_elements("blocked", "卡在连不上数据库", ["/w/结果.md"], ask, "/w")
        box = els[1]
        self.assertEqual(box["columns"][0]["elements"][1]["text"]["content"],
                         "能授权本机网络吗？（完整说明见下方「详情」）")
        panel = els[-1]
        self.assertEqual(panel["tag"], "collapsible_panel")
        self.assertIn("psql -h localhost", texts(panel["elements"])[0])
        self.assertIn("📎 本机文件", texts(panel["elements"])[-1])
        self.assertNotIn("psql", json.dumps(els[:-1], ensure_ascii=False))  # 正文里没有命令

    def test_ask_with_directory_to_grant_stays_inline(self):
        els = self.t.result_elements("blocked", "缺写权限", [], "能授权 ~/Projects/x 吗？推荐：可以", "/w")
        self.assertEqual(texts(els), ["缺写权限", "👉 需要你", "能授权 ~/Projects/x 吗？推荐：可以"])
        # 只有一句、截不出更短的：原样显示，不加「见详情」
        els = self.t.result_elements("blocked", "x", [], "执行 `git push` 吗？", "/w")
        self.assertEqual(texts(els)[-1], "执行 `git push` 吗？")
        self.assertNotIn("collapsible_panel", [e["tag"] for e in els])

    def test_dropped_reason_is_brief(self):
        els = self.t.result_elements("dropped", "不属于本单", [], "追问要改另一个仓库。详见 `~/Projects/y/` 的说明。另开单更便宜", "/w")
        self.assertEqual(els[1]["text"]["content"], "说明：追问要改另一个仓库。不引用直接发我即可开新单。")
        self.assertEqual(els[-1]["tag"], "collapsible_panel")

    def test_gallery_card_folds_long_ask_too(self):
        els = self.t.result_elements("blocked", "4 张好了", ["a/1.png"], "选哪张？`a/1.png` 最好。第二句。第三句。", "/w",
                                     images=True)
        self.assertEqual(els[-1]["tag"], "collapsible_panel")
        self.assertIn("`a/1.png` 最好", json.dumps(els[-1], ensure_ascii=False))

    def test_code_fences_never_become_the_headline(self):
        els = self.t.result_elements("blocked", "```bash\ngit push\n```", [], "```bash\ngit push\n```\n要执行吗？", "/w")
        self.assertEqual(els[0]["text"]["content"], "结果见下方「详情」")
        self.assertEqual(els[1]["columns"][0]["elements"][1]["text"]["content"], "要执行吗？（完整说明见下方「详情」）")
        self.assertIn("git push", json.dumps(els[-1], ensure_ascii=False))
        els = self.t.result_elements("ready", "改好了：\n```\nx = 1\n```", [], None, "/w")
        self.assertEqual(els[0]["text"]["content"], "改好了：")
        self.assertEqual(self.t.brief("```\nx\n```", 120)[0], "（内容是代码，见下方「详情」）")
        self.assertEqual(self.t.ping_message("T1", "blocked", "```\nx\n```\n卡住了", None).splitlines()[0],
                         "⚠️ T1 需要你决定：卡住了")

    def test_semicolon_clauses_fold(self):
        els = self.t.result_elements("ready", "登录报错修好了。加了重试；跑了单测", [], None, "/w")
        self.assertEqual([e["tag"] for e in els], ["div", "collapsible_panel"])

    def test_blank_ask_is_ignored(self):
        els = self.t.result_elements("ready", "好了", [], "   ", "/w")
        self.assertEqual([e["tag"] for e in els], ["div"])
        self.assertIsNone(self.t.ping_message("T1", "ready", "好了", "  "))

    # ---------- 沙箱外执行申请 ----------
    def test_run_purpose(self):
        self.assertEqual(self.t.run_purpose("#!/bin/bash\n# 推送分支并开 PR\n# 不改别的\nset -e\n# 后面的注释\n"),
                         "推送分支并开 PR")  # 只取第一行说明
        self.assertEqual(self.t.run_purpose("#!/bin/bash\n# -*- coding: utf-8 -*-\n# 部署\n"), "部署")
        self.assertEqual(self.t.run_purpose("git push\n# 晚了\n"), "")

    def test_run_script_collapsed_with_one_line_purpose(self):
        run = {"id": 3, "path": "推送.sh", "timeout": 1800,
               "script": "#!/bin/bash\n# 推送分支并开 PR\nset -e\n  git push -u origin fix/a\n"}
        card = self.t.result_card("T1", "t", "/w", "blocked", "改好了，要推送", ["/w/结果.md"], "可以执行吗？", ["写代码"], 60,
                                  run=run)
        els = card["body"]["elements"]
        self.assertIn("🖥️ 申请在沙箱外执行：推送分支并开 PR", texts(els))
        panel = next(e for e in els if e.get("element_id") == self.t.RUN_PANEL_ID)
        self.assertFalse(panel["expanded"])
        self.assertEqual(panel["header"]["title"]["content"], "查看完整脚本（R3 · 推送.sh · 4 行）")
        self.assertIn("  git push -u origin fix/a", panel["elements"][0]["text"]["content"])  # 原样含缩进
        # 稿子按钮和内部版标识不放进脚本面板，排在它后面
        self.t.with_doc_links(card, [("结果", "https://x.feishu.cn/docx/1")])
        self.t.with_badge(card, "内部版 1.0")
        tags = [(e["tag"], e.get("element_id")) for e in els]
        run_at = tags.index(("collapsible_panel", self.t.RUN_PANEL_ID))
        btn_at = next(i for i, e in enumerate(els) if e["tag"] == "button")
        self.assertGreater(btn_at, run_at)
        self.assertNotIn("内部版", json.dumps(panel, ensure_ascii=False))
        self.assertIn("内部版 1.0", json.dumps(els[-2], ensure_ascii=False))

    def test_run_without_purpose_asks_to_expand(self):
        els = self.t.run_elements({"id": 1, "path": "x.sh", "script": "git push\n"})
        self.assertEqual(els[0]["text"]["content"], "🖥️ 申请在沙箱外执行：脚本开头没写说明，批准前请展开看完整脚本")

    # ---------- 需要你决定的提醒 ----------
    def test_ping_is_brief(self):
        ask = "能授权本机网络吗？原因是 `psql -h localhost` 被拒。推荐：可以"
        msg = self.t.ping_message("T1", "blocked", "卡住了", ask)
        self.assertEqual(msg, "⚠️ T1 需要你决定：能授权本机网络吗？\n详情已更新在上方状态卡，引用这条回复即可")
        msg = self.t.ping_message("T1", "blocked", "缺写权限。细节一大段", None)
        self.assertTrue(msg.startswith("⚠️ T1 需要你决定：缺写权限\n"))

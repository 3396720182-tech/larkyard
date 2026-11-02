#!/usr/bin/env python3
"""把 ticketd 会发的每种卡片都渲染一遍，交给飞书 cardkit「创建卡片实体」接口校验结构。

不发任何消息（只创建一个不挂在任何会话上的卡片实体）。改了 templates.py / card.py 的卡片结构后跑一次：
    /usr/bin/python3 scripts/validate_cards.py            # 校验
    /usr/bin/python3 scripts/validate_cards.py --dump     # 只打印 JSON，不调接口
    /usr/bin/python3 scripts/validate_cards.py --with-images   # 另传两张小图（1x1、9x16），连含图片的画廊卡一起校验
需要 lark-cli 和 bot 身份（与 ticketd 相同的环境变量）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def samples():
    tmp = Path(tempfile.mkdtemp(prefix="ticketd-cards-"))
    os.environ.update({"TICKETD_HOME": str(tmp / "home"), "TICKETD_WORKROOT": str(tmp / "work")})
    from ticketd import card, core, db, templates
    con = db.connect()
    now = time.time()
    tid = core.create_ticket(con, chat_id="oc_x", message_id="om_x", title="状态卡视觉与推送审查", slug="x",
                             request_text="x", ack_text="开工：动 ticketd 的卡片，预期 一单一张卡")
    t = lambda: core.get(con, tid)  # noqa: E731
    out = [("排队", card.render(t(), card.view(con, t(), now), [], now))]
    con.execute("UPDATE tickets SET state='running', started_at=?, last_action_at=?, last_step=? WHERE id=?",
                (now - 300, now - 30, "运行：跑全部单测", tid))
    con.execute("UPDATE inputs SET status='delivered'")
    for p in ("读代码", "设计方案", "写核心代码"):
        core.add_report(con, tid, "progress", p)
    out.append(("进行中", card.render(t(), card.view(con, t(), now), [], now)))
    out.append(("疑似卡住", card.render(t(), card.view(con, t(), now + 3600), [], now + 3600)))
    wd = t()["workdir"]
    for st, ask, arts in (("ready", None, [wd + "/结果.md", "https://example.feishu.cn/docx/abc"]),
                          ("blocked", "能授权 ~/Projects/x 吗？推荐：可以", []),
                          ("failed", None, []), ("waiting", None, []), ("dropped", "不属于本单", [])):
        out.append(("结果卡 " + st, templates.result_card(tid, t()["title"], wd, st, "一句话结果\n第二行", arts, ask,
                                                     ["读代码", "写代码"], 600)))
    # 画廊卡（T0010）：都走 result_card + 发件箱真实的视频/图片替换流程，上传器用桩（不传云空间）。
    # 含 img 的卡要真实存在的 img_key：--with-images 时先传两张小图（1x1、9x16）拿 key。
    out += _gallery_samples(tid, t()["title"], wd, "--with-images" in sys.argv)
    core.add_report(con, tid, "ready", "做好了", [wd + "/结果.md"])
    con.execute("UPDATE outbox SET status='sent'")
    out.append(("结果卡 带稿子按钮", templates.with_doc_links(
        templates.result_card(tid, t()["title"], wd, "ready", "一句话结果", [wd + "/稿子.md"], None, ["读代码"], 600),
        [("稿子.md", "https://example.feishu.cn/docx/x"), ("表.xlsx", "https://example.feishu.cn/sheets/y")])))
    out.append(("收起", card.render(t(), card.view(con, t(), now), [], now)))
    con.execute("UPDATE outbox SET status='dead'")
    out.append(("终态直写", card.render(t(), card.view(con, t(), now), [], now)))
    return out


def _png(w: int, h: int) -> bytes:
    """生成 w×h 的纯色 PNG（不依赖第三方库）。"""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\xe8\x8a\x3c" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


_PNG = _png(1, 1)
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


def _gallery_samples(tid, title, wd, with_images):
    from ticketd import outbox, templates
    url = "https://example.feishu.cn/file/boxcnExample"
    os.makedirs(wd + "/生成", exist_ok=True)

    def put(name, data):
        with open(wd + "/生成/" + name, "wb") as f:
            f.write(data)
        return wd + "/生成/" + name

    put("v.mp4", _MP4)
    put("无封面.mp4", _MP4)
    out = []

    def resolve(card, up, drive=lambda v, w, t: (True, url)):
        card, _ = outbox.resolve_card_videos(card, wd, tid, drive_uploader=drive, uploader=up)
        return outbox.resolve_card_images(card, wd, uploader=up)

    no_img = lambda path: (False, "不该上传图片", True)  # noqa: E731
    c = templates.result_card(tid, title, wd, "blocked", "720p 预览好了。细节", [wd + "/生成/无封面.mp4"],
                              "满意就回复「出 1080p」", ["出视频"], 300)
    out.append(("画廊 视频无封面", resolve(c, no_img)))
    if not with_images:
        return out
    key = _sample_img_key(_PNG)
    key916 = _sample_img_key(_png(9, 16))
    tall_png = _png(9, 16)

    def up(path):  # 上传器拿到的是私有临时副本（文件名不保留），按内容区分竖图
        with open(path, "rb") as f:
            return True, key916 if f.read() == tall_png else key, False
    pngs = [put("预览-%d.png" % n, _PNG) for n in range(1, 5)]
    put("v.png", _PNG)
    tall = put("竖版-9x16.png", tall_png)
    put("竖版视频.mp4", _MP4)
    put("竖版视频.png", tall_png)
    md = wd + "/结果.md"
    for label, st, arts, ask in (
            ("单图", "ready", pngs[:1], None),
            ("4 图网格", "blocked", pngs, "选第几张？回复编号"),
            ("3 图网格", "ready", pngs[:3], None),
            ("视频+缩略图", "ready", [wd + "/生成/v.mp4", pngs[0]], None),
            ("视频待确认", "blocked", [wd + "/生成/v.mp4"], "满意就回复「出 1080p」"),
            ("9:16 单图", "ready", [tall], None),
            ("9:16 视频", "ready", [wd + "/生成/竖版视频.mp4"], None)):
        c = templates.result_card(tid, title, wd, st, "做好了。第二句细节", arts + [md], ask, ["出图"], 60)
        out.append(("画廊 " + label, resolve(c, up)))
    # 降级形态：网格里一张图文件不合格 → 灰字；云空间传不上 → 封面 + 「视频在下一条消息」
    put("坏图.png", b"not an image")
    c = templates.result_card(tid, title, wd, "blocked", "4 张好了", pngs[:2] + [wd + "/生成/坏图.png", pngs[3]],
                              "选？", [], 60)
    out.append(("画廊 网格有一张失败", resolve(c, up)))
    c = templates.result_card(tid, title, wd, "ready", "视频好了", [wd + "/生成/v.mp4"], None, [], 60)
    out.append(("画廊 视频兜底", resolve(c, up, drive=lambda v, w, t: (False, "auth"))))
    return out


def _sample_img_key(png: bytes) -> str:
    """传一张小 PNG 拿 image_key（只上传图片，不发消息），供含 img 的卡片校验。"""
    d = tempfile.mkdtemp(prefix="ticketd-validate-")
    with open(os.path.join(d, "x.png"), "wb") as f:
        f.write(png)
    from ticketd import outbox
    ok, key, _ = outbox._run_lark([os.environ.get("TICKETD_LARK", "lark-cli"), "im", "images", "create", "--as", "bot",
                                   "--data", json.dumps({"image_type": "message"}), "--file", "image=x.png"],
                                  field="image_key", cwd=d)
    if not (ok and key):
        sys.exit("上传示例图片失败：%s" % key)
    return key


def check(card: dict) -> str:
    body = {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}
    p = subprocess.run([os.environ.get("TICKETD_LARK", "lark-cli"), "api", "POST", "/open-apis/cardkit/v1/cards",
                        "--as", "bot", "--data", json.dumps(body, ensure_ascii=False)],
                       capture_output=True, text=True, timeout=30)
    if p.returncode == 0:
        return ""
    out = p.stdout + p.stderr
    i = out.find('"message"')
    return out[i:i + 300] if i >= 0 else out[-300:]


def main() -> int:
    bad = 0
    for name, c in samples():
        if "--dump" in sys.argv:
            print("== %s\n%s" % (name, json.dumps(c, ensure_ascii=False, indent=1)))
            continue
        err = check(c)
        print("%-12s %s" % (name, "OK" if not err else "FAIL " + err))
        bad += bool(err)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

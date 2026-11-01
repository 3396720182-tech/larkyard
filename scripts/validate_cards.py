#!/usr/bin/env python3
"""把 ticketd 会发的每种卡片都渲染一遍，交给飞书 cardkit「创建卡片实体」接口校验结构。

不发任何消息（只创建一个不挂在任何会话上的卡片实体）。改了 templates.py / card.py 的卡片结构后跑一次：
    /usr/bin/python3 scripts/validate_cards.py            # 校验
    /usr/bin/python3 scripts/validate_cards.py --dump     # 只打印 JSON，不调接口
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
    core.add_report(con, tid, "ready", "做好了", [wd + "/结果.md"])
    con.execute("UPDATE outbox SET status='sent'")
    out.append(("结果卡 带稿子按钮", templates.with_doc_links(
        templates.result_card(tid, t()["title"], wd, "ready", "一句话结果", [wd + "/稿子.md"], None, ["读代码"], 600),
        [("稿子.md", "https://example.feishu.cn/docx/x"), ("表.xlsx", "https://example.feishu.cn/sheets/y")])))
    out.append(("收起", card.render(t(), card.view(con, t(), now), [], now)))
    con.execute("UPDATE outbox SET status='dead'")
    out.append(("终态直写", card.render(t(), card.view(con, t(), now), [], now)))
    return out


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

"""发给飞书的所有文案和卡片（Card 2.0）的拼装。

- worker 可控的文字（摘要、阶段、问题、产物）一律放 plain_text，不经 Markdown 解析。
- 只用实测过的卡片能力（div / hr / button、header 的 title / subtitle / template）。
  例外：结果卡的 img（T0005）尚未经 validate_cards.py 校验；飞书若拒收，发件箱会改发文字兜底。
  改卡片结构后跑 `scripts/validate_cards.py`，用飞书 cardkit 接口校验（不发消息）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import List, Optional

# 终态：(emoji, 文字, header 颜色)
FINAL = {
    "ready": ("✅", "完成", "green"),
    "blocked": ("⚠️", "需要你决定", "orange"),
    "waiting": ("⏳", "等待外部条件", "yellow"),
    "failed": ("❌", "失败", "red"),
    "dropped": ("⏭️", "无需处理", "grey"),
}
LABELS = {k: "%s %s" % (e, w) for k, (e, w, _) in FINAL.items()}
LABELS["progress"] = "❓ 有问题问你"

_URL = re.compile(r"^https?://[^\s<>\"]+$")

# 结果卡里直接显示的图片产物。占位元素带 UPLOAD_KEY，由发件箱在发送前上传换成 img_key；
# 换不成的占位会被整个删掉（产物路径仍在 📎 列表里），所以占位字段永远不会原样发给飞书。
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_CARD_IMAGES = 6
UPLOAD_KEY = "ticketd_upload"


def short(s, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"


def clip(s, n: int) -> str:
    """和 short 一样截断，但保留换行（多行摘要更好读），并压掉连续空行。"""
    s = re.sub(r"\n\s*\n+", "\n", "\n".join(" ".join(x.split()) for x in str(s).strip().splitlines()))
    return s if len(s) <= n else s[:n] + "…"


def hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def dur(sec: float) -> str:
    m = int(sec // 60)
    if m < 1:
        return "不到 1 分钟"
    if m < 60:
        return "%d 分钟" % m
    return "%d 小时 %d 分钟" % (m // 60, m % 60) if m % 60 else "%d 小时" % (m // 60)


def ago(sec: float) -> str:
    return "刚刚" if sec < 60 else dur(sec) + "前"


def nice_path(p: str, workdir: Optional[str] = None) -> str:
    """工单目录里的文件只显示相对路径，家目录换成 ~。"""
    p = str(p)
    if workdir and p.startswith(workdir.rstrip("/") + "/"):
        return p[len(workdir.rstrip("/")) + 1:]
    home = str(Path.home())
    if p == home or p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p


def scope_text(grant_paths: List[str]) -> str:
    return "、".join(nice_path(p) for p in grant_paths) if grant_paths else "仅工单目录"


def goal_text(ack_text: str) -> str:
    """开工句去掉「开工：」前缀，作为卡片上的「目标」。"""
    return re.sub(r"^\s*开工\s*[：:]\s*", "", ack_text or "")


def trail(phases: List[str], done: bool, limit: int = 4) -> str:
    """阶段轨迹：进行中是「✓ 读代码 → ✓ 写代码 → ● 审查」，结束后是「读代码 → 写代码 → 审查」。"""
    if not phases:
        return ""
    shown = [short(p, 15) for p in phases[-limit:]]
    if not done:
        shown = ["✓ " + p for p in shown[:-1]] + ["● " + shown[-1]]
    return ("… → " if len(phases) > limit else "") + " → ".join(shown)


# ---------- 卡片元素 ----------
def text(content: str, size: Optional[str] = None, color: Optional[str] = None) -> dict:
    t = {"tag": "plain_text", "content": content}
    if size:
        t["text_size"] = size
    if color:
        t["text_color"] = color
    return {"tag": "div", "text": t}


def note(content: str, color: str = "grey") -> dict:
    return text(content, size="notation", color=color)


def hr() -> dict:
    return {"tag": "hr"}


def card(summary: str, title: str, subtitle: str, template: str, elements: list) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": short(summary, 60)}},
        "header": {
            "title": {"tag": "plain_text", "content": short(title, 40)},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
        },
        "body": {"elements": elements},
    }


def title_of(ticket_id: str, title: str) -> str:
    return "%s · %s" % (ticket_id, title)


def image_artifacts(artifacts: List[str]) -> List[str]:
    return [a for a in artifacts if not _URL.match(a) and a.lower().endswith(IMAGE_EXTS)][:MAX_CARD_IMAGES]


def result_elements(status: str, summary: str, artifacts: List[str], ask: Optional[str],
                    workdir: Optional[str], images: bool = False) -> list:
    """结果正文：一句话结果、需要你做的事、产物。images=True 时图片产物直接显示（只用于推送的结果卡，
    状态卡原地 PATCH 不经发件箱，不能带待上传的占位）。"""
    els = [text(clip(summary, 1200))]
    if ask:
        if status == "dropped":
            els.append(text("说明：%s。不引用直接发我即可开新单。" % clip(ask, 400).rstrip("。")))
        else:
            els.append(text("👉 需要你：" + clip(ask, 600), color="orange" if status == "blocked" else None))
    files = [a for a in artifacts if not _URL.match(a)]
    links = [a for a in artifacts if _URL.match(a)]
    if images:
        for a in image_artifacts(artifacts):
            name = short(Path(a).name, 60)
            els.append(note(name))
            els.append({"tag": "img", "img_key": "", UPLOAD_KEY: a,
                        "alt": {"tag": "plain_text", "content": name}})
    if files:
        els.append(note("\n".join("📎 " + short(nice_path(a, workdir), 120) for a in files[:8])))
    for u in links[:3]:
        host = re.sub(r"^https?://", "", u).split("/")[0]
        els.append({"tag": "button", "text": {"tag": "plain_text", "content": "打开 " + short(host, 30)},
                    "type": "default", "size": "small",
                    "behaviors": [{"type": "open_url", "default_url": u}]})
    return els


def doc_buttons(links: List[tuple]) -> list:
    """稿子的云文档链接（publish.doc_links 的结果）：每份一个按钮，一键打开。"""
    return [{"tag": "button", "text": {"tag": "plain_text", "content": "📄 打开 " + short(name, 30)},
             "type": "primary_filled" if i == 0 else "default", "size": "small",
             "behaviors": [{"type": "open_url", "default_url": url}]}
            for i, (name, url) in enumerate(links)]


def with_doc_links(card: dict, links: List[tuple]) -> dict:
    """把稿子按钮插到结果卡片正文末尾（分隔线之前）。"""
    els = card["body"]["elements"]
    at = next((i for i, e in enumerate(els) if e.get("tag") == "hr"), len(els))
    els[at:at] = doc_buttons(links)
    return card


def result_card(ticket_id: str, title: str, workdir: Optional[str], status: str, summary: str,
                artifacts: List[str], ask: Optional[str], phases: List[str],
                elapsed: Optional[float]) -> dict:
    """终态汇报的结果卡：本轮有状态卡时直接覆盖到状态卡上；没有时作为一条新消息推给用户。"""
    emoji, word, color = FINAL[status]
    sub = "%s %s" % (emoji, word)
    if elapsed is not None:
        sub += " · 用时 %s" % dur(elapsed)
    els = result_elements(status, summary, artifacts, ask, workdir, images=True)
    els.append(hr())
    els.append(note(("过程：%s · " % trail(phases, done=True, limit=6) if phases else "") + "引用此卡可追问"))
    return card("%s %s %s：%s" % (emoji, ticket_id, word, short(summary, 40)),
                title_of(ticket_id, title), sub, color, els)


# ---------- 文字消息 ----------
def ack_message(ticket_id: str, title: str) -> str:
    """`ticket new` 的输出，由 bridge 原样回复。目标、可写目录都在状态卡上，这里不重复。"""
    return "🎫 已开单 %s「%s」，进度看下方状态卡" % (ticket_id, title)


def report_message(ticket_id: str, status: str, summary: str,
                   artifacts: List[str], ask: Optional[str]) -> Optional[str]:
    """汇报对应的文字消息；返回 None 表示不推消息。终态汇报实际发的是结果卡片，这段文字是卡片发不出去时的兜底。
    progress 只刷新状态卡，不发消息；除非带了 --ask（需要用户决策时才主动推）。"""
    if status == "progress" and not ask:
        return None
    if status == "dropped" and not ask:
        return None
    emoji, word = LABELS[status].split(" ", 1)
    lines = ["%s %s %s：%s" % (emoji, ticket_id, word, summary)]
    for a in artifacts:
        lines.append("📎 " + nice_path(a))
    if ask:
        if status == "dropped":
            lines.append("说明：%s。不引用直接发我即可开新单。" % ask.rstrip("。"))
        else:
            lines.append("👉 需要你：" + ask)
    return "\n".join(lines)


def ping_message(ticket_id: str, status: str, summary: str, ask: Optional[str]) -> Optional[str]:
    """结果已覆盖到状态卡上（编辑不推送提醒）时的一行提醒：只在需要用户回答时发（blocked，或带 --ask 且不是 dropped）。"""
    if status == "dropped" or not (ask or status == "blocked"):
        return None
    emoji, word = LABELS[status].split(" ", 1)
    if status == "blocked":
        head = "%s %s 需要你决定：%s" % (emoji, ticket_id, short(ask or summary, 300))
    else:
        head = "%s %s %s · 👉 需要你：%s" % (emoji, ticket_id, word, short(ask, 300))
    return head + "\n详情已更新在上方状态卡，引用这条回复即可"


def stall_message(ticket_id: str, minutes: int, last_step: Optional[str]) -> str:
    lines = ["⏱️ %s 疑似卡住：已 %d 分钟没有新动作" % (ticket_id, minutes)]
    if last_step:
        lines.append("最近：%s" % short(last_step, 80))
    lines.append("可能只是在跑耗时命令。要停就说「停 %s」，不用管就继续等。" % ticket_id)
    return "\n".join(lines)

"""发给飞书的所有文案和卡片（Card 2.0）的拼装。

- worker 可控的文字（摘要、阶段、问题、产物）一律放 plain_text，不经 Markdown 解析。
- 只用实测过的卡片能力（div / hr / button、header 的 title / subtitle / template）。
  例外：结果卡的 img（T0005）、视频块的 interactive_container（T0010）；改了要跑 validate_cards.py。飞书若拒收，发件箱会改发文字兜底。
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
# 换不成的占位换成一行灰字「（图 N 未能显示）」（路径仍在本机文件列表里），所以占位字段永远不会原样发给飞书。
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_CARD_IMAGES = 6
UPLOAD_KEY = "ticketd_upload"

# 视频产物（T0010）：飞书卡片没有视频组件，不能内嵌播放。结果卡里放 VIDEO_KEY 占位，发件箱发卡前把视频
# 以用户身份传到云空间拿链接，换成「可点击的封面 + ▶ 播放视频按钮」（点开走飞书云空间的播放器）；
# 上传不成才退回：封面照常显示，视频在卡片之后另发一条视频消息。封面约定：与视频同目录同名的图片。
VIDEO_EXTS = (".mp4",)
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_VIDEOS = 2
VIDEO_KEY = "ticketd_video"
VIDEO_URL_KEY = "ticketd_video_url"  # 已经传好的云空间链接（封面瞬时失败时记在占位上，重试不再重传视频）


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


def video_artifacts(artifacts: List[str]) -> List[str]:
    """产物里的视频。和视频同名的图片都算封面候选：卡片只显示实际用上的那张，其余同名图不单独显示。"""
    return [a for a in artifacts if not _URL.match(a) and a.lower().endswith(VIDEO_EXTS)][:MAX_VIDEOS]


def cover_candidates(video: str) -> List[str]:
    """视频的封面候选：同目录同名、扩展名换成图片（视频-720p.mp4 → 视频-720p.jpg …）。"""
    stem = video[: -len(Path(video).suffix)] if Path(video).suffix else video
    return [stem + e for e in COVER_EXTS]


def image_artifacts(artifacts: List[str]) -> List[str]:
    """卡片里直接显示的图片；已作为视频封面显示的同名图不再重复。"""
    covers = {c.lower() for v in video_artifacts(artifacts) for c in cover_candidates(v)}  # macOS 文件名不分大小写
    return [a for a in artifacts if not _URL.match(a) and a.lower().endswith(IMAGE_EXTS)
            and a.lower() not in covers][:MAX_CARD_IMAGES]


_SENTENCE_END = re.compile(r"[。！？!?；;\n]")


def split_summary(summary: str):
    """摘要拆成「结论」（第一句，卡片里放大显示）和「细节」（其余，正常字号）。"""
    s = clip(summary, 1200)
    m = _SENTENCE_END.search(s)
    if not m or m.start() > 120:
        return (s, "") if len(s) <= 120 else (short(s, 60), s)
    head = s[:m.start()].strip()
    rest = s[m.end():].strip()
    return (head or s, rest) if head else (s, "")


def callout(label: str, body: str, color: str) -> dict:
    """浅色底的提示块（需要你做的事）。"""
    return {"tag": "column_set", "flex_mode": "none", "columns": [{
        "tag": "column", "width": "weighted", "weight": 1, "background_style": color + "-50",
        "padding": "8px 12px 8px 12px", "vertical_spacing": "4px",
        "elements": [text(label, size="notation", color=color), text(body)]}]}


def _img(path: str, name: str, **extra) -> dict:
    return dict({"tag": "img", "img_key": "", UPLOAD_KEY: path, "alt": {"tag": "plain_text", "content": name}}, **extra)


def _badge(n: int) -> dict:
    """图下方居中的圆形编号（内容是我们自己生成的数字，不含 worker 文字，可以用 markdown）。"""
    return {"tag": "markdown", "content": "<number_tag>%d</number_tag>" % n, "text_align": "center"}


def image_grid(paths: List[str], grid: bool = False) -> list:
    """画廊：单张图做主图（不裁剪、圆角、可点开放大）；多张图两列网格，每张不裁剪、下面一个圆形编号，
    方便回复「选 2」；奇数张补空列保持等宽。grid=True（卡里已经有视频做主角）时，图片作为三列小缩略图跟在后面。"""
    if grid:
        cols = [{"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [_img(pth, short(Path(pth).name, 60), scale_type="crop_center", corner_radius="6px")]}
                for pth in paths[:3]]
        while len(cols) < 3:
            cols.append({"tag": "column", "width": "weighted", "weight": 1, "elements": []})
        return [{"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": cols}]
    if len(paths) == 1:
        return [_img(paths[0], short(Path(paths[0]).name, 60), scale_type="fit_horizontal", corner_radius="8px")]
    rows = []
    for i in range(0, len(paths), 2):
        cols = []
        for j, pth in enumerate(paths[i:i + 2], start=i + 1):
            cols.append({"tag": "column", "width": "weighted", "weight": 1, "vertical_spacing": "4px",
                         "elements": [_img(pth, "图 %d" % j, scale_type="fit_horizontal", corner_radius="6px"),
                                      _badge(j)]})
        if len(cols) == 1:
            cols.append({"tag": "column", "width": "weighted", "weight": 1, "elements": []})
        rows.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": cols})
    return rows


def details_panel(rest: str, files: List[str], workdir: Optional[str]) -> dict:
    """收起的「详情」：摘要里结论以外的话 + 本机文件路径（在飞书里点不开，只给需要时看）。"""
    els = []
    if rest:
        els.append(text(rest))
    if files:
        els.append(note("📎 本机文件\n" + "\n".join(short(nice_path(a, workdir), 120) for a in files[:8])
                        + ("\n…" if len(files) > 8 else "")))
    title = "详情" if rest else "📎 本机文件（%d）" % len(files)
    return {"tag": "collapsible_panel", "expanded": False, "vertical_spacing": "4px",
            "header": {"title": {"tag": "plain_text", "content": title, "text_size": "notation",
                                 "text_color": "grey"}},
            "elements": els}


def run_elements(run: dict) -> list:
    """沙箱外执行申请：完整脚本（worker 写的，plain_text）放在默认展开的面板里，用户看过再批准。"""
    head = "🖥️ 申请在沙箱外执行（R%s · %s）" % (run["id"], short(run["path"], 60))
    return [{"tag": "collapsible_panel", "expanded": True, "vertical_spacing": "4px",
             "header": {"title": {"tag": "plain_text", "content": head, "text_size": "notation"}},
             "elements": [text(run["script"].rstrip()[:9000]),  # 原样显示（含缩进），汇报时已限制在 8KB 以内
                          note("回复「执行」才会执行：由 ticketd 在工单目录里、沙箱外运行这段脚本（最长 %d 分钟），"
                               "输出交回 worker 接着做。脚本里调用的其他文件以批准时工单目录里的为准。"
                               % max(1, int(run.get("timeout", 1800) // 60)))]}]


def _ask_element(status: str, ask: str) -> dict:
    if status == "dropped":
        return text("说明：%s。不引用直接发我即可开新单。" % clip(ask, 400).rstrip("。"), color="grey")
    return callout("👉 需要你", clip(ask, 600), "orange" if status == "blocked" else "blue")


def _link_buttons(links: List[str]) -> list:
    out = []
    for u in links[:3]:
        host = re.sub(r"^https?://", "", u).split("/")[0]
        out.append({"tag": "button", "text": {"tag": "plain_text", "content": "打开 " + short(host, 30)},
                    "type": "default", "size": "small", "behaviors": [{"type": "open_url", "default_url": u}]})
    return out


def result_elements(status: str, summary: str, artifacts: List[str], ask: Optional[str],
                    workdir: Optional[str], images: bool = False, run: Optional[dict] = None) -> list:
    """结果正文，所有状态通用。worker 可控的文字一律 plain_text，不经 Markdown 解析。
    有图片/视频（images=True，只用于推送的结果卡）→ 画廊：媒体占主体在最上面 → 一行结论 → 需要你（色块）
      → 链接 → 收起的详情（其余文字 + 本机文件）。
    没有媒体 → 文档：结论（放大）→ 需要你 → 细节 → 链接 → 收起的本机文件。
    状态卡原地 PATCH 不经发件箱，不能带待上传的占位，所以 images=False 时不显示媒体。"""
    head, rest = split_summary(summary)
    files = [a for a in artifacts if not _URL.match(a)]
    links = [a for a in artifacts if _URL.match(a)]
    videos = video_artifacts(artifacts) if images else []
    pics = image_artifacts(artifacts) if images else []
    if videos or pics:
        els = [dict(note("🎬 " + short(Path(v).name, 60)), **{VIDEO_KEY: v}) for v in videos]
        if pics:
            els += image_grid(pics, grid=bool(videos))
        headline = text(head, size="heading-4")
        headline["text"]["lines"] = 2
        els.append(headline)
        if ask:
            els.append(_ask_element(status, ask))
        if run:
            els += run_elements(run)
        els += _link_buttons(links)
        if rest or files:
            els.append(details_panel(rest, files, workdir))
        return els
    els = [text(head, size="heading-4")]
    if ask:
        els.append(_ask_element(status, ask))
    if run:
        els += run_elements(run)
    if rest:
        els.append(text(rest))
    els += _link_buttons(links)
    if files:
        els.append(details_panel("", files, workdir))
    return els


def doc_buttons(links: List[tuple]) -> list:
    """稿子的云文档链接（publish.doc_links 的结果）：每份一个按钮，一键打开。"""
    return [{"tag": "button", "text": {"tag": "plain_text", "content": "📄 打开 " + short(name, 30)},
             "type": "primary_filled" if i == 0 else "default", "size": "small",
             "behaviors": [{"type": "open_url", "default_url": url}]}
            for i, (name, url) in enumerate(links)]


def with_doc_links(card: dict, links: List[tuple]) -> dict:
    """把稿子按钮插到结果卡片的链接按钮之后：收起的详情面板之前；没有面板时在页脚（最后一行过程轨迹）之前。"""
    els = card["body"]["elements"]
    at = next((i for i, e in enumerate(els) if e.get("tag") in ("collapsible_panel", "hr")), max(len(els) - 1, 0))
    els[at:at] = doc_buttons(links)
    return card


def with_doc_failures(card: dict, fails: List[tuple], workdir: Optional[str] = None) -> dict:
    """稿子没能导入云文档时，在稿子按钮的位置写一行灰字说明原因和本机路径（issue #12）。"""
    if not fails:
        return card
    lines = ["📄 %s 没能导入云文档：%s。本机文件：%s" % (short(name, 30), short(reason, 80), short(nice_path(path, workdir), 80))
             for name, path, reason in fails]
    els = card["body"]["elements"]
    at = next((i for i, e in enumerate(els) if e.get("tag") in ("collapsible_panel", "hr")), max(len(els) - 1, 0))
    els[at:at] = [note("\n".join(lines))]
    return card


def cost_hint(ticket_id: str, session_cost: float, threshold: float) -> Optional[str]:
    """会话很长时的成本提示（T0005：一张单 $88，后期每次追问 $5～8，因为每轮都按整段上下文计费）。"""
    if not threshold or session_cost < threshold:
        return None
    return ("💰 这张单的会话已累计约 $%.0f，之后每次追问都按整段上下文计费。换了目标就开新单；"
            "同一件事要接着做，可以说「换新会话重来 %s」（工单目录和历次追问会带上）" % (session_cost, ticket_id))


def result_card(ticket_id: str, title: str, workdir: Optional[str], status: str, summary: str,
                artifacts: List[str], ask: Optional[str], phases: List[str],
                elapsed: Optional[float], run: Optional[dict] = None) -> dict:
    """终态汇报的结果卡：本轮有状态卡时直接覆盖到状态卡上；没有时作为一条新消息推给用户。"""
    emoji, word, color = FINAL[status]
    sub = "%s %s" % (emoji, word)
    if elapsed is not None:
        sub += " · 用时 %s" % dur(elapsed)
    els = result_elements(status, summary, artifacts, ask, workdir, images=True, run=run)
    els.append(note(("过程：%s · " % trail(phases, done=True, limit=6) if phases else "") + "引用此卡可追问"))
    c = card("%s %s %s：%s" % (emoji, ticket_id, word, short(summary, 40)),
             title_of(ticket_id, title), sub, color, els)
    c["body"].update({"padding": "12px 12px 16px 12px", "vertical_spacing": "12px"})
    return c


# ---------- 文字消息 ----------
def ack_message(ticket_id: str, title: str) -> str:
    """`ticket new` 的输出，由 bridge 原样回复。目标、可写目录都在状态卡上，这里不重复。"""
    return "🎫 已开单 %s「%s」，进度看下方状态卡" % (ticket_id, title)


def video_elements(video: str, url: Optional[str], cover_key: Optional[str]) -> list:
    """卡片里的一个视频（画廊风）。url 有值：整块可点——大封面（不裁剪、圆角）+ 通栏「▶ 播放视频」按钮，
    点封面或按钮都打开云空间播放；url 为空（上传云空间失败）：封面 + 一行说明，视频另发一条消息。"""
    name = short(Path(video).name, 60)
    cover = ({"tag": "img", "img_key": cover_key, "scale_type": "fit_horizontal", "corner_radius": "8px",
              "alt": {"tag": "plain_text", "content": name + " 封面"}} if cover_key else None)
    if not url:
        els = [cover] if cover else []
        return els + [note("🎬 %s · 视频在下一条消息，点开播放" % name)]
    open_url = [{"type": "open_url", "default_url": url}]
    inner = [dict(cover, preview=False)] if cover else []
    inner.append({"tag": "button", "text": {"tag": "plain_text", "content": "▶ 播放视频"}, "type": "primary_filled",
                  "size": "medium", "width": "fill", "behaviors": open_url})
    return [{"tag": "interactive_container", "width": "fill", "padding": "0px", "vertical_spacing": "8px",
             "behaviors": open_url, "elements": inner}]


def video_fallback(ticket_id: str, video: str, workdir: Optional[str] = None) -> str:
    """视频消息发不出去时的兜底文字。"""
    return "🎬 %s 的视频没能直接发出，文件在：%s" % (ticket_id, nice_path(video, workdir))


def report_message(ticket_id: str, status: str, summary: str,
                   artifacts: List[str], ask: Optional[str], run: Optional[dict] = None) -> Optional[str]:
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
    if run:
        lines.append("🖥️ 申请在沙箱外执行（R%s · %s）：\n%s" % (run["id"], run["path"], run["script"].rstrip()[:9000]))
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


def api_error_message(ticket_id: str, error: str, account: Optional[str], bg: Optional[str]) -> str:
    """本轮以模型接口报错结束、但后台任务还在跑（进程要等任务结束才退出）时推的提醒。"""
    lines = ["⚠️ %s 模型接口报错%s：%s" % (ticket_id, "（%s）" % account if account else "", short(error, 200))]
    later = ("来不及也没关系，这一轮结束后会定时自动重试，也可以引用回复「继续」。" if account
             else "否则这一轮会以失败结束，之后引用回复「继续」即可。")
    lines.append("后台任务还在跑%s。赶在它结束前处理好，worker 被唤醒后就能接着做；%s"
                 % ("（%s）" % short(bg, 60) if bg else "", later))
    return "\n".join(lines)


def stall_message(ticket_id: str, minutes: int, last_step: Optional[str]) -> str:
    lines = ["⏱️ %s 疑似卡住：已 %d 分钟没有新动作" % (ticket_id, minutes)]
    if last_step:
        lines.append("最近：%s" % short(last_step, 80))
    lines.append("可能只是在跑耗时命令。要停就说「停 %s」，不用管就继续等。" % ticket_id)
    return "\n".join(lines)

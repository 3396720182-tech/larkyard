"""用量统计（采集清单 c3 起才上报；openspec add-usage-report）。

只产出数字和写死的代号，不经手任何文字：
- 大类：每张单建单时归进 7 个大类之一（主会话派单时用 --cat 给；没给或不认识时，本机按标题和原话的关键词判断），
  存在 tickets.cat。上报「哪天、哪一类、几单、做成几单」。
- 能力：worker 调用控制台下发的技能脚本时（命令里出现技能目录），按过程日志里的工具调用和结果记一次
  「用了 / 成功 / 失败归类码」，存 cap_calls。归类码只有写死的几种（exit_<退出码>、tool_error）。
- 两个数：最近连续没做成几单；正在跑的单里最久多少分钟没有新动作。
"""
from __future__ import annotations

import re
import time
from typing import Optional

from . import skills

CATS = ("media", "asset", "doc", "research", "sheet", "code", "other")
CAT_TEXT = {"media": "生图生视频", "asset": "找素材", "doc": "写文档", "research": "查资料", "sheet": "做表格",
            "code": "写代码", "other": "其他"}

# 关键词判断（主会话没给 --cat 时的兜底）：按顺序，先命中的算。只在本机看标题和原话，结果只留代号。
_RULES = (
    ("media", ("生图", "出图", "生成图", "画一张", "画个", "海报", "插画", "配图", "封面", "头像", "壁纸", "分镜", "关键帧",
               "生视频", "出视频", "短片", "动起来", "图生视频", "抠图", "修图", "改图", "p图", "视频生成", "图片生成",
               "image", "video")),
    ("asset", ("素材", "空镜", "镜头", "找片", "找视频", "找照片", "素材库", "footage")),
    ("sheet", ("表格", "excel", "xlsx", "csv", "多维表", "透视", "统计表", "报表", "汇总表")),
    ("doc", ("视频脚本", "拍摄脚本", "口播稿", "脚本稿")),
    ("code", ("代码", "脚本", "bug", "报错", "接口", "部署", "编程", "程序", "仓库", "pr ", "commit", "python", "函数",
              "前端", "后端", "数据库", "sql")),
    ("research", ("查一下", "查查", "调研", "搜一下", "搜索", "找资料", "查资料", "了解一下", "对比", "竞品", "资讯",
                  "新闻", "是什么", "怎么回事")),
    ("doc", ("文档", "写一篇", "文案", "稿子", "大纲", "总结", "纪要", "周报", "方案", "翻译", "润色", "改写", "邮件",
             "ppt", "演讲稿", "报告")),
)


def classify(title: str, request_text: str) -> str:
    text = ("%s\n%s" % (title or "", request_text or "")).lower()
    for cat, words in _RULES:
        if any(w in text for w in words):
            return cat
    return "other"


def norm_cat(cat: Optional[str]) -> Optional[str]:
    c = (cat or "").strip().lower()
    return c if c in CATS else None


# ---------- 能力调用（过程日志观测） ----------
CAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CODE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_EXIT_RE = re.compile(r"[Ee]xit code[: ]+([0-9]{1,3})\b")


_READERS = ("cat", "ls", "head", "tail", "less", "more", "sed", "grep", "rg", "find", "wc", "file", "stat")


def skill_of_command(command: str, installed: Optional[list] = None) -> Optional[str]:
    """Bash 命令里运行了哪个控制台下发的技能的脚本：命令里出现 <技能根>/current/skills/<名字>/(scripts|bin)/…，
    名字在本机装着的技能里，且命令不是查看文件（cat / ls 等）。没有返回 None。"""
    if not isinstance(command, str) or "skills" not in command:
        return None
    first = command.strip().split(None, 1)[0].rsplit("/", 1)[-1] if command.strip() else ""
    if first in _READERS:
        return None
    names = set(skills.installed() if installed is None else installed)
    for r in {str(skills.root()), str(skills.root().resolve())}:
        for m in re.finditer(re.escape(r.rstrip("/")) + r"/current/skills/([a-z0-9][a-z0-9-]{0,39})/(scripts|bin)/",
                             command):
            if m.group(1) in names:
                return m.group(1)
    return None


def tool_calls(ev: dict) -> list:
    """assistant 事件里调用了技能脚本的 Bash 工具调用：[(tool_use_id, 技能名)]。"""
    if ev.get("type") != "assistant":
        return []
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if isinstance(content, list):
        for c in content:
            if not (isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash"):
                continue
            inp = c.get("input") if isinstance(c.get("input"), dict) else {}
            cap = skill_of_command(inp.get("command"))
            if cap and isinstance(c.get("id"), str):
                out.append((c["id"], cap))
    return out


def tool_results(ev: dict) -> list:
    """user 事件里的工具结果：[(tool_use_id, 成功?, 失败归类码)]。归类码只从退出码推，不带原文。"""
    if ev.get("type") != "user":
        return []
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if isinstance(content, list):
        for c in content:
            if not (isinstance(c, dict) and c.get("type") == "tool_result" and isinstance(c.get("tool_use_id"), str)):
                continue
            if not c.get("is_error"):
                out.append((c["tool_use_id"], True, None))
                continue
            body = c.get("content")
            if isinstance(body, list):
                body = " ".join(x.get("text", "") for x in body if isinstance(x, dict) and isinstance(x.get("text"), str))
            m = _EXIT_RE.search(body if isinstance(body, str) else "")
            out.append((c["tool_use_id"], False, ("exit_%s" % m.group(1)) if m else "tool_error"))
    return out


def record_call(con, ticket_id: str, cap: str, ok: bool, code: Optional[str], at: Optional[float] = None) -> None:
    if not CAP_RE.match(cap or ""):
        return
    code = code if (code and CODE_RE.match(code)) else (None if ok else "tool_error")
    con.execute("INSERT INTO cap_calls(ticket_id, cap, ok, code, created_at) VALUES(?,?,?,?,?)",
                (ticket_id, cap, 1 if ok else 0, None if ok else code, time.time() if at is None else at))


# ---------- 心跳里的 usage ----------
DAYS = 2                 # 每次报最近两天（本地日期）的累计数，控制台按天覆盖
STREAK_LOOKBACK = 50     # 算「连续没做成」最多往回看几张单
STREAK_MAX = 10000
STUCK_MAX = 10000000
DONE = "ready"
FAILED = ("failed",)
NEUTRAL = ("blocked", "waiting", "dropped")  # 要用户决定、等外部、不用做：不算做成，也不算没做成


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_start(ts: float) -> float:
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _cats(con, since: float) -> list:
    rows = con.execute(
        "SELECT t.created_at, t.cat, t.title, t.request_text, "
        "EXISTS(SELECT 1 FROM rounds r WHERE r.ticket_id=t.id AND r.outcome=?) AS ok "
        "FROM tickets t WHERE t.created_at>=?", (DONE, since)).fetchall()
    agg = {}
    for r in rows:
        cat = norm_cat(r["cat"]) or classify(r["title"], r["request_text"])
        k = (day_of(r["created_at"]), cat)
        n, ok = agg.get(k, (0, 0))
        agg[k] = (n + 1, ok + (1 if r["ok"] else 0))
    return [{"day": d, "cat": c, "n": min(n, 10000), "ok": min(ok, n, 10000)}
            for (d, c), (n, ok) in sorted(agg.items())][:14]


def _caps(con, since: float) -> list:
    agg = {}
    for r in con.execute("SELECT cap, ok, code, created_at FROM cap_calls WHERE created_at>=?", (since,)):
        if not CAP_RE.match(r["cap"] or ""):
            continue
        k = (day_of(r["created_at"]), r["cap"])
        a = agg.setdefault(k, {"n": 0, "ok": 0, "fails": {}})
        a["n"] += 1
        if r["ok"]:
            a["ok"] += 1
        else:
            code = r["code"] if CODE_RE.match(r["code"] or "") else "tool_error"
            a["fails"][code] = a["fails"].get(code, 0) + 1
    out = []
    for (d, cap), a in sorted(agg.items()):
        fails = sorted(a["fails"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        out.append({"day": d, "cap": cap, "n": min(a["n"], 100000), "ok": min(a["ok"], 100000),
                    "fails": [{"code": c, "n": min(n, 100000)} for c, n in fails]})
    return out[-20:]


def streak_fail(con, since: float = 0.0) -> int:
    """最近连续没做成的单数：按每张单最后一次有结论的那一轮（做成 / 失败）从新到旧数，遇到做成的停。
    要用户决定、等外部、不用做的单跳过，不打断也不计数。"""
    rows = con.execute(
        "SELECT ticket_id, outcome, MAX(ended_at) AS at FROM rounds WHERE outcome IN (%s) AND ended_at>=? "
        "GROUP BY ticket_id ORDER BY at DESC LIMIT ?" % ",".join("?" * (1 + len(FAILED) + len(NEUTRAL))),
        (DONE,) + FAILED + NEUTRAL + (since, STREAK_LOOKBACK)).fetchall()
    # GROUP BY 里 MAX(ended_at) 配的 outcome 在 SQLite 里就是那一行的（bare column 规则）
    n = 0
    for r in rows:
        if r["outcome"] == DONE:
            break
        if r["outcome"] in FAILED:
            n += 1
    return min(n, STREAK_MAX)


def stuck_min(con, now: float) -> int:
    """正在跑的单里，最久多少分钟没有新动作（等后台任务的不算）。"""
    worst = 0.0
    for r in con.execute("SELECT last_action_at, started_at FROM tickets WHERE state='running' AND bg_wait IS NULL"):
        since = max(r["last_action_at"] or 0, r["started_at"] or 0)
        if since:
            worst = max(worst, now - since)
    return min(int(worst // 60), STUCK_MAX)


def build(con, now: Optional[float] = None, since: float = 0.0) -> dict:
    """心跳里的 usage。since：同意 c3 的时刻，之前建的单、之前的调用、之前结束的轮次都不算。"""
    now = time.time() if now is None else now
    start = max(_day_start(now) - (DAYS - 1) * 86400, since)
    return {"cats": _cats(con, start), "caps": _caps(con, start), "streak_fail": streak_fail(con, since),
            "stuck_min": stuck_min(con, now)}

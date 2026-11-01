from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

from . import config, safefs, templates

ASSIGNMENT = """# 工单 {id}：{title}

- 工单目录（当前 cwd）：{workdir}
- 可写目录：{scope}
- 结果文件：{workdir}/结果.md

## 用户原话
{request}

## 派单备注（只是背景，不是需求）
{note}"""

FOLLOWUP = """## 用户追问（消息 {mid}）
{text}

先判断这条是否属于本工单范围：属于就处理；不属于就执行
`ticket report --status dropped --summary "<一句话>" --ask "<为什么不属于本单>"` 然后结束本轮。"""

REMINDER = """---
本轮结束前必须执行一次：
`ticket report --status <ready|blocked|failed|dropped|waiting> --summary "<一句话，面向用户>" [--artifact <路径或链接>] [--ask "<一个具体问题>"]`"""

TRANSIENT_MARKERS = ("overloaded", "timed out", "timeout",
                     "econnreset", "econnrefused", "etimedout", "socket hang up", "connection error",
                     "fetch failed", "network error")
TRANSIENT_RE = re.compile(r"\b(?:api error|error|status|http)\b[^\n]{0,20}?\b(?:408|429|5\d\d)\b")


_short = templates.short


def _base(p) -> str:
    p = str(p or "").rstrip("/")
    return p.rsplit("/", 1)[-1] or p


def _program(command) -> str:
    """命令行里真正干活的程序名（跳过 cd），不展示参数和路径。"""
    for seg in re.split(r"&&|\|\||;|\|", str(command or "")):
        words = seg.split()
        while words and "=" in words[0] and not words[0].startswith(("=", "-")):
            words = words[1:]  # 跳过 FOO=bar 这类环境变量前缀
        if words and words[0] != "cd":
            return _short(_base(words[0]), 30)
    return "命令"


def step_label(name, inp) -> str:
    """把一次工具调用说成人话，给状态卡的「最近」一行用（不展示原始命令行）。"""
    inp = inp if isinstance(inp, dict) else {}
    name = str(name or "")
    if name == "Bash":
        d = inp.get("description")
        return _short(d, 40) if d else "运行 " + _program(inp.get("command", ""))
    if name == "Read":
        return "读 " + _base(inp.get("file_path"))
    if name in ("Edit", "MultiEdit"):
        return "改 " + _base(inp.get("file_path"))
    if name == "Write":
        return "写 " + _base(inp.get("file_path"))
    if name == "NotebookEdit":
        return "改 " + _base(inp.get("notebook_path"))
    if name in ("Grep", "Glob"):
        return "搜索 " + _short(inp.get("pattern", ""), 30)
    if name == "WebSearch":
        return "上网搜 " + _short(inp.get("query", ""), 30)
    if name == "WebFetch":
        return "看网页 " + _short(re.sub(r"^https?://", "", str(inp.get("url", ""))).split("/")[0], 30)
    if name in ("Agent", "Task"):
        return "派子代理：" + _short(inp.get("description") or inp.get("prompt") or "", 30)
    if name == "Skill":
        return "用技能 " + _short(inp.get("skill", ""), 30)
    if name == "TodoWrite":
        return "更新待办"
    if name.startswith("mcp__"):
        return "调用 " + _short(name.split("__")[-1], 30)
    return "调用 " + _short(name, 30)


def last_step(d: dict) -> Optional[str]:
    """assistant 事件里最后一个工具调用的人话描述；没有工具调用返回 None（纯文字不算一步）。"""
    if d.get("type") != "assistant":
        return None
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    step = None
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                step = step_label(c.get("name"), c.get("input"))
    return step


def compose_card(ticket, grant_paths: List[str], inputs, include_assignment: bool) -> str:
    parts = []
    if include_assignment:
        parts.append(ASSIGNMENT.format(
            id=ticket["id"], title=ticket["title"], workdir=ticket["workdir"],
            scope="、".join([ticket["workdir"]] + list(grant_paths)) if grant_paths
            else templates.scope_text([]) + "（%s）" % ticket["workdir"],
            request=ticket["request_text"], note=ticket["router_note"] or "无"))
    for i in inputs:
        if i["kind"] == "assignment":
            continue
        if i["kind"] == "followup":
            parts.append(FOLLOWUP.format(mid=i["source_message_id"], text=i["text"]))
        else:
            parts.append("## 系统通知（%s）\n%s" % (i["kind"], i["text"]))
    parts.append(REMINDER)
    return "\n\n".join(parts)


def build_command(ticket, settings_path, prompt_text: str) -> List[str]:
    cmd = [config.claude_bin(), "-p", "--setting-sources", "user"]
    if ticket["session_started"]:
        cmd += ["--resume", ticket["session_id"]]
    else:
        cmd += ["--session-id", ticket["session_id"]]
    cmd += ["--output-format", "stream-json", "--verbose", "--permission-mode", "dontAsk",
            "--settings", str(settings_path), "--append-system-prompt", prompt_text]
    if config.model():
        cmd += ["--model", config.model()]
    return cmd


def worker_env(ticket) -> dict:
    env = dict(os.environ)
    env["TICKETD_TICKET"] = ticket["id"]
    env["TICKETD_SPOOL"] = str(Path(ticket["workdir"]) / ".reports")
    env["PATH"] = str(config.bin_dir()) + os.pathsep + env.get("PATH", "")
    return env


def _result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def summarize_event(d: dict) -> List[str]:
    t = d.get("type")
    out: List[str] = []
    if t == "system" and d.get("subtype") == "init":
        out.append("会话就绪 %s" % d.get("session_id"))
    elif t == "assistant":
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                target = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                          or inp.get("url") or inp.get("query") or json.dumps(inp, ensure_ascii=False))
                out.append("调用 %s：%s" % (c.get("name"), _short(target)))
            elif c.get("type") == "text" and (c.get("text") or "").strip():
                out.append("说：%s" % _short(c["text"], 200))
    elif t == "user":
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    out.append("工具报错：%s" % _short(_result_text(c.get("content")), 200))
    elif t == "result":
        out.append("本轮结束：%s，用时 %.0fs，被拒操作 %d 次" % (
            d.get("subtype"), (d.get("duration_ms") or 0) / 1000.0, len(d.get("permission_denials") or [])))
    return out


def is_action(d: dict) -> bool:
    """是否算一次「动作」：任何代理（含子代理）发起工具调用、收到工具结果，或后台任务报进度。"""
    t = d.get("type")
    if t == "system":
        return d.get("subtype") == "task_progress"
    if t not in ("assistant", "user"):
        return False
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return False
    want = "tool_use" if t == "assistant" else "tool_result"
    return any(isinstance(c, dict) and c.get("type") == want for c in content)


class StreamTail:
    """增量读取 stream-json 文件，只返回完整的行。"""

    def __init__(self, path, offset: int = 0):
        self.path = Path(path)
        self.offset = offset
        self.buf = b""

    MAX_READ = 1024 * 1024

    def read_events(self) -> List[dict]:
        try:
            fd = safefs.open_read(self.path)  # 工单目录由 worker 控制：不跟随链接、不在 FIFO 上阻塞
        except OSError:
            return []
        if fd is None:
            return []
        try:
            data = safefs.read_at(fd, self.offset, self.MAX_READ)
        finally:
            os.close(fd)
        self.offset += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > self.MAX_READ:
            self.buf = b""  # 超长的半行直接丢弃，防止内存无限增长
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
        return events


def is_transient(exit_code: Optional[int], result_event: Optional[dict], stderr_text: str) -> bool:
    if result_event is not None:
        st = result_event.get("api_error_status")
        if isinstance(st, int) and (st in (408, 429) or st >= 500):
            return True
        if exit_code == 0 and not result_event.get("is_error"):
            return False
    text = stderr_text or ""
    if result_event is not None and result_event.get("is_error"):
        text += "\n" + str(result_event.get("result") or "")
    low = text.lower()
    return any(m in low for m in TRANSIENT_MARKERS) or bool(TRANSIENT_RE.search(low))

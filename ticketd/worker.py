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
- 结果文件：{workdir}/结果.md{sandbox}

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
                     "fetch failed", "network error",
                     # 断网 / DNS：「API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)」（issue #10）
                     "enotfound", "eai_again", "getaddrinfo", "can't reach the api server", "enetunreach", "ehostunreach",
                     # Codex：网关断流 / 请求发不出去（重连 5 次后以 turn.failed 结束）
                     "stream disconnected", "error sending request")
TRANSIENT_RE = re.compile(r"\b(?:api error|error|status|http)\b[^\n]{0,20}?\b(?:408|429|5\d\d)\b")

# 账户级故障：重试不会好，所有工单一起失败，要用户去处理（充值、换 key、登录）。
# 实测（网关）：「Failed to authenticate. API Error: 403 insufficient balance」
ACCOUNT_MARKERS = (
    ("余额不足", ("insufficient balance", "insufficient account balance", "credit balance is too low",
                "insufficient_quota", "exceeded your current quota")),
    ("认证失败", ("failed to authenticate", "invalid api key", "invalid x-api-key", "authentication_error",
                "please run /login", "oauth token has expired")),
)


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
            request=ticket["request_text"], note=ticket["router_note"] or "无", sandbox=_sandbox_line(ticket)))
    for i in inputs:
        if i["kind"] == "assignment":
            continue
        if i["kind"] == "followup":
            parts.append(FOLLOWUP.format(mid=i["source_message_id"], text=i["text"]))
        elif i["kind"] == "run_result":
            parts.append(i["text"])  # 自带标题
        else:
            parts.append("## 系统通知（%s）\n%s" % (i["kind"], i["text"]))
    parts.append(REMINDER)
    return "\n\n".join(parts)


def _sandbox_line(ticket) -> str:
    """Claude worker 的沙箱事实（issue #21：worker 不知道自己的沙箱配了什么，把网络抖动误判成权限问题）。
    Codex 的沙箱由 codex 自己管，这里不写。"""
    if executor_of(ticket) != "claude":
        return ""
    return ("\n- 沙箱：只能写可写目录和临时目录；出网放开；连本机端口、unix socket（如 docker.sock）、"
            "在本机 bind socket 默认被拒，用户可以逐单放开（本机网络、某个 socket 路径）。完整设置：%s"
            % (config.settings_dir() / ("%s.json" % ticket["id"])))


def build_command(ticket, settings_path, prompt_text: str) -> List[str]:
    """旧接口（只给 Claude 用，保留给现有测试）；调度器走 executors.get(...).build_command。"""
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


def user_env(env: dict) -> dict:
    """按配置补上用户自己的环境（issue #19）：TICKETD_WORKER_ENV 白名单里的变量、TICKETD_WORKER_PATH 追加的目录。
    worker 和沙箱外执行的脚本共用。原地修改并返回 env。"""
    for name in config.worker_env_names():
        value = config.worker_env_value(name)
        if value is not None:
            env[name] = value
    extra = config.worker_path()
    if extra:
        have = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(have + [p for p in extra if p not in have])
    return env


def worker_env(ticket) -> dict:
    env = user_env(dict(os.environ))
    env["TICKETD_TICKET"] = ticket["id"]
    env["TICKETD_SPOOL"] = str(Path(ticket["workdir"]) / ".reports")
    env["PATH"] = str(config.bin_dir()) + os.pathsep + env.get("PATH", "")
    if executor_of(ticket) == "codex" and config.codex_home():
        env["CODEX_HOME"] = config.codex_home()
    return env


def executor_of(ticket) -> str:
    try:
        return ticket["executor"] or "claude"
    except (KeyError, IndexError):
        return "claude"


def _result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


def summarize_event(d: dict) -> List[str]:
    t = d.get("type")
    out: List[str] = []
    if t == "system" and d.get("subtype") == "init":
        out.append("会话就绪 %s" % d.get("session_id"))
    elif t == "system" and d.get("subtype") == "notice":  # Codex 的 error 事件（含自动重连提示）
        out.append("提示：%s" % _short(d.get("text"), 200))
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
        # is_error 时 Claude Code 的 subtype 仍是 success，不能照抄（issue #15）
        how = "出错（%s）" % _short(result_error_text(d), 120) if d.get("is_error") else d.get("subtype")
        out.append("本轮结束：%s，用时 %.0fs，被拒操作 %d 次" % (
            how, (d.get("duration_ms") or 0) / 1000.0, len(d.get("permission_denials") or [])))
    return out


def is_action(d: dict) -> bool:
    """是否算一次「动作」：任何代理（含子代理）发起工具调用、收到工具结果，或后台任务报进度、结束。"""
    t = d.get("type")
    if t == "system":
        return d.get("subtype") in ("task_progress", "task_notification")
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


def result_error_text(result_event: Optional[dict]) -> str:
    """本轮 result 事件是错误时的报错原文（模型接口报错等）；不是错误返回空串。"""
    if not isinstance(result_event, dict) or not result_event.get("is_error"):
        return ""
    return str(result_event.get("result") or result_event.get("subtype") or "").strip()


CLIENT_ERROR_RE = re.compile(r"\bapi error:?\s*(?:400|404|413|422)\b")


def is_client_error(text: str) -> bool:
    """模型接口返回的请求错误（4xx，不含限流和账户问题）：请求本身被拒，原样重试不会好。"""
    return bool(text) and bool(CLIENT_ERROR_RE.search(text.lower()))


def account_error(*texts: str) -> Optional[str]:
    """报错里认出账户级故障时返回「余额不足」/「认证失败」，否则 None。余额判断在前（网关常把余额不足报成认证失败）。"""
    low = "\n".join(t for t in texts if t).lower()
    for label, markers in ACCOUNT_MARKERS:
        if any(m in low for m in markers):
            return label
    return None


def bg_task_event(d: dict):
    """后台任务事件：返回 ("start", task_id, 描述) / ("end", task_id, "")；其他事件返回 None。
    Claude Code 对后台 Bash、后台子代理发 system/task_started，结束（完成、失败、被停）时发 system/task_notification。"""
    if d.get("type") != "system":
        return None
    tid = d.get("task_id")
    if not isinstance(tid, str) or not tid:
        return None
    sub = d.get("subtype")
    if sub == "task_started":
        desc = d.get("description") or d.get("task_type") or "后台任务"
        return ("start", tid, _short(desc, 60))
    if sub == "task_notification":
        return ("end", tid, "")
    return None


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

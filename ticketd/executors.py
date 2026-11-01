"""worker 执行器：把「用哪个 agent CLI 跑一轮」从调度器里拆出来。

调度器只认 Claude Code 的 stream-json 事件形状（system/init、assistant.tool_use、user.tool_result、result）。
其他执行器在 normalizer() 里把自己的事件翻译成这个形状，调度器、状态卡、卡死检测、重试逻辑都不用动。
.stream.jsonl 里落盘的始终是执行器的原始输出。
"""
from __future__ import annotations

import json
import shlex
import time
from typing import Callable, Dict, List

from . import config, perms, worker

NAMES = ("claude", "codex")


class Executor:
    name = ""

    def build_command(self, ticket, grant_paths: List[str], prompt_text: str) -> List[str]:
        raise NotImplementedError

    def normalizer(self) -> Callable[[dict], List[dict]]:
        """每一轮新建一个（可以带状态），把原始事件翻译成 Claude 形状的事件列表。"""
        return lambda ev: [ev]

    def pid_markers(self, ticket) -> List[str]:
        """重启恢复时用来确认 pid 还是这一单的 worker：命令行里出现其中任何一个即可。"""
        return [ticket["session_id"]]

    def takeover_hint(self, ticket) -> str:
        raise NotImplementedError


class ClaudeExecutor(Executor):
    name = "claude"

    def build_command(self, ticket, grant_paths, prompt_text):
        settings = perms.write_settings(ticket["id"], ticket["workdir"], grant_paths)
        return worker.build_command(ticket, settings, prompt_text)

    def takeover_hint(self, ticket):
        return "cd %s && claude --resume %s" % (ticket["workdir"], ticket["session_id"])


def _toml_str(s: str) -> str:
    # JSON 字符串同时是合法的 TOML 基本字符串（json.dumps 不会产生 TOML 不认的 \/ 转义）
    return json.dumps(s, ensure_ascii=False)


def _toml_list(items: List[str]) -> str:
    return "[" + ",".join(_toml_str(x) for x in items) + "]"


class CodexExecutor(Executor):
    name = "codex"

    def config_args(self, ticket, grant_paths, prompt_text) -> List[str]:
        roots = perms.codex_writable_roots(ticket["workdir"], grant_paths)
        args = [
            "-c", 'approval_policy="never"',              # 无人值守：越权直接失败，不挂起等审批
            "-c", 'sandbox_mode="workspace-write"',       # cwd（工单目录）+ /tmp + $TMPDIR 可写
            "-c", "sandbox_workspace_write.writable_roots=" + _toml_list(roots),
            "-c", "sandbox_workspace_write.network_access=true",
            # shell 快照会重放用户的登录 shell 环境：PATH 被改写、shell_environment_policy 失效（V1 实测）
            "--disable", "shell_snapshot",
            # 守护进程环境里的凭据（如网关 token）不进 worker 的 shell；只在关掉 shell 快照时才生效（V1 实测）
            "-c", 'shell_environment_policy.exclude=["*TOKEN*","*KEY*","*SECRET*"]',
            "-c", "developer_instructions=" + _toml_str(prompt_text),
        ]
        if config.codex_model():
            args += ["-m", config.codex_model()]
        return args

    def build_command(self, ticket, grant_paths, prompt_text):
        perms.write_codex_rules()
        common = ["--json", "--skip-git-repo-check"] + self.config_args(ticket, grant_paths, prompt_text)
        if ticket["session_started"]:
            # resume 不接受 -s/-C/--add-dir，沙箱参数只能走 -c；命令行里有 thread_id，可作 pid 校验标记
            return [config.codex_bin(), "exec", "resume", ticket["session_id"]] + common + ["-"]
        # 首轮还没有 thread_id：带上 -C <工单目录> 作为 pid 校验标记（cwd 本来就是它）
        return [config.codex_bin(), "exec", "-C", ticket["workdir"]] + common + ["-"]

    def pid_markers(self, ticket):
        # 首轮命令行里只有 -C <工单目录>；拿到 thread_id 后 session_started 已置 1，但进程仍是首轮那个，所以两个都认
        return [ticket["workdir"], ticket["session_id"]]

    def takeover_hint(self, ticket):
        home = config.codex_home()
        prefix = ("CODEX_HOME=%s " % home) if home else ""
        if not ticket["session_started"]:
            return "（Codex 会话还没建立）"
        return "cd %s && %scodex resume %s" % (ticket["workdir"], prefix, ticket["session_id"])

    def normalizer(self):
        return CodexNormalizer()


def _unwrap_shell(command: str) -> str:
    """Codex 把每条命令包成 `/bin/zsh -lc '<脚本>'`：取出里面的脚本，状态卡才能显示真正的程序名。"""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and words[0].rsplit("/", 1)[-1] in ("zsh", "bash", "sh") and words[1] in ("-c", "-lc"):
        return words[2]
    return command


def _tool_use(name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _tool_result(is_error: bool, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": bool(is_error), "content": (text or "")[-2000:]}]}}


class CodexNormalizer:
    """codex exec --json 的事件 → Claude stream-json 形状（字段按 codex-cli 0.134.0 实测）。"""

    def __init__(self):
        self.turn_started = None

    def __call__(self, ev: dict) -> List[dict]:
        t = ev.get("type")
        if t == "thread.started":
            return [{"type": "system", "subtype": "init", "session_id": ev.get("thread_id")}]
        if t == "turn.started":
            self.turn_started = time.monotonic()
            return []
        if t in ("turn.completed", "turn.failed"):
            ms = (time.monotonic() - self.turn_started) * 1000 if self.turn_started else 0
            if t == "turn.completed":
                return [{"type": "result", "subtype": "success", "is_error": False, "duration_ms": ms}]
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            return [{"type": "result", "subtype": "error", "is_error": True, "duration_ms": ms,
                     "result": str(err.get("message") or "")}]
        if t == "error":
            return [{"type": "system", "subtype": "notice", "text": str(ev.get("message") or "")}]
        if t not in ("item.started", "item.completed"):
            return []
        item = ev.get("item")
        if not isinstance(item, dict):
            return []
        kind, done = item.get("type"), t == "item.completed"
        if kind == "agent_message":
            text = item.get("text")
            if done and isinstance(text, str) and text.strip():
                return [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
            return []
        if kind == "command_execution":
            if not done:
                return [_tool_use("Bash", {"command": _unwrap_shell(item.get("command") or "")})]
            code = item.get("exit_code")
            return [_tool_result(code not in (0, None), item.get("aggregated_output") or "")]
        if kind == "file_change":
            changes = [c for c in (item.get("changes") or []) if isinstance(c, dict)]
            path = changes[0].get("path") if changes else ""
            if not done:
                name = "Write" if changes and all(c.get("kind") == "add" for c in changes) else "Edit"
                return [_tool_use(name, {"file_path": path})]
            return [_tool_result(item.get("status") == "failed", "%d 个文件" % len(changes))]
        if kind == "mcp_tool_call":
            if not done:
                return [_tool_use("mcp__%s__%s" % (item.get("server"), item.get("tool")), {})]
            return [_tool_result(item.get("status") == "failed", "")]
        if kind == "web_search":
            if not done:
                return [_tool_use("WebSearch", {"query": item.get("query") or ""})]
            return [_tool_result(False, "")]
        if kind == "todo_list":
            return [_tool_use("TodoWrite", {})] if not done else []
        if kind == "error":
            return [{"type": "system", "subtype": "notice", "text": str(item.get("message") or "")}]
        return []  # reasoning 等


_EXECUTORS: Dict[str, Executor] = {"claude": ClaudeExecutor(), "codex": CodexExecutor()}


def get(name) -> Executor:
    return _EXECUTORS.get(name or "claude") or _EXECUTORS["claude"]


def prompt_text(name: str) -> str:
    base = config.prompt_file().read_text(encoding="utf-8")
    extra = config.prompt_file().with_name("worker.%s.md" % name)
    if extra.exists():
        base += "\n\n" + extra.read_text(encoding="utf-8")
    return base

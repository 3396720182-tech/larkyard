from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from . import config

# V1 实测（spec §6）：lark-cli 在沙箱里做 TLS 校验需要这些 mach 服务
MACH_LOOKUP = ["com.apple.trustd.agent", "com.apple.trustd", "com.apple.SecurityServer", "com.apple.securityd"]
# 这份 deny 列表只是防误发的护栏，不是安全边界
DENY = [
    "Bash(lark-cli im +messages-send:*)", "Bash(lark-cli im +messages-reply:*)",
    "Bash(lark-cli im messages forward:*)", "Bash(lark-cli im messages merge_forward:*)",
    "Bash(lark-cli im messages urgent_app:*)", "Bash(lark-cli im messages urgent_sms:*)",
    "Bash(lark-cli im messages urgent_phone:*)", "Bash(lark-cli im messages delete:*)",
    "Bash(lark-cli im threads forward:*)",
    "Bash(lark-cli api POST /open-apis/im:*)", "Bash(lark-cli api PUT /open-apis/im:*)",
    "Bash(lark-cli api PATCH /open-apis/im:*)", "Bash(lark-cli api DELETE /open-apis/im:*)",
]
BASE_ALLOW = ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Bash", "TodoWrite", "Agent"]


def writable_paths(workdir: str, grant_paths: List[str]) -> List[str]:
    return [str(Path(workdir).resolve())] + [str(Path(g).resolve()) for g in grant_paths]


def build_settings(workdir: str, grant_paths: List[str]) -> dict:
    paths = writable_paths(workdir, grant_paths)
    allow = list(BASE_ALLOW)
    for p in paths:
        # 规则格式是 Tool(//绝对路径/**)：p 自带开头的 "/"
        allow += ["Edit(/%s/**)" % p, "Write(/%s/**)" % p, "NotebookEdit(/%s/**)" % p]
    sandbox_write = paths + [
        str(Path.home() / "Library" / "Application Support" / "lark-cli"),
        "/private/tmp",
    ]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        sandbox_write.append(str(Path(tmpdir).resolve()))
    lark_cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR")
    if lark_cfg:
        sandbox_write.append(str(Path(lark_cfg).resolve()))
    return {
        "permissions": {"allow": allow, "deny": list(DENY)},
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "filesystem": {"allowWrite": sandbox_write},
            "network": {"allowedDomains": ["*"], "allowMachLookup": list(MACH_LOOKUP)},
        },
    }


def codex_writable_roots(workdir: str, grant_paths: List[str]) -> List[str]:
    """Codex workspace-write 的可写根：和 Claude 沙箱的 allowWrite 对齐（/tmp、$TMPDIR 是 Codex 默认可写的）。"""
    roots = writable_paths(workdir, grant_paths) + [
        str(Path.home() / "Library" / "Application Support" / "lark-cli")]
    lark_cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR")
    if lark_cfg:
        roots.append(str(Path(lark_cfg).resolve()))
    if config.codex_git_writable():
        # Codex 把每个可写根下的 .git 设为只读（V1 实测：授权仓库里 git commit 失败）。
        # 把 .git 本身也列为可写根即可放开，和 Claude worker 能在授权仓库里提交保持一致。
        roots += [str(Path(g).resolve() / ".git") for g in grant_paths if (Path(g) / ".git").is_dir()]
    return roots


# 和 DENY 同一份护栏，写成 Codex execpolicy 规则（只是防误发，不是安全边界）
CODEX_RULES = '''# 由 ticketd 生成，勿手改：ticketd worker 只能通过 ticket report 对外沟通
prefix_rule(
    pattern = ["lark-cli", "im", ["+messages-send", "+messages-reply"]],
    decision = "forbidden",
    justification = "ticketd worker 只能通过 ticket report 对外沟通",
)
prefix_rule(
    pattern = ["lark-cli", "im", ["messages", "threads"], ["forward", "merge_forward", "urgent_app", "urgent_sms", "urgent_phone", "delete"]],
    decision = "forbidden",
    justification = "ticketd worker 只能通过 ticket report 对外沟通",
)
prefix_rule(
    pattern = ["lark-cli", "api", ["POST", "PUT", "PATCH", "DELETE"]],
    decision = "forbidden",
    justification = "ticketd worker 不能用 lark-cli api 做写操作",
)
'''


def write_codex_rules() -> None:
    """只写进 ticketd 专用的 CODEX_HOME（TICKETD_CODEX_HOME）；没配就不写，免得改到用户自己的 ~/.codex。"""
    home = config.codex_home()
    if not home:
        return
    d = Path(home) / "rules"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "ticketd.rules"
    if not p.exists() or p.read_text(encoding="utf-8") != CODEX_RULES:
        p.write_text(CODEX_RULES, encoding="utf-8")


def write_settings(ticket_id: str, workdir: str, grant_paths: List[str]) -> Path:
    d = config.settings_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s.json" % ticket_id)
    p.write_text(json.dumps(build_settings(workdir, grant_paths), ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from . import config, host

# V1 实测（spec §6）：lark-cli 在沙箱里做 TLS 校验需要这些 mach 服务（只有 macOS 有，Linux 上不写这一项）
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


def rule_paths(p: str) -> List[str]:
    """权限规则里的路径写法（规则格式是 Tool(/<路径>/**)，路径自带开头的 "/"）。
    Windows：Claude Code 先把路径规整成 POSIX 形式再匹配（C:\\Users\\a → /c/Users/a）；盘符大小写两种都写，
    不赌匹配是否区分大小写。UNC 路径（\\\\srv\\share）写成 //srv/share。其他平台原样。"""
    if not host.is_windows():
        return [p]
    q = p.replace("\\", "/")
    if len(q) >= 2 and q[1] == ":" and q[0].isalpha():
        rest = q[2:] if q[2:].startswith("/") else "/" + q[2:]
        return ["/%s%s" % (q[0].lower(), rest.rstrip("/")), "/%s%s" % (q[0].upper(), rest.rstrip("/"))]
    return [q.rstrip("/")]


def writable_paths(workdir: str, grant_paths: List[str]) -> List[str]:
    return [str(Path(workdir).resolve())] + [str(Path(g).resolve()) for g in grant_paths]


def _existing_on_linux(paths: List[str], keep: int) -> List[str]:
    """Linux 上沙箱是 bubblewrap，给它一个不存在的绑定路径会整条命令起不来；前 keep 个（工单目录和授权目录）
    原样保留，其余的（lark-cli、临时目录这类附带路径）不存在就不写。macOS 不过滤，生成的设置保持不变。"""
    if not host.is_linux():
        return paths
    return paths[:keep] + [p for p in paths[keep:] if os.path.isdir(p)]


def secret_dirs() -> List[str]:
    """worker 读不到也写不了的目录（spec §12「凭据不进 worker」）：控制台目录里放设备令牌，以后 ticketd 代持的
    网关令牌也放这里；诊断包目录（diag.diag_dir()）里是各张单的原话和日志，worker 不能读、也不能换掉要发的文件。
    解析后的真实路径和字面路径不同时两个都写。"""
    out = []
    for name in ("console", "diag"):
        d = config.home() / name
        for p in (str(d.absolute()), str(d.resolve())):
            if p not in out:
                out.append(p)
    return out


def readonly_dirs() -> List[str]:
    """worker 能读、不能写的目录（spec §12「下发技能」）：控制台下发的技能和它们的凭据。授权目录再宽也不能改，
    否则一张单的 worker 能改掉别的单要加载的技能。路径和 skills.root() 一致；真实路径不同时两个都写。"""
    d = config.home() / "skills"
    out = [str(d.absolute())]
    real = str(d.resolve())
    if real not in out:
        out.append(real)
    return out


def ensure_secret_dirs() -> None:
    """先把控制台目录建好（700、空目录，不算入网）：Linux 的 bubblewrap 只能遮住已经存在的路径，
    这一轮中途才入网时，令牌也落在已被遮住的目录里。下发技能的目录同理（空目录 = 没装技能）。
    建不了就算了，macOS 的规则不依赖目录存在。"""
    for name, mode in (("console", 0o700), ("diag", 0o700), ("skills", 0o755)):
        try:
            (config.home() / name).mkdir(parents=True, exist_ok=True, mode=mode)
        except OSError:
            pass


def build_settings(workdir: str, grant_paths: List[str], net: Optional[dict] = None) -> dict:
    paths = writable_paths(workdir, grant_paths)
    allow = list(BASE_ALLOW)
    for p in paths:
        # 规则格式是 Tool(//绝对路径/**)：p 自带开头的 "/"
        for r in rule_paths(p):
            allow += ["Edit(/%s/**)" % r, "Write(/%s/**)" % r, "NotebookEdit(/%s/**)" % r]
    secrets = secret_dirs()
    # 内置文件工具（Read / Grep / Glob / Edit 等）按 permissions 拦，Bash 及其子进程按 sandbox 拦，两边都要写。
    # 写也禁：授权目录很宽（比如整个家目录）时，worker 也不能改 console.json 把令牌引到别的地址
    deny = list(DENY)
    for s in secrets:
        for r in rule_paths(s):
            deny += ["%s(/%s/**)" % (tool, r) for tool in ("Read", "Edit", "Write", "NotebookEdit")]
    readonly = readonly_dirs()
    for s in readonly:
        for r in rule_paths(s):
            deny += ["%s(/%s/**)" % (tool, r) for tool in ("Edit", "Write", "NotebookEdit")]
    if host.is_windows():
        return _windows_settings(allow, deny)
    sandbox_write = paths + [host.lark_cli_data_dir(), host.shared_tmp()]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        sandbox_write.append(str(Path(tmpdir).resolve()))
    lark_cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR")
    if lark_cfg:
        sandbox_write.append(str(Path(lark_cfg).resolve()))
    sandbox_write = _existing_on_linux(sandbox_write, len(paths))
    network = {"allowedDomains": ["*"]}
    if host.system() == "darwin":
        network["allowMachLookup"] = list(MACH_LOOKUP)
    # 用户逐单授权的本机网络 / unix socket（issue #20）。字段名和语义按 Claude Code 2.1.316 内置的沙箱规则核对过：
    # allowLocalBinding 一次放开本机 bind/listen 和连 localhost 的所有端口（不能按端口细分）；
    # allowUnixSockets 按路径（含子路径）放开 unix socket 的连接和 bind
    if net and net.get("local"):
        network["allowLocalBinding"] = True
    if net and net.get("sockets"):
        network["allowUnixSockets"] = list(net["sockets"])
    return {
        "permissions": {"allow": allow, "deny": deny},
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            # Linux / WSL2 上沙箱靠 bubblewrap + socat：没装时宁可让 worker 起不来，也不裸跑
            "failIfUnavailable": True,
            "filesystem": {"allowWrite": sandbox_write, "denyRead": list(secrets),
                           "denyWrite": list(secrets) + readonly},
            "network": network,
        },
    }


def _windows_settings(allow: List[str], deny: List[str]) -> dict:
    """Windows 上给 Claude worker 的设置（spec §3.2）。Claude Code 自带的沙箱不支持原生 Windows：开着它、
    再加 failIfUnavailable 会让 Claude Code 直接起不来，不加则命令照样在沙箱外跑。所以这里关掉它，
    Bash 命令的系统级边界改由 worker 环境里的 CLAUDE_CODE_SHELL_PREFIX 套上 sandbox-runtime（见 write_srt_settings、
    bin/ticketd-wsandbox）。PowerShell 工具不经过 shell 前缀，一律禁用（worker 环境里也关掉了它）。"""
    deny = list(deny) + ["PowerShell"]
    return {
        "permissions": {"allow": allow, "deny": deny},
        "sandbox": {"enabled": False},
    }


def srt_settings(workdir: str, grant_paths: List[str], net: Optional[dict] = None) -> dict:
    """Windows：sandbox-runtime（srt）的设置，和 macOS / Linux 上 Claude 沙箱的 allowWrite / denyRead / denyWrite 对齐。
    临时目录不用写：srt 让命令以专用的 srt-sandbox 账户运行，它自己的 %TEMP% 本来就能写。
    本机网络（allowLocalBinding）在 Windows 上是 srt 装机时的全机开关（--allow-loopback），不能按单放开，这里照写只是
    表明意图；unix socket 在 Windows 上没有对应物。"""
    paths = writable_paths(workdir, grant_paths)
    allow_write = list(paths)
    lark_cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR")
    if lark_cfg:
        allow_write.append(str(Path(lark_cfg).resolve()))
    secrets = secret_dirs()
    network = {"allowedDomains": ["*"]}
    if net and net.get("local"):
        network["allowLocalBinding"] = True
    return {
        "network": network,
        "filesystem": {"allowWrite": allow_write, "denyRead": list(secrets),
                       "denyWrite": list(secrets) + readonly_dirs()},
    }


def srt_settings_path(ticket_id: str) -> Path:
    return config.settings_dir() / ("%s.srt.json" % ticket_id)


def write_srt_settings(ticket_id: str, workdir: str, grant_paths: List[str], net: Optional[dict] = None) -> Path:
    ensure_secret_dirs()
    p = srt_settings_path(ticket_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(srt_settings(workdir, grant_paths, net), ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


def codex_writable_roots(workdir: str, grant_paths: List[str]) -> List[str]:
    """Codex workspace-write 的可写根：和 Claude 沙箱的 allowWrite 对齐（/tmp、$TMPDIR 是 Codex 默认可写的）。"""
    paths = writable_paths(workdir, grant_paths)
    roots = paths + [d for d in [host.lark_cli_data_dir()] if d]
    lark_cfg = os.environ.get("LARKSUITE_CLI_CONFIG_DIR")
    if lark_cfg:
        roots.append(str(Path(lark_cfg).resolve()))
    roots = _existing_on_linux(roots, len(paths))
    if config.codex_git_writable():
        # Codex 把每个可写根下的 .git 设为只读（V1 实测：授权仓库里 git commit 失败）。
        # 把 .git 本身也列为可写根即可放开，和 Claude worker 能在授权仓库里提交保持一致。
        # （macOS 实测；Linux 上 Codex 用 bubblewrap，这一条未实测，多列一个可写根不会放宽到 .git 以外。）
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


def write_settings(ticket_id: str, workdir: str, grant_paths: List[str], net: Optional[dict] = None) -> Path:
    ensure_secret_dirs()
    d = config.settings_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s.json" % ticket_id)
    p.write_text(json.dumps(build_settings(workdir, grant_paths, net), ensure_ascii=False, indent=1),
                 encoding="utf-8")
    if host.is_windows():
        write_srt_settings(ticket_id, workdir, grant_paths, net)
    return p

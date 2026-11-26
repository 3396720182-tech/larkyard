"""原生 Windows 的一键安装向导（spec §3.2）。scripts/windows/bootstrap.ps1 装好 Python / Git / Node、npm 上的
lark-cli / lark-channel-bridge / sandbox-runtime、Claude Code，拉好仓库之后调用它；也可以在仓库目录里
`py -3 -m ticketd.winsetup` 重跑。

① Claude 登录 → ② worker 沙箱（sandbox-runtime，一次 UAC）→ ③ 飞书应用（bridge 扫码建 profile）→ ④ 工作区约定 →
⑤ ticketd 服务（wininstall）→ ⑥ bridge 后台服务 → ⑦ 认主人 → ⑧ 自检。

每一步先看做没做过，做过就跳过：重跑同一行命令就是「继续」和「更新」。⑥ 每次都执行 `lark-channel-bridge start`：
bridge 的 Windows 启动脚本（.cmd）只在 start 时记下当时的 PATH（restart 不重写），主会话要靠它找到 ticket。
可选环境变量：LARKYARD_OWNER（直接给出主人 open_id）、LARKYARD_ALLOW_LOOPBACK=1（装沙箱时放开本机端口，全机生效）、
LARK_CHANNEL_PROFILE（bridge profile，默认 claude）、LARKYARD_WORKSPACE（新建 profile 时的工作区）。只用标准库。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import bridge, config, host, setup, wininstall, worker

REPO = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = "~/larkyard-workspace"
API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


class WinSetupError(Exception):
    pass


class NeedTerminal(WinSetupError):
    """要人在终端里操作（扫码）：退出码 3，和其他失败分开（CI 靠它确认向导是按预期停下的，不是崩了）。"""


def _json(text: str) -> Optional[dict]:
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


# ---------- ① Claude 登录 ----------
def api_configured(env=None, settings: Optional[Path] = None) -> bool:
    """不走账户登录、改用 API 密钥 / 网关 / 云厂商的：环境变量或 ~/.claude/settings.json 的 env / apiKeyHelper。"""
    env = os.environ if env is None else env
    if any(env.get(k) for k in API_ENV):
        return True
    d = None
    try:
        # utf-8-sig：PowerShell 5.1 / 旧记事本存的文件开头常带 BOM
        d = _json((settings or Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
    except OSError:
        pass
    if not d:
        return False
    senv = d.get("env") if isinstance(d.get("env"), dict) else {}
    return bool(d.get("apiKeyHelper")) or any(senv.get(k) for k in API_ENV)


def claude_logged_in(runner=subprocess.run) -> Optional[bool]:
    """claude auth status 的 loggedIn；查不出来返回 None。"""
    try:
        p = runner(host.argv(["claude", "auth", "status", "--json"]), capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    d = _json(p.stdout)
    if d is None or not isinstance(d.get("loggedIn"), bool):
        return None
    return d["loggedIn"]


# ---------- ② worker 沙箱 ----------
def srt_win_exe(srt: str, arch: Optional[str] = None) -> Path:
    """npm 全局装的 sandbox-runtime 自带的 srt-win.exe（srt.cmd 和 node_modules 在同一个 npm 前缀目录下）。"""
    arch = arch or ("arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "x64")
    return Path(srt).parent / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "vendor" / "srt-win" / arch / \
        "srt-win.exe"


def sandbox_ready(srt: str, runner=subprocess.run) -> Optional[bool]:
    """`srt-win user status`：沙箱账户在、凭据在、装的是这个版本要的那一版。查不出来返回 None（照装一遍，装是幂等的）。"""
    exe = srt_win_exe(srt)
    if not exe.is_file():
        return None
    try:
        p = runner([str(exe), "user", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    d = _json(p.stdout)
    if d is None or not isinstance(d.get("user"), dict):
        return None
    if not d["user"].get("exists") or not d.get("cred_present"):
        return False
    mv, sv = d.get("marker_version"), d.get("setup_version")
    return sv is None or mv == sv


# ---------- ⑥ bridge ----------
def service_path(found: dict, path: Optional[str] = None) -> str:
    """bridge 启动脚本记下的 PATH：仓库 bin（ticket）在最前，再是 node / lark-cli / claude / codex 所在目录，然后原 PATH。"""
    dirs = [str(REPO / "bin")]
    for t in ("node", "lark-cli", "claude", "codex"):
        if found.get(t):
            dirs.append(str(Path(found[t]).parent))
    have = [p for p in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep) if p]
    out = []
    for d in dirs + have:
        if os.path.normcase(d.rstrip("\\/")) not in [os.path.normcase(x.rstrip("\\/")) for x in out]:
            out.append(d)
    return os.pathsep.join(out)


class _Reword:
    """包一层 Term：复用 ticket setup 的认主人步骤时，把它的编号和「重新执行 ticket setup」换成 Windows 这边的说法。"""

    def __init__(self, term, pairs):
        self.term, self.pairs = term, pairs

    def _fix(self, text: str) -> str:
        for a, b in self.pairs:
            text = text.replace(a, b)
        return text

    @property
    def interactive(self) -> bool:
        return self.term.interactive

    def say(self, text: str = "") -> None:
        self.term.say(self._fix(text))

    def ask(self, prompt: str, default: str = "") -> str:
        return self.term.ask(self._fix(prompt), default)

    def yes(self, prompt: str, default: bool = True) -> bool:
        return setup.Term.yes(self, prompt, default)

    def child_io(self) -> dict:
        return self.term.child_io()


OWNER_REWORD = (("④", "⑦"), ("重新执行 ticket setup", "重跑安装命令"), ("之后执行 ticket setup 认主人", "之后重跑安装命令认主人"))


class WinSetup:
    def __init__(self, term: Optional[setup.Term] = None, runner=subprocess.run, install=None):
        self.t = term or setup.Term()
        self.runner = runner
        self.install = install or wininstall.install
        self.warnings = []

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self.t.say("   ⚠️ " + text)

    def _run(self, argv: list, interactive: bool = False, **kw):
        """交互的（扫码、登录、UAC）直接接终端；其余收起输出。"""
        try:
            argv = host.argv(argv)
        except OSError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))
        try:
            if interactive:
                return self.runner(argv, **dict(self.t.child_io(), **kw))
            return self.runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
        except OSError as e:
            return subprocess.CompletedProcess(argv, 127, "", str(e))

    def step_claude(self) -> None:
        if api_configured():
            self.t.say("① Claude：用的是 API 密钥 / 网关配置，跳过登录")
            return
        state = claude_logged_in(self.runner)
        if state:
            self.t.say("① Claude：已登录")
            return
        if not self.t.interactive:
            self.warn("Claude 还没登录：在终端里执行 claude auth login，再重跑安装命令")
            return
        self.t.say("① Claude：还没登录，接下来会打开浏览器，登录完回到这个窗口")
        self._run(["claude", "auth", "login"], interactive=True)
        if not claude_logged_in(self.runner):
            self.warn("Claude 还是没登录上：在终端里执行 claude auth login，再重跑安装命令")

    def step_sandbox(self) -> None:
        if config.win_sandbox() == "off":
            self.warn("config.env 里写了 TICKETD_WIN_SANDBOX=off：worker 的 Bash 命令不在沙箱里跑")
            return
        srt = worker.srt_path()
        if not srt:
            self.warn("没找到 sandbox-runtime（srt）：先 npm install -g @anthropic-ai/sandbox-runtime，再重跑安装命令")
            return
        loopback = os.environ.get("LARKYARD_ALLOW_LOOPBACK") == "1"
        ready = sandbox_ready(srt, self.runner)
        if ready and not loopback:
            self.t.say("② worker 沙箱：已装好")
            return
        self.t.say("② worker 沙箱：接下来会弹一次管理员确认（UAC），点「是」。\n"
                   "   第一次要让沙箱账户能读你的用户目录（凭据目录除外），用户目录大的话要几分钟")
        args = [srt, "windows-install"]
        if loopback:
            # 放开本机端口是装机时的全机设置；已经装过（没放开）时，配置不同要 --force 才会替换
            args += ["--allow-loopback"] + (["--force"] if ready else [])
        p = self._run(args, interactive=True)
        if p.returncode != 0:
            self.warn("沙箱没装成（多半是这个账户没有管理员权限）。请 IT 用管理员身份执行一次 srt windows-install，"
                      "之后你自己再跑一遍安装命令（补上读用户目录的授权）。装好前 worker 不会启动")

    def step_app(self, profile: str, workspace: Path) -> Path:
        pc = setup.profile_config(profile)
        if pc is not None:
            self.t.say("③ 飞书应用：已有 profile %s，跳过" % profile)
            return Path(str(setup._dig(pc, "workspaces", "default") or workspace)).expanduser()
        if not self.t.interactive:
            raise NeedTerminal("建飞书应用要扫码：请在 PowerShell 窗口里重跑安装命令")
        workspace.mkdir(parents=True, exist_ok=True)
        self.t.say("③ 飞书应用：用飞书扫下面的二维码，按提示创建应用。\n"
                   "   建议新建一个（这台电脑专用的 bot），不要选别的电脑上在用的那个：一个应用连两台电脑，消息会随机落到其中一台")
        p = self._run(["lark-channel-bridge", "profile", "create", profile, "--agent", "claude",
                       "--workspace", str(workspace)], interactive=True)
        if p.returncode != 0 or setup.profile_config(profile) is None:
            raise WinSetupError("建应用没有完成（bridge 退出码 %s）：重跑安装命令" % p.returncode)
        if not (bridge.lc_home() / "active-profile").exists():
            self._run(["lark-channel-bridge", "profile", "use", profile])
        return workspace

    def step_workspace(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        if (workspace / "CLAUDE.md").exists():
            self.t.say("④ 工作区 %s：已有 CLAUDE.md，不动" % workspace)
            return
        bash = host.git_bash()
        if not bash:
            raise WinSetupError("没找到 Git Bash：先装 Git for Windows，再重跑安装命令")
        p = self._run([bash, str(REPO / "scripts" / "setup-workspace.sh").replace("\\", "/"),
                       str(workspace).replace("\\", "/")])
        if p.returncode != 0:
            raise WinSetupError("写工作区约定失败：%s" % (p.stderr or p.stdout or "").strip()[-300:])
        self.t.say("④ 工作区 %s：已写入 CLAUDE.md 和 CLAUDE.local.md" % workspace)

    def step_service(self) -> None:
        self.t.say("⑤ ticketd 服务：")
        try:
            rc = self.install(guided=True)
        except wininstall.InstallError as e:
            raise WinSetupError(str(e))
        if rc != 0:
            raise WinSetupError("ticketd 服务没装成（退出码 %s）" % rc)

    def step_bridge(self, profile: str) -> None:
        found, _ = wininstall.find_tools()
        env = dict(os.environ)
        env["PATH"] = service_path(found)
        self.t.say("⑥ bridge 后台服务：（重新）启动，让主会话用上 ticket 命令")
        p = self._run(["lark-channel-bridge", "start", "--profile", profile], interactive=True, env=env)
        if p.returncode != 0:
            self.warn("bridge 没启动成功（退出码 %s）：看 %s" % (p.returncode, bridge.daemon_logs(profile)[1]))

    def step_owner(self, profile: str) -> Optional[str]:
        given = (os.environ.get("LARKYARD_OWNER") or "").strip()
        if given and not (config.file_value("TICKETD_OWNER") or "").strip():
            if not given.startswith("ou_"):
                raise WinSetupError("LARKYARD_OWNER 要是 ou_ 开头的 open_id")
            setup.config_put("TICKETD_OWNER", given)
            self.t.say("⑦ 主人：已按 LARKYARD_OWNER 写入")
            return given
        have = (config.file_value("TICKETD_OWNER") or "").strip()
        if have:
            self.t.say("⑦ 主人：config.env 里已有 TICKETD_OWNER（%s），跳过" % setup._mask(have))
            return have
        self.t.say("⑦ 认主人（只有主人发的消息才会派单）")
        # 复用 ticket setup 的认主人：先用 bot 身份查应用所有者、问「是你吗」；查不到就请你私聊 bot 发验证码
        w = setup.Wizard(term=_Reword(self.t, OWNER_REWORD), services=object(), runner=self.runner)
        owner = w.step_owner(profile)
        if not owner:
            self.warn("还没认主人，派单会被拒绝：重跑安装命令，或在 %s 里写 TICKETD_OWNER=<你的 open_id>"
                      % config.config_file())
        return owner

    def step_check(self) -> None:
        self.t.say("⑧ 自检（ticket doctor）：")
        p = self._run([wininstall.python(), "-c",
                       "import sys; sys.path.insert(0, sys.argv.pop(1)); from ticketd.cli import main; sys.exit(main())",
                       str(REPO), "doctor"], env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        self.t.say((p.stdout or p.stderr or "").rstrip())


def run(profile: str = "", workspace: str = "", ws: Optional[WinSetup] = None) -> int:
    if not host.is_windows():
        raise WinSetupError("这是原生 Windows 的安装向导；macOS / Linux 用一行安装（README「安装」）")
    if os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"):
        raise WinSetupError("worker 里不能执行安装（要由本人在 PowerShell 里执行）")
    if os.environ.get("LARK_CHANNEL") == "1":
        raise WinSetupError("安装会重启 bridge：看起来是在 bridge 驱动的会话里执行（LARK_CHANNEL=1），会把这个会话自己打断。"
                            "请在你自己的 PowerShell 窗口里执行")
    ws = ws or WinSetup()
    profile = bridge.profile_name(profile)
    bridge.service_id(profile)
    # wininstall.service_env 只看这个变量（默认 claude）：已有别的激活 profile 时，守护进程也要用同一个
    os.environ["LARK_CHANNEL_PROFILE"] = profile
    default_ws = Path(os.path.expanduser(workspace or os.environ.get("LARKYARD_WORKSPACE") or DEFAULT_WORKSPACE))
    ws.step_claude()
    ws.step_sandbox()
    wsdir = ws.step_app(profile, default_ws)
    ws.step_workspace(wsdir)
    ws.step_service()
    ws.step_bridge(profile)
    ws.step_owner(profile)
    ws.step_check()
    ws.t.say("")
    if ws.warnings:
        ws.t.say("装好了，但还有 %d 件事没完成（见上面的 ⚠️）。处理完重跑同一行安装命令即可，做过的步骤会跳过。" % len(ws.warnings))
        return 1
    ws.t.say("装好了。在飞书里给 bot 发一句「开一张单：在工单目录里建一个 hello.txt」试试。\n"
             "以后更新：重跑同一行安装命令。新开的终端里可以直接用 ticket 命令。")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ticketd.winsetup", description="原生 Windows 一键安装向导")
    ap.add_argument("--profile", default="", help="bridge profile（默认 LARK_CHANNEL_PROFILE、当前激活的或 claude）")
    ap.add_argument("--workspace", default="", help="新建 profile 时的工作区（默认 ~/larkyard-workspace）")
    a = ap.parse_args(argv)
    try:
        return run(a.profile, a.workspace)
    except (WinSetupError, bridge.BridgeError, setup.SetupError) as e:
        print("安装停在这一步：%s" % e, file=sys.stderr)
        return 3 if isinstance(e, NeedTerminal) else 1


if __name__ == "__main__":
    sys.exit(main())

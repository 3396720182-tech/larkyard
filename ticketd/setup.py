"""ticket setup：一行安装之后的向导。① 飞书应用 → ② 工作区 → ③ 服务 → ④ 认主人 → ⑤ 用户身份（可选）→ ⑥ 自检。
每一步先看做没做过，做过就跳过（可重复执行）。交互从终端读（curl | sh 时标准输入是管道，改读 /dev/tty）。
见 openspec vendor-bridge-one-line-install（one-line-install）和 docs/spec.md §13。
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import bridge, config, host, update

DEFAULT_WORKSPACE = "~/larkyard-workspace"
OWNER_WAIT = 300.0
BIND_WAIT = 60.0


class SetupError(Exception):
    pass


class Term:
    """终端读写。标准输入不是终端时改用 /dev/tty；都没有就 interactive = False。"""

    def __init__(self, fin=None, fout=None):
        self.fin, self.fout = fin, fout or sys.stdout
        if self.fin is None:
            if sys.stdin is not None and sys.stdin.isatty():
                self.fin = sys.stdin
            else:
                try:
                    self.fin = open("/dev/tty", "r+")
                    self.fout = self.fin
                except OSError:
                    self.fin = None

    @property
    def interactive(self) -> bool:
        return self.fin is not None

    def say(self, text: str = "") -> None:
        self.fout.write(text + "\n")
        self.fout.flush()

    def ask(self, prompt: str, default: str = "") -> str:
        if not self.interactive:
            return default
        self.fout.write(prompt)
        self.fout.flush()
        line = self.fin.readline()
        if not line:
            return default
        return line.strip() or default

    def yes(self, prompt: str, default: bool = True) -> bool:
        a = self.ask("%s [%s] " % (prompt, "Y/n" if default else "y/N"), "y" if default else "n")
        return a.lower() in ("y", "yes", "是", "好", "对")

    def child_io(self) -> dict:
        """给子进程（bridge 扫码、lark-cli 授权）的标准输入输出：直接接终端。"""
        if self.fin is None:
            return {}
        return {"stdin": self.fin, "stdout": self.fout, "stderr": self.fout}


# ---------- config.env ----------
def config_put(key: str, value: str) -> bool:
    """写 config.env：没有这个键就追加；有但值为空就填上；已有非空值不动。返回是否写了。"""
    p = config.home() / "config.env"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    pat = re.compile(r"^(\s*%s\s*=)(.*)$" % re.escape(key), re.M)
    m = pat.search(text)
    if m:
        if m.group(2).strip().strip("\"'"):
            return False
        text = text[:m.start()] + "%s=%s" % (key, value) + text[m.end():]
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + "%s=%s\n" % (key, value)
    tmp = p.with_name("config.env.tmp-%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(p))
    return True


# ---------- bridge 配置（只读） ----------
def bridge_config() -> dict:
    try:
        d = json.loads((bridge.lc_home() / "config.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def profile_config(profile: str) -> Optional[dict]:
    p = (bridge_config().get("profiles") or {}).get(profile)
    return p if isinstance(p, dict) else None


def lark_env(profile: str) -> dict:
    """和 scripts/larkenv.sh 一致：lark-cli 走 bridge 绑定的 profile。"""
    home = bridge.lc_home()
    env = dict(os.environ)
    env.update({
        "LARK_CHANNEL": "1", "LARK_CHANNEL_HOME": str(home), "LARK_CHANNEL_PROFILE": profile,
        "LARK_CHANNEL_CONFIG": str(home / "profiles" / profile / "lark-cli-source" / "config.json"),
        "LARKSUITE_CLI_CONFIG_DIR": str(home / "profiles" / profile / "lark-cli"),
        "PATH": bridge.service_path(bridge.tool_dir("claude")),
    })
    return env


def _lark_json(args: list, profile: str, runner=subprocess.run) -> Optional[dict]:
    try:
        p = runner([str(bridge.lark_cli())] + args, env=lark_env(profile), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def app_owner(profile: str, app_id: str, runner=subprocess.run) -> Optional[str]:
    """bot 身份查应用信息里的所有者 open_id（bridge 运行时也是这样认 owner 的）。"""
    d = _lark_json(["api", "GET", "/open-apis/application/v6/applications/%s" % app_id,
                    "--params", json.dumps({"lang": "zh_cn", "user_id_type": "open_id"}), "--as", "bot"],
                   profile, runner)
    for path in (("data", "app", "owner", "owner_id"), ("app", "owner", "owner_id")):
        v = _dig(d, *path)
        if isinstance(v, str) and v.startswith("ou_"):
            return v
    return None


def user_name(profile: str, open_id: str, runner=subprocess.run) -> str:
    d = _lark_json(["api", "GET", "/open-apis/contact/v3/users/%s" % open_id,
                    "--params", json.dumps({"user_id_type": "open_id"}), "--as", "bot"], profile, runner)
    for path in (("data", "user", "name"), ("user", "name")):
        v = _dig(d, *path)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def sender_with_code(profile: str, code: str, since: float) -> Optional[str]:
    """bridge 日志里 since 之后、预览含这个码的私聊消息的发送人。"""
    logs = bridge.lc_home() / "profiles" / profile / "logs"
    for day in (0, 1):
        p = logs / ("bridge-%s.jsonl" % time.strftime("%Y%m%d", time.localtime(time.time() - day * 86400)))
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("event") != "enter":
                continue
            ts = bridge._parse_ts(d.get("ts", ""))
            if ts is None or ts < since:
                continue
            # 只认私聊、整句就是「认主人 <码>」：bridge 在权限检查之前就记 enter，群聊和陌生人的消息也在里面
            if d.get("chatType") not in (None, "p2p"):
                continue
            if re.fullmatch(r"\s*认主人\s*%s\s*" % re.escape(code), str(d.get("preview", ""))) and \
                    str(d.get("sender", "")).startswith("ou_"):
                return d["sender"]
    return None


def _mask(open_id: str) -> str:
    return "…" + open_id[-6:]


# ---------- 各步骤 ----------
class Wizard:
    def __init__(self, term: Optional[Term] = None, services: Optional[bridge.Services] = None, runner=subprocess.run,
                 run_install=None, sleep=time.sleep, clock=time.time, wait=bridge.wait_online):
        self.t = term or Term()
        self.services = services or bridge.Services()
        self.runner = runner
        self.run_install = run_install or update._install_sh
        self.sleep, self.clock, self.wait = sleep, clock, wait

    def _bridge_cmd(self, *args) -> list:
        return [str(bridge.node_bin()), str(bridge.bridge_entry())] + list(args)

    def _bridge_env(self) -> dict:
        env = dict(os.environ)
        env["LARK_CHANNEL_HOME"] = str(bridge.lc_home())
        env["PATH"] = bridge.service_path(bridge.tool_dir("claude"), bridge.tool_dir("codex"))
        return env

    def step_app(self, profile: str, workspace: Path, app_id: str = "") -> str:
        if profile_config(profile) is not None:
            self.t.say("① 飞书应用：已有 profile %s，跳过" % profile)
            pc = profile_config(profile) or {}
            return str(_dig(pc, "workspaces", "default") or workspace)
        if not self.t.interactive:
            raise SetupError("建飞书应用要扫码：请在终端里执行 ticket setup")
        workspace.mkdir(parents=True, exist_ok=True)
        self.t.say("① 飞书应用：用飞书扫下面的二维码，按提示创建应用（权限和事件订阅会一起带上）")
        cmd = self._bridge_cmd("profile", "create", profile, "--agent", "claude", "--workspace", str(workspace))
        if app_id:
            cmd += ["--app-id", app_id]
        p = self.runner(cmd, env=self._bridge_env(), **self.t.child_io())
        if p.returncode != 0 or profile_config(profile) is None:
            raise SetupError("建应用没有完成（bridge 退出码 %s）：重新执行 ticket setup" % p.returncode)
        if not (bridge.lc_home() / "active-profile").exists():
            self.runner(self._bridge_cmd("profile", "use", profile), env=self._bridge_env(), capture_output=True)
        return str(workspace)

    def step_workspace(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        if (workspace / "CLAUDE.md").exists():
            self.t.say("② 工作区 %s：已有 CLAUDE.md，不动（ticket doctor 会对照模板）" % workspace)
            return
        script = update.current_link() / "scripts" / "setup-workspace.sh"
        p = self.runner(["/bin/sh", str(script), str(workspace)], capture_output=True, text=True)
        if p.returncode != 0:
            raise SetupError("渲染工作区约定失败：%s" % (p.stderr or p.stdout).strip()[-300:])
        self.t.say("② 工作区 %s：已写入 CLAUDE.md 和 CLAUDE.local.md" % workspace)

    def step_services(self, profile: str) -> bool:
        """返回 bridge 服务是否由 Larkyard 管理（分开安装的不动）。"""
        sf = bridge.bridge_service_file(profile)
        managed = True
        if sf.exists() and not bridge.is_managed_service(sf):
            managed = False
            self.t.say("③ bridge 服务 %s 是分开安装的，不覆盖；迁移用 ticket bridge adopt" % sf)
        else:
            if not bridge.bridge_link().exists():
                bridge.link_bridge(update.current_link().resolve())
            data = bridge.render_bridge_service(profile, bridge.service_path(
                bridge.tool_dir("claude"), bridge.tool_dir("codex"), bridge.tool_dir("node")))
            old = sf.read_bytes() if sf.exists() else None
            if old == data:
                # 重复执行：服务定义没变就不重新加载（重新加载会重启 bridge、打断主会话）
                self.t.say("③ bridge 服务 %s：已装好，没变" % bridge.bridge_label(profile))
            else:
                bridge._write(sf, data)
                for d in bridge.daemon_logs(profile):
                    d.parent.mkdir(parents=True, exist_ok=True)
                started = self.clock()
                self.services.load(bridge.bridge_label(profile), sf)
                self.t.say("③ bridge 服务 %s：已加载" % bridge.bridge_label(profile))
                if self.wait(profile, started - 1):
                    self.t.say("   bridge 已连上飞书")
                else:
                    self.t.say("   ⚠️ bridge %d 秒内没连上飞书：看 %s" % (bridge.HEALTH_WAIT, bridge.daemon_logs(profile)[1]))
            have_job = bridge.restart_service_file().exists() or bridge.find_restart_jobs(exclude=(sf,)) or \
                (bridge.read_state().get("adopted") or {}).get("restart_jobs")
            if not have_job:
                bridge.install_restart_job(["restart", "--if-stale", "--profile", profile], services=self.services,
                                           env={"LARK_CHANNEL_HOME": str(bridge.lc_home())})
                self.t.say("   每天 04:07 的重启窗口 %s：已加载（版本没变不重启）" % bridge.RESTART_LABEL)
        env = {"LARK_CHANNEL_PROFILE": profile, "LARK_CHANNEL_HOME": str(bridge.lc_home())}
        label = os.environ.get("TICKETD_LABEL") or update.DEFAULT_LABEL
        tsf = update.service_file(label)
        if tsf.is_file():
            prog = " ".join(bridge.service_program(tsf))
            path = bridge.service_env(tsf).get("PATH", "")
            if "launcher.py" in prog and str(bridge.node_link() / "bin") in path.split(":"):
                self.t.say("   ticketd 服务 %s：已装好" % label)
                return managed
            label, have = update.capture_service()
            env.update(have)
        self.run_install(update.current_link().resolve(), update.app_dir(), {"label": label, "env": env})
        self.t.say("   ticketd 服务 %s：已安装" % label)
        return managed

    def step_owner(self, profile: str) -> Optional[str]:
        have = (config.file_value("TICKETD_OWNER") or "").strip()
        if have:
            self.t.say("④ 主人：config.env 里已有 TICKETD_OWNER（%s），跳过" % _mask(have))
            return have
        if not self.t.interactive:
            self.t.say("④ 主人：不在终端里，跳过；之后执行 ticket setup 认主人")
            return None
        app_id = str(_dig(profile_config(profile) or {}, "accounts", "app", "id") or "")
        owner = None
        if app_id:
            deadline = self.clock() + BIND_WAIT
            while owner is None:
                owner = app_owner(profile, app_id, self.runner)
                if owner or self.clock() >= deadline:
                    break
                self.sleep(5)  # bridge 首次启动时才执行 lark-cli config bind
        if owner:
            name = user_name(profile, owner, self.runner)
            who = "%s（%s）" % (name, _mask(owner)) if name else _mask(owner)
            if self.t.yes("④ 应用所有者：%s，是你吗？" % who):
                config_put("TICKETD_OWNER", owner)
                self.t.say("   已写入 TICKETD_OWNER")
                return owner
        else:
            self.t.say("④ 没查到应用所有者（权限未生效，或 bridge 还没完成绑定）")
        code = "%06d" % random.SystemRandom().randint(0, 999999)
        since = self.clock()
        self.t.say("   在飞书里私聊 bot 发送：认主人 %s （最多等 %d 分钟）" % (code, OWNER_WAIT // 60))
        deadline = since + OWNER_WAIT
        sender = None
        while self.clock() < deadline:
            sender = sender_with_code(profile, code, since - 1)
            if sender:
                break
            self.sleep(3)
        if not sender:
            self.t.say("   没等到。手动填：在 %s 里写 TICKETD_OWNER=<你的 open_id>，或重新执行 ticket setup"
                       % (config.home() / "config.env"))
            return None
        name = user_name(profile, sender, self.runner)
        if self.t.yes("   认出发送人：%s，是你吗？" % ("%s（%s）" % (name, _mask(sender)) if name else _mask(sender)), False):
            config_put("TICKETD_OWNER", sender)
            self.t.say("   已写入 TICKETD_OWNER")
            return sender
        return None

    def step_user_auth(self, profile: str, ask: bool = True) -> None:
        if not self.t.interactive:
            return
        if ask and not self.t.yes("⑤ 现在授权用户身份吗？（传视频、导入文档要用；以后可以 ticket setup --user-auth）", False):
            self.t.say("⑤ 用户身份：跳过")
            return
        self.runner([str(bridge.lark_cli()), "auth", "login"], env=lark_env(profile), **self.t.child_io())

    def step_check(self) -> None:
        ticket = str(update.current_link() / "bin" / "ticket")
        self.t.say("⑥ 自检：")
        p = self.runner([ticket, "doctor"], capture_output=True, text=True)
        self.t.say((p.stdout or p.stderr or "").rstrip())
        if (config.file_value("TICKETD_OWNER") or "").strip():
            p = self.runner([ticket, "doctor", "--dm"], capture_output=True, text=True)
            self.t.say((p.stdout or p.stderr or "").rstrip())
        self.t.say("最后在飞书里给 bot 说句话，确认主会话能回。")


def run(profile: str = "", workspace: str = "", app_id: str = "", user_auth_only: bool = False,
        wizard: Optional[Wizard] = None) -> int:
    if os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"):
        raise SetupError("worker 里不能执行 ticket setup（要由本人在终端里执行）")
    if os.environ.get("LARK_CHANNEL") == "1" and not user_auth_only:
        raise SetupError("ticket setup 会（重新）加载 bridge 服务：看起来是在 bridge 驱动的会话里执行（LARK_CHANNEL=1），"
                         "会把这个会话自己打断。请在你自己的终端里执行")
    w = wizard or Wizard()
    if not update.current_link().exists() or not (bridge.app_vendor() / bridge.BRIDGE_ENTRY).is_file():
        raise SetupError("还不是带 bridge 的受管安装：先用一行安装（README「安装」）")
    profile = bridge.profile_name(profile)
    bridge.service_id(profile)
    if user_auth_only:
        w.step_user_auth(profile, ask=False)
        return 0
    if not w.t.interactive:
        w.t.say("不在终端里：装好的部分已就绪。请在终端里执行 %s setup 完成扫码和认主人" %
                (update.current_link() / "bin" / "ticket"))
        return 2
    out = bridge.ensure_runtime(update.current_link().resolve())
    for line in out:
        w.t.say(line)
    ws = Path(os.path.expanduser(workspace or DEFAULT_WORKSPACE))
    ws = Path(w.step_app(profile, ws, app_id)).expanduser()
    w.step_workspace(ws)
    managed = w.step_services(profile)
    if managed:
        w.step_owner(profile)
    else:
        w.t.say("④ 主人：bridge 是分开安装的，迁移后再执行 ticket setup 认主人（或手动填 TICKETD_OWNER）")
    w.step_user_auth(profile)
    w.step_check()
    if host.system() == "linux":
        w.t.say("Linux：要常驻请确认已开启 linger（sudo loginctl enable-linger %s）" % os.environ.get("USER", ""))
    return 0

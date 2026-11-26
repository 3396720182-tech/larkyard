"""原生 Windows 上安装 / 卸载 ticketd 服务（spec §3.2）。由 scripts/windows/install.ps1 / uninstall.ps1 调用，
也可以直接 `py -3 -m ticketd.wininstall install|uninstall|print`（在仓库目录里）。

对应 macOS 的 scripts/install.sh + launchd plist：
- 计划任务（登录时触发，以当前用户、普通权限运行）跑 `pythonw -m ticketd.winsvc run`（托管进程，见 winsvc.py）。
  用 XML 注册：schtasks 命令行建的任务默认「运行超过 72 小时就停止」「改用电池时停止」，常驻服务不能要。
  公司策略不让建计划任务时，退回「启动」文件夹里的快捷方式（效果相同，只是没有失败重试）。
- ~/.ticketd/service.env：守护进程的环境（PATH、bridge profile 的变量，和 plist 里的一致）。
- 把仓库的 bin 加进用户 PATH，设 TICKETD_PYTHON：bridge 里的主会话、worker 都要能直接执行 ticket。
可重复执行：先停掉在跑的，再装、再启动。只用标准库。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from . import config, host, winsvc

REPO = Path(__file__).resolve().parent.parent
TOOLS = ("node", "lark-cli", "claude")


class InstallError(Exception):
    pass


def label() -> str:
    lab = os.environ.get("TICKETD_LABEL", "dev.larkyard.ticketd")
    if not lab or any(not (c.isalnum() or c in "._-") for c in lab):
        raise InstallError("TICKETD_LABEL 只能含字母、数字和 . _ -")
    return lab


def pythonw() -> str:
    """托管进程用 pythonw.exe（不弹控制台窗口）；找不到就用当前解释器。"""
    exe = Path(os.environ.get("TICKETD_PYTHON") or sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def python() -> str:
    exe = Path(os.environ.get("TICKETD_PYTHON") or sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return str(exe)


def find_tools(which=shutil.which) -> Tuple[Dict[str, str], List[str]]:
    """必需的工具（node、lark-cli、claude）和可选的（codex）所在路径；返回 (路径表, 缺的必需工具)。"""
    found, missing = {}, []
    for t in TOOLS + ("codex",):
        p = which(t)
        if p:
            found[t] = p
        elif t in TOOLS:
            missing.append(t)
    return found, missing


def service_env(found: Dict[str, str], env: Optional[dict] = None) -> Dict[str, str]:
    """守护进程的环境（写进 service.env），和 plist 的 EnvironmentVariables 一一对应。"""
    env = dict(os.environ if env is None else env)
    lc_home = env.get("LARK_CHANNEL_HOME") or str(Path.home() / ".lark-channel")
    lc_profile = env.get("LARK_CHANNEL_PROFILE") or "claude"
    dirs = [str(REPO / "bin")]
    for t in ("lark-cli", "node", "claude", "codex"):
        if t in found:
            d = str(Path(found[t]).parent)
            if d not in dirs:
                dirs.append(d)
    have = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    path = os.pathsep.join(dirs + [p for p in have if p not in dirs])
    out = {
        "PATH": path,
        "TICKETD_HOME": str(config.home()),
        "TICKETD_PYTHON": python(),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "LARK_CHANNEL": "1",
        "LARK_CHANNEL_HOME": lc_home,
        "LARK_CHANNEL_PROFILE": lc_profile,
        "LARK_CHANNEL_CONFIG": str(Path(lc_home) / "profiles" / lc_profile / "lark-cli-source" / "config.json"),
        "LARKSUITE_CLI_CONFIG_DIR": str(Path(lc_home) / "profiles" / lc_profile / "lark-cli"),
    }
    bash = host.git_bash()
    if bash:
        out["CLAUDE_CODE_GIT_BASH_PATH"] = bash
    return out


def render_env(env: Dict[str, str]) -> str:
    return "# 由 ticketd.wininstall 生成：ticketd 守护进程的环境（重新安装会覆盖）\n" + \
        "".join("%s=%s\n" % (k, v) for k, v in env.items())


def task_xml(user: str) -> str:
    """计划任务定义（schtasks /XML）。登录时触发；不限运行时长；用电池也跑；失败每分钟重试。"""
    args = '-m ticketd.winsvc run --home "%s"' % config.home()
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Larkyard ticketd (%(label)s)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>%(user)s</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>%(user)s</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>%(cmd)s</Command><Arguments>%(args)s</Arguments><WorkingDirectory>%(cwd)s</WorkingDirectory></Exec></Actions>
</Task>
""" % {"label": escape(label()), "user": escape(user), "cmd": escape(pythonw()), "args": escape(args),
       "cwd": escape(str(REPO))}


def current_user() -> str:
    dom, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return "%s\\%s" % (dom, name) if dom else name


def startup_shortcut() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / \
        "Programs" / "Startup" / ("%s.lnk" % label())


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _run(argv, run=subprocess.run):
    try:
        return run(argv, capture_output=True, text=True, errors="replace")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def register(run=subprocess.run) -> str:
    """注册开机（登录）自启；返回用的方式："task" 或 "startup"。"""
    xml = config.home() / ("%s.task.xml" % label())
    xml.write_text(task_xml(current_user()), encoding="utf-16")
    p = _run(["schtasks", "/Create", "/TN", label(), "/XML", str(xml), "/F"], run)
    if p.returncode == 0:
        _remove_shortcut()
        return "task"
    print("install: ⚠️ 计划任务没建成（%s），改用「启动」文件夹" % (p.stderr or p.stdout).strip()[:200],
          file=sys.stderr)
    lnk = startup_shortcut()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);$s.TargetPath=%s;$s.Arguments=%s;"
              "$s.WorkingDirectory=%s;$s.WindowStyle=7;$s.Save()" % (
                  _ps_quote(str(lnk)), _ps_quote(pythonw()),
                  _ps_quote('-m ticketd.winsvc run --home "%s"' % config.home()), _ps_quote(str(REPO))))
    p = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], run)
    if p.returncode != 0 or not lnk.exists():
        raise InstallError("计划任务和「启动」文件夹快捷方式都没建成：%s" % (p.stderr or p.stdout).strip()[:300])
    return "startup"


def _remove_shortcut() -> None:
    try:
        startup_shortcut().unlink()
    except OSError:
        pass


def start(how: str, run=subprocess.run, popen=subprocess.Popen, wait: float = 15.0) -> None:
    """现在就启动。计划任务 /Run 成功不等于真的跑起来了（比如任务只在交互登录时能跑）：看托管进程的锁，
    等不到就直接拉起一个（下次登录时照常由计划任务启动）。"""
    if how == "task":
        p = _run(["schtasks", "/Run", "/TN", label()], run)
        if p.returncode == 0:
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if winsvc._running(winsvc.lock_path()):
                    return
                time.sleep(0.5)
            print("install: ⚠️ 计划任务启动后 %d 秒还没看到托管进程，直接启动一个" % wait, file=sys.stderr)
    from .winapi import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, DETACHED_PROCESS
    popen([pythonw(), "-m", "ticketd.winsvc", "run", "--home", str(config.home())], cwd=str(REPO),
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, close_fds=True)


def _user_env_set(name: str, value: Optional[str]) -> None:
    """写 / 删用户环境变量（HKCU\\Environment），并通知已打开的程序（新开的终端才会看到）。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        if value is None:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
        else:
            kind = winreg.REG_EXPAND_SZ if "%" in value else winreg.REG_SZ
            winreg.SetValueEx(k, name, 0, kind, value)
    _broadcast_env()


def _user_env_get(name: str) -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return ""


def _broadcast_env() -> None:
    import ctypes
    try:
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except Exception:
        pass


def path_with(path: str, d: str, add: bool) -> str:
    parts = [p for p in path.split(";") if p]
    keep = [p for p in parts if os.path.normcase(p.rstrip("\\/")) != os.path.normcase(d.rstrip("\\/"))]
    return ";".join(([d] if add else []) + keep)


def preflight(found, missing) -> List[str]:
    """装之前的检查：缺必需工具直接停；沙箱缺件只警告（worker 起不来时会如实报，不会裸跑）。返回警告列表。"""
    if sys.version_info < (3, 9):
        raise InstallError("需要 Python 3.9+（当前 %s）" % sys.version.split()[0])
    if missing:
        raise InstallError("PATH 上没有：%s（node 装 LTS 版；lark-cli 和 claude 按 README 安装）" % "、".join(missing))
    warns = []
    if not host.git_bash():
        warns.append("没找到 Git Bash：Claude worker 要用它跑 Bash 命令，先装 Git for Windows")
    if config.win_sandbox() == "srt":
        from . import worker
        if not worker.srt_path():
            warns.append("没找到 sandbox-runtime（srt）：worker 会拒绝启动。装法：npm install -g "
                         "@anthropic-ai/sandbox-runtime，再执行一次 npx @anthropic-ai/sandbox-runtime windows-install"
                         "（弹一次管理员确认）；确实不要沙箱时在 config.env 写 TICKETD_WIN_SANDBOX=off")
    else:
        warns.append("TICKETD_WIN_SANDBOX=off：worker 的 Bash 命令不在沙箱里跑，只有 Claude 的读写工具受限")
    return warns


def install(run=subprocess.run, popen=subprocess.Popen, wait: float = 20.0) -> int:
    if not host.is_windows():
        raise InstallError("这是 Windows 的安装程序；macOS / Linux 用 scripts/install.sh")
    found, missing = find_tools()
    for w in preflight(found, missing):
        print("install: ⚠️ " + w, file=sys.stderr)
    home = config.home()
    (home / "logs").mkdir(parents=True, exist_ok=True)
    if not config.config_file().exists():
        shutil.copyfile(str(REPO / "config.example.env"), str(config.config_file()))
        print("created: %s（先填好 TICKETD_OWNER）" % config.config_file())
    if not config.owner():
        print("install: ⚠️ %s 里还没有 TICKETD_OWNER，派单会被拒绝" % config.config_file(), file=sys.stderr)
    config.workroot().mkdir(parents=True, exist_ok=True)
    env = service_env(found)
    winsvc.service_env_path().write_text(render_env(env), encoding="utf-8")
    winsvc.stop()
    how = register(run)
    start(how, run, popen)
    _user_env_set("PATH", path_with(_user_env_get("PATH"), str(REPO / "bin"), True))
    _user_env_set("TICKETD_PYTHON", python())
    end = time.monotonic() + wait
    hb = config.heartbeat_path()
    before = hb.stat().st_mtime if hb.exists() else 0
    while time.monotonic() < end:
        if hb.exists() and hb.stat().st_mtime > before:
            break
        time.sleep(1)
    else:
        print("install: ⚠️ %d 秒内没看到守护进程心跳，看看 %s" % (wait, home / "logs" / "daemon.err.log"),
              file=sys.stderr)
    print("installed: %s（%s）" % (label(), "计划任务" if how == "task" else "「启动」文件夹"))
    print("workroot: %s" % config.workroot())
    print("PATH: 已把 %s 加进用户 PATH；重开终端，并重启 bridge（lark-channel-bridge service restart）让主会话用上 ticket"
          % (REPO / "bin"))
    return 0


def uninstall(run=subprocess.run) -> int:
    if not host.is_windows():
        raise InstallError("这是 Windows 的卸载程序；macOS / Linux 用 scripts/uninstall.sh")
    winsvc.stop()
    _run(["schtasks", "/Delete", "/TN", label(), "/F"], run)
    _remove_shortcut()
    if host.is_windows():
        _user_env_set("PATH", path_with(_user_env_get("PATH"), str(REPO / "bin"), False))
    print("uninstalled %s（数据保留在 %s 和工单目录）" % (label(), config.home()))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "install"
    try:
        if cmd == "install":
            return install()
        if cmd == "uninstall":
            return uninstall()
        if cmd == "print":
            found, _ = find_tools()
            print(render_env(service_env(found)))
            print(task_xml(current_user()))
            return 0
    except InstallError as e:
        print("install: " + str(e), file=sys.stderr)
        return 1
    print("用法：python -m ticketd.wininstall install|uninstall|print", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

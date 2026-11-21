"""打包进来的 lark-channel-bridge：运行时（Node、lark-cli）、bridge 链接、服务定义、按需重启、从分开安装迁移。

行为以 openspec/changes/vendor-bridge-one-line-install 为准，实现约定见 docs/spec.md §13。
平台差异（launchd / systemd）集中在 Services 里；测试注入假的 run 函数和 host.system()。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform as _platform
import plistlib
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

from . import config, host

RUNTIME_LIMIT = 200 * 1024 * 1024
RUNTIME_TIMEOUT = 900.0
HEALTH_WAIT = 90.0
RESTART_LABEL = "dev.larkyard.bridge-restart"
MANAGED_MARK = "LARKYARD_MANAGED"          # Larkyard 写的 bridge 服务带这个环境变量，用来和用户自己装的区分
TICKET_BIN_ENV = "LARKYARD_TICKET_BIN"         # bridge 派单用的 ticket 命令（会议待办 → 工单）
BRIDGE_ENTRY = Path("bin") / "lark-channel-bridge.mjs"
PLATFORMS = ("darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64")
ONLINE_EVENTS = (("ws", "connected"), ("supervisor", "profile-online"))
_UNSAFE_UNIT = re.compile(r'[\s%$"\\]')


class BridgeError(Exception):
    pass


# ---------- 路径 ----------
def runtime_dir() -> Path:
    return config.home() / "runtime"


def node_link() -> Path:
    return runtime_dir() / "node"


def node_bin() -> Path:
    return node_link() / "bin" / "node"


def bin_dir() -> Path:
    return config.home() / "bin"


def lark_cli() -> Path:
    return bin_dir() / "lark-cli"


def bridge_home() -> Path:
    return config.home() / "bridge"


def bridge_link() -> Path:
    return bridge_home() / "current"


def bridge_entry() -> Path:
    return bridge_link() / BRIDGE_ENTRY


def state_path() -> Path:
    return bridge_home() / "state.json"


def lc_home() -> Path:
    return Path(os.environ.get("LARK_CHANNEL_HOME") or Path.home() / ".lark-channel").expanduser()


def app_vendor(root: Optional[Path] = None) -> Path:
    """某个版本目录里打包的 bridge；默认当前受管版本（~/.ticketd/app/current）。"""
    from . import update
    return Path(root or update.current_link()) / "vendor" / "bridge"


def platform_key(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    s = system or host.system()
    m = (machine or _platform.machine()).lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(m, m)
    return "%s-%s" % (s, arch)


def _in_worker() -> bool:
    return bool(os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"))


def _refuse_in_worker(what: str, restarts: bool = True) -> None:
    if _in_worker():
        raise BridgeError("worker 里不能执行 %s（要由本人在终端里执行）" % what)
    if restarts and os.environ.get("LARK_CHANNEL") == "1":
        # bridge 给它驱动的会话（主会话）注入 LARK_CHANNEL=1；ticketd 服务的环境也有。在这里重启 bridge
        # 会把正在执行这条命令的会话一起杀掉，后面的确认和切回都来不及跑
        raise BridgeError("%s 会重启 bridge：看起来是在 bridge 驱动的会话里执行（LARK_CHANNEL=1），"
                          "会把这个会话自己打断。请在你自己的终端里执行（不要先 source larkenv.sh）" % what)


# ---------- 内容摘要 ----------
def tree_digest(root: Path, skip: tuple = ("BRIDGE.json",)) -> str:
    """按路径排序的「路径、权限、内容 SHA-256」再取 SHA-256。遇到链接或特殊文件抛 BridgeError。
    发版（release.py）、构建（build_bridge.py）和本机（restart --if-stale）用同一个函数。"""
    root = Path(root)
    if not root.is_dir():
        raise BridgeError("不是目录：%s" % root)
    rows = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                raise BridgeError("bridge 目录里不能有链接：%s" % rel)
            if name in filenames:
                if rel in skip:
                    continue
                if not p.is_file():
                    raise BridgeError("bridge 目录里有特殊文件：%s" % rel)
                mode = "755" if os.stat(str(p)).st_mode & 0o111 else "644"
                rows.append("%s\0%s\0%s\n" % (rel, mode, _file_sha256(p)))
    h = hashlib.sha256()
    for row in sorted(rows):
        h.update(row.encode("utf-8"))
    return h.hexdigest()


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(str(p), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def bridge_info(d: Path) -> Optional[dict]:
    """打包 bridge 的 BRIDGE.json（repo、commit、version、tree_sha256）；没有或读不了返回 None。"""
    try:
        info = json.loads((Path(d) / "BRIDGE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(info.get("commit", ""))):
        return None
    return info


def same_bridge(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a and b and a.get("commit") == b.get("commit") and a.get("tree_sha256") == b.get("tree_sha256"))


# ---------- 状态 ----------
def read_state() -> dict:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(st: dict) -> None:
    bridge_home().mkdir(parents=True, exist_ok=True)
    tmp = state_path().with_name("state.json.tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(state_path()))


def _swap_link(link: Path, target: str) -> None:
    """原子地把 link 指向 target（相对路径）：先建临时链接再 rename。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(".%s.tmp-%d" % (link.name, os.getpid()))
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target, str(tmp))
    os.replace(str(tmp), str(link))


def link_bridge(version_dir: Path) -> str:
    """bridge/current → ../app/versions/<版本>/vendor/bridge（相对链接）。返回原来的目标（没有为空）。"""
    vendor = Path(version_dir) / "vendor" / "bridge"
    if not (vendor / BRIDGE_ENTRY).is_file():
        raise BridgeError("%s 里没有打包的 bridge" % version_dir)
    old = ""
    if bridge_link().is_symlink():
        old = os.readlink(str(bridge_link()))
    rel = os.path.relpath(str(vendor), str(bridge_home()))
    _swap_link(bridge_link(), rel)
    return old


def bridge_version_dir() -> Optional[str]:
    """bridge/current 指向的版本目录名（清理旧版本时要保留它）。"""
    try:
        p = bridge_link().resolve()
    except OSError:
        return None
    if p.name == "bridge" and p.parent.name == "vendor":
        return p.parent.parent.name
    return None


# ---------- 运行时 ----------
def load_runtime(root: Path) -> dict:
    """<版本目录>/vendor/runtime.json，按格式检查。"""
    try:
        spec = json.loads((Path(root) / "vendor" / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError("读不了 vendor/runtime.json：%s" % e)
    for tool in ("node", "lark_cli"):
        t = spec.get(tool)
        if not isinstance(t, dict) or not re.fullmatch(r"\d+\.\d+\.\d+", str(t.get("version", ""))):
            raise BridgeError("runtime.json 的 %s 缺版本号" % tool)
        for k, a in (t.get("assets") or {}).items():
            if not (isinstance(a, dict) and re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256", "")))
                    and str(a.get("url", "")).startswith("https://")):
                raise BridgeError("runtime.json 的 %s/%s 不完整" % (tool, k))
    return spec


def _asset(spec: dict, tool: str, plat: str) -> dict:
    a = (spec[tool].get("assets") or {}).get(plat)
    if not a:
        raise BridgeError("%s 没有 %s 平台的包（支持：%s）" % (tool, plat, "、".join(sorted(spec[tool].get("assets") or {}))))
    return a


def fetch_verified(urls: list, dest: Path, sha256: str, limit: int = RUNTIME_LIMIT,
                   timeout: float = RUNTIME_TIMEOUT, opener=None, attempts: int = 3) -> str:
    """按顺序试每个地址，边下边算 SHA-256，对上了才落到 dest。返回用上的地址；都不行抛 BridgeError。
    连接中途断开时 urllib 不一定报错、只是提前读到结尾：按 Content-Length 判断「不完整」，同一个地址重试。"""
    from . import update
    opener = opener or update._open  # 只跟随到 https 的重定向
    errors = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name("%s.part-%d" % (dest.name, os.getpid()))
    for url in urls:
        if urllib.parse.urlparse(url).scheme not in ("https", "file"):
            errors.append("%s：只接受 https" % url)
            continue
        for attempt in range(attempts):
            h, n = hashlib.sha256(), 0
            retry = False
            try:
                started = time.monotonic()
                with opener(url, 60) as r, open(str(part), "wb") as f:
                    want = getattr(r, "headers", None) and r.headers.get("Content-Length")
                    while True:
                        if time.monotonic() - started > timeout:
                            raise BridgeError("超过 %d 秒" % timeout)
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > limit:
                            raise BridgeError("超过 %d 字节" % limit)
                        h.update(chunk)
                        f.write(chunk)
                if want and str(want).isdigit() and int(want) != n:
                    retry = True
                    raise BridgeError("下载不完整（%d / %s 字节）" % (n, want))
                if h.hexdigest() != sha256:
                    raise BridgeError("SHA-256 不一致")
                os.replace(str(part), str(dest))
                return url
            except (BridgeError, OSError, ValueError, urllib.error.URLError) as e:
                if isinstance(e, (OSError, urllib.error.URLError)) and not isinstance(e, urllib.error.HTTPError):
                    retry = True  # 网络抖动：同一个地址再试
                if not retry or attempt == attempts - 1:
                    errors.append("%s：%s" % (url, getattr(e, "reason", None) or e))
                    break
            finally:
                if part.exists():
                    part.unlink()
    raise BridgeError("下载失败：" + "；".join(errors))


def _extract_members(tgz: Path, dest: Path, want: Callable[[str], Optional[str]]) -> list:
    """只解出 want(成员名) 返回非空的普通文件，放到 dest/<返回的相对路径>。拒绝绝对路径和 ..。"""
    got = []
    with tarfile.open(str(tgz), "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isreg():
                continue
            name = m.name
            if name.startswith("/") or ".." in name.split("/"):
                raise BridgeError("包里有不安全的路径：%r" % name)
            rel = want(name)
            if not rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(m) as src, open(str(target), "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(str(target), 0o755 if m.mode & 0o111 else 0o644)
            got.append(rel)
    return got


def ensure_runtime(root: Path, plat: Optional[str] = None, fetch=fetch_verified) -> list:
    """按 root/vendor/runtime.json 确保 Node 和 lark-cli 在固定目录里、链接指向对的版本。返回做了什么（给人看）。
    已经是这个版本就只核对文件；下载或核对失败抛 BridgeError，原来的链接不动。"""
    spec = load_runtime(root)
    plat = plat or platform_key()
    done = []
    rd = runtime_dir()
    rd.mkdir(parents=True, exist_ok=True)

    nv = spec["node"]["version"]
    node_dir = "node-v%s-%s" % (nv, plat)
    if not (rd / node_dir / "bin" / "node").is_file():
        a = _asset(spec, "node", plat)
        tgz = rd / (".%s.tar.gz" % node_dir)
        tmp = rd / (".%s.tmp-%d" % (node_dir, os.getpid()))
        try:
            fetch([a["url"]] + list(a.get("mirrors") or []), tgz, a["sha256"])
            shutil.rmtree(str(tmp), ignore_errors=True)
            top = "node-v%s-%s/" % (nv, plat)
            got = _extract_members(tgz, tmp, lambda n: n[len(top):] if n in (top + "bin/node", top + "LICENSE") else None)
            if "bin/node" not in got:
                raise BridgeError("Node 包里没有 bin/node")
            shutil.rmtree(str(rd / node_dir), ignore_errors=True)
            os.replace(str(tmp), str(rd / node_dir))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)
            if tgz.exists():
                tgz.unlink()
        done.append("已下载 Node %s（%s）" % (nv, plat))
    if not node_link().is_symlink() or os.readlink(str(node_link())) != node_dir:
        _swap_link(node_link(), node_dir)
        done.append("runtime/node → %s" % node_dir)

    lv = spec["lark_cli"]["version"]
    lark_dir = "lark-cli-%s" % lv
    if not (rd / lark_dir / "lark-cli").is_file():
        a = _asset(spec, "lark_cli", plat)
        tgz = rd / (".%s-%s.tar.gz" % (lark_dir, plat))
        tmp = rd / (".%s.tmp-%d" % (lark_dir, os.getpid()))
        try:
            fetch([a["url"]] + list(a.get("mirrors") or []), tgz, a["sha256"])
            shutil.rmtree(str(tmp), ignore_errors=True)
            got = _extract_members(tgz, tmp, lambda n: {"lark-cli": "lark-cli", "LICENSE": "LICENSE"}.get(n.split("/")[-1]))
            if "lark-cli" not in got:
                raise BridgeError("lark-cli 包里没有 lark-cli")
            os.chmod(str(tmp / "lark-cli"), 0o755)
            shutil.rmtree(str(rd / lark_dir), ignore_errors=True)
            os.replace(str(tmp), str(rd / lark_dir))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)
            if tgz.exists():
                tgz.unlink()
        done.append("已下载 lark-cli %s（%s）" % (lv, plat))
    want = os.path.relpath(str(rd / lark_dir / "lark-cli"), str(bin_dir()))
    if not lark_cli().is_symlink() or os.readlink(str(lark_cli())) != want:
        bin_dir().mkdir(parents=True, exist_ok=True)
        _swap_link(lark_cli(), want)
        done.append("bin/lark-cli → %s" % lark_dir)
    return done


def runtime_versions() -> dict:
    """在用的 Node / lark-cli 版本（按链接目标的目录名）。"""
    out = {}
    try:
        m = re.match(r"node-v([\d.]+)-", os.readlink(str(node_link())))
        out["node"] = m.group(1) if m else ""
    except OSError:
        out["node"] = ""
    try:
        m = re.search(r"lark-cli-([\d.]+)", os.readlink(str(lark_cli())))
        out["lark_cli"] = m.group(1) if m else ""
    except OSError:
        out["lark_cli"] = ""
    return out


# ---------- 服务定义 ----------
def profile_name(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("LARK_CHANNEL_PROFILE")
    if env:
        return env
    try:
        p = (lc_home() / "active-profile").read_text(encoding="utf-8").strip()
        if p:
            return p
    except OSError:
        pass
    return "claude"


def service_id(profile: str) -> str:
    """和 bridge 的 serviceProfileId 一致：ASCII 安全的名字原样用。其他名字 Larkyard 不管（bridge 会加哈希）。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", profile) or profile in (".", ".."):
        raise BridgeError("profile 名字 %r 不是纯 ASCII：请用字母、数字和 . _ -" % profile)
    return profile


def bridge_label(profile: str) -> str:
    if host.system() == "darwin":
        return "ai.lark-channel-bridge.bot.%s" % service_id(profile)
    return "lark-channel-bridge.bot.%s" % service_id(profile)


def _user_unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "systemd" / "user"


def bridge_service_file(profile: str) -> Path:
    if host.system() == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % bridge_label(profile))
    return _user_unit_dir() / ("%s.service" % bridge_label(profile))


def restart_service_file(label: str = RESTART_LABEL) -> Path:
    if host.system() == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % label)
    return _user_unit_dir() / ("%s.service" % label)


def daemon_logs(profile: str, home: Optional[Path] = None) -> tuple:
    d = Path(home or lc_home()) / "profiles" / profile / "logs" / "daemon"
    return d / "daemon-stdout.log", d / "daemon-stderr.log"


def service_path(claude_dir: str = "", codex_dir: str = "", extra: str = "") -> str:
    """bridge 服务的 PATH：固定目录的 Node 和 lark-cli 在前（不跟着 nvm 走）；之后是 claude / codex 所在目录和
    extra（迁移时是原服务的 PATH，新装时是当前 PATH 上 node 所在的目录），主会话用 npm 全局装的工具、npx 还能找到。"""
    parts = [str(node_link() / "bin"), str(bin_dir())]
    for d in [claude_dir, codex_dir] + [x for x in (extra or "").split(":")]:
        if d and d not in parts and not (host.is_linux() and d.startswith("/mnt/")):
            parts.append(d)
    for d in (["/usr/local/bin"] if host.is_linux() else []) + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        if d not in parts:
            parts.append(d)
    return ":".join(parts)


def tool_dir(name: str) -> str:
    """claude / codex 所在目录（服务 PATH 用）：找不到为空。WSL2 里 Windows 盘上的不要。"""
    p = shutil.which(name)
    if not p:
        return ""
    d = str(Path(p).parent)
    if host.is_linux() and d.startswith("/mnt/"):
        return ""
    return d


def render_bridge_service(profile: str, path: str, home: Optional[Path] = None,
                          stdout: Optional[Path] = None, stderr: Optional[Path] = None) -> bytes:
    """bridge 服务定义。入口是固定目录的 Node + bridge/current，服务名和日志位置和 bridge 自己装的一样。"""
    home = Path(home or lc_home())
    out, err = daemon_logs(profile, home)
    out, err = stdout or out, stderr or err
    from . import update
    args = [str(node_bin()), str(bridge_entry()), "run", "--profile", profile]
    env = {"PATH": path, "LARK_CHANNEL_HOME": str(home), "HOME": str(Path.home()), MANAGED_MARK: "1",
           # 会议纪要卡片的「派成工单」按钮由 bridge 调 ticket new；跟着版本走的 current，不是某个版本目录
           TICKET_BIN_ENV: str(update.app_dir() / "current" / "bin" / "ticket")}
    if host.system() == "darwin":
        return plistlib.dumps({
            "Label": bridge_label(profile), "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
            "StandardOutPath": str(out), "StandardErrorPath": str(err), "EnvironmentVariables": env,
        }, sort_keys=False)
    for v in args + list(env.values()) + [str(out), str(err)]:
        if _UNSAFE_UNIT.search(v):
            raise BridgeError("Linux 上路径里不能有空白或 %% $ \" \\ 字符：%s" % v)
    lines = [
        "[Unit]", "Description=lark-channel-bridge (%s, managed by Larkyard)" % profile,
        "After=network-online.target", "Wants=network-online.target", "",
        "[Service]", "Type=simple",
        "ExecStart=%s" % " ".join('"%s"' % a for a in args),
        "Restart=always", "RestartSec=5",
    ] + ['Environment="%s=%s"' % (k, v) for k, v in env.items()] + [
        "StandardOutput=append:%s" % out, "StandardError=append:%s" % err, "",
        "[Install]", "WantedBy=default.target", "",
    ]
    return "\n".join(lines).encode("utf-8")


def render_restart_job(ticket_bin: Path, args: list, hour: int = 4, minute: int = 7, label: str = RESTART_LABEL,
                       env: Optional[dict] = None) -> dict:
    """每天的 bridge 重启任务。macOS 返回 {plist 路径: 内容}；Linux 返回 service + timer 两个文件。"""
    env = dict(env or {})
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.setdefault("TICKETD_HOME", str(config.home()))
    log = config.home() / "logs" / "bridge-restart.log"
    cmd = [str(ticket_bin), "bridge"] + list(args)
    if host.system() == "darwin":
        data = plistlib.dumps({
            "Label": label, "ProgramArguments": cmd, "RunAtLoad": False, "ProcessType": "Background",
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(log), "StandardErrorPath": str(log), "EnvironmentVariables": env,
        }, sort_keys=False)
        return {restart_service_file(label): data}
    for v in cmd + list(env.values()) + [str(log)]:
        if _UNSAFE_UNIT.search(v):
            raise BridgeError("Linux 上路径里不能有空白或 %% $ \" \\ 字符：%s" % v)
    svc = "\n".join(["[Unit]", "Description=Restart lark-channel-bridge when needed (Larkyard)", "",
                     "[Service]", "Type=oneshot", "ExecStart=%s" % " ".join('"%s"' % a for a in cmd)]
                    + ['Environment="%s=%s"' % (k, v) for k, v in env.items()]
                    + ["StandardOutput=append:%s" % log, "StandardError=append:%s" % log, ""])
    timer = "\n".join(["[Unit]", "Description=Daily lark-channel-bridge restart window (Larkyard)", "",
                       "[Timer]", "OnCalendar=*-*-* %02d:%02d:00" % (hour, minute), "Persistent=false", "",
                       "[Install]", "WantedBy=timers.target", ""])
    unit = restart_service_file(label)
    return {unit: svc.encode("utf-8"), unit.with_suffix(".timer"): timer.encode("utf-8")}


def is_managed_service(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if path.suffix == ".plist":
        try:
            return (plistlib.loads(raw).get("EnvironmentVariables") or {}).get(MANAGED_MARK) == "1"
        except Exception:
            return False
    return ('Environment="%s=1"' % MANAGED_MARK).encode() in raw


def service_program(path: Path) -> list:
    """服务定义里的启动命令（参数列表）；读不了为空。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if path.suffix == ".plist":
        try:
            return [str(a) for a in plistlib.loads(raw).get("ProgramArguments") or []]
        except Exception:
            return []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("ExecStart="):
            v = line[len("ExecStart="):].strip()
            return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', v)]
    return []


def service_env(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    if path.suffix == ".plist":
        try:
            return {k: str(v) for k, v in (plistlib.loads(raw).get("EnvironmentVariables") or {}).items()}
        except Exception:
            return {}
    env = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        m = re.match(r'^Environment="?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?$', line.strip())
        if m:
            env[m.group(1)] = m.group(2)
    return env


def service_logs(path: Path) -> tuple:
    try:
        raw = path.read_bytes()
    except OSError:
        return "", ""
    if path.suffix == ".plist":
        try:
            d = plistlib.loads(raw)
            return str(d.get("StandardOutPath") or ""), str(d.get("StandardErrorPath") or "")
        except Exception:
            return "", ""
    out = err = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("StandardOutput=append:"):
            out = line.split(":", 1)[1]
        elif line.startswith("StandardError=append:"):
            err = line.split(":", 1)[1]
    return out, err


NVM_HINTS = ("/.nvm/", "/node_modules/", "/.volta/", "/.fnm/", "/.asdf/")


def pinned_to_user_node(paths) -> bool:
    return any(h in p for p in paths for h in NVM_HINTS)


# ---------- 服务管理器 ----------
class Services:
    """launchctl / systemctl 的薄封装。run 可注入（测试用假的）。"""

    def __init__(self, run=None):
        self.run = run or (lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True, timeout=120, **kw))

    def _ok(self, argv) -> bool:
        try:
            return self.run(argv).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def load(self, label: str, path: Path, timer: bool = False) -> None:
        """(重新)加载并启动：macOS bootout + bootstrap；Linux daemon-reload + enable + restart。"""
        if host.system() == "darwin":
            dom = "gui/%d" % os.getuid()
            self._ok(["launchctl", "enable", "%s/%s" % (dom, label)])  # lark-channel-bridge stop 会 disable
            self._ok(["launchctl", "bootout", "%s/%s" % (dom, label)])
            for _ in range(50):
                if not self._ok(["launchctl", "print", "%s/%s" % (dom, label)]):
                    break
                time.sleep(0.2)
            for i in range(3):
                if self._ok(["launchctl", "bootstrap", dom, str(path)]):
                    return
                time.sleep(1 + i)
            raise BridgeError("launchctl bootstrap %s 失败" % label)
        unit = "%s.%s" % (label, "timer" if timer else "service")
        self._ok(["systemctl", "--user", "daemon-reload"])
        if not self._ok(["systemctl", "--user", "enable", unit]):
            raise BridgeError("systemctl --user enable %s 失败" % unit)
        if not self._ok(["systemctl", "--user", "restart", unit]):
            raise BridgeError("systemctl --user restart %s 失败" % unit)

    def restart(self, label: str) -> None:
        if host.system() == "darwin":
            if not self._ok(["launchctl", "kickstart", "-k", "gui/%d/%s" % (os.getuid(), label)]):
                raise BridgeError("launchctl kickstart %s 失败" % label)
            return
        if not self._ok(["systemctl", "--user", "restart", "%s.service" % label]):
            raise BridgeError("systemctl --user restart %s 失败" % label)

    def unload(self, label: str, timer: bool = False) -> None:
        if host.system() == "darwin":
            self._ok(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)])
        else:
            self._ok(["systemctl", "--user", "disable", "--now", "%s.%s" % (label, "timer" if timer else "service")])
            self._ok(["systemctl", "--user", "daemon-reload"])


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    tmp.write_bytes(data)
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))


def secrets_getter_text(home: Optional[Path] = None) -> str:
    """和 bridge 自己生成的同一格式（bridge 每次启动按自己的 Node 和入口重写，内容会一致）。"""
    sq = lambda s: "'" + str(s).replace("'", "'\\''") + "'"  # noqa: E731
    return ("#!/bin/sh\n# Auto-generated by lark-channel-bridge. Do not edit.\n"
            "# Forwards exec-provider requests to: node bridge secrets get\n"
            "LARK_CHANNEL_HOME=%s exec %s %s secrets get \"$@\"\n" % (
                sq(Path(home or lc_home())), sq(node_bin()), sq(bridge_entry())))


# ---------- 在线确认 ----------
def _parse_ts(s: str) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def online_since(profile: str, since: float, home: Optional[Path] = None) -> bool:
    """bridge 在 since 之后有没有记「长连接已连上」（ws connected / profile-online）。看今天和昨天的日志。"""
    logs = Path(home or lc_home()) / "profiles" / profile / "logs"
    for day in (0, 1):
        t = time.localtime(time.time() - day * 86400)
        p = logs / ("bridge-%s.jsonl" % time.strftime("%Y%m%d", t))
        try:
            with open(str(p), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - (2 << 20)))  # 只看最后 2MB
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(d.get("ts", ""))
            if ts is None or ts < since:
                continue
            if (d.get("phase"), d.get("event")) in ONLINE_EVENTS:
                return True
    return False


def wait_online(profile: str, since: float, timeout: float = HEALTH_WAIT, sleep=time.sleep, clock=time.time,
                home: Optional[Path] = None) -> bool:
    deadline = clock() + timeout
    while True:
        if online_since(profile, since, home):
            return True
        if clock() >= deadline:
            return False
        sleep(3)


# ---------- 重启（换上新 bridge） ----------
def stale(app_current: Optional[Path] = None) -> Optional[tuple]:
    """当前版本带的 bridge 和在用的不同：返回 (当前版本目录, 新 BRIDGE.json, 在用 BRIDGE.json)；相同或没有打包 bridge 返回 None。"""
    from . import update
    cur = Path(app_current or update.current_link())
    new = bridge_info(app_vendor(cur))
    if not new:
        return None
    old = bridge_info(bridge_link()) if bridge_link().exists() else None
    if same_bridge(new, old):
        return None
    return cur.resolve(), new, old


def restart(profile: str = "", if_stale: bool = False, services: Optional[Services] = None,
            fetch=fetch_verified, wait=wait_online, notify=None) -> list:
    """切到当前版本带的 bridge（必要时）、确保运行时、重启 bridge 服务、确认在线；不在线就切回再重启一次。"""
    from . import update
    _refuse_in_worker("ticket bridge restart")
    services = services or Services()
    profile = profile_name(profile)
    sf = bridge_service_file(profile)
    if not is_managed_service(sf):
        raise BridgeError("%s 不是 Larkyard 写的 bridge 服务：先执行 ticket setup 或 ticket bridge adopt" % sf)
    out = []
    if update.read_state().get("status") == "probation":
        # 新版本还在试用期（可能被切回）：等它确认了再换 bridge
        if if_stale:
            return ["Larkyard 新版本还在试用期，bridge 等它确认后再换"]
        target = None
    else:
        target = stale()
    if if_stale and not target:
        return ["bridge 已是当前版本带的那一份，不用重启"]
    home = Path(service_env(sf).get("LARK_CHANNEL_HOME") or lc_home())
    cur = Path(update.current_link()).resolve()
    out += ensure_runtime(cur, fetch=fetch)
    old = ""
    st = read_state()
    if target:
        old = link_bridge(target[0])
        st.update({"previous": old, "commit": target[1].get("commit"), "version": target[1].get("version"),
                   "tree_sha256": target[1].get("tree_sha256"), "switched_at": int(time.time())})
        write_state(st)
        out.append("bridge → %s（%s）" % (target[1].get("version"), str(target[1].get("commit"))[:12]))
    started = time.time()
    try:
        services.restart(bridge_label(profile))
    except BaseException as e:
        if old:  # 链接已切、重启没成：切回，免得下一个窗口当成「已是这个版本」不再重启
            _swap_link(bridge_link(), old)
            info = bridge_info(bridge_link()) or {}
            st = read_state()
            st.update({"previous": "", "commit": info.get("commit"), "version": info.get("version"),
                       "tree_sha256": info.get("tree_sha256")})
            write_state(st)
        st = read_state()
        st["last_restart"] = {"at": int(started), "ok": False, "why": "服务重启失败：%s" % e}
        write_state(st)
        raise
    if wait(profile, started - 1, home=home):
        st = read_state()
        st["last_restart"] = {"at": int(started), "ok": True}
        write_state(st)
        update_pending_clear()
        out.append("bridge 已重启并连上飞书")
        return out
    # 不在线：切回原来的 bridge（如果切过）再重启一次
    why = "重启后 %d 秒内没有连上飞书" % HEALTH_WAIT
    if old:
        _swap_link(bridge_link(), old)
        st = read_state()
        st.update({"previous": "", "rolled_back_from": st.get("commit")})
        info = bridge_info(bridge_link()) or {}
        st.update({"commit": info.get("commit"), "version": info.get("version"), "tree_sha256": info.get("tree_sha256")})
        write_state(st)
        services.restart(bridge_label(profile))
        back = wait(profile, time.time() - 1, home=home)
        why += "，已切回原来的 bridge（%s）" % ("已连上" if back else "仍没连上，请看 bridge 日志")
    st = read_state()
    st["last_restart"] = {"at": int(started), "ok": False, "why": why}
    write_state(st)
    if notify:
        try:
            notify("⚠️ bridge 重启：" + why)
        except Exception:
            pass
    raise BridgeError(why)


def update_pending_clear() -> None:
    """换上新 bridge 后清掉守护进程记的「待生效」。数据库打不开时忽略（下次 doctor 会按链接重新算）。"""
    try:
        from . import db
        con = db.connect()
        try:
            con.execute("DELETE FROM kv WHERE key = 'bridge.pending'")
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def note_pending(con) -> Optional[str]:
    """新版本确认后调用：带的 bridge 和在用的不同就记 kv bridge.pending（doctor 显示），返回版本号。"""
    from . import metrics
    if not is_managed_service(bridge_service_file(profile_name())):
        return None
    t = stale()
    if not t:
        return None
    metrics.kv_set(con, "bridge.pending", str(t[1].get("version") or ""))
    return t[1].get("version")


# ---------- 状态和自检 ----------
def status_lines(profile: str = "") -> list:
    from . import update
    profile = profile_name(profile)
    sf = bridge_service_file(profile)
    lines = []
    if not sf.exists():
        lines.append("bridge 服务：没有找到 %s" % sf)
    elif is_managed_service(sf):
        lines.append("bridge 服务：%s（Larkyard 管理）" % bridge_label(profile))
    else:
        lines.append("bridge 服务：%s（分开安装，可以用 ticket bridge adopt 迁移）" % bridge_label(profile))
    prog = service_program(sf)
    if prog:
        lines.append("启动命令：%s" % " ".join(prog))
    using = bridge_info(bridge_link()) if bridge_link().exists() else None
    if using:
        lines.append("在用的 bridge：%s（%s，来自版本 %s）" % (using.get("version"), str(using.get("commit"))[:12],
                                                     bridge_version_dir() or "?"))
    try:
        cur = bridge_info(app_vendor())
    except Exception:
        cur = None
    if cur:
        same = same_bridge(cur, using)
        lines.append("当前版本 %s 带的 bridge：%s（%s）%s" % (update.current_version() or "?", cur.get("version"),
                                                       str(cur.get("commit"))[:12], "" if same else " · 待生效"))
    rv = runtime_versions()
    lines.append("Node：%s（%s）" % (rv.get("node") or "未安装", node_bin()))
    lines.append("lark-cli：%s（%s）" % (rv.get("lark_cli") or "未安装", lark_cli()))
    lr = read_state().get("last_restart")
    if lr:
        lines.append("上次重启：%s %s%s" % (time.strftime("%m-%d %H:%M", time.localtime(lr.get("at", 0))),
                                        "成功" if lr.get("ok") else "失败", "" if lr.get("ok") else "（%s）" % lr.get("why", "")))
    if read_state().get("adopted"):
        lines.append("迁移：已从分开安装迁移（ticket bridge adopt --undo 可以退回）")
    return lines


def doctor_line(profile: str = "") -> str:
    profile = profile_name(profile)
    sf = bridge_service_file(profile)
    if not sf.exists():
        return "ℹ️ bridge：没有找到服务 %s（还没装：ticket setup）" % bridge_label(profile)
    prog = service_program(sf)
    if not is_managed_service(sf):
        return "ℹ️ bridge：分开安装（%s）；可以用 ticket bridge adopt 迁移到 Larkyard 打包的 bridge" % (
            prog[1] if len(prog) > 1 else sf)
    missing = [p for p in prog[:2] if not Path(p).exists()]
    if missing:
        return "❌ bridge：服务指向的文件不存在：%s（ticket bridge restart 会重建运行时）" % "、".join(missing)
    if pinned_to_user_node(prog[:2]):  # PATH 里可以有 nvm 下的 claude / codex，Node 在它们前面
        return "❌ bridge：服务的启动命令写死了 nvm / npm 的路径（重新执行 ticket setup）"
    lr = read_state().get("last_restart") or {}
    if lr and not lr.get("ok"):
        return "❌ bridge：上次重启失败（%s）" % lr.get("why", "")
    if stale():
        return "⚠️ bridge：新版本带的 bridge 待生效：每天 04:07 自动换上，或现在 ticket bridge restart（会打断进行中的回复）"
    using = bridge_info(bridge_link()) or {}
    return "✅ bridge：Larkyard 管理 · %s（%s）" % (using.get("version", "?"), str(using.get("commit", ""))[:12])


# ---------- 每天的重启任务 ----------
def find_restart_jobs(exclude: tuple = ()) -> list:
    """用户自己装的「每天重启 bridge」任务：定时运行、命令里有 lark-channel-bridge 和 restart。
    返回 [{label, path, timer, hour, minute}]。Larkyard 自己的任务不算。"""
    found = []
    if host.system() == "darwin":
        d = Path.home() / "Library" / "LaunchAgents"
        for p in sorted(d.glob("*.plist")) if d.is_dir() else []:
            if p in exclude:
                continue
            try:
                doc = plistlib.loads(p.read_bytes())
            except Exception:
                continue
            cmd = " ".join(str(a) for a in doc.get("ProgramArguments") or [])
            cal = doc.get("StartCalendarInterval")
            if "lark-channel-bridge" in cmd and re.search(r"\brestart\b", cmd) and doc.get("Label") != RESTART_LABEL:
                daily = isinstance(cal, dict) and "Hour" in cal and set(cal) <= {"Hour", "Minute"}
                found.append({"label": str(doc.get("Label") or p.stem), "path": str(p), "timer": "",
                              "hour": int(cal.get("Hour", 4)) if daily else -1,
                              "minute": int(cal.get("Minute", 0)) if daily else -1, "daily": daily})
        return found
    d = _user_unit_dir()
    for p in sorted(d.glob("*.service")) if d.is_dir() else []:
        if p in exclude or p.stem == RESTART_LABEL:
            continue
        cmd = " ".join(service_program(p))
        timer = p.with_suffix(".timer")
        if "lark-channel-bridge" in cmd and re.search(r"\brestart\b", cmd):
            text = timer.read_text(encoding="utf-8", errors="replace") if timer.is_file() else ""
            cals = re.findall(r"^OnCalendar=(.*)$", text, re.M)
            m = re.fullmatch(r"(?:\*-\*-\*\s+)?(\d{1,2}):(\d{2})(?::00)?", cals[0].strip()) if len(cals) == 1 else None
            found.append({"label": p.stem, "path": str(p), "timer": str(timer) if timer.is_file() else "",
                          "hour": int(m.group(1)) if m else -1, "minute": int(m.group(2)) if m else -1,
                          "daily": bool(m)})
    return found


def install_restart_job(args: list, label: str = RESTART_LABEL, hour: int = 4, minute: int = 7,
                        services: Optional[Services] = None, env: Optional[dict] = None, on_files=None) -> list:
    """写并加载每天的重启任务。on_files(路径列表) 在写文件之前调用（迁移用它先记下要新建的文件）。"""
    from . import update
    services = services or Services()
    ticket = update.app_dir() / "current" / "bin" / "ticket"
    files = render_restart_job(ticket, args, hour, minute, label, env)
    if on_files:
        on_files([str(p) for p in files])
    for path, data in files.items():
        _write(path, data)
    if host.system() == "darwin":
        services.load(label, next(iter(files)))
    else:
        services.load(label, restart_service_file(label).with_suffix(".timer"), timer=True)
    return [str(p) for p in files]


# ---------- 从分开安装迁移 ----------
def _ticketd_service() -> Optional[tuple]:
    from . import update
    label = os.environ.get("TICKETD_LABEL") or update.DEFAULT_LABEL
    sf = update.service_file(label)
    return (label, sf) if sf.is_file() else None


def adopt_plan(profile: str = "") -> dict:
    """检查前提，算出要改哪些文件。不满足前提抛 BridgeError。不写任何东西。"""
    from . import update
    _refuse_in_worker("ticket bridge adopt", restarts=False)  # 只读：--dry-run 在哪儿都能看
    profile = profile_name(profile)
    if read_state().get("adopted"):
        raise BridgeError("已经迁移过（%s）；要退回用 ticket bridge adopt --undo" %
                          time.strftime("%Y-%m-%d %H:%M", time.localtime(read_state()["adopted"].get("at", 0))))
    cur = update.current_link()
    if not cur.exists():
        raise BridgeError("还不是受管安装（没有 %s）：先用一行安装或 ticket update adopt" % cur)
    if not (app_vendor(cur) / BRIDGE_ENTRY).is_file():
        raise BridgeError("当前版本 %s 没有打包 bridge：等自动更新到带 bridge 的版本再迁移" % update.current_version())
    sf = bridge_service_file(profile)
    if not sf.is_file():
        raise BridgeError("找不到 bridge 服务 %s（profile %s）；profile 不对就加 --profile" % (sf, profile))
    if is_managed_service(sf):
        raise BridgeError("bridge 服务已经由 Larkyard 管理，不用迁移")
    prog = service_program(sf)
    if not prog or not any("lark-channel-bridge" in a for a in prog):
        raise BridgeError("%s 的启动命令不是 lark-channel-bridge（%s），不认识的启动方式不动" % (sf, " ".join(prog)))
    env = service_env(sf)
    home = Path(env.get("LARK_CHANNEL_HOME") or lc_home())
    out, err = service_logs(sf)
    jobs = find_restart_jobs(exclude=(sf,))
    odd = [j for j in jobs if not j.get("daily")]
    if odd:
        raise BridgeError("这些重启 bridge 的定时任务不是「每天一次」，不知道怎么改：%s。先手工删掉或改成每天一次再迁移"
                          % "、".join(j["path"] for j in odd))
    td = _ticketd_service()
    if td:
        if "launcher.py" not in " ".join(service_program(td[1])):
            raise BridgeError("ticketd 服务 %s 还在直接跑克隆（不是受管安装）：先在终端里执行 ticket update adopt，"
                              "确认没问题后再迁移 bridge" % td[1])
        running = _running_tickets()
        if running:
            raise BridgeError("有 %d 张工单正在运行（%s）：迁移要重启 ticketd，等它们结束或先 ticket hold 再迁移"
                              % (len(running), "、".join(running[:5])))
    return {"profile": profile, "service": str(sf), "label": bridge_label(profile), "home": str(home),
            "stdout": out, "stderr": err, "program": prog, "restart_jobs": jobs,
            "old_path": env.get("PATH", ""), "secrets_getter": str(home / "secrets-getter"),
            "ticketd": {"label": td[0], "path": str(td[1])} if td else None}


def _running_tickets() -> list:
    try:
        from . import db
        con = db.connect()
        try:
            return [r["id"] for r in con.execute("SELECT id FROM tickets WHERE state='running'")]
        finally:
            con.close()
    except Exception:
        return []


def adopt_plan_lines(plan: dict) -> list:
    lines = ["要做的事（改之前全部备份到 %s/adopt-backup/<时间>/）：" % bridge_home(),
             "1. bridge 服务 %s：启动命令 %s → %s %s run --profile %s（服务名、LARK_CHANNEL_HOME、日志位置不变）" % (
                 plan["service"], " ".join(plan["program"][:2]), node_bin(), bridge_entry(), plan["profile"]),
             "2. %s：改用固定目录的 Node 和 bridge" % plan["secrets_getter"]]
    if plan["restart_jobs"]:
        for j in plan["restart_jobs"]:
            lines.append("3. 每天的重启任务 %s（%02d:%02d）：改成 ticket bridge restart --profile %s" % (
                j["label"], j["hour"], j["minute"], plan["profile"]))
    else:
        lines.append("3. 新建每天 04:07 的重启任务 %s（ticket bridge restart --if-stale：版本没变不重启）" % RESTART_LABEL)
    if plan["ticketd"]:
        lines.append("4. ticketd 服务 %s：重装，PATH 换成固定目录（会重启 ticketd）" % plan["ticketd"]["path"])
    lines.append("然后重新加载 bridge 服务：bridge 会重启一次，正在进行的那一轮回复会中断。")
    lines.append("不动：%s 里的 profile、会话、凭据、日志。退回：ticket bridge adopt --undo" % plan["home"])
    return lines


def adopt(profile: str = "", confirm=None, services: Optional[Services] = None, fetch=fetch_verified,
          wait=wait_online, run_install=None) -> list:
    """从分开安装迁移。confirm(计划文字) 返回 True 才动手（终端里读 yes）。"""
    from . import update
    _refuse_in_worker("ticket bridge adopt")
    plan = adopt_plan(profile)
    lines = adopt_plan_lines(plan)
    if confirm is None or not confirm("\n".join(lines)):
        return ["没有改任何东西"]
    services = services or Services()
    run_install = run_install or update._install_sh
    cur = update.current_link().resolve()
    out = ensure_runtime(cur, fetch=fetch)
    link_bridge(cur)
    info = bridge_info(bridge_link()) or {}

    backup = bridge_home() / "adopt-backup" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=False)
    saved = []

    def save(orig: str) -> None:
        p = Path(orig)
        if p.is_file():
            dst = backup / ("%02d-%s" % (len(saved), p.name))
            shutil.copy2(str(p), str(dst))
            saved.append({"orig": str(p), "backup": str(dst)})

    save(plan["service"])
    save(plan["secrets_getter"])
    for j in plan["restart_jobs"]:
        save(j["path"])
        if j["timer"]:
            save(j["timer"])
    if plan["ticketd"]:
        save(plan["ticketd"]["path"])
    st = read_state()
    st.update({"commit": info.get("commit"), "version": info.get("version"), "tree_sha256": info.get("tree_sha256"),
               "switched_at": int(time.time()), "previous": ""})
    st["adopted"] = {"at": int(time.time()), "profile": plan["profile"], "label": plan["label"],
                     "service": plan["service"],
                     "backup": str(backup), "files": saved, "created": [], "done": [],
                     "restart_jobs": plan["restart_jobs"], "ticketd": plan["ticketd"]}
    write_state(st)  # 先记下备份在哪，后面任何一步失败都能 --undo

    def mark(step: str, created=None) -> None:
        st = read_state()
        if step not in st["adopted"]["done"]:
            st["adopted"]["done"].append(step)
        if created:
            st["adopted"]["created"] += [c for c in created if c not in [x["orig"] for x in saved]]
        write_state(st)

    try:
        home = Path(plan["home"])
        job_env = {"LARK_CHANNEL_HOME": str(home)}
        data = render_bridge_service(plan["profile"], service_path(tool_dir("claude"), tool_dir("codex"),
                                                                   plan.get("old_path", "")), home,
                                     Path(plan["stdout"]) if plan["stdout"] else None,
                                     Path(plan["stderr"]) if plan["stderr"] else None)
        mark("bridge-file")
        _write(Path(plan["service"]), data)
        _write(Path(plan["secrets_getter"]), secrets_getter_text(home).encode("utf-8"), 0o700)
        if not any(x["orig"] == plan["secrets_getter"] for x in saved):
            mark("secrets-getter", [plan["secrets_getter"]])
        mark("restart-job")
        if plan["restart_jobs"]:
            for j in plan["restart_jobs"]:
                install_restart_job(["restart", "--profile", plan["profile"]], j["label"], j["hour"], j["minute"],
                                    services, env=job_env)
        else:
            install_restart_job(["restart", "--if-stale", "--profile", plan["profile"]], services=services,
                                env=job_env, on_files=lambda files: mark("restart-job", files))
        if plan["ticketd"]:
            mark("ticketd")
            label, env = update.capture_service()
            run_install(cur, update.app_dir(), {"label": label, "env": env})
        started = time.time()
        mark("bridge-load")
        services.load(plan["label"], Path(plan["service"]))
        if not wait(plan["profile"], started - 1, home=home):
            raise BridgeError("迁移后 bridge %d 秒内没有连上飞书" % HEALTH_WAIT)
    except BaseException as e:
        try:
            back = undo_adopt(services=services)
        except Exception as e2:  # noqa: BLE001
            back = ["自动退回也失败了：%s；备份在 %s，按 ticket bridge adopt --undo 再试" % (e2, backup)]
        raise BridgeError("迁移失败：%s\n已自动退回：\n%s" % (e, "\n".join(back)))
    out.append("已迁移：bridge 由 Larkyard 打包的 %s（%s）运行" % (info.get("version"), str(info.get("commit"))[:12]))
    out.append("备份：%s（退回：ticket bridge adopt --undo）" % backup)
    return out


def undo_adopt(services: Optional[Services] = None) -> list:
    _refuse_in_worker("ticket bridge adopt --undo")
    services = services or Services()
    st = read_state()
    ad = st.get("adopted")
    if not ad:
        raise BridgeError("没有迁移记录，不用退回")
    missing = [f["backup"] for f in ad.get("files", []) if not Path(f["backup"]).is_file()]
    if missing:
        raise BridgeError("备份文件不见了：%s；没有改任何文件" % "、".join(missing))
    out = []
    for f in ad.get("files", []):
        orig = Path(f["orig"])
        orig.parent.mkdir(parents=True, exist_ok=True)
        tmp = orig.with_name(".%s.undo-%d" % (orig.name, os.getpid()))
        shutil.copy2(f["backup"], str(tmp))
        os.replace(str(tmp), str(orig))
        out.append("已恢复 %s" % orig)
    for c in ad.get("created", []):
        p = Path(c)
        if p.suffix in (".plist", ".service", ".timer"):
            label = p.stem
            services.unload(label, timer=p.suffix == ".timer")
        try:
            p.unlink()
            out.append("已删除 %s" % p)
        except FileNotFoundError:
            pass
    # 只重新加载真正改过的服务（老记录没有 done：全部重载）。迁移在前几步失败时，ticketd 和 bridge 都没被动过，不必重启
    done = set(ad.get("done") or ["restart-job", "ticketd", "bridge-load", "bridge-file"])
    if "restart-job" in done:
        for j in ad.get("restart_jobs", []):
            if host.system() == "darwin":
                services.load(j["label"], Path(j["path"]))
            elif j.get("timer"):
                services.load(j["label"], Path(j["timer"]), timer=True)
    td = ad.get("ticketd")
    if td and "ticketd" in done:
        services.load(td["label"], Path(td["path"]))
    if "bridge-load" in done:
        services.load(ad["label"], Path(ad["service"]))
        out.append("已重新加载 bridge 服务（回到迁移前的启动方式）")
    del st["adopted"]
    write_state(st)
    return out

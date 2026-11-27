"""主会话约定（bridge 工作目录的 CLAUDE.md）跟着版本走（docs/spec.md §14「主会话约定自动同步」，T0084）。

以前主会话约定只在装机时由 setup-workspace.sh 渲染一次，之后版本怎么升都不变（只有 adopt / migrate 改 ticket 路径），
模板里新加的规则到不了已经装好的电脑：实测 2026-11-26 两台同事电脑和发布者自己的电脑都停在旧模板，主会话不知道
「生图一律派单」，在自己的会话里跑生图工具、拿不到网关子令牌，就告诉同事「没开通，找管理员」。

守护进程每次启动（升级后由启动器拉起的那一次也算）调 sync()：
- 工作目录的 CLAUDE.md 和本版模板渲染出来的一样：不动。
- 把里面的 `<根目录>/bin/ticket` 的根目录换回 @LARKYARD@ 之后，逐字等于发过的某一版模板（workspace/CLAUDE.md.history
  里的摘要）：说明没人手改过，先备份成 CLAUDE.md.bak-<时间>，再换成本版模板（根目录用受管安装的 current）。
- 其他情况（手改过、找不到工作目录、读不了）：不动，`ticket doctor` 照旧提示由人决定。私人约定写在 CLAUDE.local.md。

Windows（T0109）：安装向导在 Git Bash 里渲染，根目录写成 `C:/Users/…/larkyard`（`pwd -W`）。原来只认 / 开头的根目录，
Windows 的认不出 → 当成手改过 → 自检报 ❌，也不自动换新版。现在盘符开头的也认；Windows 的正式安装就是一份克隆
（还没有受管安装），所以 Windows 上从克隆运行也照常自动同步。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from . import config

PLACEHOLDER = "@LARKYARD@"
# 根目录：/ 开头，或 Windows 的盘符开头（C:/… 或 C:\…；盘符前面不能紧挨着字母数字，免得把「path:/x」认成盘符）
ROOT_RE = re.compile(r"((?:(?<![\w])[A-Za-z]:)?[\\/][^\s`'\"()<>]*?)/bin/ticket(?![\w.-])")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def template_path() -> Path:
    return config.REPO / "workspace" / "CLAUDE.md.tmpl"


def history_path() -> Path:
    return config.REPO / "workspace" / "CLAUDE.md.history"


def bridge_workspace() -> str:
    """bridge 的默认工作目录：先看 TICKETD_WORKSPACE，再尽力读 bridge 的配置（只读；格式是上游的，读不懂就算了）。"""
    ws = config.workspace()
    if ws:
        return os.path.expanduser(ws)
    try:
        home = os.environ.get("LARK_CHANNEL_HOME") or os.path.expanduser("~/.lark-channel")
        cfg = json.loads(Path(home, "config.json").read_text(encoding="utf-8"))
        return cfg["profiles"][cfg["activeProfile"]]["workspaces"]["default"]
    except Exception:
        return ""


def root() -> str:
    """主会话里 ticket 的根目录：受管安装用跟着版本走的 current，否则是本仓库。
    用 / 分隔（as_posix）：Windows 上和安装向导渲染的 C:/Users/… 写法一致；macOS / Linux 上不变。"""
    from . import update
    return (update.current_link() if update.is_managed() else config.REPO).as_posix()


def _windows() -> bool:
    # main 上 host.system() 在 Windows 原样返回 sys.platform（win32）；原生 Windows 分支把它映射成 windows
    from . import host
    return host.system() in ("windows", "win32")


def auto_sync() -> bool:
    """守护进程启动时会不会自动换旧版模板：受管安装会；Windows 的正式安装是克隆（还没有受管安装），也会。
    macOS / Linux 上从克隆、worktree 起的守护进程（开发时）不碰真实的主会话约定。"""
    from . import update
    return update.is_managed() or _windows()


def render(text: str, at: str) -> str:
    return text.replace(PLACEHOLDER, at)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def known() -> set:
    """发过的每一版模板（含本版）的 sha256。"""
    out = {digest(template_path().read_text(encoding="utf-8"))}
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        d = line.split("#", 1)[0].strip()
        if DIGEST_RE.match(d):
            out.add(d)
    return out


def original_root(text: str) -> Optional[str]:
    """text 是某一版发过的模板（没手改过）时，返回渲染它用的根目录；否则 None。
    先试本机已知的根目录（路径里有空格、括号也认得），再试从 `<根>/bin/ticket` 里认出来的。只换一种根目录：
    混着两种写法的，换完摘要对不上，算手改过。"""
    from . import update
    known_roots = []
    for p in (update.current_link(), config.REPO):
        known_roots += [str(p), p.as_posix()]   # Windows：C:\… 和 C:/… 两种写法都试
    cands = known_roots + sorted(set(ROOT_RE.findall(text)))
    ok = known()
    for r in dict.fromkeys(cands):
        if r and (r + "/bin/ticket") in text and digest(text.replace(r, PLACEHOLDER)) in ok:
            return r
    return None


def target_root(orig: str) -> str:
    """换模板时用的根目录：文件里原来的根目录还在（有 bin/ticket）就沿用，不在了才用 root()。
    这样换的只是内容，主会话用的 ticket 不会被换成别的安装（审查意见：别的守护进程启动时不该改掉路径）。"""
    return orig if Path(orig, "bin", "ticket").exists() else root()


def state(ws: str = "") -> Tuple[str, Path]:
    """("same" | "old" | "edited" | "missing" | "nows", CLAUDE.md 路径)。old = 没手改过的旧版模板，可以自动换。"""
    ws = ws or bridge_workspace()
    if not ws:
        return "nows", Path()
    live = Path(ws) / "CLAUDE.md"
    try:
        have = live.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", live
    tmpl = template_path().read_text(encoding="utf-8")
    if have == render(tmpl, root()):
        return "same", live
    orig = original_root(have)
    if orig is None:
        return "edited", live
    if have == render(tmpl, target_root(orig)):
        return "same", live
    if live.is_symlink():   # 符号链接指到别处：不换成普通文件，doctor 也按手改过提示
        return "edited", live
    return "old", live


def sync(ws: str = "", now: Optional[float] = None) -> Optional[str]:
    """守护进程启动时调用：没手改过的旧版主会话约定换成本版，返回备份路径；没换返回 None。读写出错照样抛出。
    只在 auto_sync() 为真时做（开发时从克隆、worktree 起的守护进程不碰真实的主会话约定）。"""
    if not auto_sync():
        return None
    st, live = state(ws)
    if st != "old":
        return None
    old = live.read_bytes()
    try:
        orig = original_root(old.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    st_old = os.stat(str(live))
    if orig is None or st_old.st_nlink > 1:   # 硬链接：os.replace 会断开它，不动
        return None
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(now))
    n = 0
    while True:   # 同一秒里换过一次（测试、连续重启）：不覆盖已有的备份
        bak = live.with_name("CLAUDE.md.bak-%s%s" % (stamp, "-%d" % n if n else ""))
        try:
            fd = os.open(str(bak), os.O_WRONLY | os.O_CREAT | os.O_EXCL, st_old.st_mode & 0o777)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "wb") as f:
        f.write(old)
    want = render(template_path().read_text(encoding="utf-8"), target_root(orig))
    tmp = live.with_name(".CLAUDE.md.tmp-%d" % os.getpid())
    done = False
    try:
        tmp.write_text(want, encoding="utf-8")
        os.chmod(str(tmp), st_old.st_mode & 0o777)
        if live.read_bytes() == old:   # 备份之后有人改了：不覆盖
            os.replace(str(tmp), str(live))
            done = True
    finally:
        if tmp.exists():
            tmp.unlink()
        if not done:
            bak.unlink()
    return str(bak) if done else None

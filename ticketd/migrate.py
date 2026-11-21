"""ticket migrate：把已有的、就地运行的安装（早期的私有版本、从 clone 装的 Larkyard）一条命令转成受管安装 +
Larkyard 管理的打包 bridge。见 openspec migrate-existing-install 和 docs/spec.md §14。

前提：受管安装的版本目录已经装好（一行安装 install.sh --upgrade-existing 会先装好再调用这里；也可以先
install.sh --no-setup 再单独执行 ticket migrate）。

做的事（改前全部备份到 ~/.ticketd/migrate/backup/<时间>/，--undo 按备份退回）：
1. 认出正在跑的 ticketd 服务（任何服务名，包括早期的自定义服务名），停掉它；
2. 拷一份数据库快照（库结构由新版本自动迁移，只增不删，旧代码仍能读写）；
3. 旧代码里实际生效、而新版本默认值不同的配置（写死在代码里的主用户、工单根目录，服务环境里的 TICKETD_*）写进 config.env；
4. 服务改为经启动器运行（服务名统一成 dev.larkyard.ticketd，环境沿用原来的），主会话约定里的 ticket 路径改到 current；
5. bridge 是分开安装的（Homebrew / npm）就接着 ticket bridge adopt（会重启 bridge）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import bridge, config, updsig, update

# 旧代码里要对照的配置：(config.env 的键, config 模块里的函数)。这两项在早期版本里写死在代码中，新版本默认值不同
COMPARED = (("TICKETD_OWNER", "owner"), ("TICKETD_WORKROOT", "workroot"))
SKIP_ENV = set(update.SERVICE_KEYS) | {"TICKETD_APP", "TICKETD_LABEL"}
DAEMON_WAIT = 90.0
ACTIVE = ("applying", "ticketd-done", "done")  # 迁移记录的这些状态算「迁移过」；rolled-back（自动退回了）、undone 可以重来
STOP_WAIT = 90.0


class MigrateError(Exception):
    pass


def root_dir() -> Path:
    return config.home() / "migrate"


def record_path() -> Path:
    return root_dir() / "state.json"


def read_record() -> dict:
    try:
        d = json.loads(record_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(rec: dict) -> None:
    root_dir().mkdir(parents=True, exist_ok=True)
    tmp = record_path().with_name("state.json.tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(record_path()))


def _refuse(what: str, restarts: bool = True) -> None:
    if os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"):
        raise MigrateError("worker 里不能执行 %s（要由本人在终端里执行）" % what)
    if restarts and os.environ.get("LARK_CHANNEL") == "1":
        raise MigrateError("%s 会重启 ticketd 和 bridge：看起来是在 bridge 驱动的会话里执行（LARK_CHANNEL=1），"
                           "会把这个会话自己打断。请在你自己的终端里执行（--dry-run 可以在哪儿都跑）" % what)


def _mask(v: str) -> str:
    return "…" + v[-6:] if v.startswith("ou_") else v


# ---------- 认出现有安装 ----------
def _service_dir() -> tuple:
    sample = update.service_file("x")
    return sample.parent, sample.suffix


def _ticketd_arg(prog: list) -> str:
    """启动命令里指向 ticketd 的那一项：克隆的 <目录>/bin/ticketd，或受管安装的 launcher.py。"""
    for a in prog:
        if any(c.isspace() for c in a) and not os.path.isfile(os.path.expanduser(a)):
            continue  # sh -c "…" 这类包装不认；路径本身带空格（存在的文件）照认
        if a.endswith("/bin/ticketd"):
            return a
        if a.endswith("/launcher.py") and os.path.normpath(os.path.expanduser(a)) == os.path.normpath(
                str(update.app_dir() / "launcher.py")):
            return a
    return ""


def _label_of(p: Path) -> str:
    """plist 里的 Label（文件名可能和它不一样；bootout 要用真正的 Label）。systemd 用单元名。"""
    if p.suffix == ".plist":
        try:
            import plistlib
            label = plistlib.loads(p.read_bytes()).get("Label")
            if isinstance(label, str) and label:
                return label
        except Exception:
            pass
    return p.stem


def find_ticketd_services() -> list:
    """服务目录里所有跑 ticketd、数据目录是本机 TICKETD_HOME 的服务：[{label, path, program, env}]。"""
    d, suffix = _service_dir()
    found = []
    if not d.is_dir():
        return found
    for p in sorted(d.glob("*" + suffix)):
        prog = bridge.service_program(p)
        if not _ticketd_arg(prog):
            continue
        env = bridge.service_env(p)
        home = Path(env.get("TICKETD_HOME") or str(Path.home() / ".ticketd")).expanduser()
        try:
            if home.resolve() != config.home().resolve():
                continue
        except OSError:
            continue
        found.append({"label": _label_of(p), "path": str(p), "program": prog, "env": env})
    return found


def old_effective(repo: Path, env: dict, python: str = "") -> dict:
    """用旧代码自己的 config 模块、按服务的环境算出它实际在用的值（不混进当前 shell 的环境）。读不出来返回空。"""
    code = ("import sys, json\nsys.path.insert(0, sys.argv[1])\nfrom ticketd import config as c\nout = {}\n"
            "for k, f in json.loads(sys.argv[2]):\n"
            "    try:\n        out[k] = str(getattr(c, f)())\n    except Exception:\n        pass\n"
            "print(json.dumps(out))\n")
    e = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
    e.update({k: str(v) for k, v in env.items() if k not in ("PATH",)})
    py = python or env.get("TICKETD_PYTHON") or sys.executable
    try:
        p = subprocess.run([py, "-I", "-B", "-c", code, str(repo), json.dumps(COMPARED)], env=e, capture_output=True,
                           text=True, timeout=30, cwd=str(config.home()) if config.home().is_dir() else None)
        if p.returncode != 0:
            return {}
        d = json.loads(p.stdout.strip().splitlines()[-1])
        return {k: v for k, v in d.items() if isinstance(v, str)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}


CODE_STATUS_MAX = 40


def code_info(repo: Path) -> tuple:
    """只读：旧代码目录的版本、最后一次提交和本地改动（git log -1、git status -s），给 --dry-run 看。
    返回 (行列表, 有没有本地改动)。不用 git 也不报错：没装 git（macOS 没装命令行工具时 /usr/bin/git 只是个会弹窗的
    占位程序）或不是 git 目录就只给版本号。GIT_OPTIONAL_LOCKS=0：git status 不回写索引。"""
    lines, dirty = [], False
    try:
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""",
                      (repo / "ticketd" / "__init__.py").read_text(encoding="utf-8", errors="replace"))
        lines.append("版本：%s" % (m.group(1) if m else "代码里没写版本号（早期版本）"))
    except OSError:
        pass
    g = shutil.which("git")
    if not g or not (repo / ".git").exists():
        return lines, dirty
    if sys.platform == "darwin" and os.path.realpath(g) == "/usr/bin/git":
        try:
            if subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=10).returncode != 0:
                return lines, dirty
        except (OSError, subprocess.SubprocessError):
            return lines, dirty
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C")

    def git(*a) -> Optional[str]:
        try:
            r = subprocess.run([g, "-C", str(repo)] + list(a), env=env, capture_output=True, timeout=20,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.decode("utf-8", "replace").rstrip("\n") if r.returncode == 0 else None

    last = git("log", "-1", "--format=%h %ad %s", "--date=short")
    if last:
        lines.append("最后一次提交（git log -1）：%s" % last)
    st = git("status", "-s", "-b")
    if st is not None:
        rows = st.splitlines()
        head = rows.pop(0)[3:] if rows and rows[0].startswith("## ") else ""
        if head:
            lines.append("分支：%s" % head)
        dirty = bool(rows)
        if rows:
            lines.append("本地改动（git status -s，%d 处）：" % len(rows))
            lines += ["    %s" % r for r in rows[:CODE_STATUS_MAX]]
            if len(rows) > CODE_STATUS_MAX:
                lines.append("    …还有 %d 处" % (len(rows) - CODE_STATUS_MAX))
        else:
            lines.append("本地改动（git status -s）：没有")
    return lines, dirty


def _new_default(key: str) -> str:
    return {"TICKETD_WORKROOT": str(Path.home() / "larkyard-tickets")}.get(key, "")


def _same(key: str, a: str, b: str) -> bool:
    if key == "TICKETD_WORKROOT":
        return os.path.normpath(os.path.expanduser(a)) == os.path.normpath(os.path.expanduser(b))
    return a.strip() == b.strip()


def config_changes(repo: Optional[Path], env: dict) -> tuple:
    """要写进 config.env 的值：[(键, 新值, 现在 config.env 里的值或 None, 原因)]，以及提醒。"""
    changes, notes = [], []
    if repo is not None:
        old = old_effective(repo, env)
        if not old:
            notes.append("⚠️ 读不出旧代码 %s 在用的配置（主用户、工单根目录）：迁移后用 ticket doctor 核对" % repo)
        for key, _ in COMPARED:
            if key not in old:
                continue
            have = config.file_value(key)
            now = have if have not in (None, "") else _new_default(key)
            if _same(key, old[key], now) or (key == "TICKETD_OWNER" and not old[key].strip()):
                continue
            why = "旧代码在用 %s，新版本会用 %s" % (_mask(old[key]) or "（空）", _mask(now) or "（空）")
            changes.append((key, old[key], have, why))
    for k, v in sorted(env.items()):
        if k.startswith("TICKETD_") and k not in SKIP_ENV and k not in [c[0] for c in changes] and \
                config.file_value(k) in (None, ""):
            changes.append((k, str(v), None, "原来写在服务环境里，新服务只读 config.env"))
    return changes, notes


def _running_tickets() -> Optional[list]:
    """只读打开数据库（不建表、不迁移：旧守护进程可能还在跑）。读不了返回 None（当作挡路）。"""
    p = config.db_path()
    if not p.is_file():
        return []
    try:
        con = sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            return [r[0] for r in con.execute("SELECT id FROM tickets WHERE state='running'")]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _busy_reason() -> str:
    running = _running_tickets()
    if running is None:
        return "读不了工单数据库（%s），确认不了有没有正在运行的工单" % config.db_path()
    if running:
        return "有 %d 张工单正在运行（%s）：要重启 ticketd，等它们结束（或 ticket hold）再执行" % (
            len(running), "、".join(running[:5]))
    return ""


def _workspace(profile: str) -> str:
    ws = config.workspace()
    if ws:
        return os.path.expanduser(ws)
    try:
        d = json.loads((bridge.lc_home() / "config.json").read_text(encoding="utf-8"))
        return str(d["profiles"][profile]["workspaces"]["default"])
    except Exception:
        return ""


def _ticket_root_in(md: Path, roots: list) -> str:
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return ""
    for r in roots:
        if r and re.search(re.escape("%s/bin/ticket" % r) + r"(?![\w.-])", text):
            return r
    return ""


def bridge_kind(prog: list) -> str:
    head = " ".join(prog[:2])
    if any(h in head for h in ("/opt/homebrew/", "/Cellar/", "/home/linuxbrew/", "/usr/local/bin/lark-channel-bridge")):  # check-private: ignore（Linuxbrew 的标准前缀）
        return "homebrew"
    if bridge.pinned_to_user_node(prog[:2]) or "/lib/node_modules/" in head:
        return "npm"
    return "other"


KIND_TEXT = {"homebrew": "Homebrew 装的", "npm": "npm 全局装的", "other": "分开安装的"}


def plan(profile: str = "", target: str = "") -> dict:
    """只读：认出现有安装、算出要做什么。blockers 不为空时不能动手。"""
    _refuse("ticket migrate", restarts=False)
    p = {"blockers": [], "warnings": [], "notes": [], "ticketd": None, "bridge": None, "config": [],
         "workspace_md": "", "workspace_root": "", "others": []}
    rec = read_record()
    if rec.get("status") in ACTIVE:
        p["blockers"].append("已经迁移过（%s，备份 %s）；要退回用 ticket migrate --undo" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("at", 0))), rec.get("backup", "")))
        return p
    cur = update.current_link()
    cur_v = update.current_version() if cur.is_symlink() else ""
    target = target or cur_v
    p["target"] = target
    svcs = find_ticketd_services()
    label_new = os.environ.get("TICKETD_LABEL") or update.DEFAULT_LABEL
    if not svcs:
        p["blockers"].append("没找到跑 ticketd 的服务（%s 下）：新机器用一行安装（不带 --upgrade-existing）"
                             % _service_dir()[0])
    elif len(svcs) > 1:
        p["blockers"].append("找到 %d 个跑 ticketd 的服务，会抢同一个数据库锁：%s。先停掉并移走不用的那个"
                             % (len(svcs), "、".join(s["path"] for s in svcs)))
    svc = svcs[0] if len(svcs) == 1 else None
    env = svc["env"] if svc else {}
    profile = profile or env.get("LARK_CHANNEL_PROFILE") or bridge.profile_name()
    try:
        bridge.service_id(profile)
    except bridge.BridgeError as e:
        p["blockers"].append(str(e))
        return p
    p["profile"] = profile
    repo = None
    if svc:
        arg = _ticketd_arg(svc["program"])
        if arg.endswith("/launcher.py"):
            if cur_v and target and updsig.cmp_version(cur_v, target) < 0:
                p["blockers"].append(
                    "ticketd 已是受管安装 %s：升级到 %s 由守护进程按试用期流程完成（config.env 的 TICKETD_UPDATE_URL "
                    "指向发布目录后执行 ticket update check）；装好后再执行 ticket migrate 迁移 bridge" % (cur_v, target))
            p["notes"].append("ticketd 服务 %s：已经经启动器运行（受管安装 %s），不用改" % (svc["label"], cur_v or "?"))
        else:
            if cur_v and target and updsig.cmp_version(cur_v, target) < 0:
                # bootstrap 不会切换已有的 current（交给守护进程），可这台的守护进程跑的是克隆、不会去更新它
                p["blockers"].append("%s 指向旧的版本目录 %s，但服务还在直接跑克隆：确认服务没在用它后删掉这个链接"
                                     "（rm %s），再执行" % (cur, cur_v, cur))
            arg_path = Path(arg)
            repo_logical = arg_path.parent.parent
            repo = repo_logical.resolve() if repo_logical.exists() else repo_logical
            if not (repo / "ticketd").is_dir():
                p["warnings"].append("⚠️ 服务指向的代码目录 %s 不在了：读不出旧配置，迁移后用 ticket doctor 核对" % repo)
                repo_ok = None
            else:
                repo_ok = repo
            code, dirty = code_info(repo) if repo_ok else ([], False)
            if dirty:
                p["warnings"].append("⚠️ 旧代码目录有本地改动（上面列的）：迁移后跑的是 %s 的发布版，不含这些改动；"
                                     "旧目录原样保留，要的改动之后另外处理" % (target or "新"))
            if label_new != svc["label"] and update.service_file(label_new).exists():
                p["blockers"].append("%s 已经存在（另一个服务文件）：先确认它是什么" % update.service_file(label_new))
            svc_env = update.read_service_env(Path(svc["path"]))
            svc_env["TICKETD_HOME"] = str(config.home())  # 服务里可能写的是 ~/…：install.sh 要绝对路径
            if "LARK_CHANNEL_PROFILE" not in svc_env and env.get("LARK_CHANNEL_PROFILE") is None:
                svc_env["LARK_CHANNEL_PROFILE"] = profile
            changes, notes = config_changes(repo_ok, env)
            p["config"] = changes
            p["warnings"] += notes
            p["ticketd"] = {"label_old": svc["label"], "service_old": svc["path"], "program": svc["program"],
                            "repo": str(repo), "repo_logical": str(repo_logical), "label_new": label_new,
                            "service_new": str(update.service_file(label_new)), "env": svc_env, "code": code}
            busy = _busy_reason()
            if busy:
                p["blockers"].append(busy)
            ws = _workspace(profile)
            if ws:
                md = Path(ws) / "CLAUDE.md"
                roots = list(dict.fromkeys([str(repo_logical), str(repo)] + update._aliases(md, repo)))
                root = _ticket_root_in(md, roots)
                if root:
                    p["workspace_md"], p["workspace_root"] = str(md), root
                else:
                    p["notes"].append("主会话约定 %s 里没有 %s/bin/ticket，不改" % (md, repo))
            # 还引用旧代码目录的其他服务（比如自己加的定时任务）：不动，提醒
            d, suffix = _service_dir()
            for f in sorted(d.glob("*" + suffix)) if d.is_dir() else []:
                if str(f) == svc["path"]:
                    continue
                try:
                    if str(repo_logical) in f.read_text(encoding="utf-8", errors="replace"):
                        p["others"].append(str(f))
                except OSError:
                    pass
    # bridge
    sf = bridge.bridge_service_file(profile)
    if not sf.is_file():
        p["notes"].append("bridge 服务 %s：没找到（profile %s），不迁移 bridge" % (sf, profile))
    elif bridge.is_managed_service(sf):
        p["notes"].append("bridge 服务 %s：已经由 Larkyard 管理" % bridge.bridge_label(profile))
    else:
        prog = bridge.service_program(sf)
        b = {"service": str(sf), "label": bridge.bridge_label(profile), "program": prog, "kind": bridge_kind(prog),
             "jobs": [], "skip": ""}
        if not any("lark-channel-bridge" in a for a in prog):
            b["skip"] = "启动命令不是 lark-channel-bridge（%s）" % " ".join(prog)
        elif cur_v and cur_v == target and not (bridge.app_vendor(cur) / bridge.BRIDGE_ENTRY).is_file():
            b["skip"] = "当前版本 %s 没有打包 bridge" % cur_v
        else:
            jobs = bridge.find_restart_jobs(exclude=(sf,))
            b["jobs"] = jobs
            odd = [j["path"] for j in jobs if not j.get("daily")]
            if odd:
                b["skip"] = "这些重启 bridge 的定时任务不是「每天一次」：%s" % "、".join(odd)
        p["bridge"] = b
    owner = (config.file_value("TICKETD_OWNER") or "").strip()
    if p["ticketd"] is not None and not owner and not any(c[0] == "TICKETD_OWNER" and c[1].strip() for c in p["config"]):
        p["blockers"].append("定不出主用户（旧代码里读不出，config.env 里也没有 TICKETD_OWNER）：迁移后新服务会拒绝所有派单。"
                             "先在 %s 里写一行 TICKETD_OWNER=<你在这个飞书应用下的 open_id>，再执行" % config.config_file())
    if p["ticketd"] is None and (p["bridge"] is None or p["bridge"]["skip"]) and not p["blockers"]:
        p["nothing"] = True
    return p


def plan_lines(p: dict) -> list:
    out = []
    if p.get("blockers"):
        out.append("不能迁移：")
        out += ["  ✗ %s" % b for b in p["blockers"]]
    td = p.get("ticketd")
    step = 0
    if td:
        out.append("ticketd：服务 %s 在直接跑 %s（不是受管安装）" % (td["label_old"], td["repo_logical"]))
        out += ["   %s" % line for line in td.get("code", [])]
        step += 1
        out.append("%d. 停掉服务 %s，拷一份数据库快照（新版本启动时自动加列加表，只增不删，旧代码仍能读）" % (step, td["label_old"]))
        for key, val, have, why in p["config"]:
            step += 1
            shown = _mask(val) if key == "TICKETD_OWNER" else val
            out.append("%d. config.env：%s=%s（%s%s）" % (step, key, shown, why,
                                                      "；原来是 %s" % (_mask(have) or "空") if have else ""))
        step += 1
        if td["label_new"] != td["label_old"]:
            out.append("%d. 服务改名 %s → %s，经启动器运行 ~/.ticketd/app/current/bin/ticketd（%s 移到备份目录）" % (
                step, td["label_old"], td["label_new"], td["service_old"]))
        else:
            out.append("%d. 服务 %s 改为经启动器运行 ~/.ticketd/app/current/bin/ticketd" % (step, td["label_new"]))
        if p.get("workspace_md"):
            step += 1
            out.append("%d. 主会话约定 %s：%s/bin/ticket → ~/.ticketd/app/current/bin/ticket" % (
                step, p["workspace_md"], p["workspace_root"]))
        out.append("   旧代码目录 %s 不动（退回要用）" % td["repo_logical"])
    b = p.get("bridge")
    if b:
        if b["skip"]:
            out.append("bridge：%s %s，不迁移：%s（之后可以单独 ticket bridge adopt）" % (KIND_TEXT[b["kind"]], b["label"],
                                                                        b["skip"]))
        else:
            step += 1
            out.append("%d. bridge：%s（%s）→ Larkyard 打包的 bridge（= ticket bridge adopt：服务名、profile、会话、凭据、"
                       "日志位置不变；改 secrets-getter 和每天的重启任务）" % (step, KIND_TEXT[b["kind"]],
                                                              " ".join(b["program"][:2])))
            out.append("   ⚠️ 这一步会重启 bridge：正在进行的那一轮回复会中断。旧的 bridge 安装不卸载（退回要用）")
    for line in p.get("notes", []):
        out.append("· %s" % line)
    for f in p.get("others", []):
        out.append("⚠️ %s 也引用了旧代码目录：不动，确认它还要不要" % f)
    out += p.get("warnings", [])
    if p.get("nothing"):
        out.append("没有要迁移的：ticketd 已是受管安装，bridge 已由 Larkyard 管理（或没有）")
    elif not p.get("blockers"):
        out.append("备份：~/.ticketd/migrate/backup/<时间>/；退回：~/.ticketd/app/current/bin/ticket migrate --undo")
    return out


# ---------- config.env ----------
def _config_set(key: str, value: Optional[str]) -> None:
    """设或删 config.env 里的一个键（value 为 None 时删掉）。只动这一行，其他行原样保留。"""
    p = config.config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    pat = re.compile(r"^[ \t]*(?:export[ \t]+)?%s[ \t]*=.*\n?" % re.escape(key), re.M)
    if value is None:
        text = pat.sub("", text)
    elif pat.search(text):
        m = pat.search(text)  # 换掉第一处，后面重复的删掉（config.py 按最后一处为准）
        text = text[:m.start()] + "%s=%s\n" % (key, value) + pat.sub("", text[m.end():])
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + "%s=%s\n" % (key, value)
    tmp = p.with_name("config.env.tmp-%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(p))


# ---------- 动手 ----------
def _lock_free(timeout: float = STOP_WAIT, sleep=time.sleep) -> bool:
    """旧守护进程退出了没有：拿得到 daemon.lock 就是退出了（拿到马上放掉）。"""
    import fcntl
    lock = config.db_dir() / "daemon.lock"
    if not lock.exists():
        return True
    deadline = time.time() + timeout
    while True:
        try:
            with open(str(lock), "a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            sleep(1)


def wait_daemon(since: float, timeout: float = DAEMON_WAIT, sleep=time.sleep) -> bool:
    """新守护进程起来了没有：心跳文件在 since 之后更新过。"""
    deadline = time.time() + timeout
    while True:
        try:
            if config.heartbeat_path().stat().st_mtime >= since:
                return True
        except OSError:
            pass
        if time.time() >= deadline:
            return False
        sleep(2)


def _backup_db(dest: Path) -> bool:
    src = config.db_path()
    if not src.is_file():
        return False
    a = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)  # as_uri：路径里有 # ? % 空格也对
    try:
        b = sqlite3.connect(str(dest))
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()
    return True


def apply(p: dict, update_url: str = "", channel: str = "", installed_now: bool = False,
          services: Optional[bridge.Services] = None, run_install=None, wait=wait_daemon, lock_free=_lock_free,
          bridge_adopt=None) -> list:
    """按 plan 动手。ticketd 这部分任何一步失败都按备份退回；bridge 那一步失败只退回 bridge（ticketd 已升级好，照常可用）。"""
    _refuse("ticket migrate")
    if p.get("blockers"):
        raise MigrateError("\n".join(p["blockers"]))
    cur = update.current_link()
    if not cur.is_symlink() or not (cur / "bin" / "ticketd").is_file():
        raise MigrateError("还没有受管安装的版本目录（%s）：先用 install.sh --upgrade-existing（或 --no-setup）装好" % cur)
    services = services or bridge.Services()
    run_install = run_install or update._install_sh
    bridge_adopt = bridge_adopt or bridge.adopt
    out = []
    from . import bootstrap
    td = p.get("ticketd")
    if td:
        busy = _busy_reason()  # 确认可能停了很久：停服务前再看一次
        if busy:
            raise MigrateError(busy)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = root_dir() / "backup" / ts
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(td["service_old"], str(backup / Path(td["service_old"]).name))
        if config.config_file().is_file():
            shutil.copy2(str(config.config_file()), str(backup / "config.env"))
        if p.get("workspace_md"):
            shutil.copy2(p["workspace_md"], str(backup / "CLAUDE.md"))
        rec = {"status": "applying", "at": time.time(), "backup": str(backup), "installed_now": installed_now,
               "label_old": td["label_old"], "service_old": td["service_old"],
               "service_old_backup": str(backup / Path(td["service_old"]).name),
               "label_new": td["label_new"], "service_new": td["service_new"], "repo": td["repo_logical"],
               "env": td["env"], "config": {}, "workspace_md": p.get("workspace_md", ""),
               "workspace_root": p.get("workspace_root", ""), "done": [], "bridge": False,
               "config_existed": config.config_file().is_file(),
               "profile": p.get("profile", "")}
        write_record(rec)

        def mark(step: str) -> None:
            rec["done"].append(step)
            write_record(rec)

        try:
            mark("stop-old")
            services.unload(td["label_old"])
            if not lock_free():
                raise MigrateError("旧服务 %s 停了 %d 秒还没退出（daemon.lock 还被占着）" % (td["label_old"], STOP_WAIT))
            if _backup_db(backup / "ticketd.db"):
                out.append("数据库快照：%s" % (backup / "ticketd.db"))
            mark("config")
            sets = [(k, v) for k, v, _, _ in p["config"]]
            if update_url and config.file_value("TICKETD_UPDATE_URL") in (None, ""):
                sets.append(("TICKETD_UPDATE_URL", update_url))
            if channel and channel != "stable" and config.file_value("TICKETD_UPDATE_CHANNEL") in (None, ""):
                sets.append(("TICKETD_UPDATE_CHANNEL", channel))
            for k, v in sets:
                rec["config"][k] = config.file_value(k)
                write_record(rec)
                _config_set(k, v)
                out.append("config.env：%s=%s" % (k, _mask(v) if k == "TICKETD_OWNER" else v))
            out += bootstrap.finish_db()  # 一行安装推迟的「信任公钥、记下清单」：旧守护进程停了、快照拍完才动库
            mark("service")
            if td["label_new"] != td["label_old"]:
                Path(td["service_old"]).unlink()
            started = time.time()
            run_install(cur.resolve(), update.app_dir(), {"label": td["label_new"], "env": td["env"]})
            with update.state_lock():
                st = update.read_state()
                st.update({"label": td["label_new"], "service_env": td["env"], "migrated_from": td["repo_logical"],
                           "migrated_at": time.time()})
                if not st.get("version"):
                    st.update({"status": "confirmed", "version": update.current_version()})
                update.write_state(st)
            out.append("ticketd 服务 %s：经启动器运行 %s/bin/ticketd" % (td["label_new"], cur))
            if p.get("workspace_md"):
                mark("workspace")
                bak = update._rewrite(Path(p["workspace_md"]), "%s/bin/ticket" % p["workspace_root"],
                                      "%s/bin/ticket" % cur)
                if bak:
                    out.append("主会话约定：ticket 路径改为 %s/bin/ticket（备份 %s）" % (cur, bak))
            if not wait(started - 1):
                raise MigrateError("新的 ticketd %d 秒内没有心跳（看 %s）" % (DAEMON_WAIT, config.logs_dir()))
            out.append("ticketd 已用 %s 启动" % update.current_version())
        except BaseException as e:
            try:
                back = _undo_ticketd(rec, services)
            except Exception as e2:  # noqa: BLE001
                back = ["自动退回也失败了：%s；备份在 %s，按 ticket migrate --undo 再试" % (e2, backup)]
            rec["status"] = "rolled-back"
            write_record(rec)
            raise MigrateError("迁移失败：%s\n已自动退回（旧服务照原样在跑；修好原因后可以直接重新执行升级）：\n%s"
                               % (e, "\n".join(back)))
        rec["status"] = "ticketd-done"
        write_record(rec)
        out.append("备份：%s" % backup)
    b = p.get("bridge")
    if b and not b["skip"]:
        # ticketd 本来就是受管安装时只迁移 bridge：新记录（不带上一次已退回的 ticketd 记录）
        rec = read_record() if td else {"status": "applying", "at": time.time(), "backup": "", "done": [],
                                        "installed_now": False, "config": {}, "bridge": False}
        try:
            if not td:
                out += bootstrap.finish_db()
            out += bridge_adopt(p.get("profile", ""), confirm=lambda text: True, services=services,
                                run_install=run_install)
            rec["bridge"] = True
        except (Exception, KeyboardInterrupt) as e:  # bridge.adopt 失败时自己退回；它动手之前被打断就什么都没改
            out.append("❌ bridge 迁移没成（原来的 bridge 照常在跑）：%s" % (e or type(e).__name__))
            if td:
                out.append("   ticketd 已升级好，照常可用；之后可以单独执行 ticket bridge adopt，或 ticket migrate --undo 全部退回")
        rec["status"] = "done"
        write_record(rec)
    elif td:
        rec = read_record()
        rec["status"] = "done"
        write_record(rec)
    out.append("退回：%s/bin/ticket migrate --undo" % cur)
    return out


def _undo_ticketd(rec: dict, services: bridge.Services) -> list:
    out = []
    done = set(rec.get("done") or [])
    cur = update.current_link()
    if "service" in done:
        if rec["label_new"] != rec["label_old"]:
            services.unload(rec["label_new"])
            try:
                Path(rec["service_new"]).unlink()
                out.append("已删除 %s" % rec["service_new"])
            except FileNotFoundError:
                pass
    if "workspace" in done and rec.get("workspace_md"):
        if update._rewrite(Path(rec["workspace_md"]), "%s/bin/ticket" % cur, "%s/bin/ticket" % rec["workspace_root"]):
            out.append("主会话约定里的 ticket 路径已改回 %s/bin/ticket" % rec["workspace_root"])
    for k, prev in (rec.get("config") or {}).items():
        _config_set(k, prev)
        out.append("config.env：%s 已%s" % (k, "删掉" if prev is None else "改回原值"))
    cf = config.config_file()
    if rec.get("config") and rec.get("config_existed") is False and cf.is_file() and not cf.read_text(encoding="utf-8").strip():
        cf.unlink()  # 迁移前没有 config.env：退回后也不留一个空文件
    if "stop-old" in done:
        old = Path(rec["service_old"])
        old.parent.mkdir(parents=True, exist_ok=True)
        tmp = old.with_name(".%s.undo-%d" % (old.name, os.getpid()))
        shutil.copy2(rec["service_old_backup"], str(tmp))
        os.replace(str(tmp), str(old))
        services.load(rec["label_old"], old)
        out.append("已恢复并重新加载 %s（直接跑 %s）" % (rec["label_old"], rec.get("repo")))
    if rec.get("installed_now"):
        _drop_fresh_install()
        out.append("已撤掉受管安装的 current（版本目录留在 %s）" % update.versions_dir())
    return out


def undo(services: Optional[bridge.Services] = None) -> list:
    _refuse("ticket migrate --undo")
    rec = read_record()
    if not rec or rec.get("status") not in ACTIVE:
        raise MigrateError("没有迁移记录，不用退回")
    if rec.get("service_old_backup") and not Path(rec["service_old_backup"]).is_file():
        raise MigrateError("备份文件不见了：%s；没有改任何文件" % rec["service_old_backup"])
    busy = _busy_reason()
    if busy:
        raise MigrateError("%s；没有改任何文件" % busy)
    if rec.get("service_old") and not (Path(rec.get("repo") or "/nonexistent") / "bin" / "ticketd").is_file():
        raise MigrateError("原来的代码目录 %s 不在了（退回后旧服务要跑它）：先把它放回原处再退回；没有改任何文件"
                           % rec.get("repo"))
    services = services or bridge.Services()
    out = []
    if rec.get("bridge") and bridge.read_state().get("adopted"):
        try:
            out += bridge.undo_adopt(services=services)
        except bridge.BridgeError as e:
            raise MigrateError("退回 bridge 失败：%s；没有动 ticketd" % e)
    if rec.get("service_old"):
        out += _undo_ticketd(rec, services)
        out.append("数据库没有改回（新版本只加列加表，旧代码照常读写）；迁移前的快照在 %s" % rec.get("backup"))
    rec["status"] = "undone"
    rec["undone_at"] = time.time()
    write_record(rec)
    return out


# ---------- 命令 ----------
def _confirm(text: str) -> bool:
    from . import setup
    t = setup.Term()
    if not t.interactive:
        raise MigrateError("要在终端里确认：不能在管道或脚本里执行（看计划用 --dry-run）")
    t.say(text)
    return t.ask("输入 yes 开始：") == "yes"


def run(a) -> str:
    """ticket migrate [--dry-run | --undo]（cli.py 在打开数据库之前调用：--dry-run 不能改库）。"""
    if a.undo:
        return "\n".join(undo())
    p = plan(a.profile, getattr(a, "target", "") or "")
    lines = plan_lines(p)
    if a.dry_run or getattr(a, "check", False):
        if p.get("blockers"):
            raise MigrateError("\n".join(lines))
        return "\n".join(lines + ["（--dry-run：没有改任何东西）"])
    try:
        if p.get("blockers"):
            raise MigrateError("\n".join(lines))
        if p.get("nothing"):
            return "\n".join(lines)
        if not a.yes and not _confirm("\n".join(lines)):
            raise MigrateError("没有改任何东西")
        return "\n".join(apply(p, update_url=a.update_url or "", channel=a.channel or "",
                               installed_now=a.installed_now))
    except BaseException:
        if a.installed_now and read_record().get("status") not in ACTIVE:
            _drop_fresh_install()  # 这次 install.sh 才装的受管安装：迁移没开始或已退回，撤掉，机器回到原样
        raise


def _drop_fresh_install() -> None:
    from . import bootstrap
    for f in (update.current_link(), update.app_dir() / "launcher.py", bootstrap.pending_db_path()):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    update.write_state({"status": "unmigrated", "at": time.time()})


def add_args(ap) -> None:
    ap.add_argument("--dry-run", action="store_true", help="只列出要做的事，不改任何东西（在哪儿都能跑）")
    ap.add_argument("--undo", action="store_true", help="按备份退回迁移前的样子")
    ap.add_argument("--profile", default="", help="bridge 的 profile（默认取 ticketd 服务里的 LARK_CHANNEL_PROFILE）")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)  # install.sh 已经确认过
    ap.add_argument("--update-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--channel", default="", help=argparse.SUPPRESS)
    ap.add_argument("--installed-now", action="store_true", help=argparse.SUPPRESS)


def main(argv=None) -> int:
    """python3 -m ticketd.migrate --check：install.sh --upgrade-existing 在安装之前用包里的代码只读检查。"""
    ap = argparse.ArgumentParser(prog="python3 -m ticketd.migrate")
    add_args(ap)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--target", default="")
    a = ap.parse_args(argv)
    if not (a.check or a.dry_run):
        ap.error("只支持 --check / --dry-run；动手用 ~/.ticketd/app/current/bin/ticket migrate")
    try:
        print(run(a))
    except (MigrateError, bridge.BridgeError, update.UpdateError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""控制台下发的技能（docs/spec.md §12「下发技能」）。

入网的机器上，控制台可以给本机的 worker 下发技能（Agent Skills 格式：一个目录、一个 SKILL.md、可选的脚本），
以及技能要用的凭据。下发的技能只给 worker 用：

- 技能内容（名字 + 全部文件）要有发布者的签名：和版本包同一把 main 公钥（入网时按指纹信任的那把）、命名空间
  `larkyard-skill`，控制台只分发、改不了内容（凭据是数据，不签）。验不过整批不装。
- 装在 `$TICKETD_HOME/skills/`，整个目录是一个 Claude Code 插件（`.claude-plugin/plugin.json`，插件名 larkyard）；
  Claude worker 每轮以 `--plugin-dir` 加载，技能名带 `larkyard:` 前缀。用户自己的 Claude Code 会话、bridge 的主会话
  都不加载它。worker 的沙箱能读这里（技能脚本要读凭据），不能写。
- 布局：`skills/<名字>/…`（技能文件，644）、`secrets/<名字>/<键>`（凭据，目录 700、文件 600）、`state.json`（摘要）。
  技能脚本按「本技能目录往上两级的 secrets/<名字>/」找自己的凭据。

什么时候变：
- 心跳成功时，响应里的 `skills` 是控制台算的摘要；和 `state.json` 里的不同，才 `GET /api/v1/skills` 领全部内容，
  整个目录换成新的（先在旁边写好，再两次 rename）。列表为空也照样换：只剩 `state.json`，worker 不再加载插件。
- 设备被吊销 / 已退出 / 控制台不认识（心跳停止）、`ticket enroll --leave`、本机不再入网：删掉整个目录。
- 控制台的心跳响应里没有 `skills`（旧版控制台）、摘要格式不对、领取失败、内容不合规：本机不动，下次心跳再说。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import config, updsig

PATH = "/api/v1/skills"
PLUGIN = "larkyard"
PULL_LIMIT = 1024 * 1024                      # 领取响应的上限
MAX_SKILLS = 8
MAX_FILES = 64                                # 每个技能
MAX_BYTES = 512 * 1024                        # 所有技能文件合计
MAX_SECRETS = 8                               # 每个技能
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PART_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")   # 路径的每一段：不能是 . / ..，不能以点开头
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SECRET_RE = re.compile(r"^[!-~]{1,4096}$")    # 可见 ASCII，不含空白
NAMESPACE = "larkyard-skill"                  # 签名命名空间：版本清单的签名冒充不了技能，反过来也一样
SIG_MAX = 8192


class Invalid(ValueError):
    pass


def root() -> Path:
    return config.home() / "skills"


def _state_path() -> Path:
    return root() / "state.json"


def state() -> Optional[dict]:
    """本机装着的：{"digest", "skills": [名字], "installed_at"}；没有或坏了返回 None。"""
    try:
        obj = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not DIGEST_RE.fullmatch(str(obj.get("digest"))) or not isinstance(obj.get("skills"), list):
        return None
    return obj


def installed() -> List[str]:
    st = state()
    return [n for n in (st or {}).get("skills", []) if isinstance(n, str) and NAME_RE.fullmatch(n)]


def plugin_dir() -> Optional[str]:
    """worker 要加载的插件目录；没装任何技能时为 None（不加 --plugin-dir）。"""
    r = root()
    if installed() and (r / ".claude-plugin" / "plugin.json").is_file():
        return str(r)
    return None


# ---------- 校验 ----------
def parse(resp) -> Tuple[str, list]:
    """GET /api/v1/skills 的响应 → (摘要, [{"name", "files": {相对路径: 文本}, "secrets": {键: 值}}])。不合规抛 Invalid。"""
    if not isinstance(resp, dict):
        raise Invalid("响应不是 JSON 对象")
    digest, skills = resp.get("digest"), resp.get("skills")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise Invalid("digest 格式不对")
    if not isinstance(skills, list) or len(skills) > MAX_SKILLS:
        raise Invalid("skills 不是列表或太多")
    out, names, total = [], set(), 0
    for s in skills:
        if not isinstance(s, dict) or set(s) - {"name", "files", "secrets", "sig"}:
            raise Invalid("技能条目的字段不对")
        name, files, secrets, sig = s.get("name"), s.get("files"), s.get("secrets", {}), s.get("sig")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in names:
            raise Invalid("技能名不合规或重复")
        names.add(name)
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES or "SKILL.md" not in files:
            raise Invalid("%s：文件列表不对（要有 SKILL.md，最多 %d 个）" % (name, MAX_FILES))
        for rel, text in files.items():
            parts = rel.split("/") if isinstance(rel, str) else []
            if not parts or len(parts) > 4 or not all(PART_RE.fullmatch(p) for p in parts) or not isinstance(text, str):
                raise Invalid("%s：文件路径或内容不合规" % name)
            total += len(text.encode("utf-8"))
        paths = set(files)
        for rel in paths:  # 不能既是文件又是目录（a 和 a/b 同时出现）
            if any(other.startswith(rel + "/") for other in paths):
                raise Invalid("%s：%s 既是文件又是目录" % (name, rel))
        if not isinstance(secrets, dict) or len(secrets) > MAX_SECRETS:
            raise Invalid("%s：凭据列表不对" % name)
        for k, v in secrets.items():
            if not KEY_RE.fullmatch(str(k)) or not isinstance(v, str) or not SECRET_RE.fullmatch(v):
                raise Invalid("%s：凭据的键或值不合规" % name)
        if not isinstance(sig, str) or len(sig) > SIG_MAX or not sig.startswith("-----BEGIN SSH SIGNATURE-----"):
            raise Invalid("%s：没有签名" % name)
        out.append({"name": name, "files": dict(files), "secrets": dict(secrets), "sig": sig})
    if total > MAX_BYTES:
        raise Invalid("技能文件合计超过 %d 字节" % MAX_BYTES)
    return digest, out


def canonical(name: str, files: dict) -> bytes:
    """签名针对的字节：{"name", "files"} 的确定性 JSON（和控制台的 assets.canonical、签名脚本一致）。"""
    return updsig.canonical({"name": name, "files": files})


def verify_signed(skills: list, verify: Optional[Callable[[bytes, bytes], bool]] = None) -> None:
    """每个技能都要验过发布者的签名（main 公钥、命名空间 larkyard-skill）；有一个不过就整批不装（抛 Invalid）。"""
    if verify is None:
        from . import update

        def verify(data, sig):
            return updsig.verify(data, sig, update.main_signers(), updsig.MAIN_PRINCIPAL, namespace=NAMESPACE)
    for s in skills:
        if not verify(canonical(s["name"], s["files"]), s["sig"].encode("ascii", "replace")):
            raise Invalid("%s：签名验不过（不是发布者签的，或者内容被改过）" % s["name"])


# ---------- 落盘 ----------
def _write(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(str(path), mode)


def _leftovers() -> list:
    home = config.home()
    try:
        return [p for p in home.iterdir() if p.name.startswith(("skills.new-", "skills.old-"))]
    except OSError:
        return []


def install(digest: str, skills: list, now: Optional[float] = None) -> None:
    """把整套内容写到旁边的新目录，再换掉 skills/。中途失败时旧的保持不动；换的那一下崩了，下次心跳重新领。"""
    now = time.time() if now is None else now
    home = config.home()
    for p in _leftovers():
        shutil.rmtree(str(p), ignore_errors=True)
    tag = "%d-%d" % (os.getpid(), int(now * 1000) % 10 ** 9)
    new = home / ("skills.new-" + tag)
    os.makedirs(str(new), 0o755)
    try:
        if skills:
            (new / ".claude-plugin").mkdir(0o755)
            meta = {"name": PLUGIN, "version": digest[:12],
                    "description": "Larkyard 控制台下发的技能（只给 worker 用，内容随控制台更新）"}
            _write(new / ".claude-plugin" / "plugin.json",
                   (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o644)
        for s in skills:
            base = new / "skills" / s["name"]
            for rel, text in sorted(s["files"].items()):
                dst = base.joinpath(*rel.split("/"))
                dst.parent.mkdir(0o755, parents=True, exist_ok=True)
                _write(dst, text.encode("utf-8"), 0o644)
            if s["secrets"]:
                sd = new / "secrets" / s["name"]
                sd.mkdir(0o700, parents=True)
                os.chmod(str(sd.parent), 0o700)
                os.chmod(str(sd), 0o700)
                for k, v in sorted(s["secrets"].items()):
                    _write(sd / k, (v + "\n").encode("ascii"), 0o600)
        st = {"digest": digest, "skills": [s["name"] for s in skills],
              "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        _write(new / "state.json", (json.dumps(st, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o644)
        r = root()
        old = home / ("skills.old-" + tag)
        if r.is_symlink():
            r.unlink()
        elif r.exists():
            os.rename(str(r), str(old))
        os.rename(str(new), str(r))
    except BaseException:
        shutil.rmtree(str(new), ignore_errors=True)
        raise
    shutil.rmtree(str(old), ignore_errors=True)


def remove() -> bool:
    """删掉下发的全部技能和凭据。返回本来有没有。删不掉抛 OSError。"""
    r = root()
    had = r.exists() or r.is_symlink()
    if r.is_symlink():
        r.unlink()
    else:
        shutil.rmtree(str(r), ignore_errors=True)
    for p in _leftovers():
        shutil.rmtree(str(p), ignore_errors=True)
    if r.exists():
        raise OSError("删不掉 %s" % r)
    return had


# ---------- 同步 ----------
def sync(remote, fetch: Callable[[], dict], now: Optional[float] = None,
         verify: Optional[Callable[[bytes, bytes], bool]] = None) -> Optional[str]:
    """心跳成功后调用。remote：响应里的 skills；fetch()：领取（GET /api/v1/skills，抛 console.ConsoleError）。
    返回发生了什么："installed:<名字,…>" / "removed" / None（没变化、旧版控制台）。领取或校验失败照样抛出，本机不动。"""
    if not isinstance(remote, str) or not DIGEST_RE.fullmatch(remote):
        return None
    st = state()
    if st is not None and st["digest"] == remote:
        return None
    digest, skills = parse(fetch())
    verify_signed(skills, verify)
    before = installed()
    install(digest, skills, now)
    names = [s["name"] for s in skills]
    if names:
        return "installed:" + ",".join(names)
    return "removed" if before else None

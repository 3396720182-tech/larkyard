#!/usr/bin/env python3
"""Larkyard 的发版工具（openspec add-signed-self-update，格式见 docs/spec.md §11）。只在发布者自己的电脑上跑。

  release.py release  --tag v0.3.0 --key <主私钥> [--min-from 0.2.0] [--notes ...]
        从 tag 打出可复现的版本包，一次签齐 canary / early / stable 三份清单
  release.py rollback --version 0.2.1 --channel stable --key <主私钥> [--hours 72]
        给已发布过的旧版本签一份回滚清单（有效期默认 72 小时，最长 7 天），不重新打包
  release.py pause    --key <主私钥> [--weeks 12] [--start 2026-12-01T00:00:00Z]
        按 7 天窗口预签一批首尾相接的暂停标记
  release.py rekey    --backup-key <离线备份私钥> --old-main <旧主公钥> --new-main <新主公钥>
        用离线备份私钥签换钥清单
  release.py publish  <签好的 .json> --to <更新源目录>
        把一份预签文件放到更新源的对应位置（不需要私钥）：通道清单、暂停标记、换钥清单
  release.py unpublish-pause --to <更新源目录>

签名用 `ssh-keygen -Y sign -n larkyard-release`，只接受 Ed25519（含硬件密钥 ed25519-sk）；--key 给 .pub 时用 ssh-agent 里的私钥。
本工具不读、不复制、不记录私钥内容，也没有在服务器上签名的模式。
序号：$LARKYARD_RELEASE_HOME（默认 ~/.larkyard-release）下的 seq 计数器 + signlog.jsonl 签名日志，每签一份加一；
计数器比日志里用过的最大序号小（被回拨）时拒绝签名。产物默认写到 $LARKYARD_RELEASE_HOME/out。
所有清单共用这一个序号：发版后签过回滚清单，再推进预签的通道清单时它的序号更小，客户端会按旧清单拒绝；
publish 发现目标位置已有序号更大的清单就拒绝，这时用 release 重新签一份。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ticketd import updsig  # noqa: E402

DEFAULT_HOURS = 72
MAX_HOURS = 7 * 24
WINDOW = 7 * 86400


class ReleaseError(Exception):
    pass


def state_home() -> Path:
    return Path(os.environ.get("LARKYARD_RELEASE_HOME") or Path.home() / ".larkyard-release").expanduser()


def _git(repo: Path, *args, binary=False) -> "str | bytes":
    p = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True)
    if p.returncode != 0:
        raise ReleaseError("git %s 失败：%s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout if binary else p.stdout.decode("utf-8")


# ---------- 打包 ----------
def tag_version(repo: Path, tag: str) -> tuple:
    """返回 (版本号, 提交号, 提交时间)。tag 不存在、__version__ 和 tag 不一致都拒绝。"""
    if not re.fullmatch(r"v\d+(\.\d+){1,3}", tag):
        raise ReleaseError("tag 要写成 v<版本号>，如 v0.3.0")
    try:
        commit = _git(repo, "rev-parse", "--verify", "%s^{commit}" % tag).strip()
    except ReleaseError:
        raise ReleaseError("tag %s 不存在" % tag)
    init = _git(repo, "show", "%s:ticketd/__init__.py" % commit)
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
    have = m.group(1) if m else ""
    if have != tag[1:]:
        raise ReleaseError("版本号与 tag 不一致：tag %s，ticketd/__init__.py 里是 %r" % (tag, have))
    when = int(_git(repo, "show", "-s", "--format=%ct", commit).strip())
    return have, commit, when


def check_clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise ReleaseError("工作树有未提交的改动，先提交或还原")


def build_package(repo: Path, commit: str, version: str, mtime: int) -> bytes:
    """只含该提交里被跟踪的文件；固定 mtime（提交时间）、属主、权限和排序，gzip 头不带时间：同一 tag 两次打包字节相同。"""
    prefix = "larkyard-%s" % version
    entries = []
    for line in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, typ, obj = meta.split()
        if typ != "blob" or mode not in ("100644", "100755"):
            raise ReleaseError("不支持打包链接或子模块：%s" % path)
        entries.append((path, mode, obj))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        dirs = set()
        for path, mode, obj in sorted(entries):
            parts = path.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo("%s/%s" % (prefix, d))
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, mtime
                    tf.addfile(ti)
            data = _git(repo, "cat-file", "blob", obj, binary=True)
            ti = tarfile.TarInfo("%s/%s" % (prefix, path))
            ti.size, ti.mtime = len(data), mtime
            ti.mode = 0o755 if mode == "100755" else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


DESTRUCTIVE = re.compile(r"\b(DROP|RENAME)\b", re.I)


def migration_warnings(repo: Path, tag: str) -> list:
    """db.py 相对上一个 tag 新增的行里有 DROP / RENAME：回滚到旧版本时旧代码可能读不了新库。"""
    try:
        prev = _git(repo, "describe", "--tags", "--abbrev=0", "%s^" % tag).strip()
    except ReleaseError:
        return []
    diff = _git(repo, "diff", prev, tag, "--", "ticketd/db.py")
    hits = [l[1:].strip() for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")
            and DESTRUCTIVE.search(l)]
    return ["ticketd/db.py 相对 %s 新增了可能破坏兼容的迁移（DROP / RENAME），自动回滚后旧版本可能读不了新库：%s"
            % (prev, h[:120]) for h in hits]


# ---------- 签名 ----------
def key_type(key: Path) -> str:
    p = subprocess.run([updsig.ssh_keygen(), "-l", "-f", str(key)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    m = re.search(r"\(([A-Z0-9-]+)\)\s*$", p.stdout.strip())
    if p.returncode != 0 or not m:
        raise ReleaseError("读不了密钥 %s 的类型" % key)
    return m.group(1)


def check_key(key: Path) -> None:
    if not key.exists():
        raise ReleaseError("密钥不存在：%s" % key)
    t = key_type(key)
    if t not in ("ED25519", "ED25519-SK"):
        raise ReleaseError("只接受 Ed25519 密钥，%s 是 %s" % (key, t))


def _log_path() -> Path:
    return state_home() / "signlog.jsonl"


def _log_max() -> int:
    best = 0
    try:
        for line in _log_path().read_text(encoding="utf-8").splitlines():
            try:
                best = max(best, int(json.loads(line)["seq"]))
            except (ValueError, KeyError, TypeError):
                raise ReleaseError("签名日志 %s 有损坏的行，先人工核对" % _log_path())
    except FileNotFoundError:
        pass
    return best


def next_seq() -> int:
    """计数器 + 1；计数器比签名日志里用过的最大序号小（被回拨）就拒绝。"""
    counter = state_home() / "seq"
    try:
        cur = int(counter.read_text(encoding="utf-8").strip() or 0)
    except FileNotFoundError:
        cur = 0
    except ValueError:
        raise ReleaseError("序号计数器 %s 不是整数" % counter)
    used = _log_max()
    if cur < used:
        raise ReleaseError("序号回退：计数器是 %d，签名日志里已用到 %d。不签名；核对后把计数器改回 %d" % (cur, used, used))
    return cur + 1


def _commit_seq(seq: int, record: dict) -> None:
    home = state_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(str(_log_path()), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(record, seq=seq), ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp = home / ("seq.tmp-%d" % os.getpid())
    tmp.write_text("%d\n" % seq, encoding="utf-8")
    os.replace(str(tmp), str(home / "seq"))


def sign_bytes(data: bytes, key: Path) -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="larkyard-sign-") as d:
        f = Path(d) / "manifest.json"
        f.write_bytes(data)
        p = subprocess.run([updsig.ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", updsig.NAMESPACE, str(f)],
                           capture_output=True, text=True, timeout=120)
        sig = Path(str(f) + ".sig")
        if p.returncode != 0 or not sig.is_file():
            raise ReleaseError("签名失败：%s" % p.stderr.strip()[:300])
        return sig.read_bytes()


def sign_doc(doc: dict, key: Path, dest: Path, now: float) -> dict:
    """分配序号、签名、写出 .json 和 .sig，再记进签名日志。"""
    seq = next_seq()
    doc = dict(doc, format=updsig.FORMAT, product=updsig.PRODUCT, seq=seq, signed_at=updsig.iso(now))
    data = updsig.canonical(doc)
    sig = sign_bytes(data, key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    Path(str(dest) + ".sig").write_bytes(sig)
    _commit_seq(seq, {"kind": doc["kind"], "version": doc.get("version"), "channel": doc.get("channel"),
                      "sha256": hashlib.sha256(data).hexdigest(), "file": str(dest), "at": doc["signed_at"],
                      "key_fingerprint": _fp(key)})
    return doc


def _fp(key: Path) -> str:
    p = subprocess.run([updsig.ssh_keygen(), "-l", "-f", str(key)], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    parts = p.stdout.split()
    return parts[1] if len(parts) > 1 else ""


# ---------- 命令 ----------
def cmd_release(a) -> list:
    repo = Path(a.repo).resolve()
    out = Path(a.out)
    key = Path(a.key).expanduser()
    check_key(key)
    check_clean(repo)
    version, commit, when = tag_version(repo, a.tag)
    updsig.version_tuple(a.min_from)
    lines = ["⚠️ " + w for w in migration_warnings(repo, a.tag)]
    pkg = build_package(repo, commit, version, when)
    name = "larkyard-%s.tar.gz" % version
    pdir = out / "packages"
    pdir.mkdir(parents=True, exist_ok=True)
    pp = pdir / name
    if pp.exists() and pp.read_bytes() != pkg:
        raise ReleaseError("%s 已存在且内容不同（同一个 tag 打出了不同的包？）" % pp)
    pp.write_bytes(pkg)
    sha = hashlib.sha256(pkg).hexdigest()
    lines.append("包：%s  sha256 %s  %d 字节" % (pp, sha, len(pkg)))
    now = time.time()
    for ch in updsig.CHANNELS:
        doc = sign_doc({"kind": "release", "version": version, "package": name, "sha256": sha, "size": len(pkg),
                        "channel": ch, "min_from": a.min_from, "commit": commit, "notes": a.notes},
                       key, out / "signed" / version / ("%s.json" % ch), now)
        lines.append("清单：%s  序号 %d" % (out / "signed" / version / ("%s.json" % ch), doc["seq"]))
    lines.append("推进到某个通道：release.py publish %s --to <更新源目录>" % (out / "signed" / version / "canary.json"))
    return lines


def _release_doc(out: Path, version: str) -> dict:
    for ch in updsig.CHANNELS:
        p = out / "signed" / version / ("%s.json" % ch)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    raise ReleaseError("没找到 %s 的发版清单（%s）：只能为发过的版本签回滚清单" % (version, out / "signed" / version))


def cmd_rollback(a) -> list:
    out = Path(a.out)
    key = Path(a.key).expanduser()
    if a.hours <= 0 or a.hours > MAX_HOURS:
        raise ReleaseError("回滚清单的有效期最长 7 天（%d 小时）" % MAX_HOURS)
    if a.channel not in updsig.CHANNELS:
        raise ReleaseError("通道只能是 %s" % " / ".join(updsig.CHANNELS))
    check_key(key)
    base = _release_doc(out, a.version)
    pkg = out / "packages" / base["package"]
    if not pkg.is_file() or hashlib.sha256(pkg.read_bytes()).hexdigest() != base["sha256"]:
        raise ReleaseError("旧版本的包 %s 不在或和发版清单不一致" % pkg)
    now = time.time()
    doc = {k: base[k] for k in ("version", "package", "sha256", "size", "min_from", "commit")}
    doc.update(kind="rollback", channel=a.channel, notes=a.notes or "回滚到 %s" % a.version,
               expires_at=updsig.iso(now + a.hours * 3600))
    dest = out / "signed" / a.version / ("rollback-%s-%d.json" % (a.channel, next_seq()))
    doc = sign_doc(doc, key, dest, now)
    return ["回滚清单：%s  序号 %d  到期 %s" % (dest, doc["seq"], doc["expires_at"])]


def cmd_pause(a) -> list:
    key = Path(a.key).expanduser()
    check_key(key)
    if not 1 <= a.weeks <= 52:
        raise ReleaseError("--weeks 在 1～52 之间")
    now = time.time()
    start = updsig.parse_iso(a.start) if a.start else float(int(now // 86400) * 86400)
    lines = []
    for i in range(a.weeks):
        nb, na = start + i * WINDOW, start + (i + 1) * WINDOW
        dest = Path(a.out) / "signed" / "pause" / ("%s.json" % updsig.iso(nb)[:10])
        doc = sign_doc({"kind": "pause", "not_before": updsig.iso(nb), "not_after": updsig.iso(na)}, key, dest, now)
        lines.append("暂停标记：%s  %s ～ %s  序号 %d" % (dest, doc["not_before"], doc["not_after"], doc["seq"]))
    return lines


def cmd_rekey(a) -> list:
    key = Path(a.backup_key).expanduser()
    check_key(key)
    new_main = updsig.parse_pubkey(updsig.read_pubkey_arg(a.new_main))
    old_main = updsig.parse_pubkey(updsig.read_pubkey_arg(a.old_main))
    if _fp(key) in (updsig.fingerprint(new_main), updsig.fingerprint(old_main)):
        raise ReleaseError("新、旧主公钥都不能和备份密钥是同一把")
    if new_main == old_main:
        raise ReleaseError("新主公钥和旧主公钥是同一把")
    now = time.time()
    dest = Path(a.out) / "signed" / ("rekey-%d.json" % next_seq())
    doc = sign_doc({"kind": "rekey", "new_main_key": new_main, "old_main_key": old_main}, key, dest, now)
    return ["换钥清单：%s  序号 %d  新主公钥 %s" % (dest, doc["seq"], updsig.fingerprint(new_main))]


def _copy_pair(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sig = Path(str(src) + ".sig")
    if not sig.is_file():
        raise ReleaseError("缺签名文件 %s" % sig)
    # 先放签名、再原子替换清单，客户端读到的总是一对
    shutil.copyfile(str(sig), str(dst) + ".sig.tmp")
    shutil.copyfile(str(src), str(dst) + ".tmp")
    os.replace(str(dst) + ".sig.tmp", str(dst) + ".sig")
    os.replace(str(dst) + ".tmp", str(dst))


def cmd_publish(a) -> list:
    src = Path(a.file)
    doc = json.loads(src.read_text(encoding="utf-8"))
    to = Path(a.to)
    kind = doc.get("kind")
    if kind in ("release", "rollback"):
        pkg = Path(a.packages or (src.parent.parent.parent / "packages")) / doc["package"]
        if not pkg.is_file():
            raise ReleaseError("找不到包 %s（用 --packages 指定目录）" % pkg)
        dst_pkg = to / "packages" / doc["package"]
        if not dst_pkg.exists() or dst_pkg.read_bytes() != pkg.read_bytes():
            dst_pkg.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(pkg), str(dst_pkg) + ".tmp")
            os.replace(str(dst_pkg) + ".tmp", str(dst_pkg))
        dst = to / "channels" / doc["channel"] / "manifest.json"
    elif kind == "pause":
        dst = to / "pause.json"
    elif kind == "rekey":
        dst = to / "rekey.json"
    else:
        raise ReleaseError("不认识的类型：%r" % (kind,))
    try:
        live = json.loads(dst.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = {}
    if kind != "pause" and isinstance(live, dict) and isinstance(live.get("seq"), int) \
            and live["seq"] > doc.get("seq", 0):  # 暂停标记按时间窗口生效，客户端不比序号
        # 客户端只认更大的序号：比如推进前这个通道发过回滚清单，预签的清单放上去也会被当成重放拒绝
        hint = "用 release.py release 为这个版本重新签" if kind == "release" else "重新签一份"
        raise ReleaseError("%s 上现在是序号 %d 的%s清单，这份是 %d：客户端会按旧清单拒绝。%s" % (
            dst, live["seq"], live.get("kind", ""), doc.get("seq", 0), hint))
    _copy_pair(src, dst)
    return ["已发布：%s → %s" % (src, dst)]


def cmd_unpublish_pause(a) -> list:
    to = Path(a.to)
    for p in (to / "pause.json", to / "pause.json.sig"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return ["已撤下暂停标记"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="release.py", description="Larkyard 发版工具（只在发布者本机运行）")
    p.add_argument("--out", default=None, help="产物目录（默认 $LARKYARD_RELEASE_HOME/out）")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("release")
    r.add_argument("--tag", required=True)
    r.add_argument("--key", required=True, help="Ed25519 主私钥（或 .pub，私钥在 ssh-agent 里）")
    r.add_argument("--min-from", default="0.0.0", help="最低可升级起点版本")
    r.add_argument("--notes", default="")
    r.add_argument("--repo", default=str(REPO))
    rb = sub.add_parser("rollback")
    rb.add_argument("--version", required=True)
    rb.add_argument("--channel", required=True)
    rb.add_argument("--key", required=True)
    rb.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    rb.add_argument("--notes", default="")
    pa = sub.add_parser("pause")
    pa.add_argument("--key", required=True)
    pa.add_argument("--weeks", type=int, default=12)
    pa.add_argument("--start", help="第一个窗口的开始时间（UTC，如 2026-12-01T00:00:00Z）；默认今天 0 点（UTC）")
    rk = sub.add_parser("rekey")
    rk.add_argument("--backup-key", required=True)
    rk.add_argument("--new-main", required=True, help="新主公钥（文件或内容）")
    rk.add_argument("--old-main", required=True, help="要被替换的旧主公钥（文件或内容）：客户端只在信任的正是它时才换")
    pu = sub.add_parser("publish")
    pu.add_argument("file")
    pu.add_argument("--to", required=True)
    pu.add_argument("--packages")
    up = sub.add_parser("unpublish-pause")
    up.add_argument("--to", required=True)
    return p


COMMANDS = {"release": cmd_release, "rollback": cmd_rollback, "pause": cmd_pause, "rekey": cmd_rekey,
            "publish": cmd_publish, "unpublish-pause": cmd_unpublish_pause}


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.out is None:
        a.out = str(state_home() / "out")
    try:
        lines = COMMANDS[a.cmd](a)
    except (ReleaseError, ValueError, OSError) as e:
        print("release: %s" % e, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

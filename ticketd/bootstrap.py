"""一行安装的第二段：install.sh 核对过清单签名和包的 SHA-256、用系统 tar 解出这个包之后，
用包里的这份代码（python3 -m ticketd.bootstrap）把它装成受管安装。

再验一遍（同一份 updsig 代码）→ 解包到 ~/.ticketd/app/versions/<版本>（update.extract 的规则）→ 启动器、current →
信任两把公钥 → 记下已接受的清单 → config.env 的更新源 → Node / lark-cli 运行时 → bridge/current。
可重复执行：已经做过的步骤跳过；本机已是更高版本时不降级。见 openspec vendor-bridge-one-line-install（one-line-install）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import bridge, config, db, launcher, updsig, update


class BootstrapError(Exception):
    pass


def _read(p: str) -> bytes:
    with open(p, "rb") as f:
        return f.read()


def verify(manifest: bytes, sig: bytes, main_key: str, main_fp: str, backup_key: str, backup_fp: str,
           channel: str, package: Path) -> dict:
    """核对指纹、验签、清单字段、包的大小和 SHA-256。返回清单。任何一项不对抛 BootstrapError。"""
    keys = {}
    for label, key, fp in (("主公钥", main_key, main_fp), ("备份公钥", backup_key, backup_fp)):
        try:
            k = updsig.parse_pubkey(key)
        except ValueError as e:
            raise BootstrapError("%s格式不对：%s" % (label, e))
        if updsig.fingerprint(k) != fp:
            raise BootstrapError("%s指纹不一致：实际 %s，应为 %s" % (label, updsig.fingerprint(k), fp))
        keys[label] = k
    if keys["主公钥"] == keys["备份公钥"]:
        raise BootstrapError("主公钥和备份公钥不能是同一把")
    with tempfile.TemporaryDirectory() as td:
        signers = Path(td) / "main.allowed_signers"
        signers.write_text("%s %s\n" % (updsig.MAIN_PRINCIPAL, keys["主公钥"]), encoding="utf-8")
        if not updsig.verify(manifest, sig, signers, updsig.MAIN_PRINCIPAL):
            raise BootstrapError("清单签名验证失败")
    try:
        # 通道上也可能是回滚清单（指回某个旧版本）：新机器照样装它指向的版本
        m = update._load(manifest, ("release", "rollback"))
    except update.UpdateError as e:
        raise BootstrapError("清单不对：%s" % e)
    if m.get("channel") != channel:
        raise BootstrapError("清单是 %s 通道的，不是 %s" % (m.get("channel"), channel))
    version = m.get("version")
    try:
        updsig.version_tuple(version)
    except ValueError as e:
        raise BootstrapError(str(e))
    if m.get("package") != "larkyard-%s.tar.gz" % version:
        raise BootstrapError("清单里的包名不对：%r" % m.get("package"))
    if package.stat().st_size != m.get("size") or update.file_sha256(package) != m.get("sha256"):
        raise BootstrapError("包的大小或 SHA-256 和清单不一致")
    return m


def _set_config(values: dict) -> list:
    """config.env 不存在就从示例生成；只补没写过的键，已有的值不覆盖。返回改了哪些键。"""
    p = config.home() / "config.env"
    if not any(values.values()):
        return []  # 没有要写的：不为此新建 config.env
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        example = update.current_link() / "config.example.env"
        p.write_text(example.read_text(encoding="utf-8") if example.is_file() else "", encoding="utf-8")
        os.chmod(str(p), 0o600)
    text = p.read_text(encoding="utf-8")
    have = {m.group(1) for m in re.finditer(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", text, re.M)}
    added = []
    for k, v in values.items():
        if not v or k in have:
            continue
        if not text.endswith("\n"):
            text += "\n"
        text += "%s=%s\n" % (k, v)
        added.append(k)
    if added:
        tmp = p.with_name("config.env.tmp-%d" % os.getpid())
        tmp.write_text(text, encoding="utf-8")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(p))
    return added


def install(package: Path, manifest: bytes, sig: bytes, main_key: str, main_fp: str, backup_key: str,
            backup_fp: str, source: str, channel: str, fetch=bridge.fetch_verified, update_url: bool = True,
            upgrade_existing: bool = False) -> list:
    """upgrade_existing（install.sh --upgrade-existing）：不写 config.env、不打开数据库——旧守护进程还在跑，
    库要等 ticket migrate 停了它、拷完快照再动；信任公钥和记下清单记进 bootstrap-db.json，由 migrate 调 finish_db 做。"""
    if os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"):
        raise BootstrapError("worker 里不能安装")
    m = verify(manifest, sig, main_key, main_fp, backup_key, backup_fp, channel, package)
    version = m["version"]
    out = []
    app = update.app_dir()
    cur = update.current_version() if update.current_link().is_symlink() else ""
    if cur and not updsig.VERSION_RE.fullmatch(cur):
        raise BootstrapError("%s 指向的不是版本目录（%s），先手工检查" % (update.current_link(), cur))
    switched = False
    if cur and updsig.version_tuple(cur) > updsig.version_tuple(version):
        out.append("本机已是 %s，比发布的 %s 新：不降级" % (cur, version))
        vd = update.current_link().resolve()
    elif cur == version and (update.current_link() / "bin" / "ticketd").is_file():
        out.append("本机已是 %s" % version)
        vd = update.current_link().resolve()
    elif cur:
        # 已是受管安装、版本更低：不在这里切换（那样跳过了试用期和自动切回，守护进程也还在跑旧代码），
        # 交给守护进程按现有的更新流程装
        out.append("本机已是受管安装 %s：%s 由守护进程按试用期流程自动安装（马上检查：ticket update check）"
                   % (cur, version))
        vd = update.current_link().resolve()
    else:
        vd = update.versions_dir() / version
        if not (vd.exists() and update.package_version(vd) == version and (vd / "bin" / "ticketd").is_file()):
            vd = update.unpack(package, version)
        # 先装运行时再切 current：下载或核对失败时还不是「装了一半」的受管安装
        if (vd / "vendor" / "runtime.json").is_file():
            out += bridge.ensure_runtime(vd, fetch=fetch)
        with update.state_lock():
            update._atomic_copy((vd / "ticketd" / "launcher.py").read_bytes(), app / "launcher.py")
            launcher.switch(str(app), version)
            st = update.read_state()
            keep = update.carried(st) if st else {}
            keep.update({"status": "confirmed", "version": version, "installed_by": "install.sh",
                         "installed_at": time.time()})
            update.write_state(keep)
        switched = True
        out.append("已安装 Larkyard %s（%s）" % (version, vd))

    if upgrade_existing:
        pending = {"manifest": manifest.decode("utf-8"), "main_key": main_key, "main_fp": main_fp,
                   "backup_key": backup_key, "backup_fp": backup_fp, "channel": channel, "switched": switched}
        update._atomic_copy(json.dumps(pending).encode("utf-8"), pending_db_path(), 0o600)
    else:
        added = _set_config({"TICKETD_UPDATE_URL": source if update_url else "",
                             "TICKETD_UPDATE_CHANNEL": channel if channel != "stable" else ""})
        if added:
            out.append("config.env：写入 %s" % "、".join(added))
        out += _db_steps(m, manifest, main_key, main_fp, backup_key, backup_fp, channel, switched)

    if (vd / "vendor" / "runtime.json").is_file() and not switched:
        out += bridge.ensure_runtime(vd, fetch=fetch)
    if (vd / "vendor" / "bridge" / bridge.BRIDGE_ENTRY).is_file() and not bridge.bridge_link().exists():
        bridge.link_bridge(vd)
        out.append("bridge/current → %s" % vd.name)
    return out


def _db_steps(m: dict, manifest: bytes, main_key: str, main_fp: str, backup_key: str, backup_fp: str, channel: str,
              switched: bool) -> list:
    out = []
    con = db.connect()
    try:
        main_now = updsig.signer_key(update.main_signers())
        if main_now != updsig.parse_pubkey(main_key) or \
                updsig.signer_key(update.backup_signers()) != updsig.parse_pubkey(backup_key):
            out += update.trust(con, main_key, main_fp, backup_key, backup_fp)
        # 只记下本机通道的、刚装上的这份清单：别的通道的序号会让守护进程把本通道的清单当成旧清单
        if switched and config.update_channel() == channel and m["seq"] >= update.max_seq(con):
            update._accept(con, m, manifest)
        elif config.update_channel() != channel:
            out.append("⚠️ config.env 里的更新通道是 %s，这次装的是 %s 通道的版本；以后按 %s 更新" % (
                config.update_channel(), channel, config.update_channel()))
    finally:
        con.close()
    return out


def pending_db_path() -> Path:
    return update.app_dir() / "bootstrap-db.json"


def finish_db() -> list:
    """install.sh --upgrade-existing 推迟的数据库步骤（ticket migrate 停掉旧守护进程、拷完快照后调用）。没有就什么都不做。"""
    p = pending_db_path()
    if not p.is_file():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    manifest = d["manifest"].encode("utf-8")
    m = update._load(manifest, ("release", "rollback"))
    out = _db_steps(m, manifest, d["main_key"], d["main_fp"], d["backup_key"], d["backup_fp"], d["channel"],
                    bool(d.get("switched")))
    p.unlink()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ticketd.bootstrap", description="一行安装的第二段（由 install.sh 调用）")
    ap.add_argument("--package", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--main-key", required=True)
    ap.add_argument("--backup-key", required=True)
    ap.add_argument("--main-fp", required=True)
    ap.add_argument("--backup-fp", required=True)
    ap.add_argument("--from", dest="source", required=True)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--no-update-url", action="store_true", help="不写 TICKETD_UPDATE_URL（要入网的机器从控制台更新）")
    ap.add_argument("--upgrade-existing", action="store_true", help="升级已有安装：不写 config.env、不打开数据库（交给 ticket migrate）")
    a = ap.parse_args(argv)
    try:
        lines = install(Path(a.package), _read(a.manifest), _read(a.sig), _read(a.main_key).decode("utf-8"),
                        a.main_fp, _read(a.backup_key).decode("utf-8"), a.backup_fp, a.source, a.channel,
                        update_url=not a.no_update_url, upgrade_existing=a.upgrade_existing)
    except (BootstrapError, update.UpdateError, bridge.BridgeError, OSError) as e:
        print("install: %s" % e, file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

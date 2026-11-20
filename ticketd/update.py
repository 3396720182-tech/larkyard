"""自动更新的客户端（openspec add-signed-self-update，规格见 docs/spec.md §11）。

默认关闭：只有本机配了 TICKETD_UPDATE_URL、信任了主公钥和备份公钥、是受管安装、而且 `ssh-keygen -Y verify` 可用时才启用；
没有启用时这里不发任何网络请求。

- 检查（守护进程里的后台线程，或 `ticket update check`）：换钥清单 → 暂停标记 → 本机通道的清单；清单依次过
  验签 → 格式和产品名 → 通道 → 序号 → 降级保护 / 回滚有效期 → 最低起点 → 下载包、核对大小和 SHA-256，
  全过才记为待安装，并把序号记进 kv 的 update.max_seq（只增不减）。
- 安装（守护进程主循环）：空闲、没有本地暂停 / 更新源暂停时，解包到 ~/.ticketd/app/versions/<版本>/、
  写 update-state.json、原子切换 current，然后按停机流程退出，由 launchd / systemd 重新拉起（经版本目录外的启动器）。
- 自检：新版本守护进程转满 CONFIRM_AFTER 秒、主循环没有持续报错、版本号对得上，就记为已确认；
  确认之前崩溃或超时由启动器（ticketd/launcher.py）切回，切回后的旧版本在这里私信主用户、拉黑那个版本。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__, config, db, launcher, metrics, updsig

KIND = "update"  # events.kind；code 是下面 CODES 里的枚举
CODES = {
    "check_failed": "检查失败", "bad_signature": "验签失败", "bad_format": "清单格式不对", "bad_hash": "校验失败",
    "wrong_channel": "通道不符", "stale": "旧清单", "downgrade": "拒绝降级", "rollback_expired": "回滚已过期",
    "too_old": "需要手工升级", "blocked": "跳过已拉黑的版本", "downloaded": "已下载待安装", "installed": "已安装",
    "confirmed": "已确认", "rolled_back": "更新失败已回滚", "rollback_failed": "更新失败且切不回",
    "manual_rollback": "手工切回", "rekey": "已换钥", "rekey_invalid": "换钥清单无效", "pause_invalid": "暂停标记无效",
    "install_failed": "安装失败", "withdrawn": "待装版本被撤回",
    "stale_code": "已切换但还在跑旧代码",
}
MANIFEST_LIMIT = 64 * 1024
PACKAGE_LIMIT = 200 * 1024 * 1024
FETCH_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 600.0
KEEP_VERSIONS = 3
PAUSE_MAX = 7 * 86400 + 3600  # 一份暂停标记最长 7 天（发版工具按 7 天窗口签；留一小时余量）
CONFIRM_AFTER = 60.0       # 新版本守护进程转满这么久（且不超过试用期的 1/3）算自检通过
HEAL_TRIES = 3            # 补发重启前记状态连续失败这么多次就放弃补发（见 Updater._heal）
EXIT_RESTART = 75          # 切换版本后守护进程用这个退出码退出，由服务管理器重新拉起
EVENT_DEDUPE = 86400.0     # 同一种、同一原因的更新事件一天只记一条（每小时检查一次，不刷屏）
WORKER_DENIED = ("trust", "hold", "resume", "rollback", "adopt")


class UpdateError(Exception):
    pass


class PendingDropped(UpdateError):
    """待安装记录作废了（本地包和清单对不上、回滚清单过期、版本已拉黑）：已经丢掉，下次检查重新来过。
    和包本身坏了不同，不拉黑这个版本。"""


# ---------- 路径和状态 ----------
def app_dir() -> Path:
    return config.home() / "app"


def versions_dir() -> Path:
    return app_dir() / "versions"


def current_link() -> Path:
    return app_dir() / "current"


def update_dir() -> Path:
    return config.home() / "update"


def main_signers() -> Path:
    return update_dir() / "main.allowed_signers"


def backup_signers() -> Path:
    return update_dir() / "backup.allowed_signers"


def read_state() -> dict:
    return launcher.read_state(str(app_dir()))


def write_state(st: dict) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    launcher.write_state(str(app_dir()), st)


def state_lock():
    app_dir().mkdir(parents=True, exist_ok=True)
    return launcher.locked(str(app_dir()))


def _atomic_copy(data: bytes, dst: Path, mode: int = 0o644) -> None:
    """写临时文件、fsync、rename、fsync 目录：启动器断电后读到半个文件，服务就起不来、也切不回。"""
    tmp = dst.with_name("%s.tmp-%d" % (dst.name, os.getpid()))
    with open(str(tmp), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(dst))
    try:
        dfd = os.open(str(dst.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def mark_clean_exit() -> None:
    """守护进程收到停机信号、正常退出：试用期里这次退出不算崩溃（用户重启服务或电脑）。"""
    try:
        with state_lock():
            st = read_state()
            if st.get("status") == "probation":
                st["clean_exit"] = True
                write_state(st)
    except OSError:
        pass


CARRY = ("adopted_from", "label", "workspace_md", "workspace_root", "service_env")  # 受管安装的来历：每次重写状态都要带上（--undo 要用）


def carried(before: dict) -> dict:
    return {k: before[k] for k in CARRY if before.get(k)}


def is_managed(repo: Optional[Path] = None) -> bool:
    """当前运行的代码就是 ~/.ticketd/app/current 指向的那个版本目录。"""
    repo = Path(repo or config.REPO).resolve()
    cur = current_link()
    try:
        return cur.is_symlink() and cur.resolve() == repo and repo.parent == versions_dir().resolve()
    except OSError:
        return False


def stale_code(repo: Optional[Path] = None) -> Optional[str]:
    """当前运行的是某个版本目录里的代码，current 却已经指向另一个版本目录（切换之后没重启成）：返回 current 指向的
    版本，否则 None。从克隆就地运行、没有 current 的都不算（按 current 重启也跑不到那里，会反复重启）。"""
    repo = Path(repo or config.REPO).resolve()
    cur = current_link()
    try:
        vd = versions_dir().resolve()
        if repo.parent != vd or not cur.is_symlink():
            return None
        target = cur.resolve()
        if target == repo or target.parent != vd or not (target / "bin" / "ticketd").is_file():
            return None
        return target.name
    except OSError:
        return None


def kv_json(con, key: str, default=None):
    raw = metrics.kv_get(con, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def kv_put(con, key: str, value) -> None:
    metrics.kv_set(con, key, json.dumps(value, ensure_ascii=False, sort_keys=True))


def kv_del(con, key: str) -> None:
    con.execute("DELETE FROM kv WHERE key=?", (key,))


def max_seq(con) -> int:
    try:
        return int(metrics.kv_get(con, "update.max_seq") or 0)
    except ValueError:
        return 0


def _raise_seq(con, seq: int) -> None:
    """只增不减；回滚到旧版本代码后仍然在库里。"""
    with db.tx(con):
        if seq > max_seq(con):
            metrics.kv_set(con, "update.max_seq", str(seq))


def blocked_versions(con) -> list:
    return kv_json(con, "update.blocked", []) or []


def block_version(con, version: str) -> None:
    b = blocked_versions(con)
    if version and version not in b:
        kv_put(con, "update.blocked", (b + [version])[-20:])
    pend = kv_json(con, "update.pending")
    if pend and pend.get("version") == version:
        _drop_pending(con)


def event(con, code: str, detail: str = "", dedupe: bool = True) -> None:
    """记一条更新事件（events.kind=update）。重复的检查结果一天只记一条。"""
    if dedupe:
        row = con.execute("SELECT code, detail, created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                          (KIND,)).fetchone()
        if row and row["code"] == code and row["detail"] == detail[:300] and db.now() - row["created_at"] < EVENT_DEDUPE:
            return
    metrics.record(con, metrics.SYSTEM_TICKET, KIND, detail, code=code)


# ---------- 启用条件 ----------
_pinned = threading.local()  # check() 进行中固定的更新源：中途 --leave 也不会改走普通连接去请求控制台


def source() -> tuple:
    """(更新源, 是否经控制台)。本机配置了 TICKETD_UPDATE_URL 时以它为准；没配、但入网了控制台时是控制台上的
    更新源（经 console 的连接、带设备令牌）；都没有为 ("", False)。check() 进行中返回开始时定下的那个。"""
    pin = getattr(_pinned, "src", None)
    if pin is not None:
        return pin
    url = config.update_url()
    if url:
        return url, False
    from . import console
    base = console.updates_base()
    return (base, True) if base else ("", False)


def _via_console(url: str) -> bool:
    base, via = source()
    return via and url.startswith(base.rstrip("/") + "/")


def disabled_reasons(repo: Optional[Path] = None, probe: bool = True) -> list:
    """空列表 = 启用。第一条没满足时（没配地址、也没入网控制台）就不看后面的，开源默认安装什么都不做。"""
    url = source()[0]
    if not url:
        return ["没有配置更新源（TICKETD_UPDATE_URL）"]
    why = []
    if urllib.parse.urlparse(url).scheme not in ("file", "https"):
        why.append("更新源只支持 file:// 或 https://")
    if not main_signers().is_file() or not backup_signers().is_file():
        why.append("没有信任的公钥（ticket update trust --main … --backup … --fingerprint … --backup-fingerprint …）")
    if not is_managed(repo):
        why.append("不是受管安装（先执行 ticket update adopt）")
    if probe and not updsig.verify_available():
        why.append("系统的 ssh-keygen 不支持 -Y verify（需要 OpenSSH ≥ 8.1）")
    return why


# ---------- 拉取 ----------
class FetchError(Exception):
    pass


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """只跟随到 https 的重定向：不降级到 http / ftp，也不被引到别的协议。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise urllib.error.URLError("拒绝重定向到 %s" % urllib.parse.urlparse(newurl).scheme)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirect)


def _open(url: str, timeout: float):
    if urllib.parse.urlparse(url).scheme == "file":
        p = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
        if os.path.exists(p) and not os.path.isfile(p):  # FIFO、设备文件读起来可能永远阻塞
            raise urllib.error.URLError("不是普通文件：%s" % p)
        return urllib.request.urlopen(url, timeout=timeout)
    return _opener.open(url, timeout=timeout)


def fetch(url: str, limit: int = MANIFEST_LIMIT, timeout: float = FETCH_TIMEOUT) -> Optional[bytes]:
    """file:// 或 https://。文件不存在 / 404 返回 None；其他失败抛 FetchError。"""
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("file", "https"):
        raise FetchError("不支持的地址：%s" % scheme)
    if _via_console(url):
        return _console_fetch(url, limit, timeout)
    try:
        with _open(url, timeout) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FetchError("HTTP %d" % e.code)
    except urllib.error.URLError as e:
        if isinstance(e.reason, FileNotFoundError):
            return None
        raise FetchError(str(e.reason)[:200])
    except (OSError, ValueError) as e:
        raise FetchError(repr(e)[:200])
    if len(data) > limit:
        raise FetchError("文件超过 %d 字节" % limit)
    return data


def _console_fetch(url: str, limit: int, timeout: float) -> Optional[bytes]:
    """入网后经控制台取：同一个连接函数、同一套证书校验、带设备令牌。404 返回 None。"""
    from . import console
    try:
        data = console.fetch_raw(url, limit, timeout)
    except console.ConsoleError as e:
        raise FetchError(e.text()[:200])
    if data is not None and len(data) > limit:
        raise FetchError("文件超过 %d 字节" % limit)
    return data


def download(url: str, dest: Path, size: int, sha256: str, timeout: float = DOWNLOAD_TIMEOUT) -> None:
    """边下边算 SHA-256；大小或摘要和清单不一致就删掉并抛 ValueError。网络失败抛 FetchError。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 后台检查线程和手动的 ticket update check 可能同时下载同一个包：各写各的临时文件
    part = dest.with_name("%s.part-%d-%d" % (dest.name, os.getpid(), threading.get_ident()))
    h, n = hashlib.sha256(), 0
    try:
        try:
            if _via_console(url):
                from . import console
                try:
                    data = console.fetch_raw(url, size, timeout)
                except console.ConsoleError as e:
                    raise FetchError("下载包失败：%s" % e.text()[:200])
                if data is None:
                    raise FetchError("下载包 HTTP 404")
                r = io.BytesIO(data)
            else:
                r = _open(url, timeout)
        except urllib.error.HTTPError as e:
            raise FetchError("下载包 HTTP %d" % e.code)
        except urllib.error.URLError as e:
            raise FetchError("下载包失败：%s" % str(e.reason)[:200])
        started = time.monotonic()
        with r, open(str(part), "wb") as f:
            while True:
                if time.monotonic() - started > timeout:  # 单次读的超时挡不住「每 29 秒滴一个字节」
                    raise FetchError("下载包超过 %d 秒" % timeout)
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                n += len(chunk)
                if n > size:
                    raise ValueError("包比清单写的大（> %d 字节）" % size)
                h.update(chunk)
                f.write(chunk)
        if n != size:
            raise ValueError("包大小 %d 和清单的 %d 不一致" % (n, size))
        if h.hexdigest() != sha256:
            raise ValueError("包的 SHA-256 和清单不一致")
        os.replace(str(part), str(dest))
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 检查 ----------
def _load(data: bytes, kinds: tuple) -> dict:
    """签名已验过的清单：必须是确定性序列化的 JSON 对象、格式版本和产品名对得上。"""
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("不是 JSON")
    if not isinstance(obj, dict) or updsig.canonical(obj) != data:
        raise ValueError("不是确定性序列化的 JSON 对象")
    if obj.get("format") != updsig.FORMAT or obj.get("product") != updsig.PRODUCT:
        raise ValueError("格式版本或产品名不对")
    if obj.get("kind") not in kinds:
        raise ValueError("类型不对：%r" % (obj.get("kind"),))
    if not isinstance(obj.get("seq"), int) or isinstance(obj.get("seq"), bool) or obj["seq"] <= 0:
        raise ValueError("序号不对")
    updsig.parse_iso(obj.get("signed_at"))  # 格式不对抛 ValueError
    return obj


def _no_constant(name):
    raise ValueError("不接受 %s" % name)


def _drop_pending(con) -> None:
    pend = kv_json(con, "update.pending")
    kv_del(con, "update.pending")
    if pend and pend.get("package"):
        try:
            (update_dir() / "pending" / pend["package"]).unlink()
        except OSError:
            pass


def _resume_rekey(con) -> None:
    """换钥写了新主公钥、还没重置序号就退出了：不依赖更新源上的 rekey.json 还在，直接补完。"""
    seq = metrics.kv_get(con, "update.rekey_inflight")
    if not seq:
        return
    try:
        _finish_rekey(con, int(seq))
    except ValueError:
        kv_del(con, "update.rekey_inflight")
        return
    event(con, "rekey", "补完上次中断的换钥（序号 %s）" % seq, dedupe=False)


def _check_rekey(con, base: str, now: float) -> Optional[str]:
    _resume_rekey(con)
    data = fetch(base + "/rekey.json")
    if data is None:
        return None
    sig = fetch(base + "/rekey.json.sig") or b""
    if not updsig.verify(data, sig, backup_signers(), updsig.BACKUP_PRINCIPAL):
        event(con, "rekey_invalid", "换钥清单不是信任的备份公钥签的，已忽略")
        return "rekey_invalid"
    try:
        obj = _load(data, ("rekey",))
        key = updsig.parse_pubkey(obj.get("new_main_key") or "")
        old = updsig.parse_pubkey(obj.get("old_main_key") or "")
    except ValueError as e:
        event(con, "rekey_invalid", "换钥清单格式不对：%s" % e)
        return "rekey_invalid"
    if key == updsig.signer_key(main_signers()):
        # 已经换过，或本机按带外指纹直接信任了新主公钥。上次换到一半（公钥写了、序号没重置）就进程退出的，在这里补完
        if metrics.kv_get(con, "update.rekey_inflight") == str(obj["seq"]):
            _finish_rekey(con, obj["seq"])
            event(con, "rekey", "补完上次中断的换钥（序号 %d）" % obj["seq"], dedupe=False)
        return None
    if old != updsig.signer_key(main_signers()):
        # 换钥清单只替换它点名的那把主公钥：重放一份更早的换钥清单（比如换回已经泄露、被换掉的那把）不生效
        event(con, "rekey_invalid", "换钥清单要替换的不是本机信任的主公钥，已忽略（中间漏了换钥时重新 ticket update trust）")
        return "rekey_invalid"
    try:
        done = int(metrics.kv_get(con, "update.rekey_seq") or 0)
    except ValueError:
        done = 0
    if obj["seq"] <= done:
        return None  # 已经处理过（或更早的一份）：不重复处理
    if key == updsig.signer_key(backup_signers()):
        event(con, "rekey_invalid", "换钥清单要把主公钥换成备份公钥，已忽略")
        return "rekey_invalid"
    metrics.kv_set(con, "update.rekey_inflight", str(obj["seq"]))  # 先记下「正在换」，崩溃后下次检查补完
    _drop_pending(con)  # 先丢：待装的包是旧主公钥验过的，可能来自泄露的私钥
    updsig.write_signers(main_signers(), updsig.MAIN_PRINCIPAL, key)
    _finish_rekey(con, obj["seq"])
    event(con, "rekey", "主公钥已换成 %s（序号 %d）" % (updsig.fingerprint(key), obj["seq"]), dedupe=False)
    return "rekey"


def _finish_rekey(con, seq: int) -> None:
    with db.tx(con):
        metrics.kv_set(con, "update.rekey_seq", str(seq))
        # 换钥是唯一允许 max_seq 变小的地方：旧主公钥签的东西从此全部验不过，不存在重放；
        # 主私钥泄露期间攻击者签的清单可能把 max_seq 推得很大，不重置的话发布者之后的清单都会被当成旧清单
        metrics.kv_set(con, "update.max_seq", str(seq))
        kv_del(con, "update.accepted")
        kv_del(con, "update.pause")  # 泄露的旧主私钥可能签了一份很长的暂停标记
        kv_del(con, "update.rekey_inflight")
    _drop_pending(con)  # 待装的包是旧主公钥验过的，可能来自泄露的私钥


def _check_pause(con, base: str, now: float) -> None:
    data = fetch(base + "/pause.json")
    if data is None:
        kv_del(con, "update.pause")
        return
    sig = fetch(base + "/pause.json.sig") or b""
    try:
        if not updsig.verify(data, sig, main_signers(), updsig.MAIN_PRINCIPAL):
            raise ValueError("签名验不过")
        obj = _load(data, ("pause",))
        nb, na = updsig.parse_iso(obj.get("not_before")), updsig.parse_iso(obj.get("not_after"))
        if not nb < na or na - nb > PAUSE_MAX:
            raise ValueError("暂停窗口不对（最长 7 天）")
    except ValueError as e:
        kv_del(con, "update.pause")  # 无效的暂停标记当作不存在
        event(con, "pause_invalid", "暂停标记无效（%s），当作没有暂停" % e)
        return
    kv_put(con, "update.pause", {"not_before": nb, "not_after": na})


def pause_until(con, now: float) -> Optional[float]:
    """更新源发布的暂停标记在当前时刻生效时返回它的 not_after。"""
    p = kv_json(con, "update.pause")
    if p and p.get("not_before", 0) <= now <= p.get("not_after", 0):
        return float(p["not_after"])
    return None


def hold_until(con, now: float) -> Optional[float]:
    try:
        t = float(metrics.kv_get(con, "update.hold_until") or 0)
    except ValueError:
        return None
    return t if t > now else None


def evaluate(con, data: bytes, sig: bytes, version: str, now: float) -> tuple:
    """按规格的顺序检查一份通道清单。返回 (结论, 清单或 None)；结论是 CODES 里的拒装原因，或
    "same"（就是上次接受的那份）/ "current"（已是这个版本）/ "ok"（可以下载）。"""
    if not updsig.verify(data, sig, main_signers(), updsig.MAIN_PRINCIPAL):
        return "bad_signature", None
    try:
        m = _load(data, ("release", "rollback"))
        updsig.version_tuple(m.get("version"))
        if m.get("min_from"):
            updsig.version_tuple(m["min_from"])
    except ValueError:
        return "bad_format", None
    if m.get("channel") != config.update_channel():
        return "wrong_channel", m
    digest = hashlib.sha256(data).hexdigest()
    seq = max_seq(con)
    if m["seq"] < seq or (m["seq"] == seq and metrics.kv_get(con, "update.accepted") != digest):
        return "stale", m
    if m["seq"] == seq:
        return "same", m
    cmpv = updsig.cmp_version(m["version"], version)
    if m["kind"] == "rollback":
        try:
            expires = updsig.parse_iso(m.get("expires_at"))
        except ValueError:
            return "bad_format", m
        if now >= expires:
            return "rollback_expired", m
    elif cmpv < 0:
        return "downgrade", m
    if cmpv == 0:
        return "current", m
    if m.get("min_from") and updsig.cmp_version(version, m["min_from"]) < 0:
        return "too_old", m
    if m["version"] in blocked_versions(con):
        return "blocked", m
    if m.get("package") != "larkyard-%s.tar.gz" % m["version"] or not re.match(r"^[0-9a-f]{64}$", str(m.get("sha256"))) \
            or not isinstance(m.get("size"), int) or not 0 < m["size"] <= PACKAGE_LIMIT:
        return "bad_format", m
    return "ok", m


def _accept(con, m: dict, data: bytes) -> None:
    with db.tx(con):  # 序号和「上次接受的那份」要一起落盘：只写了序号的话，同一份清单以后都算旧清单
        if m["seq"] > max_seq(con):
            metrics.kv_set(con, "update.max_seq", str(m["seq"]))
        metrics.kv_set(con, "update.accepted", hashlib.sha256(data).hexdigest())
        metrics.kv_set(con, "update.channel_signed_at",
                       str(updsig.parse_iso(m.get("signed_at"))) if m.get("signed_at") else "0")
        kv_del(con, "update.too_old")


def check(con, version: Optional[str] = None, now: Optional[float] = None) -> str:
    """检查一次。返回一行给人看的结论。调用方保证已启用（disabled_reasons 为空）。
    这一次检查里的所有请求走同一条路：开始时经控制台，中途退出控制台后剩下的请求失败，不会改走普通连接。"""
    _pinned.src = None
    _pinned.src = source()
    try:
        return _check(con, version, now)
    finally:
        _pinned.src = None


def _check(con, version: Optional[str] = None, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    version = version or __version__
    base = source()[0].rstrip("/")
    metrics.kv_set(con, "update.last_check_at", str(now))
    try:
        _check_rekey(con, base, now)
        _check_pause(con, base, now)
        ch = config.update_channel()
        data = fetch("%s/channels/%s/manifest.json" % (base, ch))
        if data is None:
            out = "通道 %s 现在没有发布的清单" % ch
            metrics.kv_set(con, "update.last_ok_at", str(now))
            return out
        sig = fetch("%s/channels/%s/manifest.json.sig" % (base, ch)) or b""
    except FetchError as e:
        metrics.kv_set(con, "update.last_error", "%s %s" % (updsig.iso(now), e))
        event(con, "check_failed", str(e))
        return "检查失败：%s" % e
    metrics.kv_set(con, "update.last_ok_at", str(now))
    verdict, m = evaluate(con, data, sig, version, now)
    label = "%s（%s）" % (m["version"], m["kind"]) if m else ""
    pend = kv_json(con, "update.pending")
    if pend and m and verdict in ("current", "ok") and updsig.cmp_version(m["version"], pend["version"]) < 0:
        # 发布者撤回：一份更新的清单（通常是回滚清单）指向比待装版本低的版本，待装的就不装了
        _drop_pending(con)
        event(con, "withdrawn", "%s 被序号 %d 的清单撤回" % (pend["version"], m["seq"]), dedupe=False)
    if verdict in ("same", "current"):
        if verdict == "current":
            _accept(con, m, data)
        else:
            metrics.kv_set(con, "update.channel_signed_at",
                           str(updsig.parse_iso(m["signed_at"])) if m.get("signed_at") else "0")
        pend = kv_json(con, "update.pending")
        return "有待安装的 %s" % pend["version"] if pend else "已是最新（%s）" % version
    if verdict == "too_old":
        metrics.kv_set(con, "update.too_old", m["min_from"])
        event(con, "too_old", "%s 要求从 %s 或以上升级，当前 %s：需要先手工升级" % (m["version"], m["min_from"], version))
        return "需要先手工升级到 %s 或以上，才能自动更新到 %s" % (m["min_from"], m["version"])
    if verdict != "ok":
        event(con, verdict, "%s 序号 %s" % (label, m["seq"] if m else "?"))
        return "不安装 %s：%s" % (label or "这份清单", CODES[verdict])
    dest = update_dir() / "pending" / m["package"]
    try:
        download("%s/packages/%s" % (base, m["package"]), dest, m["size"], m["sha256"])
    except FetchError as e:
        metrics.kv_set(con, "update.last_error", "%s %s" % (updsig.iso(now), e))
        event(con, "check_failed", str(e))
        return "检查失败：%s" % e
    except ValueError as e:
        event(con, "bad_hash", "%s：%s" % (label, e))
        return "不安装 %s：校验失败（%s）" % (label, e)
    old = kv_json(con, "update.pending")
    if old and old.get("package") != m["package"]:
        _drop_pending(con)
    kv_put(con, "update.pending", {"version": m["version"], "kind": m["kind"], "seq": m["seq"], "sha256": m["sha256"],
                                   "package": m["package"], "expires_at": m.get("expires_at"), "at": now})
    _accept(con, m, data)
    event(con, "downloaded", "%s 序号 %d" % (label, m["seq"]), dedupe=False)
    return "已下载并校验 %s，空闲时安装" % label


def trust(con, main_key: str, main_fp: str, backup_key: str, backup_fp: str) -> list:
    """按带外指纹核对后写入两把信任公钥。任何一项不对都不写。返回给人看的几行。"""
    keys = []
    for label, key, fp in (("主公钥", main_key, main_fp), ("备份公钥", backup_key, backup_fp)):
        if not updsig.FINGERPRINT_RE.match((fp or "").strip()):
            raise UpdateError("%s的指纹格式不对，应为 SHA256:<43 位>（发布者在更新源以外给你的那串）" % label)
        try:
            k = updsig.parse_pubkey(updsig.read_pubkey_arg(key))
            actual = updsig.fingerprint(k)
        except (OSError, ValueError) as e:
            raise UpdateError("%s读不了：%s" % (label, e))
        if actual != fp.strip():
            raise UpdateError("%s指纹不一致：实际是 %s，你给的是 %s。没有写入任何公钥" % (label, actual, fp.strip()))
        keys.append((k, actual))
    if keys[0][0] == keys[1][0]:
        raise UpdateError("主公钥和备份公钥不能是同一把")
    old_main = updsig.signer_key(main_signers())
    updsig.write_signers(backup_signers(), updsig.BACKUP_PRINCIPAL, keys[1][0])
    updsig.write_signers(main_signers(), updsig.MAIN_PRINCIPAL, keys[0][0])
    out = ["已信任主公钥 %s、备份公钥 %s" % (keys[0][1], keys[1][1])]
    if old_main and old_main != keys[0][0]:
        # 和换钥清单同理：旧主公钥签的东西从此验不过，旧序号（可能被泄露的私钥推得很大）不再有意义
        with db.tx(con):
            kv_del(con, "update.max_seq")
            kv_del(con, "update.accepted")
            kv_del(con, "update.pause")  # 旧主公钥签的暂停标记也不再算数
        _drop_pending(con)
        out.append("主公钥换了：已接受的最大序号清零，待安装的包已丢弃（旧主公钥签的清单从此验不过）")
    return out


def next_delay(every: Optional[int] = None, rand=random.uniform) -> float:
    every = every or config.update_every()
    return every + rand(0, every / 6.0)


# ---------- 解包和切换 ----------
def extract(tgz: Path, dest: Path, prefix: str) -> None:
    """只解普通文件和目录；拒绝绝对路径、..、链接和设备文件，所有成员都必须在 prefix/ 下。"""
    if dest.exists():
        shutil.rmtree(str(dest))
    dest.mkdir(parents=True)
    root = dest.resolve()
    with tarfile.open(str(tgz), "r:gz") as tf:
        for m in tf.getmembers():
            name = m.name
            if m.type == tarfile.XGLTYPE or m.type == tarfile.XHDTYPE:
                continue
            if name.startswith("/") or "\\" in name or ".." in name.split("/"):
                raise UpdateError("包里有不安全的路径：%r" % name)
            if prefix:
                if name.rstrip("/") == prefix:
                    continue
                if not name.startswith(prefix + "/"):
                    raise UpdateError("包里有不在 %s/ 下的文件：%r" % (prefix, name))
                name = name[len(prefix) + 1:]
            if not name:
                continue
            target = (root / name).resolve()
            if root != target and root not in target.parents:
                raise UpdateError("包里有不安全的路径：%r" % m.name)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                with open(str(target), "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(str(target), 0o755 if m.mode & 0o111 else 0o644)
            elif m.type == tarfile.GNUTYPE_LONGNAME or m.type == tarfile.GNUTYPE_LONGLINK:
                continue
            else:
                raise UpdateError("包里有链接或特殊文件：%r" % m.name)


def package_version(root: Path) -> str:
    """不导入：直接读 ticketd/__init__.py 里的 __version__。"""
    try:
        text = (root / "ticketd" / "__init__.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def unpack(tgz: Path, version: str) -> Path:
    vd = versions_dir()
    vd.mkdir(parents=True, exist_ok=True)
    final = vd / version
    if final.exists() and final.resolve() == current_link().resolve():
        raise UpdateError("%s 就是当前版本" % version)
    tmp = vd / (".%s.tmp-%d" % (version, os.getpid()))
    try:
        extract(tgz, tmp, "larkyard-%s" % version)
        if not (tmp / "bin" / "ticketd").is_file() or not (tmp / "ticketd" / "launcher.py").is_file():
            raise UpdateError("包里缺 bin/ticketd 或 ticketd/launcher.py")
        if package_version(tmp) != version:
            raise UpdateError("包里的版本号 %r 和清单的 %s 不一致" % (package_version(tmp), version))
        if final.exists():
            shutil.rmtree(str(final))
        os.replace(str(tmp), str(final))
    finally:
        if tmp.exists():
            shutil.rmtree(str(tmp), ignore_errors=True)
    return final


def current_version() -> str:
    try:
        return current_link().resolve().name
    except OSError:
        return ""


def prune(keep: int = KEEP_VERSIONS) -> list:
    """保留最近 keep 个版本目录，当前版本和状态里提到的版本永远保留。"""
    vd = versions_dir()
    if not vd.is_dir():
        return []
    st = read_state()
    protect = {current_version(), st.get("from"), st.get("to"), st.get("version"), st.get("previous")}
    dirs = sorted((p for p in vd.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    keepset = {p.name for p in dirs[:keep]} | {v for v in protect if v}
    removed = []
    for p in dirs:
        if p.name not in keepset:
            shutil.rmtree(str(p), ignore_errors=True)
            removed.append(p.name)
    return removed


def install_launcher(src_root: Path) -> bool:
    """启动器两步更新的第二步：新版本确认后才原子替换 ~/.ticketd/app/launcher.py；失败保留旧的。"""
    src = src_root / "ticketd" / "launcher.py"
    dst = app_dir() / "launcher.py"
    try:
        new = src.read_bytes()
        compile(new, str(src), "exec")  # 新启动器本身有语法错误就不换
        if dst.is_file() and dst.read_bytes() == new:
            return False
        _atomic_copy(new, dst)
        return True
    except (OSError, SyntaxError, ValueError):
        return False


# ---------- 空闲判定 ----------
def busy_reasons(con, sched=None, media=None) -> list:
    """不空闲的原因（空列表 = 空闲）：运行中的 worker / 会话压缩、沙箱外脚本、正在上传的媒体。"""
    why = []
    if sched is not None and sched.running:
        why.append("有 %d 个 worker 在运行（含压缩会话）" % len(sched.running))
    if sched is not None and sched.runs:
        why.append("有沙箱外脚本在执行")
    if con.execute("SELECT 1 FROM tickets WHERE state='running' LIMIT 1").fetchone():
        why.append("有工单在运行")
    if con.execute("SELECT 1 FROM runs WHERE status IN ('approved','running') LIMIT 1").fetchone():
        why.append("有已批准或执行中的沙箱外脚本")
    if media is not None and media.busy():
        why.append("有媒体在上传")
    return why


# ---------- 试用期、确认、切回之后 ----------
def confirm_deadline(start: float, probation: Optional[float] = None) -> float:
    """新版本必须在这之前自检通过。比试用期短一截：看门狗结束守护进程（最多 45 秒停机）、服务管理器重新拉起
    （launchd 节流 10 秒）、启动器切回，都要在试用期之内做完。"""
    p = float(probation if probation is not None else config.update_probation())
    return start + max(p - 120.0, p / 2.0)


def restart_window(version: str, now: Optional[float] = None, mark: bool = True) -> bool:
    """旧进程切换后没重启成（Updater._heal）：新版本一次都还没被拉起过（starts 为 0、看门狗没动手）时，试用期从现在
    重新算——不然等空闲的这段时间算进新版本的试用期，旧进程这时退出（补发的重启、用户重启服务、崩溃），启动器一拉起
    就按超时切回、把没跑过的新版本拉黑。mark=True（真要重启时）还记下这时的 starts，下一个进程据此判断这次重启
    有没有经过启动器（heal_ineffective）。返回是否重算了试用期。"""
    now = time.time() if now is None else now
    with state_lock():
        st = read_state()
        if st.get("status") != "probation" or st.get("to") != version:
            return False
        try:
            starts = int(st.get("starts") or 0)
        except (TypeError, ValueError):
            starts = 0
        if mark:
            st["healed_starts"] = starts
        fresh = not starts and not st.get("timed_out")
        if fresh:
            deadline = confirm_deadline(now)
            st.update(switched_at=now, deadline=deadline, window=deadline - now, boot=launcher.boot_time())
        if mark or fresh:
            write_state(st)
    return fresh


def heal_ineffective(st: dict) -> bool:
    """上次补发重启之后 starts 没变：那次重启没经过启动器（服务没走启动器拉 current），再重启跑的也还是旧代码，
    别反复重启。经过启动器的每次重启 starts 都加一，到上限启动器会切回，所以不会无限重启。"""
    if "healed_starts" not in st:
        return False
    try:
        return int(st.get("starts") or 0) <= int(st["healed_starts"])
    except (TypeError, ValueError):
        return True


def after_rollback(con) -> Optional[str]:
    """守护进程启动时调用：启动器刚切回过就私信主用户一次、拉黑那个版本。返回私信内容（测试用）。"""
    st = read_state()
    if st.get("status") not in ("rolled_back", "rollback_failed") or st.get("notified"):
        return None
    bad, good = st.get("to") or "?", st.get("from") or "?"
    why = {"timeout": "试用期内没有通过自检", "crash": "启动后反复退出"}.get(st.get("reason"), st.get("reason") or "")
    if st["status"] == "rolled_back":
        text = "更新到 %s 失败（%s），已回到 %s；之后不再自动安装 %s" % (bad, why, good, bad)
        event(con, "rolled_back", text, dedupe=False)
    else:
        text = "更新到 %s 失败（%s），而且切不回 %s（%s）：现在仍在跑 %s，请手工处理" % (
            bad, why, good, st.get("error", ""), bad)
        event(con, "rollback_failed", text, dedupe=False)
    block_version(con, bad)
    try:
        metrics.send_owner(con, "⚠️ 自动更新：" + text)
    except Exception:
        pass
    st["notified"] = True
    write_state(st)
    return text


def confirm(con, version: Optional[str] = None, now: Optional[float] = None) -> bool:
    """自检通过：记已确认、启动器两步更新的第二步、清理旧版本。"""
    now = time.time() if now is None else now
    version = version or __version__
    with state_lock():  # 和看门狗互斥：它在锁里重读，看到已确认就不动手
        st = read_state()
        if st.get("status") != "probation" or st.get("to") != version or st.get("timed_out"):
            return False
        st.update(status="confirmed", version=version, previous=st.get("from"), confirmed_at=now)
        for k in ("from", "to", "deadline", "starts", "token", "window", "timed_out", "extended", "boot",
                  "clean_exit", "healed_starts"):
            st.pop(k, None)
        write_state(st)
    metrics.kv_set(con, "update.installed_at", str(st.get("switched_at") or now))
    pend = kv_json(con, "update.pending")
    if pend and pend.get("version") == version:
        _drop_pending(con)
    replaced = install_launcher(versions_dir() / version)
    prune()
    event(con, "confirmed", "%s 自检通过%s" % (version, "，启动器已更新" if replaced else ""), dedupe=False)
    return True


def self_check(con, heartbeat_age: float = 60.0) -> list:
    """新版本的必需项（只看新版本自己会弄坏的东西，config.env 的问题旧版本一样有，不算）：数据库可读写、心跳新鲜、
    当前运行的就是 current 指向的版本。返回没通过的项。"""
    bad = []
    try:
        con.execute("SELECT COUNT(*) FROM tickets").fetchone()
        metrics.kv_set(con, "update.selfcheck_at", str(time.time()))
    except Exception as e:
        bad.append("数据库：%r" % (e,))
    try:
        if time.time() - config.heartbeat_path().stat().st_mtime > heartbeat_age:
            bad.append("心跳不新鲜")
    except OSError:
        bad.append("没有心跳文件")
    if not is_managed():
        bad.append("当前运行的不是 current 指向的版本")
    return bad


# ---------- 守护进程里的更新器 ----------
class Updater:
    """守护进程主循环每轮调用 tick()。没启用时只做两件不发请求的事：切回后的私信、试用期确认。"""

    REEVALUATE = 60.0

    def __init__(self, con, clock=time.time):
        self.con = con
        self.clock = clock
        self.started = clock()
        self.thread = None
        self.result = None
        self.next_check = self.started + 60.0 + random.uniform(0, config.update_every() / 6.0)
        self.enabled_at = 0.0
        self.enabled = False
        self.restart = False
        self.healing = False  # 发现「已切换、还在跑旧代码」，正在等空闲后重启
        self.heal_failures = 0
        try:
            after_rollback(con)
        except Exception:
            pass

    def _enabled(self) -> bool:
        now = self.clock()
        if now - self.enabled_at >= self.REEVALUATE:
            self.enabled_at = now
            self.enabled = not disabled_reasons()
        return self.enabled

    def _check_thread(self) -> None:
        con = None
        try:
            con = db.connect()
            self.result = check(con)
        except Exception as e:
            self.result = "检查出错：%r" % (e,)
            try:
                if con is not None:
                    event(con, "check_failed", repr(e)[:200])
            except Exception:
                pass
        finally:
            if con is not None:
                con.close()

    def tick(self, sched=None, media=None, healthy: bool = True) -> bool:
        """返回 True = 已切换版本，守护进程应按停机流程退出、由服务管理器重新拉起。"""
        now = self.clock()
        st = read_state()
        stale = stale_code() if st.get("status") == "probation" and not heal_ineffective(st) else None
        if self.healing and not stale:
            self.healing = False  # 状态已经变了（比如被手工处理）：不再等重启，放开调度
            if sched is not None:
                sched.update_hold = False
        if stale:
            return self._heal(stale, st, sched, media)
        if st.get("status") == "probation":
            if st.get("to") == __version__ and healthy and \
                    now - self.started >= min(CONFIRM_AFTER, config.update_probation() / 3.0) and not self_check(self.con):
                confirm(self.con, now=now)
            return False  # 试用期里不检查、不安装、不切换
        req = metrics.kv_get(self.con, "update.rollback_request")
        if req and is_managed():
            if not busy_reasons(self.con, sched, media):
                return self._manual_rollback(req, sched)
            return False
        if not self._enabled():
            return False
        if now >= self.next_check and (self.thread is None or not self.thread.is_alive()):
            self.next_check = now + next_delay()
            self.thread = threading.Thread(target=self._check_thread, name="ticketd-update", daemon=True)
            self.thread.start()
        if self.thread is not None and self.thread.is_alive():
            return False
        return self._maybe_install(sched, media, now)

    def _maybe_install(self, sched, media, now: float) -> bool:
        pend = kv_json(self.con, "update.pending")
        if not pend or hold_until(self.con, now) or pause_until(self.con, now):
            return False
        if metrics.kv_get(self.con, "update.rekey_inflight"):
            return False  # 换钥没补完：待装的包来历不明，等下次检查补完换钥（会丢掉它）
        if now < float(pend.get("retry_at") or 0):
            return False  # 上次安装因环境问题（如磁盘满）失败：退避到下一次检查的时间再试
        if busy_reasons(self.con, sched, media):
            return False
        if sched is not None:
            sched.update_hold = True  # 安装期间不启动新 worker / 沙箱外脚本
        try:
            install(self.con, pend, now=now)
        except Exception as e:
            if self._switched(pend.get("version")):
                # current 已经切到新版本（提交点之后才出的错）：照样重启，update_hold 不放开
                print("update: 切换到 %s 之后出错（%r），照样重启" % (pend.get("version"), e), flush=True)
                self.restart = True
                return True
            if sched is not None:
                sched.update_hold = False
            if isinstance(e, PendingDropped):
                pass
            elif isinstance(e, UpdateError):
                # 包本身的问题（签过名但解不开、版本号不符、缺入口）：重试不会好，拉黑这个版本、丢掉待安装
                block_version(self.con, pend["version"])
                _drop_pending(self.con)
            else:
                pend["retry_at"] = now + next_delay()
                pend["failures"] = int(pend.get("failures") or 0) + 1
                if pend["failures"] >= 3:
                    block_version(self.con, pend["version"])
                    _drop_pending(self.con)
                else:
                    kv_put(self.con, "update.pending", pend)
            return False
        self.restart = True
        return True

    @staticmethod
    def _switched(version) -> bool:
        """install 抛出之后核对：试用期已经写下、current 已经指向这个版本、本进程还在跑别的版本目录。"""
        try:
            st = read_state()
            return bool(version) and st.get("status") == "probation" and st.get("to") == version \
                and stale_code() == version
        except Exception:
            return False

    def _heal(self, target: str, st: dict, sched, media) -> bool:
        """试用期里 current 已经指向 target、本进程却还在跑旧版本（切换之后没退出成，比如当时记事件出错、
        或者旧代码就没有请求重启）：CLI 和守护进程会分别跑新旧两份代码。不再接新活，空闲后按新的 current 重启。"""
        if self.heal_failures >= HEAL_TRIES:
            return False  # 记不下状态：放弃补发（tick 已放开调度），照旧跑，免得没有防循环标记就反复重启
        if sched is not None:
            sched.update_hold = True
        if not self.healing:
            self.healing = True
            print("update: current 已切到 %s，本进程还在跑 %s：空闲后重启" % (target, __version__), flush=True)
            try:
                event(self.con, "stale_code", "current 已切到 %s，守护进程还在跑 %s，空闲后重启" % (target, __version__),
                      dedupe=False)
            except Exception:
                pass
        try:
            # 等空闲期间新版本的试用期不能先过期：剩不到一半就重算（starts 为 0 时），旧进程随时退出都不会被判超时
            left = float(st.get("deadline") or 0) - time.time()
            if not st.get("starts") and left < float(st.get("window") or 0) / 2.0:
                restart_window(target, mark=False)
        except Exception:
            pass
        if busy_reasons(self.con, sched, media):
            return False
        try:
            restart_window(target)
        except Exception as e:
            self.heal_failures += 1
            print("update: 补发重启前记状态失败（第 %d 次）：%r" % (self.heal_failures, e), flush=True)
            if self.heal_failures >= HEAL_TRIES:
                self.healing = False
                if sched is not None:
                    sched.update_hold = False
            return False
        self.restart = True
        return True

    def _manual_rollback(self, version: str, sched) -> bool:
        kv_del(self.con, "update.rollback_request")
        cur = current_version()
        if sched is not None:
            sched.update_hold = True
        try:
            launcher.switch(str(app_dir()), version)
        except (OSError, ValueError) as e:
            if sched is not None:
                sched.update_hold = False
            event(self.con, "install_failed", "切回 %s 失败：%r" % (version, e), dedupe=False)
            return False
        # current 已经切走：后面哪一步出错都要按新的 current 重启（不然守护进程挂着 update_hold 继续跑旧代码）
        self.restart = True
        try:
            st = {"status": "confirmed", "version": version, "previous": cur, "switched_at": self.clock(),
                  "manual": True}
            with state_lock():
                st.update(carried(read_state()))
                write_state(st)
            block_version(self.con, cur)  # 不再自动装回它，直到出现更高的版本
            event(self.con, "manual_rollback", "%s → %s" % (cur, version), dedupe=False)
        except Exception as e:
            print("update: 手工切回后记录状态失败：%r" % (e,), flush=True)
        return True


def install(con, pend: dict, now: Optional[float] = None) -> str:
    """把已下载、已校验的包装成新版本并切换。调用方已确认空闲。返回新版本号。"""
    now = time.time() if now is None else now
    version = pend["version"]
    pkg = update_dir() / "pending" / pend["package"]
    try:
        if pend.get("kind") == "rollback" and now >= updsig.parse_iso(pend.get("expires_at")):
            event(con, "rollback_expired", "%s 的回滚清单在安装前过期了" % version)
            _drop_pending(con)
            raise PendingDropped("回滚清单已过期")
        if version in blocked_versions(con):
            _drop_pending(con)
            raise PendingDropped("%s 已拉黑" % version)
        if not pkg.is_file() or file_sha256(pkg) != pend["sha256"]:
            event(con, "bad_hash", "%s：待安装的包和清单不一致" % version, dedupe=False)
            _drop_pending(con)
            try:
                pkg.unlink()
            except OSError:
                pass
            # 本地问题（磁盘、并发写）：签过名的清单没问题，不拉黑，下次检查重新下载
            raise PendingDropped("待安装的包和清单不一致")
        cur = current_version()
        unpack(pkg, version)
        start = time.time()
        before = read_state()
        deadline = confirm_deadline(start)
        st = {"status": "probation", "from": cur, "to": version, "starts": 0, "switched_at": start,
              "deadline": deadline, "window": deadline - start, "boot": launcher.boot_time(),
              "token": hashlib.sha256(os.urandom(16)).hexdigest()[:16]}
        st.update(carried(before))
        with state_lock():
            write_state(st)
            try:
                launcher.switch(str(app_dir()), version)
            except BaseException:
                write_state(before)
                raise
    except UpdateError as e:
        event(con, "install_failed", "%s：%s" % (version, e), dedupe=False)
        raise
    except Exception as e:
        event(con, "install_failed", "%s：%r" % (version, e), dedupe=False)
        raise
    # current 切换成功就是提交点：之后哪一步出错都不能抛给调用方，不然它当成没装上、放开 update_hold 接着用旧代码接单，
    # 磁盘上却已经是新版本的试用期（外部评测 P1；手工切回同理，见 Updater._manual_rollback）
    try:
        event(con, "installed", "%s → %s（%s，序号 %s）" % (cur, version, pend.get("kind"), pend.get("seq")),
              dedupe=False)
    except Exception as e:
        print("update: 已切换到 %s，记安装事件失败：%r" % (version, e), flush=True)
    return version


# ---------- 受管安装：adopt / adopt --undo ----------
# 克隆的 .git/config 里的 fsmonitor、钩子、外部 diff 这类会执行命令的配置一律关掉，也不让 shell 里的 GIT_* 改了仓库位置
_GIT_SAFE = ["-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false",
             "-c", "diff.external=", "-c", "core.pager=cat"]


def _git(repo: Path, *args, binary: bool = False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git"] + _GIT_SAFE + ["-C", str(repo)] + list(args), capture_output=True, env=env,
                       stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise UpdateError("git %s 失败：%s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()[:200]))
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def copy_tracked(repo: Path, dest: Path) -> None:
    """把克隆 HEAD 里的文件拷进版本目录：内容从 git 对象库取，不读工作树（标了 skip-worktree / assume-unchanged
    的改动也收不进来）。工作树仍须干净，免得用户以为自己的本地改动被带上了。"""
    if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError("%s 有未提交的改动：先提交或还原，受管安装只收被跟踪、已提交的文件" % repo)
    entries = []
    for rec in _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD", binary=True).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, typ, oid = meta.decode().split()
        path = path.decode("utf-8")
        if typ != "blob" or mode not in ("100644", "100755"):
            raise UpdateError("不支持链接或子模块：%s" % path)
        if path.startswith("/") or ".." in path.split("/"):
            raise UpdateError("路径不对：%s" % path)
        entries.append((path, mode, oid))
    if dest.exists():
        shutil.rmtree(str(dest))
    for path, mode, oid in entries:
        out = dest / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_git(repo, "cat-file", "blob", oid, binary=True))
        os.chmod(str(out), 0o755 if mode == "100755" else 0o644)


DEFAULT_LABEL = "dev.larkyard.ticketd"
SERVICE_KEYS = ("TICKETD_HOME", "LARK_CHANNEL_HOME", "LARK_CHANNEL_PROFILE", "TICKETD_PYTHON")


def service_file(label: str) -> Path:
    from . import host
    if host.system() == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % label)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "systemd" / "user" / ("%s.service" % label)


def read_service_env(path: Path) -> dict:
    """从已装好的 plist / unit 里读出 install.sh 当时用的环境（服务名以外，重装时要原样带上）。读不了返回空。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    env = {}
    if path.suffix == ".plist":
        import plistlib
        try:
            env = dict(plistlib.loads(raw).get("EnvironmentVariables") or {})
        except Exception:
            return {}
    else:
        for line in raw.decode("utf-8", "replace").splitlines():
            m = re.match(r'^Environment="([A-Z_]+)=(.*)"$', line.strip())
            if m:
                env[m.group(1)] = m.group(2)
    return {k: str(env[k]) for k in SERVICE_KEYS if env.get(k)}


def capture_service() -> tuple:
    """adopt 前记下现有服务的名字和环境：重装（adopt、--undo、adopt 失败时恢复）都按它来，不按当前 shell 的环境，
    免得自定义了 TICKETD_LABEL / TICKETD_HOME / bridge profile 的安装被装成另一个服务（对抗审查）。"""
    label = os.environ.get("TICKETD_LABEL") or DEFAULT_LABEL
    sf = service_file(label)
    if not sf.is_file():
        # 当初用自定义服务名装的、这次 shell 里又没设：照默认名装会多出第二个服务，和原来的抢 daemon.lock
        raise UpdateError("找不到服务 %s（%s）：当初装服务时设过 TICKETD_LABEL 的，带上同样的 TICKETD_LABEL 再执行；"
                          "还没装过服务的先跑 scripts/install.sh" % (label, sf))
    env = read_service_env(sf)
    env.setdefault("TICKETD_HOME", str(config.home()))
    if Path(env["TICKETD_HOME"]).expanduser().resolve() != config.home().resolve():
        raise UpdateError("服务 %s 用的数据目录是 %s，和当前的 %s 不同：用同一个 TICKETD_HOME（必要时再设 TICKETD_LABEL）"
                          "执行 adopt" % (label, env["TICKETD_HOME"], config.home()))
    return label, env


def _install_sh(repo: Path, app: Optional[Path], svc: Optional[dict] = None) -> None:
    env = dict(os.environ)
    for k in ("TICKETD_APP", "TICKETD_LABEL") + SERVICE_KEYS:
        env.pop(k, None)  # 只用记下的服务环境，shell 里碰巧设着的不混进去
    svc = svc or {}
    if svc.get("label"):
        env["TICKETD_LABEL"] = svc["label"]
    env.update(svc.get("env") or {})
    if app is not None:
        env["TICKETD_APP"] = str(app)
    p = subprocess.run(["/bin/sh", str(repo / "scripts" / "install.sh")], env=env, capture_output=True, text=True,
                       timeout=300)
    if p.returncode != 0:
        raise UpdateError("install.sh 失败：%s" % (p.stderr.strip() or p.stdout.strip())[-500:])


def _rewrite(path: Path, old: str, new: str) -> Optional[str]:
    """把文件里的 old 换成 new（改前备份）；文件不存在或没有 old 时不动。返回备份路径。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # 只换完整的命令路径：<克隆>/bin/ticketd、<克隆>/bin/ticket-x 这类不动
    pat = re.compile(re.escape(old) + r"(?![\w.-])")
    if not pat.search(text):
        return None
    bak = path.with_name("%s.bak-%s" % (path.name, time.strftime("%Y%m%d%H%M%S")))
    shutil.copy2(str(path), str(bak))
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    try:
        tmp.write_text(pat.sub(lambda m: new, text), encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()
    return str(bak)


def adopt(workspace: str = "", repo: Optional[Path] = None, run_install=_install_sh) -> list:
    """把一份就地运行的克隆转成受管安装。"""
    repo = Path(repo or config.REPO).resolve()
    if is_managed(repo) or repo.parent == versions_dir().resolve():
        raise UpdateError("已经是受管安装（%s）" % current_link())
    if not (repo / ".git").exists():
        raise UpdateError("%s 不是 git 克隆" % repo)
    version = package_version(repo)
    updsig.version_tuple(version)
    label, svc_env = capture_service()
    svc = {"label": label, "env": svc_env}
    app = app_dir()
    vd = versions_dir() / version
    tmp = versions_dir() / (".%s.adopt-%d" % (version, os.getpid()))
    vd.parent.mkdir(parents=True, exist_ok=True)
    try:
        copy_tracked(repo, tmp)
        if vd.exists():
            shutil.rmtree(str(vd))
        os.replace(str(tmp), str(vd))
    finally:
        if tmp.exists():
            shutil.rmtree(str(tmp), ignore_errors=True)
    _atomic_copy((vd / "ticketd" / "launcher.py").read_bytes(), app / "launcher.py")
    launcher.switch(str(app), version)
    st = {"status": "confirmed", "version": version, "adopted_from": str(repo), "adopted_at": time.time(),
          "label": label, "service_env": svc_env}
    out = ["版本目录：%s" % vd, "启动器：%s" % (app / "launcher.py")]
    write_state(st)  # 先记来历：后面任何一步失败，--undo 都认得这份受管安装
    if workspace:
        ws_md = Path(workspace) / "CLAUDE.md"
        # setup-workspace.sh 写的是 pwd 得到的逻辑路径（可能经过符号链接），这里的 repo 是解析过的真实路径：两种都试
        bak = None
        for root in dict.fromkeys([str(repo)] + _aliases(ws_md, repo)):
            if root:
                bak = _rewrite(ws_md, "%s/bin/ticket" % root, "%s/bin/ticket" % current_link())
                if bak:
                    st.update(workspace_md=str(ws_md), workspace_root=root)
                    write_state(st)
                    break
        if bak:
            out.append("主会话约定：%s 里的 ticket 路径改为 %s/bin/ticket（备份 %s）" % (ws_md, current_link(), bak))
        else:
            out.append("❌ 主会话约定：%s 里没有 %s/bin/ticket，没改。主会话会继续用克隆里的 ticket；"
                       "用 %s/scripts/setup-workspace.sh <工作目录> --force 重新生成" % (ws_md, repo, current_link()))
    else:
        out.append("⚠️ 找不到 bridge 工作目录，主会话约定没改（config.env 里写 TICKETD_WORKSPACE 后重跑）")
    try:
        run_install(vd, app, svc)
    except BaseException as e:  # Ctrl-C 也要恢复：install.sh 可能已经卸掉旧服务
        # install.sh 可能已经卸掉旧服务、写了一半新的：按原来的服务名和环境从克隆重装一次，再改回工作区
        try:
            run_install(repo, None, svc)
            restored = "已按原样从 %s 重装服务" % repo
        except Exception as e2:
            restored = "从 %s 重装服务也失败（%s）：在克隆里手动执行 scripts/install.sh" % (repo, e2)
        _undo_files(st, repo)
        raise UpdateError("%s；%s，工作区已改回" % (e, restored))
    out.append("服务 %s 已改为经启动器运行 %s/bin/ticketd 并已重启" % (label, current_link()))
    return out


def _aliases(md: Path, repo: Path) -> list:
    """CLAUDE.md 里写的、解析后就是这份克隆的其他路径（经过符号链接的逻辑路径）。"""
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for m in re.finditer(r"(/[^\s`'\"()<>]*?)/bin/ticket(?![\w.-])", text):
        root = m.group(1)
        try:
            if root not in out and Path(root).resolve() == repo:
                out.append(root)
        except OSError:
            continue
    return out


def _undo_files(st: dict, repo: Path) -> Optional[str]:
    bak = None
    if st.get("workspace_md"):
        root = st.get("workspace_root") or str(repo)
        bak = _rewrite(Path(st["workspace_md"]), "%s/bin/ticket" % current_link(), "%s/bin/ticket" % root)
    for p in (current_link(), app_dir() / "launcher.py"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    write_state({"status": "unadopted", "adopted_from": str(repo), "at": time.time()})
    return bak


def undo_adopt(run_install=_install_sh) -> list:
    st = read_state()
    src = st.get("adopted_from")
    if not src or not current_link().is_symlink():
        raise UpdateError("不是由 ticket update adopt 转成的受管安装，没有可退回的克隆")
    repo = Path(src)
    if not (repo / "scripts" / "install.sh").is_file():
        raise UpdateError("原来的克隆 %s 不在了：先把它克隆回来（同一路径）再退回" % repo)
    warn = []
    try:
        cur, old = current_version(), package_version(repo)
        if updsig.cmp_version(old, cur) < 0:
            # 数据库迁移只增不删（scripts/release.py 发版时检查），旧代码能读新库；但新版本加的功能退回后就没了
            warn.append("⚠️ 克隆是 %s，比正在运行的 %s 旧：退回后跑的是旧代码（库结构只增不删，旧版本能照常读写）；"
                        "要保持新版本，先在克隆里 git pull 到 %s 再退回" % (old, cur, cur))
    except (UpdateError, ValueError, OSError):
        pass
    svc = {"label": st.get("label") or DEFAULT_LABEL, "env": st.get("service_env") or {}}
    run_install(repo, None, svc)
    bak = _undo_files(st, repo)
    out = warn + ["服务 %s 已改回直接运行 %s/bin/ticketd" % (svc["label"], repo)]
    if bak:
        out.append("主会话约定里的 ticket 路径已改回 %s/bin/ticket（改前备份 %s）" % (repo, bak))
    out.append("版本目录留在 %s，确认不用后可以删；要关掉检查，删掉 config.env 里的 TICKETD_UPDATE_URL" % versions_dir())
    return out


# ---------- 命令（ticket update ...） ----------
def _in_worker() -> bool:
    return bool(os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"))


def _ts(t) -> str:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "—"


def rollback_target() -> Optional[str]:
    st = read_state()
    cur = current_version()
    prev = st.get("previous")
    try:
        if prev and (versions_dir() / prev).is_dir() and updsig.cmp_version(prev, cur) < 0:
            return prev  # 上一个已确认版本（只往低处切：手工切回之后 previous 是刚切走的高版本）
    except ValueError:
        pass
    vd = versions_dir()
    if not vd.is_dir():
        return None
    others = [p.name for p in vd.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != cur]
    lower = []
    for v in others:
        try:
            if updsig.cmp_version(v, cur) < 0:
                lower.append(v)
        except ValueError:
            continue  # 名字不是版本号的目录（手工备份之类）跳过，不连累其他候选
    if not lower:
        return None
    return max(lower, key=updsig.version_tuple)


def status_lines(con, now: Optional[float] = None) -> list:
    now = time.time() if now is None else now
    why = disabled_reasons(probe=False)
    managed = is_managed()
    lines = ["当前版本：%s（%s）  通道：%s" % (__version__, "受管安装" if managed else "就地运行",
                                         config.update_channel())]
    if why:
        lines.insert(0, "自动更新：未启用（%s）" % "；".join(why))
    elif not updsig.verify_available():
        lines.insert(0, "自动更新：未启用（系统的 ssh-keygen 不支持 -Y verify，需要 OpenSSH ≥ 8.1）")
    else:
        lines.insert(0, "自动更新：已启用（%s）" % source()[0])
    st = read_state()
    if st.get("status") == "probation":
        lines.append("试用期：%s → %s，%s 前要通过自检" % (st.get("from"), st.get("to"), _ts(st.get("deadline"))))
    inst = metrics.kv_get(con, "update.installed_at")
    if inst:
        lines.append("安装时间：%s" % _ts(inst))
    lines.append("上次检查：%s  上次成功：%s" % (_ts(metrics.kv_get(con, "update.last_check_at")),
                                         _ts(metrics.kv_get(con, "update.last_ok_at"))))
    err = metrics.kv_get(con, "update.last_error")
    if err:
        lines.append("最近一次失败：%s" % err)
    pend = kv_json(con, "update.pending")
    lines.append("待安装：%s" % ("%s（%s，序号 %s）" % (pend["version"], pend["kind"], pend["seq"]) if pend else "—"))
    too_old = metrics.kv_get(con, "update.too_old")
    if too_old:
        lines.append("⚠️ 需要先手工升级到 %s 或以上，才能继续自动更新" % too_old)
    lines.append("已接受的最大序号：%d" % max_seq(con))
    h = hold_until(con, now)
    if h:
        lines.append("本机暂停到：%s（ticket update resume 恢复）" % _ts(h))
    p = pause_until(con, now)
    if p:
        lines.append("更新源已暂停，到 %s" % _ts(p))
    b = blocked_versions(con)
    if b:
        lines.append("不再自动安装：%s" % "、".join(b))
    rows = con.execute("SELECT code, detail, created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 5",
                       (KIND,)).fetchall()
    if rows:
        lines.append("最近的更新事件：")
        lines += ["  %s %s %s" % (_ts(r["created_at"]), CODES.get(r["code"], r["code"]), r["detail"]) for r in rows]
    return lines


def doctor_line(con, now: Optional[float] = None) -> str:
    """ticket doctor 的更新一项：未启用 / 正常 / 清单过期 / 验签工具不可用。"""
    now = time.time() if now is None else now
    why = disabled_reasons(probe=False)
    url, via = source()
    if why and not url:
        return "ℹ️ 自动更新：未启用（默认关闭；%s）" % why[0]
    if why and via:
        # 更新源是入网时自动带上的控制台，不是本人配的：没转受管安装不算问题
        return "ℹ️ 自动更新：未启用：%s（更新源是控制台）" % "；".join(why)
    if not updsig.verify_available():
        return "❌ 自动更新：系统的 ssh-keygen 不支持 -Y verify（需要 OpenSSH ≥ 8.1），不能启用"
    if why:
        return "❌ 自动更新：配置了更新源但没有启用：%s" % "；".join(why)
    if read_state().get("status") == "rollback_failed":
        return "❌ 自动更新：上次更新失败且没能切回，见 ticket update status"
    try:
        signed = float(metrics.kv_get(con, "update.channel_signed_at") or 0)
    except ValueError:
        signed = 0.0
    if signed and now - signed > config.update_stale_days() * 86400:
        return "⚠️ 自动更新：通道 %s 当前清单签于 %d 天前，可能被冻结在旧清单上（确认发布者是否有新版本）" % (
            config.update_channel(), (now - signed) // 86400)
    return "✅ 自动更新：已启用（通道 %s，上次成功检查 %s）" % (config.update_channel(),
                                                 _ts(metrics.kv_get(con, "update.last_ok_at")))


def command(con, a, workspace: str = "") -> str:
    action = a.action
    if action in WORKER_DENIED and _in_worker():
        raise PermissionError("worker 里不能执行 ticket update %s：只能由用户本人在终端执行" % action)
    now = time.time()
    if action == "status":
        return "\n".join(status_lines(con, now))
    if action == "check":
        why = disabled_reasons()
        if why:
            raise UpdateError("自动更新没有启用：%s" % "；".join(why))
        return check(con, now=now)
    if action == "trust":
        if not (a.main and a.backup and a.fingerprint and a.backup_fingerprint):
            raise UpdateError("用法：ticket update trust --main <公钥> --fingerprint SHA256:… "
                              "--backup <公钥> --backup-fingerprint SHA256:…（指纹由发布者在更新源以外的渠道给你）")
        return "\n".join(trust(con, a.main, a.fingerprint, a.backup, a.backup_fingerprint))
    if action == "hold":
        days = 7 if a.days is None else a.days
        if not 0 < days <= 7:
            raise UpdateError("--days 在 1～7 之间（默认 7）")
        until = now + days * 86400
        metrics.kv_set(con, "update.hold_until", str(until))
        return "已暂停自动安装到 %s（期间仍会检查和下载）；ticket update resume 提前恢复" % _ts(until)
    if action == "resume":
        kv_del(con, "update.hold_until")
        return "已恢复自动安装"
    if action == "rollback":
        if not is_managed():
            raise UpdateError("不是受管安装，没有可切回的版本目录")
        target = rollback_target()
        if not target:
            raise UpdateError("本机没有可切回的旧版本目录（%s）" % versions_dir())
        metrics.kv_set(con, "update.rollback_request", target)
        return "已请求切回 %s：守护进程会在空闲时切换并重启服务，之后不再自动安装 %s，直到出现更高的版本" % (
            target, current_version())
    if action == "adopt":
        busy = busy_reasons(con)
        if busy:
            raise UpdateError("现在不空闲（%s）：重装服务会打断它们，等结束后再执行" % "；".join(busy))
        if a.undo:
            return "\n".join(undo_adopt())
        return "\n".join(adopt(workspace))
    raise UpdateError("不认识的操作：%s" % action)

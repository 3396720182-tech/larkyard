"""控制台接入（openspec add-console-enrollment，规格见 docs/spec.md §12）。

默认不启用：仓库里没有任何控制台地址，也没有配置项能设置它。只有执行过 `ticket enroll`（本人、在终端里、看过并同意
采集清单）之后，`~/.ticketd/console/` 里才有控制台地址和设备令牌；没有这两个文件时这里不发任何请求。

- 连接：所有到控制台的请求只经过 `_open()`（`http.client.HTTPSConnection` + `ssl.create_default_context()`）。
  先做完 TLS 握手（系统 CA + 主机名校验，不可关闭）再写请求，校验失败时一个请求字节都没写出，不重试、不降级。
  https 检查、响应体上限、JSON 解析和错误码都在 `_open` 之外（`request()` / `fetch_raw()`）。
- 入网：`enroll()`；退出：`leave()`。
- 心跳：`build_heartbeat()`（白名单字段，和 `ticket console show` 共用）、`send_heartbeat()`（发一次，写 kv / events）、
  `Heartbeat`（守护进程主循环里的定时器，发送在后台线程）。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from . import __version__, config, db, host, metrics, updsig

# ---------- 采集清单 ----------
CONSENT_VERSION = "c1"
# 逐字和 openspec/changes/add-console-enrollment/design.md D4 一致：UTF-8、\n 换行、每行（含最后一行）以 \n 结尾。
# 打印的就是参与哈希的这段文本；改一个字都要升版本号（控制台先登记新版本）。
CONSENT_TEXT = (
    "控制台会收集（0 级：设备状态）\n"
    "入网时一次：主机名；系统名称、版本和架构；Larkyard 版本；本机 bot 的 open_id（TICKETD_OWNER）；你同意的这份清单的版本。\n"
    "每 5 分钟一次：Larkyard 版本；系统名称、版本和架构；ticket doctor 的结论（是否通过、不通过的检查项名称）；"
    "运行中和排队中的工单数量；自动更新是否启用、更新通道、最近一次更新结果和时间、本机暂停更新的到期时间。\n"
    "不会收集：工单标题、原话、追问、产物、文件、过程日志、成本。\n"
    "谁能看：只有控制台管理员。随时查看下一次上报的内容：ticket console show。退出：ticket enroll --leave。\n"
)


def consent_sha256() -> str:
    return hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()


# ---------- 常量 ----------
TIMEOUT = 15.0
BODY_LIMIT = 64 * 1024
HEARTBEAT_EVERY = 300.0
JITTER = 30.0
NEXT_IN_MIN, NEXT_IN_MAX = 300, 570   # 控制台可以在这个范围里调慢；再加抖动，两次最长间隔 600 秒
CONNECTED_WITHIN = 1800.0             # 最近 30 分钟有成功心跳 = 已连接
EVENT_DEDUPE = 86400.0                # 同一原因的控制台事件一天只记一条
KIND = "console"                      # events.kind
STOP_CODES = ("DeviceRevoked", "DeviceLeft", "DeviceUnknown")
UPDATES_PATH = "/api/v1/updates"

HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
OS_FIELD_RE = re.compile(r"^[A-Za-z0-9._ -]{1,32}$")
OPEN_ID_RE = re.compile(r"^ou_[0-9a-f]{20,40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]{1,32})?$")
DOCTOR_ID_RE = re.compile(r"^[a-z0-9_.-]{1,40}$")
CODE_RE = re.compile(r"^LY-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")
CONFIRM_RE = re.compile(r"^[0-9]{6}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,512}$")  # 只要能原样放进 Authorization 头

# 错误码 → 中文提示（按 code 判断，不按 HTTP 状态码；401 + STOP_CODES 停止心跳）
HINTS = {
    "CodeInvalid": "入网码无效（不存在、已过期、已使用或已作废），请向管理员要一个新的",
    "CodeStartLimit": "这个入网码的提交次数已用完，请向管理员要一个新的",
    "ConsentUnknown": "控制台不认识这份采集清单的版本，请先升级 Larkyard 或联系管理员",
    "ConfirmDeliveryFailed": "确认码发不出去，请联系管理员",
    "ConfirmWrong": "确认码不对，请核对飞书私信后重新输入",
    "EnrollExpired": "这次入网已失效（超时或输错次数太多），请重新执行 ticket enroll",
    "DeviceUnknown": "控制台不认识这台设备",
    "DeviceRevoked": "设备已被控制台吊销",
    "DeviceLeft": "这台设备在控制台已标为退出",
    "UnknownField": "控制台拒收：请求里有它不认识的字段（多半是版本不匹配，请升级 Larkyard）",
    "BadValue": "控制台拒收：有字段的值不合规（检查本机时间是否准确）",
    "TooFrequent": "上报太频繁，控制台忽略了这一次，稍后自动再报",
    "Stale": "控制台已有更新的状态，这一次上报被忽略",
    "RateLimited": "请求太频繁，请过一分钟再试",
    "Unavailable": "控制台暂时不可用，请稍后再试",
    # 以下是本机判断的，不来自控制台
    "CertInvalid": "证书校验失败：控制台地址的证书不是签给这个域名的、是自签证书，或者本机 Python 找不到 CA 证书。"
                   "没有发出任何请求内容；不会改用不校验的连接",
    "NoCA": "本机 Python 找不到可用的 CA 证书，没法校验控制台的证书",
    "Network": "连不上控制台（网络不通、超时或地址不对）",
    "BadResponse": "控制台的响应格式不对",
    "HttpError": "控制台返回了意外的 HTTP 状态",
    "NotEnrolled": "本机没有入网",
}
CA_FIX = ("处理办法：python.org 的安装包先运行一次「/Applications/Python 3.x/Install Certificates.command」；"
          "Linux 装 ca-certificates；或者用 SSL_CERT_FILE 指向系统的 CA 证书文件")


class ConsoleError(Exception):
    def __init__(self, code: str, detail: str = "", status: int = 0):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(self.text())

    def text(self) -> str:
        hint = HINTS.get(self.code, "控制台返回错误")
        if self.code == "NoCA":
            hint += "。" + CA_FIX
        out = "%s（%s）" % (hint, self.code)
        if self.detail:
            out += "：%s" % self.detail
        return out


# ---------- 本机文件 ----------
def console_dir() -> Path:
    return config.home() / "console"


def info_path() -> Path:
    return console_dir() / "console.json"


def token_path() -> Path:
    return console_dir() / "device-token"


def stopped_path() -> Path:
    """心跳被拒（吊销 / 退出 / 未知设备）后写下的标记：更新源跟着失效。--leave 时随目录一起删。"""
    return console_dir() / "stopped"


def present() -> bool:
    """本机有控制台目录里的任何一个文件（含写了一半的）：算「入过网」，再入网要先 --leave。"""
    return info_path().exists() or token_path().exists()


def load() -> Optional[dict]:
    """console.json 的内容；没有入网、或文件坏了返回 None。"""
    try:
        obj = json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not _https_ok(obj.get("url")) or not DEVICE_ID_RE.match(str(obj.get("device_id"))):
        return None
    return obj


def enrolled() -> bool:
    return load() is not None and token_path().is_file()


def read_token() -> str:
    try:
        tok = token_path().read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise ConsoleError("NotEnrolled", "读不了设备令牌")
    if not TOKEN_RE.match(tok):
        raise ConsoleError("NotEnrolled", "设备令牌格式不对")
    return tok


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name("%s.tmp-%d" % (path.name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def save(info: dict, token: str) -> None:
    """目录 700、文件 600，都写临时文件再 rename；令牌先写，console.json 最后写（它在 = 入网完成）。"""
    d = console_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    _write_private(token_path(), (token + "\n").encode("ascii"))
    _write_private(info_path(), (json.dumps(info, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def remove_local() -> None:
    """删除 ~/.ticketd/console/。删不掉时抛 OSError（不能说「已删除」却还留着令牌）。"""
    shutil.rmtree(str(console_dir()), ignore_errors=True)
    if console_dir().exists():
        raise OSError("删不掉 %s" % console_dir())


def _clear_kv(con) -> None:
    con.execute("DELETE FROM kv WHERE key LIKE 'console.%'")


# ---------- 连接 ----------
def _https_ok(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        return u.scheme == "https" and bool(u.hostname) and not u.username and not u.password \
            and not u.query and not u.fragment and u.port != 0
    except ValueError:
        return False


def _ssl_context() -> ssl.SSLContext:
    """系统 CA + 主机名校验。没有任何参数能关掉它，也没有证书固定或信任自签证书的选项。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _connect(host_: str, port: int, timeout: float) -> socket.socket:
    """TCP 连接（单测在这里换成假 socket）。"""
    return socket.create_connection((host_, port), timeout=timeout)


_limits = threading.local()  # 本线程这次请求最多读多少字节（_open 的默认实现用；请求方另外再查一遍）


def _read_cap() -> int:
    return int(getattr(_limits, "cap", BODY_LIMIT))


def _open(method: str, url: str, headers: dict, body, timeout: float) -> tuple:
    """到控制台的唯一出口：返回 (状态码, 响应头（键小写）, 响应体)。只做连接和收发，不解析。

    先在 _connect 得到的 socket 上做完 TLS 握手，再交给 HTTPSConnection 写请求：证书或主机名校验失败时
    wrap_socket 就抛出，请求行、头和请求体一个字节都没写出。不跟随重定向。"""
    u = urllib.parse.urlsplit(url)
    hostname, port = u.hostname, u.port or 443
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    deadline = time.monotonic() + timeout
    ctx = _ssl_context()
    raw = _connect(hostname, port, timeout)
    # 整个请求的总时限：socket 的超时只管单次读写，控制台每隔几秒挤一个字节就能一直拖着；到点直接断开连接
    cur = {"sock": raw, "late": False}

    def _expire():
        cur["late"] = True
        try:
            socket.socket.shutdown(cur["sock"], socket.SHUT_RDWR)
        except (OSError, TypeError, ValueError):
            pass
    timer = threading.Timer(timeout, _expire)
    timer.daemon = True
    timer.start()
    try:
        try:
            sock = ctx.wrap_socket(raw, server_hostname=hostname)
        except BaseException:
            raw.close()
            raise
        cur["sock"] = sock
        conn = http.client.HTTPSConnection(hostname, port, timeout=timeout, context=ctx)
        conn.sock = sock
        try:
            return _exchange(conn, method, path, body, headers, deadline, timeout)
        except ssl.SSLError as e:
            # 握手已经完成：这是连接中途的错误，不是证书校验失败（请求内容可能已经发出）
            raise ConnectionError("TLS 连接中断：%s" % e)
        finally:
            conn.close()
    except OSError:
        if cur["late"]:
            raise socket.timeout("整个请求超过 %d 秒" % timeout)
        raise
    finally:
        timer.cancel()


def _exchange(conn, method, path, body, headers, deadline, timeout) -> tuple:
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    cap, chunks, n = _read_cap() + 1, [], 0
    while n < cap:
        if time.monotonic() > deadline:
            raise socket.timeout("读响应超过 %d 秒" % timeout)
        chunk = resp.read(min(1 << 16, cap - n))
        if not chunk:
            break
        chunks.append(chunk)
        n += len(chunk)
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, b"".join(chunks)


def ca_problem(paths=None, ca_count=None) -> Optional[str]:
    """本机 Python 找不到 CA 证书时返回提示（含处理办法），否则 None。"""
    try:
        paths = paths or ssl.get_default_verify_paths()
        if ca_count is None:
            ca_count = ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
    except Exception as e:
        return "读不出本机的 CA 证书设置（%r）。%s" % (e, CA_FIX)
    if ca_count:
        return None
    if paths.cafile and os.path.isfile(paths.cafile):
        return None
    if paths.capath and os.path.isdir(paths.capath):
        try:
            if any(os.scandir(paths.capath)):
                return None
        except OSError:
            pass
    return "%s（cafile=%s，capath=%s）。%s" % (HINTS["NoCA"], paths.cafile, paths.capath, CA_FIX)


def _transport(method: str, url: str, headers: dict, body, timeout: float, cap: int) -> tuple:
    """经 _open 发一次请求，把底层异常归成 ConsoleError。不重试。"""
    if not _https_ok(url):
        raise ConsoleError("BadResponse", "控制台地址必须是 https://")  # 调用方已查过，这里兜底
    _limits.cap = cap
    try:
        return _open(method, url, headers, body, timeout)
    except ssl.SSLCertVerificationError as e:
        if ca_problem():
            raise ConsoleError("NoCA", str(getattr(e, "verify_message", "") or e)[:200])
        raise ConsoleError("CertInvalid", str(getattr(e, "verify_message", "") or e)[:200])
    except ssl.SSLError as e:
        raise ConsoleError("CertInvalid", "TLS 握手失败：%s" % (str(e)[:200],))
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise ConsoleError("Network", repr(e)[:200])
    finally:
        _limits.cap = BODY_LIMIT


def _headers(token: Optional[str], json_body: bool) -> dict:
    h = {"Accept": "application/json", "User-Agent": "larkyard/%s" % __version__}
    if json_body:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _error_of(status: int, data: bytes) -> ConsoleError:
    try:
        err = json.loads(data.decode("utf-8")).get("error")
        code = err.get("code")
        if isinstance(code, str) and re.match(r"^[A-Za-z]{1,40}$", code):
            return ConsoleError(code, "", status)
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    return ConsoleError("HttpError", "HTTP %d" % status, status)


def request(method: str, path: str, body=None, *, base: Optional[str] = None, token: Optional[str] = None,
            timeout: float = TIMEOUT) -> dict:
    """到控制台的 JSON 请求。base 不给时用本机 console.json 里的地址。返回响应 JSON 对象；
    控制台返回错误（{"error": {"code"}}）、网络或证书问题都抛 ConsoleError。"""
    if base is None:
        info = load()
        if info is None:
            raise ConsoleError("NotEnrolled")
        base = info["url"]
    if not _https_ok(base):
        raise ConsoleError("BadResponse", "控制台地址必须是 https://")
    url = base.rstrip("/") + path
    data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, _, raw = _transport(method, url, _headers(token, data is not None), data, timeout, BODY_LIMIT)
    if len(raw) > BODY_LIMIT:
        raise ConsoleError("BadResponse", "响应超过 %d 字节" % BODY_LIMIT, status)
    if not 200 <= status < 300:
        raise _error_of(status, raw)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConsoleError("BadResponse", "不是 JSON", status)
    if not isinstance(obj, dict):
        raise ConsoleError("BadResponse", "不是 JSON 对象", status)
    return obj


def updates_base() -> Optional[str]:
    """入网后自动更新的更新源（控制台上的静态文件布局根）；没入网为 None。"""
    info = load()
    if info is None or not token_path().is_file() or stopped_path().exists():
        return None  # 被吊销 / 标为退出后，令牌已失效：自动更新也不再请求控制台
    return info["url"].rstrip("/") + UPDATES_PATH


def fetch_raw(url: str, limit: int, timeout: float) -> Optional[bytes]:
    """自动更新经控制台取清单和包：带设备令牌、同一套证书校验。404 返回 None；最多返回 limit+1 字节，
    超没超由调用方判断（和 update.fetch 的约定一样）。"""
    status, _, raw = _transport("GET", url, _headers(read_token(), False), None, timeout, limit)
    if status == 404:
        return None
    if not 200 <= status < 300:
        raise _error_of(status, raw[:BODY_LIMIT])
    return raw


# ---------- 规整（发出前按控制台的字符集） ----------
def _clean(value, allowed: str, limit: int) -> str:
    s = re.sub(r"[^%s]" % allowed, "", str(value or ""))
    return s.strip()[:limit].strip()


def clean_hostname(name) -> str:
    return _clean(name, "A-Za-z0-9._-", 63)


def os_fields() -> dict:
    raw = host.os_info()
    return {k: _clean(raw.get(k), "A-Za-z0-9._ -", 32) or "unknown" for k in ("name", "version", "arch")}


def _owner_open_id() -> str:
    return config.owner()


def _safe_text(s, limit: int = 40) -> str:
    """控制台给的、要打印到终端的短文本：去掉控制字符（防终端转义），截短。"""
    return "".join(ch for ch in str(s or "") if ch.isprintable())[:limit]


# ---------- 入网 ----------
def _in_worker() -> bool:
    return bool(os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"))


def _ask(prompt: str) -> str:
    """从终端读一行（端到端联调时替换）。"""
    return input(prompt)


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _event(con, code: str, detail: str = "", dedupe: bool = True) -> None:
    """记一条控制台事件（events.kind=console）。同一原因一天只记一条。detail 里不放令牌、确认码、入网码。"""
    try:
        if dedupe:
            row = con.execute("SELECT code, detail, created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                              (KIND,)).fetchone()
            if row and row["code"] == code and row["detail"] == detail[:300] and db.now() - row["created_at"] < EVENT_DEDUPE:
                return
        metrics.record(con, metrics.SYSTEM_TICKET, KIND, detail, code=code)
    except Exception:
        pass


def start_body(code: str, hostname: str, owner: str) -> dict:
    return {"code": code, "hostname": hostname, "os": os_fields(), "larkyard_version": __version__,
            "bot_open_id": owner, "consent": {"version": CONSENT_VERSION, "sha256": consent_sha256()}}


def _precheck(url, code, release_fp, backup_fp, hostname) -> tuple:
    """D3 第 1 步和第 4 步的本机部分：不发请求。返回 (地址, 入网码, start 请求体)。"""
    if _in_worker():
        raise PermissionError("worker 里不能执行 ticket enroll：只能由用户本人在终端执行")
    if present():
        raise ValueError("本机已经入网（%s）：要换控制台或重新入网，先执行 ticket enroll --leave" % console_dir())
    if not url or not code or not release_fp or not backup_fp:
        raise ValueError("用法：ticket enroll <控制台地址> <入网码> --release-key SHA256:<主公钥指纹> "
                         "--backup-key SHA256:<备份公钥指纹>（指纹由管理员在控制台以外的渠道给你）")
    url = url.strip().rstrip("/")
    if not _https_ok(url):
        raise ValueError("控制台地址必须是 https://<域名>（不接受 http、带用户名或参数的地址）")
    for label, fp in (("--release-key", release_fp), ("--backup-key", backup_fp)):
        if not updsig.FINGERPRINT_RE.match(fp.strip()):
            raise ValueError("%s 的格式不对，应为 SHA256:<43 位>（管理员在控制台以外给你的那串）" % label)
    code = code.strip().upper()
    if not CODE_RE.match(code):
        raise ValueError("入网码格式不对，应为 LY-XXXX-XXXX-XXXX")
    name = clean_hostname(hostname if hostname is not None else socket.gethostname())
    if not HOSTNAME_RE.match(name):
        raise ValueError("主机名只保留字母、数字和 . _ - 之后是空的：用 --hostname <名字> 指定一个")
    owner = _owner_open_id()
    if not OPEN_ID_RE.match(owner):
        raise ValueError("TICKETD_OWNER 不是有效的 open_id（ou_ 加 20～40 位小写十六进制）：检查 config.env 里的 TICKETD_OWNER")
    if not VERSION_RE.match(__version__):
        raise ValueError("Larkyard 版本号 %r 不合规，不能入网" % __version__)
    body = start_body(code, name, owner)
    for k, v in body["os"].items():
        if not OS_FIELD_RE.match(v):
            raise ValueError("系统信息 %s 规整后不合规：%r" % (k, v))
    if not _stdin_is_tty():
        raise PermissionError("ticket enroll 要在终端里由本人确认采集清单，不能在管道或脚本里执行")
    return url, code, body


def _check_keys(url: str, release_fp: str, backup_fp: str) -> tuple:
    """D3 第 3 步：取两把发版公钥，按带外指纹核对；本机已信任另一对时失败。返回 (主公钥, 备份公钥, 是否已信任同一对)。"""
    from . import update
    keys = request("GET", "/api/v1/release-keys", base=url)
    got = []
    for label, field, want in (("主公钥", "main", release_fp.strip()), ("备份公钥", "backup", backup_fp.strip())):
        try:
            k = updsig.parse_pubkey(keys.get(field) or "")
            fp = updsig.fingerprint(k)
        except (ValueError, OSError) as e:
            raise ValueError("控制台给的%s不是有效的 Ed25519 公钥（%s）。入网码没有提交，本机没有写入任何东西" % (label, e))
        if fp != want:
            raise ValueError("发版公钥指纹不一致：控制台给的%s是 %s，你给的是 %s。入网码没有提交，本机没有写入任何公钥"
                             % (label, fp, want))
        got.append(k)
    if got[0] == got[1]:
        raise ValueError("控制台给的主公钥和备份公钥是同一把，拒绝入网")
    have_main, have_backup = updsig.signer_key(update.main_signers()), updsig.signer_key(update.backup_signers())
    if (have_main and have_main != got[0]) or (have_backup and have_backup != got[1]):
        raise ValueError("本机已经信任了另一对发版公钥（ticket update status 可以看到），和控制台的不同：不覆盖，入网中止。"
                         "确认要换成控制台的公钥，先找管理员核对，再用 ticket update trust 改信任后重新入网")
    return got[0], got[1], have_main == got[0] and have_backup == got[1]


def enroll(con, url: str, code: str, release_fp: str, backup_fp: str, hostname: Optional[str] = None,
           out=print) -> list:
    """`ticket enroll`。第 1～5 步任何失败都不写任何文件；成功后先写公钥、再写控制台文件。返回最后要打印的几行。"""
    from . import update
    url, code, body = _precheck(url, code, release_fp, backup_fp, hostname)
    out("采集清单 %s（SHA-256 %s）：" % (CONSENT_VERSION, consent_sha256()))
    out(CONSENT_TEXT[:-1])  # print 补回最后的换行：终端上看到的就是参与哈希的这段文本
    answer = _ask("同意上面的清单并接入控制台 %s？输入 yes 继续：" % url)
    if answer.strip() != "yes":
        return ["没有同意，已退出：没有发出任何请求，本机没有写入任何文件"]
    main_key, backup_key, same = _check_keys(url, release_fp, backup_fp)
    try:
        st = request("POST", "/api/v1/enroll/start", body, base=url)
    except ConsoleError as e:
        raise ValueError("入网失败：%s。本机没有写入任何东西" % e.text())
    enroll_id = st.get("enroll_id")
    if not isinstance(enroll_id, str) or not TOKEN_RE.match(enroll_id):
        raise ValueError("入网失败：控制台的响应缺 enroll_id。本机没有写入任何东西")
    try:
        ttl = min(max(int(st.get("confirm_ttl") or 600), 60), 1800)
    except (TypeError, ValueError):
        ttl = 600
    out("确认码已私信给 %s，会话标记 %s，请核对私信里的标记一致。确认码只输入到这里，不要告诉任何人（包括自称管理员的人）。"
        % (_safe_text(st.get("person_hint"), 20) or "（未知）", _safe_text(st.get("marker"), 8) or "（未知）"))
    deadline = time.monotonic() + ttl
    res = None
    while res is None:
        if time.monotonic() > deadline:
            raise ValueError("确认码已过期：请重新执行 ticket enroll。本机没有写入任何东西")
        cc = _ask("确认码（6 位数字）：").strip()
        if not CONFIRM_RE.match(cc):
            out("确认码是 6 位数字，请重新输入")
            continue
        try:
            res = request("POST", "/api/v1/enroll/confirm", {"enroll_id": enroll_id, "confirm_code": cc}, base=url)
        except ConsoleError as e:
            if e.code == "ConfirmWrong":
                out(HINTS["ConfirmWrong"])
                continue
            if e.code in ("Network", "Unavailable", "RateLimited"):
                out("%s；可以重新输入确认码再试" % e.text())
                continue
            raise ValueError("入网失败：%s。本机没有写入任何东西" % e.text())
    device_id, token = res.get("device_id"), res.get("device_token")
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id) or \
            not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ValueError("入网失败：控制台的响应缺设备 id 或令牌（设备 id：%s）。请管理员在控制台吊销这台设备后重新入网"
                         % _safe_text(device_id, 64))
    now = time.time()
    try:
        if not same:
            update.trust(con, main_key, release_fp.strip(), backup_key, backup_fp.strip())
        save({"url": url, "device_id": device_id, "enrolled_at": updsig.iso(now), "consent_version": CONSENT_VERSION},
             token)
    except Exception as e:
        try:
            remove_local()
        except OSError:
            pass
        raise ValueError("控制台已登记这台设备（设备 id：%s），但本机写入失败（%r）：请管理员在控制台吊销这台设备后重新入网"
                         % (device_id, e))
    _clear_kv(con)
    _event(con, "enrolled", "已接入控制台 %s，设备 %s" % (url, device_id), dedupe=False)
    lines = ["✅ 已接入控制台 %s（设备 %s）" % (url, device_id),
             "发版公钥：%s" % ("本机原来就信任这一对，信任不变" if same else "已按你给的指纹信任主公钥和备份公钥"),
             "守护进程会在几分钟内开始每 5 分钟上报一次；上报内容：ticket console show；状态：ticket console status；"
             "退出：ticket enroll --leave"]
    if config.update_url():
        lines.append("自动更新：本机配置了 TICKETD_UPDATE_URL，更新源仍以它为准（不是控制台）")
    why = update.disabled_reasons(probe=False)
    if why:
        lines.append("自动更新：未启用：%s%s" % ("；".join(why), "" if not any("受管安装" in w for w in why)
                                                   else "。要启用，执行 ticket update adopt"))
    else:
        lines.append("自动更新：已启用（更新源 %s）" % update.source()[0])
    return lines


def leave(con, out=print) -> list:
    """`ticket enroll --leave`：先通知控制台，再删本机文件；控制台不可达时问一句。信任的发版公钥保留。"""
    if _in_worker():
        raise PermissionError("worker 里不能执行 ticket enroll --leave：只能由用户本人在终端执行")
    if not present():
        return ["本机没有入网，不用退出"]
    if not _stdin_is_tty():
        raise PermissionError("ticket enroll --leave 要在终端里由本人执行，不能在管道或脚本里执行")
    note = ""
    info = load()
    try:
        if info is None:
            raise ConsoleError("NotEnrolled", "本机的控制台文件不完整")
        request("POST", "/api/v1/device/leave", {}, token=read_token())
        note = "控制台已把这台设备标为已退出"
    except ConsoleError as e:
        if e.code == "NotEnrolled":
            # 本机文件不完整（缺地址或令牌）：本来就发不出请求，直接清理，不用再问
            note = "本机控制台文件不完整，没法通知控制台（请管理员在控制台把这台设备标为已退出或吊销）"
        elif e.code in STOP_CODES:
            note = "控制台那边这台设备已经失效（%s）" % HINTS[e.code]
        else:
            out("没能通知控制台：%s" % e.text())
            if not _stdin_is_tty() or _ask("仍然删除本机的设备令牌和控制台配置吗？输入 yes 删除：").strip() != "yes":
                return ["没有删除本机文件，心跳照常。控制台恢复后再执行 ticket enroll --leave"]
            note = "没能通知控制台（请管理员在控制台把这台设备标为已退出或吊销）"
    try:
        remove_local()
    except OSError as e:
        raise ValueError("%s，但本机的控制台文件没删掉（%s）：请手工删除 %s" % (note, e, console_dir()))
    _clear_kv(con)
    _event(con, "left", note, dedupe=False)
    return ["已退出控制台：%s；本机的 %s 已删除，不再上报" % (note, console_dir()),
            "信任的发版公钥保留（它们属于自动更新，ticket update status 可以看到）"]


# ---------- 心跳 ----------
def _iso(ts) -> Optional[str]:
    try:
        return updsig.iso(float(ts)) if ts else None
    except (TypeError, ValueError):
        return None


def _update_part(con, now: float) -> dict:
    from . import update
    try:
        enabled = not update.disabled_reasons()
    except Exception:
        enabled = False
    row = con.execute("SELECT code, created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                      (update.KIND,)).fetchone()
    last = row["code"] if row and row["code"] in update.CODES else None
    return {"enabled": enabled, "channel": config.update_channel(), "last_code": last,
            "last_at": _iso(row["created_at"]) if last else None, "hold_until": _iso(update.hold_until(con, now))}


COSTS_EVERY = 3600.0
_costs_cache = {"at": None, "bad": False}


def _costs_bad(con, now: float) -> bool:
    """成本对账要重读所有工单的过程记录：心跳里一小时最多做一次，其余时候用上次的结论。"""
    if _costs_cache["at"] is None or now - _costs_cache["at"] >= COSTS_EVERY or now < _costs_cache["at"]:
        try:
            _costs_cache["bad"] = bool(metrics.reconcile_costs(con)[1])
        except Exception:
            _costs_cache["bad"] = True
        _costs_cache["at"] = now
    return _costs_cache["bad"]


def _doctor_part(con, now: Optional[float] = None) -> dict:
    from . import cli
    now = time.time() if now is None else now
    try:
        checks = cli.doctor_checks(con, costs=False)
        if _costs_bad(con, now):
            checks.append(("costs", "❌ 成本账目和原始记录对不上"))
    except Exception:
        return {"ok": False, "failed": ["doctor"]}
    failed = []
    for cid, line in checks:
        if line.startswith("❌") and DOCTOR_ID_RE.match(cid) and cid not in failed:
            failed.append(cid)
    return {"ok": not failed, "failed": failed[:20]}


def build_heartbeat(con, now: Optional[float] = None) -> dict:
    """下一次心跳的完整内容。只有这些字段（白名单，和采集清单 c1 对应）；守护进程和 ticket console show 都用它。"""
    now = time.time() if now is None else now

    def count(state):
        n = con.execute("SELECT COUNT(*) FROM tickets WHERE state=?", (state,)).fetchone()[0]
        return min(max(int(n), 0), 10000)
    return {
        "schema": 1,
        "sent_at": updsig.iso(now),
        "larkyard_version": __version__,
        "os": os_fields(),
        "doctor": _doctor_part(con, now),
        "tickets": {"running": count("running"), "queued": count("queued")},
        "update": _update_part(con, now),
    }


def state(con) -> str:
    return metrics.kv_get(con, "console.state") or ""


def last_ok_at(con) -> float:
    try:
        return float(metrics.kv_get(con, "console.last_ok_at") or 0)
    except ValueError:
        return 0.0


def send_heartbeat(con, now: Optional[float] = None) -> dict:
    """发一次心跳，结果写 kv（console.last_ok_at / console.last_error / console.state）和 events。
    返回摘要：{"sent": 是否发出了请求, "ok": 是否成功, "code": 错误码, "next_in": 控制台要求的间隔（范围外为 None）,
    "stopped": 是否已停止上报}。只读响应里的 ok 和 next_in。"""
    now = time.time() if now is None else now
    if not enrolled():
        return {"sent": False, "ok": False, "code": "NotEnrolled", "next_in": None, "stopped": False}
    st = state(con)
    if st.startswith("stopped:"):
        return {"sent": False, "ok": False, "code": st.split(":", 1)[1], "next_in": None, "stopped": True}
    try:
        token = read_token()
        payload = build_heartbeat(con, now)
        resp = request("POST", "/api/v1/heartbeat", payload, token=token)
    except ConsoleError as e:
        if not enrolled():  # 发送期间执行了 --leave：什么都不记
            return {"sent": True, "ok": False, "code": e.code, "next_in": None, "stopped": True}
        metrics.kv_set(con, "console.last_error", "%s %s" % (updsig.iso(now), e.text()[:300]))
        if e.status == 401 and e.code in STOP_CODES:
            metrics.kv_set(con, "console.state", "stopped:" + e.code)
            try:
                _write_private(stopped_path(), (e.code + "\n").encode("ascii"))
            except OSError:
                pass
            _event(con, "stopped", "%s，停止上报" % HINTS[e.code])
            return {"sent": True, "ok": False, "code": e.code, "next_in": None, "stopped": True}
        if st != "failing":
            _event(con, "cert_failed" if e.code in ("CertInvalid", "NoCA") else "failing", e.text()[:200])
        metrics.kv_set(con, "console.state", "failing")
        return {"sent": True, "ok": False, "code": e.code, "next_in": None, "stopped": False}
    if not enrolled():
        return {"sent": True, "ok": False, "code": "NotEnrolled", "next_in": None, "stopped": True}
    ok = resp.get("ok") is True
    n = resp.get("next_in")
    next_in = n if isinstance(n, int) and not isinstance(n, bool) and NEXT_IN_MIN <= n <= NEXT_IN_MAX else None
    if ok:
        metrics.kv_set(con, "console.last_ok_at", str(now))
        if st != "ok":
            _event(con, "recovered" if st == "failing" else "started",
                    "恢复上报" if st == "failing" else "开始上报", dedupe=False)
        metrics.kv_set(con, "console.state", "ok")
    else:
        metrics.kv_set(con, "console.last_error", "%s 控制台没有确认这次上报" % updsig.iso(now))
    return {"sent": True, "ok": ok, "code": None, "next_in": next_in, "stopped": False}


class Heartbeat:
    """守护进程主循环每轮调用 tick()。没入网时只看一眼本机文件，不发请求；到期就在后台线程发一次（超时 15 秒）。"""

    def __init__(self, clock=time.time, rand=random.uniform, sender=None):
        self.clock = clock
        self.rand = rand
        self.sender = sender or self._send
        self.next_at = None
        self.thread = None
        self.last = None

    def _send(self) -> dict:
        con = db.connect()
        try:
            return send_heartbeat(con)
        finally:
            con.close()

    def _run(self, launched: float) -> None:
        try:
            self.last = self.sender()
        except Exception as e:
            self.last = {"sent": False, "ok": False, "code": "Error", "next_in": None, "stopped": False, "error": repr(e)}
        if self.last.get("next_in"):
            self.next_at = launched + self.last["next_in"] + self.rand(0, JITTER)

    def tick(self) -> bool:
        """返回这一轮是否启动了一次发送。"""
        now = self.clock()
        if not enrolled():
            self.next_at = None
            return False
        if self.next_at is None:
            self.next_at = now + self.rand(0, JITTER)  # 刚启动或刚入网：很快报第一次
            return False
        if now < self.next_at or (self.thread is not None and self.thread.is_alive()):
            return False
        self.next_at = now + HEARTBEAT_EVERY + self.rand(0, JITTER)
        self.thread = threading.Thread(target=self._run, args=(now,), name="ticketd-console", daemon=True)
        self.thread.start()
        return True


# ---------- 显示 ----------
def _ts(t) -> str:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"


def connected(con, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return now - last_ok_at(con) <= CONNECTED_WITHIN


def badge(con, now: Optional[float] = None) -> Optional[str]:
    """状态卡 / 结果卡详情里的内部版标识；没入网为 None。"""
    try:
        if not enrolled():
            return None
        return "内部版 v%s · %s" % (__version__, "已连接控制台" if connected(con, now) else "控制台未连接")
    except Exception:
        return None


def status_lines(con, now: Optional[float] = None) -> list:
    now = time.time() if now is None else now
    if not present():
        return ["控制台：未入网（不发出任何到控制台的请求）"]
    info = load()
    if info is None or not token_path().is_file():
        return ["控制台：本机控制台文件不完整（%s）：执行 ticket enroll --leave 清理后重新入网" % console_dir()]
    st = state(con)
    if st.startswith("stopped:"):
        head = "已停止上报：%s" % HINTS.get(st.split(":", 1)[1], st)
    elif connected(con, now):
        head = "已入网，正常上报"
    elif st == "failing":
        head = "已入网，最近上报失败"
    else:
        head = "已入网，还没有成功上报"
    return ["控制台：%s" % head,
            "设备 id：%s" % info["device_id"],
            "控制台地址：%s" % info["url"],
            "入网时间：%s  同意的采集清单：%s" % (info.get("enrolled_at") or "—", info.get("consent_version") or "—"),
            "最后一次成功上报：%s" % _ts(last_ok_at(con)),
            "最近一次错误：%s" % (metrics.kv_get(con, "console.last_error") or "—")]


def show_text(con) -> str:
    payload = json.dumps(build_heartbeat(con), ensure_ascii=False, indent=2)
    info = load()
    if info is None:
        return "未入网：不会发出任何内容。入网后每 5 分钟上报的就是下面这份（现在不会发出）：\n" + payload
    return "下一次心跳发往 %s/api/v1/heartbeat，内容：\n%s" % (info["url"], payload)


def doctor_line(con, now: Optional[float] = None) -> str:
    """ticket doctor 的控制台一项。未入网不算不通过。"""
    now = time.time() if now is None else now
    ca = ca_problem()
    if not present():
        return "ℹ️ 控制台：未入网" + ("（另外：%s）" % ca if ca else "")
    info = load()
    if info is None or not token_path().is_file():
        return "❌ 控制台：本机控制台文件不完整（%s）：执行 ticket enroll --leave 清理后重新入网" % console_dir()
    suffix = "；更新源不是控制台（本机配置了 TICKETD_UPDATE_URL）" if config.update_url() else ""
    st = state(con)
    if st.startswith("stopped:"):
        code = st.split(":", 1)[1]
        return ("❌ 控制台：%s，已停止上报（派单不受影响）。处理：向管理员要新的入网码，先 ticket enroll --leave 再重新入网；"
                "不再使用就只执行 ticket enroll --leave 清理%s" % (HINTS.get(code, code), suffix))
    if ca:
        return "❌ 控制台：%s%s" % (ca, suffix)
    if connected(con, now):
        return "✅ 控制台：已连接 %s（最后一次上报 %s）%s" % (info["url"], _ts(last_ok_at(con)), suffix)
    try:
        since = updsig.parse_iso(info.get("enrolled_at"))
    except ValueError:
        since = 0.0
    err = metrics.kv_get(con, "console.last_error")
    if not last_ok_at(con) and not err and now - since <= CONNECTED_WITHIN:
        return "⚠️ 控制台：刚入网，还没有上报（守护进程在运行的话几分钟内会报）%s" % suffix
    return "❌ 控制台：最近 30 分钟没有成功上报（最近的错误：%s）%s" % (err or "无，守护进程可能没在运行", suffix)


def command_console(con, action: str) -> str:
    if action == "show":
        return show_text(con)
    return "\n".join(status_lines(con))

"""匿名使用统计（spec §7.4，docs/telemetry.md）。默认关闭，`ticket telemetry on` 才开启。

- 每天一份前一天的汇总，只有计数、区间和写死的枚举值；字段白名单在 telemetry_schema.json（后端用同一份校验）。
- 只读数字和枚举列：rounds、tickets.executor / created_at、reports.status、events.kind / code / seconds / size、
  grants / net_grants 的时间和种类。绝不读标题、原话、汇报摘要、events.detail、outbox.last_error、路径。
- 报告先写到 ~/.ticketd/telemetry/pending/<day>.json，发送前再校验一遍；发出后移到 sent/。
- 发送在单独线程里（urllib，10 秒超时），不挡主循环；线程不碰数据库，结果写 telemetry/last.json。
"""
from __future__ import annotations

import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # 非 POSIX 平台；ticketd 只支持 macOS / Linux / WSL，这里只为了能导入
    fcntl = None

from . import __version__, config, db, host

SCHEMA_PATH = Path(__file__).resolve().parent / "telemetry_schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VERSION = SCHEMA["schema"]  # 当前代码支持的报告格式版本
# 后端部署后填上（第一期是维护者 Cloudflare 账号下的 workers.dev 地址）；空 = 只在本机生成报告、不发送
DEFAULT_URL = ""
PREVIEW_ID = "00000000-0000-4000-8000-000000000000"  # 关闭状态下 show 用的占位 ID（不是真实 ID）

KEEP_PENDING_DAYS = 7    # 没发出去的报告最多留这么多天
KEEP_SENT = 90           # sent/ 最多留这么多份
MAX_TRIES_PER_DAY = 6    # 每天最多尝试发送这么多次
RETRY_EVERY = 1800.0     # 两次尝试之间至少隔这么久（秒）
MAX_JITTER = 6 * 3600    # 每天 00:00 之后再随机延迟 0～6 小时才发，避免所有安装同一时刻打后端
POST_TIMEOUT = 10.0

K_CONSENT, K_ID, K_SINCE = "telemetry_consent", "telemetry_id", "telemetry_since"
K_DUE, K_TRIES, K_NEXT = "telemetry_due", "telemetry_tries", "telemetry_next_try"


# ---------- 开关 ----------
def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def forced_off() -> Optional[str]:
    """DO_NOT_TRACK=1 或 TICKETD_TELEMETRY=0（环境变量或 config.env）时返回原因，否则 None。优先级最高。"""
    if _truthy(config._env("DO_NOT_TRACK", "")):
        return "DO_NOT_TRACK"
    if config._env("TICKETD_TELEMETRY", "").strip().lower() in ("0", "false", "no", "off"):
        return "TICKETD_TELEMETRY=0"
    return None


def url() -> str:
    return config._env("TICKETD_TELEMETRY_URL", DEFAULT_URL).strip()


def _kv(con, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _kv_set(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, value))


def consented(con) -> int:
    """用户同意的报告格式版本；0 = 没开启。"""
    try:
        return int(_kv(con, K_CONSENT) or 0)
    except ValueError:
        return 0


def install_id(con) -> Optional[str]:
    v = _kv(con, K_ID)
    try:
        return str(uuid.UUID(v)) if v else None
    except ValueError:
        return None


def enabled(con) -> bool:
    return forced_off() is None and consented(con) > 0 and install_id(con) is not None


def _in_worker() -> bool:
    return bool(os.environ.get("TICKETD_TICKET") or os.environ.get("TICKETD_SPOOL"))


def turn_on(con, now: Optional[float] = None) -> str:
    now = db.now() if now is None else now
    why = forced_off()
    if why:
        raise PermissionError("%s 强制关闭了匿名统计，`ticket telemetry on` 不生效（去掉这个设置后再开）" % why)
    with db.tx(con):
        if install_id(con) is None:
            _kv_set(con, K_ID, str(uuid.uuid4()))
        if consented(con) == 0:
            # 从开启的第二天起算：开启当天有一部分是开启之前的数据，不上报
            _kv_set(con, K_SINCE, _day(_day_bounds(_day(now))[1] + 3600))
        _kv_set(con, K_CONSENT, str(VERSION))
    return install_id(con)


@contextmanager
def _send_lock():
    """「检查同意 → 发请求」「检查同意 → 写待发报告」和关闭共用的跨进程锁（flock，按打开的文件算，
    同一进程里的不同线程也互斥）。发送方拿着锁核对、发请求、搬文件；关闭要等它放锁才删同意记录。
    所以 `ticket telemetry off` 返回之后，不会再有请求发出，也不会再写出待发报告。
    持锁最长约一次 POST 的超时（POST_TIMEOUT，10 秒）。"""
    path = config.home() / "telemetry.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(str(path), "a")
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        f.close()  # 关闭文件即放锁


def turn_off(con) -> int:
    """关闭：删除匿名 ID、同意记录和没发出的报告（已发副本留在本机）。返回删了几份待发报告。
    正在发送的请求会先发完（最多等一次 POST 的超时），返回之后不会再发。
    先删同意记录、再等锁：flock 不保证先来先得，发送方放锁后可能马上又抢到锁去发下一份。
    同意记录先没了，发送方下一次拿到锁核对时就会停，off 最多只等在途的那一次请求。"""
    with db.tx(con):
        con.execute("DELETE FROM kv WHERE key LIKE 'telemetry\\_%' ESCAPE '\\'")
    with _send_lock():
        n = 0
        for p in _pending_dir().glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


# ---------- 目录 ----------
def base_dir() -> Path:
    return config.home() / "telemetry"


def _pending_dir() -> Path:
    return base_dir() / "pending"


def _sent_dir() -> Path:
    return base_dir() / "sent"


def _last_path() -> Path:
    return base_dir() / "last.json"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


# ---------- 区间与枚举 ----------
def _bucket(x: float, edges, labels) -> str:
    """edges 是升序的上界（不含），labels 比 edges 多一个（最后一个是「超过最大上界」）。"""
    for e, label in zip(edges, labels):
        if x < e:
            return label
    return labels[-1]


ROUND_MINUTES = ((2, 10, 30, 60, 120), ("<2", "2-10", "10-30", "30-60", "60-120", ">120"))
ROUND_COST = ((0.1, 1, 5, 20), ("<0.1", "0.1-1", "1-5", "5-20", ">20"))
DAY_COST = ((1, 5, 20, 50), ("<1", "1-5", "5-20", "20-50", ">50"))
PER_TICKET = ((2, 4, 11), ("1", "2-3", "4-10", ">10"))
CTX = ((50000, 150000, 400000), ("<50k", "50-150k", "150-400k", ">400k"))
RESPONSE = ((10, 60, 240), ("<10", "10-60", "60-240", ">240"))
MEDIA_SECONDS = ((5, 30, 120), tuple(SCHEMA["media"]["seconds"]))
MEDIA_MB = ((1, 10, 100), tuple(SCHEMA["media"]["size_mb"]))


def _cost_bucket(c: float, spec) -> str:
    return "0" if c <= 0 else _bucket(c, *spec)


def _keys(name: str) -> list:
    return SCHEMA["fields"][name]["keys"]


def _enum(value, allowed, fallback: str = "other") -> str:
    """不在白名单里的值一律记成 fallback，绝不把字符串原样带出去。"""
    return value if isinstance(value, str) and value in allowed else fallback


def _add(d: dict, key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


# ---------- 环境 ----------
def _os_name() -> str:
    s = host.system()
    if s == "darwin":
        return "macos"
    if s == "linux":
        try:
            if "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower():
                return "wsl"
        except OSError:
            pass
        return "linux"
    return "other"


def _os_major() -> Optional[int]:
    rel = platform.mac_ver()[0] if host.system() == "darwin" else platform.release()
    m = re.match(r"(\d+)", rel or "")
    return min(int(m.group(1)), 999) if m else None


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return "other"


_tool_cache: dict = {}


def _tool_version(binary: str) -> str:
    """某个命令行工具的版本，只到次版本（如 2.1）；没装或看不出来为空。每个进程每 6 小时最多问一次。"""
    hit = _tool_cache.get(binary)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    out = ""
    exe = shutil.which(binary)
    if exe:
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL, errors="replace")
            m = re.search(r"\b(\d{1,3})\.(\d{1,4})(?:\.\d+)?\b", p.stdout or "")
            if p.returncode == 0 and m:
                out = "%s.%s" % (m.group(1), m.group(2))
        except (OSError, subprocess.SubprocessError):
            pass
    _tool_cache[binary] = (time.monotonic(), out)
    return out


def _version() -> str:
    return __version__ if re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", __version__ or "") else "dev"


def _config_flags() -> dict:
    try:
        workers = config.max_workers()
    except ValueError:
        workers = 3
    return {
        "max_workers": max(1, min(8, workers)),
        "digest_on": bool(config.digest()),
        "compact_on": config.compact_tokens() > 0,
        "video_folder_set": bool(config.video_folder()),
        "worker_env_set": bool(config.worker_env_names()),
        "model_set": bool(config.model() or config.codex_model()),
    }


# ---------- 生成 ----------
def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_bounds(day: str):
    y, m, d = (int(x) for x in day.split("-"))
    start = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    end = time.mktime((y, m, d + 1, 0, 0, 0, 0, 0, -1))  # mktime 会把 32 号进位到下个月
    return start, end


def _concurrency(rows, start: float, end: float) -> int:
    marks = []
    for r in rows:
        a, b = max(r["started_at"], start), min(r["ended_at"], end)
        if b > a:
            marks += [(a, 1), (b, -1)]
    cur = best = 0
    for _, step in sorted(marks, key=lambda x: (x[0], x[1])):  # 同一时刻先结束后开始
        cur += step
        best = max(best, cur)
    return best


def build(con, day: str, iid: str) -> dict:
    """生成某一天（本机时间 00:00～24:00）的报告。只读数字和枚举列。"""
    start, end = _day_bounds(day)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    rep = {
        "schema": VERSION, "install_id": iid, "day": day, "version": _version(),
        "os": _os_name(), "arch": _arch(), "python": "%d.%d" % sys.version_info[:2],
        "claude_code": _tool_version(config.claude_bin()), "codex": _tool_version(config.codex_bin()),
        "config": _config_flags(),
    }
    major = _os_major()
    if major is not None:
        rep["os_major"] = major

    tickets = q("SELECT executor FROM tickets WHERE created_at>=? AND created_at<?", start, end)
    rep["tickets_new"] = len(tickets)
    by_exec: dict = {}
    for t in tickets:
        _add(by_exec, _enum(t["executor"], _keys("tickets_by_executor")))
    rep["tickets_by_executor"] = by_exec

    rounds = q("SELECT r.ticket_id, r.started_at, r.ended_at, r.cost_usd, r.outcome, r.ctx_tokens, t.executor"
               " FROM rounds r LEFT JOIN tickets t ON t.id=r.ticket_id"
               " WHERE r.ended_at>=? AND r.ended_at<? AND r.outcome<>'backfill'", start, end)
    rep["rounds"] = len(rounds)
    outcomes, minutes, costs, ctx = {}, {}, {}, {}
    total = 0.0
    for r in rounds:
        _add(outcomes, _enum(r["outcome"], _keys("round_outcomes")))
        _add(minutes, _bucket(max(0.0, r["ended_at"] - r["started_at"]) / 60, *ROUND_MINUTES))
        cost = float(r["cost_usd"] or 0)
        total += max(0.0, cost)
        if (r["executor"] or "claude") == "claude":
            _add(costs, _cost_bucket(cost, ROUND_COST))
        if r["ctx_tokens"] and r["outcome"] not in ("compact", "compact_failed"):
            _add(ctx, _bucket(r["ctx_tokens"], *CTX))
    rep.update(round_outcomes=outcomes, round_minutes=minutes, round_cost_usd=costs, ctx_tokens=ctx)
    if rounds:
        rep["day_cost_usd"] = _cost_bucket(total, DAY_COST)
        per = {}
        for tid in sorted({r["ticket_id"] for r in rounds}):
            n = con.execute("SELECT COUNT(*) FROM rounds WHERE ticket_id=? AND ended_at<? AND outcome NOT IN"
                            " ('backfill','compact','compact_failed')", (tid, end)).fetchone()[0]
            if n:
                _add(per, _bucket(n, *PER_TICKET))
        rep["rounds_per_ticket"] = per
    span = q("SELECT ticket_id, started_at, ended_at FROM rounds WHERE ended_at>? AND started_at<?"
             " AND outcome<>'backfill'", start, end)
    rep["concurrency_max"] = _concurrency(span, start, end)

    from . import metrics  # 避免模块加载时的循环导入
    events, model, lark, delivery, media = {}, {}, {}, {}, {}
    for e in q("SELECT kind, code, seconds, size FROM events WHERE created_at>=? AND created_at<?", start, end):
        kind, code = e["kind"], e["code"] or ""
        if kind == "model_error":
            _add(model, _enum(code, _keys("model_errors")))
        elif kind == "lark_error":
            _add(lark, _enum(code, _keys("lark_errors")))
        elif kind == "delivery":
            if code in _keys("delivery"):
                _add(delivery, code)
        elif kind == "media":
            kind_, _, res = code.partition(":")
            if kind_ in ("image", "video", "doc") and res in ("ok", "fail"):
                m = media.setdefault(kind_, {})
                _add(m, res)
                if e["seconds"] is not None:
                    _add(m.setdefault("seconds", {}), _bucket(max(0.0, e["seconds"]), *MEDIA_SECONDS))
                if e["size"] is not None:
                    _add(m.setdefault("size_mb", {}), _bucket(max(0, e["size"]) / 1e6, *MEDIA_MB))
        elif kind not in metrics.DETAIL_KINDS:
            _add(events, _enum(kind, _keys("events")))
    rep.update(events=events, model_errors=model, lark_errors=lark, delivery=delivery, media=media)

    needs = {}
    for r in q("SELECT status, ask FROM reports WHERE created_at>=? AND created_at<? AND status<>'progress'", start, end):
        if r["status"] in ("blocked", "waiting"):
            _add(needs, r["status"])
        if r["ask"]:
            _add(needs, "ask")
    rep["needs_user"] = needs
    grants = {}
    fs = con.execute("SELECT COUNT(*) FROM grants g JOIN tickets t ON t.id=g.ticket_id WHERE g.created_at>=?"
                     " AND g.created_at<? AND g.created_at>t.created_at", (start, end)).fetchone()[0]
    if fs:
        grants["fs"] = fs
    for r in q("SELECT kind, COUNT(*) n FROM net_grants WHERE created_at>=? AND created_at<? GROUP BY kind", start, end):
        if r["kind"] in ("local", "socket"):
            grants["local_net" if r["kind"] == "local" else "socket"] = r["n"]
    rep["grants"] = grants
    resp = {}
    for m in metrics._response_minutes(con, start, end):
        _add(resp, _bucket(m, *RESPONSE))
    rep["response_minutes"] = resp
    return _prune(rep)


def _prune(obj):
    """去掉为 0 的计数和空对象（「某类数据为 0 就不写」）。头部字段保留。"""
    keep = {"schema", "install_id", "day", "version", "os", "arch", "python", "claude_code", "codex", "config"}
    out = {}
    for k, v in obj.items():
        if isinstance(v, dict) and k != "config":
            v = _prune_counts(v)
            if not v:
                continue
        elif k not in keep and v == 0:
            continue
        out[k] = v
    return out


def _prune_counts(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = _prune_counts(v)
            if v:
                out[k] = v
        elif v:
            out[k] = v
    return out


# ---------- 校验 ----------
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Invalid(ValueError):
    pass


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_counts(path: str, v, keys) -> None:
    if not isinstance(v, dict):
        raise Invalid("%s 不是对象" % path)
    for k, n in v.items():
        if k not in keys:
            raise Invalid("%s 有白名单以外的键" % path)
        if not _is_int(n) or n < 0 or n > SCHEMA["max_count"]:
            raise Invalid("%s.%s 不是合法计数" % (path, k))


def _check_field(path: str, spec: dict, v) -> None:
    t = spec["type"]
    if t == "const":
        if not _is_int(v) or v != spec["value"]:
            raise Invalid("%s 取值不对" % path)
    elif t == "uuid":
        if not isinstance(v, str) or not UUID_RE.fullmatch(v):
            raise Invalid("%s 不是 UUIDv4" % path)
    elif t == "day":
        if not isinstance(v, str) or not DAY_RE.fullmatch(v):
            raise Invalid("%s 不是日期" % path)
    elif t == "pattern":
        if not isinstance(v, str) or not re.fullmatch(spec["pattern"], v):  # match 的 $ 会放过结尾的换行
            raise Invalid("%s 格式不对" % path)
    elif t == "enum":
        if not isinstance(v, str) or v not in spec["values"]:
            raise Invalid("%s 不在枚举里" % path)
    elif t == "int":
        if not _is_int(v) or v < spec["min"] or v > spec["max"]:
            raise Invalid("%s 不是合法整数" % path)
    elif t == "bool":
        if not isinstance(v, bool):
            raise Invalid("%s 不是布尔值" % path)
    elif t == "counts":
        _check_counts(path, v, spec["keys"])
    elif t == "object":
        _check_object(path, spec["fields"], v)
    elif t == "media":
        if not isinstance(v, dict):
            raise Invalid("%s 不是对象" % path)
        for k, x in v.items():
            if k in ("ok", "fail"):
                if not _is_int(x) or x < 0 or x > SCHEMA["max_count"]:
                    raise Invalid("%s.%s 不是合法计数" % (path, k))
            elif k in ("seconds", "size_mb"):
                _check_counts("%s.%s" % (path, k), x, SCHEMA["media"][k])
            else:
                raise Invalid("%s 有白名单以外的键" % path)
    else:
        raise Invalid("未知类型 %s" % t)


def _check_object(path: str, fields: dict, v) -> None:
    if not isinstance(v, dict):
        raise Invalid("%s 不是对象" % path)
    for k in v:
        if k not in fields:
            raise Invalid("%s 有白名单以外的键" % (path or "报告"))
    for k, spec in fields.items():
        if k in v:
            _check_field((path + "." if path else "") + k, spec, v[k])
        elif spec.get("required"):
            raise Invalid("缺少 %s" % k)


def validate(report) -> None:
    """严格按白名单校验：多一个键、类型不对、字符串不在枚举里都抛 Invalid（只说哪个字段，不回显内容）。"""
    _check_object("", SCHEMA["fields"], report)
    if len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > SCHEMA["max_bytes"]:
        raise Invalid("报告超过 %d 字节" % SCHEMA["max_bytes"])


# ---------- 发送 ----------
def _post(target: str, body: bytes):
    """返回 (结果, HTTP 状态码或 None)。结果：ok / rejected（后端明确拒收，不再重试）/ retry。"""
    req = urllib.request.Request(target, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "larkyard"})
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            return "ok", resp.status
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code >= 500 or e.code == 407:
            return "retry", e.code
        return "rejected", e.code
    except (urllib.error.URLError, OSError, ValueError):
        return "retry", None


_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def send_pending(target: str, poster=None, current=None) -> dict:
    """把 pending/ 里的报告按日期依次发出。遇到需要重试的失败就停。返回写进 last.json 的结果。
    current：返回「现在有效的 install_id」的函数（关闭了返回 None）。每发一份前都问一次：
    报告里的 ID 不是现在的 ID（中途关闭过、或关了又开换了 ID），这份是上一轮授权留下的，删掉不发。
    核对和发请求在同一段 _send_lock 里：关闭要等这段结束，核对通过后不会被关闭插进来。"""
    poster = poster or _post
    result = {"at": time.time(), "sent": [], "result": "nothing"}
    for p in sorted(_pending_dir().glob("*.json")):
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
            validate(rep)
        except (OSError, ValueError) as e:
            _drop(p, "校验没通过，已丢弃：%s" % e)
            result.update(result="invalid", day=p.stem)
            continue
        body = json.dumps(rep, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with _send_lock():
            if current is not None:
                now_id = current()
                if now_id is None:
                    result.update(result="stopped")  # 已经关闭：turn_off 会删掉待发报告，这里不再发
                    break
                if rep.get("install_id") != now_id:
                    _unlink(p)
                    continue
            if not p.exists():  # 等锁期间被关闭删掉了
                continue
            res, status = poster(target, body)
            result.update(day=p.stem, result=res, http=status)
            if res == "ok":
                _sent_dir().mkdir(parents=True, exist_ok=True)
                os.replace(str(p), str(_sent_dir() / p.name))
                result["sent"].append(p.stem)
            elif res == "rejected":
                _drop(p, "后端拒收（HTTP %s），已丢弃" % status)
        if res not in ("ok", "rejected"):
            break
    _prune_sent()
    _write_json(_last_path(), result)
    return result


def _drop(p: Path, why: str) -> None:
    try:
        p.unlink()
    except OSError:
        pass
    try:
        from . import outbox
        outbox.alert("匿名统计报告 %s %s" % (p.stem, why))
    except Exception:
        pass


def _prune_sent() -> None:
    files = sorted(_sent_dir().glob("*.json"))
    for p in files[:-KEEP_SENT] if len(files) > KEEP_SENT else []:
        try:
            p.unlink()
        except OSError:
            pass


def _due_at(con, today: str, now: float) -> float:
    """今天什么时候开始发：00:00 加 0～6 小时随机延迟（每天抽一次，记在 kv 里，跨重启不变）。"""
    raw = _kv(con, K_DUE) or ""
    day, _, ts = raw.partition("|")
    if day == today:
        try:
            return float(ts)
        except ValueError:
            pass
    due = float(round(_day_bounds(today)[0] + random.uniform(0, MAX_JITTER)))
    _kv_set(con, K_DUE, "%s|%.0f" % (today, due))
    return due


def _active_id(con) -> Optional[str]:
    """现在有效的 install_id；关闭了（或被强制关闭）返回 None。"""
    return install_id(con) if enabled(con) else None


def generate_missing(con, now: float) -> list:
    """给最近 7 天里开启过、还没生成过的日子生成报告（到昨天为止），写进 pending/。返回生成了哪些天。
    生成一份要探 claude/codex 版本（几秒），期间用户可能执行了 off（甚至又 on 换了新 ID）：
    每份写入前、写入后都核对 ID 还是开始时那个，不是就停，刚写的那份删掉。"""
    iid = _active_id(con)
    if iid is None:
        return []
    since = _kv(con, K_SINCE) or _day(now)
    made = []
    for back in range(KEEP_PENDING_DAYS, 0, -1):
        day = _day(now - back * 86400)
        if day < since or day >= _day(now):
            continue
        if (_pending_dir() / (day + ".json")).exists() or (_sent_dir() / (day + ".json")).exists():
            continue
        done = _kv(con, "telemetry_built") or ""
        if day <= done:
            continue
        rep = build(con, day, iid)
        if _active_id(con) != iid:
            break
        path = _pending_dir() / (day + ".json")
        try:
            validate(rep)
        except Invalid as e:
            _drop(path, "生成后校验没通过，没有发送：%s" % e)
        else:
            with _send_lock():  # 核对和写入在同一段锁里：off 要么在前（这里看到已关闭），要么在后（它删掉这份）
                if _active_id(con) != iid:
                    break
                _write_json(path, rep)
            made.append(day)
        _kv_set(con, "telemetry_built", day)
    return made


def _expire_pending(now: float) -> None:
    oldest = _day(now - KEEP_PENDING_DAYS * 86400)
    for p in _pending_dir().glob("*.json"):
        if p.stem < oldest:
            try:
                p.unlink()
            except OSError:
                pass


def _db_file(con) -> Optional[str]:
    try:
        for row in con.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return row[2]
    except Exception:
        pass
    return None


def _run_once(con, now: float, poster=None) -> None:
    """后台线程里做的事：生成报告（会跑 claude/codex --version，最多各 5 秒）、清过期、按节流规则发送。
    用的是这个线程自己的数据库连接。"""
    today = _day(now)
    if not enabled(con):  # 主循环起线程之后用户刚好执行了 off
        return
    generate_missing(con, now)
    if not enabled(con):  # 生成期间关闭了：不再写 kv，免得关闭后留下 telemetry_* 记录
        return
    _expire_pending(now)
    if not any(_pending_dir().glob("*.json")):
        _kv_set(con, K_NEXT, str(_day_bounds(today)[1]))  # 今天没有要发的了
        return
    target = url()
    if not target:
        _kv_set(con, K_NEXT, str(now + RETRY_EVERY))
        return
    day, _, n = (_kv(con, K_TRIES) or "").partition("|")
    tries = int(n) if day == today and n.isdigit() else 0
    if tries >= MAX_TRIES_PER_DAY:
        _kv_set(con, K_NEXT, str(_day_bounds(today)[1]))
        return
    if not enabled(con):
        return
    _kv_set(con, K_TRIES, "%s|%d" % (today, tries + 1))
    _kv_set(con, K_NEXT, str(now + RETRY_EVERY))
    send_pending(target, poster, current=lambda: _active_id(con))
    if enabled(con) and not any(_pending_dir().glob("*.json")):
        _kv_set(con, K_NEXT, str(_day_bounds(today)[1]))  # 都发完了，明天再来


def maybe_send(con, now: Optional[float] = None, poster=None, wait: bool = False) -> bool:
    """守护进程主循环每轮调用。主循环里只读几个 kv（开没开、到没到点、上次试过没有）；
    到点了就起一个后台线程，生成昨天（及最近 7 天漏掉的）报告并发送。生成要探 claude/codex 版本，
    发送要走网络，都不在主循环里做。返回这次是否起了后台线程（wait=True 只给测试用）。"""
    global _thread
    if not enabled(con) or _truthy(os.environ.get("CI")):
        return False
    now = db.now() if now is None else now
    today = _day(now)
    if now < _due_at(con, today, now):
        return False
    try:
        nxt = float(_kv(con, K_NEXT) or 0)
    except ValueError:
        nxt = 0.0
    if now < nxt:
        return False
    path = _db_file(con)
    if not path:
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False

        def run():
            tcon = None
            try:
                tcon = db.connect(Path(path))  # sqlite 连接不能跨线程用：在本线程里建
                _run_once(tcon, now, poster)
            except Exception as e:  # 失败不告警、不私信，只在 status 里显示
                try:
                    _write_json(_last_path(), {"at": time.time(), "result": "error", "error": type(e).__name__})
                except Exception:
                    pass
                if tcon is not None:
                    try:  # 别让同一个错误每轮都起一个线程
                        if enabled(tcon):
                            _kv_set(tcon, K_NEXT, str(now + RETRY_EVERY))
                    except Exception:
                        pass
            finally:
                if tcon is not None:
                    tcon.close()

        _thread = threading.Thread(target=run, name="ticketd-telemetry", daemon=True)
        _thread.start()
        th = _thread
    if wait:
        th.join(30)
    return True


# ---------- 命令 ----------
def _last() -> Optional[dict]:
    try:
        return json.loads(_last_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status_lines(con) -> list:
    why = forced_off()
    c = consented(con)
    if why:
        head = "匿名统计：关闭（被 %s 强制关闭）" % why
    elif c:
        head = "匿名统计：开启（同意的报告格式 v%d）" % c
    else:
        head = "匿名统计：关闭（默认）。开启：ticket telemetry on；先看会发什么：ticket telemetry show"
    lines = [head]
    if c and not why:
        lines.append("匿名 ID：%s（申请删除已上报的数据时用；关闭时删除，再开启会换新的）" % install_id(con))
        if c < VERSION:
            lines.append("新版本的统计多了字段，执行 ticket telemetry on 同意后才会按新格式发；在那之前按 v%d 发" % c)
        lines.append("上报地址：%s" % (url() or "还没配置（报告只在本机生成，不发送）"))
        if _truthy(os.environ.get("CI")):
            lines.append("检测到 CI=true：不发送")
    pending = sorted(p.stem for p in _pending_dir().glob("*.json"))
    if pending:
        lines.append("待发：%s" % "、".join(pending))
    last = _last()
    if last:
        res = {"ok": "成功", "rejected": "被后端拒收", "retry": "没发出去，稍后重试", "invalid": "校验没通过，已丢弃",
               "nothing": "没有要发的", "error": "发送出错"}.get(last.get("result"), str(last.get("result")))
        lines.append("上次发送：%s，%s%s" % (time.strftime("%m-%d %H:%M", time.localtime(last.get("at", 0))), res,
                                         "（%s 的报告）" % last["day"] if last.get("day") else ""))
    sent = sorted(_sent_dir().glob("*.json"))
    if sent:
        lines.append("已发 %d 份，本机副本在 %s" % (len(sent), _sent_dir()))
    return lines


def show(con, now: Optional[float] = None) -> str:
    """下一份要发的报告（基于昨天的数据），只打印、不发送；关闭状态下也能看（ID 是占位）。"""
    now = db.now() if now is None else now
    iid = install_id(con) or PREVIEW_ID
    rep = build(con, _day(now - 86400), iid)
    validate(rep)
    head = "下一份要发的匿名统计（只打印，不发送）："
    if iid == PREVIEW_ID:
        head += "\n（现在是关闭状态，install_id 是占位；开启后会换成随机生成的 ID）"
    return head + "\n" + json.dumps(rep, ensure_ascii=False, indent=2)


def show_sent() -> str:
    files = sorted(_sent_dir().glob("*.json"))
    if not files:
        return "还没有发过报告（本机副本会放在 %s）" % _sent_dir()
    lines = ["已发 %d 份（%s）：%s" % (len(files), _sent_dir(), "、".join(p.stem for p in files[-14:])),
             "最近一份（%s）：" % files[-1].stem, files[-1].read_text(encoding="utf-8").rstrip()]
    return "\n".join(lines)


INTRO = """匿名统计已开启（报告格式 v%d）。
每天发一份前一天的汇总：版本、系统类型、工单 / 轮次 / 结果的计数，用时、成本、上下文的区间分布，归类后的错误码。
不发任何内容（需求、汇报、标题、文件名、路径、链接、飞书 id、报错原文、模型名）。
看下一份会发什么：ticket telemetry show；关闭：ticket telemetry off；说明：docs/telemetry.md
匿名 ID：%s"""


def command(con, action: str, sent: bool = False) -> str:
    if action in ("on", "off") and _in_worker():
        raise PermissionError("worker 里不能开关匿名统计（只有用户本人能决定）")
    if action == "on":
        iid = turn_on(con)
        out = INTRO % (VERSION, iid)
        if not url():
            out += "\n注意：还没配置上报地址，报告只在本机生成（%s），不发送" % _pending_dir()
        return out
    if action == "off":
        n = turn_off(con)
        return "匿名统计已关闭，匿名 ID 已删除%s。已发副本仍在 %s，可以自己删" % (
            "，删掉了 %d 份没发出的报告" % n if n else "", _sent_dir())
    if action == "show":
        return show_sent() if sent else show(con)
    return "\n".join(status_lines(con))

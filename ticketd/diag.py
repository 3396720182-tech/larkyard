"""诊断包（openspec add-console-problem-reports，规格见 docs/spec.md §12「出错记录和诊断包」）。

心跳只报「哪一步坏了」的代号；要看细节时，同事本人生成一份诊断包、看过内容、明确同意后才发给控制台管理员：
- `ticket diag [T0012]`：在 `~/.ticketd/diag/` 生成一份（目录 700、文件 600），打印路径和全文，不发送；
- `ticket diag send <文件> [--note 一句话]`：只接受上面那个目录里生成的文件，发送前再打一遍码，经 console.request
  （和心跳同一个连接出口、同一个设备令牌）发到 `POST /api/v1/diagnostics`。

内容可以带报错原文、工单原话和过程日志的末尾（所以必须本人看过再发），常见密钥形态先打码；全文 ≤48KB。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__, config, console, core, metrics, safefs

CONTENT_LIMIT = 48 * 1024        # 诊断包全文上限（UTF-8 字节；控制台和 Nginx 都限请求体 64KB）
BODY_LIMIT = 60 * 1024           # 整个请求体上限
NOTE_LIMIT = 500
KEEP_FILES = 20
PROBLEM_DAYS = 7
PROBLEM_ROWS = 50
NAME_RE = re.compile(r"^diag-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}(-T[0-9]{1,8})?\.md$")
SHA_KEY = "diag.sha256:"         # kv：生成时记下的内容摘要（worker 碰不到数据库）；发送时不一致就拒发
TICKET_RE = re.compile(r"^T[0-9]{1,8}$")
PATH = "/api/v1/diagnostics"
REDACTED = "‹已打码›"

# 常见密钥形态（打码后替换为 ‹已打码›）。尽力而为：主要防线是本人发送前看过全文。规则按顺序执行，可以重复执行
_KEYS = (r"api[_-]?key|access[_-]?key|secret[_-]?key|app[_-]?secret|client[_-]?secret|secret|password|passwd|"
         r"passphrase|token|access[_-]?token|refresh[_-]?token|authorization|口令|密码|密钥")
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), REDACTED),
    # Cookie / Set-Cookie：整行（可能有好几个键）
    (re.compile(r"(?i)\b((?:set-)?cookie\s*[:=：]\s*)[^\n]+"), r"\1" + REDACTED),
    # 环境变量式的敏感键名（ANTHROPIC_AUTH_TOKEN=…、DB_PASSWORD=…）：不论值多短都打
    (re.compile(r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
                r"CREDENTIALS?)[A-Z0-9_]*\s*[=:]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;]+)"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # token / basic 后面的串要带数字才算（「token verification」这种普通单词不打）
    (re.compile(r"(?i)\b(token|basic)\s+(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{12,}"), r"\1 " + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), REDACTED),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bly[a-z]?_[A-Za-z0-9_-]{16,}"), REDACTED),                              # 设备 / 网关令牌这类
    # 飞书 tenant / user token：t- / u- 后面一段字母数字混合的长串（t-shirt-blue.png 这种文件名不算）
    (re.compile(r"\b[tu]-(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9._]{24,}"), REDACTED),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),                     # 地址里的用户名口令
    # 键 = "整段带空格的值"
    (re.compile(r"(?i)((?:%s)[\"']?\s*[:=：]\s*)(\"[^\"\n]*\"|'[^'\n]*')" % _KEYS), r"\1" + REDACTED),
    # 键: 值 / 键=值（x-lark-secret: …、"password": "…" 也在这里）
    (re.compile(r"(?i)((?:%s)[\"']?\s*[:=：]\s*[\"']?)([^\s\"',;]{4,})" % _KEYS), r"\1" + REDACTED),
    # 键 空格 值（.netrc 的 password hunter22）：值里要有字母以外的字符，「password reset」不算
    (re.compile(r"(?i)\b(password|passwd|passphrase)\s+(?=[^\s\"',;]*[^A-Za-z\s\"',;])[^\s\"',;]{4,}"),
     r"\1 " + REDACTED),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), REDACTED),                                       # 长十六进制串（多半是密钥或摘要）
]
# 长的随机串（base64 之类）：40 个字符以上、同时有大写、小写和数字。不含 / 和 .，路径和域名不会被误打
_LONG_RANDOM = re.compile(r"(?<![A-Za-z0-9+_=-])[A-Za-z0-9+_-]{40,}={0,2}(?![A-Za-z0-9+_=-])")


def _random_like(m) -> str:
    s = m.group(0)
    if re.search(r"[0-9]", s) and re.search(r"[a-z]", s) and re.search(r"[A-Z]", s):
        return REDACTED
    return s


def redact(text: str) -> str:
    """打码，并去掉除换行和制表符以外的控制字符（防终端转义、防 JSON 里一堆 \\u00XX 撑大请求体）。可以重复执行。"""
    s = "".join(ch for ch in str(text or "") if ch in "\n\t" or ch.isprintable())
    for rx, rep in _SECRET_PATTERNS:
        s = rx.sub(rep, s)
    return _LONG_RANDOM.sub(_random_like, s)


def diag_dir() -> Path:
    return config.home() / "diag"


def _cut(text: str, limit: int) -> str:
    """按 UTF-8 字节截断（不切坏字符）。"""
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    return b[:max(0, limit)].decode("utf-8", "ignore")


def _tail_file(path: Path, max_bytes: int) -> str:
    """文件末尾最多 max_bytes 字节（从下一个整行开始）。工单目录对 worker 可写：和 cli._log_tail 一样经 safefs 打开
    （不跟随符号链接、不在 FIFO 上阻塞、只认普通文件、拒绝硬链接）。读不了返回说明。"""
    try:
        fd = safefs.open_read(path)
        if fd is None:
            return "（没有这个文件）"
        try:
            size = os.fstat(fd).st_size
            raw = safefs.read_at(fd, max(0, size - max_bytes), max_bytes)
        finally:
            os.close(fd)
    except OSError as e:
        return "（读不了：%s）" % e.__class__.__name__
    text = raw.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _t(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else "—"
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def _section(title: str, lines) -> str:
    return "## %s\n%s\n" % (title, "\n".join(lines) if lines else "（没有）")


def _header(con) -> str:
    from . import update
    osd = console.os_fields()
    lines = ["生成时间：%s" % _t(time.time()),
             "Larkyard 版本：%s" % __version__,
             "系统：%s %s %s" % (osd["name"], osd["version"], osd["arch"]),
             "Python：%d.%d.%d" % sys.version_info[:3],
             "主机名：%s" % (console.clean_hostname(socket.gethostname()) or "—")]
    try:
        lines.append("自动更新：%s" % ("；".join(update.disabled_reasons(probe=False)) or "已启用"))
    except Exception as e:  # noqa: BLE001
        lines.append("自动更新：读不出来（%s）" % e.__class__.__name__)
    try:
        lines += console.status_lines(con)
    except Exception as e:  # noqa: BLE001
        lines.append("控制台：读不出来（%s）" % e.__class__.__name__)
    return "# Larkyard 诊断包\n\n" + "\n".join("- " + x for x in lines) + "\n"


def _doctor(con) -> str:
    from . import cli
    try:
        checks = cli.doctor_checks(con, costs=False)
    except Exception as e:  # noqa: BLE001
        return _section("ticket doctor", ["（自检出错：%r）" % (e,)])
    return _section("ticket doctor", ["%s（%s）" % (line, cid) for cid, line in checks])


def _problems(con) -> str:
    since = time.time() - PROBLEM_DAYS * 86400
    marks = ",".join("?" * len(console.EVENT_PROBLEM_KINDS))
    rows = con.execute("SELECT ticket_id, kind, code, detail, created_at FROM events WHERE created_at>=? AND kind IN (%s)"
                       " ORDER BY id DESC LIMIT ?" % marks, (since,) + console.EVENT_PROBLEM_KINDS + (PROBLEM_ROWS,))
    out = []
    for e in rows:
        p = console._problem_of(e["kind"], e["code"])
        if not p:
            continue
        out.append("- %s %s %s/%s %s" % (_t(e["created_at"]), e["ticket_id"], p[0], p[1], (e["detail"] or "").strip()))
    marks = ",".join("?" * len(console.ROUND_PROBLEMS))
    rr = con.execute("SELECT ticket_id, outcome, ended_at FROM rounds WHERE ended_at>=? AND outcome IN (%s)"
                     " ORDER BY id DESC LIMIT ?" % marks, (since,) + console.ROUND_PROBLEMS + (PROBLEM_ROWS,))
    out += ["- %s %s round/%s" % (_t(r["ended_at"]), r["ticket_id"], r["outcome"]) for r in rr]
    return _section("最近 %d 天的出错记录（带原文，最近的在前）" % PROBLEM_DAYS, out)


UPLOAD_ALERTS = ("稿子导入云文档失败", "稿子覆盖原云文档失败", "授权后补发稿子失败", "视频没能传到云空间", "飞书用户身份")
ALERT_ROWS = 15
FAILURE_ROWS = 10


def _uploads(con) -> str:
    """飞书用户身份的状态和最近的导入 / 上传失败原因（T0107：原来诊断包看不到原因原文，只在本机 alerts.log 里）。
    授权链接只写有没有、几点到期，不写设备码。"""
    from . import userauth
    lines = []
    try:
        lines += ["- " + x for x in userauth.status_lines(con)]
    except Exception as e:  # noqa: BLE001
        lines.append("- 用户身份状态读不出来（%s）" % e.__class__.__name__)
    since = time.time() - PROBLEM_DAYS * 86400
    try:
        rows = con.execute("SELECT path, reason, created_at FROM doc_failures WHERE created_at>=? "
                           "ORDER BY created_at DESC LIMIT ?", (since, FAILURE_ROWS)).fetchall()
    except Exception:  # noqa: BLE001  表还没建：从没导入失败过
        rows = []
    lines.append("最近 %d 天没导入成云文档的稿子（最近的在前）：" % PROBLEM_DAYS)
    lines += ["- %s %s：%s" % (_t(r["created_at"]), Path(r["path"]).name, r["reason"]) for r in rows] or ["- 没有"]
    lines.append("告警日志里导入 / 上传 / 授权相关的原文（最近的在前）：")
    text = _tail_file(config.logs_dir() / "alerts.log", 256 * 1024)
    hits = [x for x in text.splitlines() if any(k in x for k in UPLOAD_ALERTS)][-ALERT_ROWS:]
    lines += ["- " + x for x in reversed(hits)] or ["- 没有"]
    return _section("飞书用户身份和最近的上传失败", lines)


def _ticket(con, tid: str) -> tuple:
    """(工单概况, 过程.log 路径, .stderr.log 路径)。"""
    t = core.get(con, tid)
    lines = ["工单：%s（%s）" % (t["id"], t["slug"]), "标题：%s" % t["title"], "状态：%s" % t["state"],
             "创建：%s" % _t(t["created_at"]), "执行器：%s" % (t["executor"] if "executor" in t.keys() else "—"),
             "重试次数：%s" % t["retries"], "原话（前 1500 字）：", (t["request_text"] or "")[:1500]]
    rounds = con.execute("SELECT started_at, ended_at, cost_usd, outcome FROM rounds WHERE ticket_id=? "
                         "ORDER BY id DESC LIMIT 10", (tid,)).fetchall()
    lines.append("最近几轮（最近的在前）：")
    lines += ["- %s 用时 %d 分钟 结果 %s" % (_t(r["ended_at"]), max(0, (r["ended_at"] - r["started_at"]) // 60),
                                         r["outcome"]) for r in rounds] or ["- 没有"]
    ev = con.execute("SELECT kind, code, detail, created_at FROM events WHERE ticket_id=? ORDER BY id DESC LIMIT 30",
                     (tid,)).fetchall()
    lines.append("事件（最近的在前）：")
    lines += ["- %s %s %s %s" % (_t(e["created_at"]), e["kind"], e["code"] or "", (e["detail"] or "").strip())
              for e in ev] or ["- 没有"]
    rep = core.last_report(con, tid)
    if rep:
        lines.append("最近一次汇报：%s %s%s" % (rep["status"], rep["summary"], ("；问：" + rep["ask"]) if rep["ask"] else ""))
    wd = Path(t["workdir"])
    return _section("工单 %s" % tid, lines), wd / "过程.log", wd / ".stderr.log"


def build(con, ticket: Optional[str] = None) -> str:
    """诊断包全文（已打码，≤CONTENT_LIMIT 字节）。优先保留：版本 / 系统 / 控制台、doctor、出错记录、工单概况和汇报；
    过程.log 和 .stderr.log 的末尾按剩下的空间截。"""
    parts = [redact(_header(con)), redact(_doctor(con)), redact(_problems(con)), redact(_uploads(con))]
    logs = []
    if ticket:
        sec, log, err = _ticket(con, ticket)
        parts.append(redact(sec))
        logs = [("过程.log 末尾", log, 0.75), (".stderr.log 末尾", err, 0.25)]
    fixed = "\n".join(parts)
    budget = CONTENT_LIMIT - len(fixed.encode("utf-8")) - 512
    if budget < 0:  # 固定部分本身就超了：截断，日志不放
        return _cut(fixed, CONTENT_LIMIT - 64) + "\n\n（诊断包超过 48KB，后面的内容截掉了）\n"
    for title, path, share in logs:
        room = int(budget * share)
        if room < 200:
            continue
        text = redact(_tail_file(path, room * 2))  # 打码后可能变长或变短：多读一些再按字节截
        text = text.encode("utf-8")[-room:].decode("utf-8", "ignore")
        fixed += "\n" + _section(title, ["```", text.replace("```", "ʼʼʼ"), "```"])
    return _cut(fixed, CONTENT_LIMIT)


def _prune(con, d: Path) -> None:
    files = sorted(p for p in d.iterdir() if NAME_RE.match(p.name))
    for p in files[:-KEEP_FILES]:
        try:
            p.unlink()
        except OSError:
            pass
    keep = {p.name for p in d.iterdir() if NAME_RE.match(p.name)}
    for r in con.execute("SELECT key FROM kv WHERE key LIKE ?", (SHA_KEY + "%",)).fetchall():
        if r[0][len(SHA_KEY):] not in keep:
            con.execute("DELETE FROM kv WHERE key=?", (r[0],))


def _create_private(path: Path, data: bytes) -> None:
    """新建（O_EXCL，同名已存在就失败、不覆盖），600。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)


def create(con, ticket: Optional[str] = None, now: Optional[float] = None) -> Path:
    if console._in_worker():
        raise PermissionError("worker 里不能生成诊断包：由主会话或本人在终端执行 ticket diag")
    if ticket is not None:
        ticket = ticket.strip().upper()
        if not TICKET_RE.match(ticket):
            raise ValueError("工单号的格式是 T 加数字，比如 T0012")
    text = build(con, ticket)
    now = time.time() if now is None else now
    d = diag_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    data = text.encode("utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    for _ in range(5):
        path = d / ("diag-%s-%s%s.md" % (stamp, secrets.token_hex(3), "-" + ticket if ticket else ""))
        try:
            _create_private(path, data)
            break
        except FileExistsError:
            continue
    else:
        raise ValueError("生成诊断包失败：文件名一直冲突，请重试")
    metrics.kv_set(con, SHA_KEY + path.name, hashlib.sha256(data).hexdigest())
    _prune(con, d)
    return path


def _resolve(file: str) -> Path:
    """只接受 diag_dir() 里、按生成规则命名的文件（主会话不能借这个命令把任意文件发出去）。"""
    d = diag_dir().resolve()
    raw = Path(file).expanduser()
    p = raw if raw.is_absolute() else d / raw.name if raw.parent == Path(".") else raw
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("没有这个诊断包：%s（先执行 ticket diag 生成）" % file)
    if rp.parent != d or not NAME_RE.match(rp.name) or p.is_symlink():
        raise ValueError("只能发送 ticket diag 生成在 %s 里的诊断包" % d)
    return rp


def _read_generated(con, path: Path) -> bytes:
    """读诊断包：不跟随符号链接、只认普通文件、拒绝硬链接；内容必须和生成时记下的摘要一致
    （看过之后被改写、复制件、别的文件改成这个名字，都拒发）。"""
    want = metrics.kv_get(con, SHA_KEY + path.name)
    if not want:
        raise ValueError("这个诊断包不是本机 ticket diag 生成的（或已过期被清理）：请重新执行 ticket diag 生成")
    try:
        fd = safefs.open_read(path)
        if fd is None:
            raise ValueError("没有这个诊断包：%s" % path)
        try:
            size = os.fstat(fd).st_size
            if size > CONTENT_LIMIT:
                raise ValueError("诊断包超过 48KB（被改过），请重新执行 ticket diag 生成")
            raw = safefs.read_at(fd, 0, CONTENT_LIMIT + 1)
        finally:
            os.close(fd)
    except OSError as e:
        raise ValueError("读不了诊断包（%s）：只能发送 ticket diag 生成的原文件" % e)
    if hashlib.sha256(raw).hexdigest() != want:
        raise ValueError("诊断包生成之后被改过，和给你看的不一样了：请重新执行 ticket diag 生成、看过后再发")
    return raw


def send(con, file: str, note: str = "") -> str:
    if console._in_worker():
        raise PermissionError("worker 里不能发送诊断包：要同事本人看过内容、确认后，由主会话或本人在终端执行")
    path = _resolve(file)
    if not console.enrolled():
        raise ValueError("本机没有入网，诊断包发不了：可以把 %s 直接发给管理员" % path)
    raw = _read_generated(con, path)
    text = redact(raw.decode("utf-8", "replace"))
    if len(text.encode("utf-8")) > CONTENT_LIMIT:
        raise ValueError("诊断包超过 48KB（被改过），请重新执行 ticket diag 生成")
    note = "".join(ch for ch in (note or "") if ch.isprintable()).strip()
    note = redact(note)[:NOTE_LIMIT]
    m = re.search(r"-(T[0-9]{1,8})\.md$", path.name)  # 文件名里的工单号（生成时写的）
    body = {"ticket": m.group(1) if m else None, "note": note, "content": text}
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > BODY_LIMIT:
        raise ValueError("诊断包转成请求后超过 60KB，请重新执行 ticket diag 生成")
    try:
        resp = console.request("POST", PATH, body, token=console.read_token())
    except console.ConsoleError as e:
        raise ValueError("诊断包没发出去：%s" % e.text())
    did = resp.get("id")
    if resp.get("ok") is not True or not isinstance(did, int) or isinstance(did, bool):
        raise ValueError("诊断包没发出去：控制台没有确认")
    metrics.record(con, metrics.SYSTEM_TICKET, console.KIND, "诊断包 %s 已发给管理员（#%d）" % (path.name, did),
                   code="diag_sent")
    return "✅ 诊断包已发给控制台管理员（编号 #%d，文件 %s）" % (did, path)


def command(con, args: list, note: str = "") -> str:
    """`ticket diag [T0012]` / `ticket diag send <文件> [--note 一句话]`。"""
    args = [a for a in (args or []) if a]
    if args and args[0] == "send":
        if len(args) != 2:
            raise ValueError("用法：ticket diag send <诊断包文件> [--note 一句话]")
        return send(con, args[1], note)
    if len(args) > 1:
        raise ValueError("用法：ticket diag [工单号]；发送：ticket diag send <诊断包文件> [--note 一句话]")
    if note:
        raise ValueError("--note 只用于 ticket diag send")
    path = create(con, args[0] if args else None)
    text = path.read_text(encoding="utf-8")
    where = ("发送前请本人看完下面的全文。确认要发给控制台管理员，再执行：ticket diag send %s [--note 一句话]" % path
             if console.enrolled() else "本机没有入网，不能直接发送；需要的话把这个文件交给管理员")
    return "诊断包已生成（还没发送）：%s（%d 字节）\n%s\n\n%s" % (path, len(text.encode("utf-8")), where, text)

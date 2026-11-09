"""运营指标：把这次运行复盘里手工翻库、翻日志才看得到的东西变成系统自己的例行动作。

- 成本落库：worker 每轮结束时由调度器写入（tickets.cost_usd、rounds 表）。
- 关键事件落库（events 表）：疑似卡住、中断、中止、故障等待、模型接口报错、沙箱外执行、结果没送达……
- 紧急事件即时私信主用户（结果没送达、媒体任务卡住），同一类一小时最多一条；原来只写本机 alerts.log，没人看。
- `ticket stats`：最近 N 天的运行概况；守护进程按 TICKETD_DIGEST（默认每周一 10:00）私信一份。
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import List, Optional

from . import config, db, templates

SYSTEM_TICKET = "_ticketd"  # 不属于任何工单的系统消息（告警、周报）在发件箱里用的工单号
OWNER = "@owner"  # 系统消息的收件人占位：发送时由守护进程按它自己的配置解析（outbox._resolve_owner）
URGENT = {"delivery_dead": "结果没送达", "media_stuck": "媒体任务卡住"}
PUSH_EVERY = 3600.0
EVENT_LABELS = {
    "stall_flag": "疑似卡住", "stall_interrupt": "卡住后中断命令", "stall_abort": "卡住后中止一轮",
    "outage_start": "进入故障等待", "api_error": "模型接口报错", "run_requested": "申请沙箱外执行",
    "run_approved": "批准沙箱外执行", "delivery_dead": "结果没送达", "media_stuck": "媒体任务卡住",
    "card_giveup": "状态卡放弃",
}


# ---------- 落库 ----------
def record(con, ticket_id: str, kind: str, detail: str = "") -> None:
    """记一条事件。永不抛异常：指标不能挡住正事。"""
    try:
        con.execute("INSERT INTO events(ticket_id, kind, detail, created_at) VALUES(?,?,?,?)",
                    (ticket_id, kind, templates.short(detail, 300), db.now()))
    except Exception:
        pass


def kv_get(con, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, value))


def send_owner(con, text: str) -> int:
    """私信主用户（不进任何群），返回发件箱行号。收件人不在这里定：记成占位 @owner，由守护进程发送时按它自己读到的
    TICKETD_OWNER 解析——ticket 命令在 Claude Code 会话里跑时读的是环境变量，守护进程只读 config.env，两边可能不一样
    （2026-11-09：从会话里塞的测试私信用了环境变量里的值，没测到守护进程自己的路径）。"""
    return con.execute("INSERT INTO outbox(ticket_id, reply_to, text, created_at) VALUES(?,?,?,?)",
                       (SYSTEM_TICKET, OWNER, text, db.now())).lastrowid


def notify(con, ticket_id: str, kind: str, text: str) -> None:
    """紧急事件：落库，并私信主用户（同一类一小时最多一条，其余只落库）。永不抛异常。"""
    record(con, ticket_id, kind, text)
    try:
        key = "push:" + kind
        last = float(kv_get(con, key) or 0)
        if db.now() - last < PUSH_EVERY:
            return
        if config.owner().startswith("ou_"):  # 在守护进程里调用：看的就是守护进程自己的配置
            send_owner(con, "🚨 %s：%s" % (URGENT.get(kind, kind), text))
            kv_set(con, key, str(db.now()))
    except Exception:
        pass


# ---------- 成本 ----------
def round_outcome(con, t) -> str:
    """一轮结束时的结果：本轮最后一份终态汇报的状态；没有汇报时用工单状态（停止、暂停、失败、重试）。"""
    rep = con.execute("SELECT status FROM reports WHERE ticket_id=? AND status<>'progress' AND created_at>=?"
                      " ORDER BY id DESC LIMIT 1", (t["id"], t["started_at"] or 0)).fetchone()
    if rep:
        return rep["status"]
    if t["state"] == "idle" and t["retry_at"]:
        return "retry"
    return t["state"]


def add_round(con, t, cost: float, outcome: str) -> None:
    con.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome) VALUES(?,?,?,?,?)",
                (t["id"], t["started_at"] or db.now(), db.now(), cost, outcome))


def backfill_costs(con) -> int:
    """老工单（成本落库之前跑的）从 .stream.jsonl 补成本：每个 result 事件算一轮，按会话累计值换算成本轮成本。
    只补还没有任何轮次记录的工单，可以重复执行。返回补了几张单。"""
    n = 0
    for t in con.execute("SELECT * FROM tickets WHERE id NOT IN (SELECT DISTINCT ticket_id FROM rounds)").fetchall():
        p = Path(t["workdir"]) / ".stream.jsonl"
        if not p.is_file():
            continue
        prev, total, rows = 0.0, 0.0, []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"result"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    c = ev.get("total_cost_usd") if ev.get("type") == "result" else None
                    if not isinstance(c, (int, float)):
                        continue
                    delta = c - prev if c >= prev else c  # 变小了说明换了会话
                    prev = c
                    total += delta
                    rows.append(delta)
        except OSError:
            continue
        if not rows:
            continue
        with db.tx(con):
            for d in rows:
                con.execute("INSERT INTO rounds(ticket_id, started_at, ended_at, cost_usd, outcome)"
                            " VALUES(?,?,?,?,?)", (t["id"], t["created_at"], t["updated_at"], d, "backfill"))
            con.execute("UPDATE tickets SET cost_usd=?, session_cost=? WHERE id=?", (total, prev, t["id"]))
        n += 1
    return n


# ---------- 统计 ----------
def _usd(x: float) -> str:
    return "$%.2f" % x if x < 10 else "$%.0f" % x


def _alerts_since(since: float) -> int:
    p = config.logs_dir() / "alerts.log"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    return sum(1 for line in lines if line[:19] >= stamp)


def stats(con, days: float = 7, now: Optional[float] = None) -> str:
    """最近 days 天的运行概况（纯文字，私信和终端里都能看）。"""
    now = db.now() if now is None else now
    since = now - days * 86400
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    head = "📊 Larkyard 运行概况（%s ～ %s）" % (time.strftime("%m-%d", time.localtime(since)),
                                            time.strftime("%m-%d", time.localtime(now)))
    lines = [head]
    new = q("SELECT COUNT(*) n FROM tickets WHERE created_at>=?", since)[0]["n"]
    r = q("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c, SUM(outcome='backfill') b FROM rounds WHERE ended_at>=?",
          since)[0]
    lines.append("工单：新开 %d 张，跑了 %d 轮，worker 成本约 %s%s" % (
        new, r["n"], _usd(r["c"]), "（其中 %d 轮是从老工单补算的，没有结果记录）" % r["b"] if r["b"] else ""))
    outs = q("SELECT outcome, COUNT(*) n FROM rounds WHERE ended_at>=? AND outcome<>'backfill'"
             " GROUP BY outcome ORDER BY n DESC", since)
    if outs:
        names = dict(templates.LABELS, retry="🔁 临时故障重试", stopped="⏹️ 被停止", held="⏸️ 被暂停",
                     idle="空闲", running="运行中")
        lines.append("每轮结果：" + "，".join("%s %d" % (names.get(o["outcome"], o["outcome"]), o["n"]) for o in outs))
    top = q("SELECT r.ticket_id, SUM(r.cost_usd) c, t.title FROM rounds r JOIN tickets t ON t.id=r.ticket_id"
            " WHERE r.ended_at>=? GROUP BY r.ticket_id ORDER BY c DESC LIMIT 3", since)
    if top and top[0]["c"] > 0:
        lines.append("最花钱：" + "，".join("%s「%s」%s" % (x["ticket_id"], templates.short(x["title"], 12), _usd(x["c"]))
                                         for x in top if x["c"] > 0))
    hint = config.cost_hint_usd()
    long_sessions = q("SELECT id, session_cost FROM tickets WHERE session_cost>=? AND updated_at>=?"
                      " ORDER BY session_cost DESC LIMIT 3", hint, since) if hint else []
    if long_sessions:
        lines.append("会话已很长（每次追问都按整段上下文计费）：" + "，".join(
            "%s 累计 %s" % (x["id"], _usd(x["session_cost"])) for x in long_sessions) + "；可以换新会话或开新单")
    blocked = q("SELECT COUNT(*) n FROM reports WHERE status IN ('blocked','waiting') AND created_at>=?", since)[0]["n"]
    runs = q("SELECT COUNT(*) n, SUM(status IN ('approved','running','done','interrupted')) a FROM runs"
             " WHERE created_at>=?", since)[0]
    grants = q("SELECT COUNT(*) n FROM inputs WHERE kind='grant' AND created_at>=?", since)[0]["n"]
    lines.append("需要你：%d 次（其中申请沙箱外执行 %d 次、批准 %d 次；授权 %d 次）" % (
        blocked, runs["n"] or 0, runs["a"] or 0, grants))
    lat = _response_minutes(con, since)
    if lat:
        lines.append("你的回复：中位 %.0f 分钟，最慢 %.0f 分钟" % (statistics.median(lat), max(lat)))
    fails = q("SELECT ticket_id, summary FROM reports WHERE status='failed' AND created_at>=? ORDER BY id DESC", since)
    if fails:
        lines.append("失败 %d 次，最近：%s" % (len(fails), "；".join(
            "%s %s" % (f["ticket_id"], templates.short(f["summary"], 40)) for f in fails[:3])))
    ev = q("SELECT kind, COUNT(*) n FROM events WHERE created_at>=? GROUP BY kind ORDER BY n DESC", since)
    ev = [e for e in ev if e["kind"] not in ("run_requested", "run_approved")]
    if ev:
        lines.append("事件：" + "，".join("%s %d" % (EVENT_LABELS.get(e["kind"], e["kind"]), e["n"]) for e in ev))
    dead = q("SELECT COUNT(*) n FROM outbox WHERE status='dead' AND created_at>=?", since)[0]["n"]
    alerts = _alerts_since(since)
    lines.append("投递：%s；本机告警 %d 条%s" % ("全部送达" if not dead else "%d 条没送达" % dead, alerts,
                                         "（详见 ~/.ticketd/logs/alerts.log）" if alerts else ""))
    return "\n".join(lines)


def _response_minutes(con, since: float) -> List[float]:
    """blocked / waiting 汇报之后，用户多久回应（追问、授权、放开、恢复，或批准沙箱外执行）。"""
    out = []
    for rep in con.execute("SELECT ticket_id, created_at FROM reports WHERE status IN ('blocked','waiting')"
                           " AND created_at>=?", (since,)).fetchall():
        a = con.execute("SELECT MIN(created_at) m FROM inputs WHERE ticket_id=? AND created_at>?"
                        " AND kind IN ('followup','grant','release','fresh')",
                        (rep["ticket_id"], rep["created_at"])).fetchone()["m"]
        b = con.execute("SELECT MIN(approved_at) m FROM runs WHERE ticket_id=? AND approved_at>?",
                        (rep["ticket_id"], rep["created_at"])).fetchone()["m"]
        first = min(x for x in (a, b, float("inf")) if x is not None)
        if first != float("inf"):
            out.append((first - rep["created_at"]) / 60)
    return out


# ---------- 周报 ----------
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _last_slot(spec: str, now: float) -> Optional[float]:
    """TICKETD_DIGEST（如 "mon 10:00"）最近一个已经到点的时刻；格式不对返回 None。"""
    try:
        day, hm = spec.lower().split()
        wd = WEEKDAYS[day[:3]]
        h, m = (int(x) for x in hm.split(":"))
    except (ValueError, KeyError):
        return None
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    slot = base - ((lt.tm_wday - wd) % 7) * 86400
    if slot > now:
        slot -= 7 * 86400
    return slot


def maybe_digest(con, now: Optional[float] = None) -> bool:
    """到点了就私信一份最近 7 天的概况（每个时刻只发一次，跨重启也不重发）。第一次运行只记下时刻，不补发。"""
    spec = config.digest()
    if not spec:
        return False
    now = db.now() if now is None else now
    slot = _last_slot(spec, now)
    if slot is None:
        return False
    last = kv_get(con, "digest_last")
    if last is None:
        kv_set(con, "digest_last", str(slot))
        return False
    if float(last) >= slot:
        return False
    kv_set(con, "digest_last", str(slot))
    if not config.owner().startswith("ou_"):
        return False
    send_owner(con, stats(con, 7, now))
    return True

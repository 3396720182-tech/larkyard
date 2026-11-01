from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional

from . import config, db, templates

STATUSES = ("ready", "blocked", "failed", "dropped", "waiting", "progress")


class TicketError(Exception):
    pass


def _next_id(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT MAX(CAST(substr(id,2) AS INTEGER)) AS n FROM tickets").fetchone()
    return "T%04d" % ((row["n"] + 1) if row["n"] is not None else 1)


def touch(con: sqlite3.Connection, ticket_id: str, **fields) -> None:
    fields["updated_at"] = db.now()
    cols = ", ".join("%s=?" % k for k in fields)
    con.execute("UPDATE tickets SET %s WHERE id=?" % cols, list(fields.values()) + [ticket_id])


def touch_if(con: sqlite3.Connection, ticket_id: str, expected_states: Iterable[str], **fields) -> bool:
    """仅当工单当前状态在 expected_states 里时才更新；返回是否更新成功。"""
    states = list(expected_states)
    fields["updated_at"] = db.now()
    cols = ", ".join("%s=?" % k for k in fields)
    cur = con.execute("UPDATE tickets SET %s WHERE id=? AND state IN (%s)" % (cols, ",".join("?" * len(states))),
                      list(fields.values()) + [ticket_id] + states)
    return cur.rowcount == 1


def get(con: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if row is None:
        raise TicketError("没有工单 %s" % ticket_id)
    return row


def grants(con: sqlite3.Connection, ticket_id: str) -> List[str]:
    rows = con.execute("SELECT path FROM grants WHERE ticket_id=? ORDER BY created_at, path", (ticket_id,))
    return [r["path"] for r in rows]


def _norm_grant(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise TicketError("授权目录必须是绝对路径：%s" % path)
    p = p.resolve()
    if not p.is_dir():
        raise TicketError("授权目录不存在：%s" % p)
    for banned in (config.workroot().resolve(), config.home().resolve()):
        n = min(len(p.parts), len(banned.parts))
        if p.parts[:n] == banned.parts[:n]:  # 相等、祖先或后代
            raise TicketError("授权目录不能覆盖工单区或 ticketd 数据目录：%s" % p)
    return str(p)


def add_input(con: sqlite3.Connection, ticket_id: str, kind: str, text: str,
              source_message_id: Optional[str] = None) -> None:
    con.execute("INSERT INTO inputs(ticket_id, kind, text, source_message_id, created_at) VALUES(?,?,?,?,?)",
                (ticket_id, kind, text, source_message_id, db.now()))


def create_ticket(con: sqlite3.Connection, *, chat_id: str, message_id: str, title: str, slug: str,
                  request_text: str, ack_text: str, note: Optional[str] = None,
                  grant_paths: Iterable[str] = (), sender: Optional[str] = None) -> str:
    if sender is not None:
        if not config.owner():
            raise TicketError("没有配置主用户：在 %s 里写 TICKETD_OWNER=<你的 open_id>" % config.config_file())
        if sender != config.owner():
            raise TicketError("v1 只为主用户派单")
    if note and len(note) > 30:
        raise TicketError("派单备注超过 30 字")
    if not slug or "/" in slug or slug.startswith("."):
        raise TicketError("短名不合法：%r" % slug)
    if not request_text.strip():
        raise TicketError("用户原话为空")
    paths = [_norm_grant(p) for p in grant_paths]
    with db.tx(con):
        if con.execute("SELECT 1 FROM bindings WHERE message_id=?", (message_id,)).fetchone():
            raise TicketError("消息 %s 已绑定工单" % message_id)
        tid = _next_id(con)
        workdir = str((config.workroot() / ("%s-%s" % (tid, slug))).resolve())
        t = db.now()
        con.execute(
            "INSERT INTO tickets(id, slug, title, chat_id, origin_message_id, request_text, router_note,"
            " ack_text, workdir, session_id, state, card_wanted, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (tid, slug, title, chat_id, message_id, request_text, note, ack_text, workdir,
             str(uuid.uuid4()), "queued", t, t))
        for p in paths:
            con.execute("INSERT INTO grants VALUES(?,?,?)", (tid, p, t))
        con.execute("INSERT INTO bindings VALUES(?,?)", (message_id, tid))
        add_input(con, tid, "assignment", request_text, message_id)
    return tid


def lookup(con: sqlite3.Connection, message_id: str) -> Optional[str]:
    row = con.execute("SELECT ticket_id FROM bindings WHERE message_id=?", (message_id,)).fetchone()
    return row["ticket_id"] if row else None


def send_followup(con: sqlite3.Connection, ticket_id: str, message_id: str, text: str) -> None:
    with db.tx(con):
        t = get(con, ticket_id)
        if t["state"] == "stopped":
            raise TicketError("%s 已停止，需要开新单" % ticket_id)
        bound = lookup(con, message_id)
        if bound is not None and bound != ticket_id:
            raise TicketError("消息 %s 已绑定到 %s" % (message_id, bound))
        if con.execute("SELECT 1 FROM inputs WHERE ticket_id=? AND kind='followup' AND source_message_id=?",
                       (ticket_id, message_id)).fetchone():
            return
        con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (message_id, ticket_id))
        add_input(con, ticket_id, "followup", text, message_id)
        touch(con, ticket_id, retries=0, retry_at=0)


def _reply_target(con: sqlite3.Connection, ticket_id: str) -> str:
    row = con.execute(
        "SELECT source_message_id FROM inputs WHERE ticket_id=? AND kind IN ('assignment','followup')"
        " AND status='delivered' AND source_message_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (ticket_id,)).fetchone()
    if row:
        return row["source_message_id"]
    return get(con, ticket_id)["origin_message_id"]


def _validate_report(status: str, summary: str) -> None:
    if status not in STATUSES:
        raise TicketError("未知状态：%s" % status)
    if not summary.strip():
        raise TicketError("summary 不能为空")


def write_spool(spool_dir: str, status: str, summary: str, artifacts: Iterable[str] = (),
                ask: Optional[str] = None) -> Path:
    _validate_report(status, summary)
    d = Path(spool_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = "%020d-%d.json" % (time.time_ns(), os.getpid())
    tmp = d / (".tmp-" + name)
    tmp.write_text(json.dumps({"status": status, "summary": summary, "artifacts": list(artifacts),
                               "ask": ask}, ensure_ascii=False), encoding="utf-8")
    final = d / name
    os.replace(str(tmp), str(final))
    return final


SPOOL_MAX_BYTES = 64 * 1024
SPOOL_MAX_FILES = 10
_STR_MAX = 2000


def _check_spool_schema(d) -> None:
    if not isinstance(d, dict):
        raise TypeError("not a dict")
    status, summary, ask, arts = d.get("status"), d.get("summary"), d.get("ask"), d.get("artifacts")
    if not isinstance(status, str) or not isinstance(summary, str) or len(summary) > _STR_MAX:
        raise TypeError("bad status/summary")
    if ask is not None and (not isinstance(ask, str) or len(ask) > _STR_MAX):
        raise TypeError("bad ask")
    if arts is not None:
        if (not isinstance(arts, list) or len(arts) > 20
                or any(not isinstance(a, str) or len(a) > 1000 for a in arts)):
            raise TypeError("bad artifacts")


def _read_spool_file(dfd: int, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > SPOOL_MAX_BYTES:
            raise ValueError("not a small regular file")
        raw = fh.read(SPOOL_MAX_BYTES + 1)
    if len(raw) > SPOOL_MAX_BYTES:
        raise ValueError("too large")
    return json.loads(raw.decode("utf-8"))


def ingest_spool(con: sqlite3.Connection, ticket_id: str, spool_dir: str, limit: int = SPOOL_MAX_FILES) -> int:
    # spool 目录由 worker 控制：一律当作敌对输入（不跟随符号链接、不读大文件、不按路径字符串改名/删除）。
    # 运行中每个 tick 都会调用：先把文件改名为点文件「认领」再入库，删不掉也不会被重复收件。
    try:
        dfd = os.open(str(spool_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0
    n = 0
    try:
        def mark_bad(name: str, src: Optional[str] = None) -> None:
            try:
                os.rename(src or name, name + ".bad", src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError:
                pass

        names = sorted(x for x in os.listdir(dfd) if x.endswith(".json") and not x.startswith("."))
        for name in names:
            if n >= limit:
                mark_bad(name)
                continue
            claimed = ".claimed-" + name
            try:
                os.rename(name, claimed, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError:
                continue
            try:
                data = _read_spool_file(dfd, claimed)
                _check_spool_schema(data)
                add_report(con, ticket_id, data["status"], data["summary"],
                           data.get("artifacts") or [], data.get("ask"))
            except (OSError, ValueError, KeyError, TypeError, TicketError):
                mark_bad(name, claimed)
                continue
            try:
                os.unlink(claimed, dir_fd=dfd)
            except OSError:
                pass
            n += 1
    finally:
        os.close(dfd)
    return n


def add_report(con: sqlite3.Connection, ticket_id: str, status: str, summary: str,
               artifacts: Iterable[str] = (), ask: Optional[str] = None) -> None:
    _validate_report(status, summary)
    arts = list(artifacts)
    with db.tx(con):
        t = get(con, ticket_id)
        now = db.now()
        rid = con.execute("INSERT INTO reports(ticket_id, status, summary, artifacts_json, ask, created_at)"
                          " VALUES(?,?,?,?,?,?)",
                          (ticket_id, status, summary, json.dumps(arts, ensure_ascii=False), ask, now)).lastrowid
        msg = templates.report_message(ticket_id, status, summary, arts, ask)
        if not msg:
            return
        card = None
        if status != "progress":  # 终态汇报发结果卡片；文字留作卡片发不出去时的兜底
            started = t["started_at"]
            card = templates.result_card(
                ticket_id, t["title"], t["workdir"], status, summary, arts, ask,
                phases(con, ticket_id, started or now), now - started if started else None)
        enqueue_message(con, ticket_id, msg, card=card, report_id=rid)


def enqueue_message(con: sqlite3.Connection, ticket_id: str, text: str, card: Optional[dict] = None,
                    report_id: Optional[int] = None) -> None:
    """推一条新消息给用户（回复最近一条投递过的用户消息），由发件箱发送。"""
    con.execute("INSERT INTO outbox(ticket_id, reply_to, text, card_json, report_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (ticket_id, _reply_target(con, ticket_id), text,
                 json.dumps(card, ensure_ascii=False) if card else None, report_id, db.now()))


def phases(con: sqlite3.Connection, ticket_id: str, since: float) -> List[str]:
    """本轮 worker 自报过的阶段（按时间，去掉连续重复）。"""
    out: List[str] = []
    for r in con.execute("SELECT summary FROM reports WHERE ticket_id=? AND status='progress' AND created_at>=?"
                         " ORDER BY id", (ticket_id, since)):
        s = " ".join(r["summary"].split())
        if s and (not out or out[-1] != s):
            out.append(s)
    return out


def last_report(con: sqlite3.Connection, ticket_id: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM reports WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
                       (ticket_id,)).fetchone()


def last_final_report(con: sqlite3.Connection, ticket_id: str, since: float = 0) -> Optional[sqlite3.Row]:
    """最近一条终态汇报（不含 progress）；since 用来只看本轮。"""
    return con.execute("SELECT * FROM reports WHERE ticket_id=? AND status<>'progress' AND created_at>=?"
                       " ORDER BY id DESC LIMIT 1", (ticket_id, since)).fetchone()


def last_progress(con: sqlite3.Connection, ticket_id: str, since: float = 0) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM reports WHERE ticket_id=? AND status='progress' AND created_at>=?"
                       " ORDER BY id DESC LIMIT 1", (ticket_id, since)).fetchone()


def stop(con: sqlite3.Connection, ticket_id: str) -> None:
    with db.tx(con):
        if get(con, ticket_id)["state"] == "stopped":
            raise TicketError("%s 已经停止" % ticket_id)
        touch(con, ticket_id, state="stopped")


def hold(con: sqlite3.Connection, ticket_id: str) -> None:
    with db.tx(con):
        st = get(con, ticket_id)["state"]
        if st in ("stopped", "held"):
            raise TicketError("%s 当前状态为 %s，不能暂停" % (ticket_id, st))
        touch(con, ticket_id, state="held")


def release(con: sqlite3.Connection, ticket_id: str) -> None:
    with db.tx(con):
        if get(con, ticket_id)["state"] != "held":
            raise TicketError("%s 不在暂停中" % ticket_id)
        touch(con, ticket_id, state="idle")
        add_input(con, ticket_id, "release", "用户已放开暂停，可以继续。")


def grant(con: sqlite3.Connection, ticket_id: str, path: str) -> str:
    p = _norm_grant(path)
    with db.tx(con):
        if get(con, ticket_id)["state"] == "stopped":
            raise TicketError("%s 已停止" % ticket_id)
        con.execute("INSERT OR IGNORE INTO grants VALUES(?,?,?)", (ticket_id, p, db.now()))
        add_input(con, ticket_id, "grant", "用户已授权可写目录：%s" % p)
    return p


def list_tickets(con: sqlite3.Connection) -> List[sqlite3.Row]:
    return con.execute("SELECT * FROM tickets ORDER BY id").fetchall()

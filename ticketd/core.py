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


def net_grants(con: sqlite3.Connection, ticket_id: str) -> dict:
    """文件系统以外的沙箱授权：{"local": bool, "sockets": [路径...]}。"""
    rows = con.execute("SELECT kind, value FROM net_grants WHERE ticket_id=? ORDER BY created_at, value",
                       (ticket_id,)).fetchall()
    return {"local": any(r["kind"] == "local" for r in rows),
            "sockets": [r["value"] for r in rows if r["kind"] == "socket"]}


def _norm_socket(path: str) -> List[str]:
    """unix socket 授权的路径：绝对路径、要存在（socket 文件或放 socket 的目录）。沙箱按解析后的路径匹配，
    所以链接（如 /var/run/docker.sock → ~/.docker/run/docker.sock）连同解析后的路径一起授权。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise TicketError("socket 路径必须是绝对路径：%s" % path)
    if not p.exists():
        raise TicketError("socket 路径不存在：%s" % p)
    out = [str(p)]
    real = os.path.realpath(str(p))
    if real not in out:
        out.append(real)
    return out


def grant_net(con: sqlite3.Connection, ticket_id: str, local: bool = False, socket: Optional[str] = None) -> str:
    """放开本机网络（--local-net）或一个 unix socket（--socket）。只对 Claude 单生效（Codex 的沙箱没有对应开关）。"""
    paths = _norm_socket(socket) if socket else []
    with db.tx(con):
        t = get(con, ticket_id)
        if t["state"] == "stopped":
            raise TicketError("%s 已停止" % ticket_id)
        now = db.now()
        if local:
            con.execute("INSERT OR IGNORE INTO net_grants VALUES(?,?,?,?)", (ticket_id, "local", "", now))
            what = "本机网络（可以连本机端口、在本机监听端口）"
        else:
            for p in paths:
                con.execute("INSERT OR IGNORE INTO net_grants VALUES(?,?,?,?)", (ticket_id, "socket", p, now))
            what = "unix socket：%s" % paths[0]
        add_input(con, ticket_id, "grant", "用户已授权%s。" % what)
    return what


def add_input(con: sqlite3.Connection, ticket_id: str, kind: str, text: str,
              source_message_id: Optional[str] = None) -> None:
    con.execute("INSERT INTO inputs(ticket_id, kind, text, source_message_id, created_at) VALUES(?,?,?,?,?)",
                (ticket_id, kind, text, source_message_id, db.now()))


def create_ticket(con: sqlite3.Connection, *, chat_id: str, message_id: str, title: str, slug: str,
                  request_text: str, ack_text: str, note: Optional[str] = None,
                  grant_paths: Iterable[str] = (), sender: Optional[str] = None,
                  executor: Optional[str] = None) -> str:
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
    executor = executor or config.executor()
    if executor not in ("claude", "codex"):  # 与 executors.NAMES 一致；core 不 import executors，保持轻量
        raise TicketError("未知执行器：%r（可选 claude / codex）" % executor)
    paths = [_norm_grant(p) for p in grant_paths]
    with db.tx(con):
        if con.execute("SELECT 1 FROM bindings WHERE message_id=?", (message_id,)).fetchone():
            raise TicketError("消息 %s 已绑定工单" % message_id)
        tid = _next_id(con)
        workdir = str((config.workroot() / ("%s-%s" % (tid, slug))).resolve())
        t = db.now()
        con.execute(
            "INSERT INTO tickets(id, slug, title, chat_id, origin_message_id, request_text, router_note,"
            " ack_text, workdir, session_id, state, card_wanted, created_at, updated_at, executor)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (tid, slug, title, chat_id, message_id, request_text, note, ack_text, workdir,
             str(uuid.uuid4()), "queued", t, t, executor))
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
            raise TicketError("%s 已停止；要接着做先恢复（ticket release %s），或者开新单" % (ticket_id, ticket_id))
        bound = lookup(con, message_id)
        if bound is not None and bound != ticket_id:
            raise TicketError("消息 %s 已绑定到 %s" % (message_id, bound))
        if con.execute("SELECT 1 FROM inputs WHERE ticket_id=? AND kind='followup' AND source_message_id=?",
                       (ticket_id, message_id)).fetchone():
            return
        con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (message_id, ticket_id))
        add_input(con, ticket_id, "followup", text, message_id)
        # 用户说话了：马上投递（不等故障重试的时间点），故障等待重新起算，再失败会重新告诉用户
        touch(con, ticket_id, retries=0, retry_at=0, outage_since=None)


def _reply_target(con: sqlite3.Connection, ticket_id: str) -> str:
    row = con.execute(
        "SELECT source_message_id FROM inputs WHERE ticket_id=? AND kind IN ('assignment','followup')"
        " AND status='delivered' AND source_message_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (ticket_id,)).fetchone()
    if row:
        return row["source_message_id"]
    return get(con, ticket_id)["origin_message_id"]


def check_media_artifacts(workdir: str, artifacts: Iterable[str]) -> None:
    """worker 交终态汇报时检查图片/视频产物：结果卡只显示工单目录内的媒体（发件箱的安全规则，§5.3），
    目录外或不存在的当场报错，让 worker 复制进来再报（原来静默丢图，T0005 选图卡里一张图都没有）。
    这里只是提前告诉 worker，安全检查仍以发件箱为准。"""
    root = os.path.realpath(workdir)
    bad = []
    for a in artifacts:
        if templates._URL.match(a) or not a.lower().endswith(templates.IMAGE_EXTS + templates.VIDEO_EXTS):
            continue
        real = os.path.realpath(os.path.join(workdir, a))  # 绝对路径时 join 返回 a 本身
        if not real.startswith(root + os.sep):
            bad.append("%s（不在工单目录内）" % a)
        elif not os.path.isfile(real):
            bad.append("%s（文件不存在）" % a)
    if bad:
        raise TicketError("这些图片/视频不会显示在结果卡里：%s。请先复制到工单目录（如 cp <文件> %s/），"
                          "再用工单目录里的路径重新执行这条 ticket report" % ("；".join(bad), workdir))


def _validate_report(status: str, summary: str, run: Optional[dict] = None) -> None:
    if status not in STATUSES:
        raise TicketError("未知状态：%s" % status)
    if not summary.strip():
        raise TicketError("summary 不能为空")
    if run is not None and status != "blocked":
        raise TicketError("--run 只能和 --status blocked 一起用（要等用户批准）")


RUN_MAX_BYTES = 8 * 1024
RUN_DEFAULT_ASK = "要我在沙箱外执行上面这段脚本吗？同意就回复「执行」；不同意或要改，直接说怎么改"


def read_run_script(workdir: str, path: str) -> dict:
    """worker 申请沙箱外执行时读脚本（在 worker 沙箱里由 ticket report 调用）：必须是工单目录里的普通文件、
    ≤8KB、UTF-8 文本。返回 {"path": 相对工单目录的路径, "script": 内容}，汇报时连内容一起存下来。"""
    root = os.path.realpath(workdir)
    real = os.path.realpath(os.path.join(workdir, path))
    if not real.startswith(root + os.sep):
        raise TicketError("--run 的脚本要放在工单目录里：%s" % path)
    if not os.path.isfile(real):
        raise TicketError("--run 的脚本不存在：%s" % path)
    if os.path.getsize(real) > RUN_MAX_BYTES:
        raise TicketError("--run 的脚本超过 %dKB，拆小一点，只放必须在沙箱外做的步骤" % (RUN_MAX_BYTES // 1024))
    try:
        script = Path(real).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise TicketError("--run 的脚本读不了（要是 UTF-8 文本）：%s" % e)
    if not script.strip():
        raise TicketError("--run 的脚本是空的")
    return {"path": os.path.relpath(real, root), "script": script}


def write_spool(spool_dir: str, status: str, summary: str, artifacts: Iterable[str] = (),
                ask: Optional[str] = None, run: Optional[dict] = None) -> Path:
    _validate_report(status, summary, run)
    d = Path(spool_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = "%020d-%d.json" % (time.time_ns(), os.getpid())
    tmp = d / (".tmp-" + name)
    tmp.write_text(json.dumps({"status": status, "summary": summary, "artifacts": list(artifacts),
                               "ask": ask, "run": run}, ensure_ascii=False), encoding="utf-8")
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
    run = d.get("run")
    if run is not None:
        if (not isinstance(run, dict) or not isinstance(run.get("path"), str) or len(run["path"]) > 1000
                or not isinstance(run.get("script"), str) or not run["script"].strip()
                or len(run["script"].encode("utf-8")) > RUN_MAX_BYTES):
            raise TypeError("bad run")


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
                           data.get("artifacts") or [], data.get("ask"), run=data.get("run"))
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
               artifacts: Iterable[str] = (), ask: Optional[str] = None, run: Optional[dict] = None) -> None:
    _validate_report(status, summary, run)
    arts = list(artifacts)
    if run is not None and not ask:
        ask = RUN_DEFAULT_ASK
    with db.tx(con):
        t = get(con, ticket_id)
        now = db.now()
        rid = con.execute("INSERT INTO reports(ticket_id, status, summary, artifacts_json, ask, created_at)"
                          " VALUES(?,?,?,?,?,?)",
                          (ticket_id, status, summary, json.dumps(arts, ensure_ascii=False), ask, now)).lastrowid
        if status != "progress":
            # 新的终态汇报取代之前还没批准的申请：用户只能批准最新这一份
            con.execute("UPDATE runs SET status='superseded' WHERE ticket_id=? AND status='pending'", (ticket_id,))
        if run is not None:
            run_id = con.execute("INSERT INTO runs(ticket_id, report_id, path, script, created_at) VALUES(?,?,?,?,?)",
                                 (ticket_id, rid, run["path"], run["script"], now)).lastrowid
            run = dict(run, id=run_id, timeout=config.run_timeout())
            con.execute("INSERT INTO events(ticket_id, kind, detail, created_at) VALUES(?,?,?,?)",
                        (ticket_id, "run_requested", "R%d %s" % (run_id, run["path"]), now))
        msg = templates.report_message(ticket_id, status, summary, arts, ask, run)
        if not msg:
            return
        card = None
        if status != "progress":  # 终态汇报发结果卡片；文字留作卡片发不出去时的兜底
            started = t["started_at"]
            card = templates.result_card(
                ticket_id, t["title"], t["workdir"], status, summary, arts, ask,
                phases(con, ticket_id, started or now), now - started if started else None, run=run)
            # 开了自动压缩时不提示「会话很长、换新会话」：上下文会自动压下来，会话累计成本只增不减，提示会一直挂着
            hint = (None if config.compact_tokens() else
                    templates.cost_hint(ticket_id, t["session_cost"] or 0, config.cost_hint_usd()))
            if hint and status != "dropped":
                card["body"]["elements"].insert(len(card["body"]["elements"]) - 1, templates.note(hint))
                msg += "\n" + hint
        # 视频产物在发件箱发卡时处理：传云空间成功就放进卡片里，失败才另排一条视频消息（outbox.resolve_card_videos）
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


RESUME_TEXT = ("用户恢复了这张已停止的工单。上一轮是被用户停掉的，进程已经结束，后台进程也被清理了。"
               "先检查工单目录、结果.md 和已有产物，确认停下时做到哪一步、有没有做了一半的外部动作，再接着做。")
FRESH_TEXT = ("这一轮换了新会话：上一个会话无法继续（例如模型接口反复报同一个错误）。你看不到之前的对话，"
              "先读工单目录里的 结果.md、过程.log 和已有产物，弄清做到哪一步，再接着做；不要重复已经完成的外部动作。")


def release(con: sqlite3.Connection, ticket_id: str) -> str:
    """放开暂停；已停止的单也可以用它恢复（同一个会话接着做，issue #18）。返回原来的状态。"""
    with db.tx(con):
        st = get(con, ticket_id)["state"]
        if st not in ("held", "stopped"):
            raise TicketError("%s 不在暂停中，也没有停止" % ticket_id)
        touch(con, ticket_id, state="idle", retries=0, retry_at=0, outage_since=None)
        add_input(con, ticket_id, "release", "用户已放开暂停，可以继续。" if st == "held" else RESUME_TEXT)
    return st


def restart(con: sqlite3.Connection, ticket_id: str) -> None:
    """换新会话从头续做（issue #14：会话被不可恢复的错误毒化后，只能另开新单、丢掉工单目录）。
    工单号、目录、授权都不变；新会话首轮带上原话（任务卡）和历次追问。"""
    with db.tx(con):
        t = get(con, ticket_id)
        if t["state"] in ("running", "queued"):
            raise TicketError("%s 正在运行或排队，先停止或暂停再换会话" % ticket_id)
        asks = con.execute("SELECT text FROM inputs WHERE ticket_id=? AND kind='followup' ORDER BY id",
                           (ticket_id,)).fetchall()
        text = FRESH_TEXT
        if asks:
            shown = asks[-10:]
            text += "\n\n用户在之前会话里的追问（按时间顺序%s）：\n" % (
                "，只列最近 10 条" if len(asks) > 10 else "")
            text += "\n".join("%d. %s" % (i + 1, templates.short(a["text"], 500)) for i, a in enumerate(shown))
        # 之前没投递的输入已经包含在上面的追问里，标掉，免得新会话里重复
        con.execute("UPDATE inputs SET status='delivered' WHERE ticket_id=? AND status='pending'", (ticket_id,))
        touch(con, ticket_id, session_id=str(uuid.uuid4()), session_started=0, state="idle",
              retries=0, retry_at=0, outage_since=None, session_cost=0)
        add_input(con, ticket_id, "fresh", text)


def pending_run(con: sqlite3.Connection, ticket_id: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE ticket_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                       (ticket_id,)).fetchone()


def approve(con: sqlite3.Connection, ticket_id: str) -> int:
    """用户批准最新一份沙箱外执行申请；守护进程下一个 tick 开始执行。返回申请编号。"""
    with db.tx(con):
        t = get(con, ticket_id)
        run = pending_run(con, ticket_id)
        if run is None:
            raise TicketError("%s 没有待批准的沙箱外执行" % ticket_id)
        if t["state"] not in ("idle", "failed"):
            raise TicketError("%s 当前状态为 %s，不能执行（先放开或恢复）" % (ticket_id, t["state"]))
        con.execute("UPDATE runs SET status='approved', approved_at=? WHERE id=?", (db.now(), run["id"]))
        con.execute("INSERT INTO events(ticket_id, kind, detail, created_at) VALUES(?,?,?,?)",
                    (ticket_id, "run_approved", "R%d" % run["id"], db.now()))
        touch(con, ticket_id, retry_at=0)
    return run["id"]


def expire_pending_runs(con: sqlite3.Connection, ticket_id: str) -> None:
    """worker 开新一轮（用户追问、放开等）时，之前没批准的申请作废：worker 会根据新情况重新判断。"""
    con.execute("UPDATE runs SET status='expired' WHERE ticket_id=? AND status='pending'", (ticket_id,))


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

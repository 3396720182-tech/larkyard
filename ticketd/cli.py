from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import card, config, core, db, executors, safefs, templates, worker

HEARTBEAT_STALE = 60


class LookupMiss(Exception):
    pass


def _text(args, name: str) -> str:
    value = getattr(args, name)
    path = getattr(args, name + "_file")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise core.TicketError("读不了 %s：%s" % (path, e))
    if value is None:
        raise core.TicketError("缺少 --%s 或 --%s-file" % (name, name))
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ticket", description="ticketd 工单命令")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="派单（bridge 会话用）")
    n.add_argument("--chat", required=True)
    n.add_argument("--message", required=True)
    n.add_argument("--title", required=True)
    n.add_argument("--slug", required=True)
    n.add_argument("--request")
    n.add_argument("--request-file")
    n.add_argument("--ack", required=True)
    n.add_argument("--note")
    n.add_argument("--grant", action="append", default=[])
    n.add_argument("--sender")
    n.add_argument("--executor", choices=executors.NAMES,
                   help="worker 用哪个 agent（默认 TICKETD_EXECUTOR，未设为 claude）")

    s = sub.add_parser("send", help="把用户追问转给工单")
    s.add_argument("ticket")
    s.add_argument("--message", required=True)
    s.add_argument("--text")
    s.add_argument("--text-file")

    lk = sub.add_parser("lookup", help="按消息 id 查工单")
    lk.add_argument("--message", required=True)

    r = sub.add_parser("report", help="worker 汇报")
    r.add_argument("--ticket", default=os.environ.get("TICKETD_TICKET"))
    r.add_argument("--status", required=True, choices=core.STATUSES)
    r.add_argument("--summary", required=True)
    r.add_argument("--artifact", action="append", default=[])
    r.add_argument("--ask")

    for name in ("stop", "hold", "release", "show"):
        x = sub.add_parser(name)
        x.add_argument("ticket")
    g = sub.add_parser("grant")
    g.add_argument("ticket")
    g.add_argument("path")
    sub.add_parser("ls")
    return p


def _report_line(con, ticket_id: str) -> str:
    last = core.last_report(con, ticket_id)
    return ("%s %s" % (last["status"], last["summary"])) if last else "—"


def _liveness_warning() -> str:
    try:
        age = time.time() - config.heartbeat_path().stat().st_mtime
    except OSError:
        why = "无心跳"
    else:
        if age <= HEARTBEAT_STALE:
            return ""
        why = "心跳 %d 秒前" % age
    return "⚠️ ticketd 守护进程似乎没在运行（%s），工单已登记，恢复前不会执行。" % why


def _with_warning(out: str) -> str:
    warn = _liveness_warning()
    return out + "\n" + warn if warn else out


def cmd_new(con, a) -> str:
    tid = core.create_ticket(con, chat_id=a.chat, message_id=a.message, title=a.title, slug=a.slug,
                             request_text=_text(a, "request"), ack_text=a.ack, note=a.note,
                             grant_paths=a.grant, sender=a.sender, executor=a.executor)
    return _with_warning(templates.ack_message(tid, a.title))


def cmd_send(con, a) -> str:
    core.send_followup(con, a.ticket, a.message, _text(a, "text"))
    if core.get(con, a.ticket)["state"] == "held":
        return _with_warning("已转给 %s（暂停中，放开后处理）" % a.ticket)
    return _with_warning("已转给 %s" % a.ticket)


def cmd_lookup(con, a) -> str:
    tid = core.lookup(con, a.message)
    if not tid:
        raise LookupMiss()
    return tid


def cmd_report(con, a) -> str:
    if not a.ticket:
        raise core.TicketError("缺少 --ticket（或环境变量 TICKETD_TICKET）")
    core.add_report(con, a.ticket, a.status, a.summary, a.artifact, a.ask)
    return "已记录 %s 的汇报：%s" % (a.ticket, a.status)


MAX_LISTED = 6


def _new_files(workdir: str, since: float) -> list:
    """本轮（since 之后）工单目录里新写的文件，相对路径，按修改时间倒序。跳过隐藏文件、ticketd 内部文件，不跟随链接。"""
    found = []
    try:
        for root, dirs, files in os.walk(workdir, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if root.count(os.sep) - workdir.rstrip(os.sep).count(os.sep) > 3:
                dirs[:] = []
            for f in files:
                if f.startswith(".") or f == "过程.log":
                    continue
                p = os.path.join(root, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_mtime >= since:
                    found.append((st.st_mtime, os.path.relpath(p, workdir)))
            if len(found) > 500:
                break
    except OSError:
        return []
    return [rel for _, rel in sorted(found, reverse=True)]


def _stop_summary(con, t, verb: str) -> str:
    """停止 / 暂停时的交代（issue #17）：停在哪一步、这一轮写了哪些文件、怎么接着做。
    主会话把这段原样回复给用户；信息都是 ticketd 自己有的，不问 worker。"""
    now = time.time()
    lines = ["%s %s" % (verb, t["id"])]
    if t["state"] == "running":
        since = t["started_at"] or 0
        prog = core.last_progress(con, t["id"], since)
        where = []
        if prog:
            where.append("阶段「%s」" % templates.short(prog["summary"], 30))
        if t["last_step"]:
            ago = templates.ago(now - t["last_action_at"]) if t["last_action_at"] else ""
            where.append("最近：%s%s" % (templates.short(t["last_step"], 40), "（%s）" % ago if ago else ""))
        if where:
            lines.append("停在：" + " · ".join(where))
        files = _new_files(t["workdir"], since)
        if files:
            shown = "、".join(templates.short(f, 40) for f in files[:MAX_LISTED])
            more = " 等 %d 个" % len(files) if len(files) > MAX_LISTED else ""
            lines.append("这一轮写了：%s%s（目录 %s）" % (shown, more, templates.nice_path(t["workdir"])))
    if verb.startswith("⏹️"):
        lines.append("想接着做：说「恢复 %s」，会用同一个会话继续" % t["id"])
    else:
        lines.append("你放开前不会继续；放开时说「继续 %s」" % t["id"])
    lines.append("终端接管：%s" % executors.get(worker.executor_of(t)).takeover_hint(t))
    return "\n".join(lines)


def cmd_stop(con, a) -> str:
    t = core.get(con, a.ticket)
    core.stop(con, a.ticket)
    return _stop_summary(con, t, "⏹️ 已停止")


def cmd_hold(con, a) -> str:
    t = core.get(con, a.ticket)
    core.hold(con, a.ticket)
    return _stop_summary(con, t, "⏸️ 已暂停")


def cmd_release(con, a) -> str:
    was = core.release(con, a.ticket)
    if was == "stopped":
        return "▶️ 已恢复 %s，用同一个会话接着做" % a.ticket
    return "▶️ 已放开 %s" % a.ticket


def cmd_grant(con, a) -> str:
    p = core.grant(con, a.ticket, a.path)
    return "已授权 %s 可写：%s" % (a.ticket, p)


def cmd_ls(con, a) -> str:
    rows = core.list_tickets(con)
    if not rows:
        return "（没有工单）"
    lines = []
    now = time.time()
    for t in rows:
        v = card.view(con, t, now)
        extra = ""
        if t["state"] == "running":
            extra = "  阶段：%s" % (v["phase"] or "—")
            if v["elapsed"] is not None:
                extra += "  用时 %s" % templates.dur(v["elapsed"])
        lines.append("%s  %-7s  %s  %s%s  可写：%s  最近：%s" % (
            t["id"], t["state"], t["title"], v["label"], extra,
            templates.scope_text(core.grants(con, t["id"])), _report_line(con, t["id"])))
    return "\n".join(lines)


LOG_TAIL_BYTES = 64 * 1024


def _log_tail(log: Path) -> list:
    try:
        fd = safefs.open_read(log)
        if fd is None:
            return ["（还没有过程日志）"]
        try:
            size = os.fstat(fd).st_size
            data = safefs.read_at(fd, max(0, size - LOG_TAIL_BYTES), LOG_TAIL_BYTES)
        finally:
            os.close(fd)
    except OSError:
        return ["（过程日志不可读）"]
    return data.decode("utf-8", errors="replace").splitlines()[-20:]


def _status_lines(con, t) -> list:
    """和状态卡同一套口径：显示状态、阶段（worker 自述）、最近一步和最后动作（ticketd 观测）。"""
    now = time.time()
    v = card.view(con, t, now)
    line = "%s  %s  状态：%s（%s）" % (t["id"], t["title"], t["state"], v["label"])
    if v["elapsed"] is not None:
        line += "  用时 %s" % templates.dur(v["elapsed"])
    out = [line]
    if t["state"] == "running":
        out.append("阶段：%s" % (templates.trail(v["phases"], done=False) if v["phases"] else "worker 还没报告阶段"))
        if v["last_action_at"]:
            out.append("最后动作：%s（%s）  最近一步：%s" % (
                templates.hm(v["last_action_at"]), templates.ago(now - v["last_action_at"]), v["last_step"] or "—"))
        if v["bg_wait"]:
            out.append("本轮已结束，等后台任务：%s（结束后 worker 会自动接着做）" % v["bg_wait"])
    return out


def cmd_show(con, a) -> str:
    t = core.get(con, a.ticket)
    log = Path(t["workdir"]) / "过程.log"
    tail = _log_tail(log)
    info = _status_lines(con, t) + [
        "目录：%s" % t["workdir"],
        "执行器：%s" % worker.executor_of(t),
        "终端接管：%s" % executors.get(worker.executor_of(t)).takeover_hint(t),
        "可写：%s" % templates.scope_text(core.grants(con, t["id"])),
        "最近汇报：%s" % _report_line(con, t["id"]),
        "过程（最后 20 行）：",
    ]
    return "\n".join(info + tail)


COMMANDS = {
    "new": cmd_new, "send": cmd_send, "lookup": cmd_lookup, "report": cmd_report,
    "stop": cmd_stop, "hold": cmd_hold, "release": cmd_release, "grant": cmd_grant,
    "ls": cmd_ls, "show": cmd_show,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    con = None
    spool = os.environ.get("TICKETD_SPOOL")
    try:
        if args.cmd == "report" and spool:
            # worker 沙箱内：只写工单目录下的待收件，绝不打开数据库
            if args.status != "progress":
                core.check_media_artifacts(os.path.dirname(spool.rstrip("/")), args.artifact)
            core.write_spool(spool, args.status, args.summary, args.artifact, args.ask)
            print("已记录汇报：%s" % args.status)
            return 0
        con = db.connect()
        out = COMMANDS[args.cmd](con, args)
    except LookupMiss:
        return 1
    except core.TicketError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 2
    except Exception as e:
        print("错误：内部错误：%r" % (e,), file=sys.stderr)
        return 2
    finally:
        if con is not None:
            con.close()
    if out:
        print(out)
    return 0

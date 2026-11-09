from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import card, config, core, db, executors, metrics, safefs, templates, worker

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
    r.add_argument("--run", help="申请在沙箱外执行的脚本（工单目录里，≤8KB）；只能和 --status blocked 一起用")

    for name in ("stop", "hold", "release", "restart", "approve", "show"):
        x = sub.add_parser(name)
        x.add_argument("ticket")
    g = sub.add_parser("grant")
    g.add_argument("ticket")
    g.add_argument("path", nargs="?", help="可写目录（绝对路径）")
    g.add_argument("--local-net", action="store_true", help="放开本机网络：连本机端口、在本机监听（只对 Claude 单生效）")
    g.add_argument("--socket", help="放开一个 unix socket 路径（如 /var/run/docker.sock；只对 Claude 单生效）")
    sub.add_parser("ls")
    dr = sub.add_parser("doctor", help="配置自检：当前环境和守护进程看到的配置是否一致、主用户、心跳")
    dr.add_argument("--dm", action="store_true", help="让守护进程按它自己的配置真的私信你一条，等它发完报结果")
    st = sub.add_parser("stats", help="最近几天的运行概况（成本、需要你的次数、失败、事件）")
    st.add_argument("--days", type=float, default=7)
    st.add_argument("--backfill", action="store_true", help="先从老工单的 .stream.jsonl 补成本（只补没有记录的单）")
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
    run = core.read_run_script(core.get(con, a.ticket)["workdir"], a.run) if a.run else None
    core.add_report(con, a.ticket, a.status, a.summary, a.artifact, a.ask, run=run)
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


def cmd_approve(con, a) -> str:
    rid = core.approve(con, a.ticket)
    return "▶️ 已批准 %s 的沙箱外执行（R%d），马上开始，结果会交给 worker 接着做" % (a.ticket, rid)


def cmd_restart(con, a) -> str:
    core.restart(con, a.ticket)
    return "🔄 %s 换了新会话重新接着做（原话和历次追问已带上，工单目录不变）" % a.ticket


def cmd_grant(con, a) -> str:
    n = sum(bool(x) for x in (a.path, a.local_net, a.socket))
    if n != 1:
        raise core.TicketError("grant 一次只授权一样：目录、--local-net 或 --socket <路径>")
    if a.path:
        p = core.grant(con, a.ticket, a.path)
        return "已授权 %s 可写：%s" % (a.ticket, p)
    what = core.grant_net(con, a.ticket, local=a.local_net, socket=a.socket)
    out = "已授权 %s 使用%s" % (a.ticket, what)
    if worker.executor_of(core.get(con, a.ticket)) != "claude":
        out += "\n⚠️ 这是 Codex 单，Codex 的沙箱没有这个开关，授权不会生效"
    return out


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
        if t["cost_usd"]:
            extra += "  $%.2f" % t["cost_usd"]
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
    run = con.execute("SELECT * FROM runs WHERE ticket_id=? AND status IN ('pending','approved','running')"
                      " ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    if run is not None:
        what = {"pending": "待批准（用户明确同意后执行 `ticket approve %s`）" % t["id"],
                "approved": "已批准，马上执行", "running": "执行中"}[run["status"]]
        out.append("沙箱外执行 R%d（%s）：%s" % (run["id"], run["path"], what))
    return out


def cmd_doctor(con, a) -> str:
    """守护进程由 launchd / systemd 启动，只读 config.env；ticket 命令在 Claude Code 会话里跑，会先读环境变量
    （如 ~/.claude/settings.json 的 env）。两边不一致时，派单正常但守护进程的私信、执行会用错的值（2026-11-09 实例：
    TICKETD_OWNER 在 config.env 里是迁移带来的旧 open_id，周报私信报 open_id cross app）。"""
    lines, bad = [], 0
    names = sorted(set(k for k in os.environ if k.startswith("TICKETD_")) | set(
        k for k in config._file_values() if k.startswith("TICKETD_")))
    for k in names:
        env, file = os.environ.get(k), config.file_value(k)
        if env is not None and file is not None and env != file:
            bad += 1
            lines.append("❌ %s：当前环境和 config.env 不一致（守护进程用 config.env 里的）" % k)
        elif env is not None and file is None and k not in ("TICKETD_HOME", "TICKETD_SPOOL", "TICKETD_TICKET",
                                                              "TICKETD_RUN", "TICKETD_PYTHON"):
            lines.append("⚠️ %s：只在当前环境里有，config.env 没有，守护进程看不到" % k)
    lines += _workspace_drift()
    checked, mismatched = metrics.reconcile_costs(con)
    if mismatched:
        lines.append("❌ 成本账目有 %d 张单和原始记录对不上：%s" % (len(mismatched), "，".join(
            "%s 库里 $%.2f / 重算 $%.2f" % m for m in mismatched[:5])))
    elif checked:
        lines.append("✅ 成本账目和原始记录一致（核对了 %d 张单）" % checked)
    owner = config.file_value("TICKETD_OWNER") or ""
    if not owner.startswith("ou_"):
        bad += 1
        lines.append("❌ config.env 里没有有效的 TICKETD_OWNER（ou_ 开头）：只为主用户派单、私信告警都会失败")
    try:
        age = time.time() - config.heartbeat_path().stat().st_mtime
        if age > 60:
            bad += 1
            lines.append("❌ 守护进程心跳 %d 秒前，可能没在运行" % age)
        else:
            lines.append("✅ 守护进程在运行（心跳 %d 秒前）" % age)
    except OSError:
        bad += 1
        lines.append("❌ 没有守护进程心跳文件，服务可能没装或没启动")
    if getattr(a, "dm", False):
        ok, note = _doctor_dm(con)
        bad += not ok
        lines.append(("✅ " if ok else "❌ ") + note)
    bad = sum(1 for line in lines if line.startswith("❌"))
    lines.insert(0, "配置自检：%s" % ("没有发现问题" if not bad else "%d 个问题" % bad))
    return "\n".join(lines)


def _bridge_workspace() -> str:
    """bridge 的默认工作目录：先看 TICKETD_WORKSPACE，再尽力读 bridge 的配置（只读；格式是上游的，读不懂就算了）。"""
    ws = config.workspace()
    if ws:
        return os.path.expanduser(ws)
    try:
        import json
        home = os.environ.get("LARK_CHANNEL_HOME") or os.path.expanduser("~/.lark-channel")
        cfg = json.loads(Path(home, "config.json").read_text(encoding="utf-8"))
        return cfg["profiles"][cfg["activeProfile"]]["workspaces"]["default"]
    except Exception:
        return ""


def _workspace_drift() -> list:
    """主会话约定（工作目录的 CLAUDE.md）是不是本仓库模板渲染出来的最新版（2026-11-08 发现线上那份停在 11-01）。"""
    ws = _bridge_workspace()
    if not ws:
        return ["⚠️ 找不到 bridge 的工作目录，没检查主会话约定（可以在 config.env 里写 TICKETD_WORKSPACE=<目录>）"]
    live = Path(ws) / "CLAUDE.md"
    try:
        want = (config.REPO / "workspace" / "CLAUDE.md.tmpl").read_text(encoding="utf-8").replace("@LARKYARD@", str(config.REPO))
        have = live.read_text(encoding="utf-8")
    except OSError as e:
        return ["❌ 读不了主会话约定（%s）：%s" % (live, e)]
    if have == want:
        return ["✅ 主会话约定和本仓库的模板一致"]
    return ["❌ 主会话约定（%s）和本仓库的模板不一致，可能落后了：确认没有手改的内容后，执行 "
            "scripts/setup-workspace.sh %s --force 同步（会先备份）" % (live, ws)]


def _doctor_dm(con, timeout: float = 45.0):
    """守护进程视角的私信测试：只放一行占位收件人的消息，收件人和发送都由守护进程自己决定。"""
    rid = metrics.send_owner(con, "🔧 ticket doctor 测试私信：守护进程按它自己的配置发出。收到即可，不用回复。")
    end = time.time() + timeout
    while time.time() < end:
        row = con.execute("SELECT status, last_error FROM outbox WHERE id=?", (rid,)).fetchone()
        if row["status"] == "sent":
            return True, "守护进程发出了测试私信（去飞书私聊里确认收到）"
        if row["status"] == "dead":
            return False, "测试私信没发出：%s" % (row["last_error"] or "")[:200]
        time.sleep(1)
    return False, "%d 秒内守护进程没有发出测试私信（服务没在运行，或发送在重试：看 ~/.ticketd/logs/alerts.log）" % timeout


def cmd_stats(con, a) -> str:
    out = []
    if a.backfill:
        out.append("补了 %d 张老工单的成本" % metrics.backfill_costs(con))
    out.append(metrics.stats(con, a.days))
    return "\n".join(out)


def _cost_text(t) -> str:
    try:
        cost, sess = t["cost_usd"] or 0, t["session_cost"] or 0
    except (KeyError, IndexError):
        return ""
    if not cost:
        return ""
    return "本单约 $%.2f（当前会话累计 $%.2f）" % (cost, sess)


def _ctx_text(con, t) -> str:
    n = con.execute("SELECT COUNT(*) ok, SUM(outcome='compact_failed') bad FROM rounds WHERE ticket_id=?"
                    " AND outcome IN ('compact','compact_failed')", (t["id"],)).fetchone()
    size = "上一轮约 %d 万 token" % (t["ctx_tokens"] // 10000) if t["ctx_tokens"] else "—"
    if n["ok"]:
        size += "；压缩过 %d 次%s" % (n["ok"], "（失败 %d 次）" % n["bad"] if n["bad"] else "")
    return size


def cmd_show(con, a) -> str:
    t = core.get(con, a.ticket)
    log = Path(t["workdir"]) / "过程.log"
    tail = _log_tail(log)
    info = _status_lines(con, t) + [
        "目录：%s" % t["workdir"],
        "执行器：%s" % worker.executor_of(t),
        "成本：%s" % (_cost_text(t) or "—"),
        "上下文：%s" % _ctx_text(con, t),
        "worker PATH：%s" % worker.worker_env(t).get("PATH", ""),
        "终端接管：%s" % executors.get(worker.executor_of(t)).takeover_hint(t),
        "可写：%s" % templates.scope_text(core.grants(con, t["id"])),
        "最近汇报：%s" % _report_line(con, t["id"]),
        "过程（最后 20 行）：",
    ]
    return "\n".join(info + tail)


COMMANDS = {
    "new": cmd_new, "send": cmd_send, "lookup": cmd_lookup, "report": cmd_report,
    "stop": cmd_stop, "hold": cmd_hold, "release": cmd_release, "restart": cmd_restart, "approve": cmd_approve, "grant": cmd_grant,
    "ls": cmd_ls, "show": cmd_show, "stats": cmd_stats, "doctor": cmd_doctor,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    con = None
    spool = os.environ.get("TICKETD_SPOOL")
    try:
        if args.cmd == "report" and spool:
            # worker 沙箱内：只写工单目录下的待收件，绝不打开数据库
            workdir = os.path.dirname(spool.rstrip("/"))
            if args.status != "progress":
                core.check_media_artifacts(workdir, args.artifact)
            run = core.read_run_script(workdir, args.run) if args.run else None
            core.write_spool(spool, args.status, args.summary, args.artifact, args.ask, run)
            print("已记录汇报：%s" % args.status)
            if run:
                print("沙箱外执行的申请已登记（%s）。现在结束本轮：用户批准后 ticketd 会执行它，"
                      "退出码和输出会作为下一轮的输入交给你。" % run["path"])
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

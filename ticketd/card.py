"""工单状态卡：派单后由 ticketd 发一张飞书卡片，运行中只原地编辑这一张（编辑不产生推送提醒）。

一张单在聊天里的样子（尽量少占位置、不重复）：
- bridge 的一行开单回执（`ticket new` 的输出）；
- 状态卡：运行中显示阶段轨迹和最近动作；终态汇报直接覆盖到这张卡上（T0017：不另发结果消息）；
- 只有需要用户回答时（blocked 或带 --ask），才另推一行文字提醒，免得问题被漏看。
追问等后续轮次默认不发新状态卡，结果作为新消息推送；跑超过 round_card_delay 还没结果，
才在追问下面另发一张状态卡，结果再覆盖到那一张上。

- 「阶段」来自 worker 的 `ticket report --status progress`（模型自述）；
  「最近」来自 stream 事件（ticketd 观测到的事实）。两者对不上就是异常信号。
- 只在里程碑变化（状态、阶段、汇报、结果送达）时立即刷新，运行中另外每 card_heartbeat 秒心跳刷新一次。
- 不用流式卡片（服务端 10 分钟后失效），用普通交互卡片 + 消息 PATCH 接口更新。
"""
from __future__ import annotations

import json
from typing import Optional

from . import config, core, db, outbox, templates
from .templates import dur, hm, ago, note, short, text, hr

CARD_GIVEUP = 24 * 3600.0
MIN_INTERVAL = 10.0          # 同一张卡两次刷新的最小间隔（飞书单条消息更新限 5 QPS）
PATCH_WINDOW = 13.5 * 24 * 3600  # 飞书只能编辑 14 天内的消息
PATCH_MAX_ATTEMPTS = 30
SENDING_GRACE = 30.0         # 结果刚进发件箱：先别刷卡，等结果发出后直接收起，免得卡片闪一下


def _delivery(con, t, report_id: int) -> Optional[str]:
    """这份汇报对应的推送消息的状态：pending / sent / dead / oncard（已覆盖到本卡上）；没有推送消息时为 None。"""
    row = con.execute("SELECT status, sent_message_id FROM outbox WHERE report_id=? ORDER BY id DESC LIMIT 1",
                      (report_id,)).fetchone()
    if row is None:
        return None
    if row["status"] == "sent" and row["sent_message_id"] and row["sent_message_id"] == t["card_message_id"]:
        return "oncard"
    return row["status"]


def live_card(t, now: float) -> Optional[str]:
    """本轮可以覆盖结果的状态卡的 message_id；没有时为 None（结果改为发新消息）。
    还开着的卡就是当前这一轮的卡；已收起的卡只有属于本轮（同一轮又交了一份终态汇报）时才再覆盖。"""
    if not t["card_wanted"] or not t["card_message_id"]:
        return None
    if now - (t["card_sent_at"] or t["created_at"]) > PATCH_WINDOW:
        return None
    if t["card_closed"] and not (t["card_round"] is not None and t["card_round"] == t["started_at"]):
        return None
    return t["card_message_id"]


def deliver(con, ticket_id: str, result: dict, patch=None, now: Optional[float] = None):
    """把结果卡片覆盖到本轮的状态卡上。返回 (是否成功, message_id 或错误信息, 是否永久失败)；
    没有可覆盖的卡（或卡已无法编辑）时返回 None，由发件箱改发新消息。"""
    patch = patch or outbox.patch_card
    now = db.now() if now is None else now
    t = core.get(con, ticket_id)
    mid = live_card(t, now)
    if mid is None:
        return None
    ok, info, permanent = patch(mid, result)
    if ok:
        con.execute("UPDATE tickets SET card_closed=1, card_round=?, card_key=?, card_at=?, card_attempts=0,"
                    " card_retry_at=0 WHERE id=?", (t["started_at"], "result", now, ticket_id))
        return True, mid, False
    if permanent:
        # 卡片已经编辑不了（被删、超期等）：以后不再动它，这份结果改发新消息
        con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (ticket_id,))
        outbox.alert("结果覆盖状态卡失败，改发新消息（%s）：%s" % (ticket_id, info))
        return None
    return False, info, False


def view(con, t, now: float) -> dict:
    """把工单当前状态整理成状态卡和 `ticket show` 共用的视图。

    form：live（运行/排队/暂停等，可变）、sending（已汇报、结果在发）、
    result（结果卡已覆盖到本卡上，之后不再改动）、
    folded（结果已单独推送，卡片收起）、final（没有单独推送的结果，终态直接写在卡上）。"""
    tid, state = t["id"], t["state"]
    started = t["started_at"]
    since = started or 0
    pending = con.execute("SELECT 1 FROM inputs WHERE ticket_id=? AND status='pending' LIMIT 1",
                          (tid,)).fetchone() is not None
    final = core.last_final_report(con, tid, since) if started else None
    progress = core.last_progress(con, tid, since) if started else None
    # 新守护进程开轮时就把 last_action_at 设为开轮时间；为空说明这一轮没被观测（如老版本守护进程），不推断卡死
    last_action = t["last_action_at"]
    v = {
        "state": state, "final": final, "active": False, "stalled": False, "form": "live",
        "phase": progress["summary"] if progress else None,
        "phase_at": progress["created_at"] if progress else None,
        "phases": core.phases(con, tid, since) if started else [],
        "last_step": t["last_step"], "last_action_at": last_action or None, "elapsed": None,
        "queued_note": None,
    }
    if state == "stopped":
        v["label"], v["color"] = "⏹️ 已停止", "grey"
    elif state == "held":
        v["label"], v["color"] = "⏸️ 已暂停", "grey"
    elif state == "running":
        v["active"] = True
        v["elapsed"] = now - started if started else None
        if final is not None:
            v["label"], v["color"] = "📮 已汇报，正在发送结果", "turquoise"
        elif last_action and now - last_action >= config.stall_seconds():
            v["stalled"] = True
            v["label"], v["color"] = "⚠️ 疑似卡住", "orange"
        else:
            v["label"], v["color"] = "🔄 进行中", "blue"
    elif state == "queued" or pending:
        v["active"] = True
        if t["retry_at"] and t["retry_at"] > now:
            v["label"], v["color"] = "🔁 临时故障，等待重试", "wathet"
            v["queued_note"] = "临时故障，%s 自动重试" % hm(t["retry_at"])
        else:
            v["label"], v["color"] = "🕒 排队中", "wathet"
    else:
        # 只看本轮的终态汇报（之后又报了 progress 也不影响）
        st = "failed" if state == "failed" else (final["status"] if final is not None else None)
        v["label"], v["color"] = (templates.LABELS[st], templates.FINAL[st][2]) if st in templates.FINAL \
            else ("空闲", "grey")
    f = v["final"]
    if f is not None:
        if started:
            v["elapsed"] = max(0.0, f["created_at"] - started)
        if f["status"] in templates.FINAL:
            v["label"], v["color"] = templates.LABELS[f["status"]], templates.FINAL[f["status"]][2]
        d = _delivery(con, t, f["id"])
        v["form"] = {"pending": "sending", "sent": "folded", "oncard": "result"}.get(d, "final")
        if v["form"] == "sending":
            v["label"], v["color"] = "📮 已汇报，正在发送结果", "turquoise"
    fid = f["id"] if f is not None else None
    # 收起/终态的卡片内容和进程是否还在无关：签名里不带 state，进程退出时不必再刷一次
    v["key"] = "|".join(str(x) for x in (
        "" if v["form"] in ("folded", "final", "result") else state, v["form"], v["label"], v["phase"], fid))
    return v


def _activity(v: dict, now: float) -> Optional[dict]:
    if not v["last_action_at"]:
        return None
    if v["stalled"]:
        s = "⚠️ 已 %s没有新动作" % ago(now - v["last_action_at"]).replace("前", "")
        if v["last_step"]:
            s += " · 最近：%s" % short(v["last_step"], 40)
        return note(s, color="red")
    if not v["last_step"]:
        return None
    return note("最近：%s · %s" % (short(v["last_step"], 50), ago(now - v["last_action_at"])))


def render(t, v: dict, grant_paths, now: float) -> dict:
    """渲染卡片 JSON（Card 2.0）。worker 可控的文字一律放 plain_text，不经 Markdown 解析。"""
    tid = t["id"]
    title = templates.title_of(tid, t["title"])
    if v["form"] == "folded":
        # 结果已经作为新消息推送（用时、过程都在结果卡上）：这张卡收起成一行灰色标题，不重复任何内容
        return templates.card("%s %s" % (tid, v["label"]), title, v["label"] + " · 结果见下方 ↓", "grey", [])
    sub = v["label"]
    if v["elapsed"] is not None:
        sub += " · %s %s" % ("已用" if v["state"] == "running" and v["final"] is None else "用时", dur(v["elapsed"]))
    els = []
    if v["form"] in ("final", "result"):  # result 正常由 deliver 直接覆盖、不经这里渲染；兜底按终态直写
        f = v["final"]
        els += templates.result_elements(f["status"], f["summary"], _artifacts(f), f["ask"], t["workdir"])
        if v["phases"]:
            els.append(note("过程：" + templates.trail(v["phases"], done=True, limit=6)))
    else:
        if v["state"] == "running" and v["final"] is None:
            els.append(text(templates.trail(v["phases"], done=False) or "● 刚开始"))
            act = _activity(v, now)
            if act:
                els.append(act)
        elif v["phases"]:
            els.append(text(templates.trail(v["phases"], done=v["final"] is not None)))
        if v["queued_note"]:
            els.append(text(v["queued_note"]))
    if els:
        els.append(hr())
    goal = templates.goal_text(t["ack_text"])
    if goal:
        els.append(note("目标：" + short(goal, 120)))
    els.append(note("可写：%s · %s 更新 · 引用此卡可追问" % (
        short(templates.scope_text(grant_paths), 80), hm(now))))
    preview = "%s %s" % (tid, v["label"])
    if v["state"] == "running" and v["phase"] and v["final"] is None:
        preview += "：" + short(v["phase"], 30)
    return templates.card(preview, title, sub, v["color"], els)


def _artifacts(report) -> list:
    try:
        arts = json.loads(report["artifacts_json"] or "[]")
    except ValueError:
        return []
    return [a for a in arts if isinstance(a, str)] if isinstance(arts, list) else []


def _failed(con, t, info: str, permanent: bool, now: float, what: str, creating: bool) -> None:
    attempts = t["card_attempts"] + 1
    round_card = creating and t["card_message_id"] is not None
    if round_card:
        giveup = permanent or attempts >= PATCH_MAX_ATTEMPTS
    elif creating:
        giveup = permanent or now - t["created_at"] > CARD_GIVEUP
    else:
        giveup = (permanent or attempts >= PATCH_MAX_ATTEMPTS
                  or now - (t["card_sent_at"] or t["created_at"]) > PATCH_WINDOW)
    if giveup and round_card:
        # 追问轮次的卡发不出去：只放弃这一轮（记成这一轮已处理），以后的轮次照常
        con.execute("UPDATE tickets SET card_round=?, card_attempts=0, card_retry_at=0 WHERE id=?",
                    (t["started_at"], t["id"]))
        outbox.alert("状态卡%s失败，本轮放弃（%s）：%s" % (what, t["id"], info))
        return
    if giveup:
        con.execute("UPDATE tickets SET card_wanted=0, card_attempts=? WHERE id=?", (attempts, t["id"]))
        outbox.alert("状态卡%s失败，已放弃（%s）：%s" % (what, t["id"], info))
        return
    con.execute("UPDATE tickets SET card_attempts=?, card_retry_at=? WHERE id=?",
                (attempts, now + min(5 * 2 ** (attempts - 1), 300), t["id"]))
    if attempts == 3:
        outbox.alert("状态卡%s连续失败 3 次（%s）：%s" % (what, t["id"], info))


def _create(con, t, v, now: float, create, reply_to: str, idem: str) -> str:
    card = render(t, v, core.grants(con, t["id"]), now)
    ok, info, permanent = create(reply_to, card, idem)
    if not ok and outbox._reply_gone(info) and (t["chat_id"] or "").startswith("oc_"):
        # 被回复的消息已撤回（230011）：状态卡改为直接发到会话里，不然整单都看不到进度
        ok, info, permanent = create(t["chat_id"], card, idem)
    if ok and not info:
        # 可能其实已经发出去了：用同一个幂等键重试，飞书会返回同一条消息的 id
        ok, info, permanent = False, "发卡成功但没拿到 message_id，稍后用同一幂等键重试", False
    if not ok:
        _failed(con, t, info, permanent, now, "发送", creating=True)
        return info
    with db.tx(con):
        con.execute("UPDATE tickets SET card_message_id=?, card_key=?, card_at=?, card_attempts=0, card_retry_at=0,"
                    " card_closed=?, card_round=?, card_sent_at=? WHERE id=?",
                    (info, v["key"], now, 1 if v["form"] in ("folded", "final") else 0, t["started_at"], now,
                     t["id"]))
        con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (info, t["id"]))
    return ""


def _user_round(con, tid: str) -> bool:
    """本轮是不是用户发起的（追问、授权、放开）；服务重启恢复、临时故障重试这类轮次不另发卡片。"""
    row = con.execute("SELECT kind FROM inputs WHERE ticket_id=? AND status='delivered' ORDER BY id DESC LIMIT 1",
                      (tid,)).fetchone()
    return row is not None and row["kind"] in ("assignment", "followup", "grant", "release")


def _refresh_one(con, t, now: float, create, patch) -> str:
    """返回 lark-cli 的错误信息（没调用或成功时为空串），供调用方判断是否网络故障。"""
    tid = t["id"]
    v = view(con, t, now)
    if t["card_message_id"] is None:
        if not v["active"] or v["final"] is not None:
            # 卡片还没发出去单就已经结束/已有结果了（如守护进程停过、发卡一直失败）：不再补发，
            # 免得卡片排到结果后面、还写着「结果见下方」
            con.execute("UPDATE tickets SET card_wanted=0 WHERE id=?", (tid,))
            return ""
        idem = "ticketd-card-%s-%d" % (tid, int(t["created_at"] * 1000) % 10 ** 10)
        return _create(con, t, v, now, create, t["origin_message_id"], idem)
    if t["card_closed"]:
        same_round = t["card_round"] is not None and t["card_round"] == t["started_at"]
        if same_round and v["form"] in ("folded", "final") and v["key"] != t["card_key"]:
            pass  # 同一轮又交了一份终态汇报：按最新的结果重新收起（下面照常 PATCH）
        elif (not same_round and v["state"] == "running" and v["final"] is None and t["started_at"]
              and now - t["started_at"] >= config.round_card_delay() and _user_round(con, tid)):
            # 上一张卡已经收起。追问等用户发起的轮次跑久了还没结果，才在这一轮的消息下面另发一张
            idem = "ticketd-card-%s-%d" % (tid, int(t["started_at"] * 1000) % 10 ** 10)
            return _create(con, t, v, now, create, core._reply_target(con, tid), idem)
        else:
            if v["state"] != "running":
                con.execute("UPDATE tickets SET card_at=? WHERE id=?", (now, tid))
            return ""
    if v["form"] == "sending" and now - v["final"]["created_at"] < SENDING_GRACE:
        return ""
    if v["key"] == t["card_key"]:
        if v["state"] != "running":
            # 终态已经刷过：记下检查时间，之后状态不变就不会再被选中
            con.execute("UPDATE tickets SET card_at=? WHERE id=?", (now, tid))
            return ""
        if now - t["card_at"] < config.card_heartbeat():
            return ""
    elif now - t["card_at"] < MIN_INTERVAL:
        return ""
    card = render(t, v, core.grants(con, tid), now)
    ok, info, permanent = patch(t["card_message_id"], card)
    if not ok:
        _failed(con, t, info, permanent, now, "刷新", creating=False)
        return info
    con.execute("UPDATE tickets SET card_key=?, card_at=?, card_attempts=0, card_retry_at=0, card_closed=?,"
                " card_round=? WHERE id=?",
                (v["key"], now, 1 if v["form"] in ("folded", "final") else 0, t["started_at"], tid))
    return ""


def refresh(con, create=None, patch=None, now: Optional[float] = None) -> None:
    """守护进程每个循环调用一次：补发新单的卡片，按需刷新已有卡片。"""
    create = create or outbox.send_card
    patch = patch or outbox.patch_card
    now = db.now() if now is None else now
    # 已经刷成终态、之后再没变过的单不用每秒重算（状态变化都会更新 updated_at；结果送达会把 card_at 归零）
    rows = con.execute(
        "SELECT * FROM tickets t WHERE card_wanted=1 AND card_retry_at<=? AND (state IN ('queued','running')"
        " OR card_message_id IS NULL OR updated_at>card_at"
        " OR EXISTS(SELECT 1 FROM inputs i WHERE i.ticket_id=t.id AND i.status='pending')) ORDER BY id",
        (now,)).fetchall()
    for t in rows:
        try:
            err = _refresh_one(con, t, now, create, patch)
            if err and outbox._is_network_failure(err):
                break  # 网络故障：本轮不再试其余卡片，别拖住守护进程的主循环
        except Exception as e:  # 一张卡出错不能耽误其他卡，更不能拖住结果发送
            outbox.alert("状态卡处理出错（%s）：%r" % (t["id"], e))
            try:
                con.execute("UPDATE tickets SET card_retry_at=? WHERE id=?", (now + 60, t["id"]))
            except Exception:
                pass

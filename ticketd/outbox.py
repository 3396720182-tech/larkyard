from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Optional, Tuple

from . import config, core, db, safefs, templates

PERMANENT_CODES = {230001, 230002, 230011, 230025}


def alert(message: str) -> None:
    d = config.logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "alerts.log", "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))


def _parse_json(text: str) -> Optional[dict]:
    """在输出里找出带 "ok" 字段的 JSON 对象（容忍前后的日志行和缩进）。"""
    text = text or ""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def _is_network_failure(info: str) -> bool:
    return info.startswith("调用 lark-cli 失败") or '"type": "network"' in info


def send_reply(reply_to: str, text: str, idem: str) -> Tuple[bool, str, bool]:
    """回复飞书消息。返回 (是否成功, message_id 或错误信息, 是否永久失败)。"""
    return _run_lark([config.lark_bin(), "im", "+messages-reply", "--as", "bot", "--message-id", reply_to,
                      "--idempotency-key", idem, "--text", text])


def send_card(reply_to: str, card: dict, idem: str) -> Tuple[bool, str, bool]:
    """用交互卡片回复飞书消息（状态卡）。返回值同 send_reply。"""
    return _run_lark([config.lark_bin(), "im", "+messages-reply", "--as", "bot", "--message-id", reply_to,
                      "--idempotency-key", idem, "--msg-type", "interactive",
                      "--content", json.dumps(card, ensure_ascii=False)])


_MESSAGE_ID = re.compile(r"^om_[0-9A-Za-z_-]+$")


def patch_card(message_id: str, card: dict) -> Tuple[bool, str, bool]:
    """原地更新 bot 发出的卡片（PATCH /im/v1/messages/:id，不产生推送提醒）。返回值同 send_reply。"""
    if not _MESSAGE_ID.match(message_id or ""):
        return False, "卡片消息 id 不合法：%r" % message_id, True
    body = {"content": json.dumps(card, ensure_ascii=False)}
    return _run_lark([config.lark_bin(), "api", "PATCH", "/open-apis/im/v1/messages/" + message_id, "--as", "bot",
                      "--data", json.dumps(body, ensure_ascii=False)])


# ---------- 结果卡里的图片 ----------
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 飞书 im/v1/images 上限 10MB
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def upload_image(path: str) -> Tuple[bool, str, bool]:
    """上传图片拿 image_key（im/v1/images，image_type=message）。返回值同 send_reply，info 为 image_key。
    lark-cli 的 --file 只接受当前目录下的相对路径（绝对路径报 cannot open file），所以切到图片所在目录再传文件名。"""
    return _run_lark([config.lark_bin(), "im", "images", "create", "--as", "bot",
                      "--data", json.dumps({"image_type": "message"}), "--file", "image=" + os.path.basename(path)],
                     field="image_key", cwd=os.path.dirname(os.path.abspath(path)))


def _read_workdir_image(path: str, workdir: Optional[str]) -> bytes:
    """读工单目录里的一张图。守护进程不在沙箱里，路径由 worker 控制：只认工单目录内、非符号链接、
    非硬链接的普通文件，按文件头确认是图片，避免借上传把沙箱外的文件发出去。"""
    if not workdir:
        raise OSError("工单没有工作目录")
    if not os.path.isabs(path):  # worker 常报相对工单目录的路径；守护进程的 cwd 不是工单目录
        path = os.path.join(workdir, path)
    root = os.path.realpath(workdir)
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep):
        raise OSError("不在工单目录内")
    fd = safefs.open_read(path)
    if fd is None:
        raise OSError("文件不存在")
    try:
        st = os.fstat(fd)
        rst = os.stat(real)
        if (st.st_dev, st.st_ino) != (rst.st_dev, rst.st_ino):
            raise OSError("路径在读取过程中被替换")
        if st.st_size > MAX_IMAGE_BYTES:
            raise OSError("超过 10MB")
        chunks, left = [], MAX_IMAGE_BYTES + 1
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_IMAGE_BYTES:
        raise OSError("超过 10MB")
    if not (data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise OSError("文件头不是图片")
    return data


def resolve_card_images(card: dict, workdir: Optional[str], uploader=None) -> dict:
    """把卡片里待上传的图片占位换成 img_key。上传不成的整张图（连同上方的文件名）删掉，结果不能因图片卡住。
    上传的是读进来的字节的私有副本，不把 worker 可控的路径交给 lark-cli。"""
    uploader = uploader or upload_image
    els = card.get("body", {}).get("elements", [])
    out = []
    for el in els:
        if not (isinstance(el, dict) and templates.UPLOAD_KEY in el):
            out.append(el)
            continue
        path = el[templates.UPLOAD_KEY]
        key, why = "", ""
        try:
            data = _read_workdir_image(path, workdir)
            with tempfile.TemporaryDirectory(prefix="ticketd-img-") as d:
                tmp = os.path.join(d, "image" + os.path.splitext(path)[1].lower())
                with open(tmp, "wb") as f:
                    f.write(data)
                ok, info, _ = uploader(tmp)
            key, why = (info, "") if ok and info else ("", info or "未返回 image_key")
        except OSError as e:
            why = str(e)
        if key:
            el = {k: v for k, v in el.items() if k != templates.UPLOAD_KEY}
            el["img_key"] = key
            out.append(el)
        else:
            alert("结果卡图片未显示（%s）：%s" % (path, why))
            name = (el.get("alt") or {}).get("content")
            if out and isinstance(out[-1], dict) and (out[-1].get("text") or {}).get("content") == name:
                out.pop()  # 去掉这张图上方的文件名
    card = dict(card)
    card["body"] = dict(card.get("body", {}), elements=out)
    return card


def _has_uploads(card: dict) -> bool:
    return any(isinstance(el, dict) and templates.UPLOAD_KEY in el
               for el in card.get("body", {}).get("elements", []))


def _run_lark(cmd, field: str = "message_id", cwd: Optional[str] = None) -> Tuple[bool, str, bool]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "调用 lark-cli 失败：%s" % e, False
    data = _parse_json(p.stdout) or _parse_json(p.stderr)
    if data and data.get("ok"):
        body = data.get("data")
        return True, (body.get(field) or "") if isinstance(body, dict) else "", False
    if p.returncode == 0 and not (data and data.get("ok") is False):
        if data is None:
            alert("lark-cli 返回 0 但输出无法解析，按已发送处理：%s" % (p.stdout or p.stderr)[:200])
        return True, "", False
    err = (data or {}).get("error") or {}
    if not isinstance(err, dict):
        err = {}
    # 这些错误码未经实测验证；真正的兜底是 flush 里的24 小时年龄上限。
    permanent = (err.get("retryable") is False
                 or err.get("type") == "validation"
                 or (err.get("type") == "api" and err.get("code") in PERMANENT_CODES))
    detail = json.dumps(err, ensure_ascii=False) if err else (p.stderr or p.stdout)[-500:]
    return False, "rc=%s %s" % (p.returncode, detail), permanent


CARD_HOLD = 120.0  # 新单的状态卡还没发出时，同单的消息先等它（保证卡片排在结果前面），最多等这么久


def _send_row(r, sender, card_sender, con, card_patcher=None) -> Tuple[bool, str, bool]:
    idem = "ticketd-%s-%d-%d" % (r["ticket_id"], r["id"], int(r["created_at"] * 1000) % 10 ** 10)
    if r["card_json"] is None:
        return sender(r["reply_to"], r["text"], idem)
    if r["card_json"] == "":  # 卡片已被拒：兜底文字的每次重试都用同一个幂等键
        return sender(r["reply_to"], r["text"], idem + "-t")
    card, text = json.loads(r["card_json"]), r["text"]
    if _has_uploads(card):
        # 先上传图片，把换好 img_key 的卡片写回发件箱：重试时不重复上传（稿子按钮不写回，每次按缓存现加）
        t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
        card = resolve_card_images(card, t["workdir"] if t else None)
        con.execute("UPDATE outbox SET card_json=? WHERE id=?", (json.dumps(card, ensure_ascii=False), r["id"]))
    links = _doc_links(con, r)
    if links:
        card = templates.with_doc_links(card, links)
        text += "".join("\n📄 %s：%s" % (name, url) for name, url in links)
    if r["report_id"] is not None:
        from . import card as status_card  # card 依赖本模块的发送函数
        res = status_card.deliver(con, r["ticket_id"], card, patch=card_patcher)
        if res is not None:  # 本轮有状态卡：结果直接覆盖到卡上，不另发消息
            return res
    ok, info, permanent = card_sender(r["reply_to"], card, idem)
    if ok or not permanent:
        return ok, info, permanent
    # 卡片被飞书判为永久失败（多半是卡片结构不被接受）：改发文字兜底，结果不能丢
    alert("outbox %d（%s）结果卡片发送失败，改发文字：%s" % (r["id"], r["ticket_id"], info))
    con.execute("UPDATE outbox SET card_json='', text=? WHERE id=?", (text, r["id"]))
    return sender(r["reply_to"], text, idem + "-t")


def _doc_links(con, r) -> list:
    """结果卡片上的稿子：把本地文档产物导入成云文档，拿到链接（有缓存，重试不重复导入）。"""
    if r["report_id"] is None:
        return []
    rep = con.execute("SELECT artifacts_json FROM reports WHERE id=?", (r["report_id"],)).fetchone()
    t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
    if not rep or not t:
        return []
    try:
        arts = json.loads(rep["artifacts_json"] or "[]")
    except ValueError:
        return []
    from . import publish  # publish 依赖本模块的 alert / _parse_json
    try:
        return publish.doc_links(con, r["ticket_id"], t["workdir"], arts if isinstance(arts, list) else [])
    except Exception as e:  # 导入是锦上添花：任何意外都不能挡住结果送达
        alert("%s 稿子链接生成异常：%r" % (r["ticket_id"], e))
        return []


def _ping(con, r, message_id: str) -> Optional[str]:
    """结果覆盖到了状态卡上（编辑不推送提醒）：需要用户回答时返回要另推的一行提醒，否则 None。"""
    if r["report_id"] is None or not message_id:
        return None
    t = con.execute("SELECT card_message_id FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
    if t is None or t["card_message_id"] != message_id:
        return None  # 结果是作为新消息发出的，本身就有提醒
    rep = con.execute("SELECT status, summary, ask FROM reports WHERE id=?", (r["report_id"],)).fetchone()
    return templates.ping_message(r["ticket_id"], rep["status"], rep["summary"], rep["ask"]) if rep else None


def flush(con, sender=None, limit: int = 5, card_sender=None, card_patcher=None) -> None:
    sender = sender or send_reply
    card_sender = card_sender or send_card
    now = db.now()
    rows = con.execute(
        "SELECT * FROM outbox WHERE id IN (SELECT MIN(id) FROM outbox WHERE status='pending' GROUP BY ticket_id)"
        " AND next_at<=? AND ticket_id NOT IN (SELECT id FROM tickets WHERE card_wanted=1"
        " AND card_message_id IS NULL AND created_at>?) ORDER BY id LIMIT ?",
        (now, now - CARD_HOLD, limit)).fetchall()
    for r in rows:
        try:
            ok, info, permanent = _send_row(r, sender, card_sender, con, card_patcher)
        except Exception as e:  # 毒丸行：发送器本身抛异常，标记 dead 后继续处理其余行
            con.execute("UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                        (r["attempts"] + 1, repr(e), r["id"]))
            con.execute("UPDATE tickets SET card_at=0 WHERE id=?", (r["ticket_id"],))
            alert("outbox %d dead（%s）：发送异常 %r" % (r["id"], r["ticket_id"], e))
            continue
        attempts = r["attempts"] + 1
        if r["report_id"] is not None and (ok or permanent or db.now() - r["created_at"] > 24 * 3600):
            # 结果已送达（或确定送不到）：让状态卡下一轮就收起成终态（card_at 归零即视为需要刷新）
            con.execute("UPDATE tickets SET card_at=0 WHERE id=?", (r["ticket_id"],))
        if ok:
            ping = _ping(con, r, info)
            with db.tx(con):
                con.execute("UPDATE outbox SET status='sent', attempts=?, sent_message_id=?, last_error=NULL"
                            " WHERE id=?", (attempts, info, r["id"]))
                if info:
                    con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (info, r["ticket_id"]))
                if ping:
                    core.enqueue_message(con, r["ticket_id"], ping)
        elif permanent:
            con.execute("UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                        (attempts, info, r["id"]))
            alert("outbox %d dead（%s）：%s" % (r["id"], r["ticket_id"], info))
        elif db.now() - r["created_at"] > 24 * 3600:
            con.execute("UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                        (attempts, info, r["id"]))
            alert("outbox %d dead（%s）：重试 %d 次仍失败：%s" % (r["id"], r["ticket_id"], attempts, info))
        else:
            delay = min(5 * 2 ** (attempts - 1), 300)
            con.execute("UPDATE outbox SET attempts=?, next_at=?, last_error=? WHERE id=?",
                        (attempts, db.now() + delay, info, r["id"]))
            if attempts == 3:
                alert("outbox %d（%s）连续失败 3 次：%s" % (r["id"], r["ticket_id"], info))
        if not ok and _is_network_failure(info):
            break  # 网络/超时：本 tick 不再尝试其余行，避免拖住守护进程

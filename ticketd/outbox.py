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
    """读工单目录里的一张图，按文件头确认是图片。"""
    data = _read_workdir_file(path, workdir, MAX_IMAGE_BYTES)
    if not (data.startswith(_IMAGE_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise OSError("文件头不是图片")
    return data


def _fd_path(fd: int) -> Optional[str]:
    """已打开文件的真实路径（macOS F_GETPATH / Linux /proc）；拿不到返回 None。"""
    try:
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    except OSError:
        return None
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return None


def _read_workdir_file(path: str, workdir: Optional[str], limit: int) -> bytes:
    """读工单目录里的一个文件。守护进程不在沙箱里，路径由 worker 控制：只认工单目录内、非符号链接、
    非硬链接的普通文件，避免借上传把沙箱外的文件发出去。"""
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
        opened = _fd_path(fd)
        # O_NOFOLLOW 只管最后一级：中间某级目录在检查和打开之间被换成符号链接时，按实际打开的文件再核一次
        if opened is not None and not os.path.realpath(opened).startswith(root + os.sep):
            raise OSError("打开的文件不在工单目录内")
        if st.st_size > limit:
            raise OSError("超过 %dMB" % (limit // (1024 * 1024)))
        chunks, left = [], limit + 1
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > limit:
        raise OSError("超过 %dMB" % (limit // (1024 * 1024)))
    return data


def _upload_workdir_image(path: str, workdir: Optional[str], uploader) -> Tuple[str, str]:
    """上传工单目录里的一张图（私有副本），返回 (image_key, 失败原因)。"""
    try:
        data = _read_workdir_image(path, workdir)
        with tempfile.TemporaryDirectory(prefix="ticketd-img-") as d:
            tmp = os.path.join(d, "image" + os.path.splitext(path)[1].lower())
            with open(tmp, "wb") as f:
                f.write(data)
            ok, info, _ = uploader(tmp)
        return (info, "") if ok and info else ("", info or "未返回 image_key")
    except OSError as e:
        return "", str(e)


def resolve_card_videos(card: dict, workdir: Optional[str], ticket_id: str, drive_uploader=None,
                        uploader=None) -> Tuple[dict, list]:
    """把卡片里的视频占位换成「可点击封面 + 播放按钮」（视频先以用户身份传到云空间拿链接）。
    返回 (新卡片, 没能放进卡片的视频列表)——后者由调用方另排视频消息。"""
    drive_uploader = drive_uploader or upload_video_drive
    uploader = uploader or upload_image
    out, leftover = [], []
    for el in card.get("body", {}).get("elements", []):
        if not (isinstance(el, dict) and templates.VIDEO_KEY in el):
            out.append(el)
            continue
        video = el[templates.VIDEO_KEY]
        ok, url = drive_uploader(video, workdir, ticket_id)
        if not ok:
            alert("视频没能传到云空间，改为卡片后另发视频消息（%s）：%s" % (video, url))
            url = None
            leftover.append(video)
        cover = find_cover(video, workdir)
        key = ""
        if cover:
            key, why = _upload_workdir_image(cover, workdir, uploader)
            if not key:
                alert("视频封面未显示（%s）：%s" % (cover, why))
        out.extend(templates.video_elements(video, url, key or None))
    card = dict(card)
    card["body"] = dict(card.get("body", {}), elements=out)
    return card, leftover


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
        key, why = _upload_workdir_image(path, workdir, uploader)
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
    return any(isinstance(el, dict) and (templates.UPLOAD_KEY in el or templates.VIDEO_KEY in el)
               for el in card.get("body", {}).get("elements", []))


MAX_DRIVE_VIDEO_BYTES = 20 * 1024 * 1024  # 云空间一次性上传（upload_all）的上限
_DRIVE_URL = re.compile(r"^https://[A-Za-z0-9.-]+\.(feishu\.cn|larksuite\.com|feishu-pre\.net)/\S+$")


def _safe_drive_name(ticket_id: str, video: str) -> str:
    """云空间里的文件名：工单号-原文件名（只留字母数字汉字和 ._-，不以 - 开头，限长）。"""
    base = re.sub(r"[^\w.\-]", "_", os.path.basename(video))[-80:] or "video.mp4"
    if not base.lower().endswith(".mp4"):
        base += ".mp4"
    return "%s-%s" % (re.sub(r"[^\w]", "", ticket_id) or "ticket", base)


def upload_video_drive(video: str, workdir: Optional[str], ticket_id: str) -> Tuple[bool, str]:
    """以用户身份把视频传到用户自己的云空间（文件归用户所有，用户本人能打开），返回 (成功, 链接或失败原因)。
    文件规则同图片/视频消息：只认工单目录里的普通 mp4，交给 lark-cli 的是私有临时副本。"""
    try:
        data = _read_workdir_file(video, workdir, MAX_DRIVE_VIDEO_BYTES)
        if not _is_mp4(data):
            raise OSError("文件头不是 mp4")
    except OSError as e:
        return False, "视频不能上传（%s）：%s" % (video, e)
    with tempfile.TemporaryDirectory(prefix="ticketd-drive-") as d:
        with open(os.path.join(d, "video.mp4"), "wb") as f:
            f.write(data)
        cmd = [config.lark_bin(), "drive", "+upload", "--as", "user", "--file", "./video.mp4",
               "--name", _safe_drive_name(ticket_id, video)]
        if config.video_folder():
            cmd += ["--folder-token", config.video_folder()]
        ok, body, err = _lark_json(cmd, cwd=d, timeout=VIDEO_TIMEOUT)
    if not ok:
        return False, "上传云空间失败：%s" % err
    url = _find_url(body)
    token = _find_key(body, "file_token")
    if not url and token:
        ok, meta, err = _lark_json([config.lark_bin(), "drive", "metas", "batch_query", "--as", "user", "--data",
                                    json.dumps({"request_docs": [{"doc_token": token, "doc_type": "file"}],
                                                "with_url": True})])
        url = _find_url(meta) if ok else None
        if not url:
            return False, "已上传（file_token=%s）但拿不到链接：%s" % (token, err or "返回里没有 url")
    if not url:
        return False, "上传云空间后没有返回 file_token 或链接"
    return True, url


def _find_key(obj, key: str):
    """在 lark-cli 返回的 data 里深度找第一个 key 字段（各命令包装层不一样）。"""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), str) and obj[key]:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


def _find_url(obj) -> Optional[str]:
    url = _find_key(obj, "url")
    return url if url and _DRIVE_URL.match(url) else None


def _lark_json(cmd, cwd: Optional[str] = None, timeout: float = 30) -> Tuple[bool, Optional[dict], str]:
    """跑 lark-cli，返回 (成功, data 字段, 失败信息)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, None, "调用 lark-cli 失败：%s" % e
    data = _parse_json(p.stdout) or _parse_json(p.stderr)
    if data and data.get("ok"):
        return True, data.get("data") if isinstance(data.get("data"), (dict, list)) else {}, ""
    err = (data or {}).get("error")
    return False, None, "rc=%s %s" % (p.returncode, json.dumps(err, ensure_ascii=False) if err else (p.stderr or p.stdout)[-300:])


# ---------- 视频消息（T0010） ----------
MAX_VIDEO_BYTES = 30 * 1024 * 1024  # 飞书 im/v1/files 上限 30MB
VIDEO_TIMEOUT = 180
VIDEO_MAX_ATTEMPTS = 3  # 失败这么多次后改发兜底文字


def find_cover(video: str, workdir: Optional[str]) -> Optional[str]:
    """视频的封面：同目录同名的第一张能通过图片检查的图。返回原样（相对/绝对）路径，没有返回 None。"""
    for c in templates.cover_candidates(video):
        try:
            _read_workdir_image(c, workdir)
            return c
        except OSError:
            continue
    return None


def _is_mp4(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def send_video(reply_to: str, video: str, workdir: Optional[str], idem: str) -> Tuple[bool, str, bool]:
    """回复一条视频消息（lark-cli 的 --video 会先上传成 file_key，封面 --video-cover 必填）。
    和图片一样：只认工单目录里的普通文件，复制成私有临时文件再交给 lark-cli（它只接受 cwd 下的相对路径）。
    没有封面时退成文件消息（--file），飞书里同样能点开预览。本地检查不过算永久失败（重试也不会好）。"""
    try:
        data = _read_workdir_file(video, workdir, MAX_VIDEO_BYTES)
        if not _is_mp4(data):
            raise OSError("文件头不是 mp4")
        cover = find_cover(video, workdir)
        cover_data = _read_workdir_image(cover, workdir) if cover else None
    except OSError as e:
        return False, "视频不能发送（%s）：%s" % (video, e), True
    with tempfile.TemporaryDirectory(prefix="ticketd-video-") as d:
        name = "video.mp4"  # 固定 ASCII 名：不把 worker 起的文件名交给 lark-cli
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        cmd = [config.lark_bin(), "im", "+messages-reply", "--as", "bot", "--message-id", reply_to,
               "--idempotency-key", idem]
        if cover_data is not None:
            cname = "cover" + os.path.splitext(cover)[1].lower()
            with open(os.path.join(d, cname), "wb") as f:
                f.write(cover_data)
            cmd += ["--video", "./" + name, "--video-cover", "./" + cname]
        else:
            alert("视频 %s 没有同名封面图，改发文件消息" % video)
            cmd += ["--file", "./" + name]
        return _run_lark(cmd, cwd=d, timeout=VIDEO_TIMEOUT)


def _run_lark(cmd, field: str = "message_id", cwd: Optional[str] = None, timeout: float = 30) -> Tuple[bool, str, bool]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
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


def _send_row(r, sender, card_sender, con, video_sender=None, card_patcher=None) -> Tuple[bool, str, bool]:
    video_sender = video_sender or send_video
    idem = "ticketd-%s-%d-%d" % (r["ticket_id"], r["id"], int(r["created_at"] * 1000) % 10 ** 10)
    media = r["media_json"] if "media_json" in r.keys() else None
    if media and r["attempts"] >= VIDEO_MAX_ATTEMPTS:
        # 网络/超时反复失败：每次都要阻塞守护进程最多 180 秒、还挡住同单后面的消息，不再硬发，改发文字
        alert("outbox %d（%s）视频连续 %d 次没发出，改发文字：%s" % (r["id"], r["ticket_id"], r["attempts"], r["last_error"]))
        con.execute("UPDATE outbox SET media_json='' WHERE id=?", (r["id"],))
        media = ""
    if media:
        t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
        ok, info, permanent = video_sender(r["reply_to"], json.loads(media)["video"], t["workdir"] if t else None, idem)
        if ok or not permanent:
            return ok, info, permanent
        # 视频发不出去（文件不合格或飞书永久拒收）：改发文字告诉用户文件在哪
        alert("outbox %d（%s）视频消息发送失败，改发文字：%s" % (r["id"], r["ticket_id"], info))
        con.execute("UPDATE outbox SET media_json='' WHERE id=?", (r["id"],))
        return sender(r["reply_to"], r["text"], idem + "-t")
    if media == "":  # 视频已放弃：兜底文字的每次重试都用同一个幂等键
        return sender(r["reply_to"], r["text"], idem + "-t")
    if r["card_json"] is None:
        return sender(r["reply_to"], r["text"], idem)
    if r["card_json"] == "":  # 卡片已被拒：兜底文字的每次重试都用同一个幂等键
        return sender(r["reply_to"], r["text"], idem + "-t")
    card, text = json.loads(r["card_json"]), r["text"]
    if _has_uploads(card):
        # 先传视频（云空间）和图片，把换好链接/img_key 的卡片写回发件箱：重试时不重复上传
        t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
        wd = t["workdir"] if t else None
        card, leftover = resolve_card_videos(card, wd, r["ticket_id"])
        card = resolve_card_images(card, wd)
        with db.tx(con):
            con.execute("UPDATE outbox SET card_json=? WHERE id=?", (json.dumps(card, ensure_ascii=False), r["id"]))
            for v in leftover:  # 没进卡片的视频：卡片之后另发视频消息（id 更大，排在卡片后面）
                con.execute("INSERT INTO outbox(ticket_id, reply_to, text, media_json, created_at) VALUES(?,?,?,?,?)",
                            (r["ticket_id"], r["reply_to"], templates.video_fallback(r["ticket_id"], v, wd),
                             json.dumps({"video": v}, ensure_ascii=False), db.now()))
    links = _doc_links(con, r)
    if links:  # 稿子按钮每次按缓存现加，不写回发件箱
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
            ok, info, permanent = _send_row(r, sender, card_sender, con, card_patcher=card_patcher)
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

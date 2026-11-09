from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Optional, Tuple

from . import config, core, db, safefs, templates

# 99992361 open_id cross app：收件人的 open_id 不属于发消息的这个应用（TICKETD_OWNER 配错），重试不会好
PERMANENT_CODES = {230001, 230002, 230011, 230025, 99992361}


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


def _is_transient(info: str) -> bool:
    """网络/超时这类「过一会儿可能就好」的失败（不论出现在错误信息的哪个位置，如「上传云空间失败：调用 lark-cli 失败…」）。
    lark-cli 标了 retryable 的业务错误（如 api/rate_limit 限流）也算。"""
    info = info or ""
    return "调用 lark-cli 失败" in info or '"type": "network"' in info or '"retryable": true' in info


def _is_api_error(info: str) -> bool:
    """飞书已经明确回了业务错误（lark-cli 的 error.type == "api"），不是网络问题；限流这类可重试的不算。"""
    return '"type": "api"' in (info or "") and not _is_transient(info)


# 被回复的那条消息已经撤回/删除：回复永远发不出去，改成直接发到会话里（原来结果会因此被丢弃）
REPLY_GONE_CODES = {230011}


def _parse_error(info: str) -> dict:
    """从「rc=1 {...}」形式的失败信息里取回 lark-cli 的 error 对象；取不到返回 {}。"""
    i = (info or "").find("{")
    if i < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(info, i)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _reply_gone(info: str) -> bool:
    err = _parse_error(info)
    return err.get("type") == "api" and err.get("code") in REPLY_GONE_CODES


def _target_args(to: str) -> list:
    """om_ 开头：回复那条消息；oc_ 开头（回复目标被撤回后改投）：直接发到会话；ou_ 开头（告警、周报）：私信这个人。"""
    if (to or "").startswith("oc_"):
        return ["im", "+messages-send", "--as", "bot", "--chat-id", to]
    if (to or "").startswith("ou_"):
        return ["im", "+messages-send", "--as", "bot", "--user-id", to]
    return ["im", "+messages-reply", "--as", "bot", "--message-id", to]


MEDIA_MAX_ATTEMPTS = 3  # 媒体上传瞬时失败时至少退避重试这么多次……
MEDIA_DEGRADE_AFTER = 600.0  # ……并且这一行已经等了这么久，才降级（图换灰字、视频另发）：断网半分钟不该永久丢图
# 一行结果卡在一轮里开始新上传的时间上限（秒）；超了把进度写回、下一轮接着传。它不是单次上传的超时（那是
# _drive_timeout，100MB 约 420 秒），所以守护进程里上传不在主循环做，而在媒体通道线程里做（daemon.MediaLane）；
# 这个预算只管媒体通道里各行轮流，别让一张多视频的卡一直占着通道。
MEDIA_BUDGET = 60.0
_clock = time.monotonic  # 测试可替换


def send_reply(reply_to: str, text: str, idem: str) -> Tuple[bool, str, bool]:
    """回复飞书消息。返回 (是否成功, message_id 或错误信息, 是否永久失败)。"""
    return _run_lark([config.lark_bin()] + _target_args(reply_to) + ["--idempotency-key", idem, "--text", text])


def send_card(reply_to: str, card: dict, idem: str) -> Tuple[bool, str, bool]:
    """用交互卡片回复飞书消息（状态卡）。返回值同 send_reply。"""
    return _run_lark([config.lark_bin()] + _target_args(reply_to) + [
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
    # 目录部分解析一次（允许工单目录里本来就有的、指向目录内的链接），最后一级保留原名，交给 open_read_under：
    # 从工单目录起逐级 openat + O_NOFOLLOW，检查之后被并发换成符号链接的那一级会直接打开失败
    lexical = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    if not lexical.startswith(root + os.sep):
        raise OSError("不在工单目录内")
    fd = safefs.open_read_under(root, os.path.relpath(lexical, root))
    if fd is None:
        raise OSError("文件不存在")
    try:
        st = os.fstat(fd)
        rst = os.stat(real)
        if (st.st_dev, st.st_ino) != (rst.st_dev, rst.st_ino):
            raise OSError("路径在读取过程中被替换")
        opened = _fd_path(fd)
        # 纵深防御：按实际打开的文件再核一次（逐级打开已经堵住了竞态，拿不到路径时不再是唯一防线）
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
    """上传工单目录里的一张图（私有副本），返回 (image_key, 失败原因)。失败原因可用 _is_transient 判断能不能重试。"""
    try:
        data = _read_workdir_image(path, workdir)
        with tempfile.TemporaryDirectory(prefix="ticketd-img-") as d:
            tmp = os.path.join(d, "image" + os.path.splitext(path)[1].lower())
            with open(tmp, "wb") as f:
                f.write(data)
            ok, info, _ = uploader(tmp)
        return (info, "") if ok and info else ("", info or "未返回 image_key")
    except OSError as e:
        return "", "文件不能上传：%s" % e


def resolve_card_videos(card: dict, workdir: Optional[str], ticket_id: str, drive_uploader=None,
                        uploader=None) -> Tuple[dict, list]:
    """把卡片里的视频占位换成「可点击封面 + 播放按钮」（视频先以用户身份传到云空间拿链接）。
    返回 (新卡片, 没能放进卡片的视频列表)——后者由调用方另排视频消息。瞬时失败也直接降级（给脚本/样卡用）。"""
    card, leftover, _ = _resolve_videos(card, workdir, ticket_id, drive_uploader, uploader, degrade=True)
    return card, leftover


BUDGET_STOP = "上传预算用完："  # 不是失败：不计 attempts、不退避、不挡其余行（flush 里单独处理）


def _over_budget(deadline: Optional[float]) -> str:
    """一行结果卡本 tick 的上传时间用完了：返回停下的原因（按网络失败处理：写回进度、退避、下次接着传）。"""
    if deadline is not None and _clock() > deadline:
        return BUDGET_STOP + "本轮上传已超过 %d 秒，先停下，下次接着传" % MEDIA_BUDGET
    return ""


DRIVE_LINK_TTL = 7 * 24 * 3600.0


def _video_digest(video: str, workdir: Optional[str]) -> Optional[str]:
    try:
        return hashlib.sha256(_read_workdir_file(video, workdir, MAX_DRIVE_VIDEO_BYTES)).hexdigest()
    except OSError:
        return None


def _cached_link(con, ticket_id: str, digest: Optional[str]) -> Optional[str]:
    if con is None or not digest:
        return None
    row = con.execute("SELECT url FROM drive_links WHERE ticket_id=? AND sha256=? AND created_at>?",
                      (ticket_id, digest, db.now() - DRIVE_LINK_TTL)).fetchone()
    return row["url"] if row else None


def _resolve_videos(card, workdir, ticket_id, drive_uploader=None, uploader=None, degrade=True,
                    con=None, deadline=None):
    """返回 (新卡片, 降级成另发消息的视频, 瞬时失败信息或 "")。degrade=False 时遇到网络/超时就停下：
    这个视频的占位原样保留（已拿到的云空间链接记在占位上，下次不重传），后面的视频也不再处理。
    con 不为空时按「工单 + 内容哈希」复用传过的云空间链接（同一个视频再报一次不重复上传）。
    deadline（_clock 时间）之后不再开始新的上传。"""
    drive_uploader = drive_uploader or upload_video_drive
    uploader = uploader or upload_image
    out, leftover, err = [], [], ""
    for el in card.get("body", {}).get("elements", []):
        if err or not (isinstance(el, dict) and templates.VIDEO_KEY in el):
            out.append(el)
            continue
        video = el[templates.VIDEO_KEY]
        url = el.get(templates.VIDEO_URL_KEY)
        digest = None
        if not url and con is not None:
            digest = _video_digest(video, workdir)
            url = _cached_link(con, ticket_id, digest)
        if not url:
            err = _over_budget(deadline)
            if err:
                out.append(el)
                continue
            ok, info = drive_uploader(video, workdir, ticket_id)
            if ok:
                url = info
                # 上传前后各算一次哈希，一致才缓存：上传途中文件被改写时不把旧哈希记到新内容的链接上
                if con is not None and digest and _video_digest(video, workdir) == digest:
                    try:
                        con.execute("INSERT OR REPLACE INTO drive_links VALUES(?,?,?,?)",
                                    (ticket_id, digest, url, db.now()))
                    except sqlite3.Error as e:  # 缓存是锦上添花：写不进去不影响这次发送，但要分清锁等待和故障
                        alert("云空间链接缓存写入失败（%s，%s）：%r"
                              % (video, "库被占用" if db.is_busy(e) else "数据库故障，需要检查", e))
            elif _is_transient(info) and not degrade:
                err = info
                out.append(el)
                continue
            else:
                alert("视频没能传到云空间，改为卡片后另发视频消息（%s）：%s" % (video, info))
                url = None
        cover = find_cover(video, workdir)
        key = ""
        if cover:
            err = _over_budget(deadline)
            if err:
                out.append(dict(el, **({templates.VIDEO_URL_KEY: url} if url else {})))
                continue
            key, why = _upload_workdir_image(cover, workdir, uploader)
            if not key and _is_transient(why) and not degrade:
                err = why
                out.append(dict(el, **({templates.VIDEO_URL_KEY: url} if url else {})))  # 链接留着，下次只补封面
                continue
            if not key:
                alert("视频封面未显示（%s）：%s" % (cover, why))
        if url is None:
            leftover.append(video)
        out.extend(templates.video_elements(video, url, key or None))
    card = dict(card)
    card["body"] = dict(card.get("body", {}), elements=out)
    return card, leftover, err


def resolve_card_images(card: dict, workdir: Optional[str], uploader=None) -> dict:
    """把卡片里（含 column_set / 容器里嵌套的）待上传图片占位换成 img_key；上传不成的图换成一行灰字
    「（xx 未能显示）」（瞬时失败也一样，给脚本/样卡用；发件箱用 _resolve_images 区分瞬时失败）。"""
    return _resolve_images(card, workdir, uploader, degrade=True)[0]


def _resolve_images(card, workdir, uploader=None, degrade=True, deadline=None):
    """返回 (新卡片, 瞬时失败信息或 "")。上传的是读进来的字节的私有副本，不把 worker 可控的路径交给 lark-cli。
    degrade=False 时第一张遇到网络/超时就停：这张和后面没传的都保留占位，已经成功的换好 img_key。
    deadline（_clock 时间）之后不再开始新的上传（不论 degrade），没传的保留占位。"""
    uploader = uploader or upload_image
    state = {"err": ""}

    def walk(els):
        out = []
        for el in els:
            if not isinstance(el, dict) or state["err"]:
                out.append(el)
                continue
            if templates.UPLOAD_KEY in el:
                state["err"] = _over_budget(deadline)
                if state["err"]:
                    out.append(el)
                    continue
                path = el[templates.UPLOAD_KEY]
                key, why = _upload_workdir_image(path, workdir, uploader)
                if key:
                    el = {k: v for k, v in el.items() if k != templates.UPLOAD_KEY}
                    el["img_key"] = key
                elif _is_transient(why) and not degrade:
                    state["err"] = why
                else:
                    alert("结果卡图片未显示（%s）：%s" % (path, why))
                    el = templates.note("（%s 未能显示）" % ((el.get("alt") or {}).get("content") or "图片"))
                out.append(el)
                continue
            el = dict(el)
            if isinstance(el.get("elements"), list):
                el["elements"] = walk(el["elements"])
            if isinstance(el.get("columns"), list):
                el["columns"] = [dict(c, elements=walk(c.get("elements") or [])) if isinstance(c, dict) else c
                                 for c in el["columns"]]
            out.append(el)
        return out

    card = dict(card)
    card["body"] = dict(card.get("body", {}), elements=walk(card.get("body", {}).get("elements", [])))
    return card, state["err"]


def _has_uploads(obj) -> bool:
    """卡片里（任意嵌套层）还有没有待上传的占位。"""
    if isinstance(obj, dict):
        if templates.UPLOAD_KEY in obj or templates.VIDEO_KEY in obj:
            return True
        return any(_has_uploads(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_uploads(v) for v in obj)
    return False


# 云空间视频上限。lark-cli drive +upload 超过 20MB 自动走分片上传（upload_prepare/part/finish，
# v1.0.86 源码 shortcuts/drive/drive_upload.go），20MB 不是接口上限；100MB 是为了限制守护进程的阻塞时间和内存。
MAX_DRIVE_VIDEO_BYTES = 100 * 1024 * 1024


def _drive_timeout(size: int) -> float:
    """云空间上传的超时：20MB 以内 180 秒，之后每 MB 加 3 秒（100MB 约 420 秒）。"""
    return VIDEO_TIMEOUT + max(0, size - 20 * 1024 * 1024) * 3 / (1024 * 1024)
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
        ok, body, err = _lark_json(cmd, cwd=d, timeout=_drive_timeout(len(data)))
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
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=cwd)
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
        cmd = [config.lark_bin()] + _target_args(reply_to) + ["--idempotency-key", idem]
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
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=cwd)
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


def _idem(r) -> str:
    return "ticketd-%s-%d-%d" % (r["ticket_id"], r["id"], int(r["created_at"] * 1000) % 10 ** 10)


def _send_row(r, sender, card_sender, con, video_sender=None, card_patcher=None, lane: str = "all") -> Tuple[bool, str, bool]:
    video_sender = video_sender or send_video
    idem = _idem(r)
    media = r["media_json"] if "media_json" in r.keys() else None
    if media and r["attempts"] >= VIDEO_MAX_ATTEMPTS and db.now() - r["created_at"] >= MEDIA_DEGRADE_AFTER:
        # 网络/超时反复失败且已等了 10 分钟：每次都要占住媒体通道最多 180 秒、还挡住同单后面的消息，不再硬发，改发文字
        alert("outbox %d（%s）视频连续 %d 次没发出，改发文字：%s" % (r["id"], r["ticket_id"], r["attempts"], r["last_error"]))
        con.execute("UPDATE outbox SET media_json='' WHERE id=?", (r["id"],))
        media = ""
    if media:
        t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
        ok, info, permanent = video_sender(r["reply_to"], json.loads(media)["video"], t["workdir"] if t else None, idem)
        if ok or not permanent:
            return ok, info, permanent
        if _reply_gone(info):  # 回复目标没了：不是视频的问题，由 flush 改投到会话后原样重发
            return False, info, False
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
        # 网络/超时：只写回已经成功的部分，整行按 attempts 退避重试；失败满 MEDIA_MAX_ATTEMPTS 次、
        # 且这一行已经等了 MEDIA_DEGRADE_AFTER 秒后才降级（短暂断网不该让图片永久变灰字）
        degrade = r["attempts"] >= MEDIA_MAX_ATTEMPTS and db.now() - r["created_at"] >= MEDIA_DEGRADE_AFTER
        deadline = _clock() + MEDIA_BUDGET  # 一轮里不在一行上耗太久（媒体通道里还有别的行在等）
        card, leftover, err = _resolve_videos(card, wd, r["ticket_id"], degrade=degrade, con=con, deadline=deadline)
        if not err:
            card, err = _resolve_images(card, wd, degrade=degrade, deadline=deadline)
        with db.tx(con):
            con.execute("UPDATE outbox SET card_json=? WHERE id=?", (json.dumps(card, ensure_ascii=False), r["id"]))
            for v in leftover:  # 没进卡片的视频：卡片之后另发视频消息（id 更大，排在卡片后面）
                con.execute("INSERT INTO outbox(ticket_id, reply_to, text, media_json, created_at) VALUES(?,?,?,?,?)",
                            (r["ticket_id"], r["reply_to"], templates.video_fallback(r["ticket_id"], v, wd),
                             json.dumps({"video": v}, ensure_ascii=False), db.now()))
        if err.startswith(BUDGET_STOP):
            return False, err, False  # 只是这个 tick 的时间用完了：flush 不计失败、下个 tick 接着传
        if err:
            # 以「调用 lark-cli 失败」开头：flush 按网络失败处理（退避、本 tick 不再试其余行）
            return False, "调用 lark-cli 失败：上传卡片媒体暂时失败，稍后重试（%s）" % err[:300], False
    if lane == LANE_MEDIA:
        # 媒体通道只做慢的准备：上传（上面已写回）和稿子导入（结果进 published 缓存）。发送留给主循环：
        # 结果要覆盖到状态卡上，必须和 card.refresh 在同一个线程里，否则两边的 PATCH 会互相覆盖
        _doc_links(con, r)
        _docs_tried.add(r["id"])
        return False, PREPARED, False
    links = _doc_links(con, r, allow_import=lane != LANE_FAST)  # 主循环只用缓存里的链接，不在这里导入
    if links:  # 稿子按钮每次按缓存现加，不写回发件箱
        card = templates.with_doc_links(card, links)
        text += "".join("\n📄 %s：%s" % (name, url) for name, url in links)
    fails = _doc_failures(con, r)
    if fails:  # 导入失败的稿子：卡片上写一行原因，不让用户对着「没有按钮」猜
        src = _doc_sources(con, r)
        card = templates.with_doc_failures(card, fails, src[0] if src else None)
        text += "".join("\n📄 %s 没能导入云文档：%s" % (name, reason) for name, _, reason in fails)
    if r["report_id"] is not None:
        from . import card as status_card  # card 依赖本模块的发送函数
        res = status_card.deliver(con, r["ticket_id"], card, patch=card_patcher)
        if res is not None and (res[0] or not _is_api_error(res[1])):  # 本轮有状态卡：结果直接覆盖到卡上，不另发消息
            return res
        if res is not None:
            # 飞书明确拒绝了这次覆盖（业务错误）：不在状态卡上反复重试，改发新消息（再被拒就走下面的文字兜底）
            alert("outbox %d（%s）结果覆盖状态卡被拒，改发新消息：%s" % (r["id"], r["ticket_id"], res[1]))
    ok, info, permanent = card_sender(r["reply_to"], card, idem)
    if ok or not (permanent or _is_api_error(info)):
        return ok, info, permanent
    if _reply_gone(info):  # 回复目标没了：卡片本身没问题，由 flush 改投到会话后原样重发
        return False, info, False
    # 飞书明确拒收了卡片（任何业务错误，不只是白名单里的码）：马上改发文字兜底，结果不能丢
    alert("outbox %d（%s）结果卡片发送失败，改发文字：%s" % (r["id"], r["ticket_id"], info))
    con.execute("UPDATE outbox SET card_json='', text=? WHERE id=?", (text, r["id"]))
    return sender(r["reply_to"], text, idem + "-t")


def _doc_sources(con, r):
    """这一行结果的 (工作目录, 产物列表)；不是结果行或查不到时返回 None。"""
    if r["report_id"] is None:
        return None
    rep = con.execute("SELECT artifacts_json FROM reports WHERE id=?", (r["report_id"],)).fetchone()
    t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
    if not rep or not t:
        return None
    try:
        arts = json.loads(rep["artifacts_json"] or "[]")
    except ValueError:
        return None
    return t["workdir"], arts if isinstance(arts, list) else []


def _doc_links(con, r, allow_import: bool = True) -> list:
    """结果卡片上的稿子：把本地文档产物导入成云文档，拿到链接（有缓存，重试不重复导入）。
    allow_import=False 时只用缓存里已有的链接（主循环里不做最长 90 秒一个的导入）。"""
    src = _doc_sources(con, r)
    if src is None:
        return []
    from . import publish  # publish 依赖本模块的 alert / _parse_json
    try:
        return publish.doc_links(con, r["ticket_id"], src[0], src[1], allow_import=allow_import)
    except sqlite3.OperationalError:
        raise  # 库的问题交给 flush 分类（锁等待退避 / 故障告警），不当成导入失败吞掉
    except Exception as e:  # 导入是锦上添花：任何意外都不能挡住结果送达
        alert("%s 稿子链接生成异常：%r" % (r["ticket_id"], e))
        return []


def _doc_failures(con, r) -> list:
    src = _doc_sources(con, r)
    if src is None:
        return []
    from . import publish
    try:
        return publish.failures(con, r["ticket_id"], src[0], src[1])
    except sqlite3.OperationalError:
        raise
    except Exception as e:
        alert("%s 稿子失败原因读取异常：%r" % (r["ticket_id"], e))
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


def _redirect_to_chat(con, r, info: str):
    """被回复的消息已撤回/删除：这一单还没发出的消息都改为直接发到会话里。返回更新后的行；查不到会话返回 None。"""
    t = con.execute("SELECT chat_id FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
    if not t or not (t["chat_id"] or "").startswith("oc_"):
        return None
    alert("outbox %d（%s）回复目标 %s 已撤回，改为直接发到会话：%s" % (r["id"], r["ticket_id"], r["reply_to"], info[:200]))
    con.execute("UPDATE outbox SET reply_to=? WHERE ticket_id=? AND reply_to=? AND status='pending'",
                (t["chat_id"], r["ticket_id"], r["reply_to"]))
    return con.execute("SELECT * FROM outbox WHERE id=?", (r["id"],)).fetchone()


def _card_dirty(con, ticket_id: str) -> None:
    """让状态卡下一轮重新刷新。card_at 归零之外还推后 updated_at：媒体通道在另一个线程里写这一笔时，主循环的
    card.refresh 可能正在 PATCH，它写回的 card_at（本轮开始的时间）会盖掉 0，updated_at 更晚，下一轮仍会被选中。"""
    con.execute("UPDATE tickets SET card_at=0, updated_at=? WHERE id=?", (db.now(), ticket_id))


def _poison(con, r, sender, e: Exception) -> bool:
    """发送这一行时抛了异常：先试一次文字兜底（送到了算 sent，并绑定消息以便追问），不行再标 dead。
    返回 True = 结果写不进库（本轮这条通道停下，内存里退避，免得每轮重发一遍文字）。"""
    tok, tinfo = False, None
    try:
        tok, tinfo, _ = sender(r["reply_to"], r["text"], _idem(r) + "-t")
    except Exception:
        pass
    try:
        with db.tx(con):
            con.execute("UPDATE outbox SET status=?, attempts=?, last_error=?, sent_message_id=? WHERE id=?",
                        ("sent" if tok else "dead", r["attempts"] + 1, repr(e), tinfo if tok else None, r["id"]))
            if tok and tinfo:
                con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (tinfo, r["ticket_id"]))
            _card_dirty(con, r["ticket_id"])
    except sqlite3.Error as e2:
        delay = _mem_backoff(r["id"])
        alert("outbox %d（%s）发送异常 %r，%s，但结果写不进库（%r），%d 秒内不重试"
              % (r["id"], r["ticket_id"], e, "已改发文字" if tok else "文字兜底也没发出", e2, delay))
        return True
    _db_faults.pop(r["id"], None)
    alert("outbox %d %s（%s）：发送异常 %r" % (r["id"], "已改发文字" if tok else "dead", r["ticket_id"], e))
    if not tok:
        _notify_dead(con, r, repr(e))
    return False


# ---------- 投递通道 ----------
# 守护进程里发件箱分两条通道。主循环的「快通道」负责所有发送（文字、卡片、结果覆盖状态卡，每次最多 30 秒）；
# 慢的准备工作交给「媒体通道」（daemon.MediaLane，一个有并发上限的任务池）：结果卡里图片/视频的上传、稿子导入
# 云文档，以及视频消息（lark-cli 上传和发送是同一条命令）。一个 100MB 视频传 7 分钟时，派单、状态卡、心跳、
# 其他工单的文字投递，以及其他工单的图片和稿子准备都照常走。同一工单仍严格按 id 顺序：每单只取最早一条
# 待发消息，它没准备好，同单后面的消息就等着。
LANE_ALL, LANE_FAST, LANE_MEDIA = "all", "fast", "media"
PREPARED = "媒体已准备好："  # 媒体通道做完了慢的部分：不算失败、不退避，交给快通道发送
_claimed = set()  # 正在被某条通道处理的行（同一进程里两个线程之间互斥；只有守护进程投递，不需要跨进程）
_claimed_lock = threading.Lock()
_docs_tried = set()  # 媒体通道已经替它导入过稿子的行：导入失败的稿子不再让这一行留在媒体通道（重启后再试一次）


def _claim(row_id: int) -> bool:
    with _claimed_lock:
        if row_id in _claimed:
            return False
        _claimed.add(row_id)
        return True


def _release(row_id: int) -> None:
    with _claimed_lock:
        _claimed.discard(row_id)


def needs_media(r, con=None) -> bool:
    """这一行发出去之前要不要先做慢的准备：视频消息、卡片里还有待上传的占位、或结果里有还没导入的稿子。"""
    if "media_json" in r.keys() and r["media_json"]:
        return True
    if not r["card_json"]:
        return False
    try:
        if _has_uploads(json.loads(r["card_json"])):
            return True
    except ValueError:
        return False  # 坏卡片交给快通道按毒丸处理（文字兜底）
    if con is None or r["id"] in _docs_tried:
        return False
    src = _doc_sources(con, r)
    if src is None:
        return False
    from . import publish
    return publish.has_pending(con, r["ticket_id"], src[0], src[1])


def _in_lane(r, lane: str, con) -> bool:
    return lane == LANE_ALL or (lane == LANE_MEDIA) == needs_media(r, con)


DB_FAULT_POISON_AFTER = 3  # 数据库故障（不是锁等待）在同一行上出现这么多次、且这一行已等了 MEDIA_DEGRADE_AFTER，才改发文字
_db_faults = {}  # 行 id -> (连续故障次数, 内存里的退避截止 _clock 时间)。库写不进去时退避也记不进库，只能记在内存


def _mem_backoff(row_id: int) -> float:
    """在内存里给这一行记一次故障并退避（30 秒起翻倍，最多 10 分钟），返回退避秒数。"""
    n = _db_faults.get(row_id, (0, 0.0))[0] + 1
    delay = min(30.0 * 2 ** (n - 1), 600.0)
    _db_faults[row_id] = (n, _clock() + delay)
    return delay


def _backing_off(row_id: int) -> bool:
    f = _db_faults.get(row_id)
    return f is not None and _clock() < f[1]


def _db_fault(con, r, sender, e: Exception) -> bool:
    """磁盘 I/O、只读、库满、缺表缺列等真正的数据库故障：明确告警，不当锁等待挂 24 小时。
    这一行单独退避（不挡其他工单）；同一行反复出错且已等够 10 分钟、库还能写，才按毒丸改发文字。
    库连退避都写不进去时返回 True：本轮这条通道停下——发了也记不下来，只会每轮重复发。"""
    delay = _mem_backoff(r["id"])
    n = _db_faults[r["id"]][0]
    try:
        con.execute("UPDATE outbox SET next_at=?, last_error=? WHERE id=?", (db.now() + delay, repr(e), r["id"]))
        writable = True
    except sqlite3.Error:
        writable = False
    alert("outbox %d（%s）数据库故障（不是锁等待，需要人工检查；第 %d 次，%d 秒后重试%s）：%r"
          % (r["id"], r["ticket_id"], n, delay, "" if writable else "，库写不进去，本轮暂停这条通道", e))
    if writable and n >= DB_FAULT_POISON_AFTER and db.now() - r["created_at"] >= MEDIA_DEGRADE_AFTER:
        return _poison(con, r, sender, e)
    return not writable


def _heads(con) -> list:
    """现在可以处理的行：每单只取最早一条待发消息（同一工单严格按 id 顺序），到了重试时间，状态卡不在等待中。"""
    now = db.now()
    return con.execute(
        "SELECT * FROM outbox WHERE id IN (SELECT MIN(id) FROM outbox WHERE status='pending' GROUP BY ticket_id)"
        " AND next_at<=? AND ticket_id NOT IN (SELECT id FROM tickets WHERE card_wanted=1"
        " AND card_message_id IS NULL AND created_at>?) ORDER BY id",
        (now, now - CARD_HOLD)).fetchall()


def _claimed_now(row_id: int) -> bool:
    with _claimed_lock:
        return row_id in _claimed


def is_video_job(r) -> bool:
    """准备这一行要不要上传视频：视频消息，或卡片里还有没拿到云空间链接的视频占位（只看占位，不读文件）。
    媒体任务池据此单独限制视频的并发（大文件占带宽和内存，也容易撞限流）。"""
    if "media_json" in r.keys() and r["media_json"]:
        return True
    try:
        card = json.loads(r["card_json"]) if r["card_json"] else None
    except ValueError:
        return False
    body = card.get("body") if isinstance(card, dict) else None
    els = body.get("elements") if isinstance(body, dict) else None
    if not isinstance(els, list):
        return False
    # 只看顶层：_resolve_videos 也只处理顶层的视频占位
    return any(isinstance(el, dict) and templates.VIDEO_KEY in el and not el.get(templates.VIDEO_URL_KEY)
               for el in els)


def media_jobs(con) -> list:
    """媒体任务池的调度：现在要准备媒体、又没人在处理的行，返回 [(行, 是否要传视频)]，按 id 排序。
    每单只会出现最早那一行：它没准备好、没发出去，同单后面的消息不会被取到。"""
    out = []
    for r in _heads(con):
        if _backing_off(r["id"]) or _claimed_now(r["id"]) or not needs_media(r, con):
            continue
        out.append((r, is_video_job(r)))
    return out


def prepare(con, row_id: int, sender=None, card_sender=None) -> bool:
    """媒体任务池里的一个任务：在调用方的线程里、用调用方自己的连接准备这一行（调用方已经认领了 row_id）。
    返回 True = 遇到网络/超时或库被占用，调度方先停一轮再派新任务。"""
    r = con.execute("SELECT * FROM outbox WHERE id=? AND status='pending' AND next_at<=?",
                    (row_id, db.now())).fetchone()
    if r is None or not needs_media(r, con):
        return False  # 认领之前另一条通道已经发掉、改成退避或准备好了
    return _flush_row(con, r, sender or send_reply, card_sender or send_card, None, LANE_MEDIA)


def flush(con, sender=None, limit: int = 5, card_sender=None, card_patcher=None, lane: str = LANE_ALL) -> None:
    """发一轮。lane：all（一个线程里全做，CLI/测试用）、fast（守护进程主循环）、media（单线程准备媒体，测试用；
    守护进程里媒体通道是 daemon.MediaLane 的任务池，按 media_jobs / prepare 逐行派发）。"""
    if lane not in (LANE_ALL, LANE_FAST, LANE_MEDIA):
        raise ValueError("未知的投递通道：%r" % lane)
    sender = sender or send_reply
    card_sender = card_sender or send_card
    rows = _heads(con)
    done = 0
    for r in rows:
        if done >= limit:
            break
        rid = r["id"]
        if _backing_off(rid) or not _in_lane(r, lane, con) or not _claim(rid):
            continue
        try:
            # 认领之后按库里的最新状态再核一次：另一条通道可能刚把它发掉、改成退避，或已把媒体准备好
            r = con.execute("SELECT * FROM outbox WHERE id=? AND status='pending' AND next_at<=?",
                            (rid, db.now())).fetchone()
            if r is None or not _in_lane(r, lane, con):
                continue
            done += 1
            if _flush_row(con, r, sender, card_sender, card_patcher, lane):
                break
        finally:
            _release(rid)


def _notify_dead(con, r, info: str) -> None:
    """一条消息彻底没送达：私信主用户（原来只写本机 alerts.log，T0016 的结果丢了没人知道）。系统消息自己没送达就不再嵌套通知。"""
    from . import metrics
    if r["ticket_id"] == metrics.SYSTEM_TICKET:
        # 私信主用户本身失败了：没法再私信，只能写本机告警，并直接给出最可能的原因
        alert("私信主用户失败（告警或每周概况没送到）：%s。多半是 ~/.ticketd/config.env 里的 TICKETD_OWNER 不是当前"
              "飞书应用下你的 open_id——守护进程只读这个文件，不读 Claude Code 的环境变量；用 ticket doctor 检查" % short_error(info))
        return
    what = "结果" if r["report_id"] is not None else "一条消息"
    metrics.notify(con, r["ticket_id"], "delivery_dead",
                   "%s 的%s没能送达（%s）。内容可以用 ticket show %s 查看" % (
                       r["ticket_id"], what, short_error(info), r["ticket_id"]))


def short_error(info: str) -> str:
    err = _parse_error(info)
    if err.get("message"):
        return "%s%s" % (err.get("message"), "，code %s" % err["code"] if err.get("code") else "")
    return (info or "")[:80]


def _flush_row(con, r, sender, card_sender, card_patcher, lane) -> bool:
    """发一行并按结果记账。返回 True = 本轮不要再发其余行（网络/超时、库被占用或写不进去）。"""
    try:
        ok, info, permanent = _send_row(r, sender, card_sender, con, card_patcher=card_patcher, lane=lane)
        if not ok and _reply_gone(info) and not r["reply_to"].startswith(("oc_", "ou_")):
            moved = _redirect_to_chat(con, r, info)
            if moved is not None:
                r = moved
                ok, info, permanent = _send_row(r, sender, card_sender, con, card_patcher=card_patcher, lane=lane)
    except sqlite3.OperationalError as e:
        if db.is_busy(e) and db.now() - r["created_at"] <= 24 * 3600:
            # 库被别的写者占着（SQLITE_BUSY/LOCKED）：不是这一行的问题，稍后重试（不能当毒丸改发文字，会把卡片永久换掉）
            alert("outbox %d（%s）库被占用，稍后重试：%r" % (r["id"], r["ticket_id"], e))
            try:
                con.execute("UPDATE outbox SET next_at=?, last_error=? WHERE id=?", (db.now() + 30, repr(e), r["id"]))
            except sqlite3.Error:
                pass
            return True
        if not db.is_busy(e):
            return _db_fault(con, r, sender, e)
        return _poison(con, r, sender, e)
    except Exception as e:  # 毒丸行：发送器本身抛异常。标 dead 之前先试一次文字兜底，结果不能丢
        return _poison(con, r, sender, e)
    try:
        stop = _settle(con, r, ok, info, permanent, sender)
    except sqlite3.OperationalError as e:
        # 已经发出去（或已试过）但结果记不进库：不能按毒丸再发文字。内存里退避，到点用同一幂等键重试（飞书 1 小时内去重）
        delay = _mem_backoff(r["id"])
        alert("outbox %d（%s）发送后写库失败（%s），%d 秒内不重试：%r"
              % (r["id"], r["ticket_id"], "库被占用" if db.is_busy(e) else "数据库故障，需要人工检查", delay, e))
        return True
    _db_faults.pop(r["id"], None)
    return stop


def _settle(con, r, ok: bool, info: str, permanent: bool, sender) -> bool:
    """按发送结果记账。返回 True = 本轮不要再发其余行。"""
    if not ok and info.startswith((BUDGET_STOP, PREPARED)):
        # 上传时间用完（进度已写回，下一轮接着传）或媒体已准备好（交给快通道发）：不算失败、不退避
        con.execute("UPDATE outbox SET next_at=0, last_error=? WHERE id=?",
                    (None if info.startswith(PREPARED) else info, r["id"]))
        return False
    attempts = r["attempts"] + 1
    if r["report_id"] is not None and (ok or permanent or db.now() - r["created_at"] > 24 * 3600):
        # 结果已送达（或确定送不到）：让状态卡下一轮就收起成终态
        _card_dirty(con, r["ticket_id"])
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
        _notify_dead(con, r, info)
    elif db.now() - r["created_at"] > 24 * 3600:
        media = r["media_json"] if "media_json" in r.keys() else None
        if r["card_json"] or media:
            # 卡片/视频重试了 24 小时都没发出：放弃前先试一次文字兜底，结果至少以文字送到
            tok, tinfo, _ = sender(r["reply_to"], r["text"], _idem(r) + "-t")
            if tok:
                alert("outbox %d（%s）24 小时没发出，已改发文字：%s" % (r["id"], r["ticket_id"], info))
                with db.tx(con):
                    con.execute("UPDATE outbox SET status='sent', attempts=?, sent_message_id=?, last_error=?"
                                " WHERE id=?", (attempts, tinfo, info, r["id"]))
                    if tinfo:
                        con.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (tinfo, r["ticket_id"]))
                return False
        con.execute("UPDATE outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                    (attempts, info, r["id"]))
        alert("outbox %d dead（%s）：重试 %d 次仍失败：%s" % (r["id"], r["ticket_id"], attempts, info))
        _notify_dead(con, r, info)
    else:
        delay = min(5 * 2 ** (attempts - 1), 300)
        con.execute("UPDATE outbox SET attempts=?, next_at=?, last_error=? WHERE id=?",
                    (attempts, db.now() + delay, info, r["id"]))
        if attempts == 3:
            alert("outbox %d（%s）连续失败 3 次：%s" % (r["id"], r["ticket_id"], info))
    # 网络/超时：这条通道本轮不再尝试其余行（快、慢两条通道各自独立，互不拖住）
    return not ok and _is_network_failure(info)

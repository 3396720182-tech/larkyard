"""飞书用户身份：查有没有授权、缺授权时机器人私信授权链接、授权后补发没导入的文档（openspec add-user-auth-link）。

文档导入（drive +import / docs +update）和视频传云空间（drive +upload）是 ticketd 里仅有的用用户身份的地方。
这台电脑没授权、或授权后来失效时，这些都会失败；同事多半没有终端习惯，所以由守护进程用 bot 私信主人一张授权卡片，
点开在飞书里同意就行（lark-cli 的设备码授权），授权完成后把这次没导入的文档补发到原来那张单下面。

- 状态：kv `userauth.state`（design D2）。守护进程定时查（本地，不联网），出稿遇到授权类失败时直接记成没授权。
- 发链接：装好后发现没授权马上发一次（本机只发这一次）；之后只在出稿失败时再发（没有没过期的链接、离上次 ≥30 分钟）。
- 只报一次：授权类失败不算媒体失败（指标记 doc:auth / video:auth），这个原因只经自检项 user_auth 上报控制台。
设备码只放在 kv 里，不写告警、事件和诊断包。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from . import config, db, metrics, templates

# design D1：导入文档、覆盖改过的稿子、视频传云空间和查链接要用的权限。授权链接、终端授权都带上这一组
SCOPES = (
    "docs:document.media:upload", "docs:document:import",          # drive +import
    "docx:document:write_only", "docx:document:readonly",          # docs +update（改过的稿子覆盖原文档）
    "drive:file:upload", "drive:drive.metadata:readonly",          # drive +upload、drive metas batch_query
)
# 自检只认缺了就一定失败的这几项：覆盖原文档失败时会改为另导入一份，查链接是上传没回链接时的兜底，
# 缺它们不该把自检判成不通过、给已经能正常出稿的同事发链接
CHECK_SCOPES = ("docs:document.media:upload", "docs:document:import", "drive:file:upload")

STATE_KEY = "userauth.state"          # {"state", "at", "src", "missing"}
LINK_KEY = "userauth.link"            # {"code", "expires_at", "sent_at"}：还在等用户点的那条链接
FIRST_KEY = "userauth.first_link_at"  # 装好后的那一次发过了没有（本机只发一次）
LAST_KEY = "userauth.last_link_at"    # 上次发链接的时间
NEED_KEY = "userauth.need_link"       # 出稿遇到授权类失败：等守护进程发链接
BACKFILL_KEY = "userauth.backfill"    # 状态从没授权变成可用：等守护进程补发文档
RECHECK_KEY = "userauth.recheck"      # 终端里刚授权过 / 出稿时用户身份调用又成功了：守护进程下一拍就查
USE_OK_KEY = "userauth.use_ok_at"     # 最近一次用户身份调用成功的时间（出稿失败记下的「没授权」以它或重新授权作废）
OWNER_ALERT_KEY = "userauth.owner_alerted"

BAD = ("missing", "expired", "scope")
CHECK_EVERY = 1800.0
RESEND_AFTER = 1800.0
LINK_MAX_WAIT = 600.0  # lark-cli 的 --device-code 轮询最多 600 秒（login.go），链接有效期按这个封顶
MIN_GAP = 60.0
BACKFILL_DAYS = 7
CMD_TIMEOUT = 30

STATE_TEXT = {"missing": "没授权", "expired": "授权已失效", "scope": "缺权限"}


def _kv_del(con, key: str) -> None:
    con.execute("DELETE FROM kv WHERE key=?", (key,))


# ---------- lark-cli ----------
def _first_json(text: str) -> Optional[dict]:
    """输出里第一个 JSON 对象（auth status / login --no-wait 的输出没有 ok 字段，不能用 outbox._parse_json）。"""
    text = text or ""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _env() -> Optional[dict]:
    """守护进程的服务文件里带着 bridge profile 的 lark-cli 配置目录；终端里执行 ticket doctor 时没有，
    要按同样的规则补上，否则查的是另一份配置（会把守护进程查到的「已授权」误改成「没授权」）。"""
    if os.environ.get("LARKSUITE_CLI_CONFIG_DIR"):
        return None
    from . import bridge
    home, prof = bridge.lc_home(), bridge.profile_name()
    d = home / "profiles" / prof / "lark-cli"
    if not d.is_dir():
        return None
    return dict(os.environ, LARK_CHANNEL="1", LARK_CHANNEL_HOME=str(home), LARK_CHANNEL_PROFILE=prof,
                LARK_CHANNEL_CONFIG=str(home / "profiles" / prof / "lark-cli-source" / "config.json"),
                LARKSUITE_CLI_CONFIG_DIR=str(d))


_waiting = {"proc": None, "stopped": False}  # 正在等用户点链接的 lark-cli：守护进程停机时结束它（不留下和新进程抢同一个设备码的轮询）


def _lark(args: List[str], timeout: float = CMD_TIMEOUT, wait: bool = False) -> Tuple[int, Optional[dict], str]:
    """跑 lark-cli，返回 (退出码, 输出里的 JSON, 原文)。跑不起来时退出码为 -1。"""
    try:
        p = subprocess.Popen([config.lark_bin()] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             errors="replace", stdin=subprocess.DEVNULL, env=_env())
    except OSError as e:
        return -1, None, "调用 lark-cli 失败：%s" % e
    if wait:
        _waiting["proc"] = p
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        p.kill()
        p.communicate()
        return -1, None, "调用 lark-cli 失败：%s" % e
    finally:
        if wait:
            _waiting["proc"] = None
    return p.returncode, _first_json(out) or _first_json(err), (err or out or "")[-300:]


def stop() -> None:
    """守护进程停机：结束正在等用户点链接的 lark-cli（链接还在 kv 里，新进程起来后接着等）。"""
    _waiting["stopped"] = True
    p = _waiting["proc"]
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass


def _iso_ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def check() -> dict:
    """本地查一次（不联网）。返回 {"state": ok/missing/expired/scope/unknown, "missing": [...], "granted_at": 秒}。"""
    rc, data, raw = _lark(["auth", "status", "--json"])
    ids = data.get("identities") if isinstance(data, dict) else None
    user = ids.get("user") if isinstance(ids, dict) else None
    if not isinstance(user, dict) or not user.get("status"):
        return {"state": "unknown", "missing": [], "why": raw[-120:]}
    st = user.get("status")
    if st == "missing":
        msg = str(user.get("message") or "")
        return {"state": "expired" if "expired" in msg else "missing", "missing": []}
    if st not in ("ready", "needs_refresh"):
        return {"state": "unknown", "missing": [], "why": str(user.get("message") or st)[:120]}
    granted = _iso_ts(user.get("grantedAt"))
    rc, data, raw = _lark(["auth", "check", "--scope", " ".join(CHECK_SCOPES), "--json"])
    if not isinstance(data, dict) or "ok" not in data:
        return {"state": "unknown", "missing": [], "why": raw[-120:]}
    if data.get("ok") is True:
        return {"state": "ok", "missing": [], "granted_at": granted}
    if data.get("error") in ("not_logged_in", "no_token"):
        return {"state": "missing", "missing": []}
    miss = [s for s in (data.get("missing") or []) if isinstance(s, str)]
    return {"state": "scope", "missing": miss[:10], "granted_at": granted}


# ---------- 状态 ----------
def load(con) -> dict:
    try:
        st = json.loads(metrics.kv_get(con, STATE_KEY) or "")
        if isinstance(st, dict) and st.get("state") in BAD + ("ok", "unknown"):
            return st
    except ValueError:
        pass
    return {}


def _save(con, state: str, src: str, missing=None, now: Optional[float] = None) -> None:
    prev = load(con).get("state")
    metrics.kv_set(con, STATE_KEY, json.dumps({"state": state, "at": now or db.now(), "src": src,
                                               "missing": list(missing or [])}, ensure_ascii=False))
    if state == "ok":
        _kv_del(con, NEED_KEY)
        if prev in BAD:
            metrics.kv_set(con, BACKFILL_KEY, "1")  # 没授权 → 可用：补发这期间没导入的文档


def refresh(con, result: Optional[dict] = None, now: Optional[float] = None) -> str:
    """查一次并记下来（design D2）。查不到（unknown）不覆盖已知的结论；出稿失败记下的「没授权」只有在
    之后重新授权过（token 的授权时间更晚）时才被「可用」覆盖——本地检查看不出服务端撤销的授权。返回这次查到的状态。"""
    r = result if result is not None else check()
    cur = load(con)
    state = r["state"]
    if state == "unknown":
        if not cur:
            _save(con, "unknown", "check", now=now)
        return state
    if state == "ok" and cur.get("src") == "failure" and cur.get("state") in BAD:
        since = max(r.get("granted_at", 0), float(metrics.kv_get(con, USE_OK_KEY) or 0))
        if since <= float(cur.get("at") or 0):
            return cur["state"]
    _save(con, state, "check", r.get("missing"), now=now)
    return state


# ---------- 失败归类（design D6） ----------
AUTH_SUBTYPES = ("missing_scope", "user_unauthorized", "token_scope_insufficient")
APP_SUBTYPES = ("app_scope_not_applied", "app_unavailable", "app_disabled")


def _error_of(info: str) -> dict:
    """失败信息里 lark-cli 的 error 对象（形如「rc=3 {...}」「上传云空间失败：rc=3 {...}」）。"""
    i = (info or "").find("{")
    while i >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(info, i)
            if isinstance(obj, dict):
                return obj.get("error") if isinstance(obj.get("error"), dict) else obj
        except ValueError:
            pass
        i = info.find("{", i + 1)
    return {}


def classify(info: str) -> str:
    """auth（缺用户授权，发链接能解决）/ app（应用没开通，要管理员）/ network / other。"""
    info = info or ""
    if "调用 lark-cli 失败" in info or '"type": "network"' in info or '"retryable": true' in info:
        return "network"
    err = _error_of(info)
    typ, sub = str(err.get("type") or ""), str(err.get("subtype") or "")
    if typ == "authentication" and sub != "refresh_server_error":
        return "auth"
    if typ == "authorization" and sub in AUTH_SUBTYPES:
        return "auth"
    if typ == "authorization" and sub in APP_SUBTYPES:
        return "app"
    if "need_user_authorization" in info:
        return "auth"
    return "other"


def _state_of_failure(info: str) -> str:
    sub = str(_error_of(info).get("subtype") or "")
    if sub in ("missing_scope", "token_scope_insufficient"):
        return "scope"
    if "refresh_token" in sub or sub == "token_expired":
        return "expired"
    return "missing"


def reason_text(info: str, what: str = "导入文档") -> str:
    """给用户看的中文原因和下一步（结果卡、文字兜底、doc_failures.reason）。"""
    cls = classify(info)
    if cls == "auth":
        return "飞书用户身份没授权或已失效。机器人已私信你授权链接，点开同意后会自动补发这份文档"
    if cls == "app":
        return "飞书应用没开通%s的权限，请管理员在飞书开放平台给应用开通后再试" % what
    if cls == "network":
        return "网络不通或超时，下次汇报时会自动重试"
    msg = str(_error_of(info).get("message") or "")
    if not msg:
        m = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', info or "")
        msg = m.group(1) if m else (info or "未知原因")
    return "飞书没能%s（原因：%s）。可以对机器人说「把诊断包发给管理员」" % (what, templates.short(msg, 60))


def mark_failure(con, info: str) -> bool:
    """出稿时用户身份调用失败：授权类的记成没授权、要求发链接，返回 True；其余返回 False。永不抛异常。"""
    try:
        if classify(info) != "auth":
            return False
        _save(con, _state_of_failure(info), "failure")
        metrics.kv_set(con, NEED_KEY, str(db.now()))
        return True
    except Exception:  # noqa: BLE001  授权状态是附带的：不能挡住发结果
        return False


def mark_ok(con) -> None:
    """用户身份调用成功了（导入 / 上传）：记下时间；之前记着没授权的，让守护进程现查一次（不在这里直接改成 ok：
    缺的可能只是另一项权限，直接改会让自检红绿来回跳）。永不抛异常。"""
    try:
        metrics.kv_set(con, USE_OK_KEY, str(db.now()))
        if load(con).get("state") in BAD:
            metrics.kv_set(con, RECHECK_KEY, "1")
    except Exception:  # noqa: BLE001
        pass


def request_recheck(con) -> None:
    """终端里刚授权过：让守护进程下一拍就查（查到可用时补发文档）。"""
    metrics.kv_set(con, RECHECK_KEY, "1")


# ---------- 自检 ----------
def doctor_line(con) -> str:
    st = load(con)
    s = st.get("state")
    if s == "ok":
        return "✅ 飞书用户身份已授权（导入文档、传视频用）"
    if s in BAD:
        extra = "：缺 %s" % "、".join(st.get("missing") or []) if s == "scope" and st.get("missing") else ""
        return ("❌ 飞书用户身份%s%s（导入文档、传视频要用）：机器人已私信你授权链接，点开同意即可；"
                "也可以在终端执行 ticket setup --user-auth" % (STATE_TEXT[s], extra))
    if s == "unknown":
        return "⚠️ 飞书用户身份查不到（lark-cli 或钥匙串读不了），守护进程会再查"
    return "⚠️ 飞书用户身份还没检查过（守护进程启动后会查）"


def live_link(con, now: Optional[float] = None) -> Optional[dict]:
    now = db.now() if now is None else now
    try:
        link = json.loads(metrics.kv_get(con, LINK_KEY) or "")
    except ValueError:
        return None
    if isinstance(link, dict) and link.get("code") and float(link.get("expires_at") or 0) > now:
        return link
    return None


def status_lines(con) -> List[str]:
    """诊断包用：状态、检查时间、有没有没过期的链接、待补发几份。不带设备码。"""
    st = load(con)
    lines = ["状态：%s（%s，%s）" % (st.get("state") or "还没查过", {"check": "定时检查", "failure": "出稿失败时记下",
                                                              "use": "导入成功", "login": "终端授权"}.get(st.get("src"), "—"),
                                 time.strftime("%m-%d %H:%M", time.localtime(float(st.get("at") or 0))) if st else "—")]
    if st.get("missing"):
        lines.append("缺的权限：%s" % "、".join(st["missing"]))
    link = live_link(con)
    lines.append("授权链接：%s" % ("有一条还没过期（%s 到期）" % time.strftime("%H:%M", time.localtime(link["expires_at"]))
                                if link else "没有在等的"))
    last = metrics.kv_get(con, LAST_KEY)
    if last:
        lines.append("上次发链接：%s" % time.strftime("%m-%d %H:%M", time.localtime(float(last))))
    try:
        n = con.execute("SELECT COUNT(*) FROM auth_backfill").fetchone()[0]
        lines.append("等授权后补发的文档：%d 份" % n)
    except Exception:  # noqa: BLE001  表还没建
        pass
    return lines


# ---------- 发链接（design D4） ----------
def link_card(url: str, minutes: int) -> dict:
    els = [templates.text("这台电脑还没授权飞书用户身份（或授权已失效）。结果里的稿子导入成云文档、视频传到云空间都要用它。"),
           templates.text("点下面的按钮，在飞书里同意就好，不用开终端；需要的权限已经选好。授权完成后，之前没导入的文档会自动补发。"),
           {"tag": "button", "text": {"tag": "plain_text", "content": "去授权"}, "type": "primary_filled",
            "behaviors": [{"type": "open_url", "default_url": url}]},
           templates.note("链接 %d 分钟内有效。过期了不用管，下次出稿遇到时会再发；也可以在终端执行 ticket setup --user-auth。"
                          % minutes)]
    return templates.card("飞书用户身份需要授权", "飞书用户身份需要授权", "点一下就好", "orange", els)


def _owner_ok() -> bool:
    return config.owner().startswith("ou_")


def send_link(con, now: Optional[float] = None) -> bool:
    """申请一个设备码，私信主人授权卡片。返回是否已排进发件箱。"""
    from . import outbox
    now = db.now() if now is None else now
    if not _owner_ok():
        return False
    rc, data, raw = _lark(["auth", "login", "--no-wait", "--json", "--scope", " ".join(SCOPES)])
    url = data.get("verification_url") if isinstance(data, dict) else None
    code = data.get("device_code") if isinstance(data, dict) else None
    if not (isinstance(url, str) and url.startswith("https://") and isinstance(code, str) and code):
        outbox.alert("飞书用户身份授权链接没申请到：%s" % (raw or "lark-cli 没有返回链接")[-200:])
        return False
    try:
        ttl = int(data.get("expires_in") or 240)
    except (TypeError, ValueError):
        ttl = 240
    ttl = max(60, min(ttl, int(LINK_MAX_WAIT)))
    minutes = max(1, ttl // 60)
    text = ("🔑 这台电脑还没授权飞书用户身份（导入文档、传视频要用）。点这个链接在飞书里同意就好（%d 分钟内有效）：%s"
            % (minutes, url))
    with db.tx(con):
        con.execute("INSERT INTO outbox(ticket_id, reply_to, text, card_json, created_at) VALUES(?,?,?,?,?)",
                    (metrics.SYSTEM_TICKET, metrics.OWNER, text, json.dumps(link_card(url, minutes), ensure_ascii=False),
                     now))
        metrics.kv_set(con, LINK_KEY, json.dumps({"code": code, "expires_at": now + ttl, "sent_at": now}))
        metrics.kv_set(con, LAST_KEY, str(now))
        if not metrics.kv_get(con, FIRST_KEY):
            metrics.kv_set(con, FIRST_KEY, str(now))
        _kv_del(con, NEED_KEY)
    return True


def wait_link(link: dict, now: Optional[float] = None) -> bool:
    """等用户在链接里同意（lark-cli 轮询，最多到链接过期）。返回是否授权成功。"""
    now = time.time() if now is None else now
    left = max(10.0, min(float(link["expires_at"]) - now, LINK_MAX_WAIT)) + 30
    rc, data, raw = _lark(["auth", "login", "--device-code", link["code"], "--json"], timeout=left, wait=True)
    # 用户只同意了部分权限时，lark-cli 照样存好 token、输出 authorization_complete（带 missing_scope 警告），但退出码是 3
    return rc == 0 or (isinstance(data, dict) and data.get("event") == "authorization_complete")


def _send_owner_card(con, title: str, lines: List[str], color: str = "green") -> None:
    els = [templates.text(x) for x in lines]
    con.execute("INSERT INTO outbox(ticket_id, reply_to, text, card_json, created_at) VALUES(?,?,?,?,?)",
                (metrics.SYSTEM_TICKET, metrics.OWNER, "%s\n%s" % (title, "\n".join(lines)),
                 json.dumps(templates.card(title, title, "飞书用户身份", color, els), ensure_ascii=False), db.now()))


# ---------- 补发文档（design D5） ----------
def remember(con, ticket_id: str, path: str, reply_to: Optional[str]) -> None:
    """记下因为没授权没导入的稿子（授权后补发）。reply_to 为空（不是结果消息）时不记。"""
    if not reply_to:
        return
    con.execute("INSERT OR REPLACE INTO auth_backfill VALUES(?,?,?,?)", (ticket_id, path, reply_to, db.now()))


def forget(con, ticket_id: str, path: str) -> None:
    con.execute("DELETE FROM auth_backfill WHERE ticket_id=? AND path=?", (ticket_id, path))


def backfill(con, importer: Optional[Callable] = None, overwriter: Optional[Callable] = None) -> int:
    """授权好了：把 7 天内因为没授权没导入的稿子导入，每张单一条消息回复在原结果下面。返回补发了几份。
    和发结果时一样：这张单这个路径已经有云文档的 Markdown / 文本稿先覆盖原文档（issue #13），不另导入一份。"""
    from . import outbox, publish
    importer = importer or publish._import
    overwriter = overwriter or publish._overwrite
    con.executescript(publish.SCHEMA)
    con.execute("DELETE FROM auth_backfill WHERE created_at<?", (db.now() - BACKFILL_DAYS * 86400,))
    rows = con.execute("SELECT * FROM auth_backfill ORDER BY created_at").fetchall()
    done, sent = {}, 0
    for r in rows:
        t = con.execute("SELECT workdir FROM tickets WHERE id=?", (r["ticket_id"],)).fetchone()
        # 按发布时同样的规则重新核对（真实路径在工单目录或授权目录里、不是内部文件、普通文件、大小在限内）
        ps = publish.candidates(con, r["ticket_id"], t["workdir"], [r["path"]]) if t else []
        if not ps or str(ps[0]) != r["path"]:
            forget(con, r["ticket_id"], r["path"])
            continue
        p = ps[0]
        try:
            st, url = publish._cached(con, p)
        except OSError:
            forget(con, r["ticket_id"], r["path"])
            continue
        if url:  # 后来的汇报已经导入成功（卡片上有按钮了）
            forget(con, r["ticket_id"], r["path"])
            continue
        start = time.monotonic()
        prev = con.execute("SELECT url FROM doc_targets WHERE ticket_id=? AND path=?",
                           (r["ticket_id"], str(p))).fetchone()
        url = None
        try:
            if prev and p.suffix.lower() in publish.OVERWRITABLE:
                try:
                    overwriter(p, prev["url"])
                    url = prev["url"]
                except RuntimeError as e:
                    if classify(str(e)) in ("auth", "network"):
                        raise
                    outbox.alert("%s 补发时覆盖原云文档失败，改为另导入一份（%s）：%s" % (r["ticket_id"], p.name, e))
            if url is None:
                name = "%s %s" % (r["ticket_id"], p.stem)
                if prev:
                    name += "（%s）" % time.strftime("%m-%d %H:%M")
                url = importer(p, name)
        except RuntimeError as e:
            outbox.alert("%s 授权后补发稿子失败（%s）：%s" % (r["ticket_id"], p.name, e))
            if mark_failure(con, str(e)) or classify(str(e)) == "network":
                break  # 还是没授权 / 网络不通：留着记录，等下一次授权或下一次检查
            forget(con, r["ticket_id"], r["path"])
            continue
        with db.tx(con):
            con.execute("INSERT OR REPLACE INTO doc_targets VALUES(?,?,?,?)", (r["ticket_id"], str(p), url, db.now()))
            con.execute("INSERT OR REPLACE INTO published VALUES(?,?,?,?,?)",
                        (str(p), st.st_mtime, st.st_size, url, db.now()))
            forget(con, r["ticket_id"], r["path"])
        outbox._metric(con, r["ticket_id"], "media", "doc:ok", time.monotonic() - start, st.st_size)
        done.setdefault((r["ticket_id"], r["reply_to"]), []).append((p.name, url))
        sent += 1
    for (tid, reply_to), links in done.items():
        title = "📄 授权好了，补上 %s 没导入的文档" % tid
        card = templates.card(title, title, "之前因为没授权飞书用户身份没导入", "green", templates.doc_buttons(links))
        text = title + "".join("\n📄 %s：%s" % (n, u) for n, u in links)
        con.execute("INSERT INTO outbox(ticket_id, reply_to, text, card_json, created_at) VALUES(?,?,?,?,?)",
                    (tid, reply_to, text, json.dumps(card, ensure_ascii=False), db.now()))
    return sent


# ---------- 守护进程 ----------
def step(con, now: Optional[float] = None) -> str:
    """守护进程后台线程里做一次（可能阻塞到链接过期）。返回做了什么（测试和日志用）。"""
    from . import outbox
    now = db.now() if now is None else now
    link = live_link(con, now)
    if link:
        _waiting["stopped"] = False
        ok = wait_link(link, now)
        if _waiting.get("stopped"):
            return "stopped"  # 守护进程停机打断的：链接留着，新进程起来后接着等
        _kv_del(con, LINK_KEY)
        if not ok:
            return "link_expired"
        r = check()
        state = refresh(con, dict(r, granted_at=max(r.get("granted_at", 0), db.now())) if r["state"] == "ok" else r)
        if state == "ok":
            n = backfill(con)
            _kv_del(con, BACKFILL_KEY)
            if load(con).get("state") == "ok":  # 补发时又遇到授权失败就不说「好了」
                _send_owner_card(con, "✅ 飞书用户身份授权好了", [
                    "以后结果里的稿子会自动导入成云文档、视频会传到云空间。" +
                    ("之前没导入的 %d 份文档已经补发到原来的单子下面。" % n if n else "")])
        elif state == "scope":
            _send_owner_card(con, "⚠️ 授权完成了，但还缺权限", [
                "还缺：%s。多半是飞书应用没开通这几项，请管理员在飞书开放平台给应用开通后再试。"
                % "、".join(load(con).get("missing") or [])], color="orange")
        return "authorized:" + state
    _kv_del(con, RECHECK_KEY)
    state = refresh(con, now=now)
    if metrics.kv_get(con, BACKFILL_KEY):
        _kv_del(con, BACKFILL_KEY)  # 不是 ok 也清掉：只有查到从没授权变成 ok 时才会再置上
        if load(con).get("state") == "ok":
            backfill(con)
            return "backfilled"
    cur = load(con).get("state")
    if cur not in BAD:
        return "checked:" + state
    need = metrics.kv_get(con, NEED_KEY)
    last = float(metrics.kv_get(con, LAST_KEY) or 0)
    if not metrics.kv_get(con, FIRST_KEY) or (need and now - last >= RESEND_AFTER):
        if send_link(con, now):
            return "link_sent"
        # 没发出去（主人没配好、申请链接失败）：也按发过算时间，30 分钟后再试，不每分钟重试
        metrics.kv_set(con, LAST_KEY, str(now))
        if not _owner_ok() and not metrics.kv_get(con, OWNER_ALERT_KEY):
            metrics.kv_set(con, OWNER_ALERT_KEY, "1")
            outbox.alert("飞书用户身份没授权，但守护进程读到的 TICKETD_OWNER 无效，授权链接发不出去")
    return "checked:" + state


class Lane:
    """守护进程主循环每拍调 tick()：要查状态、要发链接、有链接在等、要补发时，在后台线程里做（同一时间只有一个）。"""

    def __init__(self, clock=time.time, worker=None):
        self.clock = clock
        self.worker = worker or self._work
        self.thread = None
        self.next_check = None
        self.not_before = 0.0
        self.last = None

    def _work(self) -> None:
        con = db.connect()
        try:
            self.last = step(con)
        except Exception as e:  # noqa: BLE001  授权是附带的：出什么错都不影响守护进程
            self.last = "error:%r" % (e,)
        finally:
            con.close()

    def due(self, con, now: float) -> bool:
        if self.next_check is None:
            self.next_check = now + 20  # 刚启动：先让派单和发件箱跑起来
            return False
        if live_link(con, now) is not None:  # 守护进程重启前发出的链接还没过期：接着等
            return True
        if now < self.not_before:  # 上一次刚做完：别每拍都跑 lark-cli（发链接失败、补发失败时不刷）
            return False
        if now >= self.next_check or metrics.kv_get(con, BACKFILL_KEY) or metrics.kv_get(con, RECHECK_KEY):
            return True
        return bool(metrics.kv_get(con, NEED_KEY)) and now - float(metrics.kv_get(con, LAST_KEY) or 0) >= RESEND_AFTER

    def tick(self, con) -> bool:
        """返回这一拍是否启动了后台线程。"""
        if self.thread is not None and self.thread.is_alive():
            return False
        now = self.clock()
        if not self.due(con, now):
            return False
        if now >= self.next_check:
            self.next_check = now + CHECK_EVERY
        self.not_before = now + MIN_GAP
        self.thread = threading.Thread(target=self.worker, name="ticketd-userauth", daemon=True)
        self.thread.start()
        return True

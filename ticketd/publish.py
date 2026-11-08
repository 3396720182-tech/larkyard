"""把结果里的稿子（本地文档类产物）导入成飞书云文档，结果卡片上给一键打开的按钮。

- 只在发结果卡片时做（outbox 发送前），导入失败不影响结果送达，只是没有按钮。
- 只发布工单目录和授权目录里的文件；ticketd 的内部文件不发布。
- 用用户身份导入，文档落在用户自己的云空间，打开不用再申请权限。
- 同一文件（路径 + 修改时间 + 大小）只导入一次，重试和追问轮次复用同一个链接。
- 同一工单同一路径的稿子改过之后：Markdown / 文本稿覆盖原来那份云文档（链接不变，旧版本在文档历史里），
  其他格式或覆盖失败时才另导入一份，名字带上时间以便区分（issue #13：原来每轮追问都多出一份同名文档）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import List, Tuple

from . import config, db, outbox

DOC_TYPES = {".md": "docx", ".markdown": "docx", ".txt": "docx", ".docx": "docx",
             ".xlsx": "sheet", ".csv": "sheet", ".pptx": "slides"}
INTERNAL = {".reports", ".stream.jsonl", "过程.log", ".stderr.log"}
OVERWRITABLE = {".md", ".markdown", ".txt"}  # docs +update 能用 Markdown 整篇覆盖的格式
MAX_DOCS = 3
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS published(
  path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL, url TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY(path, mtime, size)
);
-- 每张单每个稿子路径当前对应的云文档：改过的稿子覆盖它，而不是再导入一份
CREATE TABLE IF NOT EXISTS doc_targets(
  ticket_id TEXT NOT NULL, path TEXT NOT NULL, url TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(ticket_id, path)
);
-- 导入失败的稿子（按文件版本记）：发结果时在卡片上写一行原因，用户知道为什么没有「打开」按钮（issue #12）
CREATE TABLE IF NOT EXISTS doc_failures(
  path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(path, mtime, size)
);
"""


def _allowed_roots(con, ticket_id: str, workdir: str) -> List[str]:
    roots = [workdir] + [r["path"] for r in con.execute("SELECT path FROM grants WHERE ticket_id=?", (ticket_id,))]
    return [os.path.realpath(r) for r in roots if r]


def candidates(con, ticket_id: str, workdir: str, artifacts: List[str]) -> List[Path]:
    """结果产物里可以发布成云文档的本地文件（去重、按原顺序、最多 MAX_DOCS 个）。"""
    roots = _allowed_roots(con, ticket_id, workdir)
    out: List[Path] = []
    for a in artifacts:
        if not isinstance(a, str) or a.startswith(("http://", "https://")):
            continue
        p = Path(os.path.realpath(a if os.path.isabs(a) else os.path.join(workdir, a)))
        if p.suffix.lower() not in DOC_TYPES or p.name.startswith(".input-") or p in out:
            continue
        if not any(str(p).startswith(r.rstrip("/") + "/") for r in roots):
            continue
        if any(part in INTERNAL for part in p.parts):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file() or st.st_size == 0 or st.st_size > MAX_BYTES:
            continue
        out.append(p)
        if len(out) >= MAX_DOCS:
            break
    return out


def _import(path: Path, name: str) -> str:
    """调 lark-cli 导入成云文档，返回链接；失败抛 RuntimeError。"""
    cmd = [config.lark_bin(), "drive", "+import", "--as", "user", "--type", DOC_TYPES[path.suffix.lower()],
           "--file", path.name, "--name", name]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=str(path.parent))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("调用 lark-cli 失败：%s" % e)
    data = outbox._parse_json(p.stdout) or outbox._parse_json(p.stderr) or {}
    body = data.get("data") if data.get("ok") else None
    url = body.get("url") if isinstance(body, dict) else None
    if not url:
        raise RuntimeError("rc=%s %s" % (p.returncode, _error_text(data, p.stderr or p.stdout)))
    return url


def _overwrite(path: Path, url: str) -> None:
    """用本地稿子整篇覆盖已有的云文档（用户身份）；失败抛 RuntimeError。"""
    cmd = [config.lark_bin(), "docs", "+update", "--as", "user", "--doc", url, "--command", "overwrite",
           "--doc-format", "markdown", "--content", "@" + path.name]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=str(path.parent))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("调用 lark-cli 失败：%s" % e)
    data = outbox._parse_json(p.stdout) or outbox._parse_json(p.stderr) or {}
    if p.returncode != 0 or not data.get("ok"):
        raise RuntimeError("rc=%s %s" % (p.returncode, _error_text(data, p.stderr or p.stdout)))


def _error_text(data: dict, raw: str) -> str:
    """lark-cli 报错里真正的原因。它的 JSON 末尾常跟着一大段 _notice（升级提示），只截尾部会把原因截掉（issue #12）。
    保留 type 字段原样（"type": "network"），发件箱靠它判断是不是网络故障。"""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        keep = {k: err[k] for k in ("type", "subtype", "code", "message", "hint", "retryable") if k in err}
        if keep:
            return json.dumps(keep, ensure_ascii=False)[:500]
    return (raw or "").strip()[:300]


def reason_text(err: str) -> str:
    """给用户看的失败原因：lark-cli 报错 JSON 里的 message，否则原文；不超过 80 字。"""
    m = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', err or "")
    text = m.group(1) if m else (err or "未知原因")
    if "调用 lark-cli 失败" in (err or ""):
        text = "调用 lark-cli 失败（网络或超时）"
    text = " ".join(text.split())
    return text if len(text) <= 80 else text[:80] + "…"


def failures(con, ticket_id: str, workdir: str, artifacts: List[str]) -> List[Tuple[str, str, str]]:
    """这份结果里没能导入的稿子：[(文件名, 本机路径, 原因)]。只认当前这个文件版本的失败记录，导入成功过的不算。
    只读：表还没建（从没导入失败过）时当作没有记录，不在这里建表（库写不进去时也要能发结果）。"""
    out = []
    for p in candidates(con, ticket_id, workdir, artifacts):
        try:
            st, url = _cached(con, p)
            row = None if url else con.execute(
                "SELECT reason FROM doc_failures WHERE path=? AND mtime=? AND size=?",
                (str(p), st.st_mtime, st.st_size)).fetchone()
        except OSError:
            continue
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise
        if row:
            out.append((p.name, str(p), row["reason"]))
    return out


def _cached(con, p: Path):
    st = p.stat()
    row = con.execute("SELECT url FROM published WHERE path=? AND mtime=? AND size=?",
                      (str(p), st.st_mtime, st.st_size)).fetchone()
    return st, (row["url"] if row else None)


def has_pending(con, ticket_id: str, workdir: str, artifacts: List[str]) -> bool:
    """还有没导入过的稿子吗（发件箱据此把这一行交给媒体通道，导入不在守护进程主循环里做）。"""
    for p in candidates(con, ticket_id, workdir, artifacts):
        try:
            if _cached(con, p)[1] is None:
                return True
        except OSError:
            continue
        except sqlite3.OperationalError:
            return True  # 缓存表还没建或读不了：交给媒体通道去导入，库的错误在那里按锁等待/故障分类
    return False


def doc_links(con, ticket_id: str, workdir: str, artifacts: List[str],
              allow_import: bool = True) -> List[Tuple[str, str]]:
    """返回 [(文件名, 云文档链接)]。单个文件导入失败只记告警，不影响其余文件和结果送达。
    allow_import=False 时只返回缓存里已有的链接，不调 lark-cli。"""
    con.executescript(SCHEMA)
    links: List[Tuple[str, str]] = []
    for p in candidates(con, ticket_id, workdir, artifacts):
        st, url = _cached(con, p)
        if url:
            links.append((p.name, url))
            continue
        if not allow_import:
            continue
        prev = con.execute("SELECT url FROM doc_targets WHERE ticket_id=? AND path=?",
                           (ticket_id, str(p))).fetchone()
        url = None
        if prev and p.suffix.lower() in OVERWRITABLE:
            try:
                _overwrite(p, prev["url"])
                url = prev["url"]
            except RuntimeError as e:
                outbox.alert("%s 稿子覆盖原云文档失败，改为另导入一份（%s）：%s" % (ticket_id, p.name, e))
        if url is None:
            name = "%s %s" % (ticket_id, p.stem)
            if prev:
                name += "（%s）" % time.strftime("%m-%d %H:%M")
            try:
                url = _import(p, name)
            except RuntimeError as e:
                outbox.alert("%s 稿子导入云文档失败（%s）：%s" % (ticket_id, p.name, e))
                con.execute("INSERT OR REPLACE INTO doc_failures VALUES(?,?,?,?,?)",
                            (str(p), st.st_mtime, st.st_size, reason_text(str(e)), db.now()))
                continue
            con.execute("INSERT OR REPLACE INTO doc_targets VALUES(?,?,?,?)", (ticket_id, str(p), url, db.now()))
        con.execute("INSERT OR REPLACE INTO published VALUES(?,?,?,?,?)",
                    (str(p), st.st_mtime, st.st_size, url, db.now()))
        links.append((p.name, url))
    return links

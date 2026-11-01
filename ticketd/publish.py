"""把结果里的稿子（本地文档类产物）导入成飞书云文档，结果卡片上给一键打开的按钮。

- 只在发结果卡片时做（outbox 发送前），导入失败不影响结果送达，只是没有按钮。
- 只发布工单目录和授权目录里的文件；ticketd 的内部文件不发布。
- 用用户身份导入，文档落在用户自己的云空间，打开不用再申请权限。
- 同一文件（路径 + 修改时间 + 大小）只导入一次，重试和追问轮次复用同一个链接。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Tuple

from . import config, db, outbox

DOC_TYPES = {".md": "docx", ".markdown": "docx", ".txt": "docx", ".docx": "docx",
             ".xlsx": "sheet", ".csv": "sheet", ".pptx": "slides"}
INTERNAL = {".reports", ".stream.jsonl", "过程.log", ".stderr.log"}
MAX_DOCS = 3
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS published(
  path TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL, url TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY(path, mtime, size)
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
        raise RuntimeError("rc=%s %s" % (p.returncode, (p.stderr or p.stdout)[-300:]))
    return url


def doc_links(con, ticket_id: str, workdir: str, artifacts: List[str]) -> List[Tuple[str, str]]:
    """返回 [(文件名, 云文档链接)]。单个文件导入失败只记告警，不影响其余文件和结果送达。"""
    con.executescript(SCHEMA)
    links: List[Tuple[str, str]] = []
    for p in candidates(con, ticket_id, workdir, artifacts):
        st = p.stat()
        row = con.execute("SELECT url FROM published WHERE path=? AND mtime=? AND size=?",
                          (str(p), st.st_mtime, st.st_size)).fetchone()
        if row:
            links.append((p.name, row["url"]))
            continue
        try:
            url = _import(p, "%s %s" % (ticket_id, p.stem))
        except RuntimeError as e:
            outbox.alert("%s 稿子导入云文档失败（%s）：%s" % (ticket_id, p.name, e))
            continue
        con.execute("INSERT OR REPLACE INTO published VALUES(?,?,?,?,?)",
                    (str(p), st.st_mtime, st.st_size, url, db.now()))
        links.append((p.name, url))
    return links

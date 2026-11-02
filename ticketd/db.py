from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets(
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  origin_message_id TEXT NOT NULL,
  request_text TEXT NOT NULL,
  router_note TEXT,
  ack_text TEXT NOT NULL,
  workdir TEXT NOT NULL,
  session_id TEXT NOT NULL,
  session_started INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  pid INTEGER,
  started_at REAL,
  retries INTEGER NOT NULL DEFAULT 0,
  retry_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants(
  ticket_id TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(ticket_id, path)
);
CREATE TABLE IF NOT EXISTS bindings(message_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inputs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  source_message_id TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
  artifacts_json TEXT NOT NULL DEFAULT '[]', ask TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL, reply_to TEXT NOT NULL, text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0, last_error TEXT, sent_message_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS locks(path TEXT PRIMARY KEY, ticket_id TEXT NOT NULL);
"""

# 上线后加的列：老库用 ALTER TABLE 补上（CLI 和守护进程都会跑，重复添加的报错忽略）
MIGRATIONS = [
    ("tickets", "card_wanted", "INTEGER NOT NULL DEFAULT 0"),   # 1 = 这张单要状态卡（上线前的老单为 0）
    ("tickets", "card_message_id", "TEXT"),
    ("tickets", "card_key", "TEXT"),                              # 上次刷新时的里程碑签名
    ("tickets", "card_at", "REAL NOT NULL DEFAULT 0"),            # 上次成功发出/刷新的时间
    ("tickets", "card_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "card_retry_at", "REAL NOT NULL DEFAULT 0"),
    ("tickets", "last_action_at", "REAL"),                        # 最后一次工具调用/结果（过程日志观测）
    ("tickets", "last_step", "TEXT"),
    ("tickets", "stall_alerted", "INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "card_closed", "INTEGER NOT NULL DEFAULT 0"),    # 1 = 当前状态卡已收起成终态，不再改动
    ("tickets", "card_round", "REAL"),                            # 当前状态卡显示的是哪一轮（该轮的 started_at）
    ("tickets", "card_sent_at", "REAL"),                          # 当前状态卡的发出时间（飞书只能编辑 14 天内的消息）
    ("tickets", "executor", "TEXT NOT NULL DEFAULT 'claude'"),   # 建单时定下，之后不变（会话不能跨执行器迁移）
    ("outbox", "card_json", "TEXT"),                             # 非空 = 发卡片（text 是卡片发不出去时的兜底）
    ("outbox", "report_id", "INTEGER"),                           # 这条消息对应的汇报
    ("outbox", "media_json", "TEXT"),                             # 非空 = 发视频消息（结果卡里放不进的视频；text 是发不出去时的兜底）
]


def _migrate(con: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        have = {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if col in have:
            continue
        try:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def now() -> float:
    return time.time()

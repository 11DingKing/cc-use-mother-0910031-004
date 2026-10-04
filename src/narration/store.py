"""SQLite 持久化层：单连接 + 可重入锁，写事务串行化，保证并发签署/发布结果确定。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audience_levels (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  min_age INTEGER,
  max_age INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  publisher TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  accessed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fragments (
  id TEXT PRIMARY KEY,
  lineage_id TEXT NOT NULL,
  theme_id TEXT NOT NULL REFERENCES themes(id),
  audience_code TEXT NOT NULL REFERENCES audience_levels(code),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  change_kind TEXT NOT NULL,            -- original | correction | replacement
  correction_note TEXT,
  supersedes_id TEXT REFERENCES fragments(id),
  revision INTEGER NOT NULL,
  status TEXT NOT NULL,                 -- active | superseded | withdrawn
  withdrawn_reason TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fragments_lineage ON fragments(lineage_id);
CREATE INDEX IF NOT EXISTS idx_fragments_outline ON fragments(theme_id, audience_code);

CREATE TABLE IF NOT EXISTS fragment_sources (
  fragment_id TEXT NOT NULL REFERENCES fragments(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  PRIMARY KEY (fragment_id, source_id)
);

CREATE TABLE IF NOT EXISTS release_drafts (
  id TEXT PRIMARY KEY,
  theme_id TEXT NOT NULL REFERENCES themes(id),
  audience_code TEXT NOT NULL REFERENCES audience_levels(code),
  status TEXT NOT NULL,                 -- open | published | abandoned
  content_seq INTEGER NOT NULL DEFAULT 1,
  published_version_id TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_items (
  draft_id TEXT NOT NULL REFERENCES release_drafts(id),
  fragment_id TEXT NOT NULL REFERENCES fragments(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (draft_id, fragment_id)
);

CREATE TABLE IF NOT EXISTS signoffs (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES release_drafts(id),
  role TEXT NOT NULL,
  signer TEXT NOT NULL,
  content_seq INTEGER NOT NULL,
  signed_at TEXT NOT NULL,
  UNIQUE (draft_id, role)
);

CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY,
  theme_id TEXT NOT NULL,
  audience_code TEXT NOT NULL,
  seq INTEGER NOT NULL,
  label TEXT NOT NULL,
  parent_id TEXT,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,                 -- published | withdrawn
  withdrawn_reason TEXT,
  withdrawn_at TEXT,
  published_by TEXT NOT NULL,
  published_at TEXT NOT NULL,
  UNIQUE (theme_id, audience_code, seq)
);

CREATE TABLE IF NOT EXISTS version_items (
  version_id TEXT NOT NULL REFERENCES versions(id),
  fragment_id TEXT NOT NULL,
  lineage_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  change_kind TEXT NOT NULL,
  correction_note TEXT,
  position INTEGER NOT NULL,
  PRIMARY KEY (version_id, fragment_id)
);

CREATE TABLE IF NOT EXISTS version_sources (
  version_id TEXT NOT NULL REFERENCES versions(id),
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  publisher TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY (version_id, source_id)
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  theme_id TEXT NOT NULL REFERENCES themes(id),
  audience_code TEXT NOT NULL REFERENCES audience_levels(code),
  title TEXT NOT NULL,
  scheduled_start TEXT NOT NULL,
  status TEXT NOT NULL,                 -- scheduled | completed | cancelled
  pin_version_id TEXT,
  completed_version_id TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_outline ON sessions(theme_id, audience_code, status);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    """SQLite 存储。单连接配合可重入锁，跨线程安全；写操作必须在 transaction 内。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 写事务：异常回滚，正常提交。可嵌套调用方须自行保证不嵌套。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            try:
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

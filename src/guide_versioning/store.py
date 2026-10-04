"""SQLite 持久化：主题、受众、片段修订、来源、发布与签署、场次。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS themes (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audience_levels (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    min_age INTEGER,
    max_age INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    publisher TEXT,
    uri TEXT,
    locator TEXT,
    year INTEGER
);

CREATE TABLE IF NOT EXISTS fragments (
    code TEXT PRIMARY KEY,
    theme_code TEXT NOT NULL REFERENCES themes(code),
    audience_code TEXT NOT NULL REFERENCES audience_levels(code),
    kind TEXT NOT NULL,
    replaces_code TEXT REFERENCES fragments(code),
    status TEXT NOT NULL DEFAULT 'active',
    current_rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fragment_revisions (
    id INTEGER PRIMARY KEY,
    fragment_code TEXT NOT NULL REFERENCES fragments(code),
    rev INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(fragment_code, rev)
);

CREATE TABLE IF NOT EXISTS fragment_sources (
    fragment_code TEXT NOT NULL REFERENCES fragments(code),
    rev INTEGER NOT NULL,
    source_code TEXT NOT NULL REFERENCES sources(code),
    note TEXT,
    PRIMARY KEY (fragment_code, rev, source_code)
);

CREATE TABLE IF NOT EXISTS outlines (
    id INTEGER PRIMARY KEY,
    theme_code TEXT NOT NULL REFERENCES themes(code),
    audience_code TEXT NOT NULL REFERENCES audience_levels(code),
    UNIQUE(theme_code, audience_code)
);

CREATE TABLE IF NOT EXISTS outline_entries (
    outline_id INTEGER NOT NULL REFERENCES outlines(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    fragment_code TEXT NOT NULL REFERENCES fragments(code),
    PRIMARY KEY (outline_id, position),
    UNIQUE (outline_id, fragment_code)
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    theme_code TEXT NOT NULL,
    audience_code TEXT NOT NULL,
    version_major INTEGER NOT NULL,
    version_minor INTEGER NOT NULL,
    version_patch INTEGER NOT NULL,
    change_kind TEXT NOT NULL,
    reason TEXT,
    content_sha TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_id INTEGER REFERENCES releases(id),
    superseded_by_id INTEGER REFERENCES releases(id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    withdrawn_at TEXT,
    withdraw_note TEXT,
    archived_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(theme_code, audience_code, version_major, version_minor, version_patch)
);

CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY,
    release_id INTEGER NOT NULL REFERENCES releases(id),
    role TEXT NOT NULL,
    signer TEXT NOT NULL,
    comment TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(release_id, role)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    theme_code TEXT NOT NULL,
    audience_code TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    venue TEXT,
    status TEXT NOT NULL,
    frozen_release_id INTEGER REFERENCES releases(id),
    frozen_at TEXT,
    created_at TEXT NOT NULL
);
"""


class Store:
    """单连接仓储。所有调用方通过 :attr:`lock` 串行化写事务。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(releases)")}
        if "withdraw_note" not in cols:
            self._conn.execute("ALTER TABLE releases ADD COLUMN withdraw_note TEXT")

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    # ---- 基础工具 -----------------------------------------------------

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def canonical(obj: object) -> bytes:
        """确定性 JSON 序列化，用于快照哈希。"""
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def commit(self) -> None:
        self._conn.commit()

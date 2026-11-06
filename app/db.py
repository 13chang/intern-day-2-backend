"""SQLite 数据访问层。

练习项目只用单文件 SQLite，不引入独立数据库服务。
数据库文件默认位于 <仓库根>/data/app.db，可通过 APP_DB_PATH 覆盖。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);

CREATE TABLE IF NOT EXISTS activity_settings (
    user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    work_start        TEXT    NOT NULL DEFAULT '09:00',
    work_end          TEXT    NOT NULL DEFAULT '18:00',
    windows_json      TEXT    NOT NULL DEFAULT '[]',
    quiet_start       TEXT,
    quiet_end         TEXT,
    reminders_enabled INTEGER NOT NULL DEFAULT 1,
    allow_during_work INTEGER NOT NULL DEFAULT 1,
    daily_target      INTEGER NOT NULL DEFAULT 3,
    postpone_policy   TEXT    NOT NULL DEFAULT 'next_slot',
    updated_at        INTEGER NOT NULL
);

-- scheduled_at 用带偏移的 ISO 串（2026-11-06T10:30:00+08:00）：
-- 要按天查（substr(...,1,10)）、要精确到分钟、还要人能直接读。
-- created_at / updated_at 用秒级整数，和 items 表保持一致。
CREATE TABLE IF NOT EXISTS activities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_type  TEXT    NOT NULL,
    title          TEXT    NOT NULL,
    description    TEXT    NOT NULL,
    scheduled_at   TEXT    NOT NULL,
    duration_min   INTEGER NOT NULL DEFAULT 5,
    status         TEXT    NOT NULL DEFAULT 'planned',
    source         TEXT    NOT NULL DEFAULT 'rule',
    postponed_from INTEGER,
    postpone_count INTEGER NOT NULL DEFAULT 0,
    skipped_reason TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_user_day ON activities(user_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_activity_user_status ON activities(user_id, status);

-- 操作流水：状态变化与统计口径的原始凭据，也方便答辩时解释"为什么这条被跳过"。
CREATE TABLE IF NOT EXISTS activity_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_id INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    note        TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_event_user ON activity_events(user_id, created_at);
"""


def db_path() -> Path:
    """返回当前使用的数据库文件路径。"""
    return Path(os.environ.get("APP_DB_PATH") or DEFAULT_DB_PATH)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """打开连接，正常退出时提交，异常时回滚。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表，可重复执行。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)

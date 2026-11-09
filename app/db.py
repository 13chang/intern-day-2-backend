"""SQLite 数据访问层。

练习项目只用单文件 SQLite，不引入独立数据库服务。
数据库文件默认位于 <仓库根>/data/app.db，可通过 APP_DB_PATH 覆盖。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

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

-- 每日总结：一条 = 一个用户的一天。
-- UNIQUE(user_id, report_date) 是幂等的根 —— 不是"先查再插"能替代的：
-- 两个 Worker 同时处理同一个用户时，只有约束能挡住第二条。
-- 统计数字由后端算好存下来（不存原始明细），这样日报内容不会随活动记录后续
-- 变化而变化，事后回看时看到的就是当天生成时的事实。
CREATE TABLE IF NOT EXISTS daily_summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_date   TEXT    NOT NULL,                    -- YYYY-MM-DD，记录所属自然日
    status        TEXT    NOT NULL DEFAULT 'pending',  -- pending|generating|ready|failed
    planned_total INTEGER NOT NULL DEFAULT 0,
    completed     INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    unfinished    INTEGER NOT NULL DEFAULT 0,
    postpone_ops  INTEGER NOT NULL DEFAULT 0,
    sedentary_minutes INTEGER,                         -- 恒为 NULL：没有自报入口
    review        TEXT,                                -- Agent 写的简短回顾
    suggestion    TEXT,                                -- 一条可执行建议
    source        TEXT NOT NULL DEFAULT 'rule',        -- rule=本地模板 / llm=真实模型
    model         TEXT,                                -- 实际使用的模型名，便于排查
    error_code    TEXT,
    error_message TEXT,                                -- 只放可读的一句话，不放堆栈
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    UNIQUE (user_id, report_date)
);
"""

# 同一用户同一时刻只能有一条记录。不放进 SCHEMA 是因为老库里可能已经有历史重复数据，
# 那样 CREATE UNIQUE INDEX 会直接抛错、应用起不来。见 init_db。
UNIQUE_SLOT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_activity_user_slot "
    "ON activities(user_id, scheduled_at)"
)


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
    # 第六天起 api 与 worker 是两个进程，共享同一个 app.db：
    # - WAL 让读不阻塞写、写不阻塞读，日报生成期间页面仍能正常查询；
    # - busy_timeout 让偶发的写冲突等一会儿再报错，而不是立刻抛
    #   "database is locked"（两个进程同时写时必然会出现几毫秒的重叠）。
    # journal_mode 是持久化到数据库文件的属性，重复设置是幂等的。
    conn.execute("PRAGMA journal_mode = WAL").fetchone()
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表，可重复执行。

    唯一索引单独建、单独容错：老库里如果留着同一时刻的重复记录（历史 bug 的产物），
    ``CREATE UNIQUE INDEX`` 会失败。这里只记一条警告，不让应用因此起不来；
    清理掉重复行之后重启，索引就会补上。
    """
    with get_conn() as conn:
        conn.executescript(SCHEMA)

    with get_conn() as conn:
        try:
            conn.execute(UNIQUE_SLOT_INDEX)
        except sqlite3.IntegrityError:
            logger.warning(
                "activities 表里还有同一时刻的重复记录，唯一索引未建立；"
                "清理后重启即可生效。应用层仍然会拦住新的重复（busy_of_day）。"
            )

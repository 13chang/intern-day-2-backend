"""日报的读写：调度要的用户列表、查询、状态标记、幂等写入。

这一层所有函数都用 ``get_conn()`` 的短事务：进来开连接，出去就提交并关闭。
**不在这里调用模型** —— 模型可能跑几十秒，握着数据库连接等它，会把 api 的
查询全部拖住（SQLite 同一时刻只允许一个写事务）。

幂等的做法只有一条：``UNIQUE(user_id, report_date)`` + ``ON CONFLICT``。
不写"先 SELECT 再 INSERT"，那种写法在两个 Worker 同时跑时会插出两条。
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from app.activity import rules
from app.db import get_conn

# 只有这几种状态。failed 允许被后续重试覆盖，ready 不允许。
STATUS_PENDING = "pending"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


def list_users_with_records(report_date: date) -> list[int]:
    """这一天产生过活动记录的用户。

    不查 users 全表：给一个当天没有任何记录的用户也生成日报，
    既浪费一次模型调用，又会在页面上堆出一串"这一天没有活动记录"的空日报。
    """
    start = rules.at_time(report_date, "00:00")
    end = start + timedelta(days=1)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT user_id FROM activities
                WHERE scheduled_at >= ? AND scheduled_at < ?""",
            (rules.iso(start), rules.iso(end)),
        ).fetchall()
    return [int(row[0]) for row in rows]


def get_summary(user_id: int, report_date: str) -> dict | None:
    """取一份日报。没有就返回 None —— 调用方据此区分"还没生成"。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM daily_summaries WHERE user_id = ? AND report_date = ?""",
            (user_id, report_date),
        ).fetchone()
    return dict(row) if row else None


def mark_status(
    user_id: int,
    report_date: str,
    status: str,
    *,
    code: str = "",
    message: str = "",
) -> None:
    """只更新状态，不碰统计与正文。

    单独一个函数，是因为 Worker 要在**调模型之前**把状态标成 ``generating`` ——
    页面要能显示"正在生成"。如果和最后的写入合成一个事务，中途查库看到的
    还是旧状态，轮询就永远等不到"生成中"这一步。

    ``WHERE status <> 'ready'`` 保证一份已经成功的日报不会被后来的失败重试改坏。
    """
    stamp = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_summaries
                (user_id, report_date, status, error_code, error_message,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, report_date) DO UPDATE SET
                status = excluded.status,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                updated_at = excluded.updated_at
            WHERE daily_summaries.status <> 'ready'
            """,
            (user_id, report_date, status, code or None, message or None, stamp, stamp),
        )


def save_ready(user_id: int, report_date: str, payload: dict) -> bool:
    """写入一份成功日报。

    返回 True 表示这次真的写进去了；False 表示库里已经有一份 ready，
    这次执行被当成重复执行忽略掉了 —— 这是"执行了几次"和"有几份日报"
    脱钩的关键：ARQ 可能因为进程中断把同一个任务跑第二遍，第二次到这里是 no-op。
    """
    stamp = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO daily_summaries
                (user_id, report_date, status, planned_total, completed, skipped,
                 unfinished, postpone_ops, sedentary_minutes, review, suggestion,
                 source, model, created_at, updated_at)
            VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, report_date) DO UPDATE SET
                status = 'ready',
                planned_total = excluded.planned_total,
                completed = excluded.completed,
                skipped = excluded.skipped,
                unfinished = excluded.unfinished,
                postpone_ops = excluded.postpone_ops,
                sedentary_minutes = excluded.sedentary_minutes,
                review = excluded.review,
                suggestion = excluded.suggestion,
                source = excluded.source,
                model = excluded.model,
                error_code = NULL,
                error_message = NULL,
                updated_at = excluded.updated_at
            WHERE daily_summaries.status <> 'ready'
            """,
            (
                user_id,
                report_date,
                payload.get("planned_total", 0),
                payload.get("completed", 0),
                payload.get("skipped", 0),
                payload.get("unfinished", 0),
                payload.get("postpone_ops", 0),
                payload.get("sedentary_minutes"),
                payload.get("review", ""),
                payload.get("suggestion", ""),
                payload.get("source", "rule"),
                payload.get("model"),
                stamp,
                stamp,
            ),
        )
        return cur.rowcount > 0

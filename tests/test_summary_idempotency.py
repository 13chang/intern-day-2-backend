"""日报幂等的测试。

任务书要求"同一用户、同一日期只保留一份有效日报"，并且明确说
"不能只先查询再插入"。这里验证的是数据库约束确实生效，而不是代码自觉。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from app.activity import rules
from app.db import get_conn
from app.summary import store

DAY = date(2026, 11, 6)
DAY_STR = DAY.isoformat()


def _payload(**overrides) -> dict:
    base = {
        "planned_total": 3,
        "completed": 2,
        "skipped": 1,
        "unfinished": 0,
        "postpone_ops": 1,
        "sedentary_minutes": None,
        "review": "计划 3 次，完成 2 次。",
        "suggestion": "把下午那次提前到午休后。",
        "source": "rule",
        "model": "template",
    }
    base.update(overrides)
    return base


def _insert_activity(user_id: int, day: date, hhmm: str) -> None:
    moment = rules.iso(rules.at_time(day, hhmm))
    stamp = int(rules.at_time(day, hhmm).timestamp())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO activities
                   (user_id, activity_type, title, description, scheduled_at,
                    duration_min, status, source, postpone_count, created_at, updated_at)
               VALUES (?, 'walk', '起身走动', '走一小段。', ?, 5, 'planned', 'rule', 0, ?, ?)""",
            (user_id, moment, stamp, stamp),
        )


def test_same_user_same_day_is_one_row(client, demo) -> None:
    """连写两次，表里仍然只有一行；第二次返回 False。"""
    assert store.save_ready(demo["id"], DAY_STR, _payload()) is True
    assert store.save_ready(demo["id"], DAY_STR, _payload()) is False

    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries WHERE user_id = ? AND report_date = ?",
            (demo["id"], DAY_STR),
        ).fetchone()[0]
    assert count == 1


def test_ready_row_is_never_overwritten(client, demo) -> None:
    """已成功的那份不会被第二次执行改掉 —— 这是 WHERE status <> 'ready' 的作用。"""
    store.save_ready(demo["id"], DAY_STR, _payload(review="第一版"))
    store.save_ready(demo["id"], DAY_STR, _payload(review="第二版", completed=99))

    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["review"] == "第一版"
    assert row["completed"] == 2


def test_failed_can_be_retried_into_ready(client, demo) -> None:
    """失败任务允许重试成功 —— 与"成功不覆盖"是两条不同的规则。"""
    store.mark_status(demo["id"], DAY_STR, store.STATUS_FAILED,
                      code="TRANSIENT", message="生成失败，稍后自动重试")
    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["status"] == "failed"

    assert store.save_ready(demo["id"], DAY_STR, _payload()) is True

    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["status"] == "ready"
    # 成功时要把上一次的失败信息清掉，否则页面上会挂着一条过期错误
    assert row["error_code"] is None
    assert row["error_message"] is None


def test_mark_status_does_not_downgrade_ready(client, demo) -> None:
    """已经成功的日报，不该因为后面某次重试失败被改回 failed。"""
    store.save_ready(demo["id"], DAY_STR, _payload())
    store.mark_status(demo["id"], DAY_STR, store.STATUS_FAILED, code="X", message="不该写入")

    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["status"] == "ready"
    assert row["error_code"] is None


def test_unique_constraint_exists_at_database_level(client, demo) -> None:
    """绕过应用层直接插第二条，必须被数据库挡住。

    这条用例的价值在于：如果哪天有人把 UNIQUE 从建表语句里删了，
    前三条用例仍然会通过（因为代码里没先查），只有这一条会红。
    """
    store.save_ready(demo["id"], DAY_STR, _payload())

    with pytest.raises(sqlite3.IntegrityError):
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO daily_summaries
                       (user_id, report_date, status, created_at, updated_at)
                   VALUES (?, ?, 'ready', 0, 0)""",
                (demo["id"], DAY_STR),
            )


def test_different_users_same_day_are_independent(client, demo, other) -> None:
    store.save_ready(demo["id"], DAY_STR, _payload(completed=1))
    store.save_ready(other["id"], DAY_STR, _payload(completed=2))

    assert store.get_summary(demo["id"], DAY_STR)["completed"] == 1
    assert store.get_summary(other["id"], DAY_STR)["completed"] == 2


def test_get_summary_returns_none_when_missing(client, demo) -> None:
    assert store.get_summary(demo["id"], DAY_STR) is None


def test_list_users_with_records(client, demo, other) -> None:
    """只有当天有活动记录的用户才进入调度列表。"""
    _insert_activity(demo["id"], DAY, "10:00")
    # other 的记录在前一天，不该出现在当天的列表里
    _insert_activity(other["id"], DAY - timedelta(days=1), "10:00")

    assert store.list_users_with_records(DAY) == [demo["id"]]
    assert store.list_users_with_records(DAY - timedelta(days=1)) == [other["id"]]


def test_list_users_with_records_deduplicates(client, demo) -> None:
    """一个用户一天有三条活动，也只返回一次。"""
    _insert_activity(demo["id"], DAY, "10:00")
    _insert_activity(demo["id"], DAY, "14:00")
    _insert_activity(demo["id"], DAY, "16:00")

    assert store.list_users_with_records(DAY) == [demo["id"]]

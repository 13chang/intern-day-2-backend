"""每日统计的单元测试。

重点覆盖三个容易算错的地方：推迟产生的替代条目、跨日推迟、日期边界。
这些用例不碰 Redis、不碰模型，日期全部作为参数传入，所以任何时间跑都稳定。
"""

from __future__ import annotations

from datetime import date, timedelta

from app.activity import rules
from app.db import get_conn
from app.summary.stats import daily_stats, day_bounds

DAY = date(2026, 11, 6)  # 周五


def _insert_activity(
    user_id: int,
    day: date,
    hhmm: str,
    *,
    status: str = "planned",
    postponed_from: int | None = None,
) -> int:
    """插一条活动。时间按传入的时区转换，和真实写入路径一致。"""
    moment = rules.iso(rules.at_time(day, hhmm))
    stamp = int(rules.at_time(day, hhmm).timestamp())
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO activities
                   (user_id, activity_type, title, description, scheduled_at,
                    duration_min, status, source, postponed_from, postpone_count,
                    created_at, updated_at)
               VALUES (?, 'walk', '起身走动', '离开座位走一小段。', ?,
                       5, ?, 'rule', ?, 0, ?, ?)""",
            (user_id, moment, status, postponed_from, stamp, stamp),
        )
        return int(cur.lastrowid)


def _insert_event(user_id: int, activity_id: int, action: str, day: date, hhmm: str) -> None:
    stamp = int(rules.at_time(day, hhmm).timestamp())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO activity_events
                   (user_id, activity_id, action, from_status, to_status, note, created_at)
               VALUES (?, ?, ?, 'planned', 'postponed', NULL, ?)""",
            (user_id, activity_id, action, stamp),
        )


def test_bounds_are_left_closed_right_open() -> None:
    start, end = day_bounds(DAY)
    assert start == "2026-11-06T00:00:00+08:00"
    assert end == "2026-11-07T00:00:00+08:00"


def test_counts_by_status(client, demo) -> None:
    """一天里四种状态各一条，五个数字逐个对得上。"""
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    _insert_activity(demo["id"], DAY, "14:00", status="skipped")
    _insert_activity(demo["id"], DAY, "16:00", status="planned")

    stats = daily_stats(demo["id"], DAY)

    assert stats["planned_total"] == 3
    assert stats["completed"] == 1
    assert stats["skipped"] == 1
    assert stats["unfinished"] == 1
    assert stats["postpone_ops"] == 0
    assert stats["has_records"] is True


def test_postponed_original_counts_as_unfinished(client, demo) -> None:
    """推迟后原条的状态是 postponed —— 它属于"没完成"，不是"完成"。"""
    original = _insert_activity(demo["id"], DAY, "10:00", status="postponed")
    _insert_activity(demo["id"], DAY, "15:00", status="planned", postponed_from=original)

    stats = daily_stats(demo["id"], DAY)

    # 替代条目有 postponed_from，不算进计划数；原条算计划 1
    assert stats["planned_total"] == 1
    assert stats["unfinished"] == 2  # 原条 postponed + 替代条目 planned
    assert stats["completed"] == 0


def test_push_entry_does_not_inflate_planned_total(client, demo) -> None:
    """推迟三次会产生三条替代记录，但计划数仍应是 1。"""
    first = _insert_activity(demo["id"], DAY, "10:00", status="postponed")
    second = _insert_activity(demo["id"], DAY, "11:00", status="postponed", postponed_from=first)
    _insert_activity(demo["id"], DAY, "13:00", status="completed", postponed_from=second)

    stats = daily_stats(demo["id"], DAY)

    assert stats["planned_total"] == 1
    # 完成数可以大于计划数：这条替代条目完成了，但它不计入计划
    assert stats["completed"] == 1
    assert stats["unfinished"] == 2


def test_cross_day_postpone(client, demo) -> None:
    """11-06 的活动被推到 11-07。

    11-06：计划 1、未完成 1（原条停在那天）
    11-07：计划 0（替代条目有 postponed_from），但如果它完成了，完成数是 1
    """
    original = _insert_activity(demo["id"], DAY, "16:00", status="postponed")
    next_day = DAY + timedelta(days=1)
    _insert_activity(demo["id"], next_day, "10:00", status="completed", postponed_from=original)

    today = daily_stats(demo["id"], DAY)
    tomorrow = daily_stats(demo["id"], next_day)

    assert today["planned_total"] == 1
    assert today["unfinished"] == 1
    assert today["completed"] == 0

    assert tomorrow["planned_total"] == 0
    assert tomorrow["completed"] == 1
    assert tomorrow["unfinished"] == 0


def test_day_boundaries(client, demo) -> None:
    """左闭右开：00:00:00 算当天，23:59:59 算当天，前一天 23:59:59 不算。"""
    prev_day = DAY - timedelta(days=1)
    _insert_activity(demo["id"], prev_day, "23:59", status="completed")
    _insert_activity(demo["id"], DAY, "00:00", status="completed")
    _insert_activity(demo["id"], DAY, "23:59", status="completed")

    stats = daily_stats(demo["id"], DAY)

    assert stats["planned_total"] == 2
    assert stats["completed"] == 2


def test_empty_day(client, demo) -> None:
    """没记录时：计数全 0，但久坐必须是 None 而不是 0。"""
    stats = daily_stats(demo["id"], DAY)

    assert stats["has_records"] is False
    assert stats["planned_total"] == 0
    assert stats["completed"] == 0
    assert stats["skipped"] == 0
    assert stats["unfinished"] == 0
    assert stats["postpone_ops"] == 0
    # "未记录"和"0 分钟"是两件事，不能混
    assert stats["sedentary_minutes"] is None


def test_other_user_is_isolated(client, demo, other) -> None:
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    _insert_activity(other["id"], DAY, "11:00", status="completed")
    _insert_activity(other["id"], DAY, "15:00", status="completed")

    assert daily_stats(demo["id"], DAY)["completed"] == 1
    assert daily_stats(other["id"], DAY)["completed"] == 2


def test_postpone_ops_counted_from_events(client, demo) -> None:
    """推迟次数按流水条数算，和活动当前状态无关。"""
    activity = _insert_activity(demo["id"], DAY, "10:00", status="postponed")
    _insert_event(demo["id"], activity, "postpone", DAY, "10:30")
    _insert_event(demo["id"], activity, "postpone", DAY, "14:20")

    # 前一天的操作不算进当天
    _insert_event(demo["id"], activity, "postpone", DAY - timedelta(days=1), "09:00")

    stats = daily_stats(demo["id"], DAY)

    assert stats["postpone_ops"] == 2


def test_completed_and_postpone_ops_are_not_added(client, demo) -> None:
    """完成数和推迟次数会重叠，任务书要求不要相加算总数。

    这条用例本身不"断言不相加"，而是固定住两个数字各自的含义：
    一条活动完成了、又被推迟过一次，completed=1 且 postpone_ops=1。
    """
    activity = _insert_activity(demo["id"], DAY, "10:00", status="completed")
    _insert_event(demo["id"], activity, "postpone", DAY, "09:40")

    stats = daily_stats(demo["id"], DAY)

    assert stats["completed"] == 1
    assert stats["postpone_ops"] == 1
    assert stats["planned_total"] == 1

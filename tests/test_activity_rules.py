"""活动时间规则的单元测试。

这一层是纯函数，不连数据库、不起服务，所以用例可以写得很细。
重点覆盖任务书里最容易翻车的几条：免打扰、跨天免打扰、窗口裁剪、
可用时间不足时如实少排、推迟时不能和自己冲突。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.activity import rules

TZ8 = timezone(timedelta(hours=8))
DAY = date(2026, 11, 6)  # 周五
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=TZ8)

SETTINGS = {
    "work_start": "09:00",
    "work_end": "18:00",
    "windows": [
        {"label": "morning", "start": "10:00", "end": "12:00"},
        {"label": "afternoon", "start": "14:00", "end": "17:30"},
    ],
    "quiet_start": "15:00",
    "quiet_end": "16:00",
    "daily_target": 3,
}


def test_window_outside_work_hours_is_clipped() -> None:
    settings = {**SETTINGS, "windows": [{"label": "x", "start": "08:00", "end": "20:00"}]}
    ranges = rules.allowed_ranges(settings, DAY)
    assert len(ranges) == 1
    assert ranges[0][0].hour == 9 and ranges[0][1].hour == 18


def test_quiet_hours_split_one_window_into_two() -> None:
    ranges = rules.free_ranges(SETTINGS, DAY, [], NOW)
    assert [(start.strftime("%H:%M"), end.strftime("%H:%M")) for start, end in ranges] == [
        ("10:00", "12:00"),
        ("14:00", "15:00"),
        ("16:00", "17:30"),
    ]


def test_overnight_quiet_hours_split_into_two_ranges() -> None:
    """22:00-07:00 必须拆成两段判，当成一段的话一条都拦不住。"""
    settings = {**SETTINGS, "quiet_start": "22:00", "quiet_end": "07:00"}
    assert len(rules.quiet_ranges(settings, DAY)) == 2
    assert rules.allowed_ranges(settings, DAY)[0][0].hour == 10


def test_plan_avoids_quiet_hours() -> None:
    drafts, feasible = rules.plan_day(SETTINGS, DAY, [], NOW, 3)
    assert feasible == 3
    assert all(not (15 <= draft.at.hour < 16) for draft in drafts)


def test_plan_reduces_count_when_time_is_short() -> None:
    settings = {**SETTINGS, "windows": [{"label": "x", "start": "10:00", "end": "10:20"}]}
    drafts, feasible = rules.plan_day(settings, DAY, [], NOW, 3)
    assert feasible == 1 and len(drafts) == 1


def test_plan_raises_when_no_free_time_left() -> None:
    late_now = datetime(2026, 11, 6, 18, 30, tzinfo=TZ8)
    with pytest.raises(rules.RuleViolation) as exc:
        rules.plan_day(SETTINGS, DAY, [], late_now, 2)
    assert exc.value.code == "NO_SLOT_AVAILABLE"


def test_free_ranges_drops_past_time() -> None:
    now = datetime(2026, 11, 6, 11, 0, tzinfo=TZ8)
    ranges = rules.free_ranges(SETTINGS, DAY, [], now)
    assert ranges[0][0] == now + timedelta(minutes=1)


def test_validate_rejects_quiet_hours() -> None:
    moment = datetime(2026, 11, 6, 15, 30, tzinfo=TZ8)
    with pytest.raises(rules.RuleViolation) as exc:
        rules.validate_slot(SETTINGS, moment, DAY, [], NOW)
    assert exc.value.code == "QUIET_HOURS"


def test_validate_rejects_outside_window() -> None:
    moment = datetime(2026, 11, 6, 12, 30, tzinfo=TZ8)
    with pytest.raises(rules.RuleViolation) as exc:
        rules.validate_slot(SETTINGS, moment, DAY, [], NOW)
    assert exc.value.code == "OUTSIDE_WINDOW"


def test_validate_rejects_past_time() -> None:
    moment = datetime(2026, 11, 6, 8, 30, tzinfo=TZ8)
    with pytest.raises(rules.RuleViolation) as exc:
        rules.validate_slot(SETTINGS, moment, DAY, [], NOW)
    assert exc.value.code == "PAST_TIME"


def test_validate_rejects_conflict_with_existing() -> None:
    busy = [{"id": 1, "scheduled_at": "2026-11-06T10:30:00+08:00", "duration_min": 5}]
    moment = datetime(2026, 11, 6, 10, 35, tzinfo=TZ8)
    with pytest.raises(rules.RuleViolation) as exc:
        rules.validate_slot(SETTINGS, moment, DAY, busy, NOW)
    assert exc.value.code == "CONFLICT"


def test_postpone_ignores_self_conflict() -> None:
    """算"后面还有没有位置"时必须把自己排除，否则永远算不出来。"""
    busy = [{"id": 7, "scheduled_at": "2026-11-06T10:30:00+08:00", "duration_min": 5}]
    moment = datetime(2026, 11, 6, 10, 30, tzinfo=TZ8)
    rules.validate_slot(SETTINGS, moment, DAY, busy, NOW, ignore_id=7)  # 不应抛异常


def test_next_slot_returns_none_after_last_window() -> None:
    after = datetime(2026, 11, 6, 18, 0, tzinfo=TZ8)
    assert rules.next_slot(SETTINGS, DAY, [], after, NOW) is None


def test_iso_roundtrip_keeps_offset() -> None:
    moment = datetime(2026, 11, 6, 10, 30, tzinfo=TZ8)
    assert rules.iso(moment) == "2026-11-06T10:30:00+08:00"
    assert rules.parse_dt(rules.iso(moment)) == moment


def test_parse_dt_fills_missing_timezone() -> None:
    """历史数据或手写数据可能没有偏移，补成本地时区而不是直接报错。"""
    assert rules.parse_dt("2026-11-06T10:30:00").tzinfo is not None


def test_plan_day_output_passes_validate_slot() -> None:
    """排程算出来的落点，必须能被保存时的校验接受。

    回归用例：``free_ranges`` 曾经只在已有活动左侧让开 BUFFER_MIN，
    而 ``validate_slot`` 两边都按"时长+BUFFER_MIN"判，
    于是排程会落在 14:15、校验却认为它离 14:25 太近 —— 表现为
    "建议了两条，确认第一条就 422"。两个函数现在共用 ``gap_needed``。
    """
    settings = {**SETTINGS, "quiet_start": None, "quiet_end": None}
    busy = [{"id": 1, "scheduled_at": "2026-11-06T14:25:00+08:00", "duration_min": 5}]
    drafts, _ = rules.plan_day(settings, DAY, busy, NOW, 3)
    assert drafts, "这个场景本来就该排出东西"
    for draft in drafts:
        rules.validate_slot(settings, draft.at, DAY, busy, NOW)  # 任何一条都不应抛异常


def test_gap_needed_is_shared_by_free_ranges_and_validate() -> None:
    """同一份间隔规则：能算出空位，就一定通得过校验。"""
    busy = [{"id": 2, "scheduled_at": "2026-11-06T10:30:00+08:00", "duration_min": 5}]
    need = rules.gap_needed(busy[0])
    assert need == 5 + rules.BUFFER_MIN
    # 刚好差 need 分钟：校验通过
    rules.validate_slot(
        SETTINGS,
        datetime(2026, 11, 6, 10, 30, tzinfo=TZ8) + timedelta(minutes=need),
        DAY,
        busy,
        NOW,
    )
    # 再靠近一分钟：校验必须拒绝
    with pytest.raises(rules.RuleViolation) as info:
        rules.validate_slot(
            SETTINGS,
            datetime(2026, 11, 6, 10, 30, tzinfo=TZ8) + timedelta(minutes=need - 1),
            DAY,
            busy,
            NOW,
        )
    assert info.value.code == "CONFLICT"


def test_at_offset_never_returns_range_end() -> None:
    """等分偏移正好等于某段长度时，落点不能是这段的右端点。

    右端点是下一段禁区的起点：14:00-14:30 与 15:30-16:00 两段可用、
    偏移 30 分钟，返回 14:30 就成了"排进免打扰"。返回下一段起点才自洽。
    """
    ranges = [
        (rules.at_time(DAY, "14:00"), rules.at_time(DAY, "14:30")),
        (rules.at_time(DAY, "15:30"), rules.at_time(DAY, "16:00")),
    ]
    got = rules._at_offset(ranges, 30)
    assert got is not None
    assert got != rules.at_time(DAY, "14:30")
    assert got == rules.at_time(DAY, "15:30")


def test_plan_day_never_lands_in_quiet_hours_across_counts() -> None:
    """回归：等分中点靠近可用区间末尾时，向上对齐会跳过界落到免打扰里。

    复现场景来自验收脚本：免打扰 14:30-15:30 把窗口切成两段，
    中点正好逼近 14:30，``_align_up`` 一步就跨进了禁区 ——
    生成时看着合理，点"确认保存"却 422。
    """
    settings = {
        **SETTINGS,
        "windows": [{"label": "afternoon", "start": "14:00", "end": "16:00"}],
        "quiet_start": "14:30",
        "quiet_end": "15:30",
    }
    quiet = rules.quiet_ranges(settings, DAY)
    for count in range(1, 5):
        drafts, _ = rules.plan_day(settings, DAY, [], NOW, count)
        for draft in drafts:
            assert not any(start <= draft.at < end for start, end in quiet), (
                f"{count} 条时排进了免打扰：{draft.at:%H:%M}"
            )
            rules.validate_slot(settings, draft.at, DAY, [], NOW)


def test_plan_day_returns_slot_when_two_short_ranges_remain() -> None:
    """两段各 30 分钟的窗口里要 1 条，不该报"时间不足"。"""
    settings = {
        **SETTINGS,
        "windows": [{"label": "afternoon", "start": "14:00", "end": "16:00"}],
        "quiet_start": "14:30",
        "quiet_end": "15:30",
    }
    drafts, feasible = rules.plan_day(settings, DAY, [], NOW, 1)
    assert feasible == 1
    assert 0 < len(drafts) <= 2
    for draft in drafts:
        rules.validate_slot(settings, draft.at, DAY, [], NOW)

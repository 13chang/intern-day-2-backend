"""活动设置、当天安排、状态流转与周统计的持久化层。

分层约定：

- 所有 SQL 与状态判断都在这里，路由只做参数转换；
- 身份一律是 ``user_id``（整数，来自已校验的登录会话），函数签名里没有 username，
  也不接受请求体里的任何用户标识 —— 否则改个 id 就能读别人的数据；
- 业务规则交给 ``app.activity.rules``，本文件只有"什么时候调它"，没有第二份实现。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta

from app.activity import rules
from app.db import get_conn

DEFAULT_WINDOWS = [
    {"label": "morning", "start": "10:00", "end": "12:00"},
    {"label": "afternoon", "start": "14:00", "end": "17:30"},
]


def _default_daily_target() -> int:
    """每天次数默认值，可用环境变量覆盖（用户仍可在页面上改）。"""
    try:
        value = int(str(os.environ.get("APP_ACTIVITY_DAILY_TARGET", "3")).strip())
    except (TypeError, ValueError):
        return 3
    return value if 1 <= value <= 8 else 3


DEFAULT_SETTINGS: dict = {
    "work_start": "09:00",
    "work_end": "18:00",
    "windows": DEFAULT_WINDOWS,
    "quiet_start": None,
    "quiet_end": None,
    "reminders_enabled": True,
    "allow_during_work": True,
    "daily_target": 3,
    "postpone_policy": "next_slot",
}

ACTIVITY_COLUMNS = (
    "id, activity_type, title, description, scheduled_at, duration_min, "
    "status, source, postponed_from, postpone_count, skipped_reason"
)


# ---------------------------------------------------------------- 设置

def _settings_from_row(row) -> dict:
    data = dict(row)
    windows = json.loads(data.pop("windows_json") or "[]")
    data["windows"] = windows or DEFAULT_WINDOWS
    data["reminders_enabled"] = bool(data["reminders_enabled"])
    data["allow_during_work"] = bool(data["allow_during_work"])
    return data


def get_settings(user_id: int) -> dict:
    """没有记录就返回默认值 —— 前端不需要先"创建"才能用。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM activity_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return {
            **DEFAULT_SETTINGS,
            "daily_target": _default_daily_target(),
            "user_id": user_id,
        }
    return _settings_from_row(row)


def validate_settings(payload: dict) -> None:
    """跨字段校验。

    pydantic 只能校验单个字段的格式，表达不了"两个字段之间的关系"（例如结束必须晚于开始），
    所以放在这里。存库（``save_settings``）和"仅本次生成用"的临时条件
    （``resolve_conditions``）共用这一份，避免出现"设置里能存、生成时却说条件不合法"。
    """
    work_start, work_end = payload["work_start"], payload["work_end"]
    if work_start >= work_end:
        raise rules.RuleViolation("INVALID_WORK_HOURS", "工作结束时间必须晚于开始时间。")

    for window in payload.get("windows") or []:
        if window["start"] >= window["end"]:
            raise rules.RuleViolation(
                "INVALID_WINDOW",
                f"时段 {window['start']}-{window['end']} 的开始时间必须早于结束时间。",
            )

    quiet_start, quiet_end = payload.get("quiet_start"), payload.get("quiet_end")
    if bool(quiet_start) != bool(quiet_end):
        raise rules.RuleViolation("INVALID_QUIET", "免打扰的开始和结束时间需要成对填写。")

    target = int(payload.get("daily_target", 3))
    if not 1 <= target <= 8:
        raise rules.RuleViolation("INVALID_DAILY_TARGET", "每天次数需要在 1 到 8 之间。")


def save_settings(user_id: int, payload: dict) -> dict:
    """整份覆盖写入。"""
    validate_settings(payload)

    work_start, work_end = payload["work_start"], payload["work_end"]
    quiet_start, quiet_end = payload.get("quiet_start"), payload.get("quiet_end")
    target = int(payload.get("daily_target", 3))

    stamp = int(time.time())
    windows_json = json.dumps(payload.get("windows") or [], ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO activity_settings
                (user_id, work_start, work_end, windows_json, quiet_start, quiet_end,
                 reminders_enabled, allow_during_work, daily_target, postpone_policy, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                work_start = excluded.work_start,
                work_end = excluded.work_end,
                windows_json = excluded.windows_json,
                quiet_start = excluded.quiet_start,
                quiet_end = excluded.quiet_end,
                reminders_enabled = excluded.reminders_enabled,
                allow_during_work = excluded.allow_during_work,
                daily_target = excluded.daily_target,
                postpone_policy = excluded.postpone_policy,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                work_start,
                work_end,
                windows_json,
                quiet_start,
                quiet_end,
                1 if payload.get("reminders_enabled", True) else 0,
                1 if payload.get("allow_during_work", True) else 0,
                target,
                payload.get("postpone_policy", "next_slot"),
                stamp,
            ),
        )
    return get_settings(user_id)


def resolve_conditions(user_id: int, overrides: dict | None = None) -> dict:
    """已保存的设置 + 本次生成用的临时条件。

    为什么要有这一层：DeepSeek 模式下用户会在建议面板里现场改工作时段、上午/下午窗口、
    免打扰，这些只对"这一次生成"生效，不写回设置（否则改一次条件就悄悄改了长期偏好）。
    合并完仍然走 ``validate_settings``，所以临时条件也不许绕过规则。
    """
    settings = get_settings(user_id)
    if not overrides:
        return settings

    merged = dict(settings)
    for key in ("work_start", "work_end"):
        value = overrides.get(key)
        if value:
            merged[key] = value

    windows = overrides.get("windows")
    if windows:
        # 传了就是整份替换：上午/下午的窗口由前端一起给，不做"部分合并"，
        # 否则删掉一个窗口在前端看起来成功、后端其实还留着原来的。
        merged["windows"] = [dict(item) for item in windows]

    if overrides.get("ignore_quiet"):
        # "暂时关闭提醒" = 这次不考虑免打扰。只是本次计算时不算它，设置里仍然保留。
        merged["quiet_start"] = None
        merged["quiet_end"] = None
    else:
        quiet_start = overrides.get("quiet_start")
        quiet_end = overrides.get("quiet_end")
        if quiet_start and quiet_end:
            merged["quiet_start"] = quiet_start
            merged["quiet_end"] = quiet_end
        elif quiet_start or quiet_end:
            raise rules.RuleViolation("INVALID_QUIET", "免打扰的开始和结束时间需要成对填写。")

    validate_settings(merged)
    return merged


# ---------------------------------------------------------------- 查询

def _load_day(conn, user_id: int, day: date) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT {ACTIVITY_COLUMNS}
        FROM activities
        WHERE user_id = ? AND substr(scheduled_at, 1, 10) = ?
        ORDER BY scheduled_at
        """,
        (user_id, day.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def list_day(user_id: int, day: date) -> list[dict]:
    with get_conn() as conn:
        return _load_day(conn, user_id, day)


def busy_of_day(user_id: int, day: date) -> list[dict]:
    """当天"这个时刻已经有安排"的记录，用于排程与冲突校验。

    口径是**当天所有记录，不分状态**。以前只算 ``planned``，理由是"完成/跳过/推迟的
    不该继续占时间"。但它带来一个直接可见的后果：同一天会排出两条同一时刻的安排 ——
    上午生成 14:50 那条做完之后，14:50 又被算成空闲，建议面板再给一次 14:50，
    确认保存后就变成列表里两条 14:50（一条 rule、一条 agent），用户看到的就是重复。

    为什么不怕"排不进东西"：``free_ranges`` 对每条记录只屏蔽"中心 ± (时长+10 分钟)"，
    一条记录最多挡住半小时，不会把整个下午占死；真把全天都排满了，本来也不该再塞。
    """
    return list_day(user_id, day)


def _owned(conn, user_id: int, activity_id: int) -> dict:
    """取本人的活动；不存在或不属于本人一律 404，不区分"没有"和"不是你的"。"""
    row = conn.execute(
        f"SELECT {ACTIVITY_COLUMNS} FROM activities WHERE id = ? AND user_id = ?",
        (activity_id, user_id),
    ).fetchone()
    if row is None:
        raise rules.RuleViolation("NOT_FOUND", "活动不存在")
    return dict(row)


# ---------------------------------------------------------------- 写入

def _insert_activity(
    conn,
    user_id: int,
    *,
    moment: datetime,
    activity_type: str,
    title: str,
    description: str,
    duration_min: int,
    source: str,
    stamp: int,
    postponed_from: int | None = None,
    postpone_count: int = 0,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO activities
            (user_id, activity_type, title, description, scheduled_at, duration_min,
             status, source, postponed_from, postpone_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            activity_type,
            title,
            description,
            rules.iso(moment),
            duration_min,
            source,
            postponed_from,
            postpone_count,
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid)


def _log_event(
    conn,
    user_id: int,
    activity_id: int,
    action: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    note: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO activity_events
            (user_id, activity_id, action, from_status, to_status, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, activity_id, action, from_status, to_status, note, int(time.time())),
    )


def generate_day(user_id: int, day: date, force: bool = False) -> dict:
    """按当前设置生成当天安排。

    当天已经有任何记录就不再生成，除非 ``force=true``。这样"点两次生成"不会变成六条；
    也不能只看 ``planned``——全部做完之后再点一次生成，旧逻辑会认为"今天还空着"，
    于是在同一批时刻上再排一遍，页面上就成了两行同一时间。
    """
    settings = get_settings(user_id)
    now = rules.now_local()

    with get_conn() as conn:
        existing = _load_day(conn, user_id, day)
        if existing and not force:
            return {
                "created": 0,
                "activities": existing,
                "notice": "今天已经有安排了，未重复生成。需要重排可以重建。",
            }

        if force:
            # 只清待执行的：已完成/已推迟/已跳过是历史记录，删掉会让周统计对不上。
            # 清完之后重排仍要避开这些历史时刻，否则又会排到同一时间。
            conn.execute(
                """
                DELETE FROM activities
                WHERE user_id = ? AND substr(scheduled_at, 1, 10) = ? AND status = 'planned'
                """,
                (user_id, day.isoformat()),
            )
            existing = [row for row in existing if row["status"] != "planned"]

        drafts, feasible = rules.plan_day(settings, day, existing, now, settings["daily_target"])

        stamp = int(time.time())
        for draft in drafts:
            activity_id = _insert_activity(
                conn,
                user_id,
                moment=draft.at,
                activity_type=draft.activity_type,
                title=draft.title,
                description=draft.description,
                duration_min=draft.duration_min,
                source="rule",
                stamp=stamp,
            )
            _log_event(conn, user_id, activity_id, "generate", to_status="planned")

        activities = _load_day(conn, user_id, day)

    notice = ""
    if feasible < settings["daily_target"]:
        notice = f"可用时间只够安排 {feasible} 次，已按 {feasible} 次生成。"
    return {"created": len(drafts), "activities": activities, "notice": notice}


def suggest_day(
    user_id: int,
    count: int | None = None,
    conditions: dict | None = None,
) -> tuple[list[rules.Draft], int, dict]:
    """只算不存（规则引擎）。Agent 与"生成建议"按钮共用这一条路径。

    ``conditions`` 是本次生成的临时条件（见 ``resolve_conditions``）；
    不传就和以前一样，完全按已保存的设置算。
    """
    settings = resolve_conditions(user_id, conditions)
    day = rules.now_local().date()
    busy = busy_of_day(user_id, day)
    drafts, feasible = rules.plan_day(
        settings, day, busy, rules.now_local(), count or settings["daily_target"]
    )
    return drafts, feasible, settings


def confirm(user_id: int, items: list[dict]) -> dict:
    """保存用户确认过的建议。

    逐条重新校验、任何一条不合法就整批失败：
    "这是 Agent 给的"不是跳过后端规则的理由，否则验收第 9 条（免打扰拒绝）就形同虚设。
    """
    settings = get_settings(user_id)
    now = rules.now_local()
    stamp = int(time.time())

    with get_conn() as conn:
        for index, item in enumerate(items, start=1):
            moment = rules.parse_dt(item["scheduled_at"])
            day = moment.date()
            # 不分状态：已完成的时刻也要拦住，否则会和已有记录叠成页面上两行同一时间
            busy = _load_day(conn, user_id, day)
            try:
                rules.validate_slot(settings, moment, day, busy, now)
            except rules.RuleViolation as exc:
                # 带上序号，前端才能把对应那张建议卡标红
                raise rules.RuleViolation(exc.code, f"第 {index} 条：{exc.message}") from None

            activity_id = _insert_activity(
                conn,
                user_id,
                moment=moment,
                activity_type=item["activity_type"],
                title=item["title"],
                description=item["description"],
                duration_min=int(item.get("duration_min") or 5),
                source="agent",
                stamp=stamp,
            )
            _log_event(conn, user_id, activity_id, "confirm", to_status="planned", note="agent")

        return {
            "saved": len(items),
            "activities": _load_day(conn, user_id, now.date()),
        }


def complete(user_id: int, activity_id: int) -> dict:
    """标记完成。已完成再点一次返回同样的结果（幂等），不做重复写入。"""
    with get_conn() as conn:
        activity = _owned(conn, user_id, activity_id)
        if activity["status"] == "completed":
            return {"activity": activity, "idempotent": True}
        if activity["status"] != "planned":
            raise rules.RuleViolation(
                "INVALID_STATE",
                f"活动当前是「{rules.STATUS_LABELS[activity['status']]}」，不能再标记完成。",
            )

        conn.execute(
            "UPDATE activities SET status = 'completed', updated_at = ? WHERE id = ?",
            (int(time.time()), activity_id),
        )
        _log_event(conn, user_id, activity_id, "complete",
                   from_status="planned", to_status="completed")
        return {"activity": _owned(conn, user_id, activity_id), "idempotent": False}


def skip(user_id: int, activity_id: int, reason: str | None = None) -> dict:
    """今天不方便：跳过并记录原因，当天不再反复安排这条。"""
    with get_conn() as conn:
        activity = _owned(conn, user_id, activity_id)
        if activity["status"] != "planned":
            raise rules.RuleViolation(
                "INVALID_STATE",
                f"活动当前是「{rules.STATUS_LABELS[activity['status']]}」，不能跳过。",
            )

        conn.execute(
            """
            UPDATE activities
            SET status = 'skipped', skipped_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            ((reason or "").strip() or None, int(time.time()), activity_id),
        )
        _log_event(conn, user_id, activity_id, "skip",
                   from_status="planned", to_status="skipped", note=reason)
        return {"activity": _owned(conn, user_id, activity_id)}


def postpone(user_id: int, activity_id: int) -> dict:
    """推迟 = 原记录冻结 + 新增替代条目，两步在同一个事务里。

    找不到可用时段就抛 RuleViolation，``get_conn()`` 回滚 ——
    原活动仍然是 planned，不会出现"点了一下推迟，活动凭空消失"。
    """
    settings = get_settings(user_id)
    now = rules.now_local()

    with get_conn() as conn:
        activity = _owned(conn, user_id, activity_id)
        if activity["status"] != "planned":
            raise rules.RuleViolation(
                "INVALID_STATE",
                f"活动当前是「{rules.STATUS_LABELS[activity['status']]}」，不能推迟。",
            )

        day = rules.parse_dt(activity["scheduled_at"]).date()
        # 不分状态：新位置不能落在当天任何一条记录的时刻上（自己由 ignore_id 排除）
        busy = _load_day(conn, user_id, day)
        not_before = rules.parse_dt(activity["scheduled_at"]) + timedelta(
            minutes=rules.POSTPONE_GAP_MIN
        )
        target = rules.next_slot(
            settings, day, busy, not_before, now, ignore_id=activity_id
        )
        if target is None:
            raise rules.RuleViolation(
                "NO_SLOT_AVAILABLE",
                "今天剩余的可活动时段不足，无法再往后安排。"
                "可以选择「今天不方便」，或者调整可活动时段。",
            )

        rules.validate_slot(
            settings, target, day, busy, now, ignore_id=activity_id
        )

        stamp = int(time.time())
        conn.execute(
            "UPDATE activities SET status = 'postponed', updated_at = ? WHERE id = ?",
            (stamp, activity_id),
        )
        new_id = _insert_activity(
            conn,
            user_id,
            moment=target,
            activity_type=activity["activity_type"],
            title=activity["title"],
            description=activity["description"],
            duration_min=activity["duration_min"],
            source=activity["source"],
            stamp=stamp,
            postponed_from=activity_id,
            postpone_count=activity["postpone_count"] + 1,
        )
        _log_event(
            conn, user_id, activity_id, "postpone",
            from_status="planned", to_status="postponed", note=rules.iso(target),
        )
        return {
            "original": _owned(conn, user_id, activity_id),
            "replacement": _owned(conn, user_id, new_id),
        }


# ---------------------------------------------------------------- 统计

def weekly_stats(user_id: int, week_start: date) -> dict:
    """本周统计。

    口径（README 里也要写）：

    - 计划数量 = 本周 ``postponed_from IS NULL`` 的原始条目数，
      推迟产生的替代条目不重复计数；
    - 完成 / 推迟 / 跳过按状态计数，推迟算在被推迟的那条原记录上。
    """
    week_end = week_start + timedelta(days=6)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, status, postponed_from, scheduled_at
            FROM activities
            WHERE user_id = ? AND substr(scheduled_at, 1, 10) BETWEEN ? AND ?
            """,
            (user_id, week_start.isoformat(), week_end.isoformat()),
        ).fetchall()

    items = [dict(row) for row in rows]
    days = []
    for offset in range(7):
        current = (week_start + timedelta(days=offset)).isoformat()
        same_day = [item for item in items if item["scheduled_at"][:10] == current]
        days.append(
            {
                "date": current,
                "completed": sum(1 for item in same_day if item["status"] == "completed"),
                "total": sum(1 for item in same_day if item["postponed_from"] is None),
            }
        )

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "planned": sum(1 for item in items if item["postponed_from"] is None),
        "completed": sum(1 for item in items if item["status"] == "completed"),
        "postponed": sum(1 for item in items if item["status"] == "postponed"),
        "skipped": sum(1 for item in items if item["status"] == "skipped"),
        "days": days,
    }

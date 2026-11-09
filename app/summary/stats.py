"""每日活动统计：给定用户和自然日，算出日报要用的那几个数字。

为什么单独成一层、且只做查询不做写入：

- 日报的数字必须**由程序算**，不能让模型猜。这一层算完直接塞进提示词，
  模型只负责把数字写成两句人话；
- 这里不 import Redis、不 import 模型客户端，所以能脱离队列和网络单测；
- 日期永远是参数，不读 ``now()``。否则跨零点跑测试会随机红。

口径上有三个地方容易算错，都在下面用注释标出来了。
"""

from __future__ import annotations

from datetime import date, timedelta

from app.activity import rules
from app.db import get_conn

# "还没结束"的状态。planned 是没动过，postponed 是被推迟后原条留下的痕迹。
UNFINISHED_STATUSES = ("planned", "postponed")


def day_bounds(report_date: date) -> tuple[str, str]:
    """日报日的查询区间，**左闭右开**。

    返回的是带 +08:00 偏移的 ISO 串，可以直接和 ``activities.scheduled_at``
    做字符串比较 —— 前提是写入路径也用 ``rules.iso()``，保证偏移量一致。
    这个前提写在 README 里了。
    """
    start = rules.at_time(report_date, "00:00")
    end = start + timedelta(days=1)
    return rules.iso(start), rules.iso(end)


def daily_stats(user_id: int, report_date: date) -> dict:
    """统计某用户某一天的活动情况。

    返回的字段直接对应日报：``planned_total`` / ``completed`` / ``skipped`` /
    ``unfinished`` / ``postpone_ops`` / ``sedentary_minutes`` / ``has_records``。
    """
    start_iso, end_iso = day_bounds(report_date)
    start = rules.at_time(report_date, "00:00")
    end = start + timedelta(days=1)

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT status, postponed_from
                 FROM activities
                WHERE user_id = ? AND scheduled_at >= ? AND scheduled_at < ?""",
            (user_id, start_iso, end_iso),
        ).fetchall()

        # 推迟次数是"操作次数"，从流水里数。
        # 不用 activities.postpone_count 求和，是因为那个字段记在活动上，
        # 一条活动跨日被推迟时归不到"哪一天操作的"；流水里的 created_at 才是操作时刻。
        postpone_ops = conn.execute(
            """SELECT COUNT(*) FROM activity_events
                WHERE user_id = ? AND action = 'postpone'
                  AND created_at >= ? AND created_at < ?""",
            (user_id, int(start.timestamp()), int(end.timestamp())),
        ).fetchone()[0]

    # 口径一：计划数只数"原始条目"。
    # 推迟会新建一条替代记录（postponed_from 指向原条），把它也算进计划数，
    # 就会出现"计划 3 次、实际只排了 3 次却有 4 条计划"的怪现象。
    planned_total = sum(1 for row in rows if row["postponed_from"] is None)
    completed = sum(1 for row in rows if row["status"] == "completed")
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    unfinished = sum(1 for row in rows if row["status"] in UNFINISHED_STATUSES)

    return {
        "report_date": report_date.isoformat(),
        "planned_total": planned_total,
        "completed": completed,
        "skipped": skipped,
        "unfinished": unfinished,
        "postpone_ops": int(postpone_ops),
        # 口径二：本项目没有"用户自报久坐时长"的入口，所以永远是 None，
        # 页面上显示"未记录"。不要写 0 —— 0 分钟是一个确定的事实陈述，
        # 而"没记录"什么都不说明，两者不能混。
        "sedentary_minutes": None,
        "has_records": bool(rows),
    }

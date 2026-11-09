"""为日报演示准备一天的活动记录。

为什么要有这个脚本：日报统计的是**已经结束的自然日**，而 ``/activity/generate``
只能给今天和未来排活动 —— 所以"昨天的完成/跳过/推迟"在正常页面操作下造不出来。
评审要复现"统计正确"这一条，就需要一个能一键生成样本数据的入口。

造出来的场景（以 ``--days-ago 1`` 为例，D = 昨天）：

| 时间 | 状态 | 用来验证 |
| --- | --- | --- |
| D 00:00 | completed | 左闭边界，必须算进这一天 |
| D 09:30 | completed | 普通完成 |
| D 14:00 | skipped | 跳过 |
| D 16:00 | planned | 未完成 |
| D 17:00 | planned（推迟替代） | `postponed_from` 指向 16:00 那条 |
| D 23:59 | completed | 右开边界前一刻，必须算进这一天 |
| D+1 00:00 | planned | 右开边界，**不能**算进这一天 |

于是统计应为：计划 5（六条里只有替代条不算新计划）、完成 3、跳过 1、
未完成 2、推迟操作 1。完成 + 跳过 + 未完成 = 6 > 计划 5，
这正是"推迟会新建替代条、几个数不能相加"的现场例子。

用法：

    docker compose exec backend python scripts/seed_activity_demo.py
    docker compose exec backend python scripts/seed_activity_demo.py --days-ago 2 --user demo

脚本只动指定用户指定那一天的 ``activities`` / ``activity_events``，
可重复执行（先清再建），不会碰别的日期。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")

from app.activity import rules  # noqa: E402
from app.db import get_conn, init_db  # noqa: E402
from app.summary.stats import daily_stats  # noqa: E402


def _insert_activity(
    conn, user_id: int, day: date, hhmm: str, status: str, postponed_from: int | None = None
) -> int:
    moment = rules.at_time(day, hhmm)
    stamp = int(moment.timestamp())
    cur = conn.execute(
        """INSERT INTO activities
               (user_id, activity_type, title, description, scheduled_at,
                duration_min, status, source, postponed_from, postpone_count,
                created_at, updated_at)
           VALUES (?, 'walk', '起身走动', '站起来走一小段，让肩颈放松一下。', ?, 5, ?,
                   'rule', ?, ?, ?, ?)""",
        (user_id, rules.iso(moment), status, postponed_from,
         1 if postponed_from else 0, stamp, stamp),
    )
    return int(cur.lastrowid)


def seed(username: str, day: date) -> None:
    start = rules.at_time(day, "00:00")
    end = start + timedelta(days=1)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"找不到用户 {username!r}，先跑 scripts/seed.py 建练习账号")
        user_id = int(row["id"])

        # 先清掉这一天已有的记录，保证脚本可重复执行
        conn.execute(
            "DELETE FROM activities WHERE user_id = ? AND scheduled_at >= ? AND scheduled_at < ?",
            (user_id, rules.iso(start), rules.iso(end)),
        )
        conn.execute(
            "DELETE FROM activity_events WHERE user_id = ? AND created_at >= ? AND created_at < ?",
            (user_id, int(start.timestamp()), int(end.timestamp())),
        )
        # 这一天如果已经有日报，也一并清掉，否则补跑会被"已有日报"挡下
        conn.execute(
            "DELETE FROM daily_summaries WHERE user_id = ? AND report_date = ?",
            (user_id, day.isoformat()),
        )

        _insert_activity(conn, user_id, day, "00:00", "completed")
        _insert_activity(conn, user_id, day, "09:30", "completed")
        _insert_activity(conn, user_id, day, "14:00", "skipped")
        original = _insert_activity(conn, user_id, day, "16:00", "postponed")
        _insert_activity(conn, user_id, day, "17:00", "planned", postponed_from=original)
        _insert_activity(conn, user_id, day, "23:59", "completed")
        # 跨日：第二天 00:00 的记录不属于这一天（左闭右开的右边界）
        _insert_activity(conn, user_id, day + timedelta(days=1), "00:00", "planned")

        # 一条推迟流水：落在这一天的操作，才计入这一天的 postpone_ops
        conn.execute(
            """INSERT INTO activity_events
                   (user_id, activity_id, action, from_status, to_status, note, created_at)
               VALUES (?, ?, 'postpone', 'planned', 'postponed', '选了下一个空档', ?)""",
            (user_id, original, int(rules.at_time(day, "16:05").timestamp())),
        )

    stats = daily_stats(user_id, day)
    print(f"用户 {username}（id={user_id}）{day} 的数据已就绪：")
    for key, value in stats.items():
        print(f"  {key:18} {value}")
    print("\n预期：planned_total=5  completed=3  skipped=1  unfinished=2  postpone_ops=1")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成日报演示用的活动记录")
    parser.add_argument(
        "--days-ago", type=int, default=1, help="几天前，默认 1（昨天）"
    )
    parser.add_argument("--user", default="demo", help="练习账号用户名，默认 demo")
    args = parser.parse_args()

    day = (datetime.now(tz=rules.tz()) - timedelta(days=args.days_ago)).date()
    init_db()
    seed(args.user, day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

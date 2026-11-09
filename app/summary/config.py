"""每日总结的配置读取：Redis 连接、cron 时间、重试策略。

和 ``app/activity/rules.py`` 一样，这一层只读环境变量、不碰数据库也不碰网络，
所以能被单测直接调用。

两个约定：

- **所有配置只在环境变量里**，不写死在代码里。演示时把
  ``APP_SUMMARY_CRON_MINUTE`` 改成 ``*/2`` 就够了，不用改代码再改回来。
- **时区必须显式给**。容器默认 UTC，不配的话 00:05 会变成北京时间 08:05，
  而且"总结昨天"会算成"总结今天"。
"""

from __future__ import annotations

import os

from arq.connections import RedisSettings


def redis_settings(*, conn_retries: int | None = None) -> RedisSettings:
    """Redis 连接参数。

    容器内 ``host`` 必须是 compose 服务名 ``redis`` —— 在 API 或 Worker 容器里，
    ``localhost`` 指的是容器自己，那里没有 Redis。
    本机直接跑 uvicorn/arq 时，用 ``APP_REDIS_HOST=127.0.0.1`` 覆盖。

    ``conn_retries`` 只在接口侧收窄：arq 默认重试 5 次、间隔递增（1+2+3+4 秒），
    Worker 常驻不急，但用户点"立即生成"时没人愿意等十秒才知道 Redis 挂了。
    """
    kwargs: dict = {
        "host": os.environ.get("APP_REDIS_HOST", "127.0.0.1"),
        "port": int(os.environ.get("APP_REDIS_PORT", "6379")),
        "database": int(os.environ.get("APP_REDIS_DB", "0")),
    }
    if conn_retries is not None:
        kwargs["conn_retries"] = conn_retries
    return RedisSettings(**kwargs)


def summary_timezone() -> str:
    """cron **触发**用的时区名（IANA 名，交给 ARQ 的 ``WorkerSettings.timezone``）。

    注意它和 ``APP_TZ_OFFSET_HOURS`` 是两个独立的配置：

    - 这个变量决定 cron 在几点几分触发；
    - ``APP_TZ_OFFSET_HOURS``（``app.activity.rules.tz``）决定活动记录与日报
      里的时间戳偏移。

    默认两者都是东八区，所以看不出区别。**如果把其中一个改成别的时区，
    必须同时改另一个**，否则 cron 在北京时间凌晨触发、却按另一个时区的
    "昨天"去查记录，查出来的是一整天的空数据。

    ``dispatch_daily_summaries`` 算"总结哪一天"用的是 ``rules.now_local()``，
    和接口的 ``_today()`` 同源，所以"哪天"这一层不会分叉；会分叉的只有触发时刻。
    """
    return os.environ.get("APP_SUMMARY_TIMEZONE", "Asia/Shanghai")


def _parse_field(raw: str, upper: int) -> set[int]:
    """把环境变量解析成 cron 需要的取值集合。

    支持三种写法，对应 cron 的常见习惯：

    - ``"5"``      → ``{5}``
    - ``"*/2"``    → ``{0, 2, 4, ...}``（演示时用这个，每两分钟触发一次）
    - ``"1,3,5"``  → ``{1, 3, 5}``
    - ``"*"``      → 全部取值

    ``upper`` 是上界（分钟 60、小时 24），返回左闭右开区间内的整数。
    """
    text = raw.strip()
    if not text or text == "*":
        return set(range(upper))
    if text.startswith("*/"):
        step = int(text[2:])
        if step <= 0:
            raise ValueError(f"cron 步长必须是正整数，收到 {raw!r}")
        return set(range(0, upper, step))
    values = {int(part) for part in text.split(",") if part.strip()}
    if not values or min(values) < 0 or max(values) >= upper:
        raise ValueError(f"cron 取值超出 0-{upper - 1}，收到 {raw!r}")
    return values


def cron_hour() -> set[int]:
    """每天几点触发调度。默认 0 点。"""
    return _parse_field(os.environ.get("APP_SUMMARY_CRON_HOUR", "0"), 24)


def cron_minute() -> set[int]:
    """每小时的第几分钟触发。

    默认 5 而不是 0：前一天 23:58 完成的记录必须被算进去，
    00:00 触发时那几分钟可能还没落库（或者刚好卡在边界上）。
    """
    return _parse_field(os.environ.get("APP_SUMMARY_CRON_MINUTE", "5"), 60)


def max_tries() -> int:
    """单条日报任务最多尝试几次（含首次）。"""
    return int(os.environ.get("APP_SUMMARY_MAX_TRIES", "3"))


def job_timeout() -> int:
    """单条日报任务的超时秒数。

    模型调用通常 1-2 秒，留 120 秒是为了容忍重试和网络抖动；
    超过这个时间说明确实卡住了，交给 ARQ 判失败比一直挂着好。
    """
    return int(os.environ.get("APP_SUMMARY_JOB_TIMEOUT", "120"))


def retry_defer_seconds() -> int:
    """任务失败后隔多久重试。

    给 20 秒而不是立刻重试：模型超时、连接被重置这类问题，
    连着重试三次通常是一起失败，隔开一段时间才有意义。
    """
    return int(os.environ.get("APP_SUMMARY_RETRY_DEFER", "20"))


def keep_result_seconds() -> int:
    """任务结果在 Redis 里保留多久。

    这个值直接决定 ``_job_id`` 去重的有效窗口：结果过期后同一个 id 可以再次入队。
    所以它不能代替数据库的唯一约束，只是"减少重复排队"。
    """
    return int(os.environ.get("APP_SUMMARY_KEEP_RESULT", "3600"))

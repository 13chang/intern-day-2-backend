"""ARQ Worker 入口：定时调度 + 后台生成日报。

为什么必须是一个独立进程，而不是 FastAPI 里的 BackgroundTasks：

1. **定时器只能有一个**。cron 写在进程里，起两个 API 副本就是两个定时器，
   任务会翻倍。Worker 单独部署，个数可控。
2. **模型调用会占住工作线程**。一次总结要等模型几百毫秒到几秒，
   塞进 API 进程会拖慢登录和查询接口。
3. **生命周期不同**。Worker 要能单独停掉做演示（验证任务在队列里等着），
   而 API 停掉用户立刻感知。

两个任务串起来是一次"调度 → 生成"的传递：

    cron(00:05) → dispatch_daily_summaries   查昨天谁有记录，逐个入队
                            ↓ enqueue_job(_job_id=summary:{user}:{date})
                   generate_daily_summary      算统计 → 调模型 → 落库

注意 ARQ 0.25 的重试规则（``arq/worker.py`` 的 ``run_job``）：

    只有 ``Retry`` / ``CancelledError`` / ``RetryJob`` 会重试，
    **普通异常抛出去就是直接判失败，不会再试**。

所以错误必须分两类处理，不能"一律 raise 等 ARQ 重试"：

- ``TransientError``（模型超时、输出不合规）→ 转成 ``Retry`` 才会重试；
- ``PermanentError``（配置缺失）→ 直接抛普通异常，一次就落 failed，
  省得白跑三次还把 error_message 刷成最后那次的样子。

启动方式：``arq app.worker.WorkerSettings``（容器里由 compose 的 command 指定）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from arq import cron, func
from arq.worker import Retry

from app.db import init_db
from app.summary import store
from app.summary.agent import PermanentError, TransientError, compose_summary
from app.summary.config import (
    cron_hour,
    cron_minute,
    job_timeout,
    keep_result_seconds,
    max_tries,
    redis_settings,
    retry_defer_seconds,
    summary_timezone,
)
from app.summary.queue import JOB_NAME, scheduled_job_id
from app.summary.stats import daily_stats

logger = logging.getLogger(__name__)

# error_code 只在这几个值里取，前端据此决定提示语，不把中文提示塞进库。
ERR_CONFIG = "config"        # 模型配置不全，不可重试
ERR_TRANSIENT = "transient"  # 模型超时 / 输出不合规，已安排重试
ERR_INTERNAL = "internal"    # 其它异常，需要看日志


def report_date_for(now: datetime) -> date:
    """cron 触发时要总结哪一天 —— 永远是昨天。

    先减 ``timedelta`` 再取日期，而不是"先取日期再减一天"：
    拿到的还是同一个时区里的时间点，跨月、跨年都不用额外判断。
    """
    return (now - timedelta(days=1)).date()


def job_id_for(user_id: int, report_date: date) -> str:
    """入队去重键（薄封装，标识格式定义在 summary/queue.py 里）。

    接口侧入队和 cron 调度必须用同一个标识，否则"页面点了立即生成"
    和"定时调度"会各排一趟、白跑一次模型。
    """
    return scheduled_job_id(user_id, report_date.isoformat())


async def ping(ctx: dict, note: str = "hello") -> str:
    """连通性自检：证明"能入队、能被领取、能回传结果"这条链路是通的。

    留着它不是为了正式功能，而是排障时第一件该做的事 ——
    日报生成不出来时，先跑 ping 就能分清是"队列坏了"还是"任务逻辑坏了"。
    """
    logger.info("ping 收到 note=%s", note)
    return f"pong: {note}"


async def dispatch_daily_summaries(ctx: dict) -> dict:
    """cron 入口：找出昨天产生过活动记录的用户，逐个入队。

    只查"昨天有记录"的用户，而不是 users 全表：给一个当天什么都没做的用户
    也生成日报，既浪费一次模型调用，又会在页面上堆出一串空日报。

    这个任务本身**可以重复跑**（演示时把 cron 改成每两分钟一次）：
    已入队未执行的 job 会被 ``_job_id`` 挡住，已生成的日报会在
    ``generate_daily_summary`` 里被跳过。
    """
    report_date = report_date_for(datetime.now(ZoneInfo(summary_timezone())))
    users = store.list_users_with_records(report_date)

    enqueued = 0
    for user_id in users:
        job = await ctx["redis"].enqueue_job(
            JOB_NAME,
            user_id,
            report_date.isoformat(),
            _job_id=job_id_for(user_id, report_date),
        )
        if job is not None:
            enqueued += 1

    logger.info(
        "调度完成 date=%s 有记录用户=%d 新入队=%d", report_date, len(users), enqueued
    )
    return {
        "report_date": report_date.isoformat(),
        "candidates": len(users),
        "enqueued": enqueued,
    }


async def generate_daily_summary(ctx: dict, user_id: int, report_date_str: str) -> dict:
    """给一个用户生成某一天的日报。

    执行顺序是有讲究的：先看有没有已完成的，再标 generating，最后才算统计、
    调模型。``generating`` 要在**调模型之前**落库 —— 页面靠它显示"正在生成"，
    如果等模型返回才写状态，轮询永远看不到中间态。
    """
    report_date = date.fromisoformat(report_date_str)

    existing = store.get_summary(user_id, report_date_str)
    if existing and existing["status"] == store.STATUS_READY:
        # ARQ 在进程被 kill 时会把同一个任务重新投递，第二次到这里必须什么都不做
        logger.info("日报已存在，跳过 user=%s date=%s", user_id, report_date_str)
        return {"status": "skipped", "reason": "already_ready"}

    store.mark_status(user_id, report_date_str, store.STATUS_GENERATING)

    try:
        stats = daily_stats(user_id, report_date)
        review, suggestion, source, model = await compose_summary(stats)
    except PermanentError as exc:
        # 重试多少次都不会好，直接抛普通异常让任务落成 failed（ARQ 不会重试）
        store.mark_status(
            user_id,
            report_date_str,
            store.STATUS_FAILED,
            code=ERR_CONFIG,
            message=str(exc),
        )
        logger.error(
            "日报生成失败（不可重试）user=%s date=%s: %s", user_id, report_date_str, exc
        )
        raise
    except TransientError as exc:
        # 标 failed 而不是留着 generating：页面要能显示"这次没成功，会再试"
        store.mark_status(
            user_id,
            report_date_str,
            store.STATUS_FAILED,
            code=ERR_TRANSIENT,
            message=str(exc),
        )
        logger.warning(
            "日报生成失败（稍后重试）user=%s date=%s: %s", user_id, report_date_str, exc
        )
        raise Retry(defer=retry_defer_seconds()) from exc
    except Exception as exc:
        store.mark_status(
            user_id,
            report_date_str,
            store.STATUS_FAILED,
            code=ERR_INTERNAL,
            message=type(exc).__name__,
        )
        logger.exception(
            "日报生成出现未预期错误 user=%s date=%s", user_id, report_date_str
        )
        raise

    wrote = store.save_ready(
        user_id,
        report_date_str,
        {
            **stats,
            "review": review,
            "suggestion": suggestion,
            "source": source,
            "model": model,
        },
    )
    # wrote=False 说明另一个 Worker 已经写好同一份，这次是重复执行、不是错误
    logger.info(
        "日报生成完成 user=%s date=%s source=%s wrote=%s",
        user_id,
        report_date_str,
        source,
        wrote,
    )
    return {
        "status": "ready" if wrote else "duplicate",
        "user_id": user_id,
        "report_date": report_date_str,
        "source": source,
    }


async def on_startup(ctx: dict) -> None:
    """Worker 起来先建表。

    正常流程是 api 容器先建表、worker 后连。但单独 ``docker compose up worker``
    时 api 可能还没跑过，日报表不存在，所有任务都会失败。
    建表语句都是 ``CREATE TABLE IF NOT EXISTS``，重复执行没有副作用。
    """
    init_db()
    logger.info("worker 就绪，时区=%s", summary_timezone())


class WorkerSettings:
    """ARQ 从这个类读取配置，只认 ``Worker.__init__`` 里存在的属性名。

    写错的属性名不会报错、只会被静默忽略 —— 比如把 ``keep_result`` 写成
    ``keep_results``，任务照样跑，只是结果不保留、``_job_id`` 去重失效。
    """

    functions = [
        func(ping, timeout=10, max_tries=1),
        # 调度只做入队，几毫秒的事；失败就等下一个 cron 周期，没必要重试
        func(dispatch_daily_summaries, timeout=60, max_tries=1),
        # name=JOB_NAME 不能省：入队时用的是这个字符串，注册名不一致就是
        # 运行时 "function not found"。写成变量而不是让 arq 取 __qualname__，
        # 是为了让"注册名"和"入队名"是同一个常量。
        func(
            generate_daily_summary,
            name=JOB_NAME,
            timeout=job_timeout(),
            max_tries=max_tries(),
        ),
    ]

    cron_jobs = [
        # unique=True：多个 worker 同时在线时，同一个时间点只有一个会真正入队
        cron(
            dispatch_daily_summaries,
            hour=cron_hour(),
            minute=cron_minute(),
            unique=True,
            max_tries=1,
        )
    ]

    # 时区必须显式给：容器默认 UTC，不配的话 00:05 会变成北京时间 08:05，
    # 而"总结昨天"也会按 UTC 的昨天来算，跨零点那几小时的数据全错位。
    timezone = ZoneInfo(summary_timezone())

    redis_settings = redis_settings()
    keep_result = keep_result_seconds()
    on_startup = on_startup

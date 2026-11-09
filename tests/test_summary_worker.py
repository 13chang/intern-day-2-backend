"""Worker 调度的测试：cron 入口、任务执行、失败分类。

这个文件重点验证三件容易写错的事：

1. **"昨天"是按配置时区算的**。容器是 UTC，如果照 ``date.today()`` 取，
   北京时间 00:05 触发时算出来的是 UTC 的今天，等于总结了一个还没过完的日子。
2. **ARQ 0.25 只对 ``Retry`` 重试**。普通异常抛出去就是终态失败。
   所以"哪些错误该重试"必须在代码里显式分类，不能指望框架兜底。
3. **重复执行不出第二份**。ARQ 在进程被 kill 时会重新投递同一个任务，
   第二次进来必须什么都不做。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from arq.worker import Retry

from app.activity import rules
from app.db import get_conn
from app.summary import agent as summary_agent
from app.summary import store
from app.summary.config import cron_hour, cron_minute, retry_defer_seconds
from app.worker import (
    ERR_CONFIG,
    ERR_INTERNAL,
    ERR_TRANSIENT,
    WorkerSettings,
    dispatch_daily_summaries,
    generate_daily_summary,
    job_id_for,
    report_date_for,
)

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 6)
DAY_STR = DAY.isoformat()


# ---------------------------------------------------------------- 测试替身


class FakeRedis:
    """只实现 ``enqueue_job`` 的假连接。

    真 Redis 在这里没有价值：要验的是"该不该入队、入队时带了什么参数"，
    而这些参数在调用点就已经确定了。
    """

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[dict] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append({"name": name, "args": args, "kwargs": kwargs})
        # 真 arq 在 _job_id 已存在时返回 None，调用方据此判断"没入队"
        return SimpleNamespace(job_id=kwargs.get("_job_id")) if self.accepted else None


def _insert_activity(
    user_id: int, day: date, hhmm: str, *, status: str = "planned"
) -> None:
    moment = rules.at_time(day, hhmm)
    stamp = int(moment.timestamp())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO activities
                   (user_id, activity_type, title, description, scheduled_at,
                    duration_min, status, source, postpone_count, created_at, updated_at)
               VALUES (?, 'walk', '起身走动', '走一小段。', ?, 5, ?, 'rule', 0, ?, ?)""",
            (user_id, rules.iso(moment), status, stamp, stamp),
        )


def _freeze_now(monkeypatch, moment: datetime) -> None:
    """把 ``app.worker`` 里的 ``datetime.now()`` 固定住。

    只替换该模块引用的名字，不动全局 datetime —— 否则 sqlite3 / logging
    这些库拿到的也是假时间，会连带出别的问题。
    """

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - 直接沿用父类语义
            return moment.astimezone(tz) if tz else moment

    monkeypatch.setattr("app.worker.datetime", _FixedDatetime)


# ---------------------------------------------------------------- 日期计算


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 普通的一天
        (datetime(2026, 11, 7, 0, 5, tzinfo=TZ), date(2026, 11, 6)),
        # 月初：跨月要退到上个月最后一天
        (datetime(2026, 3, 1, 0, 5, tzinfo=TZ), date(2026, 2, 28)),
        # 年初：跨年要退到上一年
        (datetime(2027, 1, 1, 0, 5, tzinfo=TZ), date(2026, 12, 31)),
        # 闰年 3 月 1 日
        (datetime(2028, 3, 1, 0, 5, tzinfo=TZ), date(2028, 2, 29)),
        # 白天手动触发也是"昨天"，不是"今天"
        (datetime(2026, 11, 7, 15, 30, tzinfo=TZ), date(2026, 11, 6)),
    ],
)
def test_report_date_for_is_always_yesterday(now, expected) -> None:
    assert report_date_for(now) == expected


def test_report_date_uses_given_timezone() -> None:
    """同一个 UTC 时刻，在东八区已经是新的一天，在 UTC 还是前一天。

    这条用例守的是"必须显式传时区"：不传的话取到的是进程本地时区，
    容器里就是 UTC，跨零点那八小时全部错位。
    """
    moment_utc = datetime(2026, 11, 6, 17, 0, tzinfo=ZoneInfo("UTC"))
    assert report_date_for(moment_utc.astimezone(TZ)) == date(2026, 11, 6)
    assert report_date_for(moment_utc) == date(2026, 11, 5)


def test_job_id_is_stable_and_human_readable() -> None:
    """去重键要能一眼看出是哪个用户哪一天 —— 排障时直接搜这个串。"""
    assert job_id_for(7, DAY) == "summary:7:2026-11-06"
    assert job_id_for(7, DAY) == job_id_for(7, DAY)


# ---------------------------------------------------------------- 调度任务


async def test_dispatch_enqueues_only_users_with_records(client, demo, other, monkeypatch):
    """只有昨天产生过记录的用户才入队。"""
    _freeze_now(monkeypatch, datetime(2026, 11, 7, 0, 5, tzinfo=TZ))
    _insert_activity(demo["id"], DAY, "10:00")
    # other 的记录在大前天，不该出现在昨天的候选里
    _insert_activity(other["id"], DAY - timedelta(days=2), "10:00")

    redis = FakeRedis()
    result = await dispatch_daily_summaries({"redis": redis})

    assert result == {"report_date": DAY_STR, "candidates": 1, "enqueued": 1}
    assert len(redis.calls) == 1
    call = redis.calls[0]
    assert call["name"] == "generate_daily_summary"
    assert call["args"] == (demo["id"], DAY_STR)


async def test_dispatch_passes_dedupe_job_id(client, demo, monkeypatch):
    """入队必须带 ``_job_id``，否则演示时每两分钟一次的 cron 会堆出一串重复任务。"""
    _freeze_now(monkeypatch, datetime(2026, 11, 7, 0, 5, tzinfo=TZ))
    _insert_activity(demo["id"], DAY, "10:00")

    redis = FakeRedis()
    await dispatch_daily_summaries({"redis": redis})

    assert redis.calls[0]["kwargs"]["_job_id"] == job_id_for(demo["id"], DAY)


async def test_dispatch_no_candidates_makes_no_call(client, monkeypatch):
    """没人有记录时不该碰 Redis 的入队接口。"""
    _freeze_now(monkeypatch, datetime(2026, 11, 7, 0, 5, tzinfo=TZ))

    redis = FakeRedis()
    result = await dispatch_daily_summaries({"redis": redis})

    assert result["candidates"] == 0
    assert result["enqueued"] == 0
    assert redis.calls == []


async def test_dispatch_counts_only_accepted_enqueues(client, demo, monkeypatch):
    """Redis 说"这个 _job_id 已经在队列里了"（返回 None）时，不能算进入队数。

    否则日志上会显示"入队 1"，实际队列里一直是上一次那条，排障时会被带偏。
    """
    _freeze_now(monkeypatch, datetime(2026, 11, 7, 0, 5, tzinfo=TZ))
    _insert_activity(demo["id"], DAY, "10:00")

    redis = FakeRedis(accepted=False)
    result = await dispatch_daily_summaries({"redis": redis})

    assert result["candidates"] == 1
    assert result["enqueued"] == 0
    assert len(redis.calls) == 1


# ---------------------------------------------------------------- 生成任务


def _fake_compose(review="计划 3 次，完成 2 次。", suggestion="把下午那次提前。"):
    """替换掉真模型调用，只留下"返回四元组"这个契约。"""

    async def _compose(stats: dict):
        return review, suggestion, "rule", "template"

    return _compose


async def test_generate_writes_ready_summary(client, demo, monkeypatch):
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    _insert_activity(demo["id"], DAY, "14:00", status="completed")
    _insert_activity(demo["id"], DAY, "16:00", status="skipped")
    monkeypatch.setattr("app.worker.compose_summary", _fake_compose())

    result = await generate_daily_summary({}, demo["id"], DAY_STR)

    assert result["status"] == "ready"
    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["status"] == "ready"
    assert row["planned_total"] == 3
    assert row["completed"] == 2
    assert row["skipped"] == 1
    assert row["review"] == "计划 3 次，完成 2 次。"
    assert row["source"] == "rule"
    assert row["model"] == "template"
    # 没有自报入口，久坐时长必须是 NULL 而不是 0（0 会被当成"用户报了 0 分钟"）
    assert row["sedentary_minutes"] is None


async def test_generate_skips_ready_row(client, demo, monkeypatch):
    """已经是 ready 的日报，再投递一次任务不该重算、也不该重新调模型。"""
    store.save_ready(demo["id"], DAY_STR, {"review": "已有", "suggestion": "已有"})
    called = []

    async def _should_not_run(stats):
        called.append(stats)
        return "x", "y", "rule", "template"

    monkeypatch.setattr("app.worker.compose_summary", _should_not_run)

    result = await generate_daily_summary({}, demo["id"], DAY_STR)

    assert result == {"status": "skipped", "reason": "already_ready"}
    assert called == []
    assert store.get_summary(demo["id"], DAY_STR)["review"] == "已有"


async def test_generate_marks_generating_before_calling_model(client, demo, monkeypatch):
    """调模型之前就要把状态写成 generating —— 页面靠它显示"正在生成"。

    如果等到模型返回才落状态，轮询接口永远只会看到 pending 然后直接变 ready，
    "生成中"这个中间态实际上不存在。
    """
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    seen: list[str | None] = []

    async def _peek(stats):
        row = store.get_summary(demo["id"], DAY_STR)
        seen.append(row["status"] if row else None)
        return "回顾", "建议", "rule", "template"

    monkeypatch.setattr("app.worker.compose_summary", _peek)

    await generate_daily_summary({}, demo["id"], DAY_STR)

    assert seen == ["generating"]


async def test_permanent_error_fails_once_without_retry(client, demo, monkeypatch):
    """配置类错误直接落 failed，抛普通异常。

    ARQ 0.25 只对 ``Retry`` 重试，普通异常就是终态 —— 正是这里想要的效果：
    缺 API Key 重试三次也还是缺。
    """

    async def _boom(stats):
        raise summary_agent.PermanentError("模型配置不完整：缺少 APP_LLM_API_KEY")

    monkeypatch.setattr("app.worker.compose_summary", _boom)

    with pytest.raises(summary_agent.PermanentError):
        await generate_daily_summary({}, demo["id"], DAY_STR)

    row = store.get_summary(demo["id"], DAY_STR)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == ERR_CONFIG
    assert "APP_LLM_API_KEY" in row["error_message"]


async def test_transient_error_is_converted_to_arq_retry(client, demo, monkeypatch):
    """可重试错误必须转成 ``Retry``，否则 ARQ 不会重试，一次抖动就永久失败。"""

    async def _flaky(stats):
        raise summary_agent.TransientError("模型调用失败（TimeoutError）")

    monkeypatch.setattr("app.worker.compose_summary", _flaky)

    with pytest.raises(Retry) as excinfo:
        await generate_daily_summary({}, demo["id"], DAY_STR)

    # defer_score 是毫秒；默认 20 秒
    assert excinfo.value.defer_score == retry_defer_seconds() * 1000
    row = store.get_summary(demo["id"], DAY_STR)
    assert row["status"] == "failed"
    assert row["error_code"] == ERR_TRANSIENT


async def test_unexpected_error_is_recorded_then_reraised(client, demo, monkeypatch):
    """没预料到的异常：库里留个能查的 code，栈打到日志，异常继续往上抛。"""

    async def _explode(stats):
        raise ValueError("分母为零")

    monkeypatch.setattr("app.worker.compose_summary", _explode)

    with pytest.raises(ValueError):
        await generate_daily_summary({}, demo["id"], DAY_STR)

    row = store.get_summary(demo["id"], DAY_STR)
    assert row["error_code"] == ERR_INTERNAL
    # 只存类型名，不把异常原文写库 —— 原文可能带请求体或密钥片段
    assert row["error_message"] == "ValueError"


async def test_failed_row_can_be_retried_into_ready(client, demo, monkeypatch):
    """失败不是终点：下一次重试成功之后，错误信息必须被清掉。"""
    store.mark_status(
        demo["id"], DAY_STR, store.STATUS_FAILED, code=ERR_TRANSIENT, message="超时"
    )
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    monkeypatch.setattr("app.worker.compose_summary", _fake_compose("第二次成功", "继续"))

    result = await generate_daily_summary({}, demo["id"], DAY_STR)

    assert result["status"] == "ready"
    row = store.get_summary(demo["id"], DAY_STR)
    assert row["status"] == "ready"
    assert row["review"] == "第二次成功"
    assert row["error_code"] is None
    assert row["error_message"] is None


async def test_running_twice_keeps_one_row(client, demo, monkeypatch):
    """同一个任务被投递两次（进程被 kill 后重投），表里仍然只有一行。"""
    _insert_activity(demo["id"], DAY, "10:00", status="completed")
    monkeypatch.setattr("app.worker.compose_summary", _fake_compose())

    first = await generate_daily_summary({}, demo["id"], DAY_STR)
    second = await generate_daily_summary({}, demo["id"], DAY_STR)

    assert first["status"] == "ready"
    assert second["status"] == "skipped"
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries WHERE user_id = ? AND report_date = ?",
            (demo["id"], DAY_STR),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------- WorkerSettings


def test_worker_settings_registers_all_functions() -> None:
    """任务名是入队时的字符串键，拼错就是运行时报"function not found"。"""
    names = {fn.name for fn in WorkerSettings.functions}
    assert names == {"ping", "dispatch_daily_summaries", "generate_daily_summary"}


def test_generate_job_has_timeout_and_retries() -> None:
    by_name = {fn.name: fn for fn in WorkerSettings.functions}
    generate = by_name["generate_daily_summary"]
    # 至少要给两次机会：模型抖动是最常见的失败原因
    assert generate.max_tries is not None and generate.max_tries >= 2
    assert generate.timeout_s is not None and generate.timeout_s >= 30


def test_dispatch_job_does_not_retry() -> None:
    """调度只做入队，失败了等下一个 cron 周期就够，重试反而可能重复入队。"""
    by_name = {fn.name: fn for fn in WorkerSettings.functions}
    assert by_name["dispatch_daily_summaries"].max_tries == 1


def test_cron_job_is_daily_and_unique() -> None:
    assert len(WorkerSettings.cron_jobs) == 1
    job = WorkerSettings.cron_jobs[0]
    assert job.coroutine is dispatch_daily_summaries
    # unique=True 才挡得住"两个 worker 同时在线各跑一次"
    assert job.unique is True
    assert job.hour == cron_hour()
    assert job.minute == cron_minute()
    # 不该在 worker 启动时立刻跑一次：那样每次重启都会多生成一批日报
    assert job.run_at_startup is False


def test_worker_timezone_is_configured() -> None:
    """不显式给时区的话，容器里会退回 UTC，cron 和"昨天"都会偏八小时。"""
    assert WorkerSettings.timezone == ZoneInfo("Asia/Shanghai")


def test_keep_result_is_set() -> None:
    """keep_result 决定 _job_id 去重窗口；设成 0 就等于没有去重。"""
    assert WorkerSettings.keep_result > 0


def test_worker_creates_tables_on_startup() -> None:
    """单独起 worker（api 还没跑过）时也要能把表建出来。"""
    assert WorkerSettings.on_startup is not None


# ---------------------------------------------------------------- 配置解析


def test_cron_defaults_are_daily_at_0005() -> None:
    assert cron_hour() == {0}
    # 默认 5 分而不是 0 分：给前一天 23:5x 的记录留出落库时间
    assert cron_minute() == {5}


def test_cron_env_override(monkeypatch) -> None:
    monkeypatch.setenv("APP_SUMMARY_CRON_MINUTE", "*/2")
    assert cron_minute() == set(range(0, 60, 2))


def test_retry_defer_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("APP_SUMMARY_RETRY_DEFER", "45")
    assert retry_defer_seconds() == 45

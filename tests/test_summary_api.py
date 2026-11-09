"""日报接口的测试：查询四态、权限隔离、手动触发的四种"没入队"。

这个文件守的是任务书里几条硬要求：

- 换一个 ``day`` 只能看到自己的其他日期，**永远看不到别人的日报**；
- 没生成不是错误，返回 200 + ``missing``，让页面能区分"还没生成"和"请求失败"；
- 手动触发只入队、立刻返回，**Redis 不可用时不能假装成功**；
- 已有日报 / 正在生成 / 刚点过一次，三种情况要给出不同状态，
  否则页面只会显示一句含糊的"已提交"，用户就一直刷新。

Redis 用假的替身：这里验的是"接口怎么描述入队结果"，不是 Redis 本身。
真 Redis 的链路由 ``tests/test_summary_worker.py`` 和容器内的端到端脚本覆盖。
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.activity import rules
from app.db import get_conn
from app.summary import store

TODAY = rules.now_local().date()
YESTERDAY = TODAY - timedelta(days=1)


def _payload(**overrides) -> dict:
    base = {
        "planned_total": 3,
        "completed": 2,
        "skipped": 1,
        "unfinished": 0,
        "postpone_ops": 2,
        "sedentary_minutes": None,
        "review": "计划 3 次，完成 2 次。",
        "suggestion": "把下午那次提前到午休后。",
        "source": "rule",
        "model": "template",
    }
    base.update(overrides)
    return base


class FakePool:
    """只实现 ``enqueue_job`` 的假连接池。"""

    def __init__(self, accepted: bool = True, boom: Exception | None = None) -> None:
        self.accepted = accepted
        self.boom = boom
        self.calls: list[dict] = []

    async def enqueue_job(self, name, *args, **kwargs):
        if self.boom is not None:
            raise self.boom
        self.calls.append({"name": name, "args": args, "kwargs": kwargs})
        if not self.accepted:
            # 真 arq 在 _job_id 已存在时返回 None
            return None
        return SimpleNamespace(job_id=kwargs.get("_job_id"))


@pytest.fixture()
def fake_pool(monkeypatch):
    """把接口里的连接池换成替身，返回替身本身方便断言。"""
    pool = FakePool()
    from app.routers import summary as summary_router

    async def _pool_for(request):  # noqa: ANN001 - 签名对齐真实现
        return pool

    monkeypatch.setattr(summary_router, "pool_for", _pool_for)
    return pool


# ---------------------------------------------------------------- 查询


def test_summary_requires_login(client) -> None:
    assert client.get("/summary").status_code == 401
    assert client.post("/summary/run").status_code == 401


def test_missing_day_returns_200_with_missing_status(client, demo) -> None:
    """没生成过不是 404：页面要能显示「还没有日报，可以点立即生成」。"""
    response = client.get("/summary", headers=demo["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing"
    assert body["date"] == YESTERDAY.isoformat()
    assert body["planned_total"] == 0
    assert body["review"] is None
    assert body["error_message"] is None


def test_ready_summary_is_returned_verbatim(client, demo) -> None:
    store.save_ready(demo["id"], YESTERDAY.isoformat(), _payload())

    body = client.get("/summary", headers=demo["headers"]).json()

    assert body["status"] == "ready"
    assert body["planned_total"] == 3
    assert body["completed"] == 2
    assert body["skipped"] == 1
    assert body["postpone_ops"] == 2
    assert body["has_records"] is True
    # 没有自报入口，久坐时长必须是 null 而不是 0
    assert body["sedentary_minutes"] is None
    assert body["review"] == "计划 3 次，完成 2 次。"
    assert body["source"] == "rule"
    assert body["generated_at"] is not None
    # 时间串要带偏移，前端 new Date() 才不会按 UTC 解析错
    assert body["generated_at"].endswith("+08:00")


def test_empty_day_is_ready_but_has_no_records(client, demo) -> None:
    """空日也会有一份日报（说明"没有记录"），但 has_records 必须是 false。"""
    store.save_ready(
        demo["id"],
        YESTERDAY.isoformat(),
        _payload(planned_total=0, completed=0, skipped=0, unfinished=0, postpone_ops=0,
                 review="这一天没有活动记录。", suggestion="明天先排一次试试。"),
    )

    body = client.get("/summary", headers=demo["headers"]).json()

    assert body["status"] == "ready"
    assert body["has_records"] is False
    assert body["planned_total"] == 0


def test_has_records_counts_postponed_replacements(client, demo) -> None:
    """计划数是 0 但存在推迟替代条目时，仍然要算作"有记录"。

    这种日子 planned_total=0、unfinished=1。如果只用 planned_total 判断
    "有没有记录"，就会被误判成空日。
    """
    store.save_ready(
        demo["id"],
        YESTERDAY.isoformat(),
        _payload(planned_total=0, completed=0, skipped=0, unfinished=1, postpone_ops=1),
    )

    body = client.get("/summary", headers=demo["headers"]).json()

    assert body["has_records"] is True


def test_failed_summary_exposes_reason_but_not_traceback(client, demo) -> None:
    store.mark_status(
        demo["id"], YESTERDAY.isoformat(), store.STATUS_FAILED,
        code="transient", message="模型调用失败（TimeoutError）",
    )

    body = client.get("/summary", headers=demo["headers"]).json()

    assert body["status"] == "failed"
    assert body["error_code"] == "transient"
    assert body["error_message"] == "模型调用失败（TimeoutError）"
    assert body["review"] is None


def test_future_day_is_rejected(client, demo) -> None:
    response = client.get(
        "/summary", params={"day": (TODAY + timedelta(days=1)).isoformat()},
        headers=demo["headers"],
    )
    assert response.status_code == 422


def test_other_user_cannot_see_my_summary(client, demo, other) -> None:
    """同一个日期，另一个账号拿到的是自己的 missing，不是我的日报。

    这条用例只需要一个参数：接口里没有能指定 user_id 的入口，
    所以"越权"在结构上就不可能，测试是把这个约定钉住。
    """
    store.save_ready(demo["id"], YESTERDAY.isoformat(), _payload(review="我的日报"))

    mine = client.get("/summary", headers=demo["headers"]).json()
    theirs = client.get("/summary", headers=other["headers"]).json()

    assert mine["review"] == "我的日报"
    assert theirs["status"] == "missing"
    assert theirs["review"] is None


# ---------------------------------------------------------------- 手动触发


def test_run_rejects_today(client, demo, fake_pool) -> None:
    """今天还没过完，统计没有意义。"""
    response = client.post(
        "/summary/run", params={"day": TODAY.isoformat()}, headers=demo["headers"]
    )
    assert response.status_code == 422
    assert fake_pool.calls == []


def test_run_rejects_too_far_back(client, demo, fake_pool) -> None:
    response = client.post(
        "/summary/run",
        params={"day": (TODAY - timedelta(days=30)).isoformat()},
        headers=demo["headers"],
    )
    assert response.status_code == 422
    assert fake_pool.calls == []


def test_run_enqueues_and_returns_immediately(client, demo, fake_pool) -> None:
    response = client.post("/summary/run", headers=demo["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["queued"] is True
    assert body["status"] == "queued"
    assert body["date"] == YESTERDAY.isoformat()
    # 只入队：任务什么都没执行，所以库里仍然没有日报
    assert store.get_summary(demo["id"], YESTERDAY.isoformat()) is None
    assert len(fake_pool.calls) == 1

    call = fake_pool.calls[0]
    assert call["name"] == "generate_daily_summary"
    assert call["args"] == (demo["id"], YESTERDAY.isoformat())
    # 手动触发必须带"第几分钟"，否则失败后一小时内补跑会被结果键挡住
    assert call["kwargs"]["_job_id"].startswith(
        f"summary:run:{demo['id']}:{YESTERDAY.isoformat()}:"
    )


def test_run_does_not_enqueue_when_ready(client, demo, fake_pool) -> None:
    """已有日报就直接复用，不浪费一次模型调用。"""
    store.save_ready(demo["id"], YESTERDAY.isoformat(), _payload())

    body = client.post("/summary/run", headers=demo["headers"]).json()

    assert body["queued"] is False
    assert body["status"] == "ready"
    assert fake_pool.calls == []


def test_run_does_not_enqueue_while_generating(client, demo, fake_pool) -> None:
    store.mark_status(demo["id"], YESTERDAY.isoformat(), store.STATUS_GENERATING)

    body = client.post("/summary/run", headers=demo["headers"]).json()

    assert body["queued"] is False
    assert body["status"] == "generating"
    assert fake_pool.calls == []


def test_stale_generating_can_be_requeued(client, demo, fake_pool) -> None:
    """卡在 generating 超过宽限期，说明 Worker 已经没了，允许重新入队。

    否则这一天的日报会永远停在"生成中"，用户既看不到内容也补跑不了。
    """
    store.mark_status(demo["id"], YESTERDAY.isoformat(), store.STATUS_GENERATING)
    # 把 updated_at 往回拨一小时，模拟"任务早就应该跑完了"
    with get_conn() as conn:
        conn.execute(
            "UPDATE daily_summaries SET updated_at = ? WHERE user_id = ? AND report_date = ?",
            (int(time.time()) - 3600, demo["id"], YESTERDAY.isoformat()),
        )

    body = client.post("/summary/run", headers=demo["headers"]).json()

    assert body["queued"] is True
    assert len(fake_pool.calls) == 1


def test_failed_summary_can_be_rerun(client, demo, fake_pool) -> None:
    """失败之后必须能补跑 —— 任务书明确要求的"修复后可以补跑"。"""
    store.mark_status(
        demo["id"], YESTERDAY.isoformat(), store.STATUS_FAILED,
        code="transient", message="模型调用失败",
    )

    body = client.post("/summary/run", headers=demo["headers"]).json()

    assert body["queued"] is True


def test_deduplicated_enqueue_is_reported_honestly(client, demo, fake_pool) -> None:
    """被 _job_id 挡下时不能回"已提交"，否则用户以为在跑、其实什么都没发生。"""
    fake_pool.accepted = False

    body = client.post("/summary/run", headers=demo["headers"]).json()

    assert body["queued"] is False
    assert body["status"] == "deduplicated"
    assert body["message"]


def test_redis_down_returns_503(client, demo, monkeypatch) -> None:
    """Redis 不可用时必须报错，不能假装入队成功。"""
    from app.routers import summary as summary_router

    async def _boom(request):  # noqa: ANN001
        raise ConnectionError("Error 111 connecting to redis:6379")

    monkeypatch.setattr(summary_router, "pool_for", _boom)

    response = client.post("/summary/run", headers=demo["headers"])

    assert response.status_code == 503
    assert "队列" in response.json()["detail"]


def test_enqueue_error_returns_503(client, demo, monkeypatch) -> None:
    """连接池建好了但入队那一刻失败（Redis 刚好挂掉），也要 503。"""
    from app.routers import summary as summary_router

    pool = FakePool(boom=OSError("broken pipe"))

    async def _pool_for(request):  # noqa: ANN001
        return pool

    monkeypatch.setattr(summary_router, "pool_for", _pool_for)

    assert client.post("/summary/run", headers=demo["headers"]).status_code == 503


# ---------------------------------------------------------------- 路由细节


def test_only_yesterday_by_default_on_run(client, demo, fake_pool) -> None:
    """不传 day 时补跑的是昨天，不是今天。"""
    body = client.post("/summary/run", headers=demo["headers"]).json()
    assert body["date"] == YESTERDAY.isoformat()


@pytest.mark.parametrize("days_ago", [1, 3, 7])
def test_backfill_within_window_is_allowed(client, demo, fake_pool, days_ago) -> None:
    target = TODAY - timedelta(days=days_ago)
    body = client.post(
        "/summary/run", params={"day": target.isoformat()}, headers=demo["headers"]
    ).json()
    assert body["queued"] is True


def test_old_date_query_is_allowed(client, demo) -> None:
    """查询不限制回看天数：看历史是只读的，没有副作用。"""
    old = date(2026, 1, 5)
    store.save_ready(demo["id"], old.isoformat(), _payload(review="很久以前的一天"))

    body = client.get(
        "/summary", params={"day": old.isoformat()}, headers=demo["headers"]
    ).json()

    assert body["status"] == "ready"
    assert body["review"] == "很久以前的一天"


# ---------------------------------------------------------------- 任务标识


def test_job_ids_never_collide() -> None:
    """定时调度和手动补跑必须用两套标识。

    共用一套的后果：页面点了"立即生成"，那一分钟的 cron 就排不进去了；
    或者手动补跑被 cron 用剩的结果键挡住。
    """
    from app.summary.queue import manual_job_id, scheduled_job_id

    scheduled = scheduled_job_id(7, "2026-11-08")
    manual = manual_job_id(7, "2026-11-08", minute_bucket=1000)

    assert scheduled == "summary:7:2026-11-08"
    assert manual.startswith("summary:run:7:2026-11-08:")
    assert scheduled != manual


def test_manual_job_id_changes_every_minute() -> None:
    """"第几分钟"变化就换一个标识，所以一分钟之后可以补跑。"""
    from app.summary.queue import manual_job_id

    assert manual_job_id(7, "2026-11-08", 1000) != manual_job_id(7, "2026-11-08", 1001)
    assert manual_job_id(7, "2026-11-08", 1000) == manual_job_id(7, "2026-11-08", 1000)


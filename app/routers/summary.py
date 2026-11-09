"""每日总结接口：查询某一天的日报、手动触发补跑。

两条设计决定，都来自任务书里的"不能假装成功"：

1. **查询一定要按登录身份过滤**。``user_id`` 只从 ``get_current_user`` 取，
   SQL 里固定 ``WHERE user_id = ?``。换日期只能看到自己的其他日期，
   永远看不到别人的 —— 也没有任何参数能让调用方指定别人的 user_id。
2. **没生成不是错误**。查不到就返回 200 + ``status="missing"``，
   让前端能把"这一天还没生成"和"请求挂了"分开显示。用 404 反而会让
   页面进入错误态，用户看到"加载失败"而不是"可以点立即生成"。

状态取值与页面文案一一对应：

| status | 页面 |
| --- | --- |
| ``missing`` | 这一天还没有日报，可以点「立即生成」 |
| ``pending`` / ``generating`` | 正在生成，可轮询 |
| ``ready`` | 展示数字 + 回顾 + 建议 |
| ``failed`` | 生成失败（只显示 error_message），可重试 |
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.activity import rules
from app.deps import get_current_user
from app.summary import store
from app.summary.config import job_timeout, max_tries, retry_defer_seconds
from app.summary.queue import enqueue_summary, manual_job_id, pool_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/summary", tags=["summary"])

# 手动补跑最多能回看几天。留 7 天够"周末把这一周补齐"，
# 又不至于让一次误操作把半年的模型调用全排上。
MAX_BACKFILL_DAYS = 7

# 参数不合法统一 422。不写 status.HTTP_422_UNPROCESSABLE_ENTITY：
# 新版 Starlette 已把它标成 deprecated，会在日志里刷弃用警告。
_UNPROCESSABLE = 422


def _today() -> date:
    """今天（按 APP_TZ_OFFSET_HOURS 的本地日期）。

    不用 ``date.today()``：容器是 UTC，北京时间 00:30 时它给出的是前一天，
    "昨天"就会算错一天。
    """
    return rules.now_local().date()


def _inflight_grace_seconds() -> int:
    """判定"这次生成是不是已经死了"的宽限期。

    最坏情况是每一次尝试都跑满超时、中间还要等重试间隔，
    超过这个时长还停在 generating，就认为 Worker 已经不在了，允许重新入队。
    """
    attempts = max(1, max_tries())
    return job_timeout() * attempts + retry_defer_seconds() * attempts


def _format_ts(epoch: int | None) -> str | None:
    """秒级时间戳 → 带偏移的 ISO 串（和活动记录同一种格式）。"""
    if not epoch:
        return None
    return rules.iso(datetime.fromtimestamp(int(epoch), tz=rules.tz()))


# ---------------------------------------------------------------- 模型

class SummaryOut(BaseModel):
    """一份日报。字段名与前端 ``DailySummary`` 一一对应。"""

    date: str
    status: str = Field(description="missing / pending / generating / ready / failed")

    planned_total: int = 0
    completed: int = 0
    skipped: int = 0
    unfinished: int = 0
    postpone_ops: int = Field(default=0, description="推迟**操作**次数，不是活动条数")
    sedentary_minutes: int | None = Field(
        default=None, description="恒为 null：本项目没有自报久坐时长的入口"
    )
    has_records: bool = Field(default=False, description="这一天有没有任何活动记录")

    review: str | None = None
    suggestion: str | None = None
    source: str | None = Field(default=None, description="rule=本地模板 / llm=真实模型")
    model: str | None = None
    generated_at: str | None = None

    error_code: str | None = None
    error_message: str | None = Field(
        default=None, description="一句可读的原因，不含堆栈"
    )


class RunOut(BaseModel):
    """手动触发的结果。**只表示入队情况，不表示已经生成好了。**"""

    date: str
    queued: bool = Field(description="true=这次真的入队了；false=被下列原因挡下")
    status: str = Field(description="ready / generating / queued / deduplicated")
    job_id: str | None = None
    message: str = ""


# ---------------------------------------------------------------- 序列化

def _serialize(row: dict, report_date: str) -> dict:
    """把库里一行变成响应。

    ``has_records`` 没有单独存字段，从计数反推：一条活动记录的状态必然是
    completed / skipped / planned / postponed 之一，而 planned 与 postponed
    都算进 ``unfinished``。所以三项之和为 0 等价于"这一天没有任何记录"，
    不需要再多一次 COUNT 查询。
    """
    has_records = (row["completed"] + row["skipped"] + row["unfinished"]) > 0
    return {
        "date": report_date,
        "status": row["status"],
        "planned_total": row["planned_total"],
        "completed": row["completed"],
        "skipped": row["skipped"],
        "unfinished": row["unfinished"],
        "postpone_ops": row["postpone_ops"],
        "sedentary_minutes": row["sedentary_minutes"],
        "has_records": has_records,
        "review": row["review"],
        "suggestion": row["suggestion"],
        "source": row["source"],
        "model": row["model"],
        "generated_at": _format_ts(row["updated_at"]),
        # 失败原因只回消息，不回堆栈；error_code 给前端做分支，不直接显示
        "error_code": row["error_code"],
        "error_message": row["error_message"],
    }


def _missing(report_date: str) -> dict:
    return {
        "date": report_date,
        "status": "missing",
        "planned_total": 0,
        "completed": 0,
        "skipped": 0,
        "unfinished": 0,
        "postpone_ops": 0,
        "sedentary_minutes": None,
        "has_records": False,
        "review": None,
        "suggestion": None,
        "source": None,
        "model": None,
        "generated_at": None,
        "error_code": None,
        "error_message": None,
    }


# ---------------------------------------------------------------- 查询

@router.get("", response_model=SummaryOut, summary="查看某一天的日报")
def read_summary(
    day: date | None = Query(default=None, description="不传就是昨天"),
    user: dict = Depends(get_current_user),
) -> dict:
    """查已保存的日报，**不触发任何生成**。

    只读的好处是刷新页面、轮询都由前端决定节奏，不会因为多刷几次
    就往队列里多塞任务。
    """
    today = _today()
    target = day or (today - timedelta(days=1))
    if target > today:
        raise HTTPException(_UNPROCESSABLE, detail="那一天还没结束，没有日报")

    row = store.get_summary(user["id"], target.isoformat())
    if row is None:
        return _missing(target.isoformat())
    return _serialize(row, target.isoformat())


# ---------------------------------------------------------------- 手动触发

@router.post("/run", response_model=RunOut, summary="立即生成/补跑某一天的日报")
async def run_summary(
    request: Request,
    day: date | None = Query(default=None, description="不传就是昨天；最多回看 7 天"),
    user: dict = Depends(get_current_user),
) -> dict:
    """只入队，**不等模型**。返回时任务通常还没开始跑。

    四种"没入队"的情况分开告诉用户，因为它们对应页面上四种不同的下一步：
    已有日报（不用再等）、正在生成（继续轮询）、刚点过一次（等一会儿）、
    队列不可用（这不是用户的问题，稍后重试）。
    """
    today = _today()
    target = day or (today - timedelta(days=1))
    iso_day = target.isoformat()

    if target >= today:
        raise HTTPException(_UNPROCESSABLE, detail="只能为已经结束的自然日生成日报")
    if target < today - timedelta(days=MAX_BACKFILL_DAYS):
        raise HTTPException(
            _UNPROCESSABLE, detail=f"最多补跑最近 {MAX_BACKFILL_DAYS} 天"
        )

    existing = store.get_summary(user["id"], iso_day)
    if existing is not None:
        if existing["status"] == store.STATUS_READY:
            # 任务书说"已有成功结果可以直接复用"，这里不重算、也不浪费一次模型调用
            return {
                "date": iso_day,
                "queued": False,
                "status": "ready",
                "message": "这一天已经有日报了",
            }
        # STATUS_PENDING 是表的默认值，本项目的写入路径只会落 generating / failed，
        # 所以正常流程里碰不到；一起判是为了兼容手工写进库的行，不至于被当成"没在跑"
        # 而重复入队。
        if existing["status"] in (store.STATUS_GENERATING, store.STATUS_PENDING):
            alive = int(time.time()) - int(existing["updated_at"]) < _inflight_grace_seconds()
            if alive:
                return {
                    "date": iso_day,
                    "queued": False,
                    "status": "generating",
                    "message": "正在生成，稍等一会儿再刷新",
                }
            # 超过宽限期还停在 generating：多半是 Worker 被 kill 了，
            # 允许重新入队，否则这一天会永远卡在"生成中"
            logger.warning(
                "日报长时间停在 generating，重新入队 user=%s date=%s",
                user["id"],
                iso_day,
            )

    job_id = manual_job_id(user["id"], iso_day, minute_bucket=int(time.time() // 60))
    try:
        pool = await pool_for(request)
        real_job_id = await enqueue_summary(
            pool, user["id"], iso_day, job_id=job_id
        )
    except Exception as exc:  # RedisError / OSError / 连不上
        # 关键：入队失败绝不能说"已提交"。任务书明确要求 Redis 不可用时返回明确错误。
        logger.error("日报入队失败 user=%s date=%s: %s", user["id"], iso_day, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列暂不可用，请稍后再试",
        ) from exc

    logger.info(
        "手动触发日报 user=%s date=%s job=%s 入队=%s",
        user["id"],
        iso_day,
        job_id,
        real_job_id is not None,
    )
    if real_job_id is None:
        # 同一分钟内重复点：任务已在队列里，不是失败，也不用再排一次
        return {
            "date": iso_day,
            "queued": False,
            "status": "deduplicated",
            "job_id": job_id,
            "message": "刚提交过一次，请等任务执行完再刷新",
        }
    return {
        "date": iso_day,
        "queued": True,
        "status": "queued",
        "job_id": real_job_id,
        "message": "已提交，生成完成后刷新即可看到",
    }

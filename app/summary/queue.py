"""入队侧的公共件：任务标识与 Redis 连接池。

连接池挂在 ``app.state`` 上，而不是模块级全局变量，原因有两点：

1. **测试隔离**。pytest 每个用例都新建一个 TestClient（等于新建一个事件循环），
   模块级全局池会绑在上一个已经关闭的循环上，第二个用例直接用就会报
   "attached to a different loop"。
2. **生命周期清楚**。池跟应用同生共死，shutdown 时一处关闭即可。

**惰性创建**：不在 lifespan 里建池。Redis 挂着的时候，API 的登录、活动记录、
日报查询都还能正常服务，只有"立即生成"这一个按钮不可用 ——
把 Redis 当成启动硬依赖，等于让一个可选组件拖垮整个后端。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from arq import create_pool

from app.summary.config import redis_settings

if TYPE_CHECKING:  # 只为类型标注，运行时不需要 fastapi / arq 的类型
    from arq.connections import ArqRedis
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

JOB_NAME = "generate_daily_summary"

# app.state 上的属性名。用常量而不是字符串字面量，避免读写两处拼错。
_STATE_KEY = "arq_pool"


def scheduled_job_id(user_id: int, report_date: str) -> str:
    """cron 调度用的固定任务标识：同一用户同一天只排一次。

    ``_job_id`` 只在结果保留期内有效（``keep_result``），过期后同一个 id
    可以再次入队 —— 所以它只是"减少重复排队"，真正的幂等靠数据库唯一约束。
    """
    return f"summary:{user_id}:{report_date}"


def manual_job_id(user_id: int, report_date: str, minute_bucket: int) -> str:
    """手动补跑用的任务标识：**带上"第几分钟"**，等价于一分钟内只算一次。

    为什么不能沿用固定标识：arq 的去重同时看 ``arq:job:<id>`` 和
    ``arq:result:<id>``，后者活多久由 ``keep_result`` 决定（默认 1 小时）。
    用固定标识意味着**失败之后一小时内都补跑不了** —— 页面停在"生成失败"，
    接口还回"已在队列中"，正好踩中任务书要求的"修复后可以补跑"。

    带上分钟既挡住连点（同一分钟重复点只入队一次），
    也让下一分钟的补跑能真正进队。
    """
    return f"summary:run:{user_id}:{report_date}:{minute_bucket}"


async def enqueue_summary(
    pool: ArqRedis, user_id: int, report_date: str, *, job_id: str
) -> str | None:
    """投递一条日报任务。

    返回 job_id 表示真的入队了；返回 None 表示被 ``_job_id`` 挡住
    （同一个标识的任务还在队列里或结果还没过期）—— 调用方必须把这两种情况
    分开告诉用户，不能一律回"已提交"。

    任务函数名用字符串而不是函数对象：接口进程不必 import Worker 模块，
    少一条"点一次按钮就把 Agent、模型客户端也加载起来"的路径。
    """
    job = await pool.enqueue_job(JOB_NAME, user_id, report_date, _job_id=job_id)
    return job.job_id if job is not None else None


async def pool_for(request: Request) -> ArqRedis:
    """取当前应用的连接池，没有就建一个。

    并发首次调用可能建出两个池（没有加锁），代价只是一次多余的连接；
    相比之下，为了这一个入口加把锁，会在**每个**请求上都多一次 await。
    """
    pool = getattr(request.app.state, _STATE_KEY, None)
    if pool is None:
        # conn_retries=1：Redis 挂了要尽快报错，而不是让用户干等十几秒
        pool = await create_pool(redis_settings(conn_retries=1))
        setattr(request.app.state, _STATE_KEY, pool)
        logger.info("已建立 Redis 连接池（日报入队用）")
    return pool


async def close_pool(app: FastAPI) -> None:
    """应用退出时关掉连接池。"""
    pool = getattr(app.state, _STATE_KEY, None)
    if pool is None:
        return
    try:
        await pool.aclose()
    except Exception:  # 关连接失败不该影响退出流程
        logger.warning("关闭 Redis 连接池时出错，忽略", exc_info=True)
    finally:
        setattr(app.state, _STATE_KEY, None)

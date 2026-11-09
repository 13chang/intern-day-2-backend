"""ARQ Worker 入口：定时调度 + 后台生成日报。

为什么必须是一个独立进程，而不是 FastAPI 里的 BackgroundTasks：

1. **定时器只能有一个**。cron 写在进程里，起两个 API 副本就是两个定时器，
   任务会翻倍。Worker 单独部署，个数可控。
2. **模型调用会占住工作线程**。一次总结要等模型几百毫秒到几秒，
   塞进 API 进程会拖慢登录和查询接口。
3. **生命周期不同**。Worker 要能单独停掉做演示（验证任务在队列里等着），
   而 API 停掉用户立刻感知。

启动方式：``arq app.worker.WorkerSettings``（容器里由 compose 的 command 指定）。
"""

from __future__ import annotations

import logging

from app.summary.config import redis_settings

logger = logging.getLogger(__name__)


async def ping(ctx: dict, note: str = "hello") -> str:
    """连通性自检：证明"能入队、能被领取、能回传结果"这条链路是通的。

    留着它不是为了正式功能，而是排障时第一件该做的事 ——
    日报生成不出来时，先跑 ping 就能分清是"队列坏了"还是"任务逻辑坏了"。
    """
    logger.info("ping 收到 note=%s", note)
    return f"pong: {note}"


class WorkerSettings:
    """ARQ 从这个类读取配置，只认 ``Worker.__init__`` 里存在的属性名。"""

    functions = [ping]
    redis_settings = redis_settings()

"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db
from app.observability import setup_observability
from app.routers import activity, agent, auth, items, summary
from app.summary.queue import close_pool

# 日志级别与 .env 的加载都在 app/__init__.py 里完成 ——
# 那两件事必须早于本模块（以及它导入的 routers）执行，写在这里就太晚了。

# Capacitor 打包成 App 之后，WebView 里页面的来源就是 http://localhost（不带端口），
# 由 capacitor.config.ts 的 server.androidScheme 决定。这个值不随部署环境变化，
# 所以不走 APP_CORS_ORIGINS 配置——那个变量每个环境都不一样，漏配一次就会让
# App 里所有请求卡在预检，而前端只看到一句没有细节的 "Failed to fetch"。
# https 那条对应 Capacitor 的默认 scheme，将来改回默认值时不用再动这里。
NATIVE_APP_ORIGINS = ("http://localhost", "https://localhost")

# 浏览器开发用的来源：vite 跑在 5173，前端直连宿主机映射出来的后端端口。
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def cors_origins() -> list[str]:
    raw = os.environ.get("APP_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    # 配置里没写也要补上，这样换一台开发机、换一份 .env 都不会漏。
    for native_origin in NATIVE_APP_ORIGINS:
        if native_origin not in origins:
            origins.append(native_origin)
    return origins


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # 启动时确保表结构存在，重复执行安全。
    init_db()
    # 观测是横切关注点：只在这里挂一次，业务代码不感知。
    # 内部对「一个进程只能配一次 Provider」做了幂等，测试里反复进 lifespan 也不会重复配置。
    setup_observability()
    yield
    # 日报入队用的 Redis 连接池是惰性创建的：一次都没点过「立即生成」就不会有池，
    # close_pool 对这种情况直接返回。放在应用的退出钩子里，保证只关一次。
    await close_pool(application)


app = FastAPI(
    title="Intern Day 2 Backend",
    version="0.2.0",
    description=(
        "登录 + 待办清单练习后端：会话令牌鉴权，SQLite 存储。"
        "第三天新增 Agent：Microsoft Agent Framework + AG-UI 事件流。"
    ),
    lifespan=lifespan,
)

# 浏览器直接访问宿主机映射的后端端口，因此需要放行前端来源并处理预检请求。
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(items.router)
app.include_router(activity.router)
app.include_router(summary.router)
app.include_router(agent.router)
# AG-UI 端点由官方适配函数注册，必须在 app 建好之后、启动之前挂上
agent.install_agent_endpoint(app)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}

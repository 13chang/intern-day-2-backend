"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db
from app.routers import agent, auth, items

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def cors_origins() -> list[str]:
    raw = os.environ.get("APP_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 启动时确保表结构存在，重复执行安全。
    init_db()
    yield


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
app.include_router(agent.router)
# AG-UI 端点由官方适配函数注册，必须在 app 建好之后、启动之前挂上
agent.install_agent_endpoint(app)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}

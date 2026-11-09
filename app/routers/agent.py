"""Agent 接口：AG-UI 事件流入口 + 运行信息 + 链路观测。

接口清单：

- ``POST /agent``       AG-UI 入口（SSE 事件流），需 Bearer 令牌
- ``GET  /agent/info``  当前 Agent 的运行模式、名称、只读工具列表，需 Bearer 令牌
- ``GET  /agent/traces`` 最近几次运行的去敏链路结构，需 Bearer 令牌

``POST /agent`` 由官方适配函数注册，不是手写的路由函数：
它负责把 AG-UI 的 ``RunAgentInput`` 转成 Agent Framework 的调用，
再把 Agent 的输出转成 ``RUN_STARTED`` / ``TEXT_MESSAGE_CONTENT`` / ... 事件。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel

from agent_framework.ag_ui import add_agent_framework_fastapi_endpoint

from app.agent.config import load_llm_settings
from app.agent.factory import AGENT_NAME, build_agent
from app.deps import agent_authorization_scope, get_current_user, require_agent_user
from app.observability import trace_panel_enabled
from app.trace_store import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

AGENT_PATH = "/agent"
# 手写常量：/agent/info 的 tools 字段就是它。
# 加工具时必须同步这里，否则前端顶部的「只读工具 …」标签会显示旧值。
TOOL_NAMES = [
    "list_my_todos",
    "get_my_activity_settings",
    "get_my_today_activities",
    "compute_suggested_slots",
]


class AgentInfo(BaseModel):
    """给前端和自测用的运行时信息，不含任何密钥或服务地址。"""

    agent: str
    mode: str
    model: str | None
    tools: list[str]
    reason: str


class SpanOut(BaseModel):
    """一条 span 的去敏快照：只有结构信息，没有对话内容。"""

    name: str
    span_id: str
    parent_id: str | None = None
    duration_ms: int
    attributes: dict[str, Any] = {}
    status: str


class TraceOut(BaseModel):
    trace_id: str
    span_count: int
    total_ms: int
    spans: list[SpanOut]


class TraceListOut(BaseModel):
    """链路列表。

    ``enabled=false`` 表示可观测性没开（``APP_TRACE_PANEL=0`` 或初始化失败），
    此时 ``traces`` 一定为空 —— 前端据此区分「没启用」和「还没有运行过」。
    """

    enabled: bool
    span_total: int
    traces: list[TraceOut] = []


@router.get("/info", response_model=AgentInfo, summary="Agent 运行信息")
def agent_info(user: dict = Depends(get_current_user)) -> AgentInfo:
    settings = load_llm_settings()
    return AgentInfo(
        agent=AGENT_NAME,
        mode=settings.mode,
        model=settings.model if settings.mode == "openai" else None,
        tools=TOOL_NAMES,
        reason=settings.reason,
    )


@router.get("/traces", response_model=TraceListOut, summary="最近的 Agent 链路（去敏）")
def agent_traces(limit: int = 3, user: dict = Depends(get_current_user)) -> TraceListOut:
    """返回最近几次 Agent 运行经过的步骤与耗时。

    安全边界写在实现里，不靠调用方自觉：

    - 需要登录，未登录 401（和 ``/agent/info`` 同一套校验）；
    - 只返回 span 名、耗时、白名单内的属性，**不含 prompt、回复原文、令牌**；
    - 属性白名单在 ``trace_store._filter_attributes`` 里，不在这里临时过滤 ——
      避免以后新增出口时漏掉。

    一个诚实的限制：进程内 trace 是全局的，不按用户隔离（span 里本来就没记用户标识）。
    因此这只是本地排查视角的接口，不是用户可用的功能；多用户共用一个后端进程时，
    登录用户能看到"别人那次运行有几步、每步多久"，但看不到任何对话内容。
    """
    if not trace_panel_enabled():
        return TraceListOut(enabled=False, span_total=0, traces=[])

    traces = store.recent_traces(limit=max(1, min(limit, 20)))
    return TraceListOut(
        enabled=True,
        span_total=len(store),
        traces=[
            TraceOut(
                trace_id=trace["trace_id"],
                span_count=trace["span_count"],
                total_ms=trace["total_ms"],
                spans=[SpanOut(**span) for span in trace["spans"]],
            )
            for trace in traces
        ],
    )


def install_agent_endpoint(app: FastAPI) -> None:
    """在应用上挂载 AG-UI 端点。

    - ``dependencies``：进入处理器之前先跑鉴权，未登录直接 401，不会触达模型
    - ``authorization_scope_resolver``：按登录用户划分服务端会话作用域
    """
    settings = load_llm_settings()
    agent = build_agent(settings)

    add_agent_framework_fastapi_endpoint(
        app,
        agent,
        AGENT_PATH,
        tags=["agent"],
        dependencies=[Depends(require_agent_user)],
        authorization_scope_resolver=agent_authorization_scope,
    )

    if settings.is_mock:
        logger.warning(
            "Agent 运行在 Mock 模式（%s）：链路是真的，模型回复是本地规则生成的。"
            "把 .env 里的 MODEL_MODE 改成 real 并配好 OPENAI_* 三项即可切到真实模型。",
            settings.reason,
        )
    else:
        logger.info("Agent 已接入真实模型：model=%s（密钥不进日志）", settings.model)

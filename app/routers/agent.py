"""Agent 接口：AG-UI 事件流入口 + 一个用来核对运行模式的说明接口。

接口清单：

- ``POST /agent``       AG-UI 入口（SSE 事件流），需 Bearer 令牌
- ``GET  /agent/info``  当前 Agent 的运行模式、名称、只读工具列表，需 Bearer 令牌

``POST /agent`` 由官方适配函数注册，不是手写的路由函数：
它负责把 AG-UI 的 ``RunAgentInput`` 转成 Agent Framework 的调用，
再把 Agent 的输出转成 ``RUN_STARTED`` / ``TEXT_MESSAGE_CONTENT`` / ... 事件。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel

from agent_framework.ag_ui import add_agent_framework_fastapi_endpoint

from app.agent.config import load_llm_settings
from app.agent.factory import AGENT_NAME, build_agent
from app.deps import agent_authorization_scope, get_current_user, require_agent_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

AGENT_PATH = "/agent"
TOOL_NAMES = ["list_my_todos"]


class AgentInfo(BaseModel):
    """给前端和自测用的运行时信息，不含任何密钥或服务地址。"""

    agent: str
    mode: str
    model: str | None
    tools: list[str]
    reason: str


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
            "配置 APP_LLM_BASE_URL / APP_LLM_MODEL / APP_LLM_API_KEY 后可切到真实模型。",
            settings.reason,
        )
    else:
        logger.info("Agent 已接入真实模型：model=%s", settings.model)

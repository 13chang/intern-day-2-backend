"""把「当前已验证的登录用户」交给工具函数。

为什么需要它：``add_agent_framework_fastapi_endpoint`` 在注册时就把 Agent 固定下来，
一次注册服务所有用户，工具函数拿不到 FastAPI 的 ``Depends`` 结果。
又不能把 user_id 做成工具参数——那等于让模型（和能改请求体的人）自己决定查谁的数据。

所以用一个 ContextVar 做「请求内的隐式上下文」：
1. 鉴权依赖在同一个请求的协程里校验 Bearer 令牌，成功后 ``set_current_user``；
2. 工具函数在执行时 ``current_user()`` 取回这个用户；
3. 工具不接受任何来自模型的身份参数。

一个实测过的坑：鉴权依赖必须写成 ``async def``。
写成普通 ``def`` 时 FastAPI 会把它丢到线程池执行，
``ContextVar.set`` 只改到了线程池工作线程的那份上下文副本，
请求协程里读到的仍然是 None，工具就会拿不到身份。
"""

from __future__ import annotations

import contextvars
from typing import Any, Mapping

# 一个请求一个副本；协程派生子任务时会继承当前值，SSE 流式输出的子任务也读得到。
_current_user: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "app_current_user", default=None
)


def set_current_user(user: Mapping[str, Any]) -> None:
    """由鉴权依赖在校验通过后调用。"""
    _current_user.set(user)


def current_user() -> Mapping[str, Any] | None:
    """工具函数用这个读取身份，读不到就返回 None，由工具自行降级。"""
    return _current_user.get()


def clear_current_user() -> None:
    """测试里清理用，生产路径不依赖：每个请求有自己的上下文。"""
    _current_user.set(None)

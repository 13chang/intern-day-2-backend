"""受保护接口共用的登录校验依赖。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from app.agent.identity import set_current_user
from app.db import get_conn
from app.security import token_digest

bearer_scheme = HTTPBearer(auto_error=False, description="登录接口返回的 Bearer 令牌")


def unauthorized() -> HTTPException:
    """统一的 401 响应：未携带、伪造、过期、已退出都用这一个。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="登录状态无效或已过期，请重新登录",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    """解析 Authorization: Bearer <token>，返回当前用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise unauthorized()

    digest = token_digest(credentials.credentials)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, s.expires_at
            FROM sessions AS s
            JOIN users AS u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (digest,),
        ).fetchone()

    if row is None:
        raise unauthorized()

    if int(row["expires_at"]) <= int(time.time()):
        # 过期会话顺手清理，避免表里堆积无效记录。
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
        raise unauthorized()

    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
    }


async def require_agent_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, Any]:
    """AG-UI 接口专用的鉴权依赖。

    和 ``get_current_user`` 判定完全一样（同一份会话表、同一个 401 文案），
    区别只有两点，都是为了 Agent：

    1. 写成 ``async def``：这样 ``set_current_user`` 改的是请求协程自己的上下文，
       SSE 流式输出的子任务能继承到；写成同步函数会被丢进线程池，
       工具里读到的就是 None。
    2. 校验通过后把用户放进 ContextVar，供只读工具取用。

    放在 ``dependencies=`` 里，FastAPI 会在进入 AG-UI 处理器之前执行它，
    所以未登录请求根本走不到模型调用。
    """
    # 校验要查 SQLite，是阻塞操作，放到线程池里跑，避免卡住事件循环
    user = await run_in_threadpool(get_current_user, credentials)
    set_current_user(user)
    return user


def agent_authorization_scope(user: dict[str, Any] = Depends(require_agent_user)) -> str:
    """给 AG-UI 端点的授权作用域。

    ``add_agent_framework_fastapi_endpoint`` 用它给会话、快照、审批状态分区。
    这里按用户 ID 分区，不同账号拿到的作用域不同，避免服务端状态串号。
    同一个请求内 FastAPI 会复用 ``require_agent_user`` 的缓存结果，不会重复查库。
    """
    return f"agent-user:{user['id']}"

"""受保护接口共用的登录校验依赖。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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

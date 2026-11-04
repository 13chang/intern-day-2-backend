"""登录、当前用户与退出。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.db import get_conn
from app.deps import bearer_scheme, get_current_user
from app.security import (
    issue_token,
    session_ttl_seconds,
    token_digest,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32, description="登录账号")
    password: str = Field(min_length=1, max_length=128, description="登录密码")


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


@router.post("/login", response_model=LoginResponse, summary="账号密码登录")
def login(payload: LoginRequest) -> LoginResponse:
    username = payload.username.strip()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        # 账号不存在与密码错误返回同一句提示，避免暴露账号是否存在。
        if row is None or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="账号或密码错误",
            )

        token, digest, expires_at = issue_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (digest, row["id"], expires_at, int(time.time())),
        )

    return LoginResponse(
        token=token,
        expires_in=session_ttl_seconds(),
        user=UserOut(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
        ),
    )


@router.get("/me", response_model=UserOut, summary="获取当前登录用户")
def me(user: dict = Depends(get_current_user)) -> UserOut:
    return UserOut(**user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="退出登录")
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Response:
    """删除服务端会话，原令牌立即失效。令牌无效时同样返回 204，便于前端清理本地状态。"""
    if credentials is not None and credentials.credentials:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (token_digest(credentials.credentials),),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

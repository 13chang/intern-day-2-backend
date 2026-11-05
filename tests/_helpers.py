"""测试共用的小工具。

放在这里而不是 conftest.py，是为了让测试模块显式 `from tests._helpers import ...`，
不依赖 pytest 的隐式 sys.path 插入行为。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_conn
from app.security import hash_password

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo1234"
DEMO_DISPLAY_NAME = "练习用户"

OTHER_USERNAME = "other"
OTHER_PASSWORD = "other1234"
OTHER_DISPLAY_NAME = "另一个用户"

# 账号不存在与密码错误必须返回同一句提示，测试里复用它做断言。
UNIFIED_LOGIN_ERROR = "账号或密码错误"
UNAUTHORIZED_DETAIL = "登录状态无效或已过期，请重新登录"


def create_user(username: str, password: str, display_name: str) -> int:
    """直接写库创建用户，跳过登录流程。返回用户 id。"""
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (username, display_name, hash_password(password)),
        )
        return int(cursor.lastrowid)


def login(client: TestClient, username: str, password: str) -> str:
    """登录并返回令牌明文；失败时直接让用例报错，避免后续断言掩盖真实原因。"""
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_unauthorized(response) -> None:
    """校验统一的 401 响应结构：未携带、伪造、过期、已退出都用同一个响应。"""
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == UNAUTHORIZED_DETAIL
    assert response.headers.get("www-authenticate") == "Bearer"


def fetch_session_rows() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT token_hash, user_id, expires_at, created_at FROM sessions"
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_item_rows() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, title, done, created_at FROM items ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def insert_session(token_hash: str, user_id: int, expires_at: int) -> None:
    """直接插入会话，用于构造过期等无法通过登录流程产生的状态。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, expires_at, expires_at - 3600),
        )

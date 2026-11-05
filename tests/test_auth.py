"""登录、获取当前用户、退出登录。

覆盖任务书要求的几个关键点：
  - 账号不存在与密码错误返回同一提示，不泄露账号是否存在；
  - 参数缺失 / 空值返回 422；
  - 未携带、伪造、过期、已退出的凭证一律 401；
  - 令牌明文不落库，库里只有 SHA-256 摘要。
"""

from __future__ import annotations

import time

import pytest

from app.security import token_digest
from tests._helpers import (
    DEMO_DISPLAY_NAME,
    DEMO_PASSWORD,
    DEMO_USERNAME,
    UNAUTHORIZED_DETAIL,
    UNIFIED_LOGIN_ERROR,
    assert_unauthorized,
    create_user,
    fetch_session_rows,
    insert_session,
)

LOGIN_PATH = "/auth/login"
ME_PATH = "/auth/me"
LOGOUT_PATH = "/auth/logout"


# ---------------------------------------------------------------- 登录成功路径


def test_login_returns_token_and_user(client, demo):
    response = client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["token"], str) and body["token"]
    assert body["expires_in"] >= 30
    assert body["user"] == {
        "id": demo["id"],
        "username": DEMO_USERNAME,
        "display_name": DEMO_DISPLAY_NAME,
    }


def test_login_creates_a_session_row(client, demo):
    before = len(fetch_session_rows())

    client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
    )

    rows = fetch_session_rows()
    assert len(rows) == before + 1
    assert all(row["user_id"] == demo["id"] for row in rows)


def test_login_trims_username(client, demo):
    """用户名两侧空格被容忍，避免用户复制粘贴时登录失败。"""
    response = client.post(
        LOGIN_PATH,
        json={"username": f"  {DEMO_USERNAME}  ", "password": DEMO_PASSWORD},
    )

    assert response.status_code == 200, response.text
    assert response.json()["user"]["username"] == DEMO_USERNAME


def test_login_does_not_trim_password(client, demo):
    """密码不裁剪空格：多余空格应当校验失败，而不是被悄悄放过。"""
    response = client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": f" {DEMO_PASSWORD} "},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == UNIFIED_LOGIN_ERROR


def test_login_response_does_not_leak_password(client, demo):
    response = client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
    )

    body = response.text
    assert DEMO_PASSWORD not in body
    assert "password" not in body.lower()
    assert "$argon2" not in body


# ---------------------------------------------------------------- 登录失败路径


def test_login_with_wrong_password_returns_401(client, demo):
    response = client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == UNIFIED_LOGIN_ERROR


def test_login_with_unknown_user_returns_same_message(client):
    """不存在的账号必须和密码错误返回完全一样的提示。"""
    response = client.post(
        LOGIN_PATH,
        json={"username": "nobody", "password": "whatever"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == UNIFIED_LOGIN_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"username": "demo"},
        {"password": "demo1234"},
        {"username": "", "password": "demo1234"},
        {"username": "demo", "password": ""},
        {"username": "x" * 33, "password": "demo1234"},
        {"username": "demo", "password": "x" * 129},
    ],
)
def test_login_rejects_invalid_payload(client, payload):
    """字段缺失、空值、超长都应在参数校验阶段被拒（422），不会进入业务逻辑。"""
    response = client.post(LOGIN_PATH, json=payload)

    assert response.status_code == 422, response.text


def test_login_rejects_non_json_body(client):
    response = client.post(
        LOGIN_PATH,
        content="not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------- 当前用户


def test_me_returns_current_user(client, demo):
    response = client.get(ME_PATH, headers=demo["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == DEMO_USERNAME
    assert body["display_name"] == DEMO_DISPLAY_NAME
    # 只返回必要字段，不暴露密码哈希与会话信息。
    assert set(body) == {"id", "username", "display_name"}


def test_me_without_token_returns_401(client):
    assert_unauthorized(client.get(ME_PATH))


def test_me_with_forged_token_returns_401(client, demo):
    assert_unauthorized(client.get(ME_PATH, headers={"Authorization": "Bearer forged-token"}))


def test_me_with_wrong_scheme_returns_401(client, demo):
    assert_unauthorized(client.get(ME_PATH, headers={"Authorization": f"Basic {demo['token']}"}))


def test_me_with_empty_bearer_returns_401(client):
    assert_unauthorized(client.get(ME_PATH, headers={"Authorization": "Bearer "}))


def test_me_with_expired_session_returns_401_and_cleans_up(client, demo):
    """过期会话不仅拒绝，还应顺手从 sessions 表删除。"""
    expired_token = "expired-token-for-test"
    insert_session(
        token_hash=token_digest(expired_token),
        user_id=demo["id"],
        expires_at=int(time.time()) - 10,
    )

    response = client.get(ME_PATH, headers={"Authorization": f"Bearer {expired_token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == UNAUTHORIZED_DETAIL
    remaining = [row["token_hash"] for row in fetch_session_rows()]
    assert token_digest(expired_token) not in remaining


# ---------------------------------------------------------------- 退出登录


def test_logout_returns_204_and_invalidates_token(client, demo):
    response = client.post(LOGOUT_PATH, headers=demo["headers"])

    assert response.status_code == 204
    assert response.content == b""
    assert_unauthorized(client.get(ME_PATH, headers=demo["headers"]))
    assert_unauthorized(client.get("/items", headers=demo["headers"]))


def test_logout_without_token_returns_204(client):
    """无令牌也返回 204，方便前端无论如何都能完成本地清理。"""
    response = client.post(LOGOUT_PATH)

    assert response.status_code == 204


def test_logout_with_invalid_token_returns_204(client, demo):
    response = client.post(LOGOUT_PATH, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 204
    # 别人的有效会话不受影响。
    assert client.get(ME_PATH, headers=demo["headers"]).status_code == 200


def test_logout_twice_is_idempotent(client, demo):
    assert client.post(LOGOUT_PATH, headers=demo["headers"]).status_code == 204
    assert client.post(LOGOUT_PATH, headers=demo["headers"]).status_code == 204


def test_logout_only_removes_current_session(client, demo):
    """多设备场景：退出一处不应把另一处的会话也清掉。"""
    second_token = client.post(
        LOGIN_PATH,
        json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
    ).json()["token"]

    client.post(LOGOUT_PATH, headers=demo["headers"])

    assert_unauthorized(client.get(ME_PATH, headers=demo["headers"]))
    assert client.get(ME_PATH, headers={"Authorization": f"Bearer {second_token}"}).status_code == 200


# ---------------------------------------------------------------- 存储安全


def test_session_stores_only_digest_not_plaintext(client, demo):
    stored = [row["token_hash"] for row in fetch_session_rows()]

    assert demo["token"] not in stored
    assert token_digest(demo["token"]) in stored

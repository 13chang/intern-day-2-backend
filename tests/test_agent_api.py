"""AG-UI 端点与鉴权的测试。

覆盖真实链路：FastAPI 鉴权 → AG-UI 事件流 → Agent Framework 函数调用循环
→ 只读工具 → SQLite。测试跑在 Mock 模式（conftest 强制定死），
不产生任何真实模型调用，也不需要网络。

只测配置和 Mock 决策的用例在 `tests/test_agent_config.py`。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.deps import agent_authorization_scope
from app.security import token_digest
from tests._helpers import (
    UNAUTHORIZED_DETAIL,
    assert_unauthorized,
    auth_headers,
    insert_item,
    insert_session,
    read_sse_events,
)

RUN_BODY = {
    "threadId": "thread-1",
    "runId": "run-1",
    "messages": [{"id": "m-1", "role": "user", "content": "看看我的待办"}],
}


def collect(client: TestClient, headers: dict[str, str], body: dict | None = None) -> list[dict]:
    """发起一次 AG-UI 运行并读完整条事件流。"""
    with client.stream("POST", "/agent", headers=headers, json=body or RUN_BODY) as response:
        assert response.status_code == 200, response.read()
        return read_sse_events(response)


def event_types(events: list[dict]) -> list[str]:
    return [event.get("type", "") for event in events]


def tool_result_text(events: list[dict]) -> str:
    return str([e.get("content") for e in events if e.get("type") == "TOOL_CALL_RESULT"])


# --------------------------------------------------------------------- 鉴权


def test_agent_rejects_request_without_token(client: TestClient) -> None:
    """未携带凭证：在运行模型之前就被拒绝。"""
    assert_unauthorized(client.post("/agent", json=RUN_BODY))


@pytest.mark.parametrize("token", ["", "not-a-real-token", "0" * 43])
def test_agent_rejects_invalid_token(client: TestClient, token: str) -> None:
    assert_unauthorized(client.post("/agent", headers=auth_headers(token), json=RUN_BODY))


def test_agent_rejects_expired_session(client: TestClient, demo: dict) -> None:
    expired_token = "expired-token-for-agent-test"
    insert_session(token_digest(expired_token), demo["id"], int(time.time()) - 10)

    assert_unauthorized(
        client.post("/agent", headers=auth_headers(expired_token), json=RUN_BODY)
    )


def test_agent_info_requires_login(client: TestClient) -> None:
    assert_unauthorized(client.get("/agent/info"))


def test_agent_info_reports_mock_mode(client: TestClient, demo: dict) -> None:
    response = client.get("/agent/info", headers=demo["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "mock"
    assert body["agent"] == "activity-assistant"
    assert body["tools"] == [
        "list_my_todos",
        "get_my_activity_settings",
        "get_my_today_activities",
        "compute_suggested_slots",
    ]
    # 说明里不能出现密钥内容
    assert "key" not in response.text.lower()


# --------------------------------------------------------------- 事件流形态


def test_agent_stream_emits_agui_events(client: TestClient, demo: dict) -> None:
    """完整事件序列：开始 → 工具调用 → 工具结果 → 文本增量 → 结束。"""
    insert_item(demo["id"], "买牛奶")

    types = event_types(collect(client, demo["headers"]))

    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "TOOL_CALL_START" in types
    assert "TOOL_CALL_RESULT" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "RUN_ERROR" not in types


def test_agent_streams_text_in_multiple_deltas(client: TestClient, demo: dict) -> None:
    """文本是多条增量事件发出来的，不是一次性整段。"""
    insert_item(demo["id"], "买牛奶")

    events = collect(client, demo["headers"])
    deltas = [e.get("delta", "") for e in events if e.get("type") == "TEXT_MESSAGE_CONTENT"]

    assert len(deltas) >= 2, deltas
    assert "".join(deltas).strip(), "拼接后不应该为空"


# ----------------------------------------------------------------- 工具执行


def test_tool_runs_in_backend_with_logged_in_identity(
    client: TestClient, demo: dict, caplog
) -> None:
    """工具真实执行，查询范围由登录身份决定。"""
    insert_item(demo["id"], "写周报", done=True)

    with caplog.at_level("INFO", logger="app.agent.tools"):
        events = collect(client, demo["headers"])

    results = tool_result_text(events)
    assert "写周报" in results

    # 日志能作为「工具真的执行了」的证据，并且不带令牌
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "list_my_todos" in messages
    assert f"user_id={demo['id']}" in messages
    assert demo["token"] not in messages


def test_tool_does_not_leak_other_users_items(
    client: TestClient, demo: dict, other: dict
) -> None:
    """两个账号各自的待办互不可见。"""
    insert_item(demo["id"], "demo 的私人事儿")
    insert_item(other["id"], "other 的私人事儿")

    demo_result = tool_result_text(collect(client, demo["headers"]))
    other_result = tool_result_text(collect(client, other["headers"]))

    assert "demo 的私人事儿" in demo_result
    assert "other 的私人事儿" not in demo_result
    assert "other 的私人事儿" in other_result
    assert "demo 的私人事儿" not in other_result


def test_tool_reports_empty_list_instead_of_inventing(
    client: TestClient, demo: dict
) -> None:
    """没有待办时如实说没有，不编造条目。"""
    assert "没有" in tool_result_text(collect(client, demo["headers"]))


# ------------------------------------------------ 端点注册与作用域隔离


def test_authorization_scope_is_per_user() -> None:
    """作用域按用户区分，不同账号不会共用同一份服务端状态。"""
    assert agent_authorization_scope({"id": 1, "username": "a"}) == "agent-user:1"
    assert agent_authorization_scope({"id": 2, "username": "b"}) == "agent-user:2"


def test_agent_endpoint_is_registered(client: TestClient) -> None:
    """AG-UI 端点确实挂上了，而且是 POST，不是普通 JSON 接口。"""
    paths = client.app.openapi()["paths"]

    assert "/agent" in paths
    assert "post" in paths["/agent"]
    assert "/agent/info" in paths


def test_error_message_does_not_leak_internals(client: TestClient) -> None:
    """未登录时返回的提示保持和第二天一致，不暴露实现细节。"""
    assert client.post("/agent", json=RUN_BODY).json()["detail"] == UNAUTHORIZED_DETAIL

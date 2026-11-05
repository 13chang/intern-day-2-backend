"""Agent 配置与 Mock 客户端的单元测试。

这部分不经过 HTTP 端点，只测 `app/agent/` 内部：
- `load_llm_settings` 的模式判定与降级规则
- `MockChatClient` 的决策（什么时候请求工具、拿到工具结果后怎么回）

跟端点相关的用例在 `tests/test_agent_api.py`。
"""

from __future__ import annotations

import asyncio

import pytest
from agent_framework import Content, Message

from app.agent.config import LlmConfigError, load_llm_settings
from app.agent.mock import MOCK_BANNER, TOOL_NAME, MockChatClient


# ------------------------------------------------------------------ 配置判定


def test_settings_default_to_mock_without_credentials(monkeypatch) -> None:
    """三项模型配置都没有时，auto 模式退回 Mock，并说明原因。"""
    monkeypatch.delenv("APP_LLM_MODE", raising=False)
    for name in ("APP_LLM_BASE_URL", "APP_LLM_MODEL", "APP_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = load_llm_settings()

    assert settings.mode == "mock"
    assert settings.is_mock is True
    assert "自动退回 Mock" in settings.reason


def test_settings_use_real_model_when_fully_configured(monkeypatch) -> None:
    monkeypatch.setenv("APP_LLM_MODE", "auto")
    monkeypatch.setenv("APP_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("APP_LLM_MODEL", "demo-model")
    monkeypatch.setenv("APP_LLM_API_KEY", "sk-not-a-real-key")

    settings = load_llm_settings()

    assert settings.mode == "openai"
    assert settings.model == "demo-model"


def test_settings_refuse_silent_fallback_when_openai_is_forced(monkeypatch) -> None:
    """强制 openai 却缺配置时报错，避免"以为在用真模型"。"""
    monkeypatch.setenv("APP_LLM_MODE", "openai")
    monkeypatch.setenv("APP_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("APP_LLM_MODEL", "demo-model")
    monkeypatch.delenv("APP_LLM_API_KEY", raising=False)

    with pytest.raises(LlmConfigError):
        load_llm_settings()


def test_settings_reject_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("APP_LLM_MODE", "chatgpt")

    with pytest.raises(LlmConfigError):
        load_llm_settings()


# ------------------------------------------------------- Mock 客户的决策逻辑


def respond(messages: list[Message]):
    """调用 client 的响应方法（不经过 HTTP，也不触发工具执行循环）。"""
    client = MockChatClient()
    return asyncio.run(
        client._inner_get_response(messages=messages, stream=False, options={})
    )


def test_mock_client_requests_tool_for_todo_question() -> None:
    """问到待办时，Mock 请求调用只读工具，而不是直接编一段回答。"""
    response = respond([Message("user", [Content.from_text("我还有哪些没做完的待办？")])])

    contents = response.messages[0].contents
    calls = [c for c in contents if getattr(c, "type", None) == "function_call"]
    assert len(calls) == 1
    assert calls[0].name == TOOL_NAME
    # 问题里出现"没做完"，应当要求只看未完成
    assert calls[0].arguments == {"only_pending": True}


def test_mock_client_reports_tool_result_without_inventing() -> None:
    """拿到工具结果后，Mock 只复述真实结果，并明确标注这是 Mock。"""
    tool_result = "用户 练习用户 的未完成待办共 1 条：\n1. [未完成] 给导师发周报"
    response = respond(
        [Message("tool", [Content.from_function_result(call_id="c1", result=tool_result)])]
    )

    text = "".join(
        c.text for c in response.messages[0].contents if getattr(c, "type", None) == "text"
    )
    assert tool_result in text
    assert MOCK_BANNER.startswith("（Mock")

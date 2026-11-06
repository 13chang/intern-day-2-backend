"""Agent 配置与 Mock 客户端的单元测试。

这部分不经过 HTTP 端点，只测 `app/agent/` 内部：
- `load_llm_settings` 的模式判定与降级规则
- `MockChatClient` 的决策（什么时候请求工具、拿到工具结果后怎么回）

跟端点相关的用例在 `tests/test_agent_api.py`。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from agent_framework import Content, Message

from app.agent.config import LlmConfigError, load_llm_settings
from app.agent.factory import _mock_chunk_delay
from app.agent.mock import (
    MAX_STREAM_SECONDS,
    MOCK_BANNER,
    TOOL_NAME,
    MockChatClient,
)


# ------------------------------------------------------------------ 配置判定


@pytest.fixture(autouse=True)
def _isolate_model_env(monkeypatch):
    """每个配置用例都从干净环境开始。

    ``conftest.py`` 为了保证 app.main 导入时不连真实模型，在模块级设了
    ``MODEL_MODE=mock``。但配置测试要自己控制模式，不清掉的话这个全局值
    会盖住用例里设置的 ``APP_LLM_MODE``，让判定测试全部假通过。
    """
    for name in (
        "MODEL_MODE",
        "APP_LLM_MODE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "APP_LLM_API_KEY",
        "APP_LLM_BASE_URL",
        "APP_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


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


# ------------------------------------- MODEL_MODE 开关（对接真实模型后新增）


def _deepseek_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-chat")


def test_model_mode_real_maps_to_openai(monkeypatch) -> None:
    """MODEL_MODE=real 是给使用者看的叫法，内部对应 openai 兼容端点。"""
    monkeypatch.setenv("MODEL_MODE", "real")
    _deepseek_credentials(monkeypatch)

    settings = load_llm_settings()

    assert settings.mode == "openai"
    assert settings.is_mock is False
    assert settings.model == "deepseek-chat"
    assert "real" in settings.reason


def test_model_mode_mock_keeps_mock_even_with_credentials(monkeypatch) -> None:
    """配了密钥也照样跑 Mock —— 开关优先级高于凭证是否齐全。"""
    monkeypatch.setenv("MODEL_MODE", "mock")
    _deepseek_credentials(monkeypatch)

    settings = load_llm_settings()

    assert settings.mode == "mock"
    assert "MODEL_MODE=mock" in settings.reason


def test_model_mode_takes_priority_over_legacy_name(monkeypatch) -> None:
    """两个开关都在时以 MODEL_MODE 为准，避免 compose 和 .env 各说一套。"""
    monkeypatch.setenv("MODEL_MODE", "mock")
    monkeypatch.setenv("APP_LLM_MODE", "openai")
    _deepseek_credentials(monkeypatch)

    assert load_llm_settings().is_mock is True


def test_auto_mode_accepts_openai_prefixed_credentials(monkeypatch) -> None:
    """只写 OPENAI_* 这一套名字也能自动进入真实模型。"""
    monkeypatch.setenv("MODEL_MODE", "auto")
    _deepseek_credentials(monkeypatch)

    settings = load_llm_settings()

    assert settings.mode == "openai"
    assert settings.base_url == "https://api.deepseek.com/v1"


def test_real_mode_without_credentials_fails_fast(monkeypatch) -> None:
    """强制 real 却缺配置时直接报错，不静默降级成 Mock。"""
    monkeypatch.setenv("MODEL_MODE", "real")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-chat")

    with pytest.raises(LlmConfigError, match="API_KEY"):
        load_llm_settings()


def test_legacy_credential_names_win_when_both_are_set(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MODE", "real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-name")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-chat")
    monkeypatch.setenv("APP_LLM_MODEL", "legacy-model")

    assert load_llm_settings().model == "legacy-model"


def test_reason_never_contains_api_key(monkeypatch) -> None:
    """/agent/info 会展示 reason，不能把密钥带出去。"""
    monkeypatch.setenv("MODEL_MODE", "real")
    _deepseek_credentials(monkeypatch)

    assert "sk-test-placeholder" not in load_llm_settings().reason


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


# --------------------------------------------------- Mock 的流式输出节奏（打字机）


def stream_texts(client: MockChatClient, question: str) -> list[str]:
    """消费流式响应，按块收集文本（不经过 HTTP，也不触发工具执行循环）。"""

    async def run() -> list[str]:
        stream = await client._inner_get_response(
            messages=[Message("user", [Content.from_text(question)])],
            stream=True,
            options={},
        )
        pieces: list[str] = []
        async for update in stream:
            for content in update.contents:
                if getattr(content, "type", None) == "text":
                    pieces.append(content.text)
        return pieces

    return asyncio.run(run())


def test_mock_stream_splits_text_into_chunks() -> None:
    """文本必须分成多块下发，前端才有一批批增量可以渲染。"""
    pieces = stream_texts(MockChatClient(chunk_delay=0), "你好，随便聊聊")

    assert len(pieces) > 1
    assert "".join(pieces).startswith("（Mock")


def test_mock_stream_waits_between_chunks() -> None:
    """块与块之间要有真实等待。

    这条用例锁住的是打字效果的根因：如果哪天有人把 await 去掉，
    整段文字会在同一个事件循环 tick 里推完，浏览器只重绘一次，
    界面就退化成"一次性出现整段"。故意给一半余量，避免机器慢时误报。
    """
    client = MockChatClient(chunk_size=4, chunk_delay=0.02)

    start = time.perf_counter()
    pieces = stream_texts(client, "你好，随便聊聊")
    elapsed = time.perf_counter() - start

    assert len(pieces) > 2
    assert elapsed >= 0.02 * (len(pieces) - 1) * 0.5


def test_mock_chunk_size_is_configurable() -> None:
    """分块大小可配，且不会切出空块。"""
    pieces = stream_texts(MockChatClient(chunk_size=5, chunk_delay=0), "你好，随便聊聊")

    assert all(piece and len(piece) <= 5 for piece in pieces)


def test_long_reply_compresses_delay_to_cap() -> None:
    """长回复按比例压缩间隔，整条流不超过上限。

    工具返回几十条待办时文本可能几百字，按固定节奏要等十几秒；
    这里保证短回复照常打字、长回复也不会磨人。
    """
    client = MockChatClient(chunk_delay=0.05)

    assert client._effective_delay(40) == pytest.approx(0.05)
    assert client._effective_delay(1000) * 999 == pytest.approx(MAX_STREAM_SECONDS)
    # 只有一块时不需要等待
    assert client._effective_delay(1) == 0.0
    # 关掉节奏后永远是 0
    assert MockChatClient(chunk_delay=0)._effective_delay(1000) == 0.0


def test_chunk_delay_env_override(monkeypatch) -> None:
    """环境变量能改节奏；写错值退回默认，不影响服务可用。"""
    monkeypatch.delenv("APP_MOCK_CHUNK_DELAY", raising=False)
    assert _mock_chunk_delay() is None

    monkeypatch.setenv("APP_MOCK_CHUNK_DELAY", "0")
    assert _mock_chunk_delay() == 0.0

    monkeypatch.setenv("APP_MOCK_CHUNK_DELAY", "0.05")
    assert _mock_chunk_delay() == pytest.approx(0.05)

    monkeypatch.setenv("APP_MOCK_CHUNK_DELAY", "写得不对")
    assert _mock_chunk_delay() is None

"""没有模型凭证时用来跑通链路的假模型。

它是什么：一个实现了 ``BaseChatClient`` 的本地类，不发任何网络请求。
它不是什么：不是模型，不会"理解"问题，回复内容是按规则拼出来的。

为什么还要写它：任务说明允许先用 Mock 排查链路。有了它，
即使没有模型账号，也能验证下面这些环节是真的通的：

- AG-UI 事件流：RUN_STARTED → TOOL_CALL_START → TOOL_CALL_RESULT → TEXT_MESSAGE_CONTENT → RUN_FINISHED
- 工具确实在后端执行，结果回到 Agent 参与下一轮生成
- 工具里的身份确实来自登录会话（ContextVar），不是模型编的
- 未登录请求在进入模型前就被 401 拦住

必须继承 ``FunctionInvocationLayer``，否则 Agent 只把 function_call 事件
丢给前端、不会在服务端真正执行工具——这是实测出来的关键点。
这个 mixin 目前没有公开导出路径，只能从 ``agent_framework._tools`` 取；
升级 agent-framework 时如果这里 import 失败，说明内部结构调整了，
需要重新核对官方推荐的写法。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from agent_framework._tools import FunctionInvocationLayer

TOOL_NAME = "list_my_todos"

# 触发工具调用的关键词，只用于让演示可预期
TODO_KEYWORDS = ("待办", "任务", "清单", "没做", "还没", "todo", "todo")
PENDING_KEYWORDS = ("未完成", "没完成", "没做", "还没")

MOCK_BANNER = "（Mock 模式：未连接真实模型，以下内容由本地规则生成）"


class MockChatClient(FunctionInvocationLayer, BaseChatClient):
    """按规则应答的假模型，接口和真实 chat client 一致。"""

    def __init__(self) -> None:
        # FunctionInvocationLayer.__init__ 会接着调用 BaseChatClient.__init__
        super().__init__()

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _message_text(message: Message) -> str:
        parts: list[str] = []
        for content in message.contents:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()

    @classmethod
    def _last_user_text(cls, messages: Sequence[Message]) -> str:
        for message in reversed(messages):
            if getattr(message, "role", None) == "user":
                return cls._message_text(message)
        return ""

    @staticmethod
    def _tool_result(messages: Sequence[Message]) -> str | None:
        """如果最近一条消息带着工具结果，取出工具结果文本。"""
        if not messages:
            return None
        for content in messages[-1].contents:
            if getattr(content, "type", None) == "function_result":
                result = getattr(content, "result", None)
                if isinstance(result, str):
                    return result
                return str(result) if result is not None else ""
        return None

    @staticmethod
    def _history_summary(messages: Sequence[Message]) -> str:
        user_turns = [
            MockChatClient._message_text(m)
            for m in messages
            if getattr(m, "role", None) == "user"
        ]
        user_turns = [t for t in user_turns if t]
        if not user_turns:
            return "我还没有收到你的问题。"
        if len(user_turns) == 1:
            return f"你这次说的是：「{user_turns[-1]}」。"
        earlier = "、".join(f"「{t}」" for t in user_turns[:-1])
        return (
            f"这一轮你问的是「{user_turns[-1]}」。"
            f"本次运行我一共收到了 {len(user_turns)} 轮用户消息，之前的是 {earlier}。"
        )

    # -------------------------------------------------------------- 响应构造

    def _plan(self, messages: Sequence[Message]) -> tuple[str, str]:
        """返回 (动作, 文本)：("tool" | "text", 内容)。"""
        tool_result = self._tool_result(messages)
        if tool_result is not None:
            return "text", f"{MOCK_BANNER}\n工具在后端执行并返回了：\n{tool_result}"

        question = self._last_user_text(messages)
        lowered = question.lower()
        if any(keyword in lowered for keyword in TODO_KEYWORDS):
            return "tool", question

        return (
            "text",
            f"{MOCK_BANNER}\n{MockChatClient._history_summary(messages)}\n"
            "问「我的待办有哪些」可以看到一次真实的后端工具调用。",
        )

    @staticmethod
    def _chunks(text: str, size: int = 18) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)] or [""]

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Any,
        **kwargs: Any,
    ) -> Any:
        action, text = self._plan(messages)

        if action == "tool":
            arguments: dict[str, Any] = {}
            if any(keyword in text for keyword in PENDING_KEYWORDS):
                arguments["only_pending"] = True
            call = Content.from_function_call(
                call_id=f"mock-{uuid.uuid4().hex[:12]}",
                name=TOOL_NAME,
                arguments=arguments,
            )
            if not stream:
                return ChatResponse(
                    messages=[Message("assistant", [call])],
                    finish_reason="tool_calls",
                )

            async def one_update():
                yield ChatResponseUpdate(
                    contents=[call], role="assistant", finish_reason="tool_calls"
                )

            return ResponseStream(one_update(), finalizer=ChatResponse.from_updates)

        if not stream:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_text(text)])],
                finish_reason="stop",
            )

        pieces = self._chunks(text)

        async def text_updates():
            for index, piece in enumerate(pieces):
                yield ChatResponseUpdate(
                    contents=[Content.from_text(piece)],
                    role="assistant",
                    finish_reason="stop" if index == len(pieces) - 1 else None,
                )

        return ResponseStream(text_updates(), finalizer=ChatResponse.from_updates)

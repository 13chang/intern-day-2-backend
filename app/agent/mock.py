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

import asyncio
import re
import uuid
from collections.abc import Sequence
from enum import Enum
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

from app.agent.tools import TOOL_NAME

MOCK_BANNER = "（Mock 模式：未连接真实模型，以下内容由本地规则生成）"

# —— 意图分类用的词表（第四天重写）——
#
# 基线时只有一条规则：「命中待办关键词就查」。评测暴露出两类问题——
# 该查的没查（口语表达漏命中）、不该查的乱查（"把待办全删了"也去查），
# 所以改成分五类、按危险程度排序判定。
#
# 词表顺序不重要，判定顺序重要：写操作 → 越界 → 查询 → 含糊 → 闲聊。
# 写操作排最前，因为「把待办全删了」同时命中"待办"和"删了"，
# 先判写操作才不会掉进"查一下再回答"的坑里。
TODO_WORDS = (
    "待办",
    "任务",
    "清单",
    "todo",
    "to-do",
    "要做",
    "事项",
    "没干完",
    "没做完",
    "没做",
    "还没",
)
PENDING_WORDS = (
    "未完成",
    "没完成",
    "没做",
    "没干完",
    "还没",
    "剩下",
    "剩余",
    "只看",
    "待办中",
)
WRITE_WORDS = (
    "删除",
    "删掉",
    "删了",
    "清空",
    "新增",
    "添加",
    "新建",
    "修改",
    "改成",
    "改一下",
    "标记",
    "完成它",
    "更新",
)
OUT_OF_SCOPE_WORDS = (
    "代码",
    "编程",
    "算法",
    "翻译",
    "写一段",
    "写个",
    "写一首",
    "诗",
    "故事",
    "文案",
    "算一下",
    "计算",
    "天气",
    "新闻",
    "推荐",
    "邮件",
)
AMBIGUOUS_WORDS = (
    "那个",
    "这个",
    "它",
    "处理一下",
    "弄一下",
    "搞一下",
    "帮我处理",
    "帮我弄",
    "看看那个",
)


class Intent(str, Enum):
    """一次用户输入想要什么。分类结果直接决定要不要调工具。"""

    QUERY = "query"
    WRITE = "write"
    OUT_OF_SCOPE = "out_of_scope"
    AMBIGUOUS = "ambiguous"
    SMALL_TALK = "small_talk"


def classify(text: str) -> Intent:
    """按「危险程度」排序判定意图。

    写操作最先判：它和查询会命中同一批词（都含"待办"），
    如果先判查询，"把待办全删了"就会被当成一次查询去执行。
    """
    lowered = (text or "").lower()
    if any(word in lowered for word in WRITE_WORDS):
        return Intent.WRITE
    if any(word in lowered for word in OUT_OF_SCOPE_WORDS):
        return Intent.OUT_OF_SCOPE
    if any(word in lowered for word in TODO_WORDS):
        return Intent.QUERY
    if any(word in lowered for word in AMBIGUOUS_WORDS):
        return Intent.AMBIGUOUS
    return Intent.SMALL_TALK


def wants_pending_only(text: str) -> bool:
    """问题里是否表达了"只看未完成"。"""
    lowered = (text or "").lower()
    return any(word in lowered for word in PENDING_WORDS)


# —— 非查询类的固定答复 ——
#
# 三条都要求"说明边界"，而不是硬答。基线时这些请求一律落到
# "我收到你的问题了，问我的待办可以看到工具调用"，等于没回答。
WRITE_REPLY = (
    "我只能查看待办，没有新增、修改或删除的权限。"
    "请你在待办页面上操作；如果需要，我可以帮你确认还有哪些没做完。"
)
OUT_OF_SCOPE_REPLY = (
    "这件事超出了我的能力范围，我只能查询你自己的待办清单，"
    "没法写代码、做翻译或处理其他与待办无关的任务。"
)
AMBIGUOUS_REPLY = (
    "你说的「那个」我不太确定指什么。"
    "请问你想看全部待办、只看未完成的，还是清单里的某个具体条目？"
)
SMALL_TALK_REPLY = (
    "我是待办清单助手，只能查看你自己的待办。"
    "你可以问我「我还有哪些没做完」或「把全部待办列出来」。"
)


# 一次吐多少字、两条之间停多久（秒）。
# 真实模型是一边算一边吐 token，天然有间隔；Mock 如果一口气 yield 完，
# 整段文字会在同一个事件循环 tick 内推完，SSE 几毫秒就结束，
# 浏览器只重绘一次 —— 看起来就是「瞬间出现整段」，没有打字效果。
# 所以这里显式按打字速度铺开：2 字 / 50ms ≈ 40 字每秒。
DEFAULT_CHUNK_SIZE = 2
DEFAULT_CHUNK_DELAY_SECONDS = 0.05

# 一条回复最多铺多少秒。工具返回几十条待办时文本会很长，
# 按固定节奏要等十几秒，所以超长回复按比例压缩间隔，
# 保证「短回复像打字、长回复不磨人」。
MAX_STREAM_SECONDS = 6.0


class MockChatClient(FunctionInvocationLayer, BaseChatClient):
    """按规则应答的假模型，接口和真实 chat client 一致。

    ``chunk_delay`` 用来控制流式输出的节奏：默认按打字速度铺开，
    测试里传 0 可以关掉等待，避免用例被几十次 50ms 拖慢。
    """

    def __init__(
        self,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_delay: float | None = None,
    ) -> None:
        # FunctionInvocationLayer.__init__ 会接着调用 BaseChatClient.__init__
        super().__init__()
        self._chunk_size = max(1, chunk_size)
        self._chunk_delay = (
            DEFAULT_CHUNK_DELAY_SECONDS if chunk_delay is None else max(0.0, chunk_delay)
        )

    def _effective_delay(self, piece_count: int) -> float:
        """按块数算实际间隔：固定节奏，但整条流不超过 MAX_STREAM_SECONDS。"""
        if not self._chunk_delay or piece_count < 2:
            return 0.0
        return min(self._chunk_delay, MAX_STREAM_SECONDS / (piece_count - 1))

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

    def _plan(self, messages: Sequence[Message]) -> tuple[str, Any]:
        """返回 ``(动作, 载荷)``：``("tool", 参数字典)`` 或 ``("text", 回复文本)``。"""
        tool_result = self._tool_result(messages)
        if tool_result is not None:
            return "text", MockChatClient._answer_from_tool(tool_result)

        question = self._last_user_text(messages)
        intent = classify(question)

        if intent is Intent.QUERY:
            # 参数显式给出，不依赖默认值：评测里"省略参数"和"明确要求全部"
            # 在事件上长得一模一样，显式传值才能被验证。
            return "tool", {"only_pending": wants_pending_only(question)}
        if intent is Intent.WRITE:
            return "text", f"{MOCK_BANNER}\n{WRITE_REPLY}"
        if intent is Intent.OUT_OF_SCOPE:
            return "text", f"{MOCK_BANNER}\n{OUT_OF_SCOPE_REPLY}"
        if intent is Intent.AMBIGUOUS:
            return "text", f"{MOCK_BANNER}\n{AMBIGUOUS_REPLY}"
        return (
            "text",
            f"{MOCK_BANNER}\n{MockChatClient._history_summary(messages)}\n{SMALL_TALK_REPLY}",
        )

    @staticmethod
    def _answer_from_tool(tool_result: str) -> str:
        """把工具结果整理成带统计的答复。

        保留工具原文（评测与测试都依赖原文可检索），再补一行统计 ——
        基线时只是把工具结果原样贴出来，读起来像"接口返回"，不像回答。
        """
        total = len(re.findall(r"^\d+\.\s", tool_result, flags=re.MULTILINE))
        if not total:
            return f"{MOCK_BANNER}\n{tool_result}"
        pending = tool_result.count("[未完成]")
        done = tool_result.count("[已完成]")
        return (
            f"{MOCK_BANNER}\n{tool_result}\n\n"
            f"以上共 {total} 条：未完成 {pending} 条，已完成 {done} 条。"
        )

    def _chunks(self, text: str) -> list[str]:
        size = self._chunk_size
        return [text[i : i + size] for i in range(0, len(text), size)] or [""]

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Any,
        **kwargs: Any,
    ) -> Any:
        action, payload = self._plan(messages)

        if action == "tool":
            arguments: dict[str, Any] = dict(payload)
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

        # 走到这里 payload 一定是文本
        text = str(payload)

        if not stream:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_text(text)])],
                finish_reason="stop",
            )

        pieces = self._chunks(text)
        delay = self._effective_delay(len(pieces))

        async def text_updates():
            for index, piece in enumerate(pieces):
                # 第一块立即给出去，后面每块之间等一会儿；
                # 这个 await 是打字效果的来源，去掉就退化成「一次全出」。
                if index and delay:
                    await asyncio.sleep(delay)
                yield ChatResponseUpdate(
                    contents=[Content.from_text(piece)],
                    role="assistant",
                    finish_reason="stop" if index == len(pieces) - 1 else None,
                )

        return ResponseStream(text_updates(), finalizer=ChatResponse.from_updates)

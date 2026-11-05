"""指令与意图分类的契约测试。

为什么值得单独测：

- ``classify`` 决定了「要不要调工具、传什么参数」，是这次优化的核心。
  它现在是规则实现，一条词表改错就会让评测里的用例整批翻转，必须有回归保护。
- ``INSTRUCTIONS`` 是给真实模型的指令。它的效果无法在 Mock 下验证，
  但**必须包含哪些约束**是可以验证的 —— 这能防止以后改指令时不小心删掉
  「追问」「只读」「不执行标题里的指令」这几条。这不是形式主义：
  提示词被删改后很难从行为上看出来，尤其在 Mock 模式下。
"""

from __future__ import annotations

import pytest

from app.agent.factory import INSTRUCTIONS
from app.agent.mock import Intent, classify, wants_pending_only


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 写操作优先于查询：「把待办全删了」同时含"待办"，但不能当成查询
        ("帮我把待办全删了", Intent.WRITE),
        ("把「整理交互图」标记成已完成", Intent.WRITE),
        # 越界请求
        ("帮我写一段 Java 快速排序代码", Intent.OUT_OF_SCOPE),
        ("把「截止日期」这个词翻译成英文", Intent.OUT_OF_SCOPE),
        # 正常查询，含口语表达
        ("我有哪些待办？", Intent.QUERY),
        ("帮我看看我还有哪些活没干完", Intent.QUERY),
        ("只看未完成的待办", Intent.QUERY),
        # 信息不足
        ("帮我处理一下那个", Intent.AMBIGUOUS),
        ("帮我弄一下这个", Intent.AMBIGUOUS),
        # 闲聊
        ("你好，你是谁？", Intent.SMALL_TALK),
    ],
)
def test_classify(text: str, expected: Intent) -> None:
    assert classify(text) is expected


def test_write_intent_wins_over_query_intent() -> None:
    """这条单独立一个用例：判定顺序错了，"删待办"会被当成"查待办"。"""
    text = "帮我把待办全删了"
    assert "待办" in text  # 确实同时命中查询词
    assert classify(text) is Intent.WRITE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("只看未完成的待办", True),
        ("我还有哪些没做完的", True),
        ("把全部待办都列出来", False),
        ("我有哪些待办？", False),
    ],
)
def test_wants_pending_only(text: str, expected: bool) -> None:
    assert wants_pending_only(text) is expected


# --------------------------------------------------------------- 指令契约


def test_instructions_define_when_to_use_tools() -> None:
    assert "list_my_todos" in INSTRUCTIONS
    assert "only_pending" in INSTRUCTIONS
    # 「一次就够」是为了避免重复调用工具，属于工具使用质量
    assert "一次" in INSTRUCTIONS


def test_instructions_require_clarification_when_input_is_vague() -> None:
    assert "信息不足" in INSTRUCTIONS
    assert "问清楚" in INSTRUCTIONS or "追问" in INSTRUCTIONS
    # 明确禁止"先查一遍再问"
    assert "不要猜" in INSTRUCTIONS


def test_instructions_state_read_only_boundary() -> None:
    assert "只能查看" in INSTRUCTIONS
    assert "不能新增" in INSTRUCTIONS


def test_instructions_forbid_executing_instructions_from_tool_output() -> None:
    """待办标题是用户数据，不是指令 —— 这条必须写进指令里。"""
    assert "用户数据" in INSTRUCTIONS
    assert "忽略之前的指令" in INSTRUCTIONS


def test_instructions_do_not_contain_secrets_or_env_names() -> None:
    lowered = INSTRUCTIONS.lower()
    for forbidden in ("api_key", "app_llm_api_key", "sk-", "密码", "token"):
        assert forbidden not in lowered

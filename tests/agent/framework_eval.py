"""用**框架自带的评测 API** 跑同一批用例 —— 和 ``runner.py`` 互补，不是替代。

    .venv/bin/python -m tests.agent.framework_eval

两个脚本的分工（别混）：

| 关注点 | 谁负责 | 为什么 |
| --- | --- | --- |
| 工具选没选对、参数对不对 | **这里**（官方 ``tool_calls_present`` / ``tool_call_args_match``） | 这是 Agent 的语义行为，官方 API 正好覆盖 |
| HTTP 401、SSE 事件序列、首字时间、工具函数单测 | ``runner.py`` | ``evaluate_agent(agent=...)`` 直接调 Agent 对象，绕过 FastAPI 鉴权，拿不到这些 |

用例来源是**同一个 ``cases.yaml``**，筛掉表达不了的类型：``type: tool_unit``（不经过 Agent）、
``auth: none``（HTTP 层的事）、没有 ``expect_tool`` 的用例（这里没有可断言的语义，硬跑会给出"假通过"）。

一句话：**语义层用官方 API 验，链路层用自建 harness 验。** 两边结论应当一致，
不一致就说明有一边的断言写错了 —— 这本身就是有价值的信号。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from agent_framework import (
    CheckResult,
    ExpectedToolCall,
    LocalEvaluator,
    evaluate_agent,
    evaluator,
    tool_call_args_match,
    tool_calls_present,
)

from app.agent.factory import build_agent
from app.agent.identity import clear_current_user, set_current_user
from app.agent.tools import TOOL_NAME
from tests.agent.env import EVAL_OTHER, EVAL_USER, prepare_environment

CASES_PATH = Path(__file__).with_name("cases.yaml")
RESULTS_DIR = Path(__file__).with_name("results")

ACCOUNTS = {
    "eval-a": {"id": 1, "username": EVAL_USER["username"], "display_name": EVAL_USER["display_name"]},
    "eval-b": {"id": 2, "username": EVAL_OTHER["username"], "display_name": EVAL_OTHER["display_name"]},
}


def _called_tools(conversation: list[Any]) -> list[str]:
    """从对话里抽出所有 function_call 的工具名。

    只依赖稳定公开结构（``Content.type == "function_call"``），
    不用框架内部的下划线函数，避免升级后签名变化。
    """
    names: list[str] = []
    for message in conversation:
        for content in getattr(message, "contents", []):
            if getattr(content, "type", None) == "function_call":
                name = getattr(content, "name", None)
                if name:
                    names.append(name)
    return names


@evaluator(name="no_tool_called")
def no_tool_called(conversation: list[Any]) -> CheckResult:
    """期望"不该调工具"时用它。

    不用官方 ``tool_calls_present``：它在没有期望工具时直接返回 pass，
    于是"本该不调、结果调了"这种情况被漏掉 —— 而 NT1 / O1 / O2 验的恰恰是这个。
    """
    names = _called_tools(conversation)
    if names:
        return CheckResult(False, f"不该调用工具，实际调用了 {names}", "no_tool_called")
    return CheckResult(True, "没有调用任何工具", "no_tool_called")


def forbidden_terms(*terms: str) -> Any:
    """生成一个"回复里不能出现这些词"的检查。

    官方只给了正向断言，反向断言要靠 ``evaluator()`` 扩展 —— 这里正好演示扩展点怎么用。
    """

    def _check(response: str) -> CheckResult:
        hit = [term for term in terms if term in (response or "")]
        if hit:
            return CheckResult(False, f"回复里出现了不该出现的 {hit}", "no_forbidden_terms")
        return CheckResult(True, "没有出现禁用词", "no_forbidden_terms")

    return evaluator(_check, name="no_forbidden_terms")


def select_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛出能表达成"单轮问答 + 工具期望"的用例。"""
    selected = []
    for case in cases:
        if case.get("type") == "tool_unit":
            continue
        if case.get("auth") == "none" or "expect_status" in case:
            continue
        if "expect_tool" not in case:
            continue
        selected.append(case)
    return selected


async def run_case(agent: Any, case: dict[str, Any]) -> dict[str, Any]:
    """跑单条用例。

    一条一条调 ``evaluate_agent`` 而不是一次传所有问题：身份靠 ContextVar 传递，
    不同用例用不同账号，批量跑就没法在用例之间切身份。
    """
    set_current_user(ACCOUNTS[case.get("auth", "eval-a")])

    expect_tool = bool(case.get("expect_tool"))
    expected_calls = (
        [ExpectedToolCall(TOOL_NAME, case.get("expect_args"))] if expect_tool else None
    )

    checks: list[Any] = []
    if expect_tool:
        checks.extend([tool_calls_present, tool_call_args_match])
    else:
        checks.append(no_tool_called)
    if case.get("must_not_contain"):
        checks.append(forbidden_terms(*case["must_not_contain"]))

    results = await evaluate_agent(
        agent=agent,
        queries=case["input"],
        expected_tool_calls=expected_calls,
        evaluators=LocalEvaluator(*checks),
        eval_name=str(case["id"]),
    )

    # 只挂了一个 Local provider，所以取第一条
    result = results[0]
    reasons: list[str] = []
    for item in getattr(result, "items", []) or []:
        for score in getattr(item, "scores", []) or []:
            if getattr(score, "passed", True) is False:
                reasons.append(f"{score.name}: {(score.sample or {}).get('reason', '')}")

    passed_count = getattr(result, "passed", 0)
    total_count = getattr(result, "total", 0)
    return {
        "id": case["id"],
        "category": case.get("category", ""),
        "passed": bool(total_count and passed_count == total_count),
        "detail": f"{passed_count}/{total_count}",
        "reasons": reasons,
        "input": case["input"],
    }


async def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="用框架自带评测 API 跑用例")
    parser.add_argument(
        "--label",
        default="baseline",
        help="结果标签，如 baseline / after-fix；不区分标签会覆盖基线，踩过这个坑",
    )
    args = parser.parse_args()

    prepare_environment()

    from app.agent.config import load_llm_settings

    settings = load_llm_settings()
    mode = "mock" if settings.is_mock else "real"

    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8")) or []
    selected = select_cases(cases)

    print(f"=== 框架原生评测（evaluate_agent + LocalEvaluator）| 模式={mode} | 用例={len(selected)} 条 ===")
    print("说明：只跑能表达成单轮问答的用例；HTTP / SSE 层由 runner.py 负责\n")

    agent = build_agent(settings)
    rows = []
    for index, case in enumerate(selected, start=1):
        try:
            row = await run_case(agent, case)
        except Exception as exc:  # 单条炸掉不影响其它用例，与 runner.py 策略一致
            row = {
                "id": case["id"],
                "category": case.get("category", ""),
                "passed": False,
                "detail": "error",
                "reasons": [f"{type(exc).__name__}: {exc}"],
                "input": case["input"],
            }
        rows.append(row)
        flag = "PASS" if row["passed"] else "FAIL"
        print(f"[{index:>2}/{len(selected)}] {row['id']:<5} {flag:<5} {row['detail']:<6} {row['category']}")
        for reason in row["reasons"]:
            print(f"          └─ {reason}")

    clear_current_user()

    passed = sum(1 for row in rows if row["passed"])
    total = len(rows)
    print(f"\n通过 {passed}/{total}（{passed / total * 100:.1f}%）" if total else "\n没有可跑的用例")

    RESULTS_DIR.mkdir(exist_ok=True)
    payload = {"label": args.label, "mode": mode, "passed": passed, "total": total, "rows": rows}
    stem = f"framework-{args.label}-{mode}"
    (RESULTS_DIR / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"结果已写入 tests/agent/results/{stem}.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

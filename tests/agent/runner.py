"""评测入口：跑一批用例，落盘结果，打印摘要。

用法（在仓库根目录）：

    .venv/bin/python -m tests.agent.runner --label baseline
    .venv/bin/python -m tests.agent.runner --label after-fix
    .venv/bin/python -m tests.agent.runner --only T1        # 只跑一条，调试用
    EVAL_MODE=real .venv/bin/python -m tests.agent.runner --label baseline

结果落在 ``tests/agent/results/{label}-{mode}.json`` 和同名 ``.md``。
``mode`` 取自实际生效的模型配置，所以 Mock 与真实模型的结果不会混在一起 ——
这是任务说明「Mock 链路不写成真实模型测试通过」的落地方式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

from tests.agent.env import prepare_environment

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"


def load_cases() -> list[dict]:
    import yaml

    with (HERE / "cases.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


async def run_all(cases: list[dict], mode: str) -> list:
    # 必须在这里导入：harness 会 import app.main，而 app.main 的 Agent 端点
    # 在导入时就按环境变量构建，所以环境准备必须先完成。
    from tests.agent.checks import evaluate
    from tests.agent.harness import run_chat_case, run_tool_unit_case

    results = []
    for index, case in enumerate(cases, start=1):
        runner = run_tool_unit_case if case.get("type") == "tool_unit" else run_chat_case
        result = await runner(case, mode)
        evaluate(case, result)
        results.append(result)

        flag = "PASS" if result.passed else "FAIL"
        print(f"[{index:2d}/{len(cases)}] {case['id']:<4} {flag}  {result.total_ms:>5}ms  {case.get('category', '')}")
        if not result.passed:
            for check in result.checks:
                if not check["passed"]:
                    print(f"          └─ {check['name']}: {check['detail'][:110]}")
    return results


def summarize(results: list, cases: list[dict], mode: str) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result.passed)

    # 工具调用判断正确率：只统计明确写了 expect_tool 的用例
    suite = {case["id"]: case for case in cases}
    tool_total = 0
    tool_ok = 0
    for result in results:
        spec = suite[result.case_id]
        if "expect_tool" not in spec:
            continue
        tool_total += 1
        if bool(result.tool_calls) == spec["expect_tool"]:
            tool_ok += 1

    latencies = [result.total_ms for result in results if result.status_code == 200]
    firsts = [result.first_token_ms for result in results if result.first_token_ms is not None]

    return {
        "mode": mode,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "tool_correct": tool_ok,
        "tool_expected": tool_total,
        "latency_avg_ms": int(statistics.mean(latencies)) if latencies else None,
        "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        "first_token_avg_ms": int(statistics.mean(firsts)) if firsts else None,
    }


def to_markdown(results: list, summary: dict, label: str) -> str:
    lines = [
        f"# 评测结果 · {label} · {summary['mode']}",
        "",
        f"- 通过：**{summary['passed']}/{summary['total']}**（{summary['pass_rate']}%）",
        f"- 工具调用判断正确：**{summary['tool_correct']}/{summary['tool_expected']}**",
        f"- 总耗时均值：{summary['latency_avg_ms']} ms ｜ 中位数：{summary['latency_p50_ms']} ms",
        f"- 首字时间均值：{summary['first_token_avg_ms']} ms",
        "",
        "| 用例 | 分类 | 结果 | 工具 | 总耗时 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        tools = ",".join(call["name"] for call in result.tool_calls) or "—"
        failed = [check for check in result.checks if not check["passed"]]
        note = "全部检查通过" if not failed else "；".join(check["name"] for check in failed)[:80]
        lines.append(
            f"| {result.case_id} | {result.category} | {'PASS' if result.passed else 'FAIL'} "
            f"| {tools} | {result.total_ms} ms | {note} |"
        )
    lines.append("")
    lines.append("> 本表由 `tests/agent/runner.py` 自动生成，逐项明细见同名 JSON。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Agent 评测用例")
    parser.add_argument("--label", default="baseline", help="结果标签，如 baseline / after-fix")
    parser.add_argument("--only", default=None, help="只跑指定 id 的用例")
    args = parser.parse_args()

    prepare_environment()

    from app.agent.config import load_llm_settings

    settings = load_llm_settings()
    mode = "mock" if settings.is_mock else "real"

    cases = load_cases()
    if args.only:
        cases = [case for case in cases if case["id"] == args.only]
        if not cases:
            print(f"没有找到用例 {args.only}", file=sys.stderr)
            return 2

    model_desc = "MockChatClient" if mode == "mock" else (settings.model or "unknown")
    print(f"=== 运行 {len(cases)} 条用例 | 模式={mode} | 模型={model_desc} ===")
    print(f"    模式判定依据：{settings.reason}\n")

    results = asyncio.run(run_all(cases, mode))
    summary = summarize(results, cases, mode)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.label}-{mode}"
    payload = {"summary": summary, "results": [result.to_dict() for result in results]}
    (RESULTS_DIR / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / f"{stem}.md").write_text(to_markdown(results, summary, args.label), encoding="utf-8")

    print(f"\n通过 {summary['passed']}/{summary['total']}（{summary['pass_rate']}%）")
    print(f"工具判断正确 {summary['tool_correct']}/{summary['tool_expected']}")
    print(f"总耗时均值 {summary['latency_avg_ms']} ms，首字均值 {summary['first_token_avg_ms']} ms")
    print(f"结果已写入 tests/agent/results/{stem}.json / .md")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

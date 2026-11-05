"""链路观测的可核验脚本：跑一次真实调用，把 span 树打出来。

为什么不用 pytest：``configure_otel_providers`` 一个进程只能配置一次，
而 pytest 里 app 是模块级单例、lifespan 会被反复触发，很难在同一个进程里
既测「关闭」又测「开启」。所以观测的端到端验证放在独立进程里跑。

用法：

    .venv/bin/python -m tests.agent.trace_demo

输出：
    - stdout 上的 span 树（带耗时与关键属性）
    - tests/agent/results/trace-{mode}.json，供文档与 PR 引用

同时做三条断言，失败会以非零码退出：
    1. 一次带工具的对话至少产生 2 个 span（Agent 调用 + 工具执行）；
    2. 存在 execute_tool span，且工具名正确；
    3. span 属性里没有任何对话原文 —— 这是「观测数据可贴进 PR」的前提。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.agent.env import prepare_environment

RESULTS_DIR = Path(__file__).with_name("results")


def _tree_lines(spans: list[dict]) -> list[str]:
    """按 parent_id 画缩进树。深度用简单映射算，不追求通用图算法。"""
    depth: dict[str, int] = {}
    lines: list[str] = []
    for span in spans:
        parent = span.get("parent_id")
        level = 0 if parent is None else depth.get(parent, 0) + 1
        depth[span["span_id"]] = level
        prefix = "  " * level + ("└ " if level else "")
        attrs = span.get("attributes") or {}
        detail = ""
        if "gen_ai.tool.name" in attrs:
            detail = f" tool={attrs['gen_ai.tool.name']}"
        elif "gen_ai.agent.name" in attrs:
            detail = f" agent={attrs['gen_ai.agent.name']}"
        lines.append(f"{prefix}{span['name']}{detail}  {span['duration_ms']}ms")
    return lines


def main() -> int:
    prepare_environment()

    from fastapi.testclient import TestClient

    from app.main import app
    from app.trace_store import store

    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "eval-a", "password": "eval-a-1234"}
        ).json()["token"]

        # 走一次会触发工具的提问
        with client.stream(
            "POST",
            "/agent",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "threadId": "trace-demo",
                "runId": "trace-demo-1",
                "messages": [
                    {"id": "m-1", "role": "user", "content": "我还有哪些没做完的待办？"}
                ],
            },
        ) as response:
            event_count = sum(1 for line in response.iter_lines() if line.startswith("data:"))

    # BatchSpanProcessor 是异步导出的，读之前必须显式 flush
    from opentelemetry import trace as otel_trace

    provider = otel_trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(5000)

    trace = store.latest_trace()
    if trace is None:
        print("没有采到任何 span：可观测性可能未启用（检查 APP_TRACE_PANEL）", file=sys.stderr)
        return 1

    print(f"=== 一次运行的链路 | span {trace['span_count']} 个 | 总耗时 {trace['total_ms']} ms ===")
    print(f"    SSE 事件 {event_count} 条")
    for line in _tree_lines(trace["spans"]):
        print("   " + line)

    spans = trace["spans"]
    names = [span["name"] for span in spans]
    tool_spans = [span for span in spans if span["name"].startswith("execute_tool")]
    attrs_blob = json.dumps([span.get("attributes") for span in spans], ensure_ascii=False)

    checks = [
        ("至少 2 个 span（Agent + 工具）", len(spans) >= 2, f"实际 {len(spans)}：{names}"),
        (
            "存在 execute_tool 且工具名正确",
            bool(tool_spans)
            and tool_spans[0]["attributes"].get("gen_ai.tool.name") == "list_my_todos",
            f"实际 {[s.get('attributes') for s in tool_spans]}",
        ),
        (
            "属性里没有对话原文",
            "没做完的待办" not in attrs_blob and "待办" not in attrs_blob,
            attrs_blob[:160],
        ),
    ]

    print()
    ok = True
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + ("" if passed else f" —— {detail}"))
        ok = ok and passed

    RESULTS_DIR.mkdir(exist_ok=True)
    from app.agent.config import load_llm_settings

    mode = "mock" if load_llm_settings().is_mock else "real"
    (RESULTS_DIR / f"trace-{mode}.json").write_text(
        json.dumps({"mode": mode, "trace": trace, "checks": [
            {"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks
        ]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已写入 tests/agent/results/trace-{mode}.json")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""链路存储与 /agent/traces 接口的测试。

只测**可以确定性复现**的部分：属性白名单、环形缓冲、分组与排序、接口鉴权与结构。
「一次真实运行产生哪些 span」依赖 SDK 的埋点实现，属于端到端行为，
放在 ``tests/agent/trace_demo.py`` 里用独立进程验证 —— 那里能看到 span 树。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.trace_store import SpanRecord, TraceStore, _filter_attributes, store
from tests._helpers import assert_unauthorized


def _span(
    trace_id: str = "a" * 32,
    span_id: str = "1" * 16,
    parent_id: str | None = None,
    name: str = "invoke_agent",
    start_ns: int = 0,
    end_ns: int = 1_000_000,
    attributes: dict | None = None,
) -> SpanRecord:
    return SpanRecord(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent_id,
        start_ns=start_ns,
        end_ns=end_ns,
        attributes=attributes or {},
        status="OK",
    )


# --------------------------------------------------------------- 属性白名单


def test_attribute_filter_keeps_only_whitelisted_keys() -> None:
    """自由文本属性必须被丢掉，否则 prompt 会跟着 span 一起泄露。"""
    kept = _filter_attributes(
        {
            "gen_ai.tool.name": "list_my_todos",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.prompt": "我有哪些待办",
            "gen_ai.completion": "你有三条待办",
            "http.route": "/agent",
        }
    )

    assert kept == {
        "gen_ai.tool.name": "list_my_todos",
        "gen_ai.operation.name": "execute_tool",
        "http.route": "/agent",
    }


def test_attribute_filter_truncates_long_values() -> None:
    kept = _filter_attributes({"gen_ai.request.model": "m" * 500})
    assert len(kept["gen_ai.request.model"]) == 120


def test_attribute_filter_handles_empty() -> None:
    assert _filter_attributes(None) == {}
    assert _filter_attributes({}) == {}


# ----------------------------------------------------------------- 环形缓冲


def test_ring_buffer_drops_oldest_when_full() -> None:
    local = TraceStore(capacity=20)
    for index in range(30):
        local.add(_span(span_id=f"{index:016x}"))

    assert len(local) == 20
    # 最新一条一定在，最早的已经被挤掉
    assert local.latest_trace() is not None


def test_latest_trace_groups_spans_by_trace_id() -> None:
    local = TraceStore()
    local.add(_span(trace_id="a" * 32, span_id="1" * 16, start_ns=0, end_ns=5_000_000))
    local.add(_span(trace_id="b" * 32, span_id="2" * 16, start_ns=0, end_ns=9_000_000))
    local.add(
        _span(
            trace_id="b" * 32,
            span_id="3" * 16,
            parent_id="2" * 16,
            name="execute_tool",
            start_ns=1_000_000,
            end_ns=4_000_000,
        )
    )

    latest = local.latest_trace()

    assert latest is not None
    assert latest["trace_id"] == "b" * 32
    assert latest["span_count"] == 2
    # 总耗时取最早 start 到最晚 end：0 → 9ms
    assert latest["total_ms"] == 9
    assert [span["name"] for span in latest["spans"]] == ["invoke_agent", "execute_tool"]


def test_recent_traces_returns_newest_first() -> None:
    local = TraceStore()
    local.add(_span(trace_id="a" * 32, span_id="1" * 16, start_ns=0, end_ns=1_000_000))
    local.add(_span(trace_id="b" * 32, span_id="2" * 16, start_ns=0, end_ns=8_000_000))

    traces = local.recent_traces(limit=2)

    assert [trace["trace_id"] for trace in traces] == ["b" * 32, "a" * 32]


def test_empty_store_returns_none() -> None:
    assert TraceStore().latest_trace() is None


# ------------------------------------------------------------------- 接口


def test_traces_endpoint_requires_login(client: TestClient) -> None:
    assert_unauthorized(client.get("/agent/traces"))


def test_traces_endpoint_returns_structure(client: TestClient, demo: dict) -> None:
    """手动塞一条记录，验证接口把存储转成了约定的结构。"""
    store.clear()
    store.add(_span(attributes={"gen_ai.agent.name": "todo-assistant"}))
    store.add(
        _span(
            span_id="2" * 16,
            parent_id="1" * 16,
            name="execute_tool",
            start_ns=10_000_000,
            end_ns=12_000_000,
            attributes={"gen_ai.tool.name": "list_my_todos"},
        )
    )

    response = client.get("/agent/traces", headers=demo["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["span_total"] == 2
    assert len(body["traces"]) == 1

    trace = body["traces"][0]
    assert trace["span_count"] == 2
    assert trace["total_ms"] == 12
    assert trace["spans"][0]["name"] == "invoke_agent"
    assert trace["spans"][1]["attributes"]["gen_ai.tool.name"] == "list_my_todos"

    store.clear()


def test_traces_endpoint_reports_disabled(client: TestClient, demo: dict, monkeypatch) -> None:
    """关掉面板时要明确告诉调用方「没启用」，而不是给一个空列表让人误判成「没跑过」。"""
    monkeypatch.setenv("APP_TRACE_PANEL", "0")

    response = client.get("/agent/traces", headers=demo["headers"])

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "span_total": 0, "traces": []}

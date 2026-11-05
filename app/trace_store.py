"""链路记录的内存存储 + OpenTelemetry 出口。

为什么不用 Jaeger / Aspire：任务说明只要求「可核验的观测数据，不要求部署额外监控平台」。
所以这里把 span 收进一个环形缓冲，再用一个只读接口暴露给页面和命令行，
零外部依赖，容器里起来就有数据。

三个必须说清的设计：

1. **环形缓冲**，不是无界列表。长时间运行不能把内存吃穿，超过容量就丢最旧的。
2. **属性白名单**。即使上层没开敏感数据抓取，这里也只保留结构信息
   （操作名、工具名、耗时、token 数），绝不把 prompt / 回复原文写进来 ——
   观测数据经常被贴进 PR 和截图，这一步是最后一道闸。
3. **按 trace 分组**。一次 Agent 运行 = 一个 trace，里面的 span 有先后和父子关系，
   平铺的列表看不出"慢在哪一步"，所以对外按 trace 组织并算出总耗时。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

# 只保留这些属性。前缀匹配用元组，避免把 prompt 之类的自由文本带出来。
ALLOWED_ATTRIBUTE_PREFIXES = (
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "gen_ai.request.model",
    "gen_ai.usage.",
    "gen_ai.agent.name",
    "http.route",
    "http.method",
    "http.status_code",
)

MAX_ATTRIBUTE_VALUE_LEN = 120


@dataclass
class SpanRecord:
    """一条 span 的去敏快照。"""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "UNSET"

    @property
    def duration_ms(self) -> int:
        if not self.end_ns:
            return 0
        return int((self.end_ns - self.start_ns) / 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
            "status": self.status,
        }


def _filter_attributes(raw: Any) -> dict[str, Any]:
    """按白名单裁剪属性，并截断过长取值。"""
    kept: dict[str, Any] = {}
    if not raw:
        return kept
    for key, value in dict(raw).items():
        if not key.startswith(ALLOWED_ATTRIBUTE_PREFIXES):
            continue
        if isinstance(value, str):
            kept[key] = value[:MAX_ATTRIBUTE_VALUE_LEN]
        elif isinstance(value, (int, float, bool)):
            kept[key] = value
        else:
            kept[key] = str(value)[:MAX_ATTRIBUTE_VALUE_LEN]
    return kept


class TraceStore:
    """按 trace 分组的环形缓冲。

    线程安全：导出由 SDK 的后台线程调用，读接口由请求线程调用，所以都加锁。
    """

    def __init__(self, capacity: int = 400) -> None:
        self._capacity = max(20, capacity)
        self._records: deque[SpanRecord] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()

    def add(self, record: SpanRecord) -> None:
        with self._lock:
            self._records.append(record)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def _grouped(self) -> dict[str, list[SpanRecord]]:
        grouped: dict[str, list[SpanRecord]] = {}
        for record in self._records:
            grouped.setdefault(record.trace_id, []).append(record)
        return grouped

    def latest_trace(self) -> dict[str, Any] | None:
        """返回最近一次运行（最后一个 span 所属的 trace）的结构。"""
        with self._lock:
            if not self._records:
                return None
            trace_id = self._records[-1].trace_id
            grouped = self._grouped()

        spans = grouped.get(trace_id, [])
        if not spans:
            return None
        return self._as_trace(trace_id, spans)

    def recent_traces(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近的若干次运行，新的在前。"""
        with self._lock:
            grouped = self._grouped()

        traces = [self._as_trace(trace_id, spans) for trace_id, spans in grouped.items()]
        # dict 保留插入顺序，这里按最后一条 span 的时间倒序更直观
        traces.sort(key=lambda item: item["finished_ns"], reverse=True)
        return traces[: max(1, limit)]

    @staticmethod
    def _as_trace(trace_id: str, spans: list[SpanRecord]) -> dict[str, Any]:
        ordered = sorted(spans, key=lambda item: item.start_ns)
        start = min(span.start_ns for span in ordered)
        end = max(span.end_ns or span.start_ns for span in ordered)
        return {
            "trace_id": trace_id,
            "span_count": len(ordered),
            "started_ms": 0,
            "total_ms": int((end - start) / 1_000_000) if end >= start else 0,
            "finished_ns": end,
            "spans": [span.to_dict() for span in ordered],
        }


class TraceStoreExporter(SpanExporter):
    """把 SDK 产出的 span 收进 TraceStore。

    注意这里不做任何判断，只搬运 —— 是否启用、是否开控制台出口，
    由 ``app.observability`` 决定，保持职责单一。
    """

    def __init__(self, store: TraceStore) -> None:
        self._store = store

    def export(self, spans: Any) -> SpanExportResult:
        for span in spans:
            context = getattr(span, "context", None)
            parent = getattr(span, "parent", None)
            status = getattr(span, "status", None)
            self._store.add(
                SpanRecord(
                    name=getattr(span, "name", "unknown"),
                    trace_id=f"{context.trace_id:032x}" if context else "0" * 32,
                    span_id=f"{context.span_id:016x}" if context else "0" * 16,
                    parent_id=f"{parent.span_id:016x}" if parent else None,
                    start_ns=getattr(span, "start_time", 0) or 0,
                    end_ns=getattr(span, "end_time", 0) or 0,
                    attributes=_filter_attributes(getattr(span, "attributes", None)),
                    status=getattr(status, "status_code", "UNSET").name
                    if hasattr(getattr(status, "status_code", None), "name")
                    else "UNSET",
                )
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - 进程退出时由 SDK 调用
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True


# 进程内单例：导出器和读接口必须是同一个对象
store = TraceStore()


def reset_store() -> None:
    """测试用：清空已有记录。"""
    store.clear()


def now_ms() -> int:
    """给调用方算相对时间用的便捷函数。"""
    return int(time.time() * 1000)

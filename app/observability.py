"""可观测性（OpenTelemetry）：让"走了哪几步、工具调了几次、慢在哪"变成数据。

为什么单独一个模块：观测是**横切关注点**，不该混进业务代码。这里只做一件事——
在进程启动时决定「要不要采、采了往哪送」，业务代码零改动。

三层东西要分清（很容易混）：

1. **埋点（Instrumentation）**：Agent Framework 自带。Agent 的 MRO 里有 ``AgentTelemetryLayer``，
   模型客户端的 MRO 里有 ``ChatTelemetryLayer``，会按 GenAI 语义约定产出 span。**默认就是开着的。**
2. **Provider（SDK 侧）**：决定 span 交给谁。不配置 Provider 时，埋点产出的 span 直接丢弃 ——
   这就是「代码有埋点却什么都看不到」的原因。
3. **Exporter（出口）**：内存 / Console / OTLP。默认什么都不配。

所以这个模块真正做的是第 2、3 层。

本项目的两个出口：

- ``trace-panel``（默认开）：span 进内存环形缓冲，``GET /agent/traces`` 给页面画时间线。
- ``console``（``APP_OTEL_CONSOLE=1`` 时开）：span 打到 stdout，命令行排查用。

安全边界：``enable_sensitive_data=False``，不把 prompt / 回复原文写进 span。
出口那边还有一层属性白名单（见 ``trace_store.py``），双保险 —— 观测数据经常被贴进
PR 和截图，这一步不能只靠一个开关。
"""

from __future__ import annotations

import logging
import os

from app.trace_store import TraceStoreExporter, store

logger = logging.getLogger(__name__)

SERVICE_NAME = "intern-day-2-backend-agent"

# ``configure_otel_providers`` 一个进程只能调一次，重复调用会覆盖全局 Provider。
# FastAPI 的 lifespan 在测试里会被多次触发（每个 TestClient 一次），所以这里做幂等。
_initialized = False


def _flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


def trace_panel_enabled() -> bool:
    # 面板默认开：容器里起来就有数据，不需要额外配置
    return (os.environ.get("APP_TRACE_PANEL", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def buffer_size() -> int:
    raw = os.environ.get("APP_TRACE_BUFFER_SIZE", "").strip()
    try:
        return max(0, int(raw)) if raw else 400
    except ValueError:
        return 400


def setup_observability(*, exporters: list[object] | None = None) -> bool:
    """按配置初始化 OpenTelemetry，返回是否真的启用了。

    四种用法：

    - 全关（``APP_TRACE_PANEL=0`` 且没开 console）：什么都不做，零开销；
    - 默认（面板开）：只挂内存出口，供 ``/agent/traces`` 读取；
    - ``APP_OTEL_CONSOLE=1``：再加一个控制台出口，跑一次对话就能在 stdout 看到 span 树；
    - 传 ``exporters=[...]``：由调用方决定出口（``tests/agent/trace_demo.py`` 用它做断言）。
    """
    global _initialized
    if _initialized and exporters is None:
        return True

    collected: list[object] = []
    panel_on = trace_panel_enabled() and buffer_size() > 0
    if panel_on:
        collected.append(TraceStoreExporter(store))

    console_on = exporters is None and _flag("APP_OTEL_CONSOLE")
    if exporters is not None:
        collected.extend(exporters)
    elif not console_on and not collected:
        logger.info("可观测性未开启：埋点存在但不导出（span 直接丢弃）")
        _initialized = True
        return False

    try:
        from agent_framework.observability import configure_otel_providers
    except ImportError:  # pragma: no cover - 只有装错依赖才会走到
        logger.warning("agent_framework.observability 不可用，跳过可观测性初始化")
        _initialized = True
        return False

    try:
        configure_otel_providers(
            service_name=os.environ.get("APP_OTEL_SERVICE_NAME", "").strip() or SERVICE_NAME,
            service_version="0.3.0",
            enable_console_exporters=console_on,
            exporters=collected,
            # 安全：绝不把 prompt / 回复原文写进 span
            enable_sensitive_data=False,
        )
    except Exception as exc:  # pragma: no cover - 观测失败不能拖垮业务
        # 有意的取舍：宁可没有 trace，也不能让服务起不来
        logger.warning("可观测性初始化失败（服务继续运行）：%s", exc)
        _initialized = True
        return False

    _initialized = True
    logger.info(
        "可观测性已启用：service=%s 出口=%s（敏感数据抓取已关闭）",
        os.environ.get("APP_OTEL_SERVICE_NAME", "").strip() or SERVICE_NAME,
        "+".join(
            name
            for name, enabled in (
                ("trace-panel", panel_on),
                ("console", console_on),
                ("caller", bool(exporters)),
            )
            if enabled
        )
        or "none",
    )
    return True

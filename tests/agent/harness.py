"""评测执行器：把一条用例跑成一份结构化结果。

设计取舍（三条，都会影响结论可信度）：

1. **不启端口**。用 httpx 的 ``ASGITransport`` 直接把请求喂给 FastAPI 应用，
   不受端口占用、端口转发、代理、容器状态影响，换机器也能复现。
2. **只走 HTTP 接口**，不直接调 Agent 对象。被测的正是"对外表现"：
   鉴权、SSE 编码、事件顺序、首字时间。绕过 HTTP 就漏掉了这些。
3. 时间统计取两个点：首个 ``TEXT_MESSAGE_CONTENT``（用户感知的"开始出字"）
   和整个运行结束（总耗时）。工具单元用例单独计时。

工具层用例（``type: tool_unit``）是例外：它绕过模型直接调工具函数，
因为「模型不可能稳定地生成非法参数」，但后端必须自己挡住 —— 这是最小权限的最后一道防线。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.identity import clear_current_user, set_current_user
from app.main import app
from tests.agent.env import EVAL_OTHER, EVAL_USER

CREDENTIALS = {
    "eval-a": (EVAL_USER["username"], EVAL_USER["password"]),
    "eval-b": (EVAL_OTHER["username"], EVAL_OTHER["password"]),
}


@dataclass
class CaseResult:
    """一条用例的运行结果。字段分两半：观察到的、判定出的。"""

    case_id: str
    category: str
    input_text: str
    mode: str
    # —— 实际观察到的 ——
    status_code: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_result: str = ""
    final_text: str = ""
    error_event: dict[str, Any] | None = None
    event_types: list[str] = field(default_factory=list)
    total_ms: int = 0
    first_token_ms: int | None = None
    tool_unit: list[dict[str, Any]] = field(default_factory=list)
    # —— 判定 ——
    passed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "input": self.input_text,
            "mode": self.mode,
            "status_code": self.status_code,
            "passed": self.passed,
            "tool_called": [c["name"] for c in self.tool_calls],
            "tool_args": [c["arguments"] for c in self.tool_calls],
            "tool_result": self.tool_result,
            "final_text": self.final_text,
            "error_event": self.error_event,
            "total_ms": self.total_ms,
            "first_token_ms": self.first_token_ms,
            "checks": self.checks,
            "tool_unit": self.tool_unit,
        }


async def _login(client: httpx.AsyncClient, account: str) -> str:
    username, password = CREDENTIALS[account]
    response = await client.post("/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    return response.json()["token"]


def _run_input(text: str) -> dict[str, Any]:
    """构造 AG-UI 的 RunAgentInput，字段名与前端实际发送的一致（camelCase）。"""
    return {
        "threadId": f"eval-{uuid.uuid4().hex[:10]}",
        "runId": f"run-{uuid.uuid4().hex[:10]}",
        "messages": [{"id": f"m-{uuid.uuid4().hex[:8]}", "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }


async def run_chat_case(case: dict[str, Any], mode: str) -> CaseResult:
    """跑一条对话类用例：登录 → 发消息 → 收集 SSE 事件。"""
    result = CaseResult(
        case_id=case["id"],
        category=case.get("category", ""),
        input_text=case.get("input", ""),
        mode=mode,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://eval.local", timeout=120
    ) as client:
        auth = case.get("auth", "eval-a")
        headers: dict[str, str] = {}
        if auth != "none":
            headers["Authorization"] = f"Bearer {await _login(client, auth)}"

        started = time.perf_counter()
        async with client.stream(
            "POST", "/agent", json=_run_input(case["input"]), headers=headers
        ) as response:
            result.status_code = response.status_code

            # 401 不是 SSE，读者只需把正文收下来
            if response.status_code != 200:
                body = await response.aread()
                result.final_text = body.decode("utf-8", "replace")
                result.total_ms = int((time.perf_counter() - started) * 1000)
                return result

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue

                event = json.loads(payload)
                etype = event.get("type", "")
                result.event_types.append(etype)

                if etype == "TEXT_MESSAGE_CONTENT":
                    if result.first_token_ms is None:
                        result.first_token_ms = int((time.perf_counter() - started) * 1000)
                    result.final_text += event.get("delta", "")
                elif etype == "TOOL_CALL_START":
                    result.tool_calls.append(
                        {
                            "name": event.get("toolCallName", ""),
                            "arguments": {},
                            "id": event.get("toolCallId", ""),
                        }
                    )
                elif etype == "TOOL_CALL_ARGS":
                    if result.tool_calls:
                        # 参数是分片下发的，先拼起来，等全部到齐再解析
                        current = result.tool_calls[-1]
                        current["raw"] = current.get("raw", "") + event.get("delta", "")
                elif etype == "TOOL_CALL_RESULT":
                    result.tool_result = str(event.get("content", ""))
                elif etype == "RUN_ERROR":
                    result.error_event = event

    result.total_ms = int((time.perf_counter() - started) * 1000)

    for call in result.tool_calls:
        raw = call.pop("raw", "") or "{}"
        try:
            call["arguments"] = json.loads(raw)
        except json.JSONDecodeError:
            call["arguments"] = {"__unparsable__": raw}

    return result


async def run_tool_unit_case(case: dict[str, Any], mode: str) -> CaseResult:
    """跑一条工具层用例：直接调用工具函数，验证参数校验与异常处理。"""
    from app.agent.tools import list_my_todos

    result = CaseResult(
        case_id=case["id"],
        category=case.get("category", ""),
        input_text=case.get("notes", ""),
        mode=mode,
    )

    started = time.perf_counter()
    for item in case.get("tool_calls", []):
        record: dict[str, Any] = {"name": item.get("name", ""), "args": item.get("args", {})}

        if item.get("without_auth"):
            clear_current_user()
        else:
            set_current_user({"id": 1, "username": "eval-a", "display_name": "评测用户 A"})

        try:
            output = list_my_todos(**item.get("args", {}))
            record["ok"] = True
            record["output"] = str(output)
        except Exception as exc:  # noqa: BLE001 - 这里就是要观察异常类型
            record["ok"] = False
            record["raised"] = type(exc).__name__
            record["output"] = f"{type(exc).__name__}: {exc}"

        result.tool_unit.append(record)

    clear_current_user()
    result.total_ms = int((time.perf_counter() - started) * 1000)
    return result

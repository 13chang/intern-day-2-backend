"""模型访问配置：一个开关决定用 Mock 还是真实模型。

密钥只在这里从环境变量读出来，并且只在后端进程内使用：
- 不写进代码、不写进镜像构建参数、不写进 .env.example
- 不通过任何接口返回给前端
- 前端只认识自己的登录令牌

## 开关（推荐用第一套，简洁）

``MODEL_MODE``：

- ``real``  调用真实模型（DeepSeek / 任何 OpenAI 兼容端点）
- ``mock``  本地假模型，不联网、零成本，Agent 循环与工具执行仍是真的
- ``auto``  三项凭证都配了就走真实模型，缺任意一项退回 Mock（默认）

为了兼容第三天已经写好的文档与测试，``APP_LLM_MODE`` 仍可用：
``mock`` / ``openai`` / ``auto``。两个都设置时以 ``MODEL_MODE`` 为准。

## 凭证（两套名字等价）

| 推荐（OpenAI 兼容习惯） | 第三天原有 | 含义 |
| --- | --- | --- |
| ``OPENAI_API_KEY`` | ``APP_LLM_API_KEY`` | 密钥 |
| ``OPENAI_BASE_URL`` | ``APP_LLM_BASE_URL`` | 服务地址，DeepSeek 是 ``https://api.deepseek.com/v1`` |
| ``OPENAI_MODEL`` | ``APP_LLM_MODEL`` | 模型名，例如 ``deepseek-chat`` |

同名的 ``APP_LLM_*`` 优先。强制 ``real`` 但缺配置时**直接启动失败**，
避免"以为连了真模型其实在跑 Mock"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["openai", "mock"]
RequestedMode = Literal["auto", "openai", "mock"]

# 外部写法 → 内部模式。real 与 openai 是同一个意思。
MODE_ALIASES: dict[str, str] = {
    "mock": "mock",
    "real": "openai",
    "openai": "openai",
    "auto": "auto",
}
VALID_MODES: tuple[str, ...] = tuple(MODE_ALIASES)


class LlmConfigError(RuntimeError):
    """模式写错，或强制真实模型却缺配置。两种情况都让启动直接失败。"""


@dataclass(frozen=True)
class LlmSettings:
    mode: Mode
    base_url: str | None
    model: str | None
    api_key: str | None
    # 为什么落到当前模式：写进启动日志和 GET /agent/info，演示时说得清
    reason: str

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"


def _first(*names: str) -> tuple[str | None, str | None]:
    """按优先级取第一个非空环境变量，返回值和它来自哪个名字。"""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return None, None


def _requested_mode() -> tuple[str, str]:
    """解析运行模式。返回 ``(内部模式, 来源变量名)``。"""
    raw, source = _first("MODEL_MODE", "APP_LLM_MODE")
    if raw is None:
        return "auto", "默认值"
    lowered = raw.lower()
    if lowered not in MODE_ALIASES:
        raise LlmConfigError(
            "MODEL_MODE / APP_LLM_MODE 只能是 mock、real（=openai）、auto，"
            f"当前 {source}={raw!r}"
        )
    return MODE_ALIASES[lowered], source or "默认值"


def load_llm_settings() -> LlmSettings:
    requested, source = _requested_mode()

    base_url, _ = _first("APP_LLM_BASE_URL", "OPENAI_BASE_URL")
    model, _ = _first("APP_LLM_MODEL", "OPENAI_MODEL")
    api_key, _ = _first("APP_LLM_API_KEY", "OPENAI_API_KEY")

    missing = [
        name
        for name, value in (
            ("OPENAI_BASE_URL / APP_LLM_BASE_URL", base_url),
            ("OPENAI_MODEL / APP_LLM_MODEL", model),
            ("OPENAI_API_KEY / APP_LLM_API_KEY", api_key),
        )
        if value is None
    ]

    if requested == "mock":
        return LlmSettings("mock", base_url, model, api_key, f"{source}=mock，强制本地假模型")

    if not missing:
        # reason 里不写密钥，只写模型名和端点，方便 /agent/info 如实展示
        return LlmSettings("openai", base_url, model, api_key, f"{source}=real，凭证已配置")

    if requested == "openai":
        raise LlmConfigError(
            f"{source}=real 但缺少 " + "、".join(missing) + "，拒绝以降级方式启动"
        )

    return LlmSettings(
        "mock",
        base_url,
        model,
        api_key,
        "未配置 " + "、".join(missing) + "，自动退回 Mock",
    )

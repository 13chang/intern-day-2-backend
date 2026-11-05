"""模型访问配置。

密钥只在这里从环境变量读出来，并且只在后端进程内使用：
- 不写进代码、不写进镜像构建参数
- 不通过任何接口返回给前端
- 前端只认识自己的登录令牌

``APP_LLM_MODE`` 的取值：

- ``auto``（默认）  配了 base_url + model + api_key 就用真实模型，否则退回 Mock
- ``openai``        强制走真实模型，缺配置直接抛错，避免"以为连了模型其实是 Mock"
- ``mock``          强制用假模型，只用来排查链路
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["openai", "mock"]
RequestedMode = Literal["auto", "openai", "mock"]

VALID_MODES: tuple[str, ...] = ("auto", "openai", "mock")


class LlmConfigError(RuntimeError):
    """配置不完整。强制 openai 模式时会抛出，让启动直接失败。"""


@dataclass(frozen=True)
class LlmSettings:
    mode: Mode
    base_url: str | None
    model: str | None
    api_key: str | None
    # 记录为什么落到当前模式，写进日志和 README，避免演示时说不清
    reason: str

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"


def _clean(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_llm_settings() -> LlmSettings:
    requested = (os.environ.get("APP_LLM_MODE", "auto") or "auto").strip().lower()
    if requested not in VALID_MODES:
        raise LlmConfigError(
            f"APP_LLM_MODE 只能是 {'/'.join(VALID_MODES)}，当前为 {requested!r}"
        )

    base_url = _clean("APP_LLM_BASE_URL")
    model = _clean("APP_LLM_MODEL")
    api_key = _clean("APP_LLM_API_KEY")

    missing = [
        name
        for name, value in (
            ("APP_LLM_BASE_URL", base_url),
            ("APP_LLM_MODEL", model),
            ("APP_LLM_API_KEY", api_key),
        )
        if value is None
    ]

    if requested == "mock":
        return LlmSettings("mock", base_url, model, api_key, "APP_LLM_MODE=mock")

    if not missing:
        return LlmSettings("openai", base_url, model, api_key, "环境变量已配置")

    if requested == "openai":
        raise LlmConfigError(
            "APP_LLM_MODE=openai 但缺少 " + "、".join(missing) + "，拒绝以降级方式启动"
        )

    return LlmSettings(
        "mock",
        base_url,
        model,
        api_key,
        "未配置 " + "、".join(missing) + "，自动退回 Mock",
    )

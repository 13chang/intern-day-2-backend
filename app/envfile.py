"""读取仓库根目录的 ``.env``，补进进程环境。

为什么需要这一层：``.env`` 原本只被 ``docker compose`` 消费
（compose.yaml 里写的是 ``${MODEL_MODE:-mock}``）。但开发时更常见的是在
devcontainer 里直接 ``uv run uvicorn`` —— 那条路径没有任何东西读 ``.env``，
于是 ``MODEL_MODE=real`` 明明写在文件里，进程却拿不到，``load_llm_settings()``
走 ``auto`` 分支发现没凭证，安静地退回 Mock。表现就是页面上"真实模型未启用"，
而改 ``.env`` 怎么改都没反应。

两条约束：

- **真实环境变量优先**。compose / 容器 / CI 注入的值必须压过文件里的值，
  否则"容器里传了 mock 却被 .env 改回 real"这类事故很容易发生。
- **不引入 python-dotenv**。格式只支持最常见的那种（``KEY=VALUE``、
  ``#`` 注释、可选的 ``export`` 前缀、成对引号），够用且没有新依赖。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 只做键名合法性判断：值可以是任意文本（含 = 号、空格），不做白名单，
# 否则每加一个配置项都要回来改这里。
def _valid_key(key: str) -> bool:
    return bool(key) and (key[0].isalpha() or key[0] == "_") and all(
        char.isalnum() or char == "_" for char in key
    )


def _candidates() -> list[Path]:
    """按优先级给出可能的 .env 位置。

    先看 ``APP_ENV_FILE``（显式指定，测试和特殊部署用），再看当前工作目录 ——
    在仓库里执行命令时就是它；最后从本文件往上找，兜住"从别的目录启动"的情况。
    """
    paths: list[Path] = []
    explicit = os.environ.get("APP_ENV_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path.cwd() / ".env")
    for parent in Path(__file__).resolve().parents:
        paths.append(parent / ".env")
    return paths


def load_env_file(path: Path | None = None) -> list[str]:
    """把 ``.env`` 补进 ``os.environ``，返回真正补进去的键名（不含值）。

    返回键名而不是值，是为了能在日志里说清"读了哪些配置"而不泄露密钥。
    文件不存在不算错误 —— 生产环境通常直接注入变量，没有 .env 是正常的。

    设了 ``APP_SKIP_ENV_FILE=1`` 就跳过**自动查找**：测试必须和开发机上的 ``.env``
    无关，否则"本机配了 DeepSeek 就能过、换台机器就红"这类假失败会没完没了。
    显式传 ``path`` 时不受这个开关影响 —— 那是调用方明确指定要读的文件。
    """
    if path is None:
        if os.environ.get("APP_SKIP_ENV_FILE", "").strip().lower() in ("1", "true", "yes"):
            return []
    targets = [path] if path is not None else _candidates()
    for target in targets:
        try:
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
        except OSError as exc:  # 权限问题不该让整个进程起不来
            logger.warning("读取 %s 失败：%s", target, type(exc).__name__)
            continue

        loaded: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not _valid_key(key):
                continue
            # 环境里已经有非空值就不动：真实注入优先
            if os.environ.get(key, "").strip():
                continue
            os.environ[key] = value.strip().strip('"').strip("'")
            loaded.append(key)

        if loaded:
            logger.info("已从 %s 补充配置：%s", target, ", ".join(sorted(loaded)))
        return loaded
    return []

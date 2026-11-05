"""pytest 共用夹具。

关键设计：每个用例都用独立的临时 SQLite 文件（tmp_path/test.db），
通过 APP_DB_PATH 指向它，因此：
  - 用例之间互不污染，可以任意顺序执行；
  - 永远不会碰到仓库里的 data/app.db；
  - 建表由应用自身的 lifespan 完成，测的是真实启动路径。
"""

from __future__ import annotations

import os

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

# Agent 端点在 app 导入时就会构建，所以必须在导入 app.main 之前定死模式：
# 即使开发机环境里配了真实模型密钥，测试也只用 Mock，不产生任何模型调用费用。
os.environ["APP_LLM_MODE"] = "mock"

# Mock 默认按打字速度每 6 字停 25ms；测试不需要看打字效果，
# 这里关掉等待，避免十几个流式用例各自多等一秒多。
# 打字节奏本身由 test_agent_config.py 里显式传 chunk_delay 的用例覆盖。
os.environ["APP_MOCK_CHUNK_DELAY"] = "0"

from app.main import app  # noqa: E402  (必须在设置环境变量之后导入)
from tests._helpers import (
    DEMO_DISPLAY_NAME,
    DEMO_PASSWORD,
    DEMO_USERNAME,
    OTHER_DISPLAY_NAME,
    OTHER_PASSWORD,
    OTHER_USERNAME,
    auth_headers,
    create_user,
    login,
)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    """指向临时数据库的测试客户端。

    用 with 进入上下文，才能触发 FastAPI 的 lifespan（即 init_db 建表）。
    """
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def demo(client: TestClient) -> dict[str, Any]:
    """默认练习账号 demo，已登录。"""
    user_id = create_user(DEMO_USERNAME, DEMO_PASSWORD, DEMO_DISPLAY_NAME)
    token = login(client, DEMO_USERNAME, DEMO_PASSWORD)
    return {
        "id": user_id,
        "username": DEMO_USERNAME,
        "password": DEMO_PASSWORD,
        "token": token,
        "headers": auth_headers(token),
    }


@pytest.fixture()
def other(client: TestClient) -> dict[str, Any]:
    """第二个练习账号，用于验证用户之间的数据隔离。"""
    user_id = create_user(OTHER_USERNAME, OTHER_PASSWORD, OTHER_DISPLAY_NAME)
    token = login(client, OTHER_USERNAME, OTHER_PASSWORD)
    return {
        "id": user_id,
        "username": OTHER_USERNAME,
        "password": OTHER_PASSWORD,
        "token": token,
        "headers": auth_headers(token),
    }

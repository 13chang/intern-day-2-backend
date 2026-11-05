"""健康检查接口。

任务书要求 /health 不需要登录即可访问，用于容器探活。
"""

from __future__ import annotations


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_require_login(client):
    """不带任何凭证也必须可访问，否则容器探活会失败。"""
    response = client.get("/health", headers={})

    assert response.status_code == 200

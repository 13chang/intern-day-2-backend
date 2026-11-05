"""CORS 配置。

浏览器直接访问宿主机映射的后端端口，因此必须放行前端来源并正确处理预检请求。
这两个用例覆盖「放行合法来源」和「不放行其他来源」。
"""

from __future__ import annotations

FRONTEND_ORIGIN = "http://localhost:5173"
PREFLIGHT_HEADERS = {
    "Origin": FRONTEND_ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type",
}


def test_preflight_from_frontend_origin_is_allowed(client):
    response = client.options("/items", headers=PREFLIGHT_HEADERS)

    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == FRONTEND_ORIGIN
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()


def test_preflight_from_unknown_origin_is_not_allowed(client):
    response = client.options(
        "/items",
        headers={**PREFLIGHT_HEADERS, "Origin": "http://evil.example"},
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_simple_request_carries_allow_origin(client):
    response = client.get("/health", headers={"Origin": FRONTEND_ORIGIN})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == FRONTEND_ORIGIN

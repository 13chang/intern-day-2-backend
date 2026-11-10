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


# Capacitor 打包成 App 之后，WebView 里页面的来源是 http://localhost（不带端口）。
# 少了这一条，App 里所有请求都会在预检阶段被拦掉。
NATIVE_APP_ORIGIN = "http://localhost"


def test_preflight_from_native_app_origin_is_allowed(client):
    response = client.options(
        "/items",
        headers={**PREFLIGHT_HEADERS, "Origin": NATIVE_APP_ORIGIN},
    )

    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == NATIVE_APP_ORIGIN


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


# 真正踩过的坑不在默认值，而在 .env：
# 每台机器的 APP_CORS_ORIGINS 都不一样（端口不同、要联调局域网 IP），
# 一旦配了这个变量，默认值就整串被替换，native origin 顺带丢掉，
# App 里的请求又全部卡在预检。所以必须单独验证「配置覆盖后仍然放行」，
# 只测默认场景是测不出这个回归的。
def test_native_app_origin_survives_env_override(monkeypatch):
    from app.main import cors_origins

    monkeypatch.setenv("APP_CORS_ORIGINS", FRONTEND_ORIGIN)
    origins = cors_origins()

    assert FRONTEND_ORIGIN in origins
    assert NATIVE_APP_ORIGIN in origins

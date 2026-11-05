"""自选功能：待办清单接口。

覆盖任务书要求的两条核心约束：
  - 所有接口必须校验登录状态，未登录一律 401；
  - 数据按登录用户隔离，不能通过 id 访问别人的记录。
"""

from __future__ import annotations

from tests._helpers import assert_unauthorized, fetch_item_rows

ITEMS_PATH = "/items"


def _create(client, headers, title: str) -> dict:
    response = client.post(ITEMS_PATH, json={"title": title}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- 登录保护


def test_all_item_endpoints_require_login(client):
    assert_unauthorized(client.get(ITEMS_PATH))
    assert_unauthorized(client.post(ITEMS_PATH, json={"title": "测试"}))
    assert_unauthorized(client.patch(f"{ITEMS_PATH}/1", json={"done": True}))
    assert_unauthorized(client.delete(f"{ITEMS_PATH}/1"))


def test_forged_token_is_rejected(client):
    headers = {"Authorization": "Bearer forged-token"}

    assert_unauthorized(client.get(ITEMS_PATH, headers=headers))
    assert_unauthorized(client.post(ITEMS_PATH, json={"title": "测试"}, headers=headers))


def test_items_rejected_after_logout(client, demo):
    _create(client, demo["headers"], "买菜")
    client.post("/auth/logout", headers=demo["headers"])

    assert_unauthorized(client.get(ITEMS_PATH, headers=demo["headers"]))


# ---------------------------------------------------------------- 新增


def test_create_item_returns_201(client, demo):
    response = client.post(ITEMS_PATH, json={"title": "买菜"}, headers=demo["headers"])

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "买菜"
    assert body["done"] is False
    assert isinstance(body["id"], int)
    assert isinstance(body["created_at"], int)

    rows = fetch_item_rows()
    assert len(rows) == 1
    assert rows[0]["user_id"] == demo["id"]


def test_create_item_trims_whitespace(client, demo):
    body = _create(client, demo["headers"], "  写周报  ")

    assert body["title"] == "写周报"


def test_create_item_rejects_empty_title(client, demo):
    response = client.post(ITEMS_PATH, json={"title": ""}, headers=demo["headers"])

    assert response.status_code == 422, response.text


def test_create_item_rejects_whitespace_only_title(client, demo):
    """长度校验通过但 strip 后为空，应返回明确的业务提示而不是存进空标题。"""
    response = client.post(ITEMS_PATH, json={"title": "   "}, headers=demo["headers"])

    assert response.status_code == 422
    assert response.json()["detail"] == "待办内容不能为空"
    assert fetch_item_rows() == []


def test_create_item_rejects_too_long_title(client, demo):
    response = client.post(ITEMS_PATH, json={"title": "x" * 101}, headers=demo["headers"])

    assert response.status_code == 422


def test_create_item_accepts_max_length_title(client, demo):
    body = _create(client, demo["headers"], "x" * 100)

    assert len(body["title"]) == 100


def test_create_item_rejects_missing_field(client, demo):
    response = client.post(ITEMS_PATH, json={}, headers=demo["headers"])

    assert response.status_code == 422


# ---------------------------------------------------------------- 列表


def test_list_items_empty(client, demo):
    response = client.get(ITEMS_PATH, headers=demo["headers"])

    assert response.status_code == 200
    assert response.json() == []


def test_list_items_orders_undone_first_then_newest(client, demo):
    first = _create(client, demo["headers"], "第一项")
    second = _create(client, demo["headers"], "第二项")
    third = _create(client, demo["headers"], "第三项")

    client.patch(f"{ITEMS_PATH}/{second['id']}", json={"done": True}, headers=demo["headers"])

    body = client.get(ITEMS_PATH, headers=demo["headers"]).json()

    # 未完成在前（按 id 倒序），已完成排到最后。
    assert [item["id"] for item in body] == [third["id"], first["id"], second["id"]]
    assert [item["done"] for item in body] == [False, False, True]


def test_list_items_only_returns_own_data(client, demo, other):
    _create(client, demo["headers"], "demo 的待办")
    _create(client, other["headers"], "other 的待办")

    demo_items = client.get(ITEMS_PATH, headers=demo["headers"]).json()
    other_items = client.get(ITEMS_PATH, headers=other["headers"]).json()

    assert [item["title"] for item in demo_items] == ["demo 的待办"]
    assert [item["title"] for item in other_items] == ["other 的待办"]


# ---------------------------------------------------------------- 切换状态


def test_patch_marks_item_done(client, demo):
    item = _create(client, demo["headers"], "交报告")

    response = client.patch(
        f"{ITEMS_PATH}/{item['id']}",
        json={"done": True},
        headers=demo["headers"],
    )

    assert response.status_code == 200
    assert response.json()["done"] is True
    assert fetch_item_rows()[0]["done"] == 1


def test_patch_can_toggle_back(client, demo):
    item = _create(client, demo["headers"], "交报告")
    client.patch(f"{ITEMS_PATH}/{item['id']}", json={"done": True}, headers=demo["headers"])

    response = client.patch(
        f"{ITEMS_PATH}/{item['id']}",
        json={"done": False},
        headers=demo["headers"],
    )

    assert response.status_code == 200
    assert response.json()["done"] is False


def test_patch_missing_item_returns_404(client, demo):
    response = client.patch(f"{ITEMS_PATH}/9999", json={"done": True}, headers=demo["headers"])

    assert response.status_code == 404
    assert response.json()["detail"] == "记录不存在"


def test_patch_other_users_item_returns_404(client, demo, other):
    item = _create(client, demo["headers"], "demo 的待办")

    response = client.patch(
        f"{ITEMS_PATH}/{item['id']}",
        json={"done": True},
        headers=other["headers"],
    )

    assert response.status_code == 404
    # 原始记录不能被改动。
    assert fetch_item_rows()[0]["done"] == 0


def test_patch_rejects_invalid_payload(client, demo):
    item = _create(client, demo["headers"], "交报告")

    response = client.patch(f"{ITEMS_PATH}/{item['id']}", json={}, headers=demo["headers"])

    assert response.status_code == 422


# ---------------------------------------------------------------- 删除


def test_delete_item_returns_204(client, demo):
    item = _create(client, demo["headers"], "临时待办")

    response = client.delete(f"{ITEMS_PATH}/{item['id']}", headers=demo["headers"])

    assert response.status_code == 204
    assert response.content == b""
    assert fetch_item_rows() == []


def test_delete_twice_returns_404(client, demo):
    item = _create(client, demo["headers"], "临时待办")
    client.delete(f"{ITEMS_PATH}/{item['id']}", headers=demo["headers"])

    response = client.delete(f"{ITEMS_PATH}/{item['id']}", headers=demo["headers"])

    assert response.status_code == 404


def test_delete_other_users_item_returns_404(client, demo, other):
    item = _create(client, demo["headers"], "demo 的待办")

    response = client.delete(f"{ITEMS_PATH}/{item['id']}", headers=other["headers"])

    assert response.status_code == 404
    assert len(fetch_item_rows()) == 1


def test_delete_does_not_affect_other_users_items(client, demo, other):
    mine = _create(client, demo["headers"], "demo 的待办")
    theirs = _create(client, other["headers"], "other 的待办")

    client.delete(f"{ITEMS_PATH}/{mine['id']}", headers=demo["headers"])

    remaining = client.get(ITEMS_PATH, headers=other["headers"]).json()
    assert [item["id"] for item in remaining] == [theirs["id"]]

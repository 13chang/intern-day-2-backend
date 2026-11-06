"""活动助手接口测试：设置、生成、三个操作、统计、建议与确认、越权。

时间被固定住（``monkeypatch`` 掉 ``rules.now_local``），
否则用例会随"运行时的钟点"时绿时红 —— 例如晚上跑"生成安排"必然没有可用时段。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.activity import rules
from app.db import get_conn

TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=TZ8)
DAY = date(2026, 11, 6)

SETTINGS = {
    "work_start": "09:00",
    "work_end": "18:00",
    "windows": [
        {"label": "morning", "start": "10:00", "end": "12:00"},
        {"label": "afternoon", "start": "14:00", "end": "17:30"},
    ],
    "quiet_start": "15:00",
    "quiet_end": "16:00",
    "daily_target": 3,
}


@pytest.fixture()
def frozen(monkeypatch) -> datetime:
    """把"现在"固定在 2026-11-06 09:00，用例才可重复。"""
    monkeypatch.setattr(rules, "now_local", lambda: NOW)
    return NOW


def save_settings(client: TestClient, headers: dict, **overrides) -> dict:
    payload = {**SETTINGS, **overrides}
    response = client.put("/activity/settings", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 设置

def test_settings_defaults_without_record(client: TestClient, demo: dict) -> None:
    response = client.get("/activity/settings", headers=demo["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["daily_target"] == 3
    assert body["windows"], "没设置过也要给出默认窗口，前端不需要先创建"


def test_settings_requires_login(client: TestClient) -> None:
    assert client.get("/activity/settings").status_code == 401


def test_settings_saved_and_read_back(client: TestClient, demo: dict) -> None:
    save_settings(client, demo["headers"])
    body = client.get("/activity/settings", headers=demo["headers"]).json()
    assert body["quiet_start"] == "15:00"
    assert len(body["windows"]) == 2


def test_settings_rejects_inverted_work_hours(client: TestClient, demo: dict) -> None:
    response = client.put(
        "/activity/settings",
        headers=demo["headers"],
        json={"work_start": "18:00", "work_end": "09:00"},
    )
    assert response.status_code == 422
    assert "结束时间" in response.json()["detail"]


def test_settings_rejects_inverted_window(client: TestClient, demo: dict) -> None:
    response = client.put(
        "/activity/settings",
        headers=demo["headers"],
        json={"windows": [{"start": "12:00", "end": "10:00"}]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- 生成

def test_generate_avoids_quiet_hours(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    response = client.post("/activity/generate", headers=demo["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 3
    for item in body["activities"]:
        assert not item["scheduled_at"][11:16].startswith("15"), item


def test_generate_is_idempotent(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    first = client.post("/activity/generate", headers=demo["headers"]).json()
    second = client.post("/activity/generate", headers=demo["headers"]).json()
    assert first["created"] == 3
    assert second["created"] == 0 and "已经" in second["notice"]


def test_generate_without_free_time_returns_409(
    client: TestClient, demo: dict, frozen, monkeypatch
) -> None:
    save_settings(
        client,
        demo["headers"],
        windows=[{"label": "x", "start": "09:00", "end": "09:30"}],
        quiet_start=None,
        quiet_end=None,
    )
    # 把"现在"推到窗口结束后，当天就没有可用时段了
    monkeypatch.setattr(rules, "now_local", lambda: NOW.replace(hour=10, minute=0))

    response = client.post("/activity/generate", headers=demo["headers"])
    assert response.status_code == 409
    assert response.json()["detail"]


# ---------------------------------------------------------------- 三个操作

def test_complete_is_idempotent_and_conflicts_on_skip(
    client: TestClient, demo: dict, frozen
) -> None:
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]

    done = client.post(f"/activity/{items[0]['id']}/complete", headers=demo["headers"])
    assert done.status_code == 200
    assert done.json()["activity"]["status"] == "completed"

    again = client.post(f"/activity/{items[0]['id']}/complete", headers=demo["headers"])
    assert again.status_code == 200 and again.json()["idempotent"] is True

    conflict = client.post(f"/activity/{items[0]['id']}/skip", headers=demo["headers"])
    assert conflict.status_code == 409


def test_skip_records_reason(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]

    response = client.post(
        f"/activity/{items[-1]['id']}/skip",
        headers=demo["headers"],
        json={"reason": "开会"},
    )
    assert response.status_code == 200
    body = response.json()["activity"]
    assert body["status"] == "skipped" and body["skipped_reason"] == "开会"


def test_postpone_creates_replacement(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    target = items[0]

    response = client.post(f"/activity/{target['id']}/postpone", headers=demo["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["original"]["status"] == "postponed"
    assert body["replacement"]["scheduled_at"] > target["scheduled_at"]
    assert body["replacement"]["postponed_from"] == target["id"]
    assert body["replacement"]["postpone_count"] == 1


def test_postpone_without_slot_keeps_original_planned(
    client: TestClient, demo: dict, frozen
) -> None:
    """没有可用时段时必须 409，而且原活动不能变成"消失"或半成品状态。"""
    save_settings(
        client,
        demo["headers"],
        windows=[{"label": "x", "start": "10:00", "end": "11:20"}],
        quiet_start=None,
        quiet_end=None,
    )
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    target = items[0]

    response = client.post(f"/activity/{target['id']}/postpone", headers=demo["headers"])
    assert response.status_code == 409, response.text

    after = client.get("/activity/today", headers=demo["headers"]).json()
    kept = [item for item in after if item["id"] == target["id"]][0]
    assert kept["status"] == "planned"


def test_operations_on_unknown_activity_return_404(client: TestClient, demo: dict) -> None:
    assert client.post("/activity/9999/complete", headers=demo["headers"]).status_code == 404


# ---------------------------------------------------------------- 统计

def test_weekly_stats_counts_all_outcomes(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]

    client.post(f"/activity/{items[0]['id']}/complete", headers=demo["headers"])
    client.post(f"/activity/{items[1]['id']}/postpone", headers=demo["headers"])
    client.post(f"/activity/{items[2]['id']}/skip", headers=demo["headers"])

    stats = client.get("/activity/stats/weekly", headers=demo["headers"]).json()
    # 计划数只算原始安排：推迟产生的替代条目不算新计划
    assert stats["planned"] == 3
    assert stats["completed"] == 1
    assert stats["postponed"] == 1
    assert stats["skipped"] == 1
    assert len(stats["days"]) == 7


# ---------------------------------------------------------------- 建议与确认

def test_suggestions_do_not_persist(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    before = client.get("/activity/today", headers=demo["headers"]).json()

    response = client.post(
        "/activity/suggestions",
        headers=demo["headers"],
        json={"count": 2, "note": "下午比较忙"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["suggestions"]) == 2
    assert "下午比较忙" in body["explanation"]
    assert client.get("/activity/today", headers=demo["headers"]).json() == before


def test_confirm_saves_agent_items(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])
    suggestions = client.post(
        "/activity/suggestions", headers=demo["headers"], json={"count": 2}
    ).json()["suggestions"]

    response = client.post(
        "/activity/confirm", headers=demo["headers"], json={"items": suggestions}
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["saved"] == 2
    assert all(item["source"] == "agent" for item in saved["activities"])


def test_confirm_rejects_quiet_hours_with_index(client: TestClient, demo: dict, frozen) -> None:
    """验收第 9 条：建议落在免打扰里，后端必须拒绝并指出是第几条。"""
    save_settings(client, demo["headers"])
    response = client.post(
        "/activity/confirm",
        headers=demo["headers"],
        json={
            "items": [
                {
                    "scheduled_at": f"{DAY.isoformat()}T15:30:00+08:00",
                    "activity_type": "walk",
                    "title": "起身走动",
                    "description": "走一小段",
                }
            ]
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "第 1 条" in detail and "免打扰" in detail


# ---------------------------------------------------------------- 用户隔离

def test_other_user_cannot_touch_my_activity(
    client: TestClient, demo: dict, other: dict, frozen
) -> None:
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]

    response = client.post(
        f"/activity/{items[0]['id']}/complete", headers=other["headers"]
    )
    assert response.status_code == 404
    assert client.get("/activity/today", headers=other["headers"]).json() == []


def test_settings_are_per_user(client: TestClient, demo: dict, other: dict) -> None:
    save_settings(client, demo["headers"], daily_target=2)
    other_settings = client.get("/activity/settings", headers=other["headers"]).json()
    assert other_settings["daily_target"] == 3


# ---------------------------------------------------------------- 同一时刻不能出现两条

def test_completed_slot_is_not_offered_again(
    client: TestClient, demo: dict, frozen
) -> None:
    """做完一条之后，那个时刻不能再被建议。

    回归用例。``busy`` 以前只算 ``planned``，于是"已完成"的时刻被当成空闲，
    建议面板又给一次同一个时间，确认后页面上就是两行 14:50（一行 rule、一行 agent）。
    """
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    done_at = items[0]["scheduled_at"]
    assert client.post(
        f"/activity/{items[0]['id']}/complete", headers=demo["headers"]
    ).status_code == 200

    body = client.post(
        "/activity/suggestions", headers=demo["headers"], json={"count": 3}
    ).json()

    assert body["suggestions"], body
    assert all(item["scheduled_at"] != done_at for item in body["suggestions"])


def test_confirm_rejects_duplicate_of_completed_slot(
    client: TestClient, demo: dict, frozen
) -> None:
    """手工提交一个已完成时刻的草稿，也必须被拒 —— 否则前端能绕过。"""
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    done = items[0]
    client.post(f"/activity/{done['id']}/complete", headers=demo["headers"])

    response = client.post(
        "/activity/confirm",
        headers=demo["headers"],
        json={
            "items": [
                {
                    "scheduled_at": done["scheduled_at"],
                    "activity_type": "walk",
                    "title": "重复的一条",
                    "description": "",
                    "duration_min": 5,
                }
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("第 1 条")


def test_generate_after_all_completed_does_not_duplicate(
    client: TestClient, demo: dict, frozen
) -> None:
    """全部做完之后再点「生成安排」，不能在同一批时刻上再排一遍。

    旧逻辑只把 ``planned`` 当成"今天已有安排"，全完成后会认为今天还空着，
    于是 10:45 / 14:15 / 16:45 再来一轮，页面上每行都出现两次。
    """
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    for item in items:
        client.post(f"/activity/{item['id']}/complete", headers=demo["headers"])

    again = client.post("/activity/generate", headers=demo["headers"]).json()

    assert again["created"] == 0
    assert "已经" in again["notice"]
    times = [item["scheduled_at"] for item in again["activities"]]
    assert len(times) == len(set(times))


def test_unique_index_blocks_second_row_at_the_same_moment(
    client: TestClient, demo: dict, frozen
) -> None:
    """数据库层面兜底：同一用户同一时刻写不进第二条。

    应用层的 ``busy_of_day`` 已经会避开，这一条是防"以后新增写入路径时又漏一处"——
    唯一索引让重复在 SQL 层就失败，而不是等用户在页面上发现两行同一时间。
    """
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    taken = items[0]["scheduled_at"]

    with pytest.raises(sqlite3.IntegrityError):
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO activities
                    (user_id, activity_type, title, description, scheduled_at,
                     duration_min, created_at, updated_at)
                VALUES (?, 'walk', '重复的一条', '绕过应用层直接插', ?, 5, 0, 0)
                """,
                (demo["id"], taken),
            )


def test_unique_index_allows_different_users_at_the_same_moment(
    client: TestClient, demo: dict, other: dict, frozen
) -> None:
    """唯一索引的粒度是"每个用户"，不能把别的用户也挡了。"""
    for who in (demo, other):
        save_settings(client, who["headers"])
        items = client.post("/activity/generate", headers=who["headers"]).json()["activities"]
        assert items, who["username"]
    demo_times = {
        item["scheduled_at"]
        for item in client.get("/activity/today", headers=demo["headers"]).json()
    }
    other_times = {
        item["scheduled_at"]
        for item in client.get("/activity/today", headers=other["headers"]).json()
    }
    assert demo_times and other_times
    assert demo_times & other_times, "两个账号都被排到同一批时刻，说明互不影响"

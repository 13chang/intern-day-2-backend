"""真实模型建议引擎（``engine="llm"``）的测试。

这里不联网：``planner.default_completer`` 被换成一个返回预设文本的假函数。
这样能覆盖真正容易出错的地方 —— JSON 容错、逐条校验、不合规丢弃、条件覆盖 ——
而不是去测别人的 HTTP 客户端。

另一个前提：conftest 把 ``MODEL_MODE`` 固定成 mock。所以"后端没配真实模型时
请求 llm 引擎"这条路径可以直接测，不需要额外造环境。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.activity import planner, rules

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
    monkeypatch.setattr(rules, "now_local", lambda: NOW)
    return NOW


@pytest.fixture()
def llm(monkeypatch):
    """把真实模型换成"返回固定文本"的假模型，返回安装函数。"""

    def install(text: str | dict) -> None:
        raw = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        monkeypatch.setattr(planner, "default_completer", lambda: (lambda _prompt: raw))

    return install


def save_settings(client: TestClient, headers: dict, **overrides) -> dict:
    response = client.put("/activity/settings", headers=headers, json={**SETTINGS, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def suggest(client: TestClient, headers: dict, **body) -> tuple[int, dict]:
    response = client.post("/activity/suggestions", headers=headers, json=body)
    return response.status_code, response.json()


# ---------------------------------------------------------------- 引擎可用性

def test_engines_report_rule_always_llm_only_when_configured(
    client: TestClient, demo: dict
) -> None:
    """Mock 模式下 llm 必须标成不可用，并给出原因 —— 前端据此禁用按钮。"""
    response = client.get("/activity/engines", headers=demo["headers"])
    assert response.status_code == 200, response.text
    engines = {item["id"]: item for item in response.json()["engines"]}

    assert engines["rule"]["available"] is True
    assert engines["llm"]["available"] is False
    assert "mock" in engines["llm"]["reason"].lower()


def test_engines_requires_login(client: TestClient) -> None:
    assert client.get("/activity/engines").status_code == 401


def test_llm_engine_refuses_when_backend_is_mock(
    client: TestClient, demo: dict, frozen
) -> None:
    """没配真实模型却请求 llm：如实报错，不能悄悄退回规则引擎。"""
    status, body = suggest(client, demo["headers"], engine="llm")
    assert status == 409
    assert "Mock" in body["detail"]


# ---------------------------------------------------------------- 正常产出

def test_llm_engine_returns_validated_drafts(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    save_settings(client, demo["headers"])
    llm(
        {
            "explanation": "上午和下午各一次，避开你开会的时段。",
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "走廊走两圈",
                 "description": "顺路接杯水", "reason": "刚上班不久，先动一下"},
                {"time": "16:30", "activity_type": "stretch", "title": "肩颈放松",
                 "description": "慢慢转两下", "reason": "下午容易僵"},
            ],
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=2)

    assert status == 200, body
    assert body["mode"] == "llm"
    assert body["dropped"] == 0
    assert body["explanation"] == "上午和下午各一次，避开你开会的时段。"
    assert [item["scheduled_at"][11:16] for item in body["suggestions"]] == ["10:30", "16:30"]
    assert body["suggestions"][0]["title"] == "走廊走两圈"


def test_llm_drafts_are_not_persisted(client: TestClient, demo: dict, frozen, llm) -> None:
    """建议只算不存：调完接口，当天列表必须还是空的。"""
    save_settings(client, demo["headers"])
    llm({"suggestions": [{"time": "10:30", "activity_type": "walk"}]})

    suggest(client, demo["headers"], engine="llm", count=1)

    listing = client.get("/activity/today", headers=demo["headers"]).json()
    assert listing == []


def test_llm_drops_suggestions_that_break_rules(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """模型给的时间落在免打扰/窗口外/已过去，一律丢弃，并把条数如实告诉用户。"""
    save_settings(client, demo["headers"])
    llm(
        {
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "正常一条"},
                {"time": "15:30", "activity_type": "walk", "title": "在免打扰里"},
                {"time": "08:00", "activity_type": "walk", "title": "在窗口外"},
                {"time": "08:30", "activity_type": "walk", "title": "已经过去了"},
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=4)

    assert status == 200, body
    assert [item["scheduled_at"][11:16] for item in body["suggestions"]] == ["10:30"]
    assert body["dropped"] == 3
    assert "3 条" in body["notice"]


def test_llm_falls_back_to_catalog_text(client: TestClient, demo: dict, frozen, llm) -> None:
    """模型给了不存在的活动类型、又没写标题：用活动库兜住，不留空白。"""
    save_settings(client, demo["headers"])
    llm({"suggestions": [{"time": "10:30", "activity_type": "teleport", "title": ""}]})

    status, body = suggest(client, demo["headers"], engine="llm", count=1)

    assert status == 200, body
    draft = body["suggestions"][0]
    assert draft["activity_type"] == "walk"
    assert draft["title"] == rules.ACTIVITY_CATALOG["walk"][0]


def test_unknown_type_also_drops_model_wording(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """类型不认识时，模型写的那份文案也不能留。

    否则会出现"类型是走动、标题是去打球、时长 5 分钟"的假满足 ——
    用户看到标题以为偏好生效了，实际排的还是一次起身走动。
    """
    save_settings(client, demo["headers"])
    llm(
        {
            "suggestions": [
                {
                    "time": "10:30",
                    "activity_type": "basketball",
                    "title": "去打球",
                    "description": "找同事打一会儿篮球",
                    "reason": "按你的偏好安排",
                }
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=1)

    assert status == 200, body
    draft = body["suggestions"][0]
    assert draft["activity_type"] == "walk"
    assert draft["title"] == rules.ACTIVITY_CATALOG["walk"][0]
    assert draft["description"] == rules.ACTIVITY_CATALOG["walk"][1]
    assert draft["reason"] == ""


def test_prompt_tells_model_not_to_fake_unsupported_preference(frozen) -> None:
    """偏好超出四类时，提示词要求如实说明，不许嘴上说"已按偏好安排"。"""
    prompt = planner.build_prompt({**SETTINGS}, DAY, [], NOW, 2, "打篮球")

    assert "打篮球" in prompt
    assert "【用户的偏好】" in prompt
    assert "不要硬凑" in prompt
    assert "如实说明" in prompt
    # explanation 与建议必须一致，别一边说打球一边给拉伸
    assert "explanation 必须和建议本身一致" in prompt


def test_prompt_omits_preference_section_when_note_is_empty(frozen) -> None:
    """没填偏好时整段都不能出现。

    占位符会被模型当内容抄进 explanation —— 线上真出现过
    「未说明偏好，安排两次轻活动…」，用户看到的是提示词碎片。
    """
    prompt = planner.build_prompt({**SETTINGS}, DAY, [], NOW, 2)

    assert "【用户的偏好】" not in prompt
    assert "没特别说明" not in prompt


def test_llm_drops_suggestions_too_close_to_each_other(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """同一批建议之间也要够间隔：10 分钟内两条，只留早的那条。

    回归用例。``validate_slot`` 只跟"已有安排"比，两条草稿彼此隔 10 分钟它是不管的，
    于是"建议里 4 条都能勾、确认第 2 条 422"—— 生成和保存两套结论。
    """
    save_settings(client, demo["headers"])
    llm(
        {
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "早的那条"},
                {"time": "10:40", "activity_type": "water", "title": "隔得太近"},
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=2)

    assert status == 200, body
    assert [item["scheduled_at"][11:16] for item in body["suggestions"]] == ["10:30"]
    assert body["dropped"] == 1


def test_llm_keeps_earlier_slot_when_model_returns_out_of_order(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """模型把晚的写在前面，留下的仍然是早的那条。"""
    save_settings(client, demo["headers"])
    llm(
        {
            "suggestions": [
                {"time": "10:40", "activity_type": "water", "title": "晚的写在前面"},
                {"time": "10:30", "activity_type": "walk", "title": "早的写在后面"},
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=2)

    assert status == 200, body
    assert [item["scheduled_at"][11:16] for item in body["suggestions"]] == ["10:30"]


def test_llm_does_not_reuse_a_completed_slot(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """已完成的时刻对 llm 引擎同样不可用。

    真实踩到的现象：规则引擎排的 14:50 做完之后，建议面板再给一次 14:50，
    确认保存就多出一条同样的时间（source=agent），页面上并排两行。
    """
    save_settings(client, demo["headers"])
    items = client.post("/activity/generate", headers=demo["headers"]).json()["activities"]
    done = items[0]
    assert client.post(
        f"/activity/{done['id']}/complete", headers=demo["headers"]
    ).status_code == 200
    done_hhmm = done["scheduled_at"][11:16]

    llm(
        {
            "suggestions": [
                {"time": done_hhmm, "activity_type": "walk", "title": "同一时刻再来一条"},
                {"time": "11:30", "activity_type": "water", "title": "空闲时刻"},
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=2)

    assert status == 200, body
    assert [item["scheduled_at"][11:16] for item in body["suggestions"]] == ["11:30"]
    assert body["dropped"] == 1


def test_llm_dedupes_same_time(client: TestClient, demo: dict, frozen, llm) -> None:
    """同一个时间给两条，只留一条 —— 否则用户会勾到两条一模一样的安排。"""
    save_settings(client, demo["headers"])
    llm(
        {
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "第一条"},
                {"time": "10:30", "activity_type": "water", "title": "重复时间"},
            ]
        }
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=2)

    assert status == 200, body
    assert len(body["suggestions"]) == 1
    assert body["dropped"] == 1


# ---------------------------------------------------------------- 回复容错

def test_llm_accepts_markdown_fenced_json(client: TestClient, demo: dict, frozen, llm) -> None:
    """模型常把 JSON 包在 ```json 里，要能拆掉。"""
    save_settings(client, demo["headers"])
    llm(
        """好的，建议如下：
```json
{"suggestions": [{"time": "10:30", "activity_type": "water", "title": "接水"}]}
```"""
    )

    status, body = suggest(client, demo["headers"], engine="llm", count=1)

    assert status == 200, body
    assert body["suggestions"][0]["title"] == "接水"


def test_llm_rejects_non_json_reply(client: TestClient, demo: dict, frozen, llm) -> None:
    save_settings(client, demo["headers"])
    llm("抱歉，我无法完成这个请求。")

    status, body = suggest(client, demo["headers"], engine="llm")

    assert status == 409
    assert "JSON" in body["detail"]


def test_llm_without_any_valid_slot_returns_409(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    save_settings(client, demo["headers"])
    llm({"suggestions": [{"time": "15:30", "activity_type": "walk"}]})

    status, body = suggest(client, demo["headers"], engine="llm")

    assert status == 409
    assert "不满足" in body["detail"]


def test_llm_empty_suggestions_returns_409(client: TestClient, demo: dict, frozen, llm) -> None:
    save_settings(client, demo["headers"])
    llm({"explanation": "今天没空位", "suggestions": []})

    status, body = suggest(client, demo["headers"], engine="llm")
    assert status == 409


# ---------------------------------------------------------------- 条件覆盖

def test_conditions_apply_to_rule_engine(client: TestClient, demo: dict, frozen) -> None:
    """临时把下午窗口改成 16:00-17:00、关掉免打扰：排出的时间必须只落在这里。"""
    save_settings(client, demo["headers"])

    status, body = suggest(
        client,
        demo["headers"],
        engine="rule",
        count=2,
        conditions={
            "windows": [{"label": "afternoon", "start": "16:00", "end": "17:00"}],
            "ignore_quiet": True,
        },
    )

    assert status == 200, body
    times = [item["scheduled_at"][11:16] for item in body["suggestions"]]
    assert times, "这个条件下应该能排出东西"
    assert all("16:00" <= value < "17:00" for value in times), times


def test_conditions_do_not_change_saved_settings(
    client: TestClient, demo: dict, frozen
) -> None:
    """临时条件只影响这一次生成，不能顺手改掉长期设置。"""
    save_settings(client, demo["headers"])

    suggest(
        client,
        demo["headers"],
        engine="rule",
        conditions={
            "work_start": "08:00",
            "work_end": "20:00",
            "windows": [{"label": "all", "start": "08:00", "end": "20:00"}],
            "ignore_quiet": True,
        },
    )

    saved = client.get("/activity/settings", headers=demo["headers"]).json()
    assert saved["work_start"] == "09:00"
    assert saved["quiet_start"] == "15:00"
    assert [window["start"] for window in saved["windows"]] == ["10:00", "14:00"]


def test_ignore_quiet_lets_llm_use_former_quiet_hours(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """显式关掉免打扰后，15:30 这种原本会被拒的时间应当被接受。"""
    save_settings(client, demo["headers"])
    llm({"suggestions": [{"time": "15:30", "activity_type": "stand", "title": "站起来"}]})

    status, body = suggest(
        client, demo["headers"], engine="llm", count=1, conditions={"ignore_quiet": True}
    )

    assert status == 200, body
    assert body["suggestions"][0]["scheduled_at"][11:16] == "15:30"


def test_inverted_condition_window_returns_422(client: TestClient, demo: dict, frozen) -> None:
    save_settings(client, demo["headers"])

    status, body = suggest(
        client,
        demo["headers"],
        engine="rule",
        conditions={"windows": [{"label": "x", "start": "17:00", "end": "16:00"}]},
    )

    assert status == 422
    assert "开始时间必须早于" in body["detail"]


def test_half_quiet_pair_in_conditions_returns_422(
    client: TestClient, demo: dict, frozen
) -> None:
    save_settings(client, demo["headers"])

    status, body = suggest(
        client, demo["headers"], engine="rule", conditions={"quiet_start": "15:00"}
    )

    assert status == 422
    assert "成对" in body["detail"]


# ---------------------------------------------------------------- 条件自相矛盾

# 截图上那组条件：工作时段 09:00-10:00，可活动时段 11:00-17:00 / 18:00-22:30。
# allowed_ranges 取交集，交集是空集，任何引擎都排不出东西。
CONTRADICTORY = {
    "work_start": "09:00",
    "work_end": "10:00",
    "windows": [
        {"label": "morning", "start": "11:00", "end": "17:00"},
        {"label": "afternoon", "start": "18:00", "end": "22:30"},
    ],
}


def test_llm_engine_blames_the_config_not_the_model(
    client: TestClient, demo: dict, frozen
) -> None:
    """工作时段和可活动时段没搭上：422，并说清是哪两段。

    这条是回归用例。以前这里会走完整条模型链路，最后抛
    "真实模型给出的时间都不满足你设置的条件" —— 把配置自相矛盾说成模型不合格，
    用户按提示去改免打扰、改次数，怎么改都不会好。
    """
    save_settings(client, demo["headers"], **CONTRADICTORY)

    status, body = suggest(client, demo["headers"], engine="llm", count=3)

    assert status == 422, body
    assert "没有重叠" in body["detail"]
    assert "09:00-10:00" in body["detail"]
    assert "11:00-17:00" in body["detail"]


def test_no_overlap_never_calls_the_model(
    client: TestClient, demo: dict, frozen, monkeypatch
) -> None:
    """条件自相矛盾时压根不该发出模型请求。"""
    save_settings(client, demo["headers"], **CONTRADICTORY)
    prompts: list[str] = []

    def spy() -> planner.Completer:
        def call(prompt: str) -> str:
            prompts.append(prompt)
            return '{"suggestions": []}'

        return call

    monkeypatch.setattr(planner, "default_completer", spy)

    status, _ = suggest(client, demo["headers"], engine="llm", count=3)

    assert status == 422
    assert prompts == []


def test_rule_engine_gives_the_same_answer_for_no_overlap(
    client: TestClient, demo: dict, frozen
) -> None:
    """两个引擎对同一组矛盾条件给出同一条提示 —— 判断只有一份实现。"""
    save_settings(client, demo["headers"], **CONTRADICTORY)

    status, body = suggest(client, demo["headers"], engine="rule")

    assert status == 422, body
    assert "没有重叠" in body["detail"]


def test_no_overlap_can_be_fixed_by_overriding_conditions(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """已保存的设置矛盾时，本次临时条件把它盖住就应该能正常出建议。"""
    save_settings(client, demo["headers"], **CONTRADICTORY)
    llm({"suggestions": [{"time": "11:30", "activity_type": "walk", "title": "走动"}]})

    status, body = suggest(
        client,
        demo["headers"],
        engine="llm",
        count=1,
        conditions={"work_start": "09:00", "work_end": "18:00"},
    )

    assert status == 200, body
    assert body["mode"] == "llm"
    assert body["suggestions"][0]["scheduled_at"][11:16] == "11:30"


# ---------------------------------------------------------------- 保存时仍然校验

def test_confirm_revalidates_llm_drafts(
    client: TestClient, demo: dict, frozen, llm
) -> None:
    """模型给的建议，保存时仍然要过一遍当时的设置。

    先按"没有免打扰"拿到 15:30，再把免打扰恢复成 15:00-16:00 去保存：
    必须被拒，否则"确认保存"这道闸对模型就是虚设的。
    """
    save_settings(client, demo["headers"], quiet_start=None, quiet_end=None)
    llm({"suggestions": [{"time": "15:30", "activity_type": "walk", "title": "走动"}]})

    status, body = suggest(client, demo["headers"], engine="llm", count=1)
    assert status == 200, body
    drafts = body["suggestions"]

    save_settings(client, demo["headers"])  # 恢复免打扰 15:00-16:00
    response = client.post("/activity/confirm", headers=demo["headers"], json={"items": drafts})

    assert response.status_code == 422
    assert response.json()["detail"].startswith("第 1 条")


# ---------------------------------------------------------------- 提示词

def test_prompt_only_offers_free_ranges(frozen) -> None:
    """提示词里给的是"已扣掉免打扰和已有安排"的区间，不是原始窗口。"""
    settings = {**SETTINGS}
    busy = [
        {"id": 1, "title": "已排的活动", "scheduled_at": "2026-11-06T14:25:00+08:00",
         "duration_min": 5, "status": "planned"}
    ]

    prompt = planner.build_prompt(settings, DAY, busy, NOW, 3, "下午容易困")

    assert "10:00-12:00" in prompt
    assert "16:00-17:30" in prompt
    # 免打扰那一段不能作为可选区间出现，14:00-15:00 会因为 14:25 的已排活动被切开
    assert "15:00-16:00" not in prompt
    assert "下午容易困" in prompt
    assert "walk" in prompt and "stretch" in prompt


def test_prompt_for_future_day_drops_todays_clock_constraints(frozen) -> None:
    """排明天时不能还把"现在几点"当约束喂给模型。

    未来那天不存在"已经过去"，硬塞一句"不早于现在"，模型会以为只能在
    今天剩下的时间附近挑，排出来的东西全得被后端丢掉。
    """
    tomorrow = DAY + timedelta(days=1)
    prompt = planner.build_prompt(SETTINGS, tomorrow, [], NOW, 2)

    assert tomorrow.isoformat() in prompt
    assert "还没到" in prompt
    assert "不早于现在" not in prompt
    assert "用户希望今天安排" not in prompt


def test_llm_engine_accepts_a_future_day(client: TestClient, demo: dict, frozen, llm) -> None:
    tomorrow = DAY + timedelta(days=1)
    save_settings(client, demo["headers"])
    llm(
        {
            "explanation": "明天上午下午各排一次",
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "起身走动",
                 "description": "走一小段", "reason": "上午精神好的时候"},
                {"time": "14:30", "activity_type": "water", "title": "接水",
                 "description": "顺便喝口水", "reason": "下午开头"},
            ],
        }
    )

    status, body = suggest(
        client, demo["headers"], engine="llm", count=2, day=tomorrow.isoformat()
    )

    assert status == 200, body
    assert body["day"] == tomorrow.isoformat()
    assert all(
        item["scheduled_at"].startswith(tomorrow.isoformat()) for item in body["suggestions"]
    )


def test_llm_engine_rejects_past_day_without_calling_the_model(
    client: TestClient, demo: dict, frozen, monkeypatch
) -> None:
    """日期越界要在调模型之前就拒掉，别白花一次调用。"""
    calls: list[str] = []
    monkeypatch.setattr(
        planner, "default_completer", lambda: (lambda prompt: calls.append(prompt) or "{}")
    )

    status, body = suggest(
        client, demo["headers"], engine="llm", day=(DAY - timedelta(days=1)).isoformat()
    )

    assert status == 422
    assert "已经过去" in body["detail"]
    assert calls == [], "条件已经不可能满足，不该再调模型"


# ---------------------------------------------------------------- 生成也走模型

def test_generate_with_llm_engine_saves_the_drafts(client: TestClient, demo: dict, frozen, llm) -> None:
    """「生成」按钮选 DeepSeek 时要真的落库，而不是只给一份草稿。"""
    save_settings(client, demo["headers"])
    llm(
        {
            "explanation": "上午一次下午一次",
            "suggestions": [
                {"time": "10:20", "activity_type": "walk", "title": "起身走走",
                 "description": "走一小段", "reason": "上午"},
                {"time": "16:10", "activity_type": "stand", "title": "站起来",
                 "description": "活动腿脚", "reason": "下午"},
            ],
        }
    )

    response = client.post(
        "/activity/generate", headers=demo["headers"], json={"engine": "llm"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "llm"
    assert body["created"] == 2
    assert body["explanation"] == "上午一次下午一次"
    assert [item["scheduled_at"][11:16] for item in body["activities"]] == ["10:20", "16:10"]
    # 来源要能分清是谁排的：rule 和 agent 混在一条列表里，事后要能追溯
    assert {item["source"] for item in body["activities"]} == {"agent"}
    # 确认真的写进了库，而不只是响应里带了
    listed = client.get("/activity/today", headers=demo["headers"]).json()
    assert len(listed) == 2


def test_generate_with_llm_unconfigured_is_409(client: TestClient, demo: dict, frozen) -> None:
    """后端没配真实模型时如实报错，不能偷偷用规则引擎顶上。"""
    save_settings(client, demo["headers"])

    response = client.post(
        "/activity/generate", headers=demo["headers"], json={"engine": "llm"}
    )

    assert response.status_code == 409
    assert "Mock" in response.json()["detail"]


def test_generate_with_llm_keeps_existing_plan_when_model_fails(
    client: TestClient, demo: dict, frozen
) -> None:
    """模型失败时不能把那天已经排好的东西清空。

    草稿是在写事务**之前**算的（``service.generate_day`` 里 draft 先行），
    所以 force 重建时模型一挂，旧计划原样还在。反过来说，
    如果哪天把 ``plan`` 挪到了 DELETE 之后，这条会立刻红。
    """
    save_settings(client, demo["headers"])
    first = client.post("/activity/generate", headers=demo["headers"]).json()
    assert first["created"] == 3

    failed = client.post(
        "/activity/generate?force=true", headers=demo["headers"], json={"engine": "llm"}
    )
    assert failed.status_code == 409

    rows = client.get("/activity/today", headers=demo["headers"]).json()
    assert len(rows) == 3, "模型调用失败不该动数据库"


def test_generate_passes_note_to_the_prompt(client: TestClient, demo: dict, frozen, monkeypatch) -> None:
    """偏好是"这次生成"的参数，必须真的进到提示词里，否则用户填了等于没填。"""
    save_settings(client, demo["headers"])
    prompts: list[str] = []
    payload = json.dumps(
        {
            "explanation": "按你下午容易困的说法，排一次走动",
            "suggestions": [
                {"time": "10:30", "activity_type": "walk", "title": "起身走走",
                 "description": "走一小段", "reason": "上午"},
            ],
        }
    )
    monkeypatch.setattr(
        planner, "default_completer", lambda: (lambda prompt: prompts.append(prompt) or payload)
    )

    response = client.post(
        "/activity/generate",
        headers=demo["headers"],
        json={"engine": "llm", "note": "下午容易困"},
    )

    assert response.status_code == 200, response.text
    assert prompts, "引擎选了 llm 却没调模型"
    assert "下午容易困" in prompts[0]

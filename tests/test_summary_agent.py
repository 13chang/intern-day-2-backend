"""总结生成与输出校验的测试。

不调用真实模型，只测三件能确定的事：
模板是否只使用真实数字、JSON 解析是否稳、校验能不能拦住编造与医疗表述。
"""

from __future__ import annotations

import asyncio

import pytest

from app.summary.agent import (
    PermanentError,
    TransientError,
    build_prompt,
    compose_summary,
    parse_json,
    validate_summary,
)
from app.summary.template import template_summary

FULL_STATS = {
    "report_date": "2026-11-06",
    "planned_total": 3,
    "completed": 2,
    "skipped": 1,
    "unfinished": 0,
    "postpone_ops": 1,
    "sedentary_minutes": None,
    "has_records": True,
}

EMPTY_STATS = {
    "report_date": "2026-11-06",
    "planned_total": 0,
    "completed": 0,
    "skipped": 0,
    "unfinished": 0,
    "postpone_ops": 0,
    "sedentary_minutes": None,
    "has_records": False,
}


# ---------------------------------------------------------------- 模板

def test_template_uses_real_numbers() -> None:
    review, suggestion = template_summary(FULL_STATS)
    assert "计划 3 次" in review
    assert "完成 2 次" in review
    assert "推迟 1 次" in review
    assert suggestion


def test_template_says_not_recorded_for_empty_day() -> None:
    review, suggestion = template_summary(EMPTY_STATS)
    assert "没有活动记录" in review
    # 空日不能出现"久坐 0 分钟"这种确定陈述
    assert "0 分钟" not in review
    assert suggestion


def test_template_mentions_unfinished_only_when_present() -> None:
    no_unfinished = template_summary({**FULL_STATS, "unfinished": 0})[0]
    has_unfinished = template_summary({**FULL_STATS, "unfinished": 2})[0]
    assert "没有处理" not in no_unfinished
    assert "没有处理" in has_unfinished


# ---------------------------------------------------------------- 提示词

def test_prompt_has_no_report_date() -> None:
    """日期不给模型，少一个被写进正文的字段。"""
    prompt = build_prompt(FULL_STATS)
    assert "report_date" not in prompt
    assert "2026-11-06" not in prompt
    assert '"planned_total": 3' in prompt


# ---------------------------------------------------------------- JSON 解析

def test_parse_json_plain() -> None:
    data = parse_json('{"review": "a", "suggestion": "b"}')
    assert data["review"] == "a"


def test_parse_json_with_code_fence() -> None:
    """真实模型偶尔会裹 ```json 围栏，要能剥掉。"""
    text = '```json\n{"review": "a", "suggestion": "b"}\n```'
    data = parse_json(text)
    assert data["suggestion"] == "b"


def test_parse_json_rejects_garbage() -> None:
    with pytest.raises(TransientError):
        parse_json("今天你完成了两次活动")


def test_parse_json_rejects_empty() -> None:
    with pytest.raises(TransientError):
        parse_json("   ")


# ---------------------------------------------------------------- 校验

def test_validate_accepts_numbers_from_stats() -> None:
    review, suggestion = validate_summary(
        FULL_STATS, "计划 3 次，完成 2 次，跳过 1 次。", "把下午那次挪到午休后。"
    )
    assert "计划 3 次" in review
    assert suggestion


def test_validate_rejects_fabricated_count_on_empty_day() -> None:
    """空日编造是最容易被抓的错误，必须拦。"""
    with pytest.raises(TransientError):
        validate_summary(EMPTY_STATS, "这一天你完成了 2 次活动。", "继续保持。")


def test_validate_allows_zero_count_on_empty_day() -> None:
    review, suggestion = validate_summary(
        EMPTY_STATS, "这一天没有活动记录，未记录久坐情况。", "先在页面上排几次活动。"
    )
    assert "没有活动记录" in review


def test_validate_rejects_number_outside_stats() -> None:
    """统计里没有 7 次，正文出现就是编的。"""
    with pytest.raises(TransientError):
        validate_summary(FULL_STATS, "计划 3 次，另外还完成了 7 次。", "保持。")


def test_validate_drops_medical_sentences() -> None:
    review, suggestion = validate_summary(
        FULL_STATS,
        "计划 3 次，完成 2 次。建议进行治疗。",
        "把下午那次挪到午休后。",
    )
    assert "治疗" not in review
    assert "完成 2 次" in review


def test_validate_truncates_overlong_text() -> None:
    long_review = "计划 3 次。" + "很" * 200
    review, _ = validate_summary(FULL_STATS, long_review, "保持。")
    assert len(review) <= 120


def test_validate_rejects_empty_fields() -> None:
    with pytest.raises(TransientError):
        validate_summary(FULL_STATS, "", "保持。")


def test_validate_rejects_all_medical_content() -> None:
    """整段都是医疗表述时，清洗后为空，应该判失败而不是存一句空话。"""
    with pytest.raises(TransientError):
        validate_summary(FULL_STATS, "建议进行治疗。", "可以按处方用药。")


# ------------------------------------------------- 相对时间词（真实踩到的）

def test_validate_rewrites_relative_day_words() -> None:
    """模型会写"今天计划 3 次"，而这份日报是给已经过去的那一天的。

    真实模型第一次跑出来的就是「今天计划5次活动…」，日期口径直接错了。
    页面已经把日期显示在外面，正文里出现"今天"一定是错的，换成"这一天"。
    """
    review, suggestion = validate_summary(
        FULL_STATS, "今天计划 3 次，完成 2 次。", "明天把剩下那次补上。"
    )
    assert "今天" not in review
    assert "明天" not in suggestion
    assert "这一天计划 3 次" in review
    assert "这一天把剩下那次补上" in suggestion


def test_validate_rewrites_longer_relative_words_first() -> None:
    """"前天"必须先被换掉，否则被"今天"这类短词先吃一部分字符，留下残句。"""
    review, _ = validate_summary(FULL_STATS, "前天完成 2 次，今天也完成了。", "保持。")
    assert "前天" not in review and "今天" not in review
    assert review.count("这一天") == 2


# ---------------------------------------------------------------- 组合

def test_compose_uses_template_in_mock_mode() -> None:
    """测试环境 MODEL_MODE=mock，所以走模板，不产生任何模型调用。"""
    review, suggestion, source, model = asyncio.run(compose_summary(FULL_STATS))
    assert source == "rule"
    assert model == "template"
    assert "完成 2 次" in review


def test_compose_reports_permanent_error_when_config_broken(monkeypatch) -> None:
    """real 模式但配置不全时，应该是永久错误（不重试），而不是可重试错误。"""
    monkeypatch.setenv("MODEL_MODE", "real")
    monkeypatch.setenv("APP_LLM_MODE", "real")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("APP_LLM_API_KEY", raising=False)

    with pytest.raises(PermanentError):
        asyncio.run(compose_summary(FULL_STATS))

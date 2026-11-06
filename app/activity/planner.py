"""用真实模型产出活动建议：第二个"生成建议"引擎。

和规则引擎的分工先说清楚，避免误解：

- ``rules.plan_day`` 负责"能排在哪"——纯函数、可单测、结论确定；
- 本模块负责"按用户给的条件挑时间与活动、并给出解释"，产物仍然只是**草稿**；
- 两条路都必须过同一个 ``rules.validate_slot``。模型说什么都不算数：
  它给的时间落在免打扰里、和已有安排撞了、或者已经过去，一律丢弃。

为什么把模型调用包在这一层：

- 路由层不该知道 ``Message`` / 客户端 / JSON 容错这些细节；
- 测试要能注入假模型，不联网也能覆盖解析、校验、丢弃这三条分支。

关于 ``completer``：它就是一个 ``str -> str`` 的函数，输入提示词、输出模型文本。
生产用 ``default_completer()``（真实模型），测试塞一个返回固定 JSON 的假函数。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from app.activity import rules

logger = logging.getLogger(__name__)

Completer = Callable[[str], str]

# 一次最多接受几条：任务的"每天次数"上限是 8，多出来的直接忽略
MAX_ITEMS = 8
# 模型给的长文本一律截断，避免把一屏塞满（也顺手防住超长输出）
MAX_TITLE = 20
MAX_DESCRIPTION = 60
MAX_REASON = 80
MAX_EXPLANATION = 200

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class LlmUnavailable(RuntimeError):
    """真实模型不可用：没配置、调用失败，或者回复不是能用的 JSON。

    路由层据此返回明确的中文错误，而不是悄悄退回规则引擎 ——
    用户明确选了 DeepSeek，偷偷换成别的引擎等于骗人。
    """


class _Item(BaseModel):
    """模型输出里的单条建议。字段都能缺省，缺了就用活动库的默认文案兜住。"""

    time: str = Field(pattern=HHMM, description="HH:MM，当天本地时间")
    activity_type: str = "walk"
    title: str = ""
    description: str = ""
    reason: str = ""


class _Plan(BaseModel):
    explanation: str = ""
    suggestions: list[_Item] = Field(default_factory=list)


def default_completer() -> Completer:
    """构造真实模型调用器。

    客户端类型与 ``app/agent/factory.py`` 保持一致：必须是
    ``OpenAIChatCompletionClient``（POST /chat/completions）。
    DeepSeek 的 OpenAI 兼容层没实现 Responses API，换类会直接 400。
    """
    from app.agent.config import load_llm_settings

    settings = load_llm_settings()
    if settings.is_mock:
        raise LlmUnavailable(
            "当前后端运行在 Mock 模式，无法调用真实模型。"
            "把 .env 里的 MODEL_MODE 改成 real 并配好 OPENAI_* 三项后重启即可。"
        )

    def call(prompt: str) -> str:
        from agent_framework import Message
        from agent_framework.openai import OpenAIChatCompletionClient

        client = OpenAIChatCompletionClient(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

        async def run() -> str:
            response = await client.get_response(
                [Message("user", contents=[prompt])],
                options={
                    "temperature": 0.3,
                    "max_tokens": 900,
                    # 只要 JSON 对象。DeepSeek 支持 response_format={"type":"json_object"}，
                    # 不支持 json_schema，所以这里传 Mapping 而不是 pydantic 类。
                    "response_format": {"type": "json_object"},
                },
            )
            return response.text or ""

        try:
            # 路由函数是同步的，FastAPI 会把它丢到线程池里跑，那一层没有事件循环，
            # 所以这里用 asyncio.run 是安全的（在协程里调用会直接报错，反而更容易发现）。
            return asyncio.run(run())
        except LlmUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            # 不把原始异常往上抛：里面可能带请求体（含用户偏好文本）和密钥相关字段
            logger.warning("调用真实模型失败：%s", type(exc).__name__)
            raise LlmUnavailable(
                f"调用真实模型失败（{type(exc).__name__}）。可以稍后重试，或改用本地规则引擎。"
            ) from exc

    return call


# ---------------------------------------------------------------- 提示词

def _fmt_ranges(ranges: list[tuple]) -> str:
    if not ranges:
        return "（今天没有剩余可用时段）"
    return "、".join(f"{start:%H:%M}-{end:%H:%M}" for start, end in ranges)


def _free_ranges(settings: dict, day, busy: list[dict], now) -> list[tuple]:
    """可用区间；空的话当场失败，不白烧一次模型调用。

    两件事在这里一次说清：

    - 交集为空（工作时段和可活动时段没搭上）→ ``NO_ALLOWED_RANGE``，指向配置本身；
    - 交集有、但剩下的都被占满或已经过去 → ``NO_SLOT_AVAILABLE``，指向今天的时间。

    这两种都不该说成"模型给的时间不满足条件"：模型压根没被调用。
    """
    free = rules.free_ranges(settings, day, busy, now)
    if not free:
        raise rules.RuleViolation(
            "NO_SLOT_AVAILABLE",
            "今天这些时段剩下的时间已经被安排占满，或者已经过去了，没有能放活动的位置。"
            "可以换一个更宽的时段，或者先处理掉今天的安排。",
        )
    return free


def build_prompt(
    settings: dict,
    day,
    busy: list[dict],
    now,
    count: int,
    note: str = "",
    *,
    free: list[tuple] | None = None,
) -> str:
    """拼提示词。

    关键取舍：直接把 ``free_ranges`` 的结果（已经扣掉免打扰、已占用和已经过去的时间）
    作为"只能在这些区间里挑"喂给模型。让模型自己算窗口减免打扰减冲突，
    它算不对的概率很高，而算错的部分后端还要再丢掉一遍，纯浪费一次调用。

    ``free`` 允许调用方传进来，避免 ``plan`` 和这里各算一遍。
    """
    free = _free_ranges(settings, day, busy, now) if free is None else free
    catalog_lines = "\n".join(
        f"- {key}：{title} / {description}"
        for key, (title, description) in rules.ACTIVITY_CATALOG.items()
    )
    busy_lines = (
        "\n".join(
            f"- {rules.parse_dt(row['scheduled_at']):%H:%M}「{row['title']}」"
            f" {row['duration_min']} 分钟"
            for row in busy
        )
        or "（没有）"
    )
    weekday = "一二三四五六日"[day.weekday()]
    # 没填偏好就把整段去掉。留一个"（没特别说明）"的占位符，模型会把它当内容抄进
    # explanation —— 实际出现过「未说明偏好，安排两次轻活动…」，读起来像系统日志泄漏。
    preference = note.strip()
    preference_block = (
        f"""【用户的偏好】
{preference}

偏好用来决定三件事：挑哪一类活动（在上面四类里）、排多密、说明怎么写。
如果偏好落在上面四类之外（球类、健身、跑步、需要场地或器械的，例如"打篮球"），
**不要硬凑**：照常从四类里排，并在 explanation 里如实说明这次没能排进来，
不要声称已经满足了偏好。
"""
        if preference
        else ""
    )

    return f"""你是「工作间隙活动助手」的排程模块。只输出 JSON，不要任何解释性文字、不要 Markdown。

今天是 {day.isoformat()}（周{weekday}），现在是 {now:%H:%M}。
用户希望今天安排 {count} 次短暂活动，每次 5 分钟。

【只能在这些区间内挑时间】
{_fmt_ranges(free)}
区间已扣掉免打扰时段、已有安排和已经过去的时间。每条活动占用 5 分钟，
所以挑的起点必须在区间结束前至少 5 分钟。

【已有的安排】
{busy_lines}

【可选的活动（activity_type 只能取这四者之一）】
{catalog_lines}

{preference_block}
【输出格式】
{{"explanation": "一句话说明你为什么这样排，中文，40 字以内",
  "suggestions": [{{"time": "HH:MM", "activity_type": "walk",
                   "title": "20 字以内的中文标题",
                   "description": "60 字以内的中文说明",
                   "reason": "为什么安排在这个时间，40 字以内"}}]}}

【硬约束】
1. time 必须是上面某个区间内的 HH:MM，且不早于现在；
2. 相邻两条之间至少间隔 15 分钟；
3. 最多 {min(count, MAX_ITEMS)} 条；区间不够就少给，不要凑数；
4. 只描述轻微、无需器械的活动，不写运动处方、不提疾病、不做健康评价；
5. explanation 必须和建议本身一致：不能一边说"按你的偏好安排了打球"、
   一边给的却是走动和拉伸；
6. 中文输出。"""


# ---------------------------------------------------------------- 解析

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def _loads(raw: str) -> dict:
    """从模型回复里抠出 JSON 对象。

    两种常见脏数据都要容：Markdown 代码块包裹、JSON 前后带一句"好的，这是建议："。
    实在解析不出来才算不可用。
    """
    text = (raw or "").strip()
    if not text:
        raise LlmUnavailable("真实模型没有返回内容，可以重试或改用本地规则引擎。")

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise LlmUnavailable(
            "真实模型的回复不是有效 JSON，无法解析成建议。可以重试，或改用本地规则引擎。"
        ) from exc

    if not isinstance(payload, dict):
        raise LlmUnavailable("真实模型返回的 JSON 结构不对（不是对象），无法解析成建议。")
    return payload


def _parse(raw: str) -> tuple[str, list[_Item]]:
    """逐条解析。

    单条格式不对只丢这一条，不整批失败：模型偶尔会多带一个字段或时间写成 "9:05"，
    没必要因此把另外两条能用的建议一起扔掉。
    """
    payload = _loads(raw)
    raw_items = payload.get("suggestions")
    if not isinstance(raw_items, list):
        raise LlmUnavailable("真实模型返回的 JSON 里没有 suggestions 数组。")

    items: list[_Item] = []
    for entry in raw_items[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(_Item.model_validate(entry))
        except ValidationError:
            continue

    explanation = payload.get("explanation")
    if not isinstance(explanation, str):
        explanation = ""
    return explanation.strip()[:MAX_EXPLANATION], items


def _clip(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def _to_draft(item: _Item, day) -> rules.Draft:
    """把模型的一条建议转成草稿。

    标题和说明允许模型自己写（比固定文案贴合用户偏好），但活动类型必须落在活动库里。
    类型不认识时**连文案一起换掉**，不留模型那份：它既然把类型写成 basketball，
    标题多半也在说打球，留着就会出现"类型是走动、标题是去打球"的 5 分钟假满足。
    """
    if item.activity_type in rules.ACTIVITY_CATALOG:
        activity_type = item.activity_type
        fallback_title, fallback_description = rules.ACTIVITY_CATALOG[activity_type]
        title = _clip(item.title, MAX_TITLE) or fallback_title
        description = _clip(item.description, MAX_DESCRIPTION) or fallback_description
        reason = _clip(item.reason, MAX_REASON)
    else:
        activity_type = "walk"
        title, description = rules.ACTIVITY_CATALOG[activity_type]
        reason = ""
    return rules.Draft(
        at=rules.at_time(day, item.time),
        activity_type=activity_type,
        title=title,
        description=description,
        reason=reason,
    )


def plan(
    settings: dict,
    day,
    busy: list[dict],
    now,
    count: int,
    note: str = "",
    *,
    completer: Completer | None = None,
) -> tuple[list[rules.Draft], str, int]:
    """让真实模型排一次，返回 ``(草稿, 解释, 被规则丢掉几条)``。

    抛 ``LlmUnavailable``：模型不可用或回复无法解析（调用方应如实报错，不要静默降级）。
    抛 ``RuleViolation("NO_ALLOWED_RANGE")``：条件自相矛盾，模型不会被调用。
    抛 ``RuleViolation("NO_SLOT_AVAILABLE")``：今天没有剩余位置，模型不会被调用。
    抛 ``RuleViolation("LLM_NO_VALID_SLOT")``：模型确实给了建议，但没有一条通过校验。
    """
    # 先算可用区间：算不出来就直接抛，别把"没位置"伪装成"模型给的时间不合格"。
    # 顺序上放在构造 completer 之前 —— 条件本身有矛盾时，先把配置问题说清楚。
    free = _free_ranges(settings, day, busy, now)
    call = completer or default_completer()
    prompt = build_prompt(settings, day, busy, now, count, note, free=free)
    explanation, items = _parse(call(prompt))

    drafts: list[rules.Draft] = []
    dropped = 0
    seen: set[str] = set()
    # 同一批建议之间也要够间隔。``validate_slot`` 只跟"已有安排"比，
    # 两条草稿彼此隔 10 分钟它是不管的 —— 结果是：建议里看着正常，用户全勾上点确认，
    # 第 2 条被 422（confirm 逐条插库，后一条会把前一条算进 busy）。
    # 把本轮已经收下的草稿也并进 busy，两个入口的判断就一致了。
    accepted: list[dict] = []
    # 按时间顺序处理：撞车时留早的那条，和用户的直觉一致
    for item in sorted(items, key=lambda entry: entry.time):
        if item.time in seen:
            # 同一个时间给两条，等于让用户勾两条一模一样的
            dropped += 1
            continue
        try:
            draft = _to_draft(item, day)
            rules.validate_slot(settings, draft.at, day, busy + accepted, now)
        except (ValueError, rules.RuleViolation):
            dropped += 1
            continue
        seen.add(item.time)
        drafts.append(draft)
        accepted.append(
            {"scheduled_at": rules.iso(draft.at), "duration_min": draft.duration_min}
        )

    if not drafts:
        raise rules.RuleViolation(
            "LLM_NO_VALID_SLOT",
            "真实模型给出的时间都不满足你设置的条件（免打扰、可活动时段或已有安排）。"
            "可以放宽条件后重试，或改用本地规则引擎。",
        )

    drafts.sort(key=lambda draft: draft.at)
    if not explanation:
        explanation = f"按你设置的条件挑了 {len(drafts)} 个时间，已经避开免打扰和已有安排。"
    return drafts, explanation, dropped

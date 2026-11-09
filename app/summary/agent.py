"""用 Agent 把统计数字写成两句人话。

三条边界，决定了这个文件的样子：

1. **数字不由模型产生**。统计已经由 ``stats.daily_stats()`` 算好，
   模型只负责把它叙述出来。所以提示词里只有数字，不放活动标题之类的自由文本 ——
   用户能自己写标题，把自由文本喂给模型，等于给它一个跑偏的入口。
2. **不复用 ``build_agent()``**。那个 Agent 的四个工具靠 ``identity.current_user()``
   取身份，那是 HTTP 请求建立的 ContextVar；Worker 里没有请求，读到 None，
   工具全部失效。所以这里新建一个无工具的 Agent。
3. **模型说的话要过一遍校验**。长度、医疗表述、编造的数字，都拒掉。
   空日编"完成了 3 次"是最容易被审核抓到的错误，必须有程序兜住。
"""

from __future__ import annotations

import json
import logging
import re

from agent_framework import Agent

from app.agent.config import LlmConfigError, LlmSettings, load_llm_settings
from app.summary.template import template_summary

logger = logging.getLogger(__name__)

# 出现这些词的句子不能进日报。任务书明确不要诊断、治疗、用药建议。
BANNED_WORDS = ("诊断", "治疗", "用药", "处方", "药物", "病情")

# 只抓"数字 + 量词"的组合，比如"3 次"、"2 条"。
# 不抓所有数字，是为了不误伤"7 天"、"8 小时"这类正常表达。
COUNT_PATTERN = re.compile(r"(\d+)\s*(?:次|条)")

REVIEW_MAX = 120
SUGGESTION_MAX = 80


class TransientError(RuntimeError):
    """可重试：模型超时、网络抖动、输出不合规。"""


class PermanentError(RuntimeError):
    """不可重试：配置缺失、参数错误。重试多少次都不会好。"""


SUMMARY_INSTRUCTIONS = """你是「久坐活动助手」的日报撰写模块。只依据给定的统计数据写两句中文。

输入是一个 JSON 对象，字段含义：
- planned_total：当天计划了几次活动
- completed / skipped / unfinished：完成、跳过、没处理的次数
- postpone_ops：推迟操作的次数（是操作次数，不是活动次数，不要和完成数相加）
- sedentary_minutes：用户自报的久坐分钟数。为 null 表示没有记录
- has_records：当天有没有任何活动记录

只输出 JSON，不要 Markdown，不要代码块：
{"review": "60 字以内的简短回顾", "suggestion": "40 字以内、今天就能照做的一条建议"}

硬约束：
1. 只能使用给定的数字。不要推算、不要新增任何数字；数据里没有的就不提。
2. has_records 为 false 时，直接说"这一天没有活动记录"，不要编造完成情况。
3. sedentary_minutes 为 null 时必须如实说"未记录久坐情况"。不要说"久坐 0 分钟"，
   也不要声称系统检测过用户是否一直坐着。
4. 不给疾病诊断、治疗方案、用药建议，也不给"每 X 分钟必须起身"这类统一适用的建议。
5. 建议要具体可执行（例如"把下午那一次挪到午休后"），不要写"注意多活动"。
6. 不要出现"作为 AI""根据你的数据"这类套话。"""


def build_prompt(stats: dict) -> str:
    """把统计序列化成紧凑 JSON。

    只放数字和布尔值。``report_date`` 也去掉 —— 模型不需要知道日期，
    多给一个字段就多一次被它写进正文的机会（"11 月 6 日你完成了…"）。
    """
    payload = {k: v for k, v in stats.items() if k != "report_date"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def parse_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象。

    真实模型即使被要求"只输出 JSON"，偶尔也会裹一层 ```json 代码块。
    这里先尝试直接解析，失败再去掉代码块围栏重试一次。
    """
    raw = (text or "").strip()
    if not raw:
        raise TransientError("模型返回空内容")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TransientError(f"模型输出不是合法 JSON：{raw[:80]}") from exc

    if not isinstance(data, dict):
        raise TransientError("模型输出的不是 JSON 对象")
    return data


def _drop_banned(text: str) -> tuple[str, bool]:
    """去掉含医疗表述的句子。返回 (清洗后文本, 是否改动过)。"""
    if not any(word in text for word in BANNED_WORDS):
        return text, False
    kept = [seg for seg in re.split(r"(?<=[。；！？])", text) if not any(w in seg for w in BANNED_WORDS)]
    return "".join(kept).strip(), True


def _fabricated_counts(text: str, allowed: set[int]) -> set[int]:
    """找出正文里出现、但统计里没有的次数。

    只在 has_records 为真的场景用：此时允许出现的次数是 stats 里那几个数字。
    出现别的次数说明模型自己算了或者编了。
    """
    found = {int(m.group(1)) for m in COUNT_PATTERN.finditer(text)}
    return found - allowed


def validate_summary(stats: dict, review: str, suggestion: str) -> tuple[str, str]:
    """校验并清洗模型输出。不合规就抛 ``TransientError`` 让 ARQ 重试。

    长度超限只截断（内容可用，没必要为两个字的超长重跑一次模型）；
    医疗表述按句丢弃；编造的次数直接判失败。
    """
    review = (review or "").strip()
    suggestion = (suggestion or "").strip()
    if not review or not suggestion:
        raise TransientError("模型输出的 review / suggestion 有空值")

    # 空日最容易被编造，单独一条规则拦：出现任何"N 次"且 N > 0 就判失败。
    if not stats.get("has_records"):
        suspicious = {n for n in _fabricated_counts(review + suggestion, {0}) if n > 0}
        if suspicious:
            raise TransientError(f"空日却出现了活动次数 {sorted(suspicious)}，判为编造")
    else:
        allowed = {
            int(stats.get("planned_total", 0)),
            int(stats.get("completed", 0)),
            int(stats.get("skipped", 0)),
            int(stats.get("unfinished", 0)),
            int(stats.get("postpone_ops", 0)),
        }
        fabricated = _fabricated_counts(review + suggestion, allowed)
        if fabricated:
            raise TransientError(f"正文出现统计之外的活动次数 {sorted(fabricated)}，判为编造")

    clean_review, dropped_r = _drop_banned(review)
    clean_suggestion, dropped_s = _drop_banned(suggestion)
    if dropped_r or dropped_s:
        logger.warning("模型输出含医疗表述，已按句丢弃：review=%s suggestion=%s", dropped_r, dropped_s)
    if not clean_review or not clean_suggestion:
        raise TransientError("去掉医疗表述后内容为空")

    return clean_review[:REVIEW_MAX], clean_suggestion[:SUGGESTION_MAX]


def _resolve_settings() -> LlmSettings:
    try:
        return load_llm_settings()
    except LlmConfigError as exc:
        # 配置缺失重试多少次都不会好，归到"永久错误"，不要浪费三次尝试
        raise PermanentError(f"模型配置不完整：{exc}") from exc


async def compose_summary(stats: dict) -> tuple[str, str, str, str]:
    """生成一份总结，返回 (回顾, 建议, 来源, 模型名)。

    ``source`` 只有两个值：``rule``（本地模板）和 ``llm``（真实模型）。
    页面据此标出来源，避免用户把模板当成模型写的。
    """
    settings = _resolve_settings()

    if settings.is_mock:
        review, suggestion = template_summary(stats)
        return review, suggestion, "rule", "template"

    client_or_agent = _build_agent(settings)
    try:
        response = await client_or_agent.run(build_prompt(stats))
    except Exception as exc:  # 不把原始异常往上抛：里面可能带请求体
        raise TransientError(f"模型调用失败（{type(exc).__name__}）") from exc

    text = getattr(response, "text", None) or str(response)
    data = parse_json(text)
    review, suggestion = validate_summary(
        stats, data.get("review", ""), data.get("suggestion", "")
    )
    return review, suggestion, "llm", settings.model or "unknown"


def _build_agent(settings: LlmSettings) -> Agent:
    """构造无工具的总结 Agent。

    不用 ``build_agent()``：那个 Agent 带着四个查询工具，工具依赖请求级的
    当前用户上下文，Worker 进程里拿不到。这里只需要"把数字写成话"这一件事。
    """
    from agent_framework.openai import OpenAIChatCompletionClient

    # 必须用 OpenAIChatCompletionClient（POST /chat/completions）。
    # DeepSeek 不支持 Responses API，换成 OpenAIChatClient 会在第二轮工具结果
    # 回传时报 400。第三天踩过一次，这里沿用同样的选择。
    client = OpenAIChatCompletionClient(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
    return Agent(
        client=client,
        name="daily-summary-writer",
        description="把当天的活动统计写成简短回顾与一条建议",
        instructions=SUMMARY_INSTRUCTIONS,
        # 不给工具：统计已经算好了，多一层工具调用只会多一次模型往返，
        # 也多一个模型自己挑参数调错的机会。
        tools=[],
        default_options={"temperature": 0.3, "max_tokens": 500},
    )

"""Agent 的只读工具。

约束（对应任务说明第 6.2 节与第 7 节）：

- 只读：这条工具只做 SELECT，不新增不修改不删除；
- 真实执行：数据来自 SQLite，不是写死在回复里的字符串；
- 身份来自已验证的登录会话：工具没有 user_id 参数，外部无法让模型查别人的数据；
- 参数自己校验：模型给的值一律当不可信输入处理，不合法就明确拒绝；
- 失败不抛裸异常：查询出错时返回可读提示，避免整轮运行变成 RUN_ERROR；
- 返回内容按不可信数据处理：加数据声明、做长度截断；
- 日志去敏：只记 user id、用户名、条数和错误类型，不记令牌、不记密码、不记标题内容。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from agent_framework import tool

from app.agent.guard import normalize_bool, sanitize_tool_output
from app.agent.identity import current_user
from app.activity import rules as activity_rules
from app.activity import service as activity_service
from app.db import get_conn

logger = logging.getLogger(__name__)

# 工具名只在这里定义一次：Mock 规划调用、评测断言、README 都引用它，
# 避免改名字时漏掉某一处，出现"事件里有调用但工具名对不上"的假失败。
TOOL_NAME = "list_my_todos"

# 工具结果里最多列这么多条，避免把整张表塞进模型上下文
MAX_ITEMS = 50


@tool(
    name=TOOL_NAME,
    description=(
        "读取当前登录用户自己的待办清单（只读，不能新增或修改）。"
        "当用户询问自己的待办、任务、清单、还有什么没做的时候调用。"
        "only_pending=true 只返回未完成的，false 返回全部。"
        "不需要也不能传用户标识：后端按当前登录身份自动限定范围。"
    ),
)
def list_my_todos(
    only_pending: Annotated[
        bool, "true 表示只看未完成的待办；false 表示全部返回"
    ] = False,
) -> str:
    """返回当前登录用户的待办清单。"""
    user = current_user()
    if user is None:
        # 正常路径不会出现：鉴权依赖一定先执行。真出现了说明上下文丢了，
        # 这里明确失败，不要返回任何用户数据。
        logger.warning("list_my_todos 调用时没有登录身份，已拒绝")
        return "当前请求没有可用的登录身份，无法查询待办。"

    # 参数来自模型，属于不可信输入。转成布尔值时也要给出可读的拒绝理由，
    # 而不是猜一个值继续查 —— 猜错了用户看到的是"少了几条"，很难发现。
    only_pending_flag, error = normalize_bool(only_pending, name="only_pending")
    if error:
        logger.info(
            "agent tool rejected args: tool=%s user_id=%s reason=invalid_only_pending",
            TOOL_NAME,
            user["id"],
        )
        return error

    sql = "SELECT title, done FROM items WHERE user_id = ?"
    params: list[object] = [user["id"]]
    if only_pending_flag:
        sql += " AND done = 0"
    sql += " ORDER BY done ASC, id DESC LIMIT ?"
    params.append(MAX_ITEMS)

    try:
        with get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        # 不把异常往外抛：抛出去会让整轮运行以 RUN_ERROR 结束，
        # 用户只看到"运行失败"。返回一句可读提示，模型还能继续对话。
        logger.warning(
            "agent tool failed: tool=%s user_id=%s error=%s",
            TOOL_NAME,
            user["id"],
            type(exc).__name__,
        )
        return "查询待办时出现内部错误，请稍后再试。"

    logger.info(
        "agent tool executed: tool=%s user_id=%s username=%s only_pending=%s rows=%d",
        TOOL_NAME,
        user["id"],
        user["username"],
        only_pending_flag,
        len(rows),
    )

    if not rows:
        scope = "未完成的待办" if only_pending_flag else "待办"
        return f"用户 {user['display_name']} 目前没有{scope}。"

    lines = [
        f"{index}. [{'已完成' if row['done'] else '未完成'}] {row['title']}"
        for index, row in enumerate(rows, start=1)
    ]
    scope = "未完成的待办" if only_pending_flag else "全部待办"
    text = f"用户 {user['display_name']} 的{scope}共 {len(rows)} 条：\n" + "\n".join(lines)

    # 待办标题是用户自己写的，属于不可信内容；这里统一加声明并截断
    return sanitize_tool_output(text)


# ---------------------------------------------------------------- 第五天：活动助手
#
# 三个工具都遵守上面 list_my_todos 的同一套纪律：
#   1. 签名里没有 user_id —— 身份只来自 ContextVar 里的登录会话；
#   2. 失败返回可读中文，不抛裸异常（抛出去会让整轮变成 RUN_ERROR，第四天踩过）；
#   3. 返回值过 sanitize_tool_output，按不可信内容处理；
#   4. 只读或只算，没有任何写库工具 —— 保存必须由用户在页面上确认。

TOOL_NAME_ACTIVITY_SETTINGS = "get_my_activity_settings"
TOOL_NAME_TODAY_ACTIVITIES = "get_my_today_activities"
TOOL_NAME_SUGGEST_SLOTS = "compute_suggested_slots"


@tool(
    name=TOOL_NAME_ACTIVITY_SETTINGS,
    description=(
        "读取当前登录用户的活动时段、免打扰时间与每天希望安排的次数（只读）。"
        "当用户问「我设的时段是什么」「免打扰是什么时候」，或需要为其安排活动时调用。"
    ),
)
def get_my_activity_settings() -> str:
    """返回当前用户的活动设置。"""
    user = current_user()
    if user is None:
        logger.warning("%s 调用时没有登录身份，已拒绝", TOOL_NAME_ACTIVITY_SETTINGS)
        return "当前请求没有可用的登录身份，无法查询活动设置。"

    try:
        settings = activity_service.get_settings(user["id"])
    except sqlite3.Error as exc:
        logger.warning(
            "agent tool failed: tool=%s user_id=%s error=%s",
            TOOL_NAME_ACTIVITY_SETTINGS,
            user["id"],
            type(exc).__name__,
        )
        return "读取活动设置时出现内部错误，请稍后再试。"

    windows = "、".join(f"{w['start']}-{w['end']}" for w in settings["windows"]) or "未设置"
    quiet = (
        f"{settings['quiet_start']}-{settings['quiet_end']}"
        if settings["quiet_start"]
        else "未设置"
    )
    logger.info(
        "agent tool executed: tool=%s user_id=%s daily_target=%s",
        TOOL_NAME_ACTIVITY_SETTINGS,
        user["id"],
        settings["daily_target"],
    )
    return sanitize_tool_output(
        f"工作时段 {settings['work_start']}-{settings['work_end']}；"
        f"可活动时段 {windows}；免打扰 {quiet}；每天 {settings['daily_target']} 次。"
    )


@tool(
    name=TOOL_NAME_TODAY_ACTIVITIES,
    description=(
        "读取当前登录用户今天的活动安排与每条的状态（只读）。"
        "当用户问「今天还剩几条」「我完成了没有」时调用。"
    ),
)
def get_my_today_activities() -> str:
    """返回今天的活动安排。"""
    user = current_user()
    if user is None:
        logger.warning("%s 调用时没有登录身份，已拒绝", TOOL_NAME_TODAY_ACTIVITIES)
        return "当前请求没有可用的登录身份，无法查询活动安排。"

    try:
        rows = activity_service.list_day(user["id"], activity_rules.now_local().date())
    except sqlite3.Error as exc:
        logger.warning(
            "agent tool failed: tool=%s user_id=%s error=%s",
            TOOL_NAME_TODAY_ACTIVITIES,
            user["id"],
            type(exc).__name__,
        )
        return "查询今天的活动安排时出现内部错误，请稍后再试。"

    logger.info(
        "agent tool executed: tool=%s user_id=%s rows=%d",
        TOOL_NAME_TODAY_ACTIVITIES,
        user["id"],
        len(rows),
    )

    if not rows:
        return f"用户 {user['display_name']} 今天还没有安排活动。"

    lines = [
        f"{index}. {activity_rules.parse_dt(row['scheduled_at']):%H:%M} "
        f"{row['title']}（{activity_rules.STATUS_LABELS.get(row['status'], row['status'])}）"
        for index, row in enumerate(rows, start=1)
    ]
    return sanitize_tool_output(
        f"用户 {user['display_name']} 今天共 {len(rows)} 条安排：\n" + "\n".join(lines)
    )


@tool(
    name=TOOL_NAME_SUGGEST_SLOTS,
    description=(
        "在用户的可活动时段内计算候选安排时间，只计算、不保存。"
        "count 是希望安排的次数（1-8）。"
        "返回的时间已经避开免打扰时段与已有活动，用户确认后由页面保存。"
    ),
)
def compute_suggested_slots(count: int = 3) -> str:
    """计算候选安排时间，不写库。"""
    user = current_user()
    if user is None:
        logger.warning("%s 调用时没有登录身份，已拒绝", TOOL_NAME_SUGGEST_SLOTS)
        return "当前请求没有可用的登录身份，无法计算建议时间。"

    # 参数来自模型，属于不可信输入：不合法就说明白，不要猜一个值继续算
    try:
        count_value = int(count)
    except (TypeError, ValueError):
        return "次数需要是 1 到 8 之间的整数。"
    if not 1 <= count_value <= 8:
        return "次数需要是 1 到 8 之间的整数。"

    try:
        drafts, feasible, _ = activity_service.suggest_day(user["id"], count_value)
    except activity_rules.RuleViolation as exc:
        logger.info(
            "agent tool rejected: tool=%s user_id=%s code=%s",
            TOOL_NAME_SUGGEST_SLOTS,
            user["id"],
            exc.code,
        )
        return f"暂时算不出合适的时段：{exc.message}"

    logger.info(
        "agent tool executed: tool=%s user_id=%s count=%s feasible=%s",
        TOOL_NAME_SUGGEST_SLOTS,
        user["id"],
        count_value,
        feasible,
    )

    lines = [
        f"{index}. {draft.at:%H:%M} {draft.title}：{draft.reason}"
        for index, draft in enumerate(drafts, start=1)
    ]
    note = "" if feasible == count_value else f"（只找到 {feasible} 个合适时段）"
    return sanitize_tool_output(f"建议这样安排{note}：\n" + "\n".join(lines))

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

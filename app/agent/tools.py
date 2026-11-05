"""Agent 的只读工具。

约束（对应任务说明第 6.2 节）：
- 只读：这条工具只做 SELECT，不新增不修改不删除；
- 真实执行：数据来自 SQLite，不是写死在回复里的字符串；
- 身份来自已验证的登录会话：工具没有 user_id 参数，外部无法让模型查别人的数据；
- 日志去敏：只记 user id、用户名和条数，不记令牌、不记密码。
"""

from __future__ import annotations

import logging
from typing import Annotated

from agent_framework import tool

from app.agent.identity import current_user
from app.db import get_conn

logger = logging.getLogger(__name__)

# 工具结果里最多列这么多条，避免把整张表塞进模型上下文
MAX_ITEMS = 50


@tool(
    name="list_my_todos",
    description=(
        "读取当前登录用户自己的待办清单（只读，不能新增或修改）。"
        "当用户询问自己的待办、任务、清单、还有什么没做的时候调用。"
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

    sql = "SELECT title, done FROM items WHERE user_id = ?"
    params: list[object] = [user["id"]]
    if only_pending:
        sql += " AND done = 0"
    sql += " ORDER BY done ASC, id DESC LIMIT ?"
    params.append(MAX_ITEMS)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    logger.info(
        "agent tool executed: tool=list_my_todos user_id=%s username=%s only_pending=%s rows=%d",
        user["id"],
        user["username"],
        only_pending,
        len(rows),
    )

    if not rows:
        scope = "未完成的待办" if only_pending else "待办"
        return f"用户 {user['display_name']} 目前没有{scope}。"

    lines = [
        f"{index}. [{'已完成' if row['done'] else '未完成'}] {row['title']}"
        for index, row in enumerate(rows, start=1)
    ]
    scope = "未完成的待办" if only_pending else "全部待办"
    return (
        f"用户 {user['display_name']} 的{scope}共 {len(rows)} 条：\n"
        + "\n".join(lines)
    )

"""mock 模式下的日报模板。

为什么 mock 不走 ``MockChatClient``：

那个假模型是给对话界面演示打字用的，输出的是和待办相关的固定话术。
把它塞进日报，等于库里存了一段跟当天记录毫无关系的内容 —— 那就是编造。

这里的做法是用**真实统计填模板**，``source='rule'``，页面上标「本地模板」。
这样离线也能跑通全流程，而且每一句话都能在数据库里找到依据，
和第五天"规则引擎 / DeepSeek"双引擎的处理方式一致。
"""

from __future__ import annotations


def template_summary(stats: dict) -> tuple[str, str]:
    """返回 (回顾, 建议)。两种分支都必须只使用 ``stats`` 里已有的数字。"""
    if not stats.get("has_records"):
        return (
            "这一天没有活动记录，无法判断实际久坐情况。",
            "如果想让明天有数据，先在页面上点一次「生成安排」排几次活动。",
        )

    review = (
        f"计划 {stats['planned_total']} 次，完成 {stats['completed']} 次，"
        f"跳过 {stats['skipped']} 次，推迟 {stats['postpone_ops']} 次操作。"
    )
    if stats["unfinished"] > 0:
        review += f"另有 {stats['unfinished']} 次没有处理。"

    # 建议从记录本身推出来，不用固定套话：完成率低就提"减少次数"，
    # 完成得好就提"保持"。这样模板也不会说出和数字矛盾的话。
    if stats["completed"] == 0:
        suggestion = "今天一次都没完成，先把每天的次数调到 1-2 次，比硬排 3 次更容易做到。"
    elif stats["unfinished"] > stats["completed"]:
        suggestion = "把最常被跳过的那一次挪到午休后，那个时段通常更容易起身。"
    else:
        suggestion = "保持现在的节奏，把完成得最晚的那一次提前半小时。"

    return review, suggestion

"""评测运行环境：临时库 + 虚构账号与数据。

为什么单独一个模块：``app.main`` 在**导入时**就会构建 Agent 并注册 AG-UI 端点，
模式（mock / 真实模型）和数据库路径必须在导入之前定死。所以这里只做准备、
不导入 app，把「先设环境变量、再 import」的顺序交给调用方（runner.py、framework_eval.py）。

数据全部是虚构的，不含任何真实用户聊天或个人数据；每次运行用一个全新的临时库，
重复跑不会互相污染，也不会碰到仓库里的 ``data/app.db``。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 评测使用一套独立账号，与 README 里的练习账号区分开，
# 避免以后有人拿演示库的数据对照评测结果时对不上。
EVAL_USER = {"username": "eval-a", "password": "eval-a-1234", "display_name": "评测用户 A"}
EVAL_OTHER = {"username": "eval-b", "password": "eval-b-1234", "display_name": "评测用户 B"}

# 虚构待办：标题里带可检索的关键片段，方便用 must_contain 做客观判定。
# 刻意让两个用户各有一条「已完成」，这样 only_pending 的过滤效果可验证。
EVAL_ITEMS = {
    EVAL_USER["username"]: [
        ("整理第三天的交互图", False),
        ("复习 AG-UI 事件类型", False),
        ("提交第二天的作业", True),
    ],
    EVAL_OTHER["username"]: [
        ("另一个用户的进行中事项", False),
        ("另一个用户的已完成事项", True),
    ],
}


def prepare_environment() -> Path:
    """建临时库、灌虚构数据，返回数据库路径。

    必须在导入 ``app.main`` 之前调用：Agent 端点在导入时构建，
    那时读到的 ``APP_LLM_MODE`` 就决定了本次评测是 Mock 还是真实模型。
    """
    # 评测默认走 Mock：没有模型凭证时也能跑，且结果可复现。
    # 如果开发机真的配了真实模型凭证，用 EVAL_MODE=real 显式覆盖，避免误用额度。
    os.environ["APP_LLM_MODE"] = os.environ.get("EVAL_MODE", "mock").strip() or "mock"

    # 评测关心「首字时间 / 总耗时」，保留一点间隔更像真实流式；
    # 但不按 50ms 打字速度跑，否则 13 条用例要多等十几秒。
    os.environ.setdefault("APP_MOCK_CHUNK_DELAY", "0.002")

    root = Path(tempfile.mkdtemp(prefix="agent-eval-"))
    db_path = root / "eval.db"
    os.environ["APP_DB_PATH"] = str(db_path)

    # 这两个导入必须在环境变量之后，且不经过 app.main
    from app.db import get_conn, init_db
    from app.security import hash_password

    init_db()

    users: dict[str, int] = {}
    with get_conn() as conn:
        for spec in (EVAL_USER, EVAL_OTHER):
            cursor = conn.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                (spec["username"], spec["display_name"], hash_password(spec["password"])),
            )
            users[spec["username"]] = int(cursor.lastrowid)

        for username, items in EVAL_ITEMS.items():
            for title, done in items:
                conn.execute(
                    "INSERT INTO items (user_id, title, done, created_at) VALUES (?, ?, ?, ?)",
                    (users[username], title, 1 if done else 0, 0),
                )

    return db_path

"""创建/重置练习账号。

用法（在仓库根目录执行）：
    uv run python scripts/seed.py

可用环境变量覆盖默认值：SEED_USERNAME、SEED_PASSWORD、SEED_DISPLAY_NAME。
脚本可重复执行，已存在时只重置密码和显示名，不会产生重复账号。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import get_conn, init_db  # noqa: E402
from app.security import hash_password  # noqa: E402


def main() -> int:
    username = os.environ.get("SEED_USERNAME", "demo").strip()
    password = os.environ.get("SEED_PASSWORD", "demo1234")
    display_name = os.environ.get("SEED_DISPLAY_NAME", "练习用户")

    if not username or not password:
        print("SEED_USERNAME 与 SEED_PASSWORD 不能为空", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("SEED_PASSWORD 至少 8 位，避免使用过弱密码", file=sys.stderr)
        return 1

    init_db()
    password_hash = hash_password(password)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                (username, display_name, password_hash),
            )
            action = "已创建"
        else:
            conn.execute(
                "UPDATE users SET display_name = ?, password_hash = ? WHERE id = ?",
                (display_name, password_hash, row["id"]),
            )
            action = "已重置"

        # 重置密码时顺手清掉该用户的旧会话，避免旧令牌继续可用。
        if row is not None:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    print(f"{action}练习账号：{username}（密码请见 README 的练习账号说明）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

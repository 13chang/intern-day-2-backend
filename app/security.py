"""密码哈希与会话令牌。

- 密码：pwdlib 的 Argon2 哈希，不保存明文。
- 会话：随机不透明令牌，数据库只存 SHA-256 摘要；
  令牌明文仅在登录响应中出现一次，退出时删除对应记录即失效。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()


def session_ttl_seconds() -> int:
    """会话有效期，默认 30 分钟，可用 APP_SESSION_TTL_SECONDS 调整。"""
    try:
        return max(30, int(os.environ.get("APP_SESSION_TTL_SECONDS", "1800")))
    except ValueError:
        return 1800


def hash_password(raw_password: str) -> str:
    return password_hasher.hash(raw_password)


def verify_password(raw_password: str, stored_hash: str) -> bool:
    try:
        return password_hasher.verify(raw_password, stored_hash)
    except Exception:
        # 哈希格式异常时按校验失败处理，避免把内部错误暴露给调用方。
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> tuple[str, str, int]:
    """生成会话令牌。

    返回 (明文令牌, 存储用摘要, 过期时间戳)；明文只返回给前端一次。
    """
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + session_ttl_seconds()
    return token, token_digest(token), expires_at

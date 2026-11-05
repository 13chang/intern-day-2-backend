"""密码哈希与会话令牌的单元测试。

这一层不经过 HTTP，直接验证 security.py 的契约。
"""

from __future__ import annotations

import time

from app.security import (
    hash_password,
    issue_token,
    session_ttl_seconds,
    token_digest,
    verify_password,
)


def test_hash_password_is_not_plaintext():
    raw = "demo1234"
    hashed = hash_password(raw)

    assert hashed != raw
    assert raw not in hashed
    # Argon2 的标准前缀，用于确认用的确实是 pwdlib 的 Argon2 而不是随便一个摘要。
    assert hashed.startswith("$argon2")


def test_hash_password_is_salted():
    """同一密码两次哈希结果必须不同，否则说明没加盐。"""
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password_accepts_correct_password():
    hashed = hash_password("demo1234")

    assert verify_password("demo1234", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("demo1234")

    assert verify_password("demo12345", hashed) is False
    assert verify_password("", hashed) is False


def test_verify_password_rejects_broken_hash():
    """哈希格式异常时应返回 False，而不是把内部异常抛给调用方。"""
    assert verify_password("demo1234", "not-a-valid-hash") is False


def test_token_digest_is_sha256_hex():
    digest = token_digest("some-token")

    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
    # 同样输入必须得到同样摘要，否则无法用摘要查会话。
    assert digest == token_digest("some-token")
    assert digest != token_digest("some-token ")


def test_issue_token_returns_plaintext_digest_and_expiry():
    before = int(time.time())
    token, digest, expires_at = issue_token()

    assert token
    assert digest == token_digest(token)
    assert token not in digest
    assert expires_at >= before + session_ttl_seconds() - 1


def test_issue_token_is_random():
    first, _, _ = issue_token()
    second, _, _ = issue_token()

    assert first != second


def test_session_ttl_default_is_1800(monkeypatch):
    monkeypatch.delenv("APP_SESSION_TTL_SECONDS", raising=False)

    assert session_ttl_seconds() == 1800


def test_session_ttl_can_be_overridden(monkeypatch):
    monkeypatch.setenv("APP_SESSION_TTL_SECONDS", "120")

    assert session_ttl_seconds() == 120


def test_session_ttl_has_lower_bound(monkeypatch):
    """小于 30 的值会被抬到 30，避免配置成 0 导致令牌立刻失效。"""
    monkeypatch.setenv("APP_SESSION_TTL_SECONDS", "5")

    assert session_ttl_seconds() == 30


def test_session_ttl_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("APP_SESSION_TTL_SECONDS", "abc")

    assert session_ttl_seconds() == 1800

"""Sprint 6 X / B2: tests for ``users.local_password_disabled``.

行為合約：
- 預設 False → 本機密碼登入流程不受影響。
- True → ``authenticate_user`` 回傳 ``LOCAL_PASSWORD_DISABLED_SENTINEL``，
  ``/api/auth/login`` 端應回 403 並寫 audit log。
- 其他失敗（密碼錯、帳號停用、待核可）的優先序高於這個 flag — 不要讓
  「密碼錯」與「flag 開」回不同錯誤碼，避免被當成探測 oracle。

注意：既有 conftest 因 ``platform_links.required_roles`` 用 JSONB 而無法
在 SQLite 上 create_all，所以本檔不依賴 ``db`` fixture，改用 unittest.mock
直接模擬 query/filter/first 鏈以測 ``authenticate_user`` 的判斷邏輯。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.auth_service import (
    LOCAL_PASSWORD_DISABLED_SENTINEL,
    PENDING_APPROVAL_SENTINEL,
    authenticate_user,
)
from app.utils.security import hash_password


def _make_user(**overrides):
    defaults = dict(
        id=1,
        username="alice",
        hashed_password=hash_password("pw"),
        role="user",
        is_active=True,
        is_approved=True,
        local_password_disabled=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _stub_session(user):
    """Build a SQLAlchemy-shaped Session whose ``.query(User).filter(...).first()`` returns ``user``."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user
    return session


def test_default_flag_allows_local_login():
    user = _make_user()
    result = authenticate_user(_stub_session(user), "alice", "pw")
    assert result is user


def test_local_password_disabled_blocks_login():
    user = _make_user(local_password_disabled=True)
    result = authenticate_user(_stub_session(user), "alice", "pw")
    assert result == LOCAL_PASSWORD_DISABLED_SENTINEL


def test_wrong_password_takes_precedence():
    """密碼錯時不應透露『flag 開』給攻擊者 — 一律回 None。"""
    user = _make_user(local_password_disabled=True)
    result = authenticate_user(_stub_session(user), "alice", "wrong-pw")
    assert result is None


def test_pending_approval_takes_precedence_over_disabled():
    """未核可的優先級高於 SSO-only — 用 admin 視角檢查 ordering。"""
    user = _make_user(local_password_disabled=True, is_approved=False)
    result = authenticate_user(_stub_session(user), "alice", "pw")
    assert result == PENDING_APPROVAL_SENTINEL


def test_inactive_user_returns_none_regardless_of_flag():
    user = _make_user(local_password_disabled=True, is_active=False)
    result = authenticate_user(_stub_session(user), "alice", "pw")
    assert result is None


def test_unknown_user_returns_none():
    """確認 stub 行為：找不到 user 就走 ``not user`` early-return。"""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    result = authenticate_user(session, "noone", "pw")
    assert result is None

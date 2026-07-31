# -*- coding: utf-8 -*-
"""Auth-path naive/aware comparisons must not raise.

Covers API-key expiry (the 500 that surfaced X.3) and conversation-share
expiry (same pattern). ``_as_utc`` / ``as_utc`` make both sides comparable;
dropping the helper without converting every reader back to naive would
reintroduce TypeError on the auth path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.services.api_key_service import _as_utc, validate_api_key
from app.services.conversation_service import _as_utc as share_as_utc
from app.services.conversation_service import _share_expired
from app.time_utils import as_utc, is_expired


def test_as_utc_shared_helper_matches_wrappers() -> None:
    naive = datetime(2026, 7, 30, 8, 0, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert as_utc(naive) == aware
    assert _as_utc(naive) == aware
    assert share_as_utc(naive) == aware
    assert as_utc(aware) == aware


def test_is_expired_accepts_naive_and_aware() -> None:
    past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    future_aware = datetime.now(timezone.utc) + timedelta(hours=1)
    assert is_expired(past_naive) is True
    assert is_expired(future_aware) is False
    assert is_expired(None) is False


def test_validate_api_key_naive_expiry_does_not_raise() -> None:
    """Auth path: naive expires_at vs aware now must not TypeError."""
    future_naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    api_key = MagicMock()
    api_key.is_active = True
    api_key.expires_at = future_naive
    api_key.user = MagicMock(is_active=True)
    api_key.last_used_at = None

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = api_key

    result = validate_api_key(db, "sk-test-not-a-real-key")
    assert result is api_key
    db.commit.assert_called()


def test_validate_api_key_expired_naive_returns_none() -> None:
    past_naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    api_key = MagicMock()
    api_key.is_active = True
    api_key.expires_at = past_naive
    api_key.user = MagicMock(is_active=True)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = api_key

    assert validate_api_key(db, "sk-test-expired") is None


def test_share_expired_naive_does_not_raise() -> None:
    share = MagicMock()
    share.expires_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(
        tzinfo=None
    )
    assert _share_expired(share) is True

    share.expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(
        tzinfo=None
    )
    assert _share_expired(share) is False


def test_raw_naive_aware_comparison_still_raises() -> None:
    """Pin the failure mode — revert of _as_utc without this would go green wrongly."""
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    with pytest.raises(TypeError):
        _ = naive < datetime.now(timezone.utc)

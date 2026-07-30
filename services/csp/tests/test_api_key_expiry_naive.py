"""An API key with an expiry must authenticate, not crash.

`api_keys.expires_at` is TIMESTAMP WITHOUT TIME ZONE, so a stored value comes
back naive; comparing it with `datetime.now(timezone.utc)` raises TypeError.
That is an unhandled 500 on the FIRST request any expiring key makes — not a
401 — and it stays hidden because keys with a NULL expiry short-circuit.
"""
from datetime import datetime, timedelta, timezone

from app.services.api_key_service import _as_utc


def test_naive_expiry_compares_without_raising():
    naive_future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    # This is the comparison validate_api_key performs.
    assert _as_utc(naive_future) > datetime.now(timezone.utc)


def test_naive_past_expiry_is_expired():
    naive_past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    assert _as_utc(naive_past) < datetime.now(timezone.utc)


def test_aware_value_is_left_alone():
    aware = datetime.now(timezone.utc) + timedelta(days=1)
    assert _as_utc(aware) is aware


def test_raw_naive_comparison_is_the_bug_being_fixed():
    """Pin the failure mode itself, so nobody 'simplifies' the helper away."""
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    try:
        naive < datetime.now(timezone.utc)
    except TypeError:
        return
    raise AssertionError("expected naive/aware comparison to raise TypeError")

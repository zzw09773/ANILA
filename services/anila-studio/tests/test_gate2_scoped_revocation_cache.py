"""G16 scoped revocation propagation tests for the Studio consumer."""
from __future__ import annotations

from hashlib import sha256

from app.services.revocation_cache import RevocationCache


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


async def test_jti_revocation_does_not_widen_to_other_token():
    cache = RevocationCache()
    assert cache._apply_payload(
        {
            "user_id": 7,
            "revoked_at_version": 0,
            "scope": "jti",
            "token_jti_hash": _digest("access-a"),
        }
    )

    assert await cache.is_revoked(
        7, 0, jti="access-a", sid="session-a"
    ) is True
    assert await cache.is_revoked(
        7, 0, jti="access-b", sid="session-a"
    ) is False


async def test_sid_revocation_survives_cross_instance_readback_shape():
    cache = RevocationCache()
    cold_start_row = {
        "user_id": 9,
        "revoked_at_version": 0,
        "scope": "sid",
        "session_id_hash": _digest("session-revoked"),
        "ts": "2026-07-12T00:00:00Z",
    }
    assert cache._apply_payload(cold_start_row)

    assert await cache.is_revoked(
        9, 0, jti="new-access", sid="session-revoked"
    ) is True
    assert await cache.is_revoked(
        9, 0, jti="new-access", sid="other-session"
    ) is False

"""Tests for the Redis pub/sub bridge that broadcasts token-revocation
events to downstream services (anila-studio cold-start sync + live
push channel).

The csp/backend is the *publisher* side of the broadcast. The DB bump
+ `token_revocations` insert are the durable source-of-truth (cold
start replays them via ``GET /api/auth/revocations?since=...``);
Redis pub/sub is best-effort low-latency notify.

Design choices pinned by these tests:

* Publish failure (Redis down, network blip, etc.) MUST NOT fail the
  HTTP response. The user's logout / password-change / admin-revoke
  request still returns 2xx because the DB write already happened —
  anila-studio will catch up on its next cold-start poll. This is the
  "csp publisher = weak guarantee" half of the R14 trade-off; the
  studio side will be fail-closed (503 if Redis unreachable).

* Publisher MUST be called AFTER the DB commit, never before. If we
  flipped the order, Redis could broadcast a revocation that ends up
  rolled back, and subscribers would over-invalidate.

* Connection is lazy-initialised — importing the publisher MUST NOT
  open a socket. Required so pytest collection on a machine with no
  Redis still works, and so we don't pin a Redis client to the wrong
  event loop.

Test strategy: monkeypatch the publisher's internal client factory so
each test gets a recording fake instead of a real Redis connection.
``fakeredis`` is not in the requirements file (avoiding a new dep);
the recording stub is enough for these contracts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

# Same opt-in dance other client-based tests use; the startup security
# gate refuses to boot under the dev SECRET_KEY otherwise.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_user


# ---------------------------------------------------------------------------
# Recording fake — captures every publish(channel, message) call.
# ---------------------------------------------------------------------------


class _RecordingRedis:
    """Async stand-in for ``redis.asyncio.Redis``.

    Records every publish() call. ``subscriber_count`` mimics the real
    return value (number of subscribers that received the message). We
    return 0 by default — the publisher must NOT treat 0 as an error.
    """

    def __init__(self, subscriber_count: int = 0, raise_on_publish: BaseException | None = None):
        self.published: list[tuple[str, str]] = []
        self.subscriber_count = subscriber_count
        self.raise_on_publish = raise_on_publish
        self.closed = False

    async def publish(self, channel: str, message: str) -> int:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((channel, message))
        return self.subscriber_count

    async def aclose(self) -> None:  # redis>=5 API
        self.closed = True

    # Older API alias some libraries also accept; harmless to expose.
    async def close(self) -> None:
        self.closed = True


class _RecordingSyncRedis:
    """Sync stand-in used by synchronous FastAPI endpoints."""

    def __init__(
        self,
        subscriber_count: int = 0,
        raise_on_publish: BaseException | None = None,
    ):
        self.published: list[tuple[str, str]] = []
        self.subscriber_count = subscriber_count
        self.raise_on_publish = raise_on_publish
        self.closed = False

    def publish(self, channel: str, message: str) -> int:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((channel, message))
        return self.subscriber_count

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def recording_redis(monkeypatch) -> _RecordingRedis:
    """Install a recording fake in place of the real Redis client.

    The publisher module exposes ``_get_redis_client`` (an async
    factory) for exactly this seam. Monkeypatching the public symbol
    means the fixture works regardless of whether the publisher is
    imported through routes or directly.
    """
    fake = _RecordingRedis()

    async def _factory(redis_url: str | None = None):
        return fake

    from app.services import token_revocation_publisher

    # Wipe any cached singleton from previous tests in the same session.
    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)
    return fake


@pytest.fixture
def recording_sync_redis(monkeypatch) -> _RecordingSyncRedis:
    fake = _RecordingSyncRedis()

    from app.services import token_revocation_publisher

    monkeypatch.setattr(
        token_revocation_publisher,
        "_make_sync_redis_client",
        lambda redis_url=None: fake,
    )
    return fake


# ---------------------------------------------------------------------------
# Direct publisher unit tests.
# ---------------------------------------------------------------------------


def test_publish_revocation_emits_expected_envelope(recording_redis):
    """Single call must produce one publish with the documented schema."""
    from app.services.token_revocation_publisher import publish_revocation

    asyncio.run(publish_revocation(user_id=42, revoked_at_version=3))

    assert len(recording_redis.published) == 1
    channel, message = recording_redis.published[0]
    assert channel == "anila:auth:token-revoke"

    payload = json.loads(message)
    assert payload["user_id"] == 42
    assert payload["revoked_at_version"] == 3
    assert payload["schema_version"] == 2
    assert payload["scope"] == "user_version"
    # ts is ISO-8601 UTC, parseable.
    ts = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None


def test_publish_revocation_swallows_redis_failure(monkeypatch, caplog):
    """Redis blowing up MUST NOT raise — that's the whole point of
    best-effort publish. The DB row is the durable record."""
    from app.services import token_revocation_publisher

    fake = _RecordingRedis(raise_on_publish=ConnectionError("redis down"))

    async def _factory(redis_url: str | None = None):
        return fake

    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)

    # Capture error logs but expect NO exception.
    import logging
    caplog.set_level(logging.ERROR, logger="app.services.token_revocation_publisher")
    asyncio.run(token_revocation_publisher.publish_revocation(user_id=7, revoked_at_version=1))

    # Connection error was logged but did NOT propagate.
    assert any("revoke" in rec.message.lower() or "redis" in rec.message.lower()
               for rec in caplog.records), caplog.text


def test_publish_revocation_handles_factory_failure(monkeypatch, caplog):
    """If even building the client raises (URL malformed, missing
    library, etc.), publish_revocation still returns cleanly."""
    from app.services import token_revocation_publisher

    async def _factory(redis_url: str | None = None):
        raise RuntimeError("cannot build redis client")

    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)

    import logging
    caplog.set_level(logging.ERROR, logger="app.services.token_revocation_publisher")
    asyncio.run(token_revocation_publisher.publish_revocation(user_id=11, revoked_at_version=2))
    assert caplog.records, "expected an error log entry on factory failure"


def test_publish_revocation_sync_uses_fresh_client_per_call(monkeypatch):
    """Sync endpoints must not reuse a redis.asyncio client bound to a closed
    asyncio.run() loop. Each sync publish gets and closes a sync client.
    """
    from app.services import token_revocation_publisher

    clients: list[_RecordingSyncRedis] = []

    def factory(redis_url: str | None = None):
        client = _RecordingSyncRedis()
        clients.append(client)
        return client

    monkeypatch.setattr(
        token_revocation_publisher,
        "_make_sync_redis_client",
        factory,
    )

    token_revocation_publisher.publish_revocation_sync(
        user_id=1, revoked_at_version=2
    )
    token_revocation_publisher.publish_revocation_sync(
        user_id=1, revoked_at_version=3
    )

    assert len(clients) == 2
    assert [len(client.published) for client in clients] == [1, 1]
    assert all(client.closed for client in clients)


def test_make_sync_redis_client_sets_timeouts(monkeypatch):
    """A stuck Redis connection must not hang a sync worker thread."""
    from app.services import token_revocation_publisher

    captured: dict[str, Any] = {}

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _RecordingSyncRedis()

    monkeypatch.setitem(sys.modules, "redis", FakeRedisModule)

    client = token_revocation_publisher._make_sync_redis_client(
        "redis://example.test:6379/0"
    )

    assert isinstance(client, _RecordingSyncRedis)
    assert captured["url"] == "redis://example.test:6379/0"
    assert captured["socket_connect_timeout"] == 2.0
    assert captured["socket_timeout"] == 2.0


# ---------------------------------------------------------------------------
# Integration: HTTP endpoints trigger publish + DB write.
# ---------------------------------------------------------------------------


def _bump_call(recording_redis: _RecordingRedis | _RecordingSyncRedis) -> dict[str, Any]:
    assert len(recording_redis.published) == 1, (
        f"expected exactly one publish, got {len(recording_redis.published)}"
    )
    _, message = recording_redis.published[0]
    return json.loads(message)


def test_logout_triggers_publish_after_bump(
    client: TestClient, db, recording_sync_redis
):
    """Logout publishes a focused sid revocation without widening scope."""
    user = make_user(db, username="logout-user", role="user")
    initial_version = user.token_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "logout-user", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200, resp.text

    # The event carries the current version only for compatibility; scope=sid
    # tells consumers not to invalidate the user's other sessions.
    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == user.id
    assert payload["revoked_at_version"] == initial_version
    assert payload["scope"] == "sid"
    assert len(payload["session_id_hash"]) == 64


def test_password_change_triggers_publish(
    client: TestClient, db, recording_sync_redis
):
    """Changing the password also bumps token_version → must publish."""
    user = make_user(db, username="pw-user", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "pw-user", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["access_token"]

    resp = client.put(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": "password", "new_password": "Newpassw0rd!"},
    )
    assert resp.status_code == 200, resp.text

    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == user.id
    # Original was 0; pw change bumps once.
    assert payload["revoked_at_version"] == 1


def test_admin_revoke_triggers_publish_and_bump(
    client: TestClient, db, recording_sync_redis
):
    """POST /api/auth/revoke is admin-only and force-bumps the target's
    token_version. Verifies bump, DB row insert, and publish all fire."""
    make_user(db, username="admin-rev", role="admin")
    target = make_user(db, username="target-1", role="user")
    initial_version = target.token_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-rev", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    admin_access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {admin_access}"},
        json={"user_id": target.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == target.id
    assert body["revoked_at_version"] == initial_version + 1

    # DB bump
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == target.id).first()
    assert refreshed.token_version == initial_version + 1

    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == target.id
    assert payload["revoked_at_version"] == initial_version + 1


def test_admin_revoke_forbidden_for_normal_user(
    client: TestClient, db, recording_sync_redis
):
    """A regular user must not be able to call admin revoke. The
    request must short-circuit BEFORE bump / publish happens."""
    make_user(db, username="rando", role="user")
    target = make_user(db, username="target-2", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "rando", "password": "password"},
    )
    assert resp.status_code == 200
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {access}"},
        json={"user_id": target.id},
    )
    assert resp.status_code == 403, resp.text

    # No publish, no bump.
    assert recording_sync_redis.published == []
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == target.id).first()
    assert refreshed.token_version == 0


def test_admin_revoke_404_unknown_user(
    client: TestClient, db, recording_sync_redis
):
    make_user(db, username="admin-404", role="admin")
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-404", "password": "password"},
    )
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {access}"},
        json={"user_id": 999_999},
    )
    assert resp.status_code == 404, resp.text
    assert recording_sync_redis.published == []


def test_publish_failure_does_not_break_logout(client: TestClient, db, monkeypatch):
    """End-to-end resilience: if Redis is unreachable, logout still
    returns 200 because the DB bump already happened."""
    user = make_user(db, username="resilient", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "resilient", "password": "password"},
    )
    access = resp.json()["access_token"]

    from app.services import token_revocation_publisher

    fake = _RecordingSyncRedis(raise_on_publish=ConnectionError("redis down"))

    def _factory(redis_url: str | None = None):
        return fake

    monkeypatch.setattr(
        token_revocation_publisher, "_make_sync_redis_client", _factory
    )

    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200, resp.text

    # Durable sid revocation still happened even though publish blew up.
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == user.id).first()
    assert refreshed.token_version == 0
    from app.models.token_revocation import TokenRevocation
    row = db.query(TokenRevocation).filter_by(user_id=user.id, scope="sid").one()
    assert len(row.session_id_hash) == 64

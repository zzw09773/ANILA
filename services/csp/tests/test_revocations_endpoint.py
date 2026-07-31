"""Tests for ``GET /api/auth/revocations?since=<ISO8601>``.

This is the cold-start sync endpoint anila-studio (and any future
service that consumes csp JWTs) hits on boot, before subscribing to
the live ``anila:auth:token-revoke`` Redis channel.

Why a separate endpoint instead of just consuming the channel: Redis
pub/sub is fire-and-forget. Subscribers that boot AFTER a publish
miss it permanently. The durable ``token_revocations`` table backs
this endpoint so a fresh subscriber can replay every revocation
within the retention window and rebuild its in-memory deny list
deterministically.

Contract pinned:

* Auth: ``X-CSP-Service-Token`` header (service-to-service). Legacy
  env-var fallback path is also accepted (it's what the migration
  backfills for existing fleets). Missing / invalid token = 401.
  ``Authorization: Bearer <admin-JWT>`` is NOT an alternative —
  keeping a single auth path simplifies anila-studio's client.

* Query param ``since`` is REQUIRED, ISO-8601, treated as UTC if no
  timezone supplied. 422 on malformed input.

* Response body shape:
      {
        "revocations": [{"user_id": int,
                          "revoked_at_version": int,
                          "ts": "<ISO8601>"}, ...],
        "retention_days": 30
      }
  Sorted by ``ts`` ASC so subscribers can replay in order. Empty
  array on no matches (not 404).

* Retention floor: rows older than 30 days are NEVER returned, even
  if ``since`` predates them. This matches anila-studio's local
  cache TTL — anything older that "should be revoked" is also past
  its access-token expiry and effectively dead anyway.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

# Same dev-secret opt-in pattern as the other contract tests.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_user


# Pre-pick a service token value the tests reuse. We set it on
# ``settings.CSP_SERVICE_TOKEN`` via monkeypatch so the legacy fallback
# inside ``verify_service_token`` accepts it without needing a real
# ``service_clients`` row (which would require encoding helpers).
_SERVICE_TOKEN = "csk-test-revocations-12345"


@pytest.fixture
def service_token_header(monkeypatch) -> dict[str, str]:
    """Install a legacy-fallback service token both on the canonical
    ``settings`` and on the bound copy inside ``auth_service`` —
    ``auth_service`` does ``from app.config import settings`` at
    import time, so its local binding survives any module reload we
    might do in fixtures.
    """
    from app.config import settings as canonical_settings
    from app.services import auth_service

    monkeypatch.setattr(
        canonical_settings, "CSP_SERVICE_TOKEN", _SERVICE_TOKEN, raising=False
    )
    monkeypatch.setattr(
        auth_service.settings, "CSP_SERVICE_TOKEN", _SERVICE_TOKEN, raising=False
    )
    return {"X-CSP-Service-Token": _SERVICE_TOKEN}


@pytest.fixture(autouse=True)
def _ensure_dev_secret_gate(monkeypatch):
    """See the matching fixture in ``test_token_revoke_publish.py`` —
    ``test_startup_security`` leaks a reloaded settings global that
    invalidates the dev-secret allow flag on subsequent lifespan
    boots. Re-apply + reload here so we don't depend on test order.

    Teardown 把 ``app.config.settings`` 指回原物件:reload 建的新 Settings 跟
    import 期綁定舊 Settings 的模組會並存,不還原就是把污染往後傳。
    """
    import importlib
    import app.config as config_module
    import app.services.startup_security as ss_module

    original_settings = config_module.settings
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    importlib.reload(config_module)
    importlib.reload(ss_module)
    yield
    config_module.settings = original_settings
    importlib.reload(ss_module)


@pytest.fixture(autouse=True)
def _silence_publish(monkeypatch):
    """We don't care about Redis broadcasts in these tests — stub the
    publisher to a no-op so they don't try to open sockets.

    We also patch where the symbol is *imported* (``app.api.auth``) so
    the route handler sees the no-op even though ``publish_revocation``
    was bound at import time.
    """
    from app.services import token_revocation_publisher
    import app.api.auth as auth_module

    async def _noop(*args, **kwargs):
        return None

    def _noop_sync(*args, **kwargs):
        return None

    # ``commit_token_revocation`` calls through the publisher module, so
    # patching here is enough for logout / deactivate / admin-revoke.
    monkeypatch.setattr(token_revocation_publisher, "publish_revocation", _noop)
    monkeypatch.setattr(
        token_revocation_publisher, "publish_revocation_sync", _noop_sync
    )
    monkeypatch.setattr(auth_module, "publish_revocation", _noop, raising=False)
    monkeypatch.setattr(
        auth_module, "publish_revocation_sync", _noop_sync, raising=False
    )


def _insert_revocation(db, *, user_id: int, version: int, ts: datetime) -> None:
    from app.models.token_revocation import TokenRevocation

    row = TokenRevocation(
        user_id=user_id,
        revoked_at_version=version,
        revoked_at=ts,
    )
    db.add(row)
    db.commit()


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_returns_empty_array_when_no_revocations(client: TestClient, service_token_header):
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = client.get(
        "/api/auth/revocations",
        params={"since": since.isoformat()},
        headers=service_token_header,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revocations"] == []
    assert body["retention_days"] == 30


def test_returns_recent_revocations_in_window(
    client: TestClient, db, service_token_header,
):
    user_a = make_user(db, username="rev-a")
    user_b = make_user(db, username="rev-b")
    user_c = make_user(db, username="rev-c")

    now = datetime.now(timezone.utc)
    _insert_revocation(db, user_id=user_a.id, version=1, ts=now - timedelta(minutes=30))
    _insert_revocation(db, user_id=user_b.id, version=4, ts=now - timedelta(minutes=10))
    _insert_revocation(db, user_id=user_c.id, version=2, ts=now - timedelta(minutes=5))

    since = (now - timedelta(hours=1)).isoformat()
    resp = client.get(
        "/api/auth/revocations",
        params={"since": since},
        headers=service_token_header,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    revocations = body["revocations"]
    assert len(revocations) == 3

    # Sorted ASC by ts → A first, C last.
    assert revocations[0]["user_id"] == user_a.id
    assert revocations[0]["revoked_at_version"] == 1
    assert revocations[1]["user_id"] == user_b.id
    assert revocations[1]["revoked_at_version"] == 4
    assert revocations[2]["user_id"] == user_c.id


def test_since_filter_excludes_older_rows(
    client: TestClient, db, service_token_header,
):
    user = make_user(db, username="rev-older")
    now = datetime.now(timezone.utc)

    # Two rows: one before since, one after.
    _insert_revocation(db, user_id=user.id, version=1, ts=now - timedelta(hours=2))
    _insert_revocation(db, user_id=user.id, version=2, ts=now - timedelta(minutes=10))

    since = (now - timedelta(hours=1)).isoformat()
    resp = client.get(
        "/api/auth/revocations",
        params={"since": since},
        headers=service_token_header,
    )
    assert resp.status_code == 200, resp.text
    revocations = resp.json()["revocations"]
    assert len(revocations) == 1
    assert revocations[0]["revoked_at_version"] == 2


def test_retention_floor_clamps_to_30_days(
    client: TestClient, db, service_token_header,
):
    """Even if ``since`` is 60 days ago, only rows from the last 30
    days come back — anything older is past the retention floor."""
    user = make_user(db, username="rev-retention")
    now = datetime.now(timezone.utc)

    # 45-day-old row: outside retention window.
    _insert_revocation(db, user_id=user.id, version=1, ts=now - timedelta(days=45))
    # 5-day-old row: inside.
    _insert_revocation(db, user_id=user.id, version=2, ts=now - timedelta(days=5))

    since = (now - timedelta(days=60)).isoformat()
    resp = client.get(
        "/api/auth/revocations",
        params={"since": since},
        headers=service_token_header,
    )
    assert resp.status_code == 200, resp.text
    revocations = resp.json()["revocations"]
    assert len(revocations) == 1, revocations
    assert revocations[0]["revoked_at_version"] == 2


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------


def test_401_without_service_token(client: TestClient):
    resp = client.get(
        "/api/auth/revocations",
        params={"since": "2026-05-01T00:00:00+00:00"},
    )
    assert resp.status_code == 401, resp.text


def test_401_with_bogus_service_token(client: TestClient, monkeypatch):
    """An attacker-supplied token that doesn't match any DB row or
    the env-var fallback must get 401."""
    from app.config import settings

    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", _SERVICE_TOKEN, raising=False)
    resp = client.get(
        "/api/auth/revocations",
        params={"since": "2026-05-01T00:00:00+00:00"},
        headers={"X-CSP-Service-Token": "not-the-real-token"},
    )
    assert resp.status_code == 401, resp.text


def test_admin_jwt_alone_is_rejected(client: TestClient, db):
    """Admins authenticated by JWT should NOT be able to call the
    service-to-service endpoint. Single auth path keeps client code
    in anila-studio simple."""
    make_user(db, username="admin-rev-ep", role="admin")
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-rev-ep", "password": "password"},
    )
    access = resp.json()["access_token"]

    resp = client.get(
        "/api/auth/revocations",
        params={"since": "2026-05-01T00:00:00+00:00"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_422_when_since_is_missing(client: TestClient, service_token_header):
    resp = client.get(
        "/api/auth/revocations",
        headers=service_token_header,
    )
    assert resp.status_code == 422, resp.text


def test_422_when_since_is_malformed(client: TestClient, service_token_header):
    resp = client.get(
        "/api/auth/revocations",
        params={"since": "not-a-date"},
        headers=service_token_header,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Bump-point insert path: the existing bump endpoints write rows that
# this endpoint must surface. End-to-end smoke.
# ---------------------------------------------------------------------------


def test_logout_writes_row_visible_to_endpoint(
    client: TestClient, db, service_token_header,
):
    user = make_user(db, username="e2e-logout")
    resp = client.post(
        "/api/auth/login",
        json={"username": "e2e-logout", "password": "password"},
    )
    access = resp.json()["access_token"]

    before = datetime.now(timezone.utc) - timedelta(seconds=5)

    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200, resp.text

    resp = client.get(
        "/api/auth/revocations",
        params={"since": before.isoformat()},
        headers=service_token_header,
    )
    assert resp.status_code == 200, resp.text
    revocations = resp.json()["revocations"]
    assert len(revocations) == 1, revocations
    assert revocations[0]["user_id"] == user.id
    assert revocations[0]["revoked_at_version"] == 1

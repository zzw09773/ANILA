"""Deactivating an account must revoke the credential for every consumer.

csp itself compares ``users.token_version`` directly, so a bare bump
looks like it works in the console. anila-studio / asr-gateway only
learn through the published deny-list — without a ``token_revocations``
row + Redis publish they keep honouring the old JWT for up to 60
minutes. Owner ruling: deactivate = immediate revocation.

Also pins the post-bump boundary (same contract as this morning's fix):
``revoked_at_version`` is the lowest still-valid version. A token issued
after reactivation must be accepted (``tv == revoked_at_version``).
"""
from __future__ import annotations

import json
import os
from typing import Any

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _ensure_dev_secret_gate(monkeypatch):
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


class _RecordingSyncRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 0

    def close(self) -> None:
        return None


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


def _tv_of(token: str) -> int:
    from app.utils.security import decode_token

    payload = decode_token(token)
    assert payload is not None
    return int(payload["tv"])


def _published(recording: _RecordingSyncRedis) -> dict[str, Any]:
    assert len(recording.published) == 1, (
        f"expected exactly one publish, got {len(recording.published)}"
    )
    _, message = recording.published[0]
    return json.loads(message)


def _consumer_rejects(tv: int, revoked_at_version: int) -> bool:
    """Mirror of anila-studio ``RevocationCache.is_revoked`` rule."""
    return tv < revoked_at_version


def test_deactivate_writes_revocation_publishes_and_denies_old_token(
    client: TestClient, db, recording_sync_redis
):
    """Acceptance 3 + 4: deactivate revokes; post-reactivation token lives."""
    make_user(db, username="admin-deact", role="admin")
    target = make_user(db, username="victim-deact", role="user")
    initial_version = target.token_version or 0

    resp = client.post(
        "/api/auth/login",
        json={"username": "victim-deact", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    old_access = resp.json()["access_token"]
    old_tv = _tv_of(old_access)
    assert old_tv == initial_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-deact", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    admin_access = resp.json()["access_token"]

    resp = client.delete(
        f"/api/users/{target.id}",
        headers={"Authorization": f"Bearer {admin_access}"},
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    from app.models.token_revocation import TokenRevocation
    from app.models.user import User

    refreshed = db.query(User).filter(User.id == target.id).first()
    assert refreshed is not None
    assert refreshed.is_active is False
    assert refreshed.token_version == initial_version + 1

    rows = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.user_id == target.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].revoked_at_version == initial_version + 1

    published = _published(recording_sync_redis)
    assert published["user_id"] == target.id
    assert published["revoked_at_version"] == initial_version + 1

    revoked_at_version = published["revoked_at_version"]
    assert _consumer_rejects(old_tv, revoked_at_version) is True

    # Reactivate, then issue a fresh token — must sit on the live side
    # of the boundary (tv == revoked_at_version), not permanently locked.
    resp = client.post(
        f"/api/users/{target.id}/reactivate",
        headers={"Authorization": f"Bearer {admin_access}"},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        "/api/auth/login",
        json={"username": "victim-deact", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    new_tv = _tv_of(resp.json()["access_token"])

    assert new_tv == revoked_at_version
    assert _consumer_rejects(new_tv, revoked_at_version) is False


def test_admin_reset_password_also_publishes_revocation(
    client: TestClient, db, recording_sync_redis
):
    """Every token_version bump that means 'old JWTs die' must publish."""
    make_user(db, username="admin-reset", role="admin")
    target = make_user(db, username="reset-target", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-reset", "password": "password"},
    )
    admin_access = resp.json()["access_token"]

    resp = client.post(
        f"/api/users/{target.id}/reset-password",
        headers={"Authorization": f"Bearer {admin_access}"},
        json={"new_password": "BrandNew-Passw0rd!"},
    )
    assert resp.status_code == 200, resp.text

    published = _published(recording_sync_redis)
    assert published["user_id"] == target.id
    assert published["revoked_at_version"] == 1

    from app.models.token_revocation import TokenRevocation

    row = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.user_id == target.id)
        .one()
    )
    assert row.revoked_at_version == 1

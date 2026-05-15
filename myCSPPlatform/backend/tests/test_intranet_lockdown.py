"""``REQUIRE_CARD_LOGIN_ONLY`` (branch SSO) endpoint-level lockdown tests.

Pins the contract:
- 預設 ``REQUIRE_CARD_LOGIN_ONLY=False`` → 既有 endpoint 行為不變。
- ``REQUIRE_CARD_LOGIN_ONLY=True`` 時：
  - ``POST /api/auth/login``       → 404
  - ``POST /api/auth/register``    → 404
  - ``GET  /api/auth/oidc/{id}/start`` → 404
  - ``GET  /api/auth/oidc/{id}/callback`` → 404
  - ``PUT  /api/auth/password``    → 404
  - ``GET  /api/auth/providers``   → 不再列出 OIDC providers
- Startup 一致性：``REQUIRE_CARD_LOGIN_ONLY=True`` 但
  ``ENABLE_CARD_LOGIN=False`` → ``RuntimeError``。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models.auth_provider import AuthProvider
from app.services.startup_security import assert_intranet_lockdown_consistency

from tests.conftest import make_user


@pytest.fixture
def card_only_lockdown(monkeypatch):
    """Toggle the intranet lockdown for this test only."""
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)


# ── endpoint-level lockdown ────────────────────────────────────────────────────


def test_local_login_returns_404_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    make_user(db, username="alice")
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "password"},
    )
    assert resp.status_code == 404


def test_register_returns_404_when_locked_down(
    client: TestClient, card_only_lockdown
):
    resp = client.post(
        "/api/auth/register",
        json={
            "username": "newbie",
            "email": "n@example.com",
            "password": "x" * 12,
        },
    )
    assert resp.status_code == 404


def test_oidc_start_returns_404_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    # 即使 DB 內有 active OIDC provider，也不能 trigger 起始流程。
    provider = AuthProvider(
        name="demo-oidc",
        provider_type="oidc",
        is_active=True,
        oidc_issuer_url="https://idp.example.com",
        oidc_client_id="anila",
    )
    db.add(provider)
    db.commit()

    resp = client.get(f"/api/auth/oidc/{provider.id}/start")
    assert resp.status_code == 404


def test_oidc_callback_returns_404_when_locked_down(
    client: TestClient, card_only_lockdown
):
    # callback 也要擋；攻擊者不能跳過 /start 直接打 callback URL。
    resp = client.get("/api/auth/oidc/1/callback?code=x&state=y")
    assert resp.status_code == 404


def test_providers_endpoint_hides_oidc_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    db.add_all([
        AuthProvider(
            name="hidden-oidc",
            provider_type="oidc",
            is_active=True,
            oidc_issuer_url="https://idp.example.com",
            oidc_client_id="anila",
        ),
        AuthProvider(
            name="visible-card",
            provider_type="card",
            is_active=True,
        ),
    ])
    db.commit()

    resp = client.get("/api/auth/providers")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "hidden-oidc" not in names
    # card provider 應該還能看到
    assert "visible-card" in names


# ── disabled-by-default sanity ─────────────────────────────────────────────────


def test_local_login_still_works_when_lockdown_off(client: TestClient, db):
    """確保 lockdown 預設 OFF — 不會破壞既有部署。"""
    # 預設 REQUIRE_CARD_LOGIN_ONLY=False
    make_user(db, username="alice2")
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice2", "password": "password"},
    )
    assert resp.status_code == 200


# ── startup consistency ───────────────────────────────────────────────────────


def test_startup_assertion_passes_when_lockdown_off(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", False)
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", False)
    # 不該 raise — lockdown 沒開時不檢查
    assert_intranet_lockdown_consistency()


def test_startup_assertion_passes_when_both_flags_on(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    assert_intranet_lockdown_consistency()


def test_startup_assertion_rejects_inconsistent_config(monkeypatch):
    """REQUIRE_CARD_LOGIN_ONLY=True 但 ENABLE_CARD_LOGIN=False → bricked。"""
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", False)
    with pytest.raises(RuntimeError, match="REQUIRE_CARD_LOGIN_ONLY"):
        assert_intranet_lockdown_consistency()

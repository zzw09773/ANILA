"""``REQUIRE_CARD_LOGIN_ONLY`` (branch SSO) endpoint-level lockdown tests.

Pins the contract:
- 預設 ``REQUIRE_CARD_LOGIN_ONLY=False`` → 既有 endpoint 行為不變。
- ``REQUIRE_CARD_LOGIN_ONLY=True`` 時：
  - ``POST /api/auth/login``       → 404；只有具名且未過期的 break-glass
    profile 對 owner 帳密開放，其他結果一律 404、姿態不可區分
  - ``POST /api/auth/register``    → 404
  - ``GET  /api/auth/oidc/{id}/start`` → 404
  - ``GET  /api/auth/oidc/{id}/callback`` → 404
  - ``PUT  /api/auth/password``    → 404；break-glass owner 才可輪換密碼
  - ``GET  /api/auth/providers``   → 不再列出 OIDC providers
- Startup 一致性：``REQUIRE_CARD_LOGIN_ONLY=True`` 但
  ``ENABLE_CARD_LOGIN=False`` → ``RuntimeError``。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.auth_provider import AuthProvider
from app.services.startup_security import assert_intranet_lockdown_consistency

from tests.conftest import make_user


@pytest.fixture
def card_only_lockdown(monkeypatch):
    """Toggle the intranet lockdown for this test only."""
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(settings, "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card")


@pytest.fixture
def active_break_glass(monkeypatch, card_only_lockdown):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        settings,
        "ANILA_DEPLOYMENT_PROFILE",
        "prod-intranet-card-breakglass",
    )
    monkeypatch.setenv("ANILA_BREAK_GLASS_OWNER", "system-owner")
    monkeypatch.setenv("ANILA_BREAK_GLASS_TICKET", "INC-LOCKDOWN-001")
    monkeypatch.setenv(
        "ANILA_BREAK_GLASS_EXPIRES_AT",
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


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
            # 要過得了 RegisterRequest 的密碼強度驗證,404 gate 的斷言才有效
            # (弱密碼會在進 endpoint 前就 422,測不到 lockdown)。
            "password": "Xx1!aaaaAAAA22",
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
    # 設計演進後 list_public_auth_providers 只回 oidc 型 — card 登入區塊由
    # LoginView 無條件渲染,不靠 provider row 驅動;card-only 下清單應為空。
    assert names == set()


# ── named/time-bounded owner break-glass ─────────────────────────────────────


def test_owner_password_login_rejected_in_normal_card_profile(
    client: TestClient, db, card_only_lockdown
):
    make_user(db, username="boss", role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"username": "boss", "password": "password"},
    )
    assert resp.status_code == 404


def test_owner_password_login_allowed_in_active_break_glass(
    client: TestClient, db, active_break_glass
):
    make_user(db, username="incident-owner", role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"username": "incident-owner", "password": "password"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    event = (
        db.query(AuditLog)
        .filter(AuditLog.action == "login", AuditLog.status == "success")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert event is not None
    metadata = json.loads(event.metadata_json)
    assert metadata["break_glass"] is True
    assert metadata["owner"] == "system-owner"
    assert metadata["ticket"] == "INC-LOCKDOWN-001"
    assert metadata["expires_at"]


def test_break_glass_failed_password_audit_includes_ticket(
    client: TestClient, db, active_break_glass
):
    make_user(db, username="incident-owner-failed", role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"username": "incident-owner-failed", "password": "wrong-password"},
    )
    assert resp.status_code == 404
    event = (
        db.query(AuditLog)
        .filter(AuditLog.action == "login", AuditLog.status == "failure")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert event is not None
    metadata = json.loads(event.metadata_json)
    assert metadata["break_glass"] is True
    assert metadata["owner"] == "system-owner"
    assert metadata["ticket"] == "INC-LOCKDOWN-001"


@pytest.mark.parametrize(
    "rejection",
    (
        "unsupported_auth_source",
        "pending_owner",
        "local_password_disabled_owner",
        "valid_non_owner",
    ),
)
def test_break_glass_rejected_login_branches_audit_ticket(
    client: TestClient, db, active_break_glass, rejection: str
):
    username = f"incident-{rejection.replace('_', '-')}"
    payload = {"username": username, "password": "password"}

    if rejection == "unsupported_auth_source":
        payload["auth_source"] = "ldap"
    elif rejection == "pending_owner":
        make_user(db, username=username, role="owner", is_approved=False)
    elif rejection == "local_password_disabled_owner":
        user = make_user(db, username=username, role="owner")
        user.local_password_disabled = True
        db.commit()
    else:
        make_user(db, username=username, role="user")

    resp = client.post("/api/auth/login", json=payload)
    assert resp.status_code == 404
    event = (
        db.query(AuditLog)
        .filter(AuditLog.action == "login", AuditLog.status == "failure")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert event is not None
    metadata = json.loads(event.metadata_json)
    assert metadata["break_glass"] is True
    assert metadata["owner"] == "system-owner"
    assert metadata["ticket"] == "INC-LOCKDOWN-001"


def test_owner_wrong_password_returns_404_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    """失敗姿態統一:owner 密碼錯也回 404,不可與非 owner / 端點關閉區分。"""
    make_user(db, username="boss", role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"username": "boss", "password": "wrong-password"},
    )
    assert resp.status_code == 404


def test_nonowner_valid_credentials_return_404_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    """非 owner 連「帳密完全正確」都拿 404 — owner 例外不外溢到其他角色。"""
    make_user(db, username="staffer", role="user")
    resp = client.post(
        "/api/auth/login",
        json={"username": "staffer", "password": "password"},
    )
    assert resp.status_code == 404


def test_change_password_owner_allowed_when_locked_down(
    client: TestClient, db, active_break_glass
):
    """Active break-glass owner can rotate the recovery password."""
    make_user(db, username="boss", role="owner")
    token = client.post(
        "/api/auth/login",
        json={"username": "boss", "password": "password"},
    ).json()["access_token"]
    resp = client.put(
        "/api/auth/password",
        json={"current_password": "password", "new_password": "n3w-Passw0rd!xyz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_change_password_old_nonowner_session_returns_401_when_locked_down(
    client: TestClient, db, monkeypatch
):
    """切換正式姿態後，舊的非 owner 密碼 session 在共用 JWT 邊界即失效。"""
    make_user(db, username="staffer2", role="user")
    token = client.post(
        "/api/auth/login",
        json={"username": "staffer2", "password": "password"},
    ).json()["access_token"]
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    resp = client.put(
        "/api/auth/password",
        json={"current_password": "password", "new_password": "n3w-Passw0rd!xyz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


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

"""``ANILA_AUTH_MODE`` (branch SSO) endpoint-level lockdown tests.

Pins the contract:
- ``ANILA_AUTH_MODE=password`` → 既有帳密 endpoint 行為不變。
- ``ANILA_AUTH_MODE=card-only`` 時：
  - ``POST /api/auth/login``       → 404;**例外:owner 帳密正確可登入**
    (break-glass,2026-06-11)— 其他所有結果(密碼錯/非 owner 憑證有效/
    待核准)一律 404,姿態不可區分
  - ``POST /api/auth/register``    → 404
  - ``GET  /api/auth/oidc/{id}/start`` → 404
  - ``GET  /api/auth/oidc/{id}/callback`` → 404
  - ``PUT  /api/auth/password``    → 404;**例外:owner 可輪換密碼**
  - ``GET  /api/auth/providers``   → 不再列出 OIDC providers
- Startup 一致性：未知 ``ANILA_AUTH_MODE`` → ``RuntimeError``。
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
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")


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


# ── owner break-glass(2026-06-11):card-only 下帳密登入僅限 owner ────────────


def test_owner_password_login_allowed_when_locked_down(
    client: TestClient, db, card_only_lockdown
):
    """owner 是 break-glass 例外:card-only 下帳密正確仍可登入。"""
    make_user(db, username="boss", role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"username": "boss", "password": "password"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


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
    client: TestClient, db, card_only_lockdown
):
    """owner 能登入就必須能輪換密碼(與 /login 例外對齊)。"""
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


def test_change_password_nonowner_returns_404_when_locked_down(
    client: TestClient, db, monkeypatch
):
    """非 owner 帶有效 token 改密碼仍 404(token 先在 lockdown OFF 時取得)。"""
    make_user(db, username="staffer2", role="user")
    token = client.post(
        "/api/auth/login",
        json={"username": "staffer2", "password": "password"},
    ).json()["access_token"]
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")
    resp = client.put(
        "/api/auth/password",
        json={"current_password": "password", "new_password": "n3w-Passw0rd!xyz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ── disabled-by-default sanity ─────────────────────────────────────────────────


def test_local_login_still_works_when_lockdown_off(client: TestClient, db):
    """確保 lockdown 預設 OFF — 不會破壞既有部署。"""
    # 預設測試環境是 password mode。
    make_user(db, username="alice2")
    resp = client.post(
        "/api/auth/login",
        json={"username": "alice2", "password": "password"},
    )
    assert resp.status_code == 200


# ── startup consistency ───────────────────────────────────────────────────────


def test_startup_assertion_passes_when_lockdown_off(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "password")
    # 不該 raise — password mode 是合法模式
    assert_intranet_lockdown_consistency()


def test_startup_assertion_passes_when_both_flags_on(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")
    assert_intranet_lockdown_consistency()


def test_startup_assertion_rejects_unknown_auth_mode(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "unsupported")
    with pytest.raises(RuntimeError, match="ANILA_AUTH_MODE"):
        assert_intranet_lockdown_consistency()

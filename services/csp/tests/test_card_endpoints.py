"""Integration tests for ``/api/auth/card/{challenge,verify}``.

Pins the contract:
- Endpoints 回 404 當 ``settings.ENABLE_CARD_LOGIN=False`` (預設) — prod
  錯誤配置時假裝功能不存在,避免暴露給外部探測。
- ``GET /api/auth/card/challenge`` 回 JWT + 明文 nonce。
- ``POST /api/auth/card/verify`` 接受 ephemeral synthetic card 簽章,
  端到端建 User + 種 session cookie + ``/me`` 可拿。
- 同一張卡第二次登入不會 create 重複 user。
- email collision 與 challenge 過期 / 簽章不合法都被正確拒絕。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.middleware.cookies import (
    ACCESS_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
)
from app.models.user import User
from app.utils.security import decode_token

from tests.conftest import make_user
from tests.synthetic_card_pki import (
    SYNTHETIC_CARD,
    SYNTHETIC_CARD_SERIAL,
    SYNTHETIC_DISPLAY_NAME,
    SYNTHETIC_EMAIL,
    SYNTHETIC_EMPLOYEE_ID,
)


EXPECTED_EMP_ID = SYNTHETIC_EMPLOYEE_ID
EXPECTED_EMAIL = SYNTHETIC_EMAIL
EXPECTED_CARD_SN = SYNTHETIC_CARD_SERIAL


@pytest.fixture
def card_login_enabled(monkeypatch, synthetic_card_trust):
    """Toggle the feature flag for this test only.

    預設 ``ENABLE_CARD_LOGIN=False``；明示啟用後 endpoint 才接受流量。
    把 synthetic 員工編號塞進 ``CARD_INITIAL_OWNERS`` — 這樣既有 happy
    path 測試（預期 mock 卡能直接登入成 owner）仍然成立，等同於 dev
    環境的實際 DX。Pending flow 測試另外用 ``card_login_pending_default``。
    """
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", EXPECTED_EMP_ID)


@pytest.fixture
def card_login_pending_default(monkeypatch, synthetic_card_trust):
    """跟 ``card_login_enabled`` 一樣但 ``CARD_INITIAL_OWNERS`` 為空 —
    所有人第一次刷卡都進 pending registration,給 pending flow 測試用。
    """
    monkeypatch.setattr(settings, "ENABLE_CARD_LOGIN", True)
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", "")


# ── disabled-by-default guard ──────────────────────────────────────────────────


def test_challenge_returns_404_when_card_login_disabled(client: TestClient):
    resp = client.get("/api/auth/card/challenge")
    assert resp.status_code == 404


def test_verify_returns_404_when_card_login_disabled(client: TestClient):
    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "x", "signature": "synthetic-invalid"},
    )
    assert resp.status_code == 404


# ── happy path: challenge → verify → session ──────────────────────────────────


def test_challenge_returns_jwt_and_plaintext_nonce(
    client: TestClient, card_login_enabled
):
    resp = client.get("/api/auth/card/challenge")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert isinstance(body["challenge_token"], str) and body["challenge_token"]
    assert isinstance(body["nonce"], str) and len(body["nonce"]) >= 16
    assert body["expires_in"] == 120


def test_verify_creates_user_and_sets_session_cookies(
    client: TestClient, db, card_login_enabled
):
    ch = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
            "card_serial": EXPECTED_CARD_SN,
        },
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["csrf_token"]

    cookie_names = {c.name for c in client.cookies.jar}
    assert ACCESS_COOKIE_NAME in cookie_names
    assert REFRESH_COOKIE_NAME in cookie_names

    user = db.query(User).filter(User.username == EXPECTED_EMP_ID).first()
    assert user is not None
    assert user.email == EXPECTED_EMAIL
    assert user.local_password_disabled is True
    assert user.is_approved is True
    assert user.is_active is True


def test_me_reachable_with_card_session_cookie(
    client: TestClient, card_login_enabled
):
    ch = client.get("/api/auth/card/challenge").json()
    client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
        },
    )

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == EXPECTED_EMP_ID


def test_verify_rejects_replayed_challenge(
    client: TestClient, card_login_enabled
):
    """同一組 challenge token + CMS 簽章只能兌換一次 session。"""
    ch = client.get("/api/auth/card/challenge").json()
    payload = {
        "challenge_token": ch["challenge_token"],
        "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
        "card_serial": EXPECTED_CARD_SN,
    }

    first = client.post("/api/auth/card/verify", json=payload)
    assert first.status_code == 200, first.text

    # Simulate capture by a separate unauthenticated client: replay safety
    # must not depend on the first session cookie or CSRF middleware.
    client.cookies.clear()
    replay = client.post("/api/auth/card/verify", json=payload)
    assert replay.status_code == 400, replay.text
    assert "已使用或過期" in replay.json()["detail"]


# ── error cases ────────────────────────────────────────────────────────────────


def test_verify_rejects_malformed_challenge_token(
    client: TestClient, card_login_enabled
):
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": "not.a.valid.jwt",
            "signature": SYNTHETIC_CARD.sign("unused-invalid-challenge"),
        },
    )
    assert resp.status_code == 400


def test_verify_rejects_invalid_signature_with_401(
    client: TestClient, card_login_enabled
):
    ch = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": "not-base64!!!",
        },
    )
    assert resp.status_code == 401


def test_invalid_signature_does_not_consume_challenge(
    client: TestClient, card_login_enabled
):
    """只有驗章成功的請求可消耗 challenge，避免公開 token 被惡意燒毀。"""
    ch = client.get("/api/auth/card/challenge").json()
    invalid = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": "not-base64!!!",
        },
    )
    assert invalid.status_code == 401

    valid = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
            "card_serial": EXPECTED_CARD_SN,
        },
    )
    assert valid.status_code == 200, valid.text


# ── idempotency + safety ──────────────────────────────────────────────────────


def test_second_login_reuses_existing_user(
    client: TestClient, db, card_login_enabled
):
    """同一張卡刷兩次不會 duplicate user row。"""
    for _ in range(2):
        ch = client.get("/api/auth/card/challenge").json()
        csrf = client.cookies.get(CSRF_COOKIE_NAME)
        resp = client.post(
            "/api/auth/card/verify",
            json={
                "challenge_token": ch["challenge_token"],
                "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
            },
            headers={"X-CSRF-Token": csrf} if csrf else {},
        )
        assert resp.status_code == 200, resp.text

    rows = db.query(User).filter(User.username == EXPECTED_EMP_ID).count()
    assert rows == 1


def test_email_collision_with_other_account_is_rejected(
    client: TestClient, db, card_login_enabled
):
    """既有帳號 (非卡片帳號) 已佔用同 email → 卡片登入被拒。

    這個 policy 對齊 OIDC ``_provision_external_user``：拒絕自動接管，
    要 admin 手動處理。
    """
    other = make_user(db, username="other-user")
    other.email = EXPECTED_EMAIL
    db.commit()

    ch = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
        },
    )
    assert resp.status_code == 400
    assert "已綁定其他帳號" in resp.json()["detail"]


# ── pending registration + approval flow (branch SSO B 方案) ──────────────────


def _make_department(db, name: str = "資通所人工智慧組") -> int:
    """Test helper：建一筆 active department 並回 id。"""
    from app.models.department import Department
    dept = Department(name=name, is_active=True)
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept.id


def _do_card_verify(client: TestClient) -> dict:
    """跑完 challenge → verify round-trip，回 verify response.json()."""
    ch = client.get("/api/auth/card/challenge").json()
    resp = client.post(
        "/api/auth/card/verify",
        json={
            "challenge_token": ch["challenge_token"],
            "signature": SYNTHETIC_CARD.sign(ch["nonce"]),
        },
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def test_owner_in_initial_owners_logs_in_directly(
    client: TestClient, db, card_login_enabled
):
    """``card_login_enabled`` 把 synthetic 員編設為 OWNERS → 應直接登入。"""
    result = _do_card_verify(client)
    assert result["status_code"] == 200
    assert "access_token" in result["body"]
    assert decode_token(result["body"]["access_token"])["amr"] == ["sc"]
    assert decode_token(result["body"]["refresh_token"])["amr"] == ["sc"]

    user = db.query(User).filter(User.username == EXPECTED_EMP_ID).first()
    assert user.role == "owner"
    assert user.is_approved is True


def test_non_owner_first_swipe_returns_pending_registration(
    client: TestClient, db, card_login_pending_default
):
    """OWNERS 為空 → synthetic user 回 202 pending_registration + token。"""
    result = _do_card_verify(client)
    assert result["status_code"] == 202
    body = result["body"]
    assert body["status"] == "pending_registration"
    assert body["employee_id"] == EXPECTED_EMP_ID
    assert body["display_name"] == SYNTHETIC_DISPLAY_NAME
    assert body["email"] == EXPECTED_EMAIL
    assert body["registration_token"]
    assert body["expires_in"] == 900

    # User row 該被建起來，但 role=user / is_approved=False / department_id=None
    user = db.query(User).filter(User.username == EXPECTED_EMP_ID).first()
    assert user is not None
    assert user.role == "user"
    assert user.is_approved is False
    assert user.department_id is None


def test_pending_user_does_not_get_session_cookies(
    client: TestClient, card_login_pending_default
):
    """Pending 回應不該種 cookie — 確保 /me 仍然 401。"""
    _do_card_verify(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 401


def test_complete_registration_with_valid_department(
    client: TestClient, db, card_login_pending_default
):
    """Pending → 帶 token + 有效 department_id → 200，user.department_id 更新。"""
    dept_id = _make_department(db, "資通所人工智慧組")
    pending = _do_card_verify(client)["body"]

    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert "等待管理員核准" in body["message"]

    user = db.query(User).filter(User.username == EXPECTED_EMP_ID).first()
    assert user.department_id == dept_id
    assert user.is_approved is False  # 仍然要等 admin 核准


def test_complete_registration_with_invalid_token(
    client: TestClient, db, card_login_pending_default
):
    _make_department(db)
    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": "not.a.valid.jwt",
            "department_id": 1,
        },
    )
    assert resp.status_code == 401


def test_complete_registration_with_invalid_department(
    client: TestClient, db, card_login_pending_default
):
    pending = _do_card_verify(client)["body"]
    resp = client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": 9999,  # 不存在
        },
    )
    assert resp.status_code == 400


def test_second_swipe_after_registration_returns_pending_approval(
    client: TestClient, db, card_login_pending_default
):
    """已填單位 + 仍 is_approved=False → 第二次刷卡回 pending_approval (無 token)。"""
    dept_id = _make_department(db)

    # 第一次：pending_registration → 完成註冊
    pending = _do_card_verify(client)["body"]
    client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )

    # 第二次刷卡：已填單位但 admin 還沒核准 → pending_approval
    result = _do_card_verify(client)
    assert result["status_code"] == 202
    body = result["body"]
    assert body["status"] == "pending_approval"
    assert body["registration_token"] is None  # 不再發 token
    assert "等待管理員核准" in body["message"]


def test_admin_approval_unblocks_login(
    client: TestClient, db, card_login_pending_default
):
    """Admin 把 is_approved 翻為 True 後，下次刷卡應該真的拿到 cookie session。"""
    dept_id = _make_department(db)
    pending = _do_card_verify(client)["body"]
    client.post(
        "/api/auth/card/complete-registration",
        json={
            "registration_token": pending["registration_token"],
            "department_id": dept_id,
        },
    )

    # 模擬 admin 核准
    user = db.query(User).filter(User.username == EXPECTED_EMP_ID).first()
    user.is_approved = True
    db.commit()

    result = _do_card_verify(client)
    assert result["status_code"] == 200
    assert "access_token" in result["body"]


def test_departments_endpoint_lists_only_active(
    client: TestClient, db, card_login_pending_default
):
    """``/api/auth/card/registration/departments`` 應該 public + 只列 active。"""
    from app.models.department import Department

    _make_department(db, "資通所人工智慧組")
    inactive = Department(name="已裁撤組", is_active=False)
    db.add(inactive)
    db.commit()

    resp = client.get("/api/auth/card/registration/departments")
    assert resp.status_code == 200
    rows = resp.json()
    names = {r["name"] for r in rows}
    assert "資通所人工智慧組" in names
    assert "已裁撤組" not in names
    # 僅暴露 id + name 兩個欄位
    assert all(set(r.keys()) == {"id", "name"} for r in rows)


def test_departments_endpoint_404_when_card_login_disabled(client: TestClient):
    """同 challenge / verify，feature off 時 endpoint 假裝不存在。"""
    resp = client.get("/api/auth/card/registration/departments")
    assert resp.status_code == 404

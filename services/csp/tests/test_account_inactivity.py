"""閒置停用：第一次只試算，之後才停用；刷卡回到待審。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.api.auth import card as card_api
from app.config import settings
from app.models.audit_log import AuditLog
from app.models.platform_setting import get_setting
from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.services.card_auth import CardClaims
from app.services.inactivity_service import (
    INACTIVITY_DAYS_KEY,
    inactivity_notice,
    inactivity_paused_message,
    run_inactivity_sweep,
)
from tests.conftest import login, make_user


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age(user: User, *, login_days: int | None, created_days: int) -> None:
    user.created_at = _ago(created_days)
    user.last_login_at = None if login_days is None else _ago(login_days)


def _actions(db: Session) -> list[str]:
    return [row.action for row in db.query(AuditLog).all()]


def test_default_inactivity_window_is_180_days_without_a_new_env(db):
    assert get_setting(db, INACTIVITY_DAYS_KEY) == 180


def test_first_sweep_is_preview_and_keeps_recent_logins(db):
    stale = make_user(db, username="stale-user")
    recent = make_user(db, username="recent-user")
    never = make_user(db, username="never-logged")
    fresh = make_user(db, username="new-account")
    owner = make_user(db, username="platform-owner", role="owner")
    worker = make_user(db, username="ingestion-worker", role="system")
    pending = make_user(db, username="still-pending", is_approved=False)
    for user in (stale, owner, worker, pending):
        _age(user, login_days=400, created_days=500)
    _age(recent, login_days=10, created_days=500)
    _age(never, login_days=None, created_days=200)
    _age(fresh, login_days=None, created_days=3)
    db.commit()

    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    first = run_inactivity_sweep(db, now=moment)
    assert first == {"phase": "preview", "count": 2, "days": 180}
    db.refresh(stale)
    db.refresh(never)
    assert stale.is_active is True
    assert never.is_active is True
    assert "inactivity_disable" not in _actions(db)
    assert "inactivity_sweep_preview" in _actions(db)

    same_day = run_inactivity_sweep(db, now=moment + timedelta(hours=6))
    assert same_day == {"phase": "preview", "count": 2, "days": 180}
    db.refresh(stale)
    db.refresh(never)
    assert stale.is_active is True
    assert never.is_active is True
    assert _actions(db).count("inactivity_sweep_preview") == 1
    assert inactivity_notice(db) == {"phase": "preview", "count": 2, "days": 180}

    second = run_inactivity_sweep(db, now=moment + timedelta(days=1))
    assert second["phase"] == "applied"
    assert second["count"] == 2
    db.refresh(stale)
    db.refresh(never)
    db.refresh(recent)
    db.refresh(fresh)
    db.refresh(owner)
    db.refresh(worker)
    db.refresh(pending)
    assert stale.is_active is False
    assert stale.disabled_reason == "inactivity"
    assert never.is_active is False
    assert recent.is_active is True
    assert fresh.is_active is True
    assert owner.is_active is True
    assert worker.is_active is True
    assert pending.is_active is True
    revoked = (
        db.query(TokenRevocation).filter(TokenRevocation.user_id == stale.id).one()
    )
    assert revoked.revoked_at_version == stale.token_version
    assert stale.token_version >= 1
    assert "inactivity_disable" in _actions(db)

    again = run_inactivity_sweep(db, now=moment + timedelta(days=1, hours=4))
    assert again == {"phase": "applied", "count": 2, "days": 180}
    assert inactivity_notice(db) == {"phase": "applied", "count": 2, "days": 180}
    assert _actions(db).count("inactivity_sweep_applied") == 1


def test_login_racing_the_disable_stays_active(db, db_engine):
    user = make_user(db, username="racing-login")
    _age(user, login_days=400, created_days=500)
    db.commit()
    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    preview = run_inactivity_sweep(db, now=moment)
    assert preview["phase"] == "preview"
    assert preview["count"] == 1

    raced = {"done": False}
    logged_in_at = moment + timedelta(days=1)

    def _login_wins(conn, cursor, statement, parameters, context, executemany):
        if raced["done"]:
            return
        sql = " ".join(str(statement).lower().split())
        if "update" in sql and "users" in sql and "is_active" in sql:
            raced["done"] = True
            cursor.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (logged_in_at.strftime("%Y-%m-%d %H:%M:%S"), user.id),
            )

    event.listen(db_engine, "before_cursor_execute", _login_wins)
    try:
        applied = run_inactivity_sweep(db, now=logged_in_at)
    finally:
        event.remove(db_engine, "before_cursor_execute", _login_wins)

    assert raced["done"] is True
    db.expire_all()
    fresh = db.query(User).filter(User.id == user.id).one()
    assert fresh.is_active is True
    assert fresh.disabled_reason is None
    assert applied["count"] == 0
    assert "inactivity_disable" not in _actions(db)


def test_preview_shows_on_the_alert_summary(client, db):
    stale = make_user(db, username="notice-stale")
    _age(stale, login_days=400, created_days=400)
    db.commit()
    run_inactivity_sweep(db)
    make_user(db, username="notice-admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'notice-admin')}"}
    body = client.get("/api/alerts/summary", headers=headers)
    assert body.status_code == 200, body.text
    notice = body.json()["inactivity_notice"]
    assert notice["phase"] == "preview"
    assert notice["count"] == 1
    assert notice["days"] == 180


@pytest.fixture
def card_login_on(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "mixed")
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", "")


def _stub(monkeypatch, user):
    claims = CardClaims(
        employee_id="9100099",
        display_name="閒置測試",
        email="idle@example.invalid",
        card_serial="CSTEST0000000199",
    )

    def _fake(db, *, signature_b64, challenge_token, card_serial):
        return user, claims

    monkeypatch.setattr(card_api, "verify_card_and_resolve_user", _fake)


def test_inactivity_card_login_returns_to_pending_approval(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    user = make_user(db, username="9100099")
    user.is_active = False
    user.disabled_reason = "inactivity"
    user.is_approved = True
    user.department_id = None
    user.last_login_at = _ago(200)
    db.commit()
    previous_login = user.last_login_at
    _stub(monkeypatch, user)

    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["message"] == inactivity_paused_message(180)
    assert "access_token" not in body
    db.refresh(user)
    assert user.is_active is True
    assert user.is_approved is False
    assert user.disabled_reason is None
    reloaded = _as_utc(user.last_login_at)
    assert reloaded is not None
    assert reloaded > _as_utc(previous_login)
    assert "inactivity_reapproval" in _actions(db)


def test_repending_today_survives_the_next_sweep(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    """刷卡改回待審時把閒置錨點改成現在。核准之後，隔天的排程不會再停用。"""
    user = make_user(db, username="9100077")
    _age(user, login_days=400, created_days=500)
    db.commit()
    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    assert run_inactivity_sweep(db, now=moment)["phase"] == "preview"
    applied = run_inactivity_sweep(db, now=moment + timedelta(days=1))
    assert applied["phase"] == "applied"
    db.refresh(user)
    assert user.is_active is False
    before_login = _as_utc(user.last_login_at)
    _stub(monkeypatch, user)
    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["message"] == inactivity_paused_message(180)
    assert "access_token" not in resp.json()
    db.refresh(user)
    assert user.is_approved is False
    assert _as_utc(user.last_login_at) > before_login
    user.is_approved = True
    db.commit()
    tomorrow = run_inactivity_sweep(db, now=moment + timedelta(days=2))
    assert tomorrow["phase"] == "applied"
    db.refresh(user)
    assert user.is_active is True
    assert user.disabled_reason is None


def test_password_and_oidc_reopen_inactivity_with_the_same_message_and_no_token(
    client: TestClient, db: Session, monkeypatch
):
    from app.api.auth import oidc as oidc_api
    from app.models.auth_provider import AuthProvider

    password_user = make_user(db, username="idle-password")
    password_user.is_active = False
    password_user.disabled_reason = "inactivity"
    password_user.is_approved = True
    password_user.last_login_at = _ago(200)
    oidc_user = make_user(db, username="idle-oidc")
    oidc_user.is_active = False
    oidc_user.disabled_reason = "inactivity"
    oidc_user.is_approved = True
    oidc_user.last_login_at = _ago(200)
    provider = AuthProvider(
        name="idle-provider",
        provider_type="oidc",
        is_active=True,
        default_role="user",
    )
    db.add(provider)
    db.commit()
    message = inactivity_paused_message(180)

    refused = client.post(
        "/api/auth/login",
        json={"username": "idle-password", "password": "wrong"},
    )
    assert refused.status_code == 401, refused.text
    db.refresh(password_user)
    assert password_user.is_active is False
    assert _as_utc(password_user.last_login_at) < datetime.now(timezone.utc) - timedelta(days=100)

    opened = client.post(
        "/api/auth/login",
        json={"username": "idle-password", "password": "password"},
    )
    assert opened.status_code == 403, opened.text
    assert opened.json()["detail"]["message"] == message
    assert "access_token" not in opened.text
    db.refresh(password_user)
    assert password_user.is_active is True
    assert password_user.is_approved is False
    assert _as_utc(password_user.last_login_at) > datetime.now(timezone.utc) - timedelta(minutes=5)

    monkeypatch.setattr(
        oidc_api,
        "decode_external_state",
        lambda state: {"provider_id": provider.id, "next_path": "/"},
    )

    async def _fake_oidc(db, provider, code, state_payload):
        return oidc_user

    monkeypatch.setattr(oidc_api, "authenticate_oidc_code", _fake_oidc)
    callback = client.get(
        f"/api/auth/oidc/{provider.id}/callback",
        params={"code": "stub", "state": "stub"},
    )
    assert message in callback.text
    assert "access_token" not in callback.text
    assert "anila_access" not in callback.headers.get("set-cookie", "")
    db.refresh(oidc_user)
    assert oidc_user.is_active is True
    assert oidc_user.is_approved is False
    assert _as_utc(oidc_user.last_login_at) > datetime.now(timezone.utc) - timedelta(minutes=5)


def test_refused_password_login_does_not_reset_the_inactivity_clock(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")
    user = make_user(db, username="kept-clock")
    user.last_login_at = _ago(30)
    db.commit()
    previous = _as_utc(user.last_login_at)
    resp = client.post(
        "/api/auth/login",
        json={"username": "kept-clock", "password": "password"},
    )
    assert resp.status_code == 404, resp.text
    db.expire_all()
    fresh = db.query(User).filter(User.id == user.id).one()
    assert fresh.is_active is True
    assert _as_utc(fresh.last_login_at) == previous

    paused = make_user(db, username="kept-paused")
    paused.is_active = False
    paused.disabled_reason = "inactivity"
    paused.last_login_at = _ago(200)
    db.commit()
    anchor = _as_utc(paused.last_login_at)
    paused_resp = client.post(
        "/api/auth/login",
        json={"username": "kept-paused", "password": "password"},
    )
    assert paused_resp.status_code == 404, paused_resp.text
    db.expire_all()
    paused = db.query(User).filter(User.username == "kept-paused").one()
    assert paused.is_active is False
    assert paused.is_approved is True
    assert _as_utc(paused.last_login_at) == anchor


def test_card_login_reopens_inactivity_but_accepts_an_admin_reactivation(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    """閒置停用刷卡回到待審。管理員已經重新啟用並核准的帳號要正常登入。"""
    paused = make_user(db, username="9100088")
    paused.is_active = False
    paused.disabled_reason = "inactivity"
    paused.is_approved = True
    paused.last_login_at = _ago(200)
    restored = make_user(db, username="9100087")
    restored.is_active = True
    restored.is_approved = True
    restored.disabled_reason = None
    db.commit()

    def _verify(db, *, signature_b64, challenge_token, card_serial):
        if challenge_token == "paused":
            return paused, CardClaims(
                employee_id="9100088",
                display_name="閒置",
                email=None,
                card_serial=None,
            )
        # 驗章當時看到的是停用前的快照；資料庫裡管理員已經重新啟用。
        stale = type("StaleUser", (), {})()
        stale.id = restored.id
        stale.is_active = False
        stale.is_approved = True
        stale.disabled_reason = "inactivity"
        stale.role = restored.role
        stale.username = restored.username
        stale.department_id = restored.department_id
        return stale, CardClaims(
            employee_id="9100087",
            display_name="已恢復",
            email=None,
            card_serial=None,
        )

    monkeypatch.setattr(card_api, "verify_card_and_resolve_user", _verify)
    pending = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "paused", "signature": "stub", "card_serial": None},
    )
    assert pending.status_code == 202, pending.text
    assert pending.json()["status"] == "pending_approval"
    assert "access_token" not in pending.json()
    db.refresh(paused)
    assert paused.is_approved is False

    logged_in = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "restored", "signature": "stub", "card_serial": None},
    )
    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json().get("access_token")
    db.refresh(restored)
    assert restored.is_active is True
    assert restored.is_approved is True
    assert restored.disabled_reason is None


def test_put_active_then_inactive_clears_inactivity_and_rejects_old_tokens(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    """PUT 啟用／停用要走跟恢復、停用一樣的收尾。

    閒置停用改成啟用後再停用，刷卡只能看到「已停用」，不能走回待審。
    停用前發出的權杖要失效。
    """
    user = make_user(db, username="9100066")
    user.is_active = False
    user.disabled_reason = "inactivity"
    user.is_approved = True
    db.commit()
    make_user(db, username="life-admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'life-admin')}"}

    enabled = client.put(
        f"/api/users/{user.id}",
        headers=headers,
        json={"is_active": True},
    )
    assert enabled.status_code == 200, enabled.text
    db.expire_all()
    user = db.get(User, user.id)
    assert user.is_active is True
    assert user.is_approved is True
    assert user.disabled_reason is None
    version_while_active = user.token_version

    token = login(client, "9100066")
    still = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert still.status_code == 200, still.text

    disabled = client.put(
        f"/api/users/{user.id}",
        headers=headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.text
    db.expire_all()
    user = db.get(User, user.id)
    assert user.is_active is False
    assert user.is_approved is True
    assert user.disabled_reason is None
    assert user.token_version > (version_while_active or 0)
    revoked = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.user_id == user.id)
        .one()
    )
    assert revoked.revoked_at_version == user.token_version

    rejected = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rejected.status_code == 401, rejected.text

    _stub(monkeypatch, user)
    client.cookies.clear()
    card = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
    )
    assert card.status_code == 403, card.text
    assert "已停用" in card.json()["detail"]
    db.expire_all()
    user = db.get(User, user.id)
    assert user.is_approved is True
    assert user.is_active is False
    assert user.disabled_reason is None


def test_reenable_wins_over_a_stale_inactivity_reopen(db):
    """管理員已經重新啟用時，閒置恢復不能把帳號改回待審。"""
    from sqlalchemy import update

    from app.services.inactivity_service import resume_after_inactivity

    user = make_user(db, username="race-reopen")
    user.is_active = False
    user.disabled_reason = "inactivity"
    user.is_approved = True
    db.commit()
    db.execute(
        update(User)
        .where(User.id == user.id)
        .values(is_active=True, is_approved=True, disabled_reason=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    assert user.is_active is False

    assert resume_after_inactivity(db, user) is None
    db.expire_all()
    fresh = db.get(User, user.id)
    assert fresh.is_active is True
    assert fresh.is_approved is True
    assert fresh.disabled_reason is None
    assert "inactivity_reapproval" not in _actions(db)


def test_oidc_pending_approval_is_a_readable_page(client, db, monkeypatch):
    from app.api.auth import oidc as oidc_api
    from app.models.auth_provider import AuthProvider

    user = make_user(db, username="oidc-pending", is_approved=False)
    user.is_active = True
    user.disabled_reason = None
    provider = AuthProvider(
        name="pending-provider",
        provider_type="oidc",
        is_active=True,
        default_role="user",
    )
    db.add(provider)
    db.commit()
    monkeypatch.setattr(
        oidc_api,
        "decode_external_state",
        lambda state: {"provider_id": provider.id, "next_path": "/"},
    )

    async def _fake_oidc(db, provider, code, state_payload):
        return user

    monkeypatch.setattr(oidc_api, "authenticate_oidc_code", _fake_oidc)
    callback = client.get(
        f"/api/auth/oidc/{provider.id}/callback",
        params={"code": "stub", "state": "stub"},
    )
    assert callback.status_code != 401, callback.text
    assert "等待核准" in callback.text
    assert "access_token" not in callback.text
    assert "anila_access" not in callback.headers.get("set-cookie", "")
    db.expire_all()
    stored = db.get(User, user.id)
    assert stored.is_approved is False
    assert stored.is_active is True


def test_inactivity_notice_stays_visible_while_a_run_is_in_progress(db):
    from app.models.account_inactivity import AccountInactivityState

    db.add(AccountInactivityState(
        id=1,
        preview_completed=True,
        phase="applying",
        notice_count=4,
        notice_days=180,
    ))
    db.commit()
    assert inactivity_notice(db) == {"phase": "applying", "count": 4, "days": 180}
    row = db.get(AccountInactivityState, 1)
    row.phase = "previewing"
    db.commit()
    assert inactivity_notice(db) == {"phase": "previewing", "count": 4, "days": 180}


def test_manual_disable_stays_rejected_on_card_login(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    user = make_user(db, username="9100098")
    user.is_active = False
    user.disabled_reason = None
    db.commit()
    _stub(monkeypatch, user)
    resp = client.post(
        "/api/auth/card/verify",
        json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
    )
    assert resp.status_code == 403, resp.text
    assert "access_token" not in resp.json()
    db.refresh(user)
    assert user.is_approved is True
    assert user.is_active is False


def test_record_successful_login_requires_active_and_approved(db):
    from app.services.auth_service import record_successful_login

    user = make_user(db, username="stamp-gate")
    user.is_active = False
    db.commit()
    assert record_successful_login(db, user) is False
    db.refresh(user)
    assert user.last_login_at is None

    user.is_active = True
    user.is_approved = False
    db.commit()
    assert record_successful_login(db, user) is False
    db.refresh(user)
    assert user.last_login_at is None


def test_password_login_does_not_issue_tokens_if_disable_wins(client, db, db_engine):
    user = make_user(db, username="login-race")
    raced = {"done": False}

    def _disable_first(conn, cursor, statement, parameters, context, executemany):
        if raced["done"]:
            return
        sql = " ".join(str(statement).lower().split())
        set_clause = sql.split(" where ", 1)[0]
        if sql.startswith("update") and "users" in sql and "last_login_at" in set_clause:
            raced["done"] = True
            cursor.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?",
                (user.id,),
            )

    event.listen(db_engine, "before_cursor_execute", _disable_first)
    try:
        resp = client.post(
            "/api/auth/login",
            json={"username": "login-race", "password": "password"},
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", _disable_first)

    assert raced["done"] is True
    assert resp.status_code != 200, resp.text
    assert "access_token" not in resp.text
    db.expire_all()
    fresh = db.query(User).filter(User.id == user.id).one()
    assert fresh.last_login_at is None


def test_card_login_does_not_issue_tokens_if_disable_wins(
    client, db, db_engine, monkeypatch, card_login_on
):
    from app.models.department import Department

    unit = Department(name="刷卡單位", is_active=True)
    db.add(unit)
    db.commit()
    user = make_user(db, username="card-race", department_id=unit.id)
    _stub(monkeypatch, user)
    raced = {"done": False}

    def _disable_first(conn, cursor, statement, parameters, context, executemany):
        if raced["done"]:
            return
        sql = " ".join(str(statement).lower().split())
        set_clause = sql.split(" where ", 1)[0]
        if sql.startswith("update") and "users" in sql and "last_login_at" in set_clause:
            raced["done"] = True
            cursor.execute(
                "UPDATE users SET is_active = 0 WHERE id = ?",
                (user.id,),
            )

    event.listen(db_engine, "before_cursor_execute", _disable_first)
    try:
        resp = client.post(
            "/api/auth/card/verify",
            json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
        )
    finally:
        event.remove(db_engine, "before_cursor_execute", _disable_first)

    assert raced["done"] is True
    assert "access_token" not in resp.text
    assert resp.status_code != 200, resp.text
    db.expire_all()
    fresh = db.query(User).filter(User.id == user.id).one()
    assert fresh.last_login_at is None


def test_overlapping_sweep_returns_the_stored_count_and_does_not_write_zero(tmp_path, monkeypatch):
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import app.models  # noqa: F401 — register tables
    import app.services.inactivity_service as sweep
    from app.database import Base
    from app.models.account_inactivity import AccountInactivityState
    from app.models.audit_log import AuditLog
    from app.utils.security import hash_password

    moment = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sweep.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    seed = Session()
    stale = User(
        username="overlap-stale",
        hashed_password=hash_password("password"),
        role="user",
        is_active=True,
        is_approved=True,
        last_login_at=moment - timedelta(days=400),
        created_at=moment - timedelta(days=500),
    )
    seed.add(stale)
    seed.add(AccountInactivityState(
        id=1,
        preview_completed=True,
        phase="preview",
        notice_count=2,
        notice_days=180,
        last_run_on=(moment - timedelta(days=1)).date(),
    ))
    seed.commit()
    seed.close()

    entered = threading.Event()
    release = threading.Event()
    gate = {"open": False}
    real_disable = sweep._disable_if_still_idle

    def _slow_disable(db, user, cutoff):
        if not gate["open"]:
            gate["open"] = True
            entered.set()
            assert release.wait(timeout=8)
        return real_disable(db, user, cutoff)

    monkeypatch.setattr(sweep, "_disable_if_still_idle", _slow_disable)
    results: list[dict | str] = []

    def _run() -> None:
        db = Session()
        try:
            results.append(sweep.run_inactivity_sweep(db, now=moment))
        except Exception as exc:  # noqa: BLE001 — surfaced as a result
            results.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    first = threading.Thread(target=_run)
    second = threading.Thread(target=_run)
    first.start()
    assert entered.wait(timeout=8)
    second.start()
    second.join(timeout=8)
    release.set()
    first.join(timeout=8)
    assert not first.is_alive()
    assert not second.is_alive()
    assert all(isinstance(item, dict) and item["count"] != 0 for item in results), results

    check = Session()
    try:
        state = check.query(AccountInactivityState).filter(AccountInactivityState.id == 1).one()
        assert state.notice_count != 0
        applied = (
            check.query(AuditLog)
            .filter(AuditLog.action == "inactivity_sweep_applied")
            .count()
        )
        assert applied == 1
    finally:
        check.close()


def test_disable_notice_counts_only_the_latest_run(db):
    """藍色通知的人數是這一輪停用的，不是把前幾天加總。"""
    older = make_user(db, username="older-idle")
    later = make_user(db, username="later-idle")
    _age(older, login_days=400, created_days=500)
    _age(later, login_days=10, created_days=500)
    db.commit()
    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    assert run_inactivity_sweep(db, now=moment)["phase"] == "preview"
    first_apply = run_inactivity_sweep(db, now=moment + timedelta(days=1))
    assert first_apply == {"phase": "applied", "count": 1, "days": 180}
    later.last_login_at = _ago(400)
    db.commit()
    second_apply = run_inactivity_sweep(db, now=moment + timedelta(days=2))
    assert second_apply == {"phase": "applied", "count": 1, "days": 180}
    assert inactivity_notice(db) == {"phase": "applied", "count": 1, "days": 180}


def test_one_bad_row_does_not_abort_and_retries_the_same_day(db, monkeypatch):
    """一列失敗留下那個人，其餘照停。同一天再跑把剩下的做完。"""
    import app.services.inactivity_service as sweep

    good = make_user(db, username="good-row")
    bad = make_user(db, username="bad-row")
    for user in (good, bad):
        _age(user, login_days=400, created_days=500)
    db.commit()
    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    assert run_inactivity_sweep(db, now=moment)["phase"] == "preview"
    real = sweep.commit_token_revocation

    def _fail_bad_row(session, user):
        if user.username == "bad-row":
            raise RuntimeError("bad row")
        real(session, user)

    monkeypatch.setattr(sweep, "commit_token_revocation", _fail_bad_row)
    try:
        partial = run_inactivity_sweep(db, now=moment + timedelta(days=1))
    finally:
        monkeypatch.setattr(sweep, "commit_token_revocation", real)
    assert partial["phase"] == "applying"
    db.expire_all()
    good = db.query(User).filter(User.username == "good-row").one()
    bad = db.query(User).filter(User.username == "bad-row").one()
    assert good.is_active is False
    assert bad.is_active is True
    finished = run_inactivity_sweep(db, now=moment + timedelta(days=1, hours=1))
    assert finished == {"phase": "applied", "count": 2, "days": 180}
    db.expire_all()
    bad = db.query(User).filter(User.username == "bad-row").one()
    assert bad.is_active is False
    assert bad.disabled_reason == "inactivity"


def test_incomplete_sweep_retries_in_five_minutes_not_at_midnight():
    import inspect

    from app.services.inactivity_service import delay_after_sweep, start_inactivity_sweep

    now = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    assert delay_after_sweep(now, phase="applying", failed=False) == 300
    assert delay_after_sweep(now, phase="previewing", failed=False) == 300
    assert delay_after_sweep(now, phase="applied", failed=True) == 300
    until_midnight = delay_after_sweep(now, phase="applied", failed=False)
    assert until_midnight == 16 * 3600
    assert delay_after_sweep(now, phase="preview", failed=False) == until_midnight
    source = inspect.getsource(start_inactivity_sweep)
    assert "delay_after_sweep" in source


def test_pending_accounts_are_not_auto_disabled_is_documented():
    import inspect
    from pathlib import Path

    import app.services.inactivity_service as sweep

    text = "\n".join(
        part or ""
        for part in (
            inspect.getdoc(sweep),
            inspect.getdoc(sweep._candidates),
        )
    )
    assert "尚未核准" in text
    assert "登不進" in text
    status = Path(__file__).resolve().parents[3] / "docs" / "CURRENT-STATUS.md"
    note = status.read_text()
    assert "尚未核准" in note
    assert "登不進" in note


def test_crashed_apply_resumes_the_same_day_and_the_count_matches(db, monkeypatch):
    import app.services.inactivity_service as sweep

    first = make_user(db, username="crash-a")
    second = make_user(db, username="crash-b")
    for user in (first, second):
        _age(user, login_days=400, created_days=500)
    db.commit()
    moment = datetime.now(timezone.utc).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    assert run_inactivity_sweep(db, now=moment)["phase"] == "preview"
    real = sweep.commit_token_revocation
    calls = {"n": 0}

    def _crash_after_first(session, user):
        calls["n"] += 1
        real(session, user)
        if calls["n"] == 1:
            raise RuntimeError("sweep crashed")

    monkeypatch.setattr(sweep, "commit_token_revocation", _crash_after_first)
    partial = run_inactivity_sweep(db, now=moment + timedelta(days=1))
    monkeypatch.setattr(sweep, "commit_token_revocation", real)
    assert partial["phase"] == "applying"
    db.expire_all()
    disabled = {
        row.username
        for row in db.query(User).filter(User.username.in_(("crash-a", "crash-b"))).all()
        if not row.is_active
    }
    assert disabled == {"crash-a", "crash-b"}
    resumed = run_inactivity_sweep(db, now=moment + timedelta(days=1, hours=2))
    assert resumed == {"phase": "applied", "count": 2, "days": 180}
    db.expire_all()
    for username in ("crash-a", "crash-b"):
        row = db.query(User).filter(User.username == username).one()
        assert row.is_active is False
        assert row.disabled_reason == "inactivity"
    assert _actions(db).count("inactivity_disable") == 2
    assert _actions(db).count("inactivity_sweep_applied") == 1


def test_day_claim_documents_a_crash_and_upsert_is_per_dialect():
    import inspect

    from app.services.inactivity_service import _claim_run_day, _ensure_singleton

    claim = inspect.getdoc(_claim_run_day) or ""
    assert "中斷" in claim or "當機" in claim
    assert "下一個" in claim
    source = inspect.getsource(_ensure_singleton)
    assert '"postgresql"' in source
    assert '"sqlite"' in source
    assert "one_or_none" in source


def test_lifecycle_migration_seeds_the_inactivity_singleton():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0063_account_lifecycle.py"
    )
    upgrade = path.read_text().split("def downgrade", 1)[0]
    assert "bulk_insert" in upgrade
    assert '"id": 1' in upgrade or "'id': 1" in upgrade


def test_two_first_sweeps_do_not_collide_on_the_singleton(tmp_path):
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import app.models  # noqa: F401 — register tables
    import app.services.inactivity_service as sweep
    from app.database import Base
    from app.models.account_inactivity import AccountInactivityState
    from app.models.audit_log import AuditLog

    moment = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'first-sweep.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = threading.Barrier(2)
    results: list[dict | str] = []

    def _run() -> None:
        db = Session()
        try:
            barrier.wait(timeout=5)
            results.append(sweep.run_inactivity_sweep(db, now=moment))
        except Exception as exc:  # noqa: BLE001 — surfaced as a result
            results.append(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert all(isinstance(item, dict) for item in results), results
    check = Session()
    try:
        rows = check.query(AccountInactivityState).all()
        assert len(rows) == 1
        assert rows[0].id == 1
        previews = (
            check.query(AuditLog)
            .filter(AuditLog.action == "inactivity_sweep_preview")
            .count()
        )
        assert previews == 1
    finally:
        check.close()
        engine.dispose()

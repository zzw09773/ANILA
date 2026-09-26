"""CSP 自己管 JWT 簽章金鑰：加密保存、重疊輪替、緊急輪替。"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services.settings_registry import require_spec
from tests.conftest import login, make_user

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
EMERGENCY_PATH = "/api/auth/jwt-keyring/emergency-rotation"


@pytest.fixture
def pem_pair(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt-private.pem"
    pub_path = tmp_path / "jwt-public.pem"
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)

    from app.utils import security as sec

    monkeypatch.setattr(sec, "JWT_PRIVATE_KEY_PATH", str(priv_path))
    monkeypatch.setattr(sec, "JWT_PUBLIC_KEY_PATH", str(pub_path))
    sec._load_keys.cache_clear()
    yield private_key, priv_path, pub_path, private_pem, public_pem
    sec._load_keys.cache_clear()


def _advance(db, now):
    from app.services.jwt_keyring import advance_jwt_keyring

    advance_jwt_keyring(db, now=now)
    db.flush()


def _kids(db) -> set[str]:
    from app.api.jwks import _serialize_jwks

    return {item["kid"] for item in _serialize_jwks(db)["keys"]}


def _active(db):
    from app.models.jwt_signing_key import JwtSigningKey

    return db.query(JwtSigningKey).filter_by(state="active").one()


def _state(db, state: str):
    from app.models.jwt_signing_key import JwtSigningKey

    return db.query(JwtSigningKey).filter_by(state=state).all()


def _private_pem(row) -> bytes:
    from anila_core.security.credential_crypto import decrypt_credential

    return decrypt_credential(
        row.private_ciphertext, row.private_nonce, row.private_tag
    ).encode("utf-8")


def _sign(private_pem: bytes, kid: str, *, exp_minutes: int = 5, **claims) -> str:
    payload = {
        "sub": "1",
        "type": "access",
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(minutes=exp_minutes)).timestamp()
        ),
    }
    payload.update(claims)
    return jwt.encode(
        payload,
        private_pem,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


def test_private_key_is_encrypted_at_rest_and_jwks_publishes_only_public(db, pem_pair):
    from app.services.jwt_keyring import ensure_bootstrapped

    assert ensure_bootstrapped(db, now=T0) is True
    row = _active(db)
    blob = bytes(row.private_ciphertext)
    assert b"BEGIN" not in blob
    assert b"PRIVATE" not in blob
    assert _private_pem(row) == pem_pair[3]
    assert row.public_pem.encode("utf-8") == pem_pair[4]
    assert row.kid == settings.JWT_KID
    assert row.state == "active"


def test_bootstrap_imports_existing_pem_kid_and_does_not_delete_files(db, pem_pair):
    from app.utils.security import create_access_token, decode_token
    from app.services.jwt_keyring import ensure_bootstrapped

    _key, priv_path, pub_path, private_pem, public_pem = pem_pair
    before_priv = priv_path.read_bytes()
    before_pub = pub_path.read_bytes()
    ensure_bootstrapped(db, now=T0)
    token = create_access_token({"sub": "7"}, db=db)
    assert jwt.get_unverified_header(token)["kid"] == settings.JWT_KID
    assert decode_token(token, db=db)["sub"] == "7"
    # 匯入前就用這把 PEM 簽的權杖，匯入後仍然有效。
    prior = _sign(private_pem, settings.JWT_KID)
    assert decode_token(prior, db=db)["sub"] == "1"
    assert priv_path.read_bytes() == before_priv
    assert pub_path.read_bytes() == before_pub
    priv_path.unlink()
    pub_path.unlink()
    again = create_access_token({"sub": "8"}, db=db)
    assert decode_token(again, db=db)["sub"] == "8"


def test_later_pem_replacement_does_not_replace_the_active_key(db, pem_pair, tmp_path):
    from app.services.jwt_keyring import ensure_bootstrapped
    from app.utils import security as sec

    ensure_bootstrapped(db, now=T0)
    original_public = _active(db).public_pem
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_path = tmp_path / "replacement-private.pem"
    pub_path = tmp_path / "replacement-public.pem"
    priv_path.write_bytes(
        other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        other.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    sec.JWT_PRIVATE_KEY_PATH = str(priv_path)
    sec.JWT_PUBLIC_KEY_PATH = str(pub_path)
    sec._load_keys.cache_clear()
    assert ensure_bootstrapped(db, now=T0 + timedelta(days=1)) is False
    assert _active(db).public_pem == original_public
    assert _active(db).kid == settings.JWT_KID


def test_missing_pem_error_points_at_keyring_recovery_not_a_regen_script(
    db, tmp_path, monkeypatch
):
    from app.services.jwt_keyring import ensure_bootstrapped
    from app.utils.security import JwtKeyLoadError
    from app.utils import security as sec

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sec, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "missing-private.pem"))
    monkeypatch.setattr(sec, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "missing-public.pem"))
    sec._load_keys.cache_clear()
    with pytest.raises(JwtKeyLoadError) as exc:
        ensure_bootstrapped(db, now=T0)
    text = str(exc.value)
    assert "generate-jwt-keypair" not in text
    assert "deploy-prod.sh" not in text
    assert "金鑰圈" in text
    assert "SECRET_KEY" in text
    assert "不會刪" in text


def test_sign_uses_active_only_and_next_is_not_accepted_for_verification(db, pem_pair):
    from app.services.jwt_keyring import JWKS_CACHE_MAX_AGE_SECONDS
    from app.services.proxy.dispatch_token import (
        issue_dispatch_token,
        verify_dispatch_token,
    )
    from app.utils.security import create_access_token, decode_token

    _advance(db, T0)
    # 預設 90 天、提前 7 天公布 next。
    _advance(db, T0 + timedelta(days=83))
    active = _active(db)
    nxt = _state(db, "next")
    assert len(nxt) == 1
    assert nxt[0].kid != active.kid
    access = create_access_token({"sub": "3"}, db=db)
    dispatch = issue_dispatch_token(user_id=3, department=1, agent_id=9, db=db)
    assert jwt.get_unverified_header(access)["kid"] == active.kid
    assert jwt.get_unverified_header(dispatch)["kid"] == active.kid
    assert decode_token(_sign(_private_pem(nxt[0]), nxt[0].kid), db=db) is None
    assert decode_token(_sign(_private_pem(active), "not-published"), db=db) is None
    assert decode_token(
        jwt.encode(
            {"sub": "1", "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())},
            _private_pem(active),
            algorithm="RS256",
        ),
        db=db,
    ) is None
    hs = jwt.encode(
        {"sub": "1", "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())},
        "attacker-secret",
        algorithm="HS256",
        headers={"kid": active.kid, "typ": "JWT"},
    )
    assert decode_token(hs, db=db) is None
    assert _kids(db) == {active.kid, nxt[0].kid}
    # next 公布未滿一個快取週期、輪替日也未到，不得升成 active。
    _advance(db, T0 + timedelta(days=83, seconds=JWKS_CACHE_MAX_AGE_SECONDS))
    assert _active(db).kid == active.kid


def test_scheduled_promotion_keeps_old_key_through_max_refresh_plus_margin(db, pem_pair):
    from app.api.jwks import _CACHE_HEADER_VALUE
    from app.services.jwt_keyring import JWKS_CACHE_MAX_AGE_SECONDS
    from app.services.proxy.dispatch_token import (
        build_dispatch_claims,
        verify_dispatch_token,
    )
    from app.utils.security import create_access_token, decode_token

    assert f"max-age={JWKS_CACHE_MAX_AGE_SECONDS}" in _CACHE_HEADER_VALUE
    spec = require_spec("auth.refresh_token_expire_days")
    max_days = spec.domain_fn.bounds[1]
    set_setting(db, "auth.refresh_token_expire_days", 1)
    _advance(db, T0)
    original = _active(db)
    original_kid = original.kid
    original_pem = _private_pem(original)
    # 權杖的 iat 用金鑰圈的時鐘，不用測試機器的現在時間。
    # 否則 retiring 的簽發截止點會落在 iat 前面，把升級前合法的權杖判掉。
    access = _sign(
        original_pem,
        original_kid,
        sub="4",
        iat=int(T0.timestamp()),
        exp_minutes=30,
    )
    dispatch_claims = build_dispatch_claims(
        user_id=4,
        department=2,
        agent_id=8,
        issued_at=T0,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    dispatch = jwt.encode(
        dispatch_claims,
        original_pem,
        algorithm="RS256",
        headers={"kid": original_kid, "typ": "JWT"},
    )

    _advance(db, T0 + timedelta(days=82, seconds=86399))
    assert _state(db, "next") == []
    _advance(db, T0 + timedelta(days=83))
    assert len(_state(db, "next")) == 1
    _advance(db, T0 + timedelta(days=90) - timedelta(seconds=1))
    assert _active(db).kid == original_kid
    promoted_at = T0 + timedelta(days=90)
    _advance(db, promoted_at)
    assert _active(db).kid != original_kid
    retired_soon = db.query(type(original)).filter_by(kid=original_kid).one()
    assert retired_soon.state == "retiring"
    assert original_kid in _kids(db)
    assert decode_token(access, db=db)["sub"] == "4"
    assert verify_dispatch_token(dispatch, db=db)["agent_id"] == 8

    still_valid_at = promoted_at + timedelta(days=max_days)
    _advance(db, still_valid_at)
    assert original_kid in _kids(db)
    assert decode_token(access, db=db) is not None

    gone_at = still_valid_at + timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS)
    _advance(db, gone_at)
    assert original_kid not in _kids(db)
    assert decode_token(access, db=db) is None
    assert verify_dispatch_token(dispatch, db=db) is None
    assert db.query(type(original)).filter_by(kid=original_kid).one().state == "retired"


def test_late_maintainer_does_not_activate_a_key_published_in_the_same_tick(db, pem_pair):
    from app.services.jwt_keyring import JWKS_CACHE_MAX_AGE_SECONDS

    _advance(db, T0)
    original = _active(db).kid
    jumped = T0 + timedelta(days=200)
    _advance(db, jumped)
    assert _active(db).kid == original
    nxt = _state(db, "next")
    assert len(nxt) == 1
    _advance(db, jumped + timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS - 1))
    assert _active(db).kid == original
    _advance(db, jumped + timedelta(seconds=JWKS_CACHE_MAX_AGE_SECONDS))
    assert _active(db).kid == nxt[0].kid


def test_rotation_days_is_a_bounded_platform_setting_read_each_pass(db, pem_pair):
    assert get_setting(db, "auth.jwt_rotation_days") == 90
    with pytest.raises(ValueError):
        set_setting(db, "auth.jwt_rotation_days", 0)
    with pytest.raises(ValueError):
        set_setting(db, "auth.jwt_rotation_days", 366)
    set_setting(db, "auth.jwt_rotation_days", 10)
    _advance(db, T0)
    _advance(db, T0 + timedelta(days=5) - timedelta(seconds=1))
    assert _state(db, "next") == []
    _advance(db, T0 + timedelta(days=5))
    assert len(_state(db, "next")) == 1
    # 值域外的存值要退回預設 90，不能拿 9999 天去排輪替。
    stored = db.get(PlatformSetting, "auth.jwt_rotation_days")
    stored.value = "9999"
    db.flush()
    assert get_setting(db, "auth.jwt_rotation_days") == 90


def test_only_one_active_and_one_next_row(db, pem_pair):
    from app.models.jwt_signing_key import JwtSigningKey

    _advance(db, T0)
    _advance(db, T0 + timedelta(days=83))
    active = _active(db)
    dup = JwtSigningKey(
        kid="anila-dup-active",
        state="active",
        private_ciphertext=active.private_ciphertext,
        private_nonce=active.private_nonce,
        private_tag=active.private_tag,
        public_pem=active.public_pem,
        created_at=T0,
        activated_at=T0,
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_advance_takes_postgres_advisory_lock(db, monkeypatch):
    from app.services.jwt_keyring import (
        JWT_KEYRING_ADVISORY_LOCK_KEY,
        acquire_jwt_keyring_lock,
    )

    executed: list[str] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    def _capture(stmt, *args, **kwargs):
        executed.append(str(stmt))

        class _Result:
            def fetchone(self):
                return (None,)

        return _Result()

    monkeypatch.setattr(db, "get_bind", lambda: _Bind())
    monkeypatch.setattr(db, "execute", _capture)
    acquire_jwt_keyring_lock(db)
    assert any("pg_advisory_xact_lock" in sql for sql in executed)
    assert any(str(JWT_KEYRING_ADVISORY_LOCK_KEY) in sql or ":k" in sql for sql in executed)


def test_advance_locks_before_mutating(db, pem_pair, monkeypatch):
    from app.services import jwt_keyring

    order: list[str] = []
    real = jwt_keyring.acquire_jwt_keyring_lock

    def _wrapped(session):
        order.append("lock")
        return real(session)

    monkeypatch.setattr(jwt_keyring, "acquire_jwt_keyring_lock", _wrapped)
    jwt_keyring.advance_jwt_keyring(db, now=T0)
    assert order[0] == "lock"


def test_maintainer_follows_service_client_pattern():
    from app.main import lifespan
    from app.services.jwt_keyring import (
        JWT_KEYRING_MAINTAIN_INTERVAL_SECONDS,
        maintainer_enabled,
        start_jwt_keyring_maintainer,
    )

    assert JWT_KEYRING_MAINTAIN_INTERVAL_SECONDS >= 60
    src = inspect.getsource(start_jwt_keyring_maintainer)
    assert "asyncio.sleep" in src
    life = inspect.getsource(lifespan)
    assert "maintain_jwt_keyring_once" in life
    assert "start_jwt_keyring_maintainer" in life
    assert maintainer_enabled() is False


def test_emergency_rotation_retires_every_other_key_immediately(db, pem_pair):
    from app.services.jwt_keyring import emergency_rotate
    from app.services.proxy.dispatch_token import (
        issue_dispatch_token,
        verify_dispatch_token,
    )
    from app.utils.security import create_access_token, decode_token

    _advance(db, T0)
    _advance(db, T0 + timedelta(days=83))
    _advance(db, T0 + timedelta(days=90))
    old_access = create_access_token({"sub": "5"}, db=db)
    old_dispatch = issue_dispatch_token(user_id=5, department=1, agent_id=4, db=db)
    published_before = _kids(db)
    assert len(published_before) >= 2
    result = emergency_rotate(db, now=T0 + timedelta(days=91))
    assert result.kid not in published_before
    assert set(result.retired_kids) == published_before
    assert _kids(db) == {result.kid}
    assert decode_token(old_access, db=db) is None
    assert verify_dispatch_token(old_dispatch, db=db) is None
    fresh = create_access_token({"sub": "5"}, db=db)
    assert jwt.get_unverified_header(fresh)["kid"] == result.kid
    assert decode_token(fresh, db=db)["sub"] == "5"
    assert pem_pair[1].exists() and pem_pair[2].exists()


def _bearer(client, db, username, role):
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username)}"}


def test_emergency_endpoint_is_admin_or_owner_csrf_audited_and_confirmed(client, db, pem_pair):
    from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT
    from app.utils.security import create_access_token, decode_token

    user_headers = _bearer(client, db, "jwt-user", "user")
    denied = client.post(EMERGENCY_PATH, json={"confirm": EMERGENCY_CONFIRM_TEXT}, headers=user_headers)
    assert denied.status_code == 403

    admin_headers = _bearer(client, db, "jwt-admin", "admin")
    bad = client.post(EMERGENCY_PATH, json={"confirm": "輪替"}, headers=admin_headers)
    assert bad.status_code == 400
    assert EMERGENCY_CONFIRM_TEXT in bad.json()["detail"]
    assert db.query(AuditLog).filter_by(action="jwt_signing_key_emergency_rotation").count() == 0

    token = create_access_token({"sub": "9"}, db=db)
    db.commit()
    ok = client.post(
        EMERGENCY_PATH,
        json={"confirm": EMERGENCY_CONFIRM_TEXT},
        headers=admin_headers,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["detail"] == EMERGENCY_CONFIRM_TEXT
    assert "PRIVATE" not in ok.text and "BEGIN" not in ok.text
    db.expire_all()
    assert decode_token(token, db=db) is None
    audit = (
        db.query(AuditLog)
        .filter_by(action="jwt_signing_key_emergency_rotation")
        .one()
    )
    assert audit.resource_type == "jwt_signing_key"
    assert audit.resource_id == body["kid"]
    assert EMERGENCY_CONFIRM_TEXT in (audit.detail or "")
    assert "PRIVATE" not in (audit.metadata_json or "")

    owner_headers = _bearer(client, db, "jwt-owner", "owner")
    again = client.post(
        EMERGENCY_PATH,
        json={"confirm": EMERGENCY_CONFIRM_TEXT},
        headers=owner_headers,
    )
    assert again.status_code == 200, again.text


def test_emergency_cookie_requires_csrf(client, db, pem_pair):
    from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT

    make_user(db, username="jwt-csrf-admin", role="admin")
    logged = client.post(
        "/api/auth/login",
        json={"username": "jwt-csrf-admin", "password": "password"},
    )
    assert logged.status_code == 200, logged.text
    blocked = client.post(EMERGENCY_PATH, json={"confirm": EMERGENCY_CONFIRM_TEXT})
    assert blocked.status_code == 403
    token = client.cookies.get("anila_csrf")
    assert token
    allowed = client.post(
        EMERGENCY_PATH,
        json={"confirm": EMERGENCY_CONFIRM_TEXT},
        headers={"X-CSRF-Token": token},
    )
    assert allowed.status_code == 200, allowed.text


def test_comments_and_console_copy_match_the_keyring(db, pem_pair):
    from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT

    root = Path(__file__).resolve().parents[3]
    jwks_src = (root / "services/csp/app/api/jwks.py").read_text(encoding="utf-8")
    security_src = (root / "services/csp/app/utils/security.py").read_text(encoding="utf-8")
    assert "exactly one" not in jwks_src.lower()
    assert "Today we host exactly one" not in security_src
    assert "must match ``settings.JWT_KID``" not in security_src
    status = (root / "docs/CURRENT-STATUS.md").read_text(encoding="utf-8")
    assert "jwt_signing_keys" in status or "簽章金鑰" in status
    assert "不會刪" in status
    assert EMERGENCY_CONFIRM_TEXT in status
    vue = (root / "apps/csp-governance-ui/src/views/SettingsOverviewView.vue").read_text(encoding="utf-8")
    api = (root / "apps/csp-governance-ui/src/api/jwtKeyring.js").read_text(encoding="utf-8")
    assert EMERGENCY_CONFIRM_TEXT in vue
    assert EMERGENCY_CONFIRM_TEXT in api


def test_hot_path_does_not_take_keyring_lock_when_active_exists(db, pem_pair, monkeypatch):
    """已有 active 時，驗簽、簽章、JWKS 不得再拿 PostgreSQL 金鑰圈鎖。"""
    from app.services import jwt_keyring
    from app.services.proxy.dispatch_token import issue_dispatch_token, verify_dispatch_token
    from app.utils.security import create_access_token, decode_token, get_kid, get_private_key

    assert jwt_keyring.ensure_bootstrapped(db, now=T0) is True
    db.flush()
    calls: list[int] = []
    monkeypatch.setattr(
        jwt_keyring, "acquire_jwt_keyring_lock", lambda _session: calls.append(1)
    )
    kid = get_kid(db)
    get_private_key(db)
    token = create_access_token({"sub": "1"}, db=db)
    assert decode_token(token, db=db)["sub"] == "1"
    jwt_keyring.jwks_material(db)
    assert jwt_keyring.public_pem_for_published_kid(kid, db) is not None
    dispatched = issue_dispatch_token(user_id=1, department=1, agent_id=2, db=db)
    assert verify_dispatch_token(dispatched, db=db)["agent_id"] == 2
    assert calls == []


def test_bootstrap_still_locks_and_rechecks_before_insert(db, pem_pair, monkeypatch):
    """空表才拿鎖；鎖內若已出現 active，不得再插第二把。"""
    from app.models.jwt_signing_key import JwtSigningKey
    from app.services import jwt_keyring

    real = jwt_keyring.acquire_jwt_keyring_lock
    calls: list[str] = []

    def _lock(session):
        calls.append("lock")
        real(session)
        if session.query(JwtSigningKey).filter_by(state="active").one_or_none() is None:
            jwt_keyring._import_pem_as_active(session, T0)
            session.flush()

    monkeypatch.setattr(jwt_keyring, "acquire_jwt_keyring_lock", _lock)
    assert jwt_keyring.ensure_bootstrapped(db, now=T0) is False
    assert calls == ["lock"]
    assert db.query(JwtSigningKey).filter_by(state="active").count() == 1


def test_emergency_confirm_names_the_five_minute_agent_window():
    from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT

    assert "5 分鐘" in EMERGENCY_CONFIRM_TEXT


def test_emergency_rotation_publishes_retired_kids_for_the_revocation_cache(
    client, db, pem_pair, monkeypatch
):
    """緊急輪替提交後，把退役 kid 送到既有的撤銷通道，冷啟動清單也看得到。"""
    from app.services import token_revocation_publisher
    from app.services.jwt_keyring import EMERGENCY_CONFIRM_TEXT, ensure_bootstrapped

    ensure_bootstrapped(db, now=T0)
    db.commit()
    published_before = _kids(db)
    seen: list[list[str]] = []

    def _capture(kids, **kwargs):
        seen.append(list(kids))

    monkeypatch.setattr(
        token_revocation_publisher,
        "publish_kid_revocations_sync",
        _capture,
        raising=False,
    )
    admin = _bearer(client, db, "jwt-revoke-admin", "admin")
    ok = client.post(
        EMERGENCY_PATH,
        json={"confirm": EMERGENCY_CONFIRM_TEXT},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text
    assert len(seen) == 1
    assert set(seen[0]) == set(published_before)

    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-test-jwt-kid-revoke"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
    listed = client.get(
        "/api/auth/revocations",
        params={"since": "2020-01-01T00:00:00Z"},
        headers={"X-CSP-Service-Token": token},
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    kids = {item["kid"] if isinstance(item, dict) else item for item in body["revoked_kids"]}
    assert kids == set(published_before)


def test_next_key_is_published_but_not_accepted_for_verification(db, pem_pair):
    from app.services.proxy.dispatch_token import verify_dispatch_token
    from app.utils.security import decode_token

    _advance(db, T0)
    _advance(db, T0 + timedelta(days=83))
    nxt = _state(db, "next")[0]
    assert nxt.kid in _kids(db)
    access = _sign(_private_pem(nxt), nxt.kid)
    assert decode_token(access, db=db) is None
    dispatch = jwt.encode(
        {
            "iss": "anila-csp",
            "aud": "anila-agent",
            "sub": "3",
            "user_id": 3,
            "department": 1,
            "agent_id": 9,
            "iat": int(T0.timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
            "jti": "next-key",
        },
        _private_pem(nxt),
        algorithm="RS256",
        headers={"kid": nxt.kid, "typ": "JWT"},
    )
    assert verify_dispatch_token(dispatch, db=db) is None


def test_retiring_key_accepts_only_iat_before_retirement(db, pem_pair):
    from app.utils.security import decode_token

    _advance(db, T0)
    _advance(db, T0 + timedelta(days=83))
    _advance(db, T0 + timedelta(days=90))
    from app.time_utils import as_utc

    retiring = _state(db, "retiring")[0]
    # SQLite 讀回的 datetime 可能是 naive。截止點以 UTC 的 unix 秒為準，
    # 跟 JWKS 的 anila_iat_not_after 同一個整數。
    cutoff = int(as_utc(retiring.retiring_at).timestamp())
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())
    before = _sign(_private_pem(retiring), retiring.kid, exp_minutes=5, iat=cutoff - 1)
    after = _sign(_private_pem(retiring), retiring.kid, exp_minutes=5, iat=cutoff)
    assert decode_token(before, db=db) is not None
    assert decode_token(after, db=db) is None
    assert exp > cutoff


def test_missing_iat_is_accepted_only_for_the_imported_bootstrap_kid(db, pem_pair):
    from app.services.jwt_keyring import emergency_rotate
    from app.utils.security import decode_token

    _advance(db, T0)
    bootstrap = _active(db)
    legacy = _sign(_private_pem(bootstrap), bootstrap.kid)
    assert "iat" not in jwt.get_unverified_claims(legacy)
    assert decode_token(legacy, db=db) is not None

    rotated = emergency_rotate(db, now=T0 + timedelta(days=1))
    fresh = _active(db)
    assert fresh.kid == rotated.kid
    minted = _sign(_private_pem(fresh), fresh.kid)
    assert decode_token(minted, db=db) is None


def test_access_dispatch_and_launch_sign_with_one_key_row(db, pem_pair, monkeypatch):
    """兩次查詢之間如果 active 換人，不得用舊私鑰配上新 kid。"""
    from app.models.jwt_signing_key import JwtSigningKey
    from app.modules.launch.token import issue_launch_token
    from app.services import jwt_keyring
    from app.services.proxy.dispatch_token import issue_dispatch_token
    from app.utils.security import create_access_token, create_refresh_token
    from anila_core.security.credential_crypto import encrypt_credential

    jwt_keyring.ensure_bootstrapped(db, now=T0)
    real_row = _active(db)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    other_public = other.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ciphertext, nonce, tag = encrypt_credential(other_private.decode("utf-8"))
    decoy = JwtSigningKey(
        kid="anila-split-decoy",
        state="active",
        private_ciphertext=ciphertext,
        private_nonce=nonce,
        private_tag=tag,
        public_pem=other_public.decode("ascii"),
        created_at=T0,
        activated_at=T0,
    )
    real_one = jwt_keyring._one
    phase = {"after_private": False}

    def _spy(session, state):
        row = real_one(session, state)
        if state == "active" and phase["after_private"] and row is not None:
            return decoy
        return row

    real_private = jwt_keyring.active_private_pem

    def _private(session=None):
        try:
            return real_private(session)
        finally:
            phase["after_private"] = True

    monkeypatch.setattr(jwt_keyring, "_one", _spy)
    monkeypatch.setattr(jwt_keyring, "active_private_pem", _private)

    def _consistent(token: str) -> None:
        kid = jwt.get_unverified_header(token)["kid"]
        public_pem = decoy.public_pem if kid == decoy.kid else real_row.public_pem
        jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )

    _consistent(create_access_token({"sub": "1"}, db=db))
    phase["after_private"] = False
    _consistent(create_refresh_token({"sub": "1"}, db=db))
    phase["after_private"] = False
    _consistent(
        issue_dispatch_token(user_id=1, department=1, agent_id=2, db=db)
    )
    phase["after_private"] = False
    _consistent(
        issue_launch_token(
            {
                "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
            },
            db=db,
        )
    )

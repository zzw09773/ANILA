"""External authentication service.

LDAP 已自系統移除（將以 SSO 取代）；本模組僅保留 OIDC 流程，並在
provision 階段強制 ``email_verified`` 與 ``(provider_id, subject)`` 為唯一
身分鍵，避免攻擊者透過任意 OIDC provider 用既有 admin email 接管帳號。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth_provider import AuthProvider
from app.models.department import Department
from app.models.external_identity import ExternalIdentity
from app.models.user import User
from app.utils.security import hash_password


EXTERNAL_STATE_AUDIENCE = "external-auth"


def list_public_auth_providers(db: Session) -> list[AuthProvider]:
    return (
        db.query(AuthProvider)
        .filter(
            AuthProvider.is_active == True,
            AuthProvider.provider_type == "oidc",
        )
        .order_by(AuthProvider.provider_type, AuthProvider.name)
        .all()
    )


def issue_external_state(provider: AuthProvider, next_path: str = "/") -> str:
    payload = {
        "aud": EXTERNAL_STATE_AUDIENCE,
        "provider_id": provider.id,
        "next_path": next_path,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_external_state(state: str) -> dict:
    return jwt.decode(
        state,
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
        audience=EXTERNAL_STATE_AUDIENCE,
    )


def _resolve_oidc_secret(provider: AuthProvider) -> str:
    """Decrypt the stored OIDC client_secret. Empty string when unset."""
    from app.services.auth_provider_secret import load_oidc_client_secret

    return load_oidc_client_secret(provider) or ""


async def build_oidc_authorization_url(provider: AuthProvider, next_path: str = "/") -> str:
    metadata = await _resolve_oidc_metadata(provider)
    redirect_uri = f"{settings.SITE_URL.rstrip('/')}/api/auth/oidc/{provider.id}/callback"
    params = {
        "response_type": "code",
        "client_id": provider.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.oidc_scopes or "openid profile email",
        "state": issue_external_state(provider, next_path=next_path),
    }
    return f"{metadata['authorization_endpoint']}?{urlencode(params)}"


async def authenticate_oidc_code(
    db: Session,
    provider: AuthProvider,
    code: str,
) -> User:
    metadata = await _resolve_oidc_metadata(provider)
    redirect_uri = f"{settings.SITE_URL.rstrip('/')}/api/auth/oidc/{provider.id}/callback"
    client_secret = _resolve_oidc_secret(provider)
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": provider.oidc_client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_payload = token_resp.json()
        access_token = token_payload.get("access_token")
        if not access_token:
            raise ValueError("OIDC 回應缺少 access_token")

        userinfo_resp = await client.get(
            metadata["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    subject_claim = provider.oidc_subject_claim or "sub"
    username_claim = provider.oidc_username_claim or "preferred_username"
    email_claim = provider.oidc_email_claim or "email"
    subject = str(userinfo.get(subject_claim) or "")
    if not subject:
        raise ValueError("OIDC userinfo 缺少 subject claim")
    username = (
        userinfo.get(username_claim)
        or userinfo.get("preferred_username")
        or userinfo.get("name")
        or userinfo.get(email_claim)
        or subject
    )
    email = userinfo.get(email_claim)
    email_verified = bool(userinfo.get("email_verified"))

    return _provision_external_user(
        db,
        provider=provider,
        subject=subject,
        username=str(username),
        email=email,
        email_verified=email_verified,
    )


async def _resolve_oidc_metadata(provider: AuthProvider) -> dict:
    metadata = {
        "authorization_endpoint": provider.oidc_authorization_endpoint,
        "token_endpoint": provider.oidc_token_endpoint,
        "userinfo_endpoint": provider.oidc_userinfo_endpoint,
    }
    if all(metadata.values()):
        return metadata
    issuer = (provider.oidc_issuer_url or "").rstrip("/")
    if not issuer:
        raise ValueError("OIDC Provider 缺少 issuer_url")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        discovered = response.json()
    return {
        "authorization_endpoint": discovered["authorization_endpoint"],
        "token_endpoint": discovered["token_endpoint"],
        "userinfo_endpoint": discovered["userinfo_endpoint"],
    }


def _provision_external_user(
    db: Session,
    *,
    provider: AuthProvider,
    subject: str,
    username: str,
    email: str | None,
    email_verified: bool,
) -> User:
    """Resolve / create the local user backing this external identity.

    Identity binding strategy (Sprint 5 X security review §H2):

    1. The only identity key we accept is ``(provider_id, subject)``. Any
       previously-bound identity is reused as-is.
    2. We **never** silently merge into an existing local user just because
       the ``email`` claim happens to match. That used to allow a malicious
       OIDC provider to take over the local admin account by claiming the
       admin's email; refuse the bind and surface an explicit error so an
       admin can choose whether to manually link the accounts.
    3. ``email_verified`` is required for any new auto-provisioning so an
       attacker that controls a reusable email at an unknown provider
       cannot spawn a real-user-shaped row in our DB.
    """
    identity = (
        db.query(ExternalIdentity)
        .filter(
            ExternalIdentity.provider_id == provider.id,
            ExternalIdentity.external_subject == subject,
        )
        .first()
    )
    if identity:
        identity.last_login_at = datetime.now(timezone.utc)
        if email:
            identity.external_email = email
        db.commit()
        return identity.user

    if not provider.auto_create_users:
        raise ValueError("此外部登入來源未啟用自動建立使用者")

    if email and not email_verified:
        raise ValueError(
            "OIDC userinfo 未提供 email_verified=true；拒絕自動建立帳號。"
            "請於 IdP 端確認 email 已驗證或要求 admin 手動建立。"
        )

    # Refuse to silently bind to a local account just because email matches.
    # Email collisions are surfaced as a hard error — the admin must choose
    # whether to manually merge identities (out-of-band).
    if email:
        existing = db.query(User).filter(User.email == email).first()
        if existing is not None:
            raise ValueError(
                f"OIDC 回應的 email「{email}」已綁定到本地帳號；"
                "為避免帳號接管風險，自動合併已停用，請聯絡 admin 手動處理。"
            )

    candidate_user = User(
        username=_generate_unique_username(db, username or email or subject),
        email=email,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        role=provider.default_role or "user",
        department_id=_validate_default_department(db, provider.default_department_id),
        is_active=True,
        is_approved=True,
    )
    db.add(candidate_user)
    db.flush()

    identity = ExternalIdentity(
        user_id=candidate_user.id,
        provider_id=provider.id,
        external_subject=subject,
        external_username=username,
        external_email=email,
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(identity)
    db.commit()
    db.refresh(candidate_user)
    return candidate_user


def _generate_unique_username(db: Session, base: str) -> str:
    sanitized = "".join(c for c in (base or "external") if c.isalnum() or c in {"-", "_", "."})
    sanitized = sanitized[:80] or "external"
    candidate = sanitized
    suffix = 1
    while db.query(User).filter(User.username == candidate).first():
        suffix += 1
        candidate = f"{sanitized[:70]}-{suffix}"
    return candidate


def _validate_default_department(db: Session, department_id: int | None) -> int | None:
    if department_id is None:
        return None
    department = db.query(Department).filter(Department.id == department_id).first()
    return department.id if department and department.is_active else None

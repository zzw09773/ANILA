from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from jose import jwt
from ldap3 import Connection, Server, Tls
from ldap3.utils.conv import escape_filter_chars
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
        .filter(AuthProvider.is_active == True)
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
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": provider.oidc_client_id,
                "client_secret": provider.oidc_client_secret,
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

    return _provision_external_user(
        db,
        provider=provider,
        subject=subject,
        username=str(username),
        email=email,
    )


def authenticate_ldap(
    db: Session,
    provider: AuthProvider,
    username: str,
    password: str,
) -> User | None:
    server = Server(provider.ldap_server_uri or "", get_info=None)
    bind_kwargs = {"raise_exceptions": True}
    if provider.ldap_bind_dn:
        bind_kwargs.update(
            {
                "user": provider.ldap_bind_dn,
                "password": provider.ldap_bind_password or "",
            }
        )

    try:
        with Connection(server, **bind_kwargs) as search_conn:
            if provider.ldap_start_tls:
                search_conn.start_tls()

            search_filter = (provider.ldap_user_filter or "(uid={username})").format(
                username=escape_filter_chars(username)
            )
            search_conn.search(
                search_base=provider.ldap_base_dn or "",
                search_filter=search_filter,
                attributes=[
                    provider.ldap_email_attribute or "mail",
                    provider.ldap_display_name_attribute or "displayName",
                    "uid",
                    "cn",
                ],
            )
            if not search_conn.entries:
                return None
            entry = search_conn.entries[0]
            user_dn = entry.entry_dn
            email = _safe_ldap_attr(entry, provider.ldap_email_attribute or "mail")
            display_name = _safe_ldap_attr(
                entry,
                provider.ldap_display_name_attribute or "displayName",
            )
    except Exception:
        return None

    try:
        with Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=True,
            raise_exceptions=True,
        ):
            pass
    except Exception:
        return None

    provision_username = display_name or username
    return _provision_external_user(
        db,
        provider=provider,
        subject=user_dn,
        username=provision_username,
        email=email,
        fallback_username=username,
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
    fallback_username: str | None = None,
) -> User:
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

    candidate_user = None
    if email:
        candidate_user = db.query(User).filter(User.email == email).first()
    if not candidate_user:
        lookup_username = fallback_username or username
        candidate_user = db.query(User).filter(User.username == lookup_username).first()

    if not candidate_user:
        if not provider.auto_create_users:
            raise ValueError("此外部登入來源未啟用自動建立使用者")
        candidate_user = User(
            username=_generate_unique_username(db, fallback_username or username or email or "external"),
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
        external_username=fallback_username or username,
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


def _safe_ldap_attr(entry, name: str) -> str | None:
    try:
        value = entry[name].value
    except Exception:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None

"""Durable, digest-only JWT JTI and session-ID revocation primitives."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.token_revocation import TokenRevocation
from app.models.user import User


def identifier_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _current_version(db: Session, user_id: int) -> int:
    user = db.get(User, user_id)
    return int(user.token_version or 0) if user is not None else 0


def revoke_jti(
    db: Session,
    *,
    user_id: int,
    jti: str,
    token_type: str,
    reason: str,
    commit: bool = False,
) -> TokenRevocation:
    row = TokenRevocation(
        user_id=user_id,
        revoked_at_version=_current_version(db, user_id),
        scope="jti",
        token_jti_hash=identifier_digest(jti),
        token_type=token_type,
        reason=reason,
    )
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def revoke_sid(
    db: Session,
    *,
    user_id: int,
    sid: str,
    reason: str,
    commit: bool = False,
) -> TokenRevocation:
    now = datetime.now(timezone.utc)
    session = db.get(AuthSession, sid)
    if session is not None and session.user_id == user_id:
        session.revoked_at = now
        session.revoke_reason = reason
        (
            db.query(AuthRefreshToken)
            .filter(
                AuthRefreshToken.sid == sid,
                AuthRefreshToken.revoked_at.is_(None),
            )
            .update({"revoked_at": now}, synchronize_session=False)
        )
    row = TokenRevocation(
        user_id=user_id,
        revoked_at_version=_current_version(db, user_id),
        scope="sid",
        session_id_hash=identifier_digest(sid),
        reason=reason,
    )
    db.add(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return row


def is_revoked(
    db: Session,
    *,
    user_id: int,
    jti: str,
    sid: str,
    token_type: str,
) -> bool:
    """Check focused revocations on every CSP JWT authorization boundary."""
    jti_hash = identifier_digest(jti)
    sid_hash = identifier_digest(sid)
    row = (
        db.query(TokenRevocation.id)
        .filter(
            TokenRevocation.user_id == user_id,
            or_(
                and_(
                    TokenRevocation.scope == "jti",
                    TokenRevocation.token_jti_hash == jti_hash,
                    or_(
                        TokenRevocation.token_type.is_(None),
                        TokenRevocation.token_type == token_type,
                    ),
                ),
                and_(
                    TokenRevocation.scope == "sid",
                    TokenRevocation.session_id_hash == sid_hash,
                ),
            ),
        )
        .first()
    )
    return row is not None


__all__ = [
    "identifier_digest",
    "is_revoked",
    "revoke_jti",
    "revoke_sid",
]

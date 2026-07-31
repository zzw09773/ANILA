"""Token revocation endpoints.

Split from the original ``app/api/auth.py`` god-module — bodies moved
verbatim; only this import header is new.
"""
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin, verify_service_token
from app.services.token_revocation_publisher import publish_revocation_sync

from ._common import router


# ── Token revocation retention (cross-service sync) ────────────────────
# anila-studio cold-start GET /api/auth/revocations 用,
# 同步最近 N 天的 revocation events 進它的 cache。
TOKEN_REVOCATION_RETENTION_DAYS = 30


class RevocationEntry(BaseModel):
    user_id: int
    revoked_at_version: int
    ts: str


class RevocationListResponse(BaseModel):
    revocations: List[RevocationEntry]
    retention_days: int


class RevokeUserTokensRequest(BaseModel):
    user_id: int


class RevokeUserTokensResponse(BaseModel):
    user_id: int
    revoked_at_version: int


def _serialise_ts(ts: datetime) -> str:
    """Render a UTC ISO-8601 string with a ``Z`` suffix."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@router.post("/revoke", response_model=RevokeUserTokensResponse)
def revoke_user_tokens(
    request: RevokeUserTokensRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin/owner force-revokes all outstanding JWTs for one user.

    ⚠ Same contract as ``password.py::_commit_token_revocation``:
    ``revoked_at_version`` is the **post-bump** value — the lowest version
    still valid. Consumers reject ``tv < revoked_at_version``. The user is
    NOT locked out; they log in again and get a token at the new version.
    """
    user = db.get(User, request.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="使用者不存在")

    user.token_version = (user.token_version or 0) + 1
    revoked_at_version = int(user.token_version or 0)
    db.add(
        TokenRevocation(
            user_id=user.id,
            revoked_at_version=revoked_at_version,
        )
    )
    db.commit()
    publish_revocation_sync(
        user_id=user.id,
        revoked_at_version=revoked_at_version,
    )
    log_audit_event(
        db,
        actor=admin,
        action="auth.revoke",
        resource_type="user",
        resource_id=user.id,
        detail=f"管理員撤銷使用者「{user.username}」現有權杖",
        commit=True,
    )
    return RevokeUserTokensResponse(
        user_id=user.id,
        revoked_at_version=revoked_at_version,
    )


@router.get("/revocations", response_model=RevocationListResponse)
def list_revocations(
    since: datetime = Query(
        ...,
        description=(
            "Lower-bound timestamp (ISO-8601). Naive timestamps are "
            "treated as UTC. Clamped against the retention floor."
        ),
    ),
    db: Session = Depends(get_db),
    _identity: agent_credential_service.CallerIdentity | None = Depends(
        verify_service_token
    ),
):
    """Return revocation events after ``since``, capped to the retention
    window. Auth: ``X-CSP-Service-Token`` header (used by anila-studio
    cold-start sync). Sorted ASC by ``revoked_at``.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    retention_floor = datetime.now(timezone.utc) - timedelta(
        days=TOKEN_REVOCATION_RETENTION_DAYS
    )
    effective_since = max(since, retention_floor)

    rows = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.revoked_at >= effective_since)
        .order_by(TokenRevocation.revoked_at.asc())
        .all()
    )

    return RevocationListResponse(
        revocations=[
            RevocationEntry(
                user_id=row.user_id,
                revoked_at_version=row.revoked_at_version,
                ts=_serialise_ts(row.revoked_at),
            )
            for row in rows
        ],
        retention_days=TOKEN_REVOCATION_RETENTION_DAYS,
    )

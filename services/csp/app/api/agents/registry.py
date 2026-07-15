"""Internal versioned Agent registry projection for Router services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.agent_registry import AgentRegistrySnapshot
from app.services.agent_credential_service import CallerIdentity
from app.services.agent_registry import build_registry_snapshot
from app.services.auth_service import verify_service_token


router = APIRouter()


def _resolve_user_id(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "internal registry snapshot 必須帶 "
                "X-ANILA-Caller-User-Id caller context"
            ),
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-ANILA-Caller-User-Id 無效"
        ) from exc
    if value <= 0:
        raise HTTPException(status_code=400, detail="X-ANILA-Caller-User-Id 無效")
    return value


@router.get("/registry", response_model=AgentRegistrySnapshot)
def get_registry_snapshot(
    *,
    caller: CallerIdentity | None = Depends(verify_service_token),
    caller_user_id: str | None = Header(
        default=None, alias="X-ANILA-Caller-User-Id"
    ),
    db: Session = Depends(get_db),
) -> AgentRegistrySnapshot:
    """Return a caller-scoped authority snapshot to a named service only.

    Agent credentials and the legacy fleet token are deliberately rejected;
    this view is for Router-like ``service_clients`` and never a user API.
    """

    if caller is None or caller.kind != "service_client" or caller.is_legacy:
        raise HTTPException(status_code=403, detail="registry snapshot 僅限具名 service client")
    resolved_user_id = _resolve_user_id(caller_user_id)
    try:
        return build_registry_snapshot(db, user_id=resolved_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="caller user context 無效") from exc


__all__ = ["get_registry_snapshot", "router"]

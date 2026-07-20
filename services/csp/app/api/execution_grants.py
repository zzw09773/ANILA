"""Internal CSP-issued ExecutionGrant mint endpoint."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.execution_grant import (
    ExecutionGrantMintRequest,
    ExecutionGrantMintResponse,
)
from app.services.agent_credential_service import CallerIdentity
from app.services.auth_service import verify_service_token
from app.services.execution_grant_service import (
    ExecutionGrantMintDenied,
    mint_execution_grant,
)


router = APIRouter()


def _resolve_caller_user_id(raw: str | None) -> int:
    if raw is None or not raw:
        raise HTTPException(
            status_code=400,
            detail="ExecutionGrant mint 必須帶 X-ANILA-Caller-User-Id",
        )
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise HTTPException(status_code=400, detail="X-ANILA-Caller-User-Id 無效")
    return int(raw)


@router.post("/mint", response_model=ExecutionGrantMintResponse)
def mint_grant(
    request: ExecutionGrantMintRequest,
    *,
    caller: CallerIdentity | None = Depends(verify_service_token),
    caller_user_id: str | None = Header(
        default=None, alias="X-ANILA-Caller-User-Id"
    ),
    db: Session = Depends(get_db),
) -> ExecutionGrantMintResponse:
    """Mint only after CSP revalidates all current authority bindings.

    The response is either a complete signed envelope or an HTTP error; a
    rejected candidate never receives a partially populated token.
    """

    if caller is None or caller.kind != "service_client" or caller.is_legacy:
        raise HTTPException(
            status_code=403,
            detail="ExecutionGrant mint 僅限具名 service client",
        )
    resolved_user_id = _resolve_caller_user_id(caller_user_id)
    try:
        return mint_execution_grant(
            db,
            request=request,
            caller=caller,
            caller_user_id=resolved_user_id,
        )
    except (ExecutionGrantMintDenied, ValueError) as exc:
        # Keep authority details out of the transport response.  Internal
        # callers can inspect the typed exception; the token is never returned
        # on this path.
        raise HTTPException(
            status_code=403,
            detail="ExecutionGrant mint denied",
        ) from exc


__all__ = ["mint_grant", "router"]

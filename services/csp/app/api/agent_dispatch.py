"""Internal Router → CSP → Agent dispatch endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.agent_credential_service import CallerIdentity
from app.services.auth_service import verify_service_token
from app.services.agent_dispatch_service import (
    authorize_dispatch,
    dispatch_nonstream,
    dispatch_stream,
)


class AgentDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: StrictStr = Field(min_length=1, max_length=255)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: StrictBool = False
    anila_session_id: StrictStr = Field(min_length=1, max_length=255)
    anila_binding: dict[str, Any]


router = APIRouter()


def _grant_header(request: Request) -> str:
    canonical = request.headers.getlist("X-ANILA-Execution-Grant")
    # There is one canonical signed-envelope header.  An alternate header is
    # rejected even when the canonical value is present so a sink can never
    # acquire precedence ambiguity from duplicate/legacy credentials.
    if request.headers.getlist("X-ANILA-Execution-Grant-Token"):
        raise HTTPException(status_code=400, detail="不允許 alternate ExecutionGrant header")
    if len(canonical) != 1 or not canonical[0]:
        raise HTTPException(status_code=400, detail="必須提供單一 canonical signed ExecutionGrant token")
    return canonical[0]


@router.post("/dispatch", response_model=None)
async def dispatch_agent(
    request: Request,
    payload: AgentDispatchRequest,
    *,
    caller: CallerIdentity | None = Depends(verify_service_token),
    caller_user_id: str | None = Header(default=None, alias="X-ANILA-Caller-User-Id"),
    after_cursor: str | None = Header(default=None, alias="X-ANILA-After-Cursor"),
    db: Session = Depends(get_db),
):
    del caller_user_id  # The strict value is checked from request.headers.
    del after_cursor
    grant_token = _grant_header(request)
    binding = payload.anila_binding
    if payload.model != str(binding.get("agent_id") or ""):
        raise HTTPException(status_code=403, detail="model 必須是 canonical manifest agent_id")
    if payload.anila_session_id != str(binding.get("session_id") or ""):
        raise HTTPException(status_code=403, detail="session binding 不一致")
    authority = authorize_dispatch(
        db,
        caller=caller,
        headers=request.headers,
        binding_payload=binding,
        grant_token=grant_token,
    )
    if payload.model != authority.agent.name:
        raise HTTPException(status_code=403, detail="Agent canonical id 不一致")
    if payload.stream:
        raw_cursor = request.headers.get("X-ANILA-After-Cursor", "0")
        try:
            cursor = int(raw_cursor)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="X-ANILA-After-Cursor 無效") from exc
        if cursor < 0:
            raise HTTPException(status_code=400, detail="X-ANILA-After-Cursor 不得小於 0")
        return StreamingResponse(
            dispatch_stream(
                db=db,
                authority=authority,
                messages=payload.messages,
                grant_token=grant_token,
                after_cursor=cursor,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await dispatch_nonstream(
        db=db,
        authority=authority,
        messages=payload.messages,
        grant_token=grant_token,
    )
    return JSONResponse(result)


__all__ = ["AgentDispatchRequest", "dispatch_agent", "router"]

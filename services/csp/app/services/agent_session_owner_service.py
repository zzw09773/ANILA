"""CSP-side agent session ownership — one predicate for bind + resume.

Router already pins ``session_id → owner_key_hash`` in its SQLite. CSP's
public ``POST /v1/agents/{name}/sessions/{id}/answer`` must ask the same
question independently: does this caller own the session?

Both the agent chat face (when ``anila_session_id`` is present, after
permission/ceiling gates) and the resume face route through
:func:`ensure_agent_session_owner`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent_session_owner import AgentSessionOwner

# Router ``new_session_id`` is UUID hex; keep a tight allow-list so the
# resume URL cannot be path-shaped by a caller-controlled session_id.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _normalise_session_id(session_id: str, *, missing_is_error: bool) -> str | None:
    sid = (session_id or "").strip()
    if not sid:
        if missing_is_error:
            raise HTTPException(status_code=404, detail="Session not found")
        return None
    if not _SESSION_ID_RE.match(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return sid


def ensure_agent_session_owner(
    db: Session,
    *,
    session_id: str,
    owner_user_id: int,
    missing_is_error: bool = False,
) -> None:
    """Bind session → caller on first sight, or verify the existing bind.

    ``missing_is_error=True`` (resume): unknown session → 404, same as a
    mismatched owner, so existence is not an oracle.
    ``missing_is_error=False`` (chat): create the bind.
    """
    sid = _normalise_session_id(session_id, missing_is_error=missing_is_error)
    if sid is None:
        return
    row = (
        db.query(AgentSessionOwner)
        .filter(AgentSessionOwner.session_id == sid)
        .first()
    )
    if row is None:
        if missing_is_error:
            raise HTTPException(status_code=404, detail="Session not found")
        db.add(
            AgentSessionOwner(
                session_id=sid,
                owner_user_id=owner_user_id,
                created_at=datetime.now(timezone.utc),
            )
        )
        try:
            db.commit()
        except IntegrityError:
            # Concurrent first-bind on the same session_id — re-read winner.
            db.rollback()
            row = (
                db.query(AgentSessionOwner)
                .filter(AgentSessionOwner.session_id == sid)
                .first()
            )
            if row is None or row.owner_user_id != owner_user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Session belongs to a different caller.",
                ) from None
        return
    if row.owner_user_id != owner_user_id:
        # Chat keeps an explicit 403 so a concurrent double-dispatch race is
        # diagnosable; that also means chat can probe whether a session_id
        # is already bound (UUID hex makes guessing impractical). Resume
        # always collapses to 404 so existence is not an oracle there.
        if missing_is_error:
            raise HTTPException(status_code=404, detail="Session not found")
        raise HTTPException(
            status_code=403,
            detail="Session belongs to a different caller.",
        )

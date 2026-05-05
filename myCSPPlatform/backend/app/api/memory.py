"""Per-user memory inspection & control endpoints.

GDPR / 個資法 compliance surface for the memory system added in
P1 (``app.services.memory_service``). Every authenticated user can:

* See exactly what facts the platform has remembered about them.
* See a recent slice of which conversation messages got embedded
  for cross-conversation recall.
* Delete a single fact or wipe their entire memory.

All routes are scoped to ``current_user.id`` — there's no admin
override path here. An admin who needs to inspect another user's
memory should query the DB directly with an audit-logged session;
exposing it via REST would be a privacy regression.

The chunk endpoints intentionally don't expose the embedding vector
itself (4000 floats per row × hundreds of rows = a huge response
that's also useless to a human). Operators who need vectors hit
the DB directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services import agent_credential_service, audit_service
from app.services.auth_service import get_current_user, verify_service_token

router = APIRouter(prefix="/api/memory", tags=["memory"])


# Default cap on the chunk preview list. The UI table doesn't paginate
# in MVP, so we don't want a single response carrying thousands of
# multi-KB strings. Operators can override per-request via ``?limit=``
# up to ``_CHUNK_LIMIT_MAX``; beyond that point the recommended
# workflow is "wipe and start fresh" rather than scroll.
_CHUNK_LIMIT_DEFAULT = 50
_CHUNK_LIMIT_MAX = 500

# Trim each preview to keep the total payload reasonable. A real user
# turn often runs 1-3 KB; an unbounded list of those drives the
# settings dialog into a janky scroll.
_PREVIEW_CHARS = 240


# ── Schemas ───────────────────────────────────────────────────────────────────


class FactResponse(BaseModel):
    id: int
    key: str
    value: str
    confidence: float
    source_conversation_id: Optional[int] = None
    source_message_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FactListResponse(BaseModel):
    total: int
    facts: list[FactResponse]


class ChunkPreviewResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str  # already trimmed to _PREVIEW_CHARS
    is_encrypted: bool
    created_at: datetime


class ChunkListResponse(BaseModel):
    total: int
    encrypted_total: int
    distinct_conversations: int
    items: list[ChunkPreviewResponse]


class DeleteResponse(BaseModel):
    deleted: int


# ── Facts ─────────────────────────────────────────────────────────────────────


@router.get("/facts", response_model=FactListResponse)
def list_facts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All structured facts remembered for the current user.

    No pagination — fact count is bounded in practice (one row per
    extracted key; LLMs converge on a small key vocabulary). If a
    deployment ever sees thousands of facts per user, the right fix
    is consolidation, not pagination.
    """
    rows = (
        db.query(UserFact)
        .filter(UserFact.user_id == current_user.id)
        .order_by(UserFact.updated_at.desc())
        .all()
    )
    return FactListResponse(
        total=len(rows),
        facts=[FactResponse.model_validate(r) for r in rows],
    )


@router.delete("/facts/{fact_id}", response_model=DeleteResponse)
def delete_fact(
    fact_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a single fact owned by the current user.

    404 (not 403) on cross-user IDs so we don't reveal which IDs
    exist for other users.
    """
    fact = (
        db.query(UserFact)
        .filter(UserFact.id == fact_id, UserFact.user_id == current_user.id)
        .first()
    )
    if not fact:
        raise HTTPException(status_code=404, detail="Fact 不存在")
    db.delete(fact)
    db.commit()
    return DeleteResponse(deleted=1)


@router.delete("/facts", response_model=DeleteResponse)
def clear_facts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wipe every fact for the current user. Irreversible."""
    deleted = (
        db.query(UserFact)
        .filter(UserFact.user_id == current_user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return DeleteResponse(deleted=int(deleted))


# ── Chunks (cross-conversation RAG) ───────────────────────────────────────────


@router.get("/chunks", response_model=ChunkListResponse)
def list_chunks(
    limit: int = Query(_CHUNK_LIMIT_DEFAULT, ge=1, le=_CHUNK_LIMIT_MAX),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent chunk preview + headline counts.

    Returns the ``limit`` newest chunks (each ``content`` trimmed to
    ``_PREVIEW_CHARS``) plus three roll-ups:

    * ``total`` — every chunk for this user (used by the UI to
      surface a "we have N memories of you" headline).
    * ``encrypted_total`` — drives the privacy banner about whether
      any classified material is in the pool.
    * ``distinct_conversations`` — gives a sense of how broad the
      recall is, helps the user decide whether wiping is overkill.
    """
    base_q = db.query(ConversationMemoryChunk).filter(
        ConversationMemoryChunk.user_id == current_user.id
    )
    total = base_q.count()
    encrypted_total = base_q.filter(
        ConversationMemoryChunk.is_encrypted.is_(True)
    ).count()
    distinct_conv_rows = (
        base_q.with_entities(ConversationMemoryChunk.conversation_id)
        .distinct()
        .count()
    )
    rows = base_q.order_by(ConversationMemoryChunk.id.desc()).limit(limit).all()

    items: list[ChunkPreviewResponse] = []
    for r in rows:
        content = r.content or ""
        if len(content) > _PREVIEW_CHARS:
            content = content[:_PREVIEW_CHARS] + "…"
        items.append(
            ChunkPreviewResponse(
                id=int(r.id),
                conversation_id=int(r.conversation_id),
                role=str(r.role),
                content=content,
                is_encrypted=bool(r.is_encrypted),
                created_at=r.created_at,
            )
        )
    return ChunkListResponse(
        total=total,
        encrypted_total=encrypted_total,
        distinct_conversations=distinct_conv_rows,
        items=items,
    )


@router.delete("/chunks", response_model=DeleteResponse)
def clear_chunks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Wipe every memory chunk for the current user. Irreversible.

    Facts are NOT touched — those live in their own table and have
    a separate clear endpoint. We keep the two operations distinct
    because users often want to forget recent discussions
    (chunks) while keeping the high-signal extracted facts
    (preferences, name, role).
    """
    deleted = (
        db.query(ConversationMemoryChunk)
        .filter(ConversationMemoryChunk.user_id == current_user.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return DeleteResponse(deleted=int(deleted))


# ── Cross-tenant: agent reading user facts ───────────────────────────────────
#
# Phase 3 of the route-3 anila-memory layer. An agent serving a user
# sometimes needs to read that user's stable facts (name, role,
# preferences) so its responses stay consistent across conversations
# even when CSP-side prompt injection isn't enough.
#
# Auth model: agent service token (X-CSP-Service-Token header,
# verified against agent_credentials). The agent's identity is
# resolved server-side; the URL-path ``user_id`` says *which user's
# facts* the agent wants. There is intentionally NO endpoint for
# write / delete via service token — extraction only flows through
# the existing post-turn pipeline.
#
# The current policy is "any active agent token can read any user's
# facts". Tighter scoping (e.g. require an open conversation between
# agent and user) is left to a future iteration when stateful policy
# checks become worth the latency. Every cross-tenant read writes an
# audit row so admins can detect abuse retrospectively.


@router.get("/users/{user_id}/facts", response_model=FactListResponse)
def list_user_facts_for_agent(
    user_id: int,
    db: Session = Depends(get_db),
    identity: agent_credential_service.CallerIdentity | None = Depends(
        verify_service_token
    ),
):
    """Read another user's facts via agent service token.

    Returns the same shape as the user-facing :func:`list_facts`.
    401 on missing / invalid token. 403 when the caller is not an
    agent (the legacy CSP_SERVICE_TOKEN env or a service_clients
    row that isn't agent-scoped). 404 on a non-existent user_id so
    the response shape stays uniform with other ``users/{id}`` paths.
    """
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid service token",
        )
    if identity.kind != "agent" or identity.agent_id is None:
        raise HTTPException(
            status_code=403,
            detail="此 endpoint 僅接受 agent service token",
        )

    user_exists = (
        db.query(User.id).filter(User.id == user_id).first() is not None
    )
    if not user_exists:
        raise HTTPException(status_code=404, detail="User 不存在")

    rows = (
        db.query(UserFact)
        .filter(UserFact.user_id == user_id)
        .order_by(UserFact.updated_at.desc())
        .all()
    )

    audit_service.log_audit_event(
        db,
        action="memory.cross_tenant.read_user_facts",
        resource_type="user_facts",
        resource_id=str(user_id),
        detail=(
            f"agent_id={identity.agent_id} read {len(rows)} fact(s) "
            f"for user_id={user_id}"
        ),
        metadata={
            "agent_id": identity.agent_id,
            "user_id": user_id,
            "fact_count": len(rows),
        },
        commit=True,
    )

    return FactListResponse(
        total=len(rows),
        facts=[FactResponse.model_validate(r) for r in rows],
    )

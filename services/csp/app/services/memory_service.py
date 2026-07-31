"""User-scoped memory: structured facts + cross-conversation RAG.

Storage layer for the route-3 anila-memory architecture: anila-core
owns the schema / extraction prompt / embedding contract, this module
owns the Postgres + httpx execution. The :class:`PostgresMemoryAdapter`
below implements :class:`anila_core.memory.long_term.MemoryAdapter`
verbatim; the top-level convenience functions
(:func:`build_memory_block`, :func:`persist_turn`,
:func:`retrieve_relevant_chunks`, :func:`get_user_facts`) are thin
wrappers around a default adapter instance kept for backward compat
with ``app.api.proxy`` and ``app.api.memory`` call sites.

DTOs (:class:`RetrievedChunk`, :class:`MemoryReadResult`) and the
extraction prompt + parser are **re-exported from anila-core** —
this module is the single concrete backend, not a competing source
of truth. The shapes anila-core defines are what storage backends
must conform to; if a future test needs an in-memory fake, it
implements :class:`MemoryAdapter` against the same DTOs.

Endpoint discovery
==================

LLM endpoint comes from ``model_registry`` (``MEMORY_LLM_MODEL`` /
fallback). Embedding resolves through the platform's
``is_platform_embedding`` designation (P4.8) — never by matching a
hardcoded name string. That exact-name path was the silent production
defect (``nvidia/NV-embed-V2`` vs registered ``nvidia/nv-embed-v2``).

Operator override via env:

* ``MEMORY_LLM_MODEL`` (default ``gemma4``) — fact extraction.

Why not anila-core's filesystem memdir?
=======================================

That family is **per-agent**, file-system-backed, designed for one
agent process to remember its own working context. This adapter is
**per-user**, DB-backed, multi-tenant, queryable from REST. They
share the long_term/ adapter Protocol post-route-3, but the two
backends serve different tenancy models — see
``anila_core/memory/long_term/__init__.py``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from anila_core.memory.long_term import (
    EXTRACTION_SYSTEM_PROMPT,
    MemoryAdapter,
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
    format_transcript_for_extraction,
    parse_extraction_response,
    truncate_embedding,
)

from anila_core.security import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services.platform_embedding import resolve_platform_embedding
from app.services.proxy.urls import join_upstream_path
from app.services.proxy_service import _apply_gateway_auth

logger = logging.getLogger(__name__)


def _guard_outbound(url: str) -> None:
    """Call-time SSRF re-validation for memory's *direct* (non-proxy) outbound
    calls. The endpoint comes from the admin-seeded ``model_registry``, but DNS
    rebinding (TOCTOU) can still move the host between registration and use, and
    ``_embed`` forwards ``MODEL_GATEWAY_API_KEY`` — re-run the central guard so a
    poisoned registry row can't turn this into a blind-SSRF key-leak. Raises
    ``RuntimeError`` so the existing fail-closed callers skip the call.
    """
    try:
        validate_outbound_url(url, endpoint_kind=ENDPOINT_KIND_MODEL)
    except UnsafeEndpointError as exc:
        raise RuntimeError(
            f"memory outbound endpoint failed SSRF guard: {exc}"
        ) from exc


# ── Tunables (env-overridable, CSP-deployment specific) ──────────────────────
#
# These don't belong in anila-core because they're per-deployment knobs
# (top_k / cosine threshold are quality/perf trade-offs the operator
# tunes; the model names point at deployment-specific registry rows).

_RETRIEVE_TOP_K = int(os.environ.get("MEMORY_RETRIEVE_TOP_K", "3"))
_RETRIEVE_MIN_COSINE = float(os.environ.get("MEMORY_RETRIEVE_MIN_COSINE", "0.4"))
_MAX_CHUNK_CHARS = int(os.environ.get("MEMORY_MAX_CHUNK_CHARS", "1200"))
_LLM_MODEL_NAME = os.environ.get("MEMORY_LLM_MODEL", "gemma4")
_HTTP_TIMEOUT = float(os.environ.get("MEMORY_HTTP_TIMEOUT", "30"))

# Don't waste an LLM call on a no-op turn. The extractor is robust to
# short text but spending a round-trip to confirm "[]" on every "yes"
# / "ok" reply doubles per-turn cost without value.
_EXTRACT_MIN_CHARS = 8


# ── Endpoint discovery ────────────────────────────────────────────────────────


def _resolve_endpoint(db: Session, model_name: str, model_type: str) -> str:
    """Return ``endpoint_url`` for the named registry row, or raise.

    Looked up on every call (cached implicitly by SQLAlchemy session
    cache for the duration of a request). Endpoint changes propagate
    on the next chat completion without a restart — same contract as
    the rest of the proxy layer.
    """
    row: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == model_name, ModelRegistry.model_type == model_type)
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"memory_service: model_registry row not found for "
            f"name={model_name!r} type={model_type!r}"
        )
    return row.endpoint_url.rstrip("/")


def _resolve_extraction_target(db: Session) -> tuple[str, str] | None:
    """Resolve ``(model_name, base_url)`` for fact extraction.

    Prefers the configured ``MEMORY_LLM_MODEL``. When that name isn't a
    registered active LLM — e.g. an air-gapped deployment that overrode the
    primary LLM to gpt-oss but left ``MEMORY_LLM_MODEL`` at the ``gemma4``
    default — fall back to the first active LLM in the registry so
    extraction follows whatever the deployment actually serves instead of
    silently disabling itself. Returns ``None`` only when no active LLM is
    registered at all.
    """
    try:
        return _LLM_MODEL_NAME, _resolve_endpoint(db, _LLM_MODEL_NAME, "llm")
    except RuntimeError:
        pass

    fallback: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.model_type == "llm", ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.id)
        .first()
    )
    if fallback is None:
        return None
    logger.warning(
        "memory_service: MEMORY_LLM_MODEL=%r is not a registered active LLM — "
        "falling back to %r for fact extraction. Set MEMORY_LLM_MODEL to a "
        "registered model name to silence this.",
        _LLM_MODEL_NAME,
        fallback.name,
    )
    return fallback.name, fallback.endpoint_url.rstrip("/")


# ── Embedding ─────────────────────────────────────────────────────────────────


async def _embed(db: Session, text_input: str) -> tuple[list[float], str, int]:
    """Return ``(vector, source_model_name, native_dim)`` for ``text_input``.

    Resolves the embedder through the platform's ``is_platform_embedding``
    designation (P4.8) — never by matching a hardcoded name string. That
    exact-name path was the silent production defect
    (``nvidia/NV-embed-V2`` vs registered ``nvidia/nv-embed-v2``).

    Calls the embedding endpoint directly (not via CSP /v1/embeddings
    proxy) — we're already running inside CSP and the proxy adds an
    auth + token-usage layer we don't need for an internal background
    job.
    """
    resolved = resolve_platform_embedding(db)
    if resolved is None:
        raise RuntimeError(
            "memory_service: no platform embedding model available "
            "(designate one via POST /api/models/{id}/set-platform-embedding)"
        )
    model_name = resolved.name
    native_dim = resolved.native_dim
    base_url = resolved.model.endpoint_url.rstrip("/")
    # Registry rows store bare host or ``…/v1``; join once to the FINAL URL.
    url = join_upstream_path(base_url, "/v1/embeddings")
    # SSRF re-validation BEFORE attaching the gateway key — never send the
    # bearer token to a host that fails the outbound guard. Guard the FINAL
    # url that will actually be requested.
    _guard_outbound(url)
    # 內網 gateway 拓撲下 /v1 全路由要 Bearer(MODEL_GATEWAY_API_KEY);
    # 本機 proxy 模式 key 為空 = no-op。直呼叫繞過 CSP proxy 層,要自帶。
    headers = _apply_gateway_auth({})
    # Release the pooled connection before the outbound embed HTTP call.
    db.commit()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(
            url,
            json={"model": model_name, "input": [text_input]},
            headers=headers,
        )
        r.raise_for_status()
    data = r.json()
    vec = data["data"][0]["embedding"]
    return (
        truncate_embedding(vec, pad_from=resolved.pad_from),
        model_name,
        native_dim,
    )


def _vec_to_pg_literal(vec: Iterable[float]) -> str:
    """Format a Python float list as the bracketed text pgvector accepts."""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


# ── Retrieval (sync from caller's POV; one embed + one SQL) ──────────────────


async def retrieve_relevant_chunks(
    db: Session,
    user_id: int,
    query_text: str,
    *,
    exclude_conversation_id: int | None = None,
    only_conversation_id: int | None = None,
    top_k: int | None = None,
    min_cosine: float | None = None,
) -> list[RetrievedChunk]:
    """ANN-search this user's past message embeddings.

    ``exclude_conversation_id`` filters out chunks from the active
    conversation — those messages are already in the chat history the
    LLM is about to see, so re-injecting them as "past discussion"
    just wastes context.

    ``only_conversation_id`` is the P4.5 confinement: recall may not
    leave the one conversation named here. Owner rule (PLAN 4.4/4.5) —
    ANILALM 的「同一 session」= 同一個對話框 — so the LM side searches
    its own conversation and nothing else. It is the inverse of
    ``exclude_conversation_id``; when set, exclude is ignored (excluding
    the only conversation we are allowed to read would return nothing
    and quietly disable recall instead of confining it).
    """
    if not query_text.strip():
        return []

    if only_conversation_id is not None:
        exclude_conversation_id = None

    k = top_k if top_k is not None else _RETRIEVE_TOP_K
    threshold = min_cosine if min_cosine is not None else _RETRIEVE_MIN_COSINE

    try:
        embedding, source_model, _native = await _embed(db, query_text)
    except Exception:
        logger.exception("memory_service: embed failed during retrieve")
        return []

    vec_literal = _vec_to_pg_literal(embedding)

    # halfvec_cosine_ops uses the ``<=>`` distance operator; cosine
    # similarity = 1 - distance. Filter on similarity >= threshold so
    # the threshold semantics match the caller's intuition.
    # P4.8: only vectors produced by the current designated model —
    # equal width does not make two models' spaces comparable.
    sql = text(
        """
        SELECT id, conversation_id, role, content, is_encrypted,
               1 - (embedding <=> CAST(:vec AS halfvec)) AS cosine
        FROM conversation_memory_chunks
        WHERE user_id = :user_id
          AND embedding_source_model = :source_model
          AND (:only_conv IS NULL OR conversation_id = :only_conv)
          AND (:exclude_conv IS NULL OR conversation_id <> :exclude_conv)
        ORDER BY embedding <=> CAST(:vec AS halfvec) ASC
        LIMIT :k
        """
    )
    rows = db.execute(
        sql,
        {
            "vec": vec_literal,
            "user_id": user_id,
            "source_model": source_model,
            "only_conv": only_conversation_id,
            "exclude_conv": exclude_conversation_id,
            "k": k,
        },
    ).fetchall()

    hits: list[RetrievedChunk] = []
    for r in rows:
        cosine = float(r.cosine)
        if cosine < threshold:
            continue
        hits.append(
            RetrievedChunk(
                id=int(r.id),
                conversation_id=int(r.conversation_id),
                role=str(r.role),
                content=str(r.content),
                cosine=cosine,
                is_encrypted=bool(r.is_encrypted),
            )
        )
    return hits


def get_user_facts(
    db: Session,
    user_id: int,
    *,
    only_conversation_id: int | None = None,
) -> list[UserFact]:
    """Return a user's facts, newest first (ORM rows).

    This returns the SQLAlchemy ORM ``UserFact`` rows directly because
    ``app.api.memory`` and ``_format_block`` consume them as ORM
    objects. The Adapter contract (``MemoryAdapter.get_user_facts``)
    returns ``UserFactDTO`` instead — see
    :meth:`PostgresMemoryAdapter.get_user_facts` for the conversion.

    ``only_conversation_id`` restricts the result to facts extracted
    from that one conversation (P4.5 confinement — see
    :func:`retrieve_relevant_chunks`). Default ``None`` = every fact
    the user has, which is what the ANILA side and the management page
    want.
    """
    q = db.query(UserFact).filter(UserFact.user_id == user_id)
    if only_conversation_id is not None:
        q = q.filter(UserFact.source_conversation_id == only_conversation_id)
    return q.order_by(UserFact.updated_at.desc()).all()


def _format_block(facts: list[UserFact], chunks: list[RetrievedChunk]) -> str | None:
    """Compose the markdown block prepended to system prompts.

    ``preference.*`` facts get their own ``### 使用者偏好`` section so the
    routing LLM and the Router's personalization layer can find the user's
    stable preferences in the CSP-injected memory.
    """
    if not facts and not chunks:
        return None

    prefs = [f for f in facts if f.key.startswith("preference.")]
    others = [f for f in facts if not f.key.startswith("preference.")]

    lines: list[str] = ["## 使用者背景與過往脈絡"]

    if prefs:
        lines.append("")
        lines.append("### 使用者偏好")
        for f in prefs:
            lines.append(f"- **{f.key}**: {f.value}")

    if others:
        lines.append("")
        lines.append("### 已知事實")
        for f in others:
            lines.append(f"- **{f.key}**: {f.value}")

    if chunks:
        lines.append("")
        lines.append("### 過往相關討論")
        for i, c in enumerate(chunks, start=1):
            content = c.content
            if len(content) > _MAX_CHUNK_CHARS:
                content = content[:_MAX_CHUNK_CHARS] + "…"
            tag = " (加密來源)" if c.is_encrypted else ""
            lines.append(
                f"[{i}] {c.role}{tag} (similarity {c.cosine:.2f}): {content}"
            )

    lines.append("")
    lines.append(
        "以上是平台對使用者的長期記憶，請參考但不要原文照抄；若記憶內容與本次對話矛盾，"
        "以本次對話為準。"
    )
    return "\n".join(lines)


async def build_memory_block(
    db: Session,
    user_id: int,
    latest_user_message: str,
    *,
    exclude_conversation_id: int | None = None,
    only_conversation_id: int | None = None,
) -> MemoryReadResult:
    """Top-level read: fetch facts + run RAG, return formatted block.

    ``only_conversation_id`` confines BOTH stores — facts and chunks —
    to a single conversation (P4.5). Passing it to just one of the two
    would leak through the other; the whole point of the parameter is
    that it covers every store the block is assembled from.
    """
    facts = get_user_facts(
        db, user_id, only_conversation_id=only_conversation_id
    )
    chunks = await retrieve_relevant_chunks(
        db,
        user_id,
        latest_user_message,
        exclude_conversation_id=exclude_conversation_id,
        only_conversation_id=only_conversation_id,
    )
    return MemoryReadResult(
        block=_format_block(facts, chunks),
        facts_count=len(facts),
        chunks=chunks,
    )


# ── Fact extraction (LLM call) ────────────────────────────────────────────────


async def _extract_facts(db: Session, conversation_text: str) -> list[dict[str, Any]]:
    """Ask the platform LLM to surface stable facts from a turn.

    Prompt + parser are owned by anila-core
    (:data:`EXTRACTION_SYSTEM_PROMPT`,
    :func:`parse_extraction_response`). This function only handles the
    HTTP call against the deployment's chat-completion endpoint and
    the input-length gate.
    """
    if len(conversation_text.strip()) < _EXTRACT_MIN_CHARS:
        return []

    target = _resolve_extraction_target(db)
    if target is None:
        logger.warning(
            "memory_service: no active LLM registered — fact extraction disabled"
        )
        return []
    model_name, base_url = target
    url = join_upstream_path(base_url, "/v1/chat/completions")
    try:
        _guard_outbound(url)
    except RuntimeError:
        logger.warning(
            "memory_service: extraction endpoint failed SSRF guard — "
            "fact extraction disabled",
        )
        return []

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": conversation_text},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }
    # Release the pooled connection before the outbound LLM HTTP call.
    db.commit()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("memory_service: extractor LLM call failed")
        return []

    return parse_extraction_response(raw)


# ── Writing ───────────────────────────────────────────────────────────────────


def _insert_chunk(
    db: Session,
    *,
    user_id: int,
    conversation_id: int,
    message_id: int | None,
    role: str,
    content: str,
    is_encrypted: bool,
    embedding: list[float],
    source_model: str,
    native_dim: int,
) -> None:
    """Stage one ConversationMemoryChunk INSERT (caller owns the transaction)."""
    vec_literal = _vec_to_pg_literal(embedding)
    db.execute(
        text(
            """
            INSERT INTO conversation_memory_chunks
                (user_id, conversation_id, message_id, role, content,
                 embedding, is_encrypted,
                 embedding_source_model, embedding_native_dim)
            VALUES (:user_id, :conversation_id, :message_id, :role, :content,
                    CAST(:vec AS halfvec), :is_encrypted,
                    :source_model, :native_dim)
            """
        ),
        {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": role,
            "content": content,
            "vec": vec_literal,
            "is_encrypted": is_encrypted,
            "source_model": source_model,
            "native_dim": native_dim,
        },
    )


async def _write_chunk(
    db: Session,
    *,
    user_id: int,
    conversation_id: int,
    message_id: int | None,
    role: str,
    content: str,
    is_encrypted: bool,
) -> None:
    """Embed then stage one ConversationMemoryChunk INSERT.

    Embed runs before any INSERT is staged so ``_embed``'s connection-release
    ``commit()`` cannot make a sibling chunk durable mid-pair.
    """
    if not content.strip():
        return
    embedding, source_model, native_dim = await _embed(db, content)
    _insert_chunk(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        role=role,
        content=content,
        is_encrypted=is_encrypted,
        embedding=embedding,
        source_model=source_model,
        native_dim=native_dim,
    )


def _upsert_facts(
    db: Session,
    user_id: int,
    facts: list[dict[str, Any]],
    *,
    source_conversation_id: int | None,
    source_message_id: int | None,
) -> None:
    """ON CONFLICT (user_id, key) DO UPDATE — newest extraction wins."""
    if not facts:
        return
    table = UserFact.__table__
    rows = [
        {
            "user_id": user_id,
            "key": f["key"],
            "value": f["value"],
            "confidence": f["confidence"],
            "source_conversation_id": source_conversation_id,
            "source_message_id": source_message_id,
        }
        for f in facts
    ]
    stmt = pg_insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "key"],
        set_={
            "value": stmt.excluded.value,
            "confidence": stmt.excluded.confidence,
            "source_conversation_id": stmt.excluded.source_conversation_id,
            "source_message_id": stmt.excluded.source_message_id,
            "updated_at": text("CURRENT_TIMESTAMP"),
        },
    )
    db.execute(stmt)


async def persist_turn(
    *,
    user_id: int,
    conversation_id: int,
    user_message: str,
    assistant_message: str,
    is_encrypted: bool,
    user_message_id: int | None = None,
    assistant_message_id: int | None = None,
) -> None:
    """Background entry point — writes both chunks and extracts facts.

    Designed to be invoked from FastAPI ``BackgroundTasks``. Opens its
    own DB session because the request-scoped session has already
    been closed by the time this runs. All errors are caught and
    logged so a memory write failure can never propagate up to break
    the user-facing response.
    """
    db = SessionLocal()
    try:
        try:
            # Embed BOTH sides before staging either INSERT. ``_embed`` releases
            # the pooled connection via commit(); if a user INSERT were already
            # pending, that commit would make a lone user chunk durable and a
            # later assistant-embed failure could no longer roll it back.
            # Pair atomicity = both vectors ready → both INSERTs → one commit.
            user_emb = (
                await _embed(db, user_message) if user_message.strip() else None
            )
            asst_emb = (
                await _embed(db, assistant_message)
                if assistant_message.strip()
                else None
            )
            if user_emb is not None:
                embedding, source_model, native_dim = user_emb
                _insert_chunk(
                    db,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=user_message_id,
                    role="user",
                    content=user_message,
                    is_encrypted=is_encrypted,
                    embedding=embedding,
                    source_model=source_model,
                    native_dim=native_dim,
                )
            if asst_emb is not None:
                embedding, source_model, native_dim = asst_emb
                _insert_chunk(
                    db,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    message_id=assistant_message_id,
                    role="assistant",
                    content=assistant_message,
                    is_encrypted=is_encrypted,
                    embedding=embedding,
                    source_model=source_model,
                    native_dim=native_dim,
                )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "memory_service: chunk write failed user_id=%s conv_id=%s",
                user_id,
                conversation_id,
            )
            # Continue to extraction even if chunk write failed —
            # facts and chunks are independent code paths.

        try:
            transcript = format_transcript_for_extraction(
                user_message, assistant_message
            )
            facts = await _extract_facts(db, transcript)
            if facts:
                _upsert_facts(
                    db,
                    user_id,
                    facts,
                    source_conversation_id=conversation_id,
                    source_message_id=user_message_id,
                )
                db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "memory_service: fact extraction failed user_id=%s conv_id=%s",
                user_id,
                conversation_id,
            )
    finally:
        db.close()


# ── P4.4: revoke memory when a conversation is upgraded ──────────────────────


def purge_conversation_memory(db: Session, conversation_id: int) -> dict[str, int]:
    """Delete every piece of memory derived from ``conversation_id``.

    Owner rule (PLAN.md §4.4/4.5, 2026-07-30):
    「對話**升密之後,先前萃取的記憶直接刪除**(不是標記不可用)。」
    Real DELETE, not a tombstone or a retrieval-time filter — a filter
    is one forgotten call site away from serving the content again, and
    SYSTEM-MAP §5 L189 says 撤回, not 隱藏.

    Both stores the memory subsystem writes are covered:

    * ``conversation_memory_chunks`` — RAG recall rows. The embedding is
      a column on this table, not a separate store, so the vector dies
      with the row; there is no orphaned index entry to sweep.
    * ``user_facts`` — extracted key/value facts, matched on
      ``source_conversation_id`` (the provenance column SYSTEM-MAP §5
      L189 requires precisely so an upgrade can find them again).

    Caller owns the transaction — this stages the DELETEs and does not
    commit, so the purge lands in the same transaction as the
    classification upgrade that triggered it. Half-applied is not a
    state this may end in.

    Returns the per-store row counts for the audit trail.
    """
    chunks = (
        db.query(ConversationMemoryChunk)
        .filter(ConversationMemoryChunk.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    facts = (
        db.query(UserFact)
        .filter(UserFact.source_conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    return {"chunks": int(chunks), "facts": int(facts)}


# ── PostgresMemoryAdapter — implements anila_core.memory.long_term.MemoryAdapter ─


def _user_fact_to_dto(fact: UserFact) -> UserFactDTO:
    """Convert ORM row → DTO. Adapter callers see the DTO; CSP-side
    code that wants ORM-level features (eager-loading relationships,
    SQL filters, etc.) hits :func:`get_user_facts` directly.
    """
    return UserFactDTO(
        id=fact.id,
        user_id=fact.user_id,
        key=fact.key,
        value=fact.value,
        confidence=float(fact.confidence),
        source_conversation_id=fact.source_conversation_id,
        source_message_id=fact.source_message_id,
        created_at=fact.created_at,
        updated_at=fact.updated_at,
    )


class PostgresMemoryAdapter:
    """SQLAlchemy + httpx + pgvector implementation of
    :class:`anila_core.memory.long_term.MemoryAdapter`.

    Each public method either gets a pre-existing DB session
    injected (for request-scoped CRUD on user facts) or opens its
    own via :data:`SessionLocal` (for the fire-and-forget post-turn
    writer). The two patterns mirror how the legacy module-level
    functions work; the class is the structured path the route-3
    cutover prefers.

    Method bodies delegate to the same module-level helpers the
    backward-compat top-level functions use, so behaviour is
    identical between the two entry points.
    """

    def __init__(self, db_factory=SessionLocal) -> None:
        # db_factory is held as an attr so a future test can swap in
        # a TestSessionLocal without monkey-patching the module.
        self._db_factory = db_factory

    # ── Facts ───────────────────────────────────────────────────────────────

    async def get_user_facts(self, user_id: int) -> list[UserFactDTO]:
        db = self._db_factory()
        try:
            rows = get_user_facts(db, user_id)
            return [_user_fact_to_dto(r) for r in rows]
        finally:
            db.close()

    async def upsert_user_facts(
        self,
        user_id: int,
        facts: list[dict[str, Any]],
        *,
        source_conversation_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
    ) -> None:
        db = self._db_factory()
        try:
            _upsert_facts(
                db,
                user_id,
                facts,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
            )
            db.commit()
        finally:
            db.close()

    async def delete_user_fact(self, user_id: int, fact_id: int) -> bool:
        db = self._db_factory()
        try:
            row = (
                db.query(UserFact)
                .filter(UserFact.id == fact_id, UserFact.user_id == user_id)
                .first()
            )
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True
        finally:
            db.close()

    async def clear_user_facts(self, user_id: int) -> int:
        db = self._db_factory()
        try:
            count = (
                db.query(UserFact)
                .filter(UserFact.user_id == user_id)
                .delete(synchronize_session=False)
            )
            db.commit()
            return int(count)
        finally:
            db.close()

    # ── Chunks ──────────────────────────────────────────────────────────────

    async def write_chunk(
        self,
        *,
        user_id: int,
        conversation_id: int,
        message_id: Optional[int],
        role: str,
        content: str,
        is_encrypted: bool,
    ) -> None:
        db = self._db_factory()
        try:
            await _write_chunk(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                role=role,
                content=content,
                is_encrypted=is_encrypted,
            )
            db.commit()
        finally:
            db.close()

    async def retrieve_relevant_chunks(
        self,
        user_id: int,
        query_text: str,
        *,
        exclude_conversation_id: Optional[int] = None,
        top_k: int = 3,
        min_cosine: float = 0.4,
    ) -> list[RetrievedChunk]:
        db = self._db_factory()
        try:
            return await retrieve_relevant_chunks(
                db,
                user_id,
                query_text,
                exclude_conversation_id=exclude_conversation_id,
                top_k=top_k,
                min_cosine=min_cosine,
            )
        finally:
            db.close()

    async def clear_user_chunks(self, user_id: int) -> int:
        db = self._db_factory()
        try:
            count = (
                db.query(ConversationMemoryChunk)
                .filter(ConversationMemoryChunk.user_id == user_id)
                .delete(synchronize_session=False)
            )
            db.commit()
            return int(count)
        finally:
            db.close()

    # ── Combined read / background write ────────────────────────────────────

    async def build_memory_block(
        self,
        user_id: int,
        latest_user_message: str,
        *,
        exclude_conversation_id: Optional[int] = None,
    ) -> MemoryReadResult:
        db = self._db_factory()
        try:
            return await build_memory_block(
                db,
                user_id,
                latest_user_message,
                exclude_conversation_id=exclude_conversation_id,
            )
        finally:
            db.close()

    async def persist_turn(
        self,
        *,
        user_id: int,
        conversation_id: int,
        user_message: str,
        assistant_message: str,
        is_encrypted: bool,
        user_message_id: Optional[int] = None,
        assistant_message_id: Optional[int] = None,
    ) -> None:
        # The module-level persist_turn opens its own session; just
        # delegate so behaviour stays identical between the two paths.
        await persist_turn(
            user_id=user_id,
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            is_encrypted=is_encrypted,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
        )


# Default adapter instance — most CSP code uses this. Tests / future
# multi-tenant callers can construct their own with a different
# ``db_factory``.
default_adapter: MemoryAdapter = PostgresMemoryAdapter()

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

LLM and embedding endpoints come from the platform's existing
``model_registry`` table (auto-seeded via ``AUTO_REGISTER_MODELS``).
Operator overrides via env:

* ``MEMORY_LLM_MODEL`` (default ``gemma4``) — fact extraction.
* ``MEMORY_EMBEDDING_MODEL`` (default ``nvidia/NV-embed-V2``).

Pointing the platform at a different local LLM (gpt-oss-20b,
qwen3-32b, …) automatically routes the extractor there too.

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
    DEFAULT_EMBED_MODEL,
    EMBED_DIM,
    EMBED_NATIVE_DIM,
    EXTRACTION_SYSTEM_PROMPT,
    MemoryAdapter,
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
    format_transcript_for_extraction,
    parse_extraction_response,
    truncate_embedding,
)

from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.user_memory import ConversationMemoryChunk, UserFact

logger = logging.getLogger(__name__)


# ── Tunables (env-overridable, CSP-deployment specific) ──────────────────────
#
# These don't belong in anila-core because they're per-deployment knobs
# (top_k / cosine threshold are quality/perf trade-offs the operator
# tunes; the model names point at deployment-specific registry rows).

_RETRIEVE_TOP_K = int(os.environ.get("MEMORY_RETRIEVE_TOP_K", "3"))
_RETRIEVE_MIN_COSINE = float(os.environ.get("MEMORY_RETRIEVE_MIN_COSINE", "0.4"))
_MAX_CHUNK_CHARS = int(os.environ.get("MEMORY_MAX_CHUNK_CHARS", "1200"))
_LLM_MODEL_NAME = os.environ.get("MEMORY_LLM_MODEL", "gemma4")
_EMBED_MODEL_NAME = os.environ.get("MEMORY_EMBEDDING_MODEL", DEFAULT_EMBED_MODEL)
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


# ── Embedding ─────────────────────────────────────────────────────────────────


async def _embed(db: Session, text_input: str) -> list[float]:
    """Return one truncated NV-embed-V2 vector for ``text_input``.

    Calls the embedding endpoint directly (not via CSP /v1/embeddings
    proxy) — we're already running inside CSP and the proxy adds an
    auth + token-usage layer we don't need for an internal background
    job. token_usage attribution for memory-extraction calls is a
    deliberate non-goal in P1 (revisit if it shows up as cost noise).
    """
    base_url = _resolve_endpoint(db, _EMBED_MODEL_NAME, "embedding")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(
            f"{base_url}/embeddings",
            json={"model": _EMBED_MODEL_NAME, "input": [text_input]},
        )
        r.raise_for_status()
    data = r.json()
    vec = data["data"][0]["embedding"]
    # anila-core's truncate_embedding handles both 4096 (truncate) and
    # 4000 (passthrough) cases and raises on unexpected dim.
    return truncate_embedding(vec)


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
    top_k: int | None = None,
    min_cosine: float | None = None,
) -> list[RetrievedChunk]:
    """ANN-search this user's past message embeddings.

    ``exclude_conversation_id`` filters out chunks from the active
    conversation — those messages are already in the chat history the
    LLM is about to see, so re-injecting them as "past discussion"
    just wastes context.
    """
    if not query_text.strip():
        return []

    k = top_k if top_k is not None else _RETRIEVE_TOP_K
    threshold = min_cosine if min_cosine is not None else _RETRIEVE_MIN_COSINE

    try:
        embedding = await _embed(db, query_text)
    except Exception:
        logger.exception("memory_service: embed failed during retrieve")
        return []

    vec_literal = _vec_to_pg_literal(embedding)

    # halfvec_cosine_ops uses the ``<=>`` distance operator; cosine
    # similarity = 1 - distance. Filter on similarity >= threshold so
    # the threshold semantics match the caller's intuition.
    sql = text(
        """
        SELECT id, conversation_id, role, content, is_encrypted,
               1 - (embedding <=> CAST(:vec AS halfvec)) AS cosine
        FROM conversation_memory_chunks
        WHERE user_id = :user_id
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


def get_user_facts(db: Session, user_id: int) -> list[UserFact]:
    """Return ALL facts for a user, newest first (ORM rows).

    This returns the SQLAlchemy ORM ``UserFact`` rows directly because
    ``app.api.memory`` and ``_format_block`` consume them as ORM
    objects. The Adapter contract (``MemoryAdapter.get_user_facts``)
    returns ``UserFactDTO`` instead — see
    :meth:`PostgresMemoryAdapter.get_user_facts` for the conversion.
    """
    return (
        db.query(UserFact)
        .filter(UserFact.user_id == user_id)
        .order_by(UserFact.updated_at.desc())
        .all()
    )


def _format_block(facts: list[UserFact], chunks: list[RetrievedChunk]) -> str | None:
    """Compose the markdown block prepended to system prompts."""
    if not facts and not chunks:
        return None

    lines: list[str] = ["## 使用者背景與過往脈絡"]

    if facts:
        lines.append("")
        lines.append("### 已知事實")
        for f in facts:
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
) -> MemoryReadResult:
    """Top-level read: fetch facts + run RAG, return formatted block."""
    facts = get_user_facts(db, user_id)
    chunks = await retrieve_relevant_chunks(
        db,
        user_id,
        latest_user_message,
        exclude_conversation_id=exclude_conversation_id,
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

    try:
        base_url = _resolve_endpoint(db, _LLM_MODEL_NAME, "llm")
    except RuntimeError:
        logger.warning(
            "memory_service: LLM model %r not in registry — fact extraction disabled",
            _LLM_MODEL_NAME,
        )
        return []

    payload = {
        "model": _LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": conversation_text},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(f"{base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("memory_service: extractor LLM call failed")
        return []

    return parse_extraction_response(raw)


# ── Writing ───────────────────────────────────────────────────────────────────


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
    """Embed and INSERT one ConversationMemoryChunk."""
    if not content.strip():
        return
    embedding = await _embed(db, content)
    vec_literal = _vec_to_pg_literal(embedding)
    db.execute(
        text(
            """
            INSERT INTO conversation_memory_chunks
                (user_id, conversation_id, message_id, role, content,
                 embedding, is_encrypted)
            VALUES (:user_id, :conversation_id, :message_id, :role, :content,
                    CAST(:vec AS halfvec), :is_encrypted)
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
        },
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
            await _write_chunk(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=user_message_id,
                role="user",
                content=user_message,
                is_encrypted=is_encrypted,
            )
            await _write_chunk(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=assistant_message_id,
                role="assistant",
                content=assistant_message,
                is_encrypted=is_encrypted,
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

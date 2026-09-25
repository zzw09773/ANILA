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

LLM endpoint comes from the summary model role. Embedding resolves
through the platform's ``is_platform_embedding`` designation (P4.8) —
never by matching a hardcoded name string. That exact-name path was
the silent production defect (``nvidia/NV-embed-V2`` vs registered
``nvidia/nv-embed-v2``). Unset summary role disables extraction; it
does not fall back to another model.

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
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_core.memory.long_term import (
    EXTRACTION_SYSTEM_PROMPT,
    MEMORY_REFRESH_SYSTEM_PROMPT,
    MemoryAdapter,
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
    format_transcript_for_extraction,
    parse_extraction_response,
    parse_memory_refresh_response,
    truncate_embedding,
)

from anila_core.security import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.platform_setting import get_setting
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user_memory import (
    ConversationMemoryChunk,
    ConversationSummary,
    MemoryRefreshLease,
    MemoryTombstone,
    UserFact,
)
from app.services import zh_normalize_service
from app.services.platform_embedding import resolve_platform_embedding
from app.services.proxy.urls import join_upstream_path

logger = logging.getLogger(__name__)

# 摘要角色沒設或已停用時只記一次。閒置掃描若每分鐘都喊，日誌會被洗掉。
_summary_role_warnings: set[str] = set()

_ERROR_NOTICE_MARKERS = (
    "產生回應時發生錯誤",
    "本次無法查詢院內規章知識庫",
    "LLM 無法回應",
)
_SKIP_STREAM_STATES = frozenset({
    "failed",
    "interrupted",
    "unanswered",
    "reserved",
    "streaming",
    "stopped",
})
_VERBATIM_MIN_CHARS = 12


def reset_summary_role_warning() -> None:
    """測試用。正式路徑不要呼叫。"""
    _summary_role_warnings.clear()


def _warn_summary_role_once(message: str | None) -> None:
    if not message or message in _summary_role_warnings:
        return
    _summary_role_warnings.add(message)
    logger.warning("memory_service: %s", message)


def _memory_enabled(db: Session) -> bool:
    return bool(get_setting(db, "memory.enabled"))


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


# ── Tunables ─────────────────────────────────────────────────────────────────
#
# ⚠ 保留的兩顆檢索參數（``memory.retrieve_top_k`` /
# ``memory.retrieve_min_cosine``）在**用到它的那個函式裡**走
# ``get_setting(db, key)`` 解析（``platform_settings`` → env → 程式預設）。
# 其餘記憶體格式/逾時限制是固定程式常數，不是治理頁控制項。
#
# 事實抽取用治理中心的「摘要模型」角色，不讀 MEMORY_LLM_MODEL，
# 也不在沒設定時改挑另一顆已註冊的模型。

# Keep injected user memory within a sane share of internal model context
# budgets.
_MEMORY_BLOCK_MAX_CHARS = 4_000

# Don't waste an LLM call on a no-op turn. The extractor is robust to
# short text but spending a round-trip to confirm "[]" on every "yes"
# / "ok" reply doubles per-turn cost without value.
_EXTRACT_MIN_CHARS = 8

# 摘要硬上限。模型輸出可到 800 tokens，這裡先擋住再寫入。
SUMMARY_MAX_CHARS = 600
# 跟助理原文重疊到這個比例就視為改寫抄襲，不存。
_OVERLAP_WINDOW = 6
_OVERLAP_REJECT = 0.5
_EVIDENCE_MIN_CHARS = 2
# 認領租約。過期後別的 worker 才能接手，迴圈本身不忙等。
_LEASE_SECONDS = 120
_OVERLAP_STRIP = re.compile(
    r"[\s，。、；：！？,.!?;:\"'「」『』（）()\[\]{}<>《》\-—_]+"
)

# 使用者自己寫的回覆風格。萃取不可覆寫，否則設定頁存的字會被下一輪對話洗掉。
REPLY_STYLE_KEY = "preference.reply_style"
USER_AUTHORED_FACT_KEYS = frozenset({REPLY_STYLE_KEY})
REPLY_STYLE_MAX_CHARS = 2000


def facts_safe_for_extraction(facts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Drop keys the user owns so persist_turn cannot clobber them."""
    return [f for f in (facts or []) if f.get("key") not in USER_AUTHORED_FACT_KEYS]


# ── Endpoint discovery ────────────────────────────────────────────────────────


def _resolve_endpoint(db: Session, model_name: str, model_type: str) -> str:
    """Return ``endpoint_url`` for the named active registry row, or raise.

    Looked up on every call (cached implicitly by SQLAlchemy session
    cache for the duration of a request). Endpoint changes propagate
    on the next chat completion without a restart — same contract as
    the rest of the proxy layer. An inactive row is treated as absent so
    automatic callers cannot send work to a model an operator deactivated.
    """
    row: ModelRegistry | None = (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.name == model_name,
            ModelRegistry.model_type == model_type,
            ModelRegistry.is_active.is_(True),
        )
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

    The summary role is the only source. Unset or inactive returns
    ``None`` — never another registered LLM and never a hard-coded name.
    """
    from app.services.model_roles import resolve_role

    resolved = resolve_role(db, "summary")
    if resolved.status != "ok" or resolved.model is None:
        _warn_summary_role_once(resolved.message)
        return None
    return resolved.model.name, resolved.model.endpoint_url.rstrip("/")


# ── Embedding ─────────────────────────────────────────────────────────────────


async def _embed(
    db: Session,
    text_input: str,
    *,
    user_id: int = 0,
    department_id: int | None = None,
    embedding_input_role: str = "query",
) -> tuple[list[float], str, int]:
    """Return ``(vector, source_model_name, native_dim)`` for ``text_input``.

    Resolves the embedder through the platform's ``is_platform_embedding``
    designation (P4.8) — never by matching a hardcoded name string. That
    exact-name path was the silent production defect
    (``nvidia/NV-embed-V2`` vs registered ``nvidia/nv-embed-v2``).

    Goes through ``proxy_request`` so OpenAI-compatible and ``triton_grpc``
    share one query/document decision point (recall → ``query``; chunk
    persist → ``document``).

    **Not metered** (``record_usage=False``). Long-term memory embedding is a
    platform-internal background job the user never asked for: ``persist_turn``
    embeds both halves of every turn, so metering it would add ~3
    ``token_usage`` rows per ordinary chat turn and silently inflate every
    per-user and per-department figure the pilot is measuring. Before this
    call site moved onto ``proxy_request`` it bypassed the proxy entirely and
    wrote nothing, so ``False`` is the choice that keeps the numbers
    comparable across the change. The dimension probe
    (``api/models.py``) made the same call for the same reason.
    If metering internal memory embedding is ever wanted, it needs its own
    ``request_type`` so it can be told apart from user-initiated calls —
    flipping this flag alone would just make the existing rows wrong.
    See ``tests/test_memory_embed_not_metered.py``.
    """
    from types import SimpleNamespace

    from app.services.proxy.service import proxy_request, resolve_proxy_tuning

    resolved = resolve_platform_embedding(db)
    if resolved is None:
        raise RuntimeError(
            "memory_service: no platform embedding model available "
            "(designate one via POST /api/models/{id}/set-platform-embedding)"
        )
    model = resolved.model
    model_name = resolved.name
    native_dim = resolved.native_dim
    raw_ver = getattr(model, "api_version", None)
    api_version = raw_ver if raw_ver in ("v1", "v2") else "v1"
    model_snapshot = SimpleNamespace(
        id=getattr(model, "id", 0),
        name=model.name,
        model_type=getattr(model, "model_type", "embedding"),
        endpoint_url=model.endpoint_url,
        api_version=api_version,
        protocol=getattr(model, "protocol", None) or "openai_compatible",
        api_key_secret_ref=getattr(model, "api_key_secret_ref", None),
        classification_ceiling=getattr(model, "classification_ceiling", None),
        is_active=getattr(model, "is_active", True),
        display_name=getattr(model, "display_name", model.name),
        is_internal=bool(getattr(model, "is_internal", False)),
    )
    # 逾時／重試四顆在這裡解（**還握著連線的時候**），凍結成 tuning 往下傳；
    # commit 之後 proxy 那一層就不該再碰 DB 了。
    tuning = resolve_proxy_tuning(db)
    # Release the pooled connection before the outbound embed call.
    db.commit()
    data = await proxy_request(
        model=model_snapshot,
        api_key_id=None,
        user_id=user_id,
        department_id=department_id,
        request_body={"model": model_name, "input": [text_input]},
        endpoint_path=f"/{api_version}/embeddings",
        embedding_input_role=embedding_input_role,
        record_usage=False,
        tuning=tuning,
    )
    vec = data["data"][0]["embedding"]
    return (
        truncate_embedding(vec, pad_from=resolved.pad_from),
        model_name,
        native_dim,
    )


def _vector_is_finite(vec: Iterable[float]) -> bool:
    """True when the vector is non-empty and every component is finite.

    2026-09-02 OOBE walk: the embedder returned an all-NaN query vector once.
    pgvector rejects it (``NaN not allowed in halfvec``) — and because the
    check happened *inside* the request's transaction, the abort took the
    next statement of the same turn (attachment loading) down with it, and
    the user got a confident "the attachment has no text". Memory is
    best-effort; a bad vector must degrade to "no memory", never poison the
    turn.
    """
    values = list(vec)
    return bool(values) and all(math.isfinite(float(x)) for x in values)


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

    # 每次呼叫解一次（管理員改完，下一次檢索就是新值）。⚠ 這兩行在 ``_embed``
    # 之前 —— ``_embed`` 會 ``commit()`` 把池化連線還回去，之後才走出向 HTTP。
    k = top_k if top_k is not None else int(get_setting(db, "memory.retrieve_top_k"))
    threshold = (
        min_cosine
        if min_cosine is not None
        else float(get_setting(db, "memory.retrieve_min_cosine"))
    )

    try:
        embedding, source_model, _native = await _embed(
            db,
            query_text,
            user_id=user_id,
            embedding_input_role="query",
        )
    except Exception:
        logger.exception("memory_service: embed failed during retrieve")
        return []
    if not _vector_is_finite(embedding):
        logger.warning(
            "memory_service: %s returned a non-finite (NaN/inf) query vector; "
            "skipping memory retrieval for this turn",
            source_model,
        )
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
          AND lower(embedding_source_model) = lower(:source_model)
          AND (:only_conv IS NULL OR conversation_id = :only_conv)
          AND (:exclude_conv IS NULL OR conversation_id <> :exclude_conv)
        ORDER BY embedding <=> CAST(:vec AS halfvec) ASC
        LIMIT :k
        """
    )
    # Run the vector search in its own SAVEPOINT. Memory is best-effort: if
    # PostgreSQL rejects the statement, only this savepoint rolls back and the
    # caller's transaction stays usable — the attachment loading that follows
    # in the same turn must not fail with InFailedSqlTransaction (2026-09-02).
    try:
        with db.begin_nested():
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
    except Exception:  # noqa: BLE001 — degrade to "no memory", never poison the turn
        logger.exception("memory_service: retrieval query failed; continuing without memory")
        return []

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


def _format_block(
    facts: list[UserFact],
    chunks: list[RetrievedChunk],
    *,
    max_chunk_chars: int,
) -> str | None:
    """Compose the markdown block prepended to system prompts.

    ``preference.*`` facts get their own ``### 使用者偏好`` section so the
    routing LLM and the Router's personalization layer can find the user's
    stable preferences in the CSP-injected memory.

    ``max_chunk_chars`` 是**必填的關鍵字參數**，而且刻意沒有預設值：這個函式是
    純的（不碰 DB），切塊上限由握著 session 的 ``build_memory_block`` 當場解析後
    傳進來。給它一個預設值就等於把那顆設定又釘回模組層一次 —— 呼叫端漏傳會變成
    靜默用舊值，而不是當場 ``TypeError``。
    """
    prefs = [f for f in facts if f.key.startswith("preference.")]
    others = [f for f in facts if not f.key.startswith("preference.")]

    # 呼叫端仍會傳 chunks。固定區塊不再使用它們。
    _ = chunks

    def _render(
        selected_prefs: list[str],
        selected_others: list[str],
        selected_chunks: list[RetrievedChunk],
    ) -> str:
        lines: list[str] = ["## 使用者背景與過往脈絡"]

        if selected_prefs:
            lines.append("")
            lines.append("### 使用者偏好")
            lines.extend(selected_prefs)

        if selected_others:
            lines.append("")
            lines.append("### 已知事實")
            lines.extend(selected_others)

        # 過往對話不再自動附上。chunks／max_chunk_chars 留在簽名上，呼叫端不用改。
        del selected_chunks
        if max_chunk_chars < 0:
            return ""

        lines.append("")
        lines.append(
            "以上是平台對使用者的長期記憶，請參考但不要原文照抄；若記憶內容與本次對話矛盾，"
            "以本次對話為準。"
        )
        return "\n".join(lines)

    selected_prefs: list[str] = []
    selected_others: list[str] = []
    selected_chunks: list[RetrievedChunk] = []

    # Facts are already newest-first at the storage boundary. Preferences are
    # kept ahead of regular facts, then higher-similarity chunks are considered.
    # Each candidate is admitted only as a whole rendered item; a rejected item
    # is skipped so a later smaller item can still fit without truncation.
    def _preference_line(fact: UserFact) -> str:
        if fact.key == REPLY_STYLE_KEY:
            return f"- 回覆偏好（使用者明確指定的語言與風格，依此為準）：{fact.value}"
        return f"- **{fact.key}**: {fact.value}"

    for source, selected in (
        (
            (_preference_line(f) for f in prefs),
            selected_prefs,
        ),
        (
            (f"- **{f.key}**: {f.value}" for f in others),
            selected_others,
        ),
    ):
        for item in source:
            selected.append(item)
            if len(_render(selected_prefs, selected_others, selected_chunks)) > _MEMORY_BLOCK_MAX_CHARS:
                selected.pop()

    if not (selected_prefs or selected_others):
        return None
    return _render(selected_prefs, selected_others, selected_chunks)


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
    if not _memory_enabled(db):
        return MemoryReadResult(block=None, facts_count=0, chunks=[])
    facts = get_user_facts(
        db, user_id, only_conversation_id=only_conversation_id
    )
    facts = _facts_allowed_in_prompt(db, facts)
    # 舊回答片段不再注入。exclude／only 仍只作用在事實（ANILALM 同一對話框）。
    del latest_user_message, exclude_conversation_id
    return MemoryReadResult(
        block=_format_block(
            facts,
            [],
            max_chunk_chars=1200,
        ),
        facts_count=len(facts),
        chunks=[],
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
    # ⚠ 逾時要在 ``commit()`` **之前**解析。那個 commit 是刻意把池化連線還回池子
    # 再走出向 HTTP；commit 之後才查 ``platform_settings`` 會重新 checkout 一條
    # 連線，並且一路握到 LLM 回應為止。
    http_timeout = 30.0
    # Release the pooled connection before the outbound LLM HTTP call.
    db.commit()
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
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
    # §6-3：assistant chunk 在 embed／落庫前正規化（與 persist_turn 同一契約）
    content, zh_changed = zh_normalize_service.prepare_message_content(db, role, content)
    if message_id is not None:
        zh_normalize_service.log_if_changed(message_id, zh_changed)
    if not content or not content.strip():
        return
    embedding, source_model, native_dim = await _embed(
        db,
        content,
        user_id=user_id,
        embedding_input_role="document",
    )
    if not _vector_is_finite(embedding):
        logger.warning(
            "memory_service: %s returned a non-finite (NaN/inf) vector; "
            "chunk not persisted (memory is best-effort, the turn continues)",
            source_model,
        )
        return
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
    """每一輪結束不再把原文寫進記憶。

    摘要與事實改在對話閒置、或使用者另開對話時整理。這裡留著是因為
    proxy 仍會排這個背景工作；它必須繼續吞掉例外，不能影響回答。
    """
    del (
        user_id,
        conversation_id,
        user_message,
        assistant_message,
        is_encrypted,
        user_message_id,
        assistant_message_id,
    )
    return


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def turn_is_extractable(
    *,
    user_text: str,
    assistant_text: str,
    assistant_metadata: dict | None,
) -> bool:
    """失敗、救援、錯誤通知，以及還沒寫完的回合，都不進摘要。"""
    if not (user_text or "").strip() or not (assistant_text or "").strip():
        return False
    meta = assistant_metadata if isinstance(assistant_metadata, dict) else {}
    state = (meta.get("anila_stream") or {}).get("state")
    if state in _SKIP_STREAM_STATES:
        return False
    if meta.get("rescue"):
        return False
    trace = meta.get("trace")
    if isinstance(trace, list) and any(
        isinstance(step, dict) and step.get("kind") == "rescue" for step in trace
    ):
        return False
    blob = f"{user_text}\n{assistant_text}"
    if any(marker in blob for marker in _ERROR_NOTICE_MARKERS):
        return False
    return True


def _eligible_turns(messages: list[Message]) -> list[tuple[Message, Message]]:
    pending: Message | None = None
    turns: list[tuple[Message, Message]] = []
    for msg in messages:
        if msg.role == "user":
            pending = msg
            continue
        if msg.role == "assistant" and pending is not None:
            if turn_is_extractable(
                user_text=pending.content or "",
                assistant_text=msg.content or "",
                assistant_metadata=msg.metadata_,
            ):
                turns.append((pending, msg))
            pending = None
    return turns


def _messages_for_extract(db: Session, conv: Conversation) -> list[Message]:
    from app.services.conversation_service import load_active_path

    return load_active_path(db, conv)


def conversation_excluded_from_memory(conv: Conversation | None) -> bool:
    """分類或已閂鎖的對話不進記憶。等級比布林優先，兩者任一成立都排除。"""
    if conv is None:
        return False
    if bool(getattr(conv, "classified", False)):
        return True
    level = getattr(conv, "classification_level", None) or "無機密"
    return level not in ("無機密", "")


def _forget_excluded_conversation(db: Session, conversation_id: int) -> None:
    """分類之後才發現的摘要與事實直接刪掉，不再被召回或注入。"""
    purge_conversation_memory(db, conversation_id)
    db.commit()


def _facts_allowed_in_prompt(db: Session, facts: list[UserFact]) -> list[UserFact]:
    forgotten: set[int] = set()
    kept: list[UserFact] = []
    for fact in facts:
        source = getattr(fact, "source_conversation_id", None)
        if source is None:
            kept.append(fact)
            continue
        if source in forgotten:
            continue
        conv = db.get(Conversation, source)
        if conversation_excluded_from_memory(conv):
            _forget_excluded_conversation(db, int(source))
            forgotten.add(int(source))
            continue
        kept.append(fact)
    return kept


def _insert_tombstone(db: Session, **fields: Any) -> None:
    if db.get_bind().dialect.name == "sqlite":
        fields["id"] = int(db.query(func.max(MemoryTombstone.id)).scalar() or 0) + 1
    db.add(MemoryTombstone(**fields))


def remember_summary_deleted(db: Session, row: ConversationSummary) -> None:
    """刪摘要時記下這段對話已經涵蓋到哪一則訊息。"""
    covered = row.covered_message_id
    if covered is None:
        covered = (
            db.query(func.max(Message.id))
            .filter(Message.conversation_id == row.conversation_id)
            .scalar()
        )
    _insert_tombstone(
        db,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        kind="summary",
        fact_key=None,
        covered_message_id=covered,
    )


def remember_fact_deleted(db: Session, fact: UserFact) -> None:
    """刪事實時記下來源訊息。那個範圍內的原文不能再把同一個 key 寫回來。"""
    covered = fact.source_message_id
    if covered is None and fact.source_conversation_id is not None:
        covered = (
            db.query(func.max(Message.id))
            .filter(Message.conversation_id == fact.source_conversation_id)
            .scalar()
        )
    _insert_tombstone(
        db,
        user_id=fact.user_id,
        conversation_id=fact.source_conversation_id,
        kind="fact",
        fact_key=fact.key,
        covered_message_id=covered,
    )


def _summary_tombstone_boundary(db: Session, conversation_id: int) -> int:
    rows = (
        db.query(MemoryTombstone.covered_message_id)
        .filter(
            MemoryTombstone.conversation_id == conversation_id,
            MemoryTombstone.kind == "summary",
        )
        .all()
    )
    if not rows:
        return 0
    return max(int(row[0] or 0) for row in rows)


def _turns_after_boundary(
    turns: list[tuple[Message, Message]], boundary: int
) -> list[tuple[Message, Message]]:
    if boundary <= 0:
        return turns
    return [turn for turn in turns if int(turn[1].id) > boundary]


def _fact_reinsert_blocked(
    db: Session, user_id: int, key: str, evidence_message_id: int | None
) -> bool:
    rows = (
        db.query(MemoryTombstone)
        .filter(
            MemoryTombstone.user_id == user_id,
            MemoryTombstone.kind == "fact",
            MemoryTombstone.fact_key == key,
        )
        .all()
    )
    for row in rows:
        limit = row.covered_message_id
        if limit is None:
            return True
        if evidence_message_id is None or int(evidence_message_id) <= int(limit):
            return True
    return False


def _now_epoch(now: datetime | None = None) -> int:
    return int(_as_utc(now or datetime.now(timezone.utc)).timestamp())


def claim_refresh_lease(
    db: Session, conversation_id: int, *, now: datetime | None = None
) -> str | None:
    """原子認領。別人還握著租約就回 None，呼叫端直接跳過，不重試。"""
    now_epoch = _now_epoch(now)
    until = now_epoch + _LEASE_SECONDS
    token = uuid.uuid4().hex
    updated = db.execute(
        text(
            """
            UPDATE memory_refresh_leases
               SET claim_token = :token,
                   claimed_until = :until
             WHERE conversation_id = :cid
               AND claimed_until <= :now
            """
        ),
        {"token": token, "until": until, "cid": conversation_id, "now": now_epoch},
    )
    if updated.rowcount:
        db.flush()
        return token
    try:
        with db.begin_nested():
            db.add(
                MemoryRefreshLease(
                    conversation_id=conversation_id,
                    claim_token=token,
                    claimed_until=until,
                )
            )
            db.flush()
        return token
    except IntegrityError:
        return None


def release_refresh_lease(db: Session, conversation_id: int, token: str) -> None:
    """讓出租約，但留下 token，舊的那次結果不能再寫。"""
    db.execute(
        text(
            """
            UPDATE memory_refresh_leases
               SET claimed_until = 0
             WHERE conversation_id = :cid
               AND claim_token = :token
            """
        ),
        {"cid": conversation_id, "token": token},
    )
    db.flush()


def conversations_due(
    db: Session,
    *,
    now: datetime,
    user_id: int | None = None,
    exclude_conversation_id: int | None = None,
    force: bool = False,
) -> list[int]:
    """閒置夠久、而且還有沒整理過的合格回合。開新對話時 force 不等閒置。"""
    if not _memory_enabled(db):
        return []
    idle_minutes = int(get_setting(db, "memory.idle_minutes"))
    cutoff = _as_utc(now) - timedelta(minutes=idle_minutes)
    query = db.query(Conversation)
    if user_id is not None:
        query = query.filter(Conversation.user_id == user_id)
    if exclude_conversation_id is not None:
        query = query.filter(Conversation.id != exclude_conversation_id)
    due: list[int] = []
    for conv in query.all():
        if conversation_excluded_from_memory(conv):
            continue
        messages = _messages_for_extract(db, conv)
        if not messages:
            continue
        last_at = max(_as_utc(msg.created_at) for msg in messages)
        turns = _turns_after_boundary(
            _eligible_turns(messages),
            _summary_tombstone_boundary(db, int(conv.id)),
        )
        if not turns:
            continue
        latest_id = int(turns[-1][1].id)
        summary = (
            db.query(ConversationSummary)
            .filter(ConversationSummary.conversation_id == conv.id)
            .one_or_none()
        )
        if summary is not None and (summary.covered_message_id or 0) >= latest_id:
            continue
        if not force and last_at > cutoff:
            continue
        due.append(int(conv.id))
    return due


def remove_verbatim_assistant(
    summary: str,
    assistant_texts: list[str],
    *,
    min_chars: int = _VERBATIM_MIN_CHARS,
) -> str:
    """摘要裡若出現助理原文的連續片段，就把那段拿掉。"""
    cleaned = summary or ""
    for text in assistant_texts:
        raw = text or ""
        index = 0
        while index + min_chars <= len(raw):
            if raw[index : index + min_chars] not in cleaned:
                index += 1
                continue
            end = index + min_chars
            while end < len(raw) and raw[index : end + 1] in cleaned:
                end += 1
            cleaned = cleaned.replace(raw[index:end], "")
            index = end
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n，。；")


def facts_from_user_statements(
    facts: list[dict[str, Any]],
    user_texts: list[str],
    assistant_texts: list[str],
    *,
    user_messages: list[tuple[int | None, str]] | None = None,
) -> list[dict[str, Any]]:
    """每個事實都要在某一則使用者訊息裡原樣出現，並記下那則訊息。

    模型編造、改寫或翻譯出來的值直接丟掉。助理原文不再拿來當反證。
    """
    del assistant_texts
    messages = (
        list(user_messages)
        if user_messages is not None
        else [(None, text) for text in user_texts]
    )
    kept: list[dict[str, Any]] = []
    for fact in facts:
        value = str(fact.get("value") or "").strip()
        if len(value) < _EVIDENCE_MIN_CHARS:
            continue
        evidence_id = None
        found = False
        for message_id, text in messages:
            if value in (text or ""):
                found = True
                evidence_id = message_id
        if not found:
            continue
        kept.append({**fact, "value": value, "evidence_message_id": evidence_id})
    return kept


def _compact_for_overlap(text: str) -> str:
    return _OVERLAP_STRIP.sub("", text or "")


def summary_overlaps_assistant(summary: str, assistant_texts: list[str]) -> bool:
    """改了標點或空格仍算抄助理原文。窗口比逐字刪除更短，才抓得到改寫。"""
    compact_summary = _compact_for_overlap(summary)
    if len(compact_summary) < _OVERLAP_WINDOW:
        return False
    hits = 0
    total = 0
    step = max(1, _OVERLAP_WINDOW // 2)
    for text in assistant_texts:
        raw = _compact_for_overlap(text)
        index = 0
        while index + _OVERLAP_WINDOW <= len(raw):
            total += 1
            if raw[index : index + _OVERLAP_WINDOW] in compact_summary:
                hits += 1
            index += step
    if total == 0:
        return False
    return (hits / total) >= _OVERLAP_REJECT


def _fallback_summary(user_texts: list[str]) -> str:
    joined = "；".join(text.strip() for text in user_texts if text and text.strip())
    return joined[:SUMMARY_MAX_CHARS]


def _accept_summary(summary: str, assistant_texts: list[str]) -> str | None:
    text = (summary or "").strip()
    if not text:
        return None
    if len(text) > SUMMARY_MAX_CHARS or summary_overlaps_assistant(text, assistant_texts):
        return None
    return text


def _upsert_facts_generic(
    db: Session,
    user_id: int,
    facts: list[dict[str, Any]],
    *,
    source_conversation_id: int | None,
    source_message_id: int | None,
) -> None:
    """SQLite 測試沒有 Postgres 的 ON CONFLICT。行為與正式 upsert 相同。"""
    for fact in facts:
        evidence_id = fact.get("evidence_message_id", source_message_id)
        if _fact_reinsert_blocked(db, user_id, fact["key"], evidence_id):
            continue
        row = (
            db.query(UserFact)
            .filter(UserFact.user_id == user_id, UserFact.key == fact["key"])
            .one_or_none()
        )
        if row is not None and bool(getattr(row, "user_edited", False)):
            continue
        if row is None:
            fields: dict[str, Any] = {
                "user_id": user_id,
                "key": fact["key"],
                "value": fact["value"],
                "confidence": fact["confidence"],
                "source_conversation_id": source_conversation_id,
                "source_message_id": evidence_id,
            }
            if db.get_bind().dialect.name == "sqlite":
                fields["id"] = int(db.query(func.max(UserFact.id)).scalar() or 0) + 1
            db.add(UserFact(**fields))
        else:
            row.value = fact["value"]
            row.confidence = fact["confidence"]
            row.source_conversation_id = source_conversation_id
            row.source_message_id = evidence_id
            row.updated_at = datetime.now(timezone.utc)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _parse_vec(raw: str | None) -> list[float]:
    if not raw:
        return []
    text_value = str(raw).strip().strip("[]")
    if not text_value:
        return []
    try:
        return [float(part) for part in text_value.split(",") if part.strip()]
    except ValueError:
        return []


def _summary_write_allowed(
    db: Session,
    existing: ConversationSummary | None,
    conversation_id: int,
    covered_message_id: int | None,
    claim_token: str | None,
) -> bool:
    """舊的涵蓋範圍不能蓋掉新的。租約已換人時，同一範圍的舊結果也不寫。"""
    incoming = covered_message_id or 0
    if claim_token is not None:
        lease = db.get(MemoryRefreshLease, conversation_id)
        if lease is not None and lease.claim_token != claim_token:
            live = int(lease.claimed_until or 0) > _now_epoch()
            if live:
                return False
            if existing is not None and (existing.covered_message_id or 0) >= incoming:
                return False
    if existing is not None and (existing.covered_message_id or 0) > incoming:
        return False
    return True


def save_conversation_summary(
    db: Session,
    *,
    user_id: int,
    conversation_id: int,
    summary: str,
    covered_message_id: int | None,
    embedding: list[float] | None,
    source_model: str | None,
    native_dim: int | None,
    is_encrypted: bool,
    when: datetime | None = None,
    claim_token: str | None = None,
) -> ConversationSummary | None:
    """寫入或覆蓋這一則對話的摘要。呼叫端決定何時 commit。

    已有較新的 ``covered_message_id`` 時不覆蓋。``claim_token`` 對不上
    目前租約時，同一範圍的舊結果也不寫。
    """
    now = when or datetime.now(timezone.utc)
    existing = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conversation_id)
        .one_or_none()
    )
    if not _summary_write_allowed(
        db, existing, conversation_id, covered_message_id, claim_token
    ):
        return existing
    vec_literal = _vec_to_pg_literal(embedding) if embedding else None
    if db.get_bind().dialect.name == "postgresql":
        # halfvec 不能靠 ORM 綁字串，跟訊息片段同一條 raw SQL。
        embed_expr = "CAST(:vec AS halfvec)" if vec_literal else "NULL"
        params: dict[str, Any] = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "summary": summary,
            "covered_message_id": covered_message_id,
            "source_model": source_model,
            "native_dim": native_dim,
            "is_encrypted": is_encrypted,
            "now": now,
        }
        if vec_literal:
            params["vec"] = vec_literal
        db.execute(
            text(
                f"""
                INSERT INTO conversation_summaries
                    (user_id, conversation_id, summary, covered_message_id,
                     embedding, embedding_source_model, embedding_native_dim,
                     is_encrypted, created_at, updated_at)
                VALUES
                    (:user_id, :conversation_id, :summary, :covered_message_id,
                     {embed_expr}, :source_model, :native_dim,
                     :is_encrypted, :now, :now)
                ON CONFLICT (conversation_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    summary = EXCLUDED.summary,
                    covered_message_id = EXCLUDED.covered_message_id,
                    embedding = EXCLUDED.embedding,
                    embedding_source_model = EXCLUDED.embedding_source_model,
                    embedding_native_dim = EXCLUDED.embedding_native_dim,
                    is_encrypted = EXCLUDED.is_encrypted,
                    updated_at = EXCLUDED.updated_at
                WHERE conversation_summaries.covered_message_id IS NULL
                   OR conversation_summaries.covered_message_id
                        <= EXCLUDED.covered_message_id
                """
            ),
            params,
        )
        db.flush()
        return (
            db.query(ConversationSummary)
            .filter(ConversationSummary.conversation_id == conversation_id)
            .one()
        )
    row = existing
    if row is None:
        fields: dict[str, Any] = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "summary": summary,
            "covered_message_id": covered_message_id,
            "embedding": vec_literal,
            "embedding_source_model": source_model,
            "embedding_native_dim": native_dim,
            "is_encrypted": is_encrypted,
            "created_at": now,
            "updated_at": now,
        }
        if db.get_bind().dialect.name == "sqlite":
            fields["id"] = int(
                db.query(func.max(ConversationSummary.id)).scalar() or 0
            ) + 1
        row = ConversationSummary(**fields)
        db.add(row)
    else:
        row.user_id = user_id
        row.summary = summary
        row.covered_message_id = covered_message_id
        row.embedding = vec_literal
        row.embedding_source_model = source_model
        row.embedding_native_dim = native_dim
        row.is_encrypted = is_encrypted
        row.updated_at = now
    db.flush()
    return row


_REFRESH_RETRY_NOTE = (
    "上一則摘要不合格。請重寫：全文少於 600 字，"
    "不得沿用助理原文，改標點、改寫或調整程式碼格式也不行。"
    "只寫使用者要什麼、決定了什麼。"
)


async def _call_refresh_model(
    db: Session,
    target: tuple[str, str],
    transcript: str,
    *,
    stricter: bool = False,
) -> str:
    model_name, base_url = target
    url = join_upstream_path(base_url, "/v1/chat/completions")
    try:
        _guard_outbound(url)
    except RuntimeError:
        logger.warning("memory_service: refresh endpoint failed SSRF guard")
        return ""
    system_prompt = MEMORY_REFRESH_SYSTEM_PROMPT
    if stricter:
        system_prompt = f"{system_prompt}\n\n{_REFRESH_RETRY_NOTE}"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
    }
    db.commit()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("memory_service: refresh LLM call failed")
        return ""


def _summary_from_model_text(raw: str, assistant_texts: list[str]) -> tuple[str | None, list[dict[str, Any]]]:
    parsed = parse_memory_refresh_response(raw)
    cleaned = remove_verbatim_assistant(parsed["summary"], assistant_texts)
    return _accept_summary(cleaned, assistant_texts), list(parsed["facts"])


async def _resolve_summary_text(
    db: Session,
    target: tuple[str, str],
    transcript: str,
    assistant_texts: list[str],
    user_texts: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """超長或大量重疊助理原文時重問一次；仍不合格就改用使用者自己的話。"""
    raw = await _call_refresh_model(db, target, transcript)
    accepted, facts = _summary_from_model_text(raw, assistant_texts)
    if accepted:
        return accepted, facts
    raw = await _call_refresh_model(db, target, transcript, stricter=True)
    accepted, facts = _summary_from_model_text(raw, assistant_texts)
    if accepted:
        return accepted, facts
    parsed = parse_memory_refresh_response(raw)
    cleaned = remove_verbatim_assistant(parsed["summary"], assistant_texts).strip()
    if cleaned and not summary_overlaps_assistant(cleaned, assistant_texts):
        return cleaned[:SUMMARY_MAX_CHARS], list(parsed["facts"])
    fallback = _fallback_summary(user_texts)
    return fallback, list(parsed["facts"])


async def refresh_conversation(
    conversation_id: int, *, db: Session | None = None
) -> None:
    """整理一個對話的摘要與使用者事實。角色沒設就不打模型。"""
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    token: str | None = None
    try:
        if not _memory_enabled(db):
            return
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            return
        if conversation_excluded_from_memory(conv):
            _forget_excluded_conversation(db, conv.id)
            return
        messages = _messages_for_extract(db, conv)
        turns = _turns_after_boundary(
            _eligible_turns(messages),
            _summary_tombstone_boundary(db, conv.id),
        )
        if not turns:
            return
        latest_id = int(turns[-1][1].id)
        existing = (
            db.query(ConversationSummary)
            .filter(ConversationSummary.conversation_id == conv.id)
            .one_or_none()
        )
        if existing is not None and (existing.covered_message_id or 0) >= latest_id:
            return
        target = _resolve_extraction_target(db)
        if target is None:
            return
        user_messages = [(int(turn[0].id), turn[0].content or "") for turn in turns]
        user_texts = [text for _message_id, text in user_messages]
        assistant_texts = [turn[1].content or "" for turn in turns]
        transcript = "\n\n".join(
            f"使用者：{user}\n\n助理（只供理解結論，禁止逐字抄進摘要或當成事實）：{assistant}"
            for user, assistant in zip(user_texts, assistant_texts)
        )
        if len(transcript.strip()) < _EXTRACT_MIN_CHARS:
            return
        token = claim_refresh_lease(db, conv.id)
        if token is None:
            return
        db.commit()
        existing = (
            db.query(ConversationSummary)
            .filter(ConversationSummary.conversation_id == conv.id)
            .one_or_none()
        )
        if existing is not None and (existing.covered_message_id or 0) >= latest_id:
            return
        summary, raw_facts = await _resolve_summary_text(
            db, target, transcript, assistant_texts, user_texts
        )
        if not summary:
            return
        facts = facts_safe_for_extraction(
            facts_from_user_statements(
                raw_facts,
                user_texts,
                assistant_texts,
                user_messages=user_messages,
            )
        )
        _upsert_facts_generic(
            db,
            conv.user_id,
            facts,
            source_conversation_id=conv.id,
            source_message_id=latest_id,
        )
        embedding: list[float] | None = None
        source_model: str | None = None
        native_dim: int | None = None
        try:
            embedding, source_model, native_dim = await _embed(
                db,
                summary,
                user_id=conv.user_id,
                embedding_input_role="document",
            )
            if not _vector_is_finite(embedding):
                embedding = None
        except Exception:
            logger.exception(
                "memory_service: summary embed failed conv_id=%s", conv.id
            )
            embedding = None
        save_conversation_summary(
            db,
            user_id=conv.user_id,
            conversation_id=conv.id,
            summary=summary,
            covered_message_id=latest_id,
            embedding=embedding,
            source_model=source_model,
            native_dim=native_dim,
            is_encrypted=False,
            claim_token=token,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "memory_service: refresh failed conv_id=%s", conversation_id
        )
    finally:
        if token is not None:
            try:
                release_refresh_lease(db, conversation_id, token)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "memory_service: release refresh lease failed conv_id=%s",
                    conversation_id,
                )
        if owns_session:
            db.close()


async def search_conversation_summaries(
    db: Session,
    user_id: int,
    query_text: str,
    *,
    exclude_conversation_id: int | None = None,
    top_k: int | None = None,
    min_cosine: float | None = None,
) -> list[Any]:
    """依相似度找這位使用者的對話摘要。不讀舊的訊息片段。"""
    from types import SimpleNamespace

    if not _memory_enabled(db) or not (query_text or "").strip():
        return []
    k = top_k if top_k is not None else int(get_setting(db, "memory.retrieve_top_k"))
    threshold = (
        min_cosine
        if min_cosine is not None
        else float(get_setting(db, "memory.retrieve_min_cosine"))
    )
    try:
        embedding, source_model, _native = await _embed(
            db,
            query_text,
            user_id=user_id,
            embedding_input_role="query",
        )
    except Exception:
        logger.exception("memory_service: embed failed during summary search")
        return []
    if not _vector_is_finite(embedding):
        return []

    hits: list[Any] = []
    if db.get_bind().dialect.name == "postgresql":
        vec_literal = _vec_to_pg_literal(embedding)
        sql = text(
            """
            SELECT id, conversation_id, summary, is_encrypted,
                   1 - (embedding <=> CAST(:vec AS halfvec)) AS cosine
            FROM conversation_summaries
            WHERE user_id = :user_id
              AND embedding IS NOT NULL
              AND lower(embedding_source_model) = lower(:source_model)
              AND (:exclude_conv IS NULL OR conversation_id <> :exclude_conv)
            ORDER BY embedding <=> CAST(:vec AS halfvec) ASC
            LIMIT :k
            """
        )
        try:
            with db.begin_nested():
                rows = db.execute(
                    sql,
                    {
                        "vec": vec_literal,
                        "user_id": user_id,
                        "source_model": source_model,
                        "exclude_conv": exclude_conversation_id,
                        "k": k,
                    },
                ).fetchall()
        except Exception:
            logger.exception("memory_service: summary search failed")
            return []
        for row in rows:
            cosine = float(row.cosine)
            if cosine < threshold:
                continue
            hits.append(
                SimpleNamespace(
                    id=int(row.id),
                    conversation_id=int(row.conversation_id),
                    summary=str(row.summary),
                    cosine=cosine,
                    is_encrypted=bool(row.is_encrypted),
                )
            )
        return _drop_excluded_summaries(db, hits)

    rows = (
        db.query(ConversationSummary)
        .filter(
            ConversationSummary.user_id == user_id,
            ConversationSummary.embedding.isnot(None),
        )
        .all()
    )
    for row in rows:
        if exclude_conversation_id is not None and row.conversation_id == exclude_conversation_id:
            continue
        if (row.embedding_source_model or "").lower() != (source_model or "").lower():
            continue
        cosine = _cosine(embedding, _parse_vec(row.embedding))
        if cosine < threshold:
            continue
        hits.append(
            SimpleNamespace(
                id=int(row.id),
                conversation_id=int(row.conversation_id),
                summary=str(row.summary),
                cosine=cosine,
                is_encrypted=bool(row.is_encrypted),
            )
        )
    hits.sort(key=lambda item: (-item.cosine, item.id))
    return _drop_excluded_summaries(db, hits[:k])


def _drop_excluded_summaries(db: Session, hits: list[Any]) -> list[Any]:
    """分類或加密摘要不回傳。對話事後升密的，連同它抽出的事實一起刪。"""
    visible: list[Any] = []
    forgotten: set[int] = set()
    for hit in hits:
        conversation_id = int(hit.conversation_id)
        conv = db.get(Conversation, conversation_id)
        excluded = bool(getattr(hit, "is_encrypted", False)) or (
            conversation_excluded_from_memory(conv)
        )
        if conversation_id in forgotten or excluded:
            if conversation_id not in forgotten:
                _forget_excluded_conversation(db, conversation_id)
                forgotten.add(conversation_id)
            continue
        visible.append(hit)
    return visible


async def flush_idle_conversations() -> None:
    db = SessionLocal()
    try:
        ids = conversations_due(
            db, now=datetime.now(timezone.utc), force=False
        )
    finally:
        db.close()
    for conversation_id in ids:
        await refresh_conversation(conversation_id)


async def flush_other_conversations(user_id: int, exclude_conversation_id: int) -> None:
    """使用者另開對話時，把其餘還沒整理的對話補上摘要。"""
    try:
        db = SessionLocal()
        try:
            ids = conversations_due(
                db,
                now=datetime.now(timezone.utc),
                user_id=user_id,
                exclude_conversation_id=exclude_conversation_id,
                force=True,
            )
        finally:
            db.close()
        for conversation_id in ids:
            await refresh_conversation(conversation_id)
    except Exception:
        logger.exception("memory_service: flush on new conversation failed")


def start_memory_idle_loop():
    """每分鐘看一次誰閒置夠久。第一次先睡，避免啟動當下掃全表。"""
    import asyncio

    async def _loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await flush_idle_conversations()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("memory_service: idle refresh failed")

    return asyncio.create_task(_loop())


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
    summaries = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    return {"chunks": int(chunks), "facts": int(facts), "summaries": int(summaries)}


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
        top_k: Optional[int] = None,
        min_cosine: Optional[float] = None,
    ) -> list[RetrievedChunk]:
        """``None`` = 用平台設定（``memory.retrieve_top_k`` /
        ``memory.retrieve_min_cosine``）。

        ⚠ 這兩個參數原本是 ``top_k: int = 3`` / ``min_cosine: float = 0.4`` ——
        兩個字面值剛好等於登錄表的預設值，所以「設定沒接上」與「設定就是預設值」
        在這條路徑上分不出來，而管理員從畫面改的值**永遠到不了這裡**。預設值只能
        有一份，它在登錄表。
        """
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

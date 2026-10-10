"""查詢嵌入、索引落差拒絕、chunk 命中收集。

``app.api.ingestion.search`` 與院內規章知識庫共用。放在 services，
避免 ``institutional_kb`` 回頭 import ``app.api``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from anila_core.embeddings.keyword import search_path
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.endpoint_author_service import visible_endpoint_url
from app.services.proxy.service import resolve_proxy_tuning, sync_model_deadline
from app.services.proxy_service import downstream_identity, proxy_request

logger = logging.getLogger(__name__)

async def _embed_query(
    db: Session,
    user: User,
    model_name: str,
    embedding_dim: int,
    query: str,
) -> list[float]:
    """Embed ``query`` through the same model as the collection.

    Reuses ``proxy_request`` so the call:
      - runs through the model_registry endpoint resolution (so the
        embedder URL change in one place propagates),
      - is metered into ``token_usage`` with request_type='embedding',
      - inherits the proxy's retry/backoff.

    Truncation logic mirrors ``ingestion_worker/embedder.py``:
    NV-embed-V2 returns 4096-d vectors but ``halfvec(4000)`` is what
    pgvector can index; drop the last 96. If the runtime model returns
    a shorter vector, that's a model-mismatch and we 422 — the chunks
    were embedded against a different model and the collection should
    be reindexed before search makes sense.
    """
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == model_name)
        .first()
    )
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Collection's embedding_model '{model_name}' is not registered "
                "in model_registry — admin must add it before search works."
            ),
        )
    if not model.is_active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Embedding model '{model_name}' is registered but inactive.",
        )

    native = getattr(model, "embedding_native_dim", None)

    # Snapshot every attribute proxy_request (and ceiling helpers) may read into
    # a plain namespace. ORM instances are subject to expire_on_commit; a bare
    # attribute touch before commit is NOT a load guarantee under either
    # posture. The snapshot is the guarantee: after db.commit() releases the
    # pooled connection, proxy_request must not re-checkout via lazy reload.
    from app.services.proxy.snapshot import snapshot_model

    model_snapshot = snapshot_model(model)
    if not getattr(model_snapshot, "protocol", None):
        model_snapshot.protocol = "openai_compatible"
    user_id = user.id
    department_id = user.department_id
    identity = downstream_identity(user)
    api_version = model_snapshot.api_version if model_snapshot.api_version in ("v1", "v2") else "v1"
    # 逾時、顯示用網址都在 commit 之前算完。developer／admin 的 endpoint
    # grant 查詢會重新 checkout；不能留到 embed 上游的 await 之後。
    tuning = resolve_proxy_tuning(db)
    endpoint_display = visible_endpoint_url(
        model_snapshot.endpoint_url,
        is_internal=bool(getattr(model_snapshot, "is_internal", False)),
        db=db,
        caller=user,
    )
    from app.services.quota_service import enforce_call_quota

    try:
        enforce_call_quota(
            db,
            user_id=user_id,
            department_id=department_id,
            api_key_id=None,
            usage_kind="inference",
        )
    except HTTPException:
        db.commit()
        raise
    db.commit()

    body = {"model": model_name, "input": query}
    response = await proxy_request(
        model=model_snapshot,
        api_key_id=None,  # SPA caller; usage attributes to user, no key
        user_id=user_id,
        user_identity=identity,
        department_id=department_id,
        request_body=body,
        endpoint_path=f"/{api_version}/embeddings",
        endpoint_display=endpoint_display,
        # Query-side: must not silently fall through to Triton's documents input.
        embedding_input_role="query",
        tuning=tuning,
        deadline=sync_model_deadline(),
    )

    try:
        raw_vector = response["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding endpoint returned an unexpected payload shape.",
        ) from e

    if not isinstance(raw_vector, list) or not all(
        isinstance(x, (int, float)) for x in raw_vector
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding endpoint returned a non-numeric vector.",
        )

    from anila_core.embeddings.dims import fit_stored_vector
    from anila_core.memory.long_term import EMBED_DIM

    try:
        floats = [float(x) for x in raw_vector]
        if embedding_dim == EMBED_DIM:
            declared = native if isinstance(native, int) and native > 0 else None
            return fit_stored_vector(floats, declared_native=declared).values
        # Non-production column widths (unit tests use small dims): keep
        # the historical truncate-or-422 contract so fixtures stay cheap.
        if len(floats) < embedding_dim:
            raise ValueError(
                f"dim {len(floats)} < collection embedding_dim {embedding_dim}"
            )
        return floats[:embedding_dim]
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Model returned {len(raw_vector)}-d vector but collection's "
                f"chunks are stored as {embedding_dim}-d. The collection was "
                "indexed against a different model — reindex before searching."
            ),
        ) from e



async def _assert_index_matches_designation(
    store: "CollectionScopedPgVectorStore",
    *,
    hits: list,
    designated: str | None,
    collection_id: int,
) -> None:
    """Refuse to answer "nothing" when the corpus is indexed under another model.

    ``similarity_search`` filters on ``embedding_source_model`` (P4.8).
    Designating a different platform embedding is a supported admin
    action, and the instant it happens every existing chunk stops
    matching: the endpoint keeps returning ``200 {"results": []}``,
    forever, with no error and no log line. A knowledge base full of
    documents becomes a knowledge base that answers nothing, and nothing
    anywhere says why. That is the failure mode this guard exists for —
    silent success is more dangerous than an error.

    Fires only when the vector layer itself returned nothing AND a
    designation filter was in play. A legitimately empty result — empty
    collection, or a query that genuinely matched no passage — takes the
    early return and the response is byte-for-byte what it was before.

    Three outcomes:

    * no rows under any other model → genuinely empty; say nothing.
    * rows under both the designated and another model → the collection
      is mid-reindex, search still works on the matching half. Log for
      the operator; the caller's result is unchanged. ``has_matching``
      is an exact ``EXISTS``, so a deliberate mixed-model corpus during
      a migration lands here every time and is never refused.
    * rows only under another model → the whole collection is stranded.
      409, same code this endpoint already uses for "the collection is
      not in a searchable state" (archived collections, above).

    This is the only hard failure in the package, so it is held to the
    owner's three questions:

    ① Who gets blocked? Only a caller whose collection holds zero
      retrievable chunks — every such call previously returned an empty
      list, so no call that could have returned results can reach the
      raise. The early return above is the guarantee: ``hits`` non-empty
      exits first, and the check runs before the ``document_ids``
      post-filter so a narrowed search cannot be misread.
    ② How do they rescue themselves? Only remedies that were executed
      end to end are named. The operator re-designates the previous
      embedding model — instant, non-destructive, and it is the one that
      actually fixes the whole platform at once. The user's own option is
      DELETE the document then upload it again. Two routes that look
      obvious were checked and do NOT work, so the message warns against
      one and never mentions the other: plain re-upload hits
      ``uq_documents_collection_sha256``, whose ``IntegrityError`` handler
      returns the existing row *above* the ``enqueue_ingest_document``
      call (``documents.py``), so no job is created; and ``/reprocess``
      accepts only ``status='failed'`` while a stranded document is
      ``indexed``. There is no reindex capability on this platform —
      do not write one into a user-facing message.
    ③ Will it one day block us? A mixed-model migration cannot trigger
      it (see above — that is why ``has_matching`` is exact rather than
      sampled). Callers of this endpoint on a 409, each checked rather
      than assumed: ``anila-studio`` handles it everywhere —
      ``report_runner`` catches ``CspClientError`` explicitly,
      ``datatables`` / ``infographics`` / ``studio`` swallow it via
      ``except Exception`` and fall through to no-context mode, and
      ``mindmaps`` surfaces it as a failed job carrying the message.
      ``anila_agent.retrieval.csp_http`` calls ``raise_for_status()``,
      so an in-task agent sees an exception rather than empty context.
      ``infra/loadtest/profile-search.js`` hard-fails on any non-200, so
      the load-test profile will fail against a stranded corpus instead
      of reporting empty results — worth knowing before someone reads a
      red load test as a regression.

    The end-user detail names the designated model (already readable by
    any authenticated caller via ``GET /api/models/platform-embedding``)
    and nothing else. Which model the stale rows carry, and how the
    collection got there, go to the operator log only.

    Scope — do NOT read this as "the platform no longer returns a silent
    empty". It covers **chunk search on this endpoint only**. Image
    search (``search_collection_images`` below) keeps HTTP 200 + empty
    results for studio no-context callers, but sets
    ``source_model_mismatch=true`` and logs when the designated model
    matches no indexed image. Memory retrieval
    (``app/services/memory_service.py``) still returns an empty result
    with nothing said.

    ``coverage`` describes a bounded sample, not the whole collection;
    ``source_model_coverage`` documents what that sampling can miss.
    """
    if hits or designated is None:
        return

    coverage = await store.source_model_coverage(designated)
    if not coverage.has_other:
        return

    if coverage.has_matching:
        logger.warning(
            "collection %s is partially indexed: some chunks are under the "
            "designated embedding %r and some under %r. Search still works "
            "over the matching half; the rest stays unreachable until those "
            "documents are deleted and re-uploaded, or the designation is "
            "put back (there is no reindex capability on this platform).",
            collection_id,
            designated,
            coverage.sample_other_model,
        )
        return

    logger.error(
        "collection %s has no chunks under the designated embedding %r — "
        "every indexed chunk carries %r instead. Search is answering 409 "
        "rather than an empty result set. Restore by designating (and if "
        "necessary reactivating) the model those chunks carry; there is no "
        "reindex capability on this platform.",
        collection_id,
        designated,
        coverage.sample_other_model,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "這個知識庫目前檢索不到任何內容。原因不是沒有資料——文件都還在,"
            f"是既有文件的索引由另一個向量模型建立,與平台現在指定的"
            f"「{designated}」對不起來。"
            "這要由平台管理員處理:把這些文件當初使用的 embedding 模型"
            "重新指定為平台主 embedding;若那個模型已被停用,要先重新啟用再指定"
            "(停用中的模型不能指定,系統會直接拒絕)。"
            "受影響的知識庫會立刻恢復,而且不會動到任何資料。"
            "請聯絡管理員,並附上這個知識庫的名稱。"
            "你自己唯一能做的是把急著要用的文件「先刪除、再重新上傳」,"
            "一次一份;刪掉的文件與其處理歷程不會回來。"
            "直接重傳同一個檔沒有用——系統會判定為重複,不會重建索引。"
        ),
    )


@dataclass
class ChunkHitBatch:
    hits: list
    keyword_fallback: bool = False
    used_model_slots: bool = False


async def collect_chunk_hits(
    store,
    *,
    query: str,
    query_vec: list[float] | None,
    embed_error: Exception | None,
    model_id: int | None,
    top_k: int,
    min_score: float,
    source_model: str | None,
    rerank=None,
) -> ChunkHitBatch:
    """Vector search on the active model, or keyword search when that cannot run.

    Stubs that only implement ``similarity_search`` stay on that path.
    Keyword fallback is used only when the embed failed, or the corpus
    has text but no vectors for the active model, and the store actually
    implements it. A similarity_search failure is not turned into keywords.
    """
    coverage = "legacy"
    if isinstance(model_id, int) and hasattr(store, "embedding_coverage"):
        try:
            coverage = await store.embedding_coverage(model_id)
        except Exception:
            coverage = "legacy"

    # An empty collection is not an outage: no notice, no keyword pass.
    if coverage == "empty":
        return ChunkHitBatch(hits=[])

    keyword_ok = hasattr(store, "keyword_fallback_search")
    if coverage == "legacy":
        want_keyword = bool(embed_error)
    else:
        want_keyword = (
            search_path(
                embed_ok=embed_error is None and query_vec is not None,
                active_vectors_exist=coverage == "ready",
                corpus_exists=True,
            )
            == "keyword"
        )
    if want_keyword and keyword_ok:
        limit = 30 if top_k < 30 else top_k
        hits = await store.keyword_fallback_search(query, top_k=limit)
        if rerank is not None and len(hits) > 1:
            hits = await rerank(hits)
        return ChunkHitBatch(hits=list(hits)[:top_k], keyword_fallback=True)
    if embed_error is not None:
        raise embed_error
    if (
        coverage == "ready"
        and query_vec is not None
        and isinstance(model_id, int)
        and hasattr(store, "similarity_search_for_model")
    ):
        hits = await store.similarity_search_for_model(
            query_embedding=query_vec,
            model_id=model_id,
            top_k=top_k,
            min_score=min_score,
        )
        hits = await _merge_unembedded_keyword_hits(
            store, query, model_id, hits,
        )
        return ChunkHitBatch(hits=hits, used_model_slots=True)
    if query_vec is None:
        if embed_error is not None:
            raise embed_error
        return ChunkHitBatch(hits=[])
    hits = await store.similarity_search(
        query_embedding=query_vec,
        top_k=top_k,
        min_score=min_score,
        source_model=source_model,
    )
    hits = await _merge_unembedded_keyword_hits(store, query, model_id, hits)
    return ChunkHitBatch(hits=hits)


async def _merge_unembedded_keyword_hits(store, query: str, model_id, hits: list) -> list:
    """向量結果之外，把三次失敗、沒有目標向量的列用關鍵字補回來。"""
    search = getattr(store, "keyword_search_unembedded", None)
    if search is None or not isinstance(model_id, int):
        return hits
    try:
        extra = await search(query, model_id, top_k=30)
    except Exception:
        return hits
    seen: set[int] = set()
    merged: list = []
    for hit in [*hits, *(extra or [])]:
        chunk = getattr(hit, "chunk", None)
        ident = getattr(chunk, "id", None)
        if isinstance(ident, int):
            if ident in seen:
                continue
            seen.add(ident)
        merged.append(hit)
    return merged

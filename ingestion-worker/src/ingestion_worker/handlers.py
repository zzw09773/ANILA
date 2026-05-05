"""Arq job handlers.

Currently one handler: ``ingest_document``. The handler is the integration
point where the pieces come together — parser, chunker registry,
embedder, and the agent-scoped store. Each piece raises
``IngestionError`` subclasses; the handler catches and persists the
structured failure into ``ingestion_jobs`` so the dev UI can render a
useful message.

Concurrency note: this handler is async and will run in the same event
loop as the Arq worker's main loop. A long-running embedding call
doesn't block other jobs — they're awaited not blocked on. That's why
the parser uses pure-Python (no thread offload) for now: the bottleneck
is the embedding endpoint, not parsing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import IngestionError, StoreError
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from ingestion_worker.embedder import Embedder
from ingestion_worker.parsers import extract_text
from ingestion_worker.settings import settings


logger = logging.getLogger(__name__)


# ── VLM caption injection ────────────────────────────────────────────
#
# Built lazily on first use so import-time has no network dependency.
# A single VisionProvider is reused across documents; httpx connection
# pooling keeps this efficient even on image-heavy queues.
_vision_provider: Any | None = None


def _get_vision_provider() -> Any | None:
    """Return a cached VisionProvider, or None if VLM caption is off.

    Returns None when:
      * ``settings.enable_image_captions`` is False, OR
      * ``settings.vision_url`` is empty (deployment doesn't have a VLM).

    Either case is a "captioning skipped" fast path — callers should
    treat None as "no captioning available, leave placeholders alone".
    """
    global _vision_provider
    if not settings.enable_image_captions:
        return None
    if not settings.vision_url:
        return None
    if _vision_provider is None:
        # Lazy import keeps the rag-extra dep optional at module load
        # — the worker boots fine even if vision isn't configured.
        from anila_core.providers.vision import VisionProvider

        _vision_provider = VisionProvider(
            base_url=settings.vision_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model,
            timeout=settings.vision_timeout_seconds,
            verify_ssl=settings.vision_verify_ssl,
            max_image_bytes=settings.vision_max_image_bytes,
        )
    return _vision_provider


# Reasoning-preamble patterns gemma4 likes to emit even when the prompt
# forbids it. We strip them at ingest time rather than fighting the
# model — same trick Studio does for its slide-spec JSON parser.
import re as _re

_THINK_BLOCK_RE = _re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", _re.DOTALL | _re.IGNORECASE,
)
# A "thought\n* ..." preamble — sometimes the model writes a markdown
# bullet list of its own reasoning before the actual answer. We strip
# from the literal "thought" prefix up to the first paragraph break that
# isn't a continuation of the bullets.
_THOUGHT_PREAMBLE_RE = _re.compile(
    r"^\s*thought\s*\n(?:[*\-\s].*?\n|\s*\n)*", _re.IGNORECASE,
)
# Maximum chars we keep per caption. Longer captions tend to be
# meta-commentary rather than image content; truncating keeps chunks
# focused and embedding cost bounded.
_CAPTION_MAX_CHARS = 600


def _clean_caption(raw: str) -> str:
    """Trim reasoning preamble + truncate captions to a sensible size.

    gemma4 emits a "thought\\n* ..." preamble even when the prompt asks
    for terse output. We strip the obvious shapes and fall back to
    "take the longest natural-language paragraph" for messier outputs.
    Empty input → empty output (caller decides whether to keep the
    placeholder when caption fails).
    """
    if not raw:
        return ""
    s = _THINK_BLOCK_RE.sub("", raw).strip()
    s = _THOUGHT_PREAMBLE_RE.sub("", s).strip()
    # If the model emitted multiple paragraphs (e.g. summary + reasoning),
    # the LAST paragraph is almost always the actual description — gemma4
    # writes its conclusion at the end. Take the last non-bulleted block
    # that has at least 20 chars so we don't keep a "Q1 Q2 Q3" header
    # fragment on its own.
    paras = [p.strip() for p in _re.split(r"\n\s*\n", s) if p.strip()]
    if paras:
        # Filter paras that are purely bullets ("*   x\n*   y") — keep
        # them only if they're ALL we have.
        non_bullet = [p for p in paras if not p.startswith("*") and len(p) >= 20]
        s = (non_bullet[-1] if non_bullet else paras[-1]).strip()
    # Strip leading markdown markers and excessive whitespace.
    s = _re.sub(r"^[\*\-\s]+", "", s)
    s = _re.sub(r"\s+", " ", s).strip()
    if len(s) > _CAPTION_MAX_CHARS:
        s = s[:_CAPTION_MAX_CHARS].rstrip() + "…"
    return s


async def _persist_images(
    pool: Any,
    collection_id: int,
    document_id: int,
    images: dict[str, Any],
    embedder: Any,
    billing_user_id: int | None,
) -> int:
    """Write every captioned image to disk + DB so Studio can later
    surface them via vector search.

    Phase 5 / Sprint X. Each ImageRef whose ``caption`` field is set
    (filled in by ``_caption_images_into`` upstream) gets:

      1. Bytes flushed to ``<UPLOAD_DIR>/anila-images/<doc_id>/<image_id>.<ext>``
         where the extension comes from ``ref.mime`` (image/png → .png).
      2. A row inserted into ``ingestion_images`` with the caption + a
         caption embedding for vector search. ``ON CONFLICT DO NOTHING``
         on (document_id, image_id) so re-ingesting the same document
         doesn't duplicate rows; the worker's existing chunk-level
         delete-and-reinsert dance handles the cleanup of stale rows
         (see migration 0025: FK to documents is ON DELETE CASCADE so
         worker's existing ``DELETE FROM ingestion_documents`` already
         takes care of the orphan case).

    Why batch the embedding into a single call: ``Embedder.embed`` is
    HTTP-backed; one call with N captions is much cheaper than N calls
    with one each, and we don't risk partial writes on transient errors
    (the function as a whole still continues on failure — caption
    embedding is best-effort).

    Returns the count of images successfully persisted (for log
    correlation).
    """
    if not images:
        return 0
    upload_dir = settings.upload_dir
    images_root = os.path.join(upload_dir, "anila-images", str(document_id))
    try:
        os.makedirs(images_root, mode=0o755, exist_ok=True)
    except OSError as e:
        # If we can't even mkdir we won't be able to persist anything;
        # bail loudly so an op-level alert can fire (the rest of ingest
        # still completes — the captions are already inlined in `text`).
        logger.error(
            "Failed to mkdir %s for image persistence: %s", images_root, e,
        )
        return 0

    # Build the to-be-inserted rows AND collect captions for batch embed.
    rows: list[dict[str, Any]] = []
    captions_to_embed: list[str] = []
    for img_id, ref in images.items():
        try:
            mime = getattr(ref, "mime", None) or "image/png"
            ext = ".png" if "png" in mime else (".jpg" if "jpeg" in mime else ".bin")
            # Sanitise image_id for the filename (parser uses UUID-ish so
            # this is paranoia, but cheap insurance against future ID
            # shapes that could include path separators).
            safe_img_id = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in str(img_id)
            )[:64]
            rel_path = os.path.join(
                "anila-images", str(document_id), f"{safe_img_id}{ext}",
            )
            abs_path = os.path.join(upload_dir, rel_path)
            image_bytes = getattr(ref, "image_bytes", b"") or b""
            if not image_bytes:
                continue
            with open(abs_path, "wb") as f:
                f.write(image_bytes)
            try:
                os.chmod(abs_path, 0o644)
            except OSError:
                pass

            caption = getattr(ref, "caption", "") or ""
            page = getattr(ref, "page", None)
            alt_text = getattr(ref, "alt_text", "") or None
            rows.append({
                "image_id": safe_img_id,
                "page": page,
                "storage_path": rel_path,
                "mime": mime,
                "alt_text": alt_text,
                "caption": caption,
                "bytes_size": len(image_bytes),
            })
            captions_to_embed.append(caption or alt_text or "image")
        except Exception as e:  # noqa: BLE001 — per-image best-effort
            logger.warning(
                "Skip persisting image %s for doc %s: %s",
                img_id, document_id, e,
            )

    if not rows:
        return 0

    # Embed all captions in one HTTP roundtrip; on failure, persist the
    # rows without an embedding (caption text + storage path are still
    # valuable, image-vector search just won't surface them).
    embeddings: list[list[float]] | None = None
    try:
        embeddings = await embedder.embed(
            captions_to_embed, user_id=billing_user_id,
        )
        if len(embeddings) != len(rows):
            logger.warning(
                "Caption embedding count mismatch (got %d, expected %d); "
                "persisting without embeddings.",
                len(embeddings), len(rows),
            )
            embeddings = None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Caption batch embedding failed for doc %s: %s — "
            "persisting rows without embeddings.",
            document_id, e,
        )

    # Use the same HalfVector wrapper the chunks store uses (see
    # anila_core/storage/adapters/pgvector_store.py:146). PgPool's
    # _init_connection registers the pgvector codec on every conn so
    # asyncpg knows the binary wire shape; passing a Python string of
    # the form '[v,v,...]' with a ``::halfvec`` cast fails because the
    # halfvec text-input parser interprets the leading `[` as a token
    # start and tries to atof() the literal — surfacing as the
    # "could not convert string to float" we hit on the first
    # deployment of this code path.
    from pgvector import HalfVector

    inserted = 0
    async with pool.acquire() as conn:
        for i, row in enumerate(rows):
            emb = embeddings[i] if embeddings is not None else None
            emb_value = HalfVector(emb) if emb is not None else None
            try:
                await conn.execute(
                    """
                    INSERT INTO ingestion_images
                        (collection_id, document_id, image_id, page,
                         storage_path, mime, alt_text, caption,
                         bytes_size, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (document_id, image_id) DO UPDATE
                       SET caption     = EXCLUDED.caption,
                           storage_path= EXCLUDED.storage_path,
                           bytes_size  = EXCLUDED.bytes_size,
                           embedding   = EXCLUDED.embedding,
                           updated_at  = CURRENT_TIMESTAMP
                    """,
                    collection_id, document_id, row["image_id"], row["page"],
                    row["storage_path"], row["mime"], row["alt_text"],
                    row["caption"], row["bytes_size"], emb_value,
                )
                inserted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to insert image row %s for doc %s: %s",
                    row["image_id"], document_id, e,
                )
    logger.info(
        "Persisted %d/%d images for doc %s (with embedding=%s)",
        inserted, len(rows), document_id, embeddings is not None,
    )
    return inserted


async def _caption_images_into(text: str, images: dict[str, Any]) -> str:
    """Replace every ``[[IMAGE:<id>]]`` placeholder in ``text`` with a
    VLM-generated caption.

    Behaviour:
      * No-op when there's no configured vision provider, no images, or
        the text contains no placeholders. Returns ``text`` unchanged.
      * Caption requests run with bounded parallelism
        (``settings.vision_concurrency``) so we don't melt the GPU when
        a 200-page PDF has 50 charts.
      * A single failed caption does NOT fail the whole ingest — the
        placeholder is replaced with a neutral ``[image]`` so chunking
        proceeds. Only logs at warning level; an operator can correlate
        with the VLM endpoint's logs if a pattern appears.

    Replacement format embeds the caption in a sentinel-flanked block so
    the chunker (and any future debugger) can tell "this came from VLM"
    from "this was prose":
      ``[圖片描述: <caption>]``
    Curly Chinese brackets are deliberately NOT used — kept ASCII-safe
    so the OpenCC-style normalization passes downstream don't fight it.
    """
    if not images or "[[IMAGE:" not in text:
        return text
    vision = _get_vision_provider()
    if vision is None:
        # Configured-off path. The chunker's existing fallback turns
        # remaining placeholders into ``[image]`` tokens, so retrieval
        # behaviour is unchanged from before this feature.
        logger.info(
            "Image captioning skipped (enable=%s url_set=%s) — %d image(s) "
            "left as placeholders.",
            settings.enable_image_captions, bool(settings.vision_url),
            len(images),
        )
        return text

    semaphore = asyncio.Semaphore(max(1, settings.vision_concurrency))

    async def _caption_one(image_id: str, ref: Any) -> tuple[str, str]:
        # Skip oversized images at the application layer — VisionProvider
        # raises on max_image_bytes too but that surfaces as a generic
        # error log; a structured fallback caption is friendlier.
        size = len(getattr(ref, "image_bytes", b"") or b"")
        if size > settings.vision_max_image_bytes:
            logger.warning(
                "Image %s skipped: %d bytes > limit %d",
                image_id, size, settings.vision_max_image_bytes,
            )
            return image_id, ""
        try:
            async with semaphore:
                caption = await vision.describe_image(
                    ref.image_bytes,
                    mime=getattr(ref, "mime", None) or "image/png",
                )
            cleaned = _clean_caption(caption)
            # Stash on the ref so the caller can persist (B.2/B.3) without
            # threading the captions dict through another layer. Existing
            # ImageRef has a `caption` slot expressly for this hand-off.
            try:
                ref.caption = cleaned
            except Exception:
                pass  # ImageRef should always be writable; defensive only
            return image_id, cleaned
        except Exception as e:  # noqa: BLE001 — best-effort; fall back gracefully
            logger.warning(
                "VLM caption failed for image %s (%s); using fallback marker.",
                image_id, type(e).__name__,
            )
            return image_id, ""

    results = await asyncio.gather(
        *(_caption_one(img_id, ref) for img_id, ref in images.items()),
        return_exceptions=False,
    )

    captions: dict[str, str] = {img_id: cap for img_id, cap in results}
    out_chunks: list[str] = []
    cursor = 0
    needle = "[[IMAGE:"
    while True:
        i = text.find(needle, cursor)
        if i < 0:
            out_chunks.append(text[cursor:])
            break
        out_chunks.append(text[cursor:i])
        end = text.find("]]", i + len(needle))
        if end < 0:
            # Malformed placeholder — leave as-is and stop scanning to
            # avoid infinite loop. Chunker's existing fallback handles
            # the leftover token gracefully.
            out_chunks.append(text[i:])
            break
        image_id = text[i + len(needle) : end]
        cap = captions.get(image_id, "")
        if cap:
            out_chunks.append(f"[圖片描述：{cap}]")
        else:
            # Captioning failed or returned empty — keep the existing
            # placeholder shape so the chunker's IMAGE-leaf fallback
            # still kicks in (it strips the `[[IMAGE:id]]` token to
            # `[image]` for indexing).
            out_chunks.append(text[i : end + 2])
        cursor = end + 2

    return "".join(out_chunks)


async def _load_document_meta(
    pool: PgPool, document_id: int
) -> dict[str, Any]:
    """Read the document row + its collection's chunking config.

    Sprint 4: ``agent_id`` no longer exists on ``ingestion_collections``;
    the collection itself IS the scope. A single SQL fetch joins the
    two tables so we don't pay an extra round trip.
    """
    sql = """
        SELECT d.id            AS document_id,
               d.collection_id AS collection_id,
               d.filename      AS filename,
               d.mime_type     AS mime_type,
               d.storage_path  AS storage_path,
               d.uploaded_by   AS uploaded_by,
               c.chunking_config AS chunking_config,
               c.created_by    AS owner_user_id
          FROM ingestion_documents d
          JOIN ingestion_collections c ON c.id = d.collection_id
         WHERE d.id = $1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, document_id)
    if row is None:
        raise StoreError(
            code="E_PG_CONSTRAINT",
            retryable=False,
            severity="error",
            user_message=f"Document {document_id} 不存在；可能已被刪除。",
            details={"document_id": document_id},
        )
    return dict(row)


async def _update_document_status(
    pool: PgPool,
    document_id: int,
    status: str,
    *,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update one document row's status. Called at every transition."""
    # Explicit ::text casts on $2 so asyncpg doesn't trip on the
    # parameter being used in both ``SET status = $2`` (varchar column)
    # and ``CASE WHEN $2 = 'indexed'`` (text literal compare). Without
    # the cast it raises AmbiguousParameterError.
    sql = """
        UPDATE ingestion_documents
           SET status = $2::text,
               chunk_count = COALESCE($3, chunk_count),
               error_message = $4,
               indexed_at = CASE WHEN $2::text = 'indexed' THEN now() ELSE indexed_at END
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, document_id, status, chunk_count, error_message)


async def _bump_collection_counters(
    pool: PgPool, collection_id: int, document_count_delta: int, chunk_count_delta: int
) -> None:
    """Adjust collection-level counters atomically.

    Denormalized counters keep the list page snappy without a JOIN
    aggregation per render. The worker is the only writer so there's
    no contention concern.
    """
    sql = """
        UPDATE ingestion_collections
           SET document_count = document_count + $2,
               chunk_count    = chunk_count    + $3,
               updated_at     = now()
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, collection_id, document_count_delta, chunk_count_delta)


async def _record_job_failure(
    pool: PgPool, arq_job_id: str | None, err: IngestionError
) -> None:
    """Mark the matching ingestion_jobs row as failed with the error code.

    Best-effort — failure to update the job row should never re-raise out
    of the handler (would mask the original error).
    """
    if arq_job_id is None:
        return
    sql = """
        UPDATE ingestion_jobs
           SET status = 'failed',
               progress_pct = 100,
               error_code = $2::text,
               error_message = $3::text,
               completed_at = now()
         WHERE arq_job_id = $1::text
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(sql, arq_job_id, err.code, err.user_message)
    except Exception:
        # Don't shadow the original IngestionError.
        pass


async def _update_job(
    pool: PgPool,
    arq_job_id: str | None,
    *,
    status: str | None = None,
    progress_pct: int | None = None,
    progress_message: str | None = None,
    started: bool = False,
    succeeded: bool = False,
) -> None:
    """Update one ingestion_jobs row's status / progress.

    Used by the handler to drive the SSE stream's progression
    (queued → running → parsing → chunking → embedding → indexing →
    succeeded). Best-effort — silently ignores DB failures so a
    transient blip doesn't kill the actual ingestion.
    """
    if arq_job_id is None:
        return
    sets = []
    args: list = [arq_job_id]
    if status is not None:
        sets.append(f"status = ${len(args) + 1}::text")
        args.append(status)
    if progress_pct is not None:
        sets.append(f"progress_pct = ${len(args) + 1}::smallint")
        args.append(progress_pct)
    if progress_message is not None:
        sets.append(f"progress_message = ${len(args) + 1}::text")
        args.append(progress_message)
    if started:
        sets.append("started_at = COALESCE(started_at, now())")
    if succeeded:
        sets.append("completed_at = now()")
    if not sets:
        return
    sql = (
        "UPDATE ingestion_jobs SET "
        + ", ".join(sets)
        + " WHERE arq_job_id = $1::text"
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(sql, *args)
    except Exception:
        pass


# ── Handler ─────────────────────────────────────────────────────────────────


async def ingest_document(ctx: dict[str, Any], document_id: int) -> dict[str, Any]:
    """Parse → chunk → embed → index one document.

    ``ctx`` is Arq's per-call context; the worker config injects the
    shared ``pool`` and ``embedder`` into ctx during ``startup``.
    Returns a small summary dict so the job result row carries
    "11 chunks indexed in 4.2s" without re-querying the DB.
    """
    pool: PgPool = ctx["pool"]
    embedder: Embedder = ctx["embedder"]
    arq_job_id: str | None = ctx.get("job_id")

    started_at = datetime.now(timezone.utc)
    await _update_job(pool, arq_job_id, status="running", started=True, progress_pct=5)
    try:
        meta = await _load_document_meta(pool, document_id)
        collection_id = int(meta["collection_id"])
        storage_path = meta["storage_path"]
        # Bill embedding usage to whoever uploaded the file; fall back
        # to the collection owner when the doc row's uploaded_by is null
        # (could happen for system-seeded docs).
        billing_user_id = (
            meta.get("uploaded_by") or meta.get("owner_user_id")
        )
        if not storage_path or not os.path.exists(storage_path):
            raise StoreError(
                code="E_INTERNAL",
                retryable=False,
                severity="error",
                user_message=(
                    f"Uploaded blob missing on disk: {storage_path or '(no path)'}"
                ),
                details={"storage_path": storage_path},
            )

        # 1. Parse — pure function, fast.
        await _update_document_status(pool, document_id, "parsing")
        await _update_job(pool, arq_job_id, progress_pct=15, progress_message="parsing")
        with open(storage_path, "rb") as f:
            blob = f.read()
        text, parse_meta, images = extract_text(
            meta["filename"], blob, meta["mime_type"],
        )

        # 1a. Caption embedded images via VLM (when configured).
        # Replaces ``[[IMAGE:<id>]]`` placeholders with VLM-generated
        # descriptions BEFORE chunking, so charts/diagrams become
        # searchable text instead of opaque tokens. No-op if disabled
        # or no images. See _caption_images_into for the full contract.
        if images:
            await _update_job(
                pool, arq_job_id,
                progress_pct=22,
                progress_message=f"captioning {len(images)} image(s)",
            )
            text = await _caption_images_into(text, images)
            # 1b. Persist captioned images to disk + DB so Studio can
            # vector-search over them (Phase 5). Best-effort: a failure
            # here doesn't fail ingest — the captions are already inlined
            # into `text` from step 1a, so retrieval over chunks still
            # works. Only the image-as-image use case is degraded.
            try:
                await _persist_images(
                    pool, collection_id, document_id, images,
                    embedder, billing_user_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Image persistence failed for doc %s: %s — captions "
                    "are still inlined in chunks; image-as-image retrieval "
                    "will be empty for this document.",
                    document_id, e,
                )

        # 2. Chunk — bounded by document size, also fast.
        # Semantic strategies need embeddings up-front: pre-split into
        # candidate segments, embed each, then call ``chunk()`` with the
        # embeddings stuffed into params. This keeps the chunker
        # interface pure-sync at the cost of a second embedding pass
        # (whose tokens we'd compute anyway).
        await _update_document_status(pool, document_id, "chunking")
        await _update_job(pool, arq_job_id, progress_pct=30, progress_message="chunking")
        chunking_config = meta["chunking_config"] or {"strategy": "hierarchical"}
        strategy = chunking_config.get("strategy", "hierarchical")
        params = dict(chunking_config.get("params", {}))
        chunker = get_chunker(strategy)
        if getattr(chunker, "requires_embedder", False):
            from anila_core.ingestion.chunking_plugins.builtins import SemanticChunker

            min_tok = int(params.get("min_segment_tokens", 128))
            segments = SemanticChunker.split_segments(text, min_tokens=min_tok)
            params["_segments"] = segments
            if len(segments) >= 2:
                # Real path: embed every candidate segment, semantic
                # chunker does the boundary detection.
                params["_embeddings"] = await embedder.embed(segments, user_id=billing_user_id)
            elif len(segments) == 1:
                # Single-segment short-circuit. The chunker checks
                # ``len(segments) == 1`` early and returns one chunk
                # without touching the embeddings list, but we still
                # need the count to match (or emit a dummy entry to
                # satisfy the mismatch guard).
                params["_embeddings"] = [[]]
            else:
                params["_embeddings"] = []
        chunks = chunker.chunk(text, parse_meta, params)
        if not chunks:
            await _update_document_status(
                pool, document_id, "indexed",
                chunk_count=0,
                error_message=None,
            )
            return {"chunk_count": 0, "warning": "no chunks produced"}

        # Sprint 9 X / parent-child — two-pass persistence.
        #
        # The HierarchicalChunker (and any future tree-emitting
        # chunker) returns a mix of:
        #
        #   * parent rows (chunk_type='heading' / 'document') — no
        #     embedding; their content is the heading title which
        #     gets JOIN-fetched as ``parent_content`` at retrieval.
        #   * leaf rows (chunk_type='leaf') — embedded for vector
        #     search; carry ``parent_chunk_key`` in metadata pointing
        #     at one of the parent rows above.
        #
        # We split, insert parents first (returning chunk_key→id),
        # then embed and insert leaves with parent_chunk_id resolved.
        # Other chunkers that emit only leaves still work unchanged
        # because the partition simply yields an empty parents list.
        parents = [c for c in chunks if (c.metadata or {}).get("chunk_type") in ("heading", "document")]
        leaves = [c for c in chunks if (c.metadata or {}).get("chunk_type", "leaf") == "leaf"]

        # 3. Embed leaves only (the slow part). Parent rows skip the
        #    embedding pass entirely → cost stays at the same total
        #    embedded-tokens count as the pre-9-X flat-leaf flow.
        await _update_document_status(pool, document_id, "embedding")
        await _update_job(
            pool, arq_job_id, progress_pct=60,
            progress_message=f"embedding {len(leaves)} leaf chunks",
        )
        embeddings = await embedder.embed(
            [c.content for c in leaves], user_id=billing_user_id,
        ) if leaves else []

        # 4. Index — parents first to populate the chunk_key→id map;
        #    leaves second with their parent_chunk_id resolved.
        await _update_job(pool, arq_job_id, progress_pct=85, progress_message="indexing")
        store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
        parent_id_map: dict[str, int] = {}
        if parents:
            parent_id_map = await store.add_parent_chunks(
                document_id=document_id,
                chunks=parents,
            )
        if leaves:
            await store.index_chunks(
                document_id=document_id,
                chunks=leaves,
                embeddings=embeddings,
                parent_id_map=parent_id_map,
            )

        total_chunks = len(chunks)
        # 5. Status + counters.
        await _update_document_status(
            pool, document_id, "indexed",
            chunk_count=total_chunks,
            error_message=None,
        )
        await _bump_collection_counters(
            pool, collection_id, document_count_delta=1, chunk_count_delta=total_chunks
        )
        await _update_job(
            pool, arq_job_id, status="succeeded", succeeded=True,
            progress_pct=100,
            progress_message=(
                f"{len(leaves)} leaves + {len(parents)} parents indexed"
            ),
        )

        return {
            "chunk_count": total_chunks,
            "leaf_count": len(leaves),
            "parent_count": len(parents),
            "elapsed_seconds": (
                datetime.now(timezone.utc) - started_at
            ).total_seconds(),
        }

    except IngestionError as err:
        # Persist the structured failure for the dev UI / inspector.
        await _update_document_status(
            pool, document_id, "failed",
            error_message=err.user_message or err.code,
        )
        await _record_job_failure(pool, arq_job_id, err)
        # Re-raise so Arq's retry policy sees the failure too.
        raise
    except Exception as e:
        # Unknown failure → wrap as E_INTERNAL with bounded leakage.
        wrapped = StoreError(
            code="E_INTERNAL",
            retryable=False,
            severity="error",
            user_message="內部錯誤，請聯絡管理員。",
            details={"cause": type(e).__name__, "message": str(e)[:200]},
        )
        await _update_document_status(
            pool, document_id, "failed",
            error_message=wrapped.user_message,
        )
        await _record_job_failure(pool, arq_job_id, wrapped)
        raise

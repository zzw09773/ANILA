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
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import asyncpg

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import ChunkError, IngestionError, StoreError
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from ingestion_worker.embedder import Embedder
from ingestion_worker.parsers import extract_text
from ingestion_worker.settings import settings


logger = logging.getLogger(__name__)


# ── VLM caption injection ────────────────────────────────────────────
#
# Built lazily on first use so import-time has no network dependency.
# Cached per model name so collection.caption_model cannot leak onto
# the next document (a single global would stamp the wrong fact).
_vision_providers: dict[str, Any] = {}
# Back-compat alias: existing tests assign ``handlers._vision_provider``.
_vision_provider: Any | None = None


def _empty_caption_stats(
    *, image_count: int, attempted: int = 0, required: bool = False,
) -> dict[str, int]:
    return {
        "image_count": image_count,
        "attempted": attempted,
        "succeeded": 0,
        "failed": 0,
        "reused": 0,
        "required": 1 if required else 0,
    }


def _resolve_caption_intent(
    collection_enabled: Any,
    collection_model: Any,
) -> tuple[bool, str | None]:
    """Intent vs platform default.

    NULL collection columns follow ``enable_image_captions`` / ``VISION_MODEL``.
    A False collection flag wins over a True platform flag (plain-text KB).
    A True collection flag still needs ``vision_url`` at runtime.
    """
    if collection_enabled is None:
        want = bool(settings.enable_image_captions)
    else:
        want = bool(collection_enabled)
    model = (collection_model or "").strip() or settings.vision_model
    return want, model


def _get_vision_provider(model: str | None = None) -> Any | None:
    """Return a cached VisionProvider for ``model``, or None if no VLM URL.

    On/off is decided by ``_resolve_caption_intent`` (collection NULL
    follows ``enable_image_captions``). This factory only answers
    "can we reach a VLM?". Empty ``vision_url`` → None.
    """
    global _vision_provider
    if not settings.vision_url:
        return None
    chosen = (model or settings.vision_model or "").strip() or settings.vision_model
    cached = _vision_providers.get(chosen)
    if cached is not None:
        return cached
    if _vision_provider is not None and not _vision_providers:
        # Test / leftover single-slot cache.
        return _vision_provider
    from anila_core.providers.vision import VisionProvider

    provider = VisionProvider(
        base_url=settings.vision_url,
        api_key=settings.vision_api_key,
        model=chosen,
        timeout=settings.vision_timeout_seconds,
        verify_ssl=True,
        max_image_bytes=settings.vision_max_image_bytes,
    )
    _vision_providers[chosen] = provider
    _vision_provider = provider
    return provider


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

# Provenance separator in caption_source_model: "{model}@{generator_id}".
# Generator id is hex; rsplit keeps a model name that itself contains @.
_CAPTION_PROVENANCE_SEP = "@"


def _safe_image_id(image_id: str) -> str:
    """Same sanitiser persist and reuse-load must share, or the lookup misses."""
    return "".join(
        c if c.isalnum() or c in "-_" else "_" for c in str(image_id)
    )[:64]


def _fn_fingerprint(fn: Any) -> str:
    """Source if we have it; bytecode otherwise. Either one moves when the
    function's behaviour moves."""
    import inspect

    try:
        src = inspect.getsource(fn)
        if src:
            return src
    except (OSError, TypeError):
        pass
    code = getattr(fn, "__code__", None)
    if code is not None:
        return f"{fn.__module__}.{fn.__qualname__}:{code.co_code.hex()}"
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', fn)}"


def _re_fingerprint(compiled: Any) -> str:
    """The compiled pattern and flags, not the variable name.

    ``_clean_caption``'s source only names ``_THINK_BLOCK_RE`` /
    ``_THOUGHT_PREAMBLE_RE``. Changing the regex would otherwise
    leave the generator id still.
    """
    return (
        f"{getattr(compiled, 'pattern', '')}\n"
        f"flags={int(getattr(compiled, 'flags', 0))}"
    )


def _caption_generator_id_compute() -> str:
    """Hash the listed inputs. Empty string is not a valid id.

    Blob (only these; not every path that can change a caption):
      * ``_CAPTION_MAX_CHARS``
      * ``_THINK_BLOCK_RE`` / ``_THOUGHT_PREAMBLE_RE`` pattern+flags
      * source of ``_clean_caption``
      * source of ``classify_caption`` / ``is_repetitive_caption`` /
        ``mark_truncated``
      * ``_DEFAULT_PROMPT``
      * source of ``VisionProvider.describe_image``
    Not in the blob: ``_is_uniform_color``, vision callees beyond
    ``describe_image``.
    """
    import hashlib

    from anila_core.providers.caption_quality import (
        classify_caption,
        is_repetitive_caption,
        mark_truncated,
    )
    from anila_core.providers.vision import VisionProvider, _DEFAULT_PROMPT

    blob = "\n".join(
        [
            f"max_chars={_CAPTION_MAX_CHARS}",
            _re_fingerprint(_THINK_BLOCK_RE),
            _re_fingerprint(_THOUGHT_PREAMBLE_RE),
            _fn_fingerprint(_clean_caption),
            _fn_fingerprint(classify_caption),
            _fn_fingerprint(is_repetitive_caption),
            _fn_fingerprint(mark_truncated),
            _DEFAULT_PROMPT,
            _fn_fingerprint(VisionProvider.describe_image),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def caption_generator_id() -> str:
    """Reuse key half. Empty means "do not reuse this ingest".

    Import / inspect failure must not raise into ingest — an empty
    id fails the match (``prev_gid and prev_gid == fact_gid``).
    """
    try:
        return _caption_generator_id_compute()
    except Exception:
        logger.warning(
            "caption_generator_id failed; reuse disabled this ingest",
            exc_info=True,
        )
        return ""


def _caption_provenance(model: str | None, generator_id: str) -> str:
    model_s = (model or "").strip()
    gid = (generator_id or "").strip()
    if not model_s:
        return gid
    if not gid:
        return model_s
    return f"{model_s}{_CAPTION_PROVENANCE_SEP}{gid}"


def _split_caption_provenance(stored: str | None) -> tuple[str, str]:
    s = (stored or "").strip()
    if not s:
        return "", ""
    if _CAPTION_PROVENANCE_SEP in s:
        model, gid = s.rsplit(_CAPTION_PROVENANCE_SEP, 1)
        return model.strip(), gid.strip()
    return s, ""


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
        # Same mark as the VLM finish_reason=length path. Completeness
        # is decided here because this is the last cut — classify_caption
        # upstream cannot see this ceiling. One mark: the reader needs
        # "incomplete", not which layer cut it.
        from anila_core.providers.caption_quality import mark_truncated

        s = mark_truncated(s[:_CAPTION_MAX_CHARS].rstrip())
        logger.info(
            "caption truncated by worker_char_ceiling at %d chars",
            _CAPTION_MAX_CHARS,
        )
    return s


def _is_uniform_color(
    img_bytes: bytes,
    *,
    tolerance: int = 8,
    sample_n: int = 100,
) -> bool:
    """Return True if the image is near-uniform color (max RGB range <= tolerance).

    Skips this check for tiny images (< 100 pixels total) — icons are
    intentionally small + low-variance and should NOT be filtered out
    by this check.

    Decode failure or empty input returns False (let downstream
    decoder handle the actual error, don't pretend the image is
    uniform when we couldn't read it).

    Used to filter out PDF background rectangles before captioning +
    persistence — they have no informational content but, if kept,
    burn VLM tokens and pollute RAG retrieval.
    """
    if not img_bytes:
        return False
    try:
        from PIL import Image  # local import to avoid Pillow at module load
        import io
        import random

        with Image.open(io.BytesIO(img_bytes)) as img:
            img = img.convert("RGB")
            w, h = img.size
            if w * h < 100:
                return False
            rng = random.Random(0)
            pixels = [
                img.getpixel((rng.randint(0, w - 1), rng.randint(0, h - 1)))
                for _ in range(sample_n)
            ]
        rs = [p[0] for p in pixels]
        gs = [p[1] for p in pixels]
        bs = [p[2] for p in pixels]
        max_range = max(max(rs) - min(rs), max(gs) - min(gs), max(bs) - min(bs))
        return max_range <= tolerance
    except Exception:
        return False


async def _persist_images(
    pool: Any,
    collection_id: int,
    document_id: int,
    images: dict[str, Any],
    embedder: Any,
    billing_user_id: int | None,
) -> dict[str, int]:
    """Write every captioned image to disk + DB so Studio can later
    surface them via vector search.

    Phase 5 / Sprint X. Each ImageRef whose ``caption`` field is set
    (filled in by ``_caption_images_into`` upstream) gets:

      1. Bytes flushed to ``<UPLOAD_DIR>/anila-images/<doc_id>/<image_id>.<ext>``
         where the extension comes from ``ref.mime`` (image/png → .png).
      2. A row inserted into ``ingestion_images`` with the caption + a
         caption embedding for vector search. ``ON CONFLICT`` updates
         on (document_id, image_id) so re-ingesting the same document
         doesn't duplicate rows. Embedding + provenance use
         ``COALESCE(EXCLUDED.*, existing)`` so a re-import whose
         embedder is down cannot NULL a vector that was already good.
         The worker's existing chunk-level delete-and-reinsert dance
         handles the cleanup of stale rows (see migration 0025: FK to
         documents is ON DELETE CASCADE so worker's existing
         ``DELETE FROM ingestion_documents`` already takes care of the
         orphan case).

    Why batch the embedding into a single call: ``Embedder.embed`` is
    HTTP-backed; one call with N captions is much cheaper than N calls
    with one each, and we don't risk partial writes on transient errors
    (the function as a whole still continues on failure — caption
    embedding is best-effort).

    Returns ``{parser_image_id: ingestion_images.id}`` for every row that
    actually landed. Callers stamp those PKs onto chunk metadata — the
    blob endpoint addresses rows by BIGSERIAL PK, not the per-document
    TEXT id. Empty dict on every early-exit / total-failure path.
    """
    if not images:
        return {}
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
        return {}

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
            safe_img_id = _safe_image_id(img_id)
            rel_path = os.path.join(
                "anila-images", str(document_id), f"{safe_img_id}{ext}",
            )
            abs_path = os.path.join(upload_dir, rel_path)
            image_bytes = getattr(ref, "image_bytes", b"") or b""
            if not image_bytes:
                continue
            # Defensive filter: drop uniform-color images even if
            # captioning was disabled / bypassed. Covers the path where
            # ``settings.enable_image_captions`` is False but the PDF
            # extractor still produced background-fill rectangles.
            # Confirmed against doc 1: img_4fd6f21243.jpg (RGB(26,54,93)),
            # img_d2e6cf287b.jpg and img_6cb40697ca.jpg (RGB(44,82,129)).
            if _is_uniform_color(image_bytes):
                logger.info(
                    "Skipping persist for uniform-color image %s "
                    "(doc %s) — likely PDF background fill",
                    img_id, document_id,
                )
                continue
            with open(abs_path, "wb") as f:
                f.write(image_bytes)
            try:
                os.chmod(abs_path, 0o644)
            except OSError:
                pass

            caption = getattr(ref, "caption", "") or ""
            caption_source = getattr(ref, "caption_source_model", None) or None
            if caption_source:
                caption_source = str(caption_source)
            page = getattr(ref, "page", None)
            alt_text = getattr(ref, "alt_text", "") or None
            rows.append({
                "parser_id": str(img_id),
                "image_id": safe_img_id,
                "page": page,
                "storage_path": rel_path,
                "mime": mime,
                "alt_text": alt_text,
                "caption": caption,
                "caption_source_model": caption_source,
                "bytes_size": len(image_bytes),
            })
            captions_to_embed.append(caption or alt_text or "image")
        except Exception as e:  # noqa: BLE001 — per-image best-effort
            logger.warning(
                "Skip persisting image %s for doc %s: %s",
                img_id, document_id, e,
            )

    if not rows:
        return {}

    # Embed the captions in as few roundtrips as the batch size allows
    # (``Embedder.embed`` splits at ``EMBEDDING_BATCH_SIZE``, so an
    # image-heavy PDF is several requests, not one); on failure, persist
    # the rows without an embedding (caption text + storage path are
    # still valuable, image-vector search just won't surface them).
    embeddings: list[list[float]] | None = None
    try:
        embeddings = await embedder.embed(
            captions_to_embed, user_id=billing_user_id,
        )
        if len(embeddings) != len(rows):
            logger.warning(
                "Caption embedding count mismatch (got %d, expected %d); "
                "continuing; existing embeddings are kept on conflict, "
                "new rows land without one.",
                len(embeddings), len(rows),
            )
            embeddings = None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Caption batch embedding failed for doc %s: %s — "
            "continuing; existing embeddings are kept on conflict, "
            "new rows land without one.",
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

    source_model = getattr(embedder, "model_name", None)
    native_dim = getattr(embedder, "native_dim", None)

    pk_by_parser_id: dict[str, int] = {}
    # Outer transaction so the SET LOCAL GUC takes effect (SET LOCAL is
    # txn-scoped) and is confined to this acquire — it never leaks to the next
    # pooled user. ingestion_images is FORCE-RLS (migration 0037): the INSERT
    # policy (WITH CHECK reuses the collection_id USING expr) only accepts rows
    # whose collection_id matches anila.collection_id, so we scope it here.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"SET LOCAL anila.collection_id = {int(collection_id)}"
        )
        for i, row in enumerate(rows):
            emb = embeddings[i] if embeddings is not None else None
            emb_value = HalfVector(emb) if emb is not None else None
            try:
                # Savepoint per row: a single bad row rolls back to here rather
                # than aborting the whole batch (preserves the prior best-effort
                # continue-on-error behaviour now that we're inside a txn).
                async with conn.transaction():
                    row_pk = await conn.fetchval(
                        """
                        INSERT INTO ingestion_images
                            (collection_id, document_id, image_id, page,
                             storage_path, mime, alt_text, caption,
                             bytes_size, embedding,
                             embedding_source_model, embedding_native_dim,
                             caption_source_model)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                        ON CONFLICT (document_id, image_id) DO UPDATE
                           SET caption     = EXCLUDED.caption,
                               storage_path= EXCLUDED.storage_path,
                               bytes_size  = EXCLUDED.bytes_size,
                               -- Re-import must not worsen an existing vector:
                               -- a dead embedder binds NULL here; keep the row's.
                               embedding   = COALESCE(EXCLUDED.embedding, ingestion_images.embedding),
                               embedding_source_model = COALESCE(
                                   EXCLUDED.embedding_source_model,
                                   ingestion_images.embedding_source_model),
                               embedding_native_dim   = COALESCE(
                                   EXCLUDED.embedding_native_dim,
                                   ingestion_images.embedding_native_dim),
                               caption_source_model = COALESCE(
                                   EXCLUDED.caption_source_model,
                                   ingestion_images.caption_source_model),
                               updated_at  = CURRENT_TIMESTAMP
                        RETURNING id
                        """,
                        collection_id, document_id, row["image_id"], row["page"],
                        row["storage_path"], row["mime"], row["alt_text"],
                        row["caption"], row["bytes_size"], emb_value,
                        source_model, native_dim, row["caption_source_model"],
                    )
                if row_pk is not None:
                    pk_by_parser_id[row["parser_id"]] = int(row_pk)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to insert image row %s for doc %s: %s",
                    row["image_id"], document_id, e,
                )
    logger.info(
        "Persisted %d/%d images for doc %s (with embedding=%s)",
        len(pk_by_parser_id), len(rows), document_id, embeddings is not None,
    )
    return pk_by_parser_id


async def _load_existing_captions(
    pool: Any,
    collection_id: int,
    document_id: int,
    parser_image_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Read each parser image's already-persisted caption (if any) for reuse.

    ``ingestion_images`` is FORCE RLS (0037): the USING expr only admits rows
    whose collection matches ``anila.collection_id``. So this sets the SAME
    real collection GUC ``_persist_images`` uses (a literal 0 would admit
    nothing), inside the same ``conn.transaction()`` to shrink the
    ``SET LOCAL`` to this one acquire.

    The read is fire-and-forget by the caller (``reused_captions`` may be
    empty) so an early failure here never marks the document failed — the
    fallback is simply "no caption to reuse, caption everything from
    scratch".
    """
    if not parser_image_ids:
        return {}
    safe_ids = [_safe_image_id(i) for i in parser_image_ids]
    safe_to_parser: dict[str, str] = {}
    for pid in parser_image_ids:
        safe_to_parser.setdefault(_safe_image_id(pid), str(pid))
    sql = """
        SELECT image_id, caption, caption_source_model
          FROM ingestion_images
         WHERE document_id = $1
           AND image_id = ANY($2::text[])
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            rows = await conn.fetch(sql, document_id, safe_ids)
    except Exception:
        # Best-effort read. Never shadow the ingest itself.
        return {}
    return {
        safe_to_parser.get(r["image_id"], r["image_id"]): {
            "caption": r["caption"] or "",
            "source_model": r["caption_source_model"],
        }
        for r in rows
    }


def format_caption_progress(stats: dict[str, int]) -> str:
    """User-visible line: ``0 張圖`` ≠ ``3 張圖、0 張成功``.

    ``image_count`` is figures in the document (the denominator the
    owner can check against the PDF). ``succeeded`` is captions that
    actually landed. All-fail must not look like a plain success.
    """
    n = int(stats.get("image_count") or 0)
    ok = int(stats.get("succeeded") or 0)
    required = bool(stats.get("required"))
    if n == 0:
        return "0 張圖"
    if not required:
        return f"{n} 張圖（未做圖說）"
    return f"{n} 張圖、{ok} 張成功"


async def _caption_images_into(
    text: str,
    images: dict[str, Any],
    *,
    model: str | None = None,
    enabled: bool = True,
    reuse: bool = True,
    reused_captions: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int]]:
    """Replace every ``[[IMAGE:<id>]]`` placeholder in ``text`` with a
    VLM-generated caption. Returns ``(rewritten_text, caption_stats)``.

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
      * Stats always carry ``image_count`` (figures in the file) so
        "0 張圖" and "N 張圖、0 張成功" cannot collapse.

    Replacement format embeds the caption in a sentinel-flanked block so
    the chunker (and any future debugger) can tell "this came from VLM"
    from "this was prose":
      ``[圖片描述: <caption>]``
    Curly Chinese brackets are deliberately NOT used — kept ASCII-safe
    so the OpenCC-style normalization passes downstream don't fight it.

    Reuse (LOW, cost-only): take a persisted caption instead of a
    VLM call only when the VLM name and ``caption_generator_id()``
    both still match. The id hashes the list in
    ``_caption_generator_id_compute`` (max chars, the two think /
    preamble regexes, ``_clean_caption``, the three quality
    functions, the default prompt, ``describe_image``). It does
    not hash everything that can change a caption. A missing id
    (compute failed, or a legacy model-only row) does not match,
    so this ingest re-describes.
    """
    if reused_captions is None:
        reused_captions = {}
    n_images = len(images or {})
    empty = _empty_caption_stats(image_count=n_images, required=False)
    if not images or "[[IMAGE:" not in text:
        return text, empty
    if not enabled:
        logger.info(
            "Image captioning skipped (collection/platform off) — %d image(s) "
            "left as placeholders.",
            n_images,
        )
        return text, empty
    vision = _get_vision_provider(model)
    if vision is None:
        # Configured-off path. The chunker's existing fallback turns
        # remaining placeholders into ``[image]`` tokens, so retrieval
        # behaviour is unchanged from before this feature.
        logger.info(
            "Image captioning skipped (enable=%s url_set=%s) — %d image(s) "
            "left as placeholders.",
            settings.enable_image_captions, bool(settings.vision_url),
            n_images,
        )
        return text, _empty_caption_stats(image_count=n_images, required=True)

    semaphore = asyncio.Semaphore(max(1, settings.vision_concurrency))

    fact_model = getattr(vision, "model", None) or model
    fact_gid = caption_generator_id()
    fact_prov = _caption_provenance(fact_model, fact_gid)

    # Partition before firing a single VLM call. Reuse only when the
    # persisted provenance still names THIS model AND THIS generator.
    # Model-only legacy rows (no generator id) do not match — otherwise
    # a generator change would keep the old 601/601/601 captions and
    # its own re-import acceptance would lie.
    reused: dict[str, str] = {}
    to_caption: dict[str, Any] = {}
    if reuse:
        for img_id, ref in images.items():
            prev = reused_captions.get(img_id)
            prev_cap = (prev.get("caption") or "").strip() if prev else ""
            prev_model, prev_gid = _split_caption_provenance(
                (prev.get("source_model") or "") if prev else ""
            )
            if (
                prev_cap
                and fact_gid
                and prev_model == (fact_model or "").strip()
                and prev_gid
                and prev_gid == fact_gid
            ):
                reused[img_id] = prev_cap
                # Stash so _persist_images re-upserts the SAME caption
                # rather than wiping the row with "" (which re-use would
                # otherwise silently do to the source it just read).
                try:
                    ref.caption = prev_cap
                    ref.caption_source_model = _caption_provenance(
                        prev_model, prev_gid
                    )
                except Exception:
                    pass
            else:
                to_caption[img_id] = ref
    else:
        to_caption = dict(images)

    async def _caption_one(image_id: str, ref: Any) -> tuple[str, str, bool]:
        # Skip oversized images at the application layer — VisionProvider
        # raises on max_image_bytes too but that surfaces as a generic
        # error log; a structured fallback caption is friendlier.
        img_bytes = getattr(ref, "image_bytes", b"") or b""
        size = len(img_bytes)
        if size > settings.vision_max_image_bytes:
            logger.warning(
                "Image %s skipped: %d bytes > limit %d",
                image_id, size, settings.vision_max_image_bytes,
            )
            return image_id, "", False
        # Skip uniform-color images (PDF background fills, decorative
        # solid bands). They get captioned as "一張純藍色的圖片" by the VLM,
        # then persisted, then pollute RAG citations. Drop here so we
        # also save the VLM API call. The empty caption returned makes
        # _persist_images leave the placeholder alone so the chunker's
        # IMAGE-leaf fallback turns the token into ``[image]``.
        if _is_uniform_color(img_bytes):
            logger.info(
                "Image %s skipped: uniform color (likely PDF background)",
                image_id,
            )
            try:
                ref.caption = ""
            except Exception:
                pass
            return image_id, "", False
        try:
            async with semaphore:
                caption = await vision.describe_image(
                    ref.image_bytes,
                    mime=getattr(ref, "mime", None) or "image/png",
                )
            cleaned = _clean_caption(caption)
            from anila_core.providers.caption_quality import (
                classify_caption,
                mark_truncated,
            )

            kind = classify_caption(cleaned)
            if kind == "repetitive":
                logger.warning(
                    "VLM caption for image %s discarded as repetitive loop",
                    image_id,
                )
                try:
                    ref.caption = ""
                except Exception:
                    pass
                return image_id, "", True
            if kind == "truncated":
                cleaned = mark_truncated(cleaned)
            # Stash on the ref so the caller can persist (B.2/B.3) without
            # threading the captions dict through another layer. Existing
            # ImageRef has a `caption` slot expressly for this hand-off.
            # caption_source_model is *fact*: only set when a caption
            # actually landed. A failed call must not pretend this model
            # produced anything.
            try:
                ref.caption = cleaned
                if cleaned:
                    ref.caption_source_model = fact_prov
            except Exception:
                pass  # ImageRef should always be writable; defensive only
            return image_id, cleaned, True
        except Exception as e:  # noqa: BLE001 — best-effort; fall back gracefully
            logger.warning(
                "VLM caption failed for image %s (%s); using fallback marker.",
                image_id, type(e).__name__,
            )
            return image_id, "", True

    results = await asyncio.gather(
        *(_caption_one(img_id, ref) for img_id, ref in to_caption.items()),
        return_exceptions=False,
    )

    captions: dict[str, str] = {img_id: cap for img_id, cap, _tried in results}
    captions.update(reused)
    attempted = sum(1 for _i, _c, tried in results if tried)
    from anila_core.providers.caption_quality import is_repetitive_caption

    succeeded_new = sum(
        1
        for _i, cap, tried in results
        if tried and cap and not is_repetitive_caption(cap)
    )
    succeeded = succeeded_new + sum(
        1 for cap in reused.values() if cap and not is_repetitive_caption(cap)
    )
    failed = attempted - succeeded_new
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

    stats = {
        "image_count": n_images,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "reused": len(reused),
        "required": 1,
    }
    return "".join(out_chunks), stats


def _attach_image_pks_to_chunks(
    chunks: list[Any],
    images: dict[str, Any],
    pk_by_parser_id: dict[str, int],
) -> list[Any]:
    """Stamp ``ingestion_images.id`` onto each chunk that contains that figure.

    Embed ``content`` is left untouched — captions stay searchable text;
    URLs / ``![]()`` never enter the index. Matching is by the caption
    block we just wrote (``[圖片描述：…]``) or a leftover ``[[IMAGE:id]]``
    token when captioning was skipped. Chunks without a figure keep their
    original metadata.
    """
    if not chunks or not pk_by_parser_id:
        return chunks

    out: list[Any] = []
    for ch in chunks:
        content = getattr(ch, "content", "") or ""
        pks: list[int] = []
        seen: set[int] = set()
        for parser_id, pk in pk_by_parser_id.items():
            if pk in seen:
                continue
            ref = images.get(parser_id)
            cap = (getattr(ref, "caption", "") or "").strip() if ref is not None else ""
            hit = f"[[IMAGE:{parser_id}]]" in content
            if cap and f"[圖片描述：{cap}]" in content:
                hit = True
            if not hit:
                continue
            pks.append(int(pk))
            seen.add(pk)
        if not pks:
            out.append(ch)
            continue
        meta = dict(getattr(ch, "metadata", None) or {})
        meta["image_pks"] = pks
        out.append(replace(ch, metadata=meta))
    return out


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
               c.created_by    AS owner_user_id,
               c.caption_enabled AS caption_enabled,
               c.caption_model   AS caption_model
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

    # P4.8: refresh embedder against the current platform designation so
    # a mid-runtime admin change propagates without restarting the worker.
    from ingestion_worker.platform_embedding import resolve_from_pool

    try:
        resolved = await resolve_from_pool(
            pool,
            settings_fallback_name=settings.embedding_model,
            settings_fallback_native=getattr(
                embedder, "native_dim", settings.embedding_dim
            ),
        )
        if (
            resolved.name != embedder.model_name
            or resolved.native_dim != embedder.native_dim
        ):
            await embedder.close()
            embedder = Embedder(
                settings,
                model_name=resolved.name,
                native_dim=resolved.native_dim,
            )
            ctx["embedder"] = embedder
    except Exception:
        logger.exception(
            "ingest_document: platform embedding resolve failed — "
            "continuing with existing embedder"
        )

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

        # 1. Parse — pure function, but NOT fast, and NOT off the event loop:
        # measured 2026-08-05 in this image, 33–89 s for a 400-page PDF and
        # 81–103 s for 1000 pages (the spread is host load, not code), all of it
        # occupying arq's poll loop. That is why the worker's liveness key is
        # left on arq's hour-long default TTL — see README.md 〈治理首頁那盞燈〉
        # and tests/test_worker_liveness.py.
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
        image_pks: dict[str, int] = {}
        want_captions, caption_model = _resolve_caption_intent(
            meta.get("caption_enabled"), meta.get("caption_model"),
        )
        caption_stats = _empty_caption_stats(image_count=len(images or {}))
        if images:
            await _update_job(
                pool, arq_job_id,
                progress_pct=22,
                progress_message=f"captioning {len(images)} image(s)",
            )
            # LOW, cost-only: reuse a persisted caption only when
            # (model, generator id) still match. The id is the listed
            # blob in ``_caption_generator_id_compute``, not "any
            # change that would change output". Compute failure →
            # empty id → no reuse. Empty load → caption everything.
            reused_captions: dict[str, dict[str, Any]] = {}
            try:
                reused_captions = await _load_existing_captions(
                    pool, collection_id, document_id, list(images.keys()),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Existing-caption load failed for doc %s: %s — "
                    "finishing with a full re-describe.",
                    document_id, type(e).__name__,
                )
                reused_captions = {}
            text, caption_stats = await _caption_images_into(
                text, images,
                model=caption_model,
                enabled=want_captions,
                reused_captions=reused_captions,
            )
            # 1b. Persist captioned images to disk + DB so Studio can
            # vector-search over them (Phase 5). Best-effort: a failure
            # here doesn't fail ingest — the captions are already inlined
            # into `text` from step 1a, so retrieval over chunks still
            # works. Only the image-as-image use case is degraded.
            try:
                image_pks = await _persist_images(
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
                image_pks = {}

        # 2. Chunk — bounded by document size. Also synchronous and also on the
        # poll loop, but two orders of magnitude cheaper than the parse above
        # (measured 0.03–0.6 s; ``SemanticChunker.split_segments`` below adds
        # ~0.12 s on a 934k-character document). "Fast" only relative to parsing.
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
        if image_pks:
            chunks = _attach_image_pks_to_chunks(chunks, images, image_pks)
        if not chunks:
            # A document with zero chunks has nothing in the vector index
            # and can never be retrieved, so reporting it as 'indexed' told
            # the user the one thing that isn't true: the sidebar counted it
            # under 「已索引」 and chat counted it as a source, while it
            # contributed nothing to any answer.
            #
            # 'failed' is the right value by this file's own conventions,
            # not a new one invented here:
            #   * the status vocabulary is closed and rendered by a label
            #     map elsewhere; an eighth value would render unlabelled.
            #   * this branch already declines to call
            #     ``_bump_collection_counters`` (the success path below does)
            #     — the code already refuses to count this document, only
            #     the status string disagreed.
            #   * ``error_message`` exists to carry the user-facing reason
            #     when status='failed'; this branch was passing None, i.e.
            #     explicitly claiming there was nothing to report.
            #
            # Two things were silent here, not one. There was no log call
            # at all on this path — the only trace was the string "no
            # chunks produced" inside the returned dict, which lands in
            # the arq job result and nowhere an operator looks. And the
            # ``return`` skipped the job's terminal-state update, so the
            # ingestion_jobs row stayed at status='running', progress 30.
            # The SSE progress stream only closes on a terminal status
            # and otherwise heartbeats to a 30-minute cap
            # (services/csp/app/api/ingestion/jobs.py), so the uploader
            # watched a spinner for half an hour and learned nothing.
            # Settling the document row without settling the job row
            # would leave the document saying 'failed' while the job
            # still said 'running'.
            #
            # We do NOT raise: the pipeline itself did not malfunction, and
            # raising would hand the job to Arq's retry policy to re-run a
            # parse that will produce zero chunks again every time.
            empty = ChunkError(
                code="E_CHUNK_EMPTY",
                retryable=False,
                severity="warning",
                user_message=(
                    "這份文件沒有解析出任何可索引的文字內容，無法被檢索到。"
                    "常見原因是純圖片的掃描檔或空白檔；"
                    "請改用文字可選取的版本重新上傳。"
                ),
                details={"document_id": document_id},
            )
            logger.warning(
                "doc %s produced 0 chunks — recording %s instead of "
                "'indexed'; a document indexed with nothing in it is "
                "indistinguishable from a working one.",
                document_id, empty.code,
            )
            await _update_document_status(
                pool, document_id, "failed",
                chunk_count=0,
                error_message=empty.user_message,
            )
            await _record_job_failure(pool, arq_job_id, empty)
            return {"chunk_count": 0, "error_code": empty.code}

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
                embedding_source_model=embedder.model_name,
                embedding_native_dim=embedder.native_dim,
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

        # 6. Cross-document relations (best-effort — design v2 §5/§6). The
        #    parsed text only exists here, so we extract citations + deposit
        #    rule edges + reconcile the collection now. A failure must NOT fail
        #    ingest: the chunks are already indexed and relations are an
        #    additive retrieval aid, not a correctness requirement.
        relation_failures = 0
        try:
            from ingestion_worker.relations import extract_and_resolve

            rel = await extract_and_resolve(
                pool,
                collection_id=collection_id,
                document_id=document_id,
                text=text,
                run_id=(arq_job_id or f"ingest-{document_id}")[:40],
            )
            if rel["extracted"]:
                logger.info(
                    "doc %s: %d citation edge(s) extracted, %d resolved",
                    document_id, rel["extracted"], rel["resolved"],
                )
        except Exception as e:  # noqa: BLE001
            relation_failures += 1
            logger.warning(
                "Relation extraction failed for doc %s: %s — chunks are "
                "indexed; cross-document links will be missing for this doc.",
                document_id, e,
            )

        # 6b. LLM relation extraction (document-relations Phase 2 / B). Reads
        #     the text + sibling doc list and writes source='llm' edges that
        #     point directly at a dst_document_id. Also best-effort + gated;
        #     coexists with the regex (rule) edges.
        try:
            from ingestion_worker.settings import settings as _settings
            from ingestion_worker.llm_relations import extract_and_resolve_llm

            llm_rel = await extract_and_resolve_llm(
                pool,
                collection_id=collection_id,
                document_id=document_id,
                text=text,
                run_id=(arq_job_id or f"ingest-{document_id}")[:40],
                settings=_settings,
            )
            if llm_rel["extracted"]:
                logger.info(
                    "doc %s: %d LLM relation edge(s) extracted",
                    document_id, llm_rel["extracted"],
                )
        except Exception as e:  # noqa: BLE001
            relation_failures += 1
            logger.warning(
                "LLM relation extraction failed for doc %s: %s — chunks are "
                "indexed; rule edges (if any) are unaffected.",
                document_id, e,
            )

        # 6c. Topic-similarity edges (document-relations / C). Pure-vector
        #     relations recomputed collection-wide (a new doc shifts everyone's
        #     nearest neighbours). Best-effort + gated.
        try:
            from ingestion_worker.settings import settings as _settings
            from ingestion_worker.similarity_relations import recompute_similarity_edges

            sim = await recompute_similarity_edges(
                pool,
                collection_id=collection_id,
                run_id=(arq_job_id or f"ingest-{document_id}")[:40],
                settings=_settings,
            )
            if sim["edges"]:
                logger.info(
                    "collection %s: %d similarity edge(s) (after doc %s)",
                    collection_id, sim["edges"], document_id,
                )
        except Exception as e:  # noqa: BLE001
            relation_failures += 1
            logger.warning(
                "Similarity edge recompute failed for collection %s: %s",
                collection_id, e,
            )

        progress_message = (
            f"{len(leaves)} leaves + {len(parents)} parents indexed · "
            f"{format_caption_progress(caption_stats)}"
        )
        if relation_failures:
            progress_message += f" · {relation_failures} 條關聯抽取失敗"
            await _update_job(
                pool, arq_job_id, progress_pct=95,
                progress_message=progress_message,
            )
        await _update_job(
            pool, arq_job_id, status="succeeded", succeeded=True,
            progress_pct=100,
            progress_message=progress_message,
        )

        return {
            "chunk_count": total_chunks,
            "leaf_count": len(leaves),
            "parent_count": len(parents),
            "elapsed_seconds": (
                datetime.now(timezone.utc) - started_at
            ).total_seconds(),
            "image_count": caption_stats["image_count"],
            "caption_succeeded": caption_stats["succeeded"],
            "caption_attempted": caption_stats["attempted"],
            "caption_summary": format_caption_progress(caption_stats),
        }

    except IngestionError as err:
        # Persist the structured failure for the dev UI / inspector.
        # ⚠ MEDIUM-B(R9, 升級):ingestion_jobs 只存 code + user_message,details 的
        # 具體線索(設定名/端點/上游)沒有欄位可去——worker 是**主要路徑**,不是
        # 附件/預覽那兩條支線。一人維運的氣隙環境裡,docker logs ingestion-worker
        # 是唯一「真的跑得動」的那條,所以把 details 記 log 與另兩個接縫對齊;
        # 不加 ingestion_jobs.extra 欄位(那是 migration,本包沒有,加一欄要重對
        # 版本號——不值一個 log 字串的代價)。
        logger.error(
            "ingest_document failed: %s code=%s details=%s",
            err.user_message, getattr(err, "code", ""), getattr(err, "details", {}),
        )
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


async def reresolve_collection_relations(
    ctx: dict[str, Any], collection_id: int
) -> dict[str, Any]:
    """Re-extract + reconcile cross-document relations for a whole collection
    (document-relations §8 ``:reresolve``).

    Re-parses every indexed document's blob, re-extracts rule citation edges
    (delete-then-insert per doc, ``manual`` untouched) and reconciles dst
    resolution. The API has already run the synchronous reconcile; this is the
    asynchronous '重抽' half. A per-document parse failure is skipped (its old
    rule edges simply remain) — the whole job never fails for one bad blob.
    """
    pool: PgPool = ctx["pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, filename, mime_type, storage_path "
            "FROM ingestion_documents WHERE collection_id = $1 AND status = 'indexed'",
            collection_id,
        )

    docs: list[tuple[int, str]] = []
    for r in rows:
        sp = r["storage_path"]
        if not sp or not os.path.exists(sp):
            continue
        try:
            with open(sp, "rb") as f:
                blob = f.read()
            text, _meta, _images = extract_text(r["filename"], blob, r["mime_type"])
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "reresolve: parse failed for doc %s (%s) — keeping its old "
                "rule edges: %s", r["id"], r["filename"], e,
            )
            continue
        docs.append((r["id"], text))

    from ingestion_worker.relations import reresolve_collection_edges

    result = await reresolve_collection_edges(
        pool,
        collection_id=collection_id,
        docs=docs,
        run_id=f"reresolve-{collection_id}"[:40],
    )

    # Refresh LLM (source='llm') edges per document too — gated; best-effort
    # per doc so one failure doesn't abort the whole reresolve.
    from ingestion_worker.settings import settings as _settings
    from ingestion_worker.llm_relations import extract_and_resolve_llm

    llm_extracted = 0
    for doc_id, doc_text in docs:
        try:
            r = await extract_and_resolve_llm(
                pool,
                collection_id=collection_id,
                document_id=doc_id,
                text=doc_text,
                run_id=f"reresolve-{collection_id}"[:40],
                settings=_settings,
            )
            llm_extracted += r["extracted"]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "reresolve: LLM extraction failed for doc %s: %s", doc_id, e
            )

    # Recompute topic-similarity edges once for the whole collection.
    sim_edges = 0
    try:
        from ingestion_worker.similarity_relations import recompute_similarity_edges

        sim = await recompute_similarity_edges(
            pool,
            collection_id=collection_id,
            run_id=f"reresolve-{collection_id}"[:40],
            settings=_settings,
        )
        sim_edges = sim["edges"]
    except Exception as e:  # noqa: BLE001
        logger.warning("reresolve: similarity recompute failed: %s", e)

    logger.info(
        "reresolve collection %s: %d docs, %d rule edges, %d resolved, %d llm, %d similarity",
        collection_id, len(docs), result["extracted"], result["resolved"],
        llm_extracted, sim_edges,
    )
    return {
        "collection_id": collection_id,
        "documents": len(docs),
        "llm_extracted": llm_extracted,
        "similarity_edges": sim_edges,
        **result,
    }

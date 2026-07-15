"""Arq job handlers.

Currently one handler: ``ingest_document``. The handler is the integration
point where the pieces come together — parser, chunker registry,
embedder, and the agent-scoped store. Each piece raises
``IngestionError`` subclasses; the handler catches and persists the
structured failure into ``ingestion_jobs`` so the dev UI can render a
useful message.

Concurrency note: this handler is async and runs in Arq's shared event loop.
Network calls are awaited and blocking filesystem/parser work is delegated to
``document_io.read_and_extract`` so heartbeats, cancellations and unrelated
jobs continue to make progress.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import errno
import logging
import os
import re as _re
import stat
import uuid
from datetime import datetime, timezone
from typing import Any
from anila_security import verify_queue_proof

import asyncpg

from anila_core.contracts import Classification
from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import IngestionError, StoreError
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from ingestion_worker.document_io import DocumentParseTimeout, read_and_extract
from ingestion_worker.embedder import Embedder
from ingestion_worker import job_state
from ingestion_worker.settings import settings


logger = logging.getLogger(__name__)
_ACTIVE_JOB: ContextVar[tuple[int, int, str] | None] = ContextVar(
    "active_ingestion_job", default=None
)


def _effective_chunk_classification(meta: dict[str, Any]) -> Classification:
    """Return max(document, collection) using the canonical five-level type.

    Both values are required DB state. Missing, NULL, non-string, or unknown
    values abort ingestion before source content is embedded or persisted.
    """
    parsed: list[Classification] = []
    for field in (
        "document_classification_level",
        "collection_classification_level",
    ):
        raw = meta.get(field)
        if not isinstance(raw, str):
            raise StoreError(
                code="E_CLASSIFICATION_INVALID",
                retryable=False,
                severity="critical",
                user_message="文件或知識庫的分類資料無效，已拒絕入庫。",
                details={"field": field, "value_type": type(raw).__name__},
            )
        try:
            parsed.append(Classification.from_storage(raw))
        except ValueError as exc:
            raise StoreError(
                code="E_CLASSIFICATION_INVALID",
                retryable=False,
                severity="critical",
                user_message="文件或知識庫的分類資料無效，已拒絕入庫。",
                details={"field": field, "value": raw},
            ) from exc
    return Classification.max_of(parsed)


def _require_embedding_contract(meta: dict[str, Any]) -> tuple[str, str, int]:
    """Fail before parsing when worker and collection embedding identity drift.

    A model name is not a weight identity.  The fingerprint must be an
    explicitly configured SHA-256 value on both sides; it is never derived
    from the model label.
    """
    collection_model = meta.get("embedding_model")
    collection_fingerprint = meta.get("embedding_fingerprint")
    collection_dim = meta.get("embedding_dim")
    worker_fingerprint = settings.embedding_model_fingerprint
    if (
        not isinstance(collection_model, str)
        or not collection_model
        or not isinstance(collection_fingerprint, str)
        or _re.fullmatch(r"sha256:[0-9a-f]{64}", collection_fingerprint) is None
        or not isinstance(collection_dim, int)
        or collection_dim <= 0
        or _re.fullmatch(r"sha256:[0-9a-f]{64}", worker_fingerprint) is None
    ):
        raise StoreError(
            code="E_EMBEDDING_CONTRACT_INVALID",
            retryable=False,
            severity="critical",
            user_message="嵌入模型權重契約缺失或格式無效，已拒絕入庫。",
        )
    expected = (collection_model, collection_fingerprint, collection_dim)
    supplied = (
        settings.embedding_model,
        worker_fingerprint,
        settings.embedding_dim,
    )
    if supplied != expected:
        raise StoreError(
            code="E_EMBEDDING_CONTRACT_MISMATCH",
            retryable=False,
            severity="critical",
            user_message="worker 嵌入模型、權重指紋或維度與知識庫契約不一致。",
            details={
                "expected_dim": collection_dim,
                "supplied_dim": settings.embedding_dim,
            },
        )
    return expected


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


def _image_storage_error(operation: str, exc: BaseException) -> StoreError:
    """Build a safe, retryable error for image filesystem failures.

    ``str(exc)`` is deliberately not copied into the structured error: an
    ``OSError`` commonly contains the absolute upload path, which is an
    implementation detail and may expose deployment layout to callers.  The
    errno name is useful for operations (ENOSPC/EROFS/EIO/EDQUOT, etc.) and
    does not contain user content or a path.
    """
    raw_errno = getattr(exc, "errno", None)
    errno_name = errno.errorcode.get(raw_errno) if isinstance(raw_errno, int) else None
    details: dict[str, Any] = {"operation": operation}
    if errno_name:
        details["errno"] = errno_name
    return StoreError(
        code="E_IMAGE_STORAGE_UNAVAILABLE",
        retryable=True,
        severity="error",
        user_message="圖片儲存空間暫時無法使用，系統將自動重試。",
        details=details,
    )


def _image_persistence_error(
    operation: str,
    exc: BaseException,
    *,
    rows_attempted: int = 0,
    rows_inserted: int = 0,
) -> StoreError:
    """Build a safe, retryable error for image DB/persistence failures."""
    return StoreError(
        code="E_IMAGE_PERSISTENCE_FAILED",
        retryable=True,
        severity="error",
        user_message="圖片資料寫入失敗，系統將自動重試。",
        details={
            "operation": operation,
            "cause": type(exc).__name__,
            "rows_attempted": rows_attempted,
            "rows_inserted": rows_inserted,
        },
    )


def _image_reconciliation_error(reason: str) -> StoreError:
    """Build a non-retryable error for an unsafe image residue state.

    Residue reconciliation is deliberately fail-closed.  An ambiguous
    backup/temp set cannot be safely guessed at by a retry, so preserve every
    copy and require operator intervention instead of misclassifying it as a
    transient disk error.
    """
    return StoreError(
        code="E_IMAGE_RECONCILIATION_FAILED",
        retryable=False,
        severity="critical",
        user_message="圖片儲存狀態無法安全收斂，已停止處理。",
        details={"operation": "reconcile", "reason": reason},
    )


def _is_rls_violation(exc: BaseException) -> bool:
    """Return whether an asyncpg failure is a PostgreSQL RLS denial."""
    return isinstance(exc, asyncpg.exceptions.InsufficientPrivilegeError) or (
        getattr(exc, "sqlstate", None) == "42501"
    )


def _image_rls_error(*, rows_attempted: int, rows_inserted: int = 0) -> StoreError:
    """Build the critical, non-retryable image RLS error.

    Keep SQL text, exception messages, and filesystem paths out of both the
    user-facing message and structured details.  The exception remains the
    chained cause for server-side logs only.
    """
    return StoreError.rls_violation(
        user_message="圖片資料寫入違反資料隔離政策，已停止處理。",
        details={
            "operation": "db_insert",
            "reason": "rls_violation",
            "rows_attempted": rows_attempted,
            "rows_inserted": rows_inserted,
        },
    )


def _cleanup_image_files(paths: list[str]) -> None:
    """Best-effort cleanup for files written by the current persistence run.

    Cleanup is intentionally non-throwing.  The original storage/DB error is
    the actionable failure and must remain the exception observed by the job
    retry path even if an unlink also fails.
    """
    for path in dict.fromkeys(paths):
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Image persistence cleanup failed (%s)", type(exc).__name__,
            )


def _cleanup_stale_image_temps(images_root: str) -> None:
    """Remove hard-crash temp residue for one document before a retry.

    A worker job lease serialises persistence attempts for the same document,
    so direct children matching ``*.tmp-*`` are safe to reconcile here.  Do
    not follow symlinks or recurse: a symlink/nested directory is an unsafe
    state and must remain untouched while the job fails closed.
    """
    try:
        root_mode = os.lstat(images_root).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _image_storage_error("reconcile", exc) from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise _image_reconciliation_error("unsafe_image_directory")

    try:
        with os.scandir(images_root) as entries:
            temp_paths = [
                entry.path
                for entry in entries
                if ".tmp-" in entry.name
            ]
    except OSError as exc:
        raise _image_storage_error("reconcile", exc) from exc

    for temp_path in temp_paths:
        try:
            mode = os.lstat(temp_path).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _image_storage_error("reconcile", exc) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise _image_reconciliation_error("unsafe_temp_residue")
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _image_storage_error("reconcile", exc) from exc


def _reconcile_stale_image_backups(final_path: str) -> None:
    """Reconcile backups left by an interrupted/failed publish attempt.

    A present final is the authoritative current image, so stale backups for
    it can be removed. If the final is absent, exactly one backup is the only
    recoverable copy and is restored; ambiguity is fail-closed and leaves all
    copies untouched for operator/retry handling.
    """
    parent = os.path.dirname(final_path) or "."
    prefix = f"{os.path.basename(final_path)}.bak-"

    # A backup can disappear between ``scandir`` and the operation below
    # (for example, an operator may be cleaning up a failed publish).  Retry
    # one fresh directory scan rather than treating that benign race as an
    # internal error.  Repeated disappearance is not safe to guess through:
    # preserve the remaining residue and fail closed with a stable reason.
    for attempt in range(2):
        try:
            with os.scandir(parent) as entries:
                backup_paths = [
                    entry.path
                    for entry in entries
                    if entry.name.startswith(prefix)
                ]
        except FileNotFoundError:
            # The document directory itself was removed during reconciliation;
            # there are no backups left for this publish attempt.
            return
        except OSError as exc:
            raise _image_storage_error("reconcile", exc) from exc

        if not backup_paths:
            # Even without stale backups, never let publish move a directory,
            # symlink, device, or other special residue into a backup slot.
            # A regular final (or a missing final) is the only safe state.
            try:
                final_mode = os.lstat(final_path).st_mode
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _image_storage_error("reconcile", exc) from exc
            if not stat.S_ISREG(final_mode):
                raise _image_reconciliation_error("unsafe_final")
            return

        validated_paths: list[str] = []
        missing_backup = False
        for backup_path in backup_paths:
            try:
                mode = os.lstat(backup_path).st_mode
            except FileNotFoundError:
                missing_backup = True
                break
            except OSError as exc:
                raise _image_storage_error("reconcile", exc) from exc
            # Never follow a backup symlink, recurse into a nested directory,
            # or replace a final image from a device/FIFO/socket.  These are
            # operator-controlled residue states and are not retryable disk
            # failures.
            if not stat.S_ISREG(mode):
                raise _image_reconciliation_error("unsafe_backup")
            validated_paths.append(backup_path)

        if missing_backup:
            if attempt == 0:
                continue
            raise _image_reconciliation_error("backup_race")

        try:
            final_mode = os.lstat(final_path).st_mode
        except FileNotFoundError:
            final_exists = False
        except OSError as exc:
            raise _image_storage_error("reconcile", exc) from exc
        else:
            # ``lstat`` deliberately treats a symlink as an existing final;
            # it is unsafe to remove backups or publish through any final
            # residue that is not an ordinary file.
            if not stat.S_ISREG(final_mode):
                raise _image_reconciliation_error("unsafe_final")
            final_exists = True

        if final_exists:
            try:
                for backup_path in validated_paths:
                    os.unlink(backup_path)
            except FileNotFoundError:
                if attempt == 0:
                    continue
                raise _image_reconciliation_error("backup_race")
            except OSError as exc:
                raise _image_storage_error("reconcile", exc) from exc
            return

        if len(validated_paths) != 1:
            raise _image_reconciliation_error("ambiguous_backups")
        try:
            os.replace(validated_paths[0], final_path)
        except FileNotFoundError:
            if attempt == 0:
                continue
            raise _image_reconciliation_error("backup_race")
        except OSError as exc:
            raise _image_storage_error("reconcile", exc) from exc
        return

    # The loop either returns or raises on the second attempt.  Keep an
    # explicit fail-closed guard so future edits cannot accidentally turn a
    # persistent race into a silent success.
    raise _image_reconciliation_error("backup_race")


def _publish_image_files(staged_paths: list[tuple[str, str]]) -> None:
    """Atomically publish staged files while preserving previous versions.

    The database transaction is committed before this function runs, so an
    existing final file must never be truncated while DB/RLS failure is still
    possible.  Each replacement is backed up until all replacements succeed;
    a later publish failure restores the prior final files best-effort.
    """
    backups: list[tuple[str, str | None]] = []
    try:
        for temp_path, final_path in staged_paths:
            _reconcile_stale_image_backups(final_path)
            backup_path = f"{final_path}.bak-{uuid.uuid4().hex}"
            try:
                os.replace(final_path, backup_path)
            except FileNotFoundError:
                backup_path = None
            backups.append((final_path, backup_path))
            os.replace(temp_path, final_path)
    except Exception:
        # Roll back both ordinary storage errors and unexpected replacement
        # failures, but re-raise the original exception unchanged. The caller
        # only classifies OSError as retryable storage; programming errors
        # still converge through the worker's E_INTERNAL wrapper.
        for final_path, backup_path in reversed(backups):
            try:
                if backup_path is None:
                    os.unlink(final_path)
                else:
                    os.replace(backup_path, final_path)
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Image publish rollback failed (%s)", type(exc).__name__,
                )
        raise

    # The final file and DB row are already consistent, so do not roll them
    # back when backup cleanup fails. However, an orphaned classified-image
    # copy is still a persistence failure: surface the first cleanup error so
    # the job enters the retry path and a later attempt can remove the backup.
    cleanup_error: BaseException | None = None
    for _final_path, backup_path in backups:
        if backup_path is None:
            continue
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Image publish backup cleanup failed (%s)", type(exc).__name__,
            )
            if cleanup_error is None:
                cleanup_error = exc
    if cleanup_error is not None:
        raise cleanup_error


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
         caption embedding for vector search. ``ON CONFLICT DO UPDATE``
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

    # Filter before touching the filesystem. Empty refs and near-uniform PDF
    # background fills are intentionally legal skips; they must not fail a
    # document merely because the upload volume is unavailable.
    candidates: list[dict[str, Any]] = []
    for img_id, ref in images.items():
        mime = getattr(ref, "mime", None) or "image/png"
        ext = ".png" if "png" in mime else (".jpg" if "jpeg" in mime else ".bin")
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
        # Defensive filter: drop uniform-color images even when captioning is
        # disabled/bypassed. These are PDF background rectangles with no
        # informational value and are a documented legal skip path.
        if _is_uniform_color(image_bytes):
            logger.info(
                "Skipping persist for uniform-color image %s (doc %s)",
                img_id, document_id,
            )
            continue
        candidates.append(
            {
                "ref": ref,
                "image_id": safe_img_id,
                "rel_path": rel_path,
                "abs_path": abs_path,
                "mime": mime,
                "image_bytes": image_bytes,
            }
        )

    if not candidates:
        return 0

    images_root = os.path.join(upload_dir, "anila-images", str(document_id))
    try:
        os.makedirs(images_root, mode=0o755, exist_ok=True)
    except OSError as exc:
        raise _image_storage_error("mkdir", exc) from exc
    # A hard crash can leave a UUID-suffixed temp file behind before publish
    # gets a chance to run.  The ingestion job lease serialises retries for a
    # document, so reconcile that document directory before staging new bytes.
    _cleanup_stale_image_temps(images_root)

    # Build the to-be-inserted rows AND collect captions for batch embed.
    rows: list[dict[str, Any]] = []
    captions_to_embed: list[str] = []
    written_paths: list[str] = []
    staged_paths: list[tuple[str, str]] = []
    for candidate in candidates:
        ref = candidate["ref"]
        abs_path = candidate["abs_path"]
        temp_path = f"{abs_path}.tmp-{uuid.uuid4().hex}"
        written_paths.append(temp_path)
        try:
            with open(temp_path, "wb") as f:
                written = f.write(candidate["image_bytes"])
                if written is not None and written != len(candidate["image_bytes"]):
                    raise OSError(errno.ENOSPC, "short image write")
        except asyncio.CancelledError:
            _cleanup_image_files(written_paths)
            raise
        except OSError as exc:
            _cleanup_image_files(written_paths)
            raise _image_storage_error("write", exc) from exc
        except Exception:
            _cleanup_image_files(written_paths)
            raise

        try:
            os.chmod(temp_path, 0o644)
        except asyncio.CancelledError:
            _cleanup_image_files(written_paths)
            raise
        except OSError as exc:
            _cleanup_image_files(written_paths)
            raise _image_storage_error("chmod", exc) from exc
        except Exception:
            _cleanup_image_files(written_paths)
            raise

        staged_paths.append((temp_path, abs_path))

        caption = getattr(ref, "caption", "") or ""
        page = getattr(ref, "page", None)
        alt_text = getattr(ref, "alt_text", "") or None
        rows.append(
            {
                "image_id": candidate["image_id"],
                "page": page,
                "storage_path": candidate["rel_path"],
                "mime": candidate["mime"],
                "alt_text": alt_text,
                "caption": caption,
                "bytes_size": len(candidate["image_bytes"]),
            }
        )
        captions_to_embed.append(caption or alt_text or "image")

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
    inserted = 0
    try:
        from pgvector import HalfVector

        # Outer transaction so SET LOCAL is scoped to this pooled connection.
        # ingestion_images is FORCE-RLS (migration 0037), therefore every row
        # is inserted under the collection GUC and any DB/RLS failure aborts
        # the whole image persistence operation instead of being skipped.
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            for i, row in enumerate(rows):
                emb = embeddings[i] if embeddings is not None else None
                emb_value = HalfVector(emb) if emb is not None else None
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
    except asyncio.CancelledError:
        _cleanup_image_files(written_paths)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup_image_files(written_paths)
        if _is_rls_violation(exc):
            raise _image_rls_error(
                rows_attempted=len(rows),
                rows_inserted=0,
            ) from exc
        raise _image_persistence_error(
            "db_insert",
            exc,
            rows_attempted=len(rows),
            # The surrounding transaction rolls back all prior rows when any
            # insert fails; do not report pre-rollback progress as committed.
            rows_inserted=0,
        ) from exc

    try:
        _publish_image_files(staged_paths)
    except asyncio.CancelledError:
        _cleanup_image_files(written_paths)
        raise
    except OSError as exc:
        _cleanup_image_files(written_paths)
        raise _image_storage_error("publish", exc) from exc
    except Exception:
        _cleanup_image_files(written_paths)
        raise
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
        img_bytes = getattr(ref, "image_bytes", b"") or b""
        size = len(img_bytes)
        if size > settings.vision_max_image_bytes:
            logger.warning(
                "Image %s skipped: %d bytes > limit %d",
                image_id, size, settings.vision_max_image_bytes,
            )
            return image_id, ""
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
               d.classification_level AS document_classification_level,
               c.classification_level AS collection_classification_level,
               c.chunking_config AS chunking_config,
               c.embedding_model AS embedding_model,
               c.embedding_fingerprint AS embedding_fingerprint,
               c.embedding_dim AS embedding_dim,
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
    stage = "complete" if status == "indexed" else status
    sql = """
        UPDATE ingestion_documents
           SET processing_stage = $2::text,
               status = CASE
                   WHEN $2::text = 'complete' THEN 'indexed'
                   WHEN active_generation_id IS NULL THEN $3::text
                   ELSE 'indexed'
               END,
               chunk_count = COALESCE($4, chunk_count),
               error_message = $5,
               indexed_at = CASE WHEN $2::text = 'complete' THEN now() ELSE indexed_at END
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            document_id,
            stage,
            status,
            chunk_count,
            error_message,
        )


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


async def _reconcile_collection_counters(
    pool: PgPool, collection_id: int
) -> None:
    """Delegate exact replacement counters to the canonical chunk store."""

    store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
    await store.reconcile_collection_counters()


async def _record_job_failure(
    pool: PgPool, arq_job_id: str | None, err: IngestionError
) -> None:
    """Durably fail/retry the matching job; terminal write errors fail loud."""
    active = _ACTIVE_JOB.get()
    if active is not None:
        job_id, document_id, lease_token = active
        transition = await job_state.fail_or_retry(
            pool,
            job_id=job_id,
            document_id=document_id,
            lease_token=lease_token,
            error_code=err.code,
            error_message=err.user_message or err.code,
            retryable=bool(err.retryable),
            backoff_seconds=settings.job_retry_backoff_seconds,
        )
        if transition is None:
            raise job_state.LeaseLostError(
                "failure transition rejected because the lease is no longer owned"
            )
        return
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
    async with pool.acquire() as conn:
        result = await conn.execute(sql, arq_job_id, err.code, err.user_message)
    if result != "UPDATE 1":
        raise RuntimeError("terminal ingestion job failure update matched no row")


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
    active = _ACTIVE_JOB.get()
    if active is not None:
        job_id, _document_id, lease_token = active
        if succeeded or status == "succeeded":
            updated = await job_state.succeed(
                pool,
                job_id=job_id,
                lease_token=lease_token,
                message=progress_message or "ingestion completed",
            )
        else:
            updated = await job_state.progress(
                pool,
                job_id=job_id,
                lease_token=lease_token,
                progress_pct=progress_pct,
                progress_message=progress_message,
            )
        if not updated:
            raise job_state.LeaseLostError("lease-fenced job update was rejected")
        return
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
            result = await conn.execute(sql, *args)
    except Exception:
        if succeeded or status in {"succeeded", "failed", "cancelled", "dead_letter"}:
            raise
        logger.warning("Non-terminal ingestion progress update failed", exc_info=True)
        return
    if (
        result != "UPDATE 1"
        and (succeeded or status in {"succeeded", "failed", "cancelled", "dead_letter"})
    ):
        raise RuntimeError("terminal ingestion job update matched no row")


# ── Handler ─────────────────────────────────────────────────────────────────


async def _load_ingestion_authority_user(
    pool: PgPool,
    *,
    ingestion_job_id: int | None,
    fallback_user_id: int | None,
) -> int:
    """Return the user whose live clearance authorizes ingestion work.

    Durable jobs are authorized by the user recorded when the CSP enqueued the
    job. Direct/test invocations have no job row and therefore use the
    document uploader/collection owner. Missing or malformed authority is a
    security boundary failure, never an anonymous/system fallback.
    """
    authority_user_id = fallback_user_id
    if ingestion_job_id is not None:
        async with pool.acquire() as conn:
            authority_user_id = await conn.fetchval(
                "SELECT enqueued_by FROM ingestion_jobs WHERE id=$1",
                ingestion_job_id,
            )
    if (
        isinstance(authority_user_id, bool)
        or not isinstance(authority_user_id, int)
        or authority_user_id <= 0
    ):
        raise StoreError(
            code="E_INTERNAL",
            retryable=False,
            severity="error",
            user_message="Ingestion authority is missing or invalid.",
            details={"ingestion_job_id": ingestion_job_id},
        )
    return authority_user_id


async def ingest_document(
    ctx: dict[str, Any],
    document_id: int,
    ingestion_job_id: int | None = None,
    attempt_number: int = 1,
    queue_proof: str | None = None,
) -> dict[str, Any]:
    """Parse → chunk → embed → index one document.

    ``ctx`` is Arq's per-call context; the worker config injects the
    shared ``pool`` and ``embedder`` into ctx during ``startup``.
    Returns a small summary dict so the job result row carries
    "11 chunks indexed in 4.2s" without re-querying the DB.
    """
    pool: PgPool = ctx["pool"]
    embedder: Embedder = ctx["embedder"]
    if settings.ingestion_queue_hmac_key:
        verify_queue_proof(
            settings.ingestion_queue_hmac_key,
            task_name="ingest_document",
            payload={
                "document_id": document_id,
                "ingestion_job_id": ingestion_job_id,
                "attempt_number": attempt_number,
            },
            proof=queue_proof,
        )
    arq_job_id: str | None = ctx.get("job_id")
    heartbeat_task: asyncio.Task[None] | None = None
    active_token = None
    if ingestion_job_id is not None:
        lease_token = await job_state.claim_job(
            pool,
            job_id=ingestion_job_id,
            document_id=document_id,
            attempt_number=attempt_number,
            lease_seconds=settings.job_lease_seconds,
        )
        if lease_token is None:
            return {
                "duplicate": True,
                "ingestion_job_id": ingestion_job_id,
                "attempt_number": attempt_number,
            }
        active_token = _ACTIVE_JOB.set(
            (ingestion_job_id, document_id, lease_token)
        )
        heartbeat_task = asyncio.create_task(
            job_state.heartbeat_loop(
                pool,
                job_id=ingestion_job_id,
                lease_token=lease_token,
                lease_seconds=settings.job_lease_seconds,
                interval_seconds=settings.job_heartbeat_seconds,
                owner_task=asyncio.current_task(),
            )
        )

    started_at = datetime.now(timezone.utc)
    await _update_job(pool, arq_job_id, status="running", started=True, progress_pct=5)
    try:
        meta = await _load_document_meta(pool, document_id)
        collection_id = int(meta["collection_id"])
        embedding_model, embedding_fingerprint, embedding_dim = (
            _require_embedding_contract(meta)
        )
        effective_classification = _effective_chunk_classification(meta)
        storage_path = meta["storage_path"]
        # Bill embedding usage to whoever uploaded the file; fall back
        # to the collection owner when the doc row's uploaded_by is null
        # (could happen for system-seeded docs).
        billing_user_id = (
            meta.get("uploaded_by") or meta.get("owner_user_id")
        )
        authority_user_id = await _load_ingestion_authority_user(
            pool,
            ingestion_job_id=ingestion_job_id,
            fallback_user_id=billing_user_id,
        )

        from ingestion_worker.evaluator import _require_eval_data_clearance

        async def require_current_clearance() -> None:
            await _require_eval_data_clearance(
                pool,
                user_id=authority_user_id,
                collection_id=collection_id,
                document_ids=[document_id],
            )

        await require_current_clearance()
        if not storage_path or not os.path.exists(storage_path):
            raise StoreError(
                code="E_INTERNAL",
                retryable=False,
                severity="error",
                user_message="上傳檔案目前無法使用。",
                details={
                    "reason": "missing_blob",
                    "has_path": bool(storage_path),
                },
            )

        # 1. Parse — pure function, fast.
        await _update_document_status(pool, document_id, "parsing")
        await _update_job(pool, arq_job_id, progress_pct=15, progress_message="parsing")
        try:
            text, parse_meta, images = await read_and_extract(
                storage_path,
                meta["filename"],
                meta["mime_type"],
                timeout_seconds=settings.parse_timeout_seconds,
            )
        except DocumentParseTimeout as exc:
            raise StoreError(
                code="E_PARSE_TIMEOUT",
                retryable=True,
                severity="error",
                user_message="文件解析逾時，系統將自動重試。",
                details={"timeout_s": settings.parse_timeout_seconds},
            ) from exc

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
            await require_current_clearance()
            text = await _caption_images_into(text, images)
            # 1b. Persist captioned images to disk + DB so Studio can
            # vector-search over them (Phase 5). Image bytes are part of the
            # document's durable output, so filesystem/DB failures must fail
            # this job and enter the normal structured retry path. Caption
            # embedding itself remains an explicit best-effort downgrade in
            # _persist_images: rows are retained with a NULL embedding.
            await _persist_images(
                pool, collection_id, document_id, images,
                embedder, billing_user_id,
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
                await require_current_clearance()
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
        store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
        if not chunks:
            # An empty re-index must still remove the previous generation;
            # otherwise stale chunks remain searchable even though the
            # document advertises chunk_count=0.
            if ingestion_job_id is not None:
                await job_state.ensure_lease(
                    pool, job_id=ingestion_job_id, lease_token=lease_token
                )
            if ingestion_job_id is not None:
                try:
                    async with asyncio.timeout(settings.index_timeout_seconds):
                        await store.stage_and_activate_generation(
                            document_id=document_id,
                            source_ingestion_job_id=ingestion_job_id,
                            source_ingestion_lease_token=lease_token,
                            embedding_model=embedding_model,
                            embedding_fingerprint=embedding_fingerprint,
                            embedding_dim=embedding_dim,
                            parent_chunks=[],
                            leaf_chunks=[],
                            embeddings=[],
                            classification_level=effective_classification,
                        )
                except TimeoutError as exc:
                    raise StoreError(
                        code="E_INDEX_TIMEOUT",
                        retryable=True,
                        severity="error",
                        user_message="索引寫入逾時，系統將自動重試。",
                        details={"timeout_s": settings.index_timeout_seconds},
                    ) from exc
            else:
                await store.replace_document_chunks(
                    document_id=document_id,
                    parent_chunks=[],
                    leaf_chunks=[],
                    embeddings=[],
                    classification_level=effective_classification,
                )
            await _update_document_status(
                pool, document_id, "indexed",
                chunk_count=0,
                error_message=None,
            )
            await _reconcile_collection_counters(pool, collection_id)
            await _update_job(
                pool,
                arq_job_id,
                status="succeeded",
                succeeded=True,
                progress_pct=100,
                progress_message="0 chunks indexed",
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
        await require_current_clearance()
        embeddings = await embedder.embed(
            [c.content for c in leaves], user_id=billing_user_id,
        ) if leaves else []

        # 4. Index — replace the complete parent/leaf generation in one
        #    transaction.  A failed leaf write restores the previous rows;
        #    retries never collide with a partial parent generation.
        await _update_job(pool, arq_job_id, progress_pct=85, progress_message="indexing")
        if ingestion_job_id is not None:
            await job_state.ensure_lease(
                pool, job_id=ingestion_job_id, lease_token=lease_token
            )
        if ingestion_job_id is not None:
            try:
                async with asyncio.timeout(settings.index_timeout_seconds):
                    await store.stage_and_activate_generation(
                        document_id=document_id,
                        source_ingestion_job_id=ingestion_job_id,
                        source_ingestion_lease_token=lease_token,
                        embedding_model=embedding_model,
                        embedding_fingerprint=embedding_fingerprint,
                        embedding_dim=embedding_dim,
                        parent_chunks=parents,
                        leaf_chunks=leaves,
                        embeddings=embeddings,
                        classification_level=effective_classification,
                    )
            except TimeoutError as exc:
                raise StoreError(
                    code="E_INDEX_TIMEOUT",
                    retryable=True,
                    severity="error",
                    user_message="索引寫入逾時，系統將自動重試。",
                    details={"timeout_s": settings.index_timeout_seconds},
                ) from exc
        else:
            await store.replace_document_chunks(
                document_id=document_id,
                parent_chunks=parents,
                leaf_chunks=leaves,
                embeddings=embeddings,
                classification_level=effective_classification,
            )

        total_chunks = len(chunks)
        # 5. Status + counters.
        await _update_document_status(
            pool, document_id, "indexed",
            chunk_count=total_chunks,
            error_message=None,
        )
        await _reconcile_collection_counters(pool, collection_id)

        # 6. Cross-document relations (best-effort — design v2 §5/§6). The
        #    parsed text only exists here, so we extract citations + deposit
        #    rule edges + reconcile the collection now. A failure must NOT fail
        #    ingest: the chunks are already indexed and relations are an
        #    additive retrieval aid, not a correctness requirement.
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

            await require_current_clearance()
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
            logger.warning(
                "LLM relation extraction failed for doc %s: %s — chunks are "
                "indexed; rule edges (if any) are unaffected.",
                document_id, e,
            )

        # 6c. Topic-similarity edges (document-relations / C).  Do not run the
        #     collection-wide O(N^2) query inline.  One durable row per
        #     collection debounces bursts and is lease-replayed after crashes.
        try:
            from ingestion_worker.settings import settings as _settings
            from ingestion_worker.similarity_relations import (
                request_similarity_recompute,
            )

            if _settings.enable_similarity_edges:
                await request_similarity_recompute(
                    pool,
                    collection_id=collection_id,
                    debounce_seconds=_settings.similarity_debounce_seconds,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Similarity edge recompute request failed for collection %s: %s",
                collection_id, e,
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

    except asyncio.CancelledError:
        # Arq job timeouts and worker shutdowns arrive as cancellation, which
        # bypasses ``except Exception`` on modern Python.  Best-effort terminal
        # writes prevent a permanently running document/job, then preserve the
        # cancellation so Arq can finish its own timeout/shutdown handling.
        if _ACTIVE_JOB.get() is None:
            await _update_document_status(
                pool,
                document_id,
                "failed",
                error_message="處理已取消或逾時，可重新處理。",
            )
            await _update_job(
                pool,
                arq_job_id,
                status="cancelled",
                succeeded=True,
                progress_pct=100,
                progress_message="cancelled or timed out",
            )
        else:
            wrapped = StoreError(
                code="E_WORKER_CANCELLED",
                retryable=True,
                severity="error",
                user_message="處理已取消或逾時，系統將自動重試。",
            )
            await _record_job_failure(pool, arq_job_id, wrapped)
        raise
    except IngestionError as err:
        # Persist the structured failure for the dev UI / inspector.
        if _ACTIVE_JOB.get() is None:
            await _update_document_status(
                pool, document_id, "failed",
                error_message=err.user_message or err.code,
            )
        await _record_job_failure(pool, arq_job_id, err)
        # Re-raise so Arq's retry policy sees the failure too.
        raise
    except job_state.LeaseLostError:
        # Another owner (or the reaper) now controls this logical job.  The
        # stale attempt must not mutate either the document or terminal state.
        raise
    except Exception as e:
        # Unknown failure → wrap as E_INTERNAL with bounded leakage.
        retryable = isinstance(
            e,
            (
                TimeoutError,
                ConnectionError,
                asyncpg.PostgresConnectionError,
                asyncpg.CannotConnectNowError,
                asyncpg.TooManyConnectionsError,
            ),
        )
        wrapped = StoreError(
            code="E_INTERNAL",
            retryable=retryable,
            severity="error",
            user_message="內部錯誤，請聯絡管理員。",
            details={"cause": type(e).__name__, "message": str(e)[:200]},
        )
        if _ACTIVE_JOB.get() is None:
            await _update_document_status(
                pool, document_id, "failed",
                error_message=wrapped.user_message,
            )
        await _record_job_failure(pool, arq_job_id, wrapped)
        raise
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if active_token is not None:
            _ACTIVE_JOB.reset(active_token)


async def reresolve_collection_relations(
    ctx: dict[str, Any], collection_id: int,
    actor_user_id: int,
    queue_proof: str | None = None,
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
    if settings.ingestion_queue_hmac_key:
        verify_queue_proof(
            settings.ingestion_queue_hmac_key,
            task_name="reresolve_collection_relations",
            payload={
                "collection_id": collection_id,
                "actor_user_id": actor_user_id,
            },
            proof=queue_proof,
        )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, filename, mime_type, storage_path "
            "FROM ingestion_documents WHERE collection_id = $1 AND status = 'indexed'",
            collection_id,
        )

    docs: list[tuple[int, str]] = []
    document_ids = [int(row["id"]) for row in rows]
    async def require_current_clearance() -> None:
        from ingestion_worker.evaluator import _require_eval_data_clearance

        await _require_eval_data_clearance(
            pool,
            user_id=actor_user_id,
            collection_id=collection_id,
            document_ids=document_ids,
        )
    await require_current_clearance()
    for r in rows:
        sp = r["storage_path"]
        if not sp or not os.path.exists(sp):
            continue
        try:
            text, _meta, _images = await read_and_extract(
                sp,
                r["filename"],
                r["mime_type"],
                timeout_seconds=settings.parse_timeout_seconds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "reresolve: parse failed for doc %s (%s) — keeping its old "
                "rule edges: %s", r["id"], r["filename"], e,
            )
            continue
        docs.append((r["id"], text))

    from ingestion_worker.relations import reresolve_collection_edges

    await require_current_clearance()
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
            await require_current_clearance()
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

    # Schedule one durable topic-similarity recompute for the collection.
    sim_edges = 0
    try:
        from ingestion_worker.similarity_relations import (
            request_similarity_recompute,
        )

        if _settings.enable_similarity_edges:
            await request_similarity_recompute(
                pool,
                collection_id=collection_id,
                debounce_seconds=_settings.similarity_debounce_seconds,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("reresolve: similarity recompute request failed: %s", e)

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

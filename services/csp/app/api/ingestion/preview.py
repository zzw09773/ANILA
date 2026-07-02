"""Chunking preview — interactive dry-run for picking a strategy.

Sprint 8 X / chunking-preview Phase 1.

Why this exists
===============

The previous flow forced users to pick a chunking strategy *before*
they had any way to see what it would actually produce. They created
a collection, picked ``hierarchical`` because it sounded reasonable,
uploaded a doc, and the chunks landed in ``document_chunks`` already
embedded. Comparing strategies meant building two collections and
re-uploading the same doc, by which point the embedding budget had
already been spent twice.

This endpoint flips the order:

    upload one doc  →  preview chunks for every strategy  →  pick one
                                                            →  commit

Behaviour
=========

* Multipart form with a single file, plus an optional list of
  ``strategies`` (default = every chunker that can run without an
  embedder; see ``PREVIEWABLE_STRATEGIES`` below).
* Parses the file *once* via the shared
  ``anila_core.ingestion.parsers.extract_text``, then runs each
  selected chunker against the parsed text in-memory.
* Returns ``{"per_strategy": {<name>: {"chunks": [...], "stats": {...}}}}``.
* **Zero database writes, zero embedding calls, zero queue traffic.**
  Pure CPU; safe for an interactive UX.

Why semantic isn't previewable here
-----------------------------------

``SemanticChunker`` needs per-segment embeddings to do boundary
detection. The dry-run compute budget for one preview is ~50 ms of
CPU; a real embedding pass against the user's NV-Embed endpoint is
seconds and burns budget. We surface the fact that semantic exists
(via ``PREVIEWABLE_STRATEGIES`` returning ``previewable=False`` per
entry) but skip it during the actual chunking. The frontend explains
this to the user; the strategy can still be picked at commit time.

Limits
======

* File size: ``MAX_PREVIEW_BYTES`` (default 10 MB). Larger uploads are
  rejected with 413 because preview is meant to be quick — if you
  have a 50 MB PDF you should ingest properly and use the Evaluator.
* Per-strategy chunk cap: ``MAX_CHUNKS_RETURNED`` (default 200).
  Beyond that we truncate and surface ``truncated_to`` in the stats
  so the UI can warn.
* Auth: same ``Caller`` dependency as the rest of ``/api/ingestion`` —
  any developer-tier user can preview.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from anila_core.ingestion.chunking_plugins import get_chunker, list_chunkers
from anila_core.ingestion.errors import IngestionError
from anila_core.ingestion.parsers import extract_text
from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Ingestion / Preview"])


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_PREVIEW_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_CHUNKS_RETURNED = 200

# Chunkers we can run without an embedder. Semantic excluded
# because its segment-distance pass would burn real embedding
# budget on a UI-driven endpoint.
def _previewable_strategy_names() -> list[str]:
    return [
        spec["name"]
        for spec in list_chunkers()
        if not spec.get("requires_embedder")
    ]


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class StrategySpec(BaseModel):
    """One row in the strategy catalogue.

    Frontend renders this as the picker options + a 'previewable'
    badge so semantic shows up but the user knows it'll only run at
    commit.
    """

    name: str
    display_name: str
    previewable: bool
    requires_embedder: bool
    default_params: dict[str, Any]
    param_schema: Optional[dict[str, Any]] = None


class ChunkPreviewRow(BaseModel):
    """One chunk in a preview result.

    Mirrors the shape of the production ``document_chunks`` row's
    visible-to-frontend subset, minus everything that requires a DB
    write (id / created_at / embedding norm). ``token_count`` is the
    chunker's own count — same metric the worker would persist.
    """

    chunk_key: str
    content: str
    metadata: dict[str, Any]
    token_count: int


class StrategyPreviewResult(BaseModel):
    chunks: list[ChunkPreviewRow]
    stats: dict[str, Any]
    error: Optional[str] = None


class PreviewResponse(BaseModel):
    filename: str
    bytes: int
    parse_metadata: dict[str, Any]
    per_strategy: dict[str, StrategyPreviewResult]
    skipped_strategies: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/ingestion/chunking-preview/strategies",
    response_model=list[StrategySpec],
)
def list_strategies(
    _: User = Depends(get_current_user),
    __: Session = Depends(get_db),
):
    """Catalogue of chunkers + which can be previewed without embeddings.

    Used by the frontend wizard to render the strategy picker. The
    picker itself doesn't call the chunker; this endpoint just lists
    metadata so the UI can render labels, default params, and the
    'previewable' badge.
    """
    out: list[StrategySpec] = []
    for spec in list_chunkers():
        out.append(
            StrategySpec(
                name=spec["name"],
                display_name=spec.get("display_name", spec["name"]),
                previewable=not spec.get("requires_embedder", False),
                requires_embedder=bool(spec.get("requires_embedder", False)),
                default_params=spec.get("default_params", {}) or {},
                param_schema=spec.get("param_schema"),
            )
        )
    return out


@router.post(
    "/api/ingestion/chunking-preview",
    response_model=PreviewResponse,
)
async def preview_chunking(
    file: UploadFile = File(...),
    strategies: Optional[str] = Form(
        default=None,
        description=(
            "Comma-separated strategy names. Empty/omitted = every "
            "previewable strategy (i.e. all except 'semantic')."
        ),
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PreviewResponse:
    """Parse the uploaded file once + run each strategy against the
    parsed text. Returns chunk previews for the UI's side-by-side
    compare view. Stateless — nothing persists.
    """
    blob = await file.read()
    if len(blob) > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"檔案 {len(blob):,} bytes 超過 preview 上限 "
                f"{MAX_PREVIEW_BYTES:,}（{MAX_PREVIEW_BYTES // (1024*1024)} MB）。"
                "請改走正式 ingest 流程或先裁減檔案。"
            ),
        )
    if not blob:
        raise HTTPException(status_code=400, detail="empty upload")

    # Resolve which strategies the caller wants. Default = all
    # previewable. Filter out anything the chunker registry doesn't
    # know about so a typo gets a clear 400 rather than a None on
    # one row.
    previewable = _previewable_strategy_names()
    if strategies:
        wanted = [s.strip() for s in strategies.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in {sp["name"] for sp in list_chunkers()}]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown strategy: {', '.join(unknown)}",
            )
    else:
        wanted = list(previewable)

    skipped: list[str] = []
    runnable: list[str] = []
    for name in wanted:
        if name in previewable:
            runnable.append(name)
        else:
            skipped.append(name)

    # Parse once. extract_text raises ParseError on unsupported / empty
    # / corrupt inputs; surface it as 400 with the user_message.
    try:
        text, parse_meta, _images = extract_text(
            file.filename or "upload.bin",
            blob,
            file.content_type,
        )
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=exc.user_message) from exc

    per_strategy: dict[str, StrategyPreviewResult] = {}
    for name in runnable:
        per_strategy[name] = _run_one_strategy(name, text, parse_meta)

    return PreviewResponse(
        filename=file.filename or "upload.bin",
        bytes=len(blob),
        parse_metadata={
            k: v
            for k, v in parse_meta.items()
            # Drop any list/dict values that bloat the response — the
            # UI only needs a quick summary header.
            if isinstance(v, (str, int, float, bool)) or v is None
        },
        per_strategy=per_strategy,
        skipped_strategies=skipped,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_one_strategy(
    name: str, text: str, parse_meta: dict[str, Any]
) -> StrategyPreviewResult:
    """Run one chunker; return preview rows + summary stats.

    Per-strategy errors don't fail the whole request — we surface
    the message in ``error`` and let other strategies render. This
    matches the UX intent: a 30-byte text file might fail pdf-page
    (no \\f markers) but still chunk fine via fixed/hierarchical.
    """
    try:
        chunker = get_chunker(name)
        # Use the chunker's own default_params to mirror what the
        # worker would do in the no-explicit-params path. Frontend
        # sends params at commit time, not here.
        params = dict(getattr(chunker, "default_params", {}) or {})
        results = chunker.chunk(text, parse_meta, params)
    except Exception as exc:  # noqa: BLE001 — preview is best-effort
        logger.info(
            "chunking-preview: strategy=%s failed for parse_meta=%s: %s",
            name,
            parse_meta.get("format"),
            exc,
        )
        return StrategyPreviewResult(
            chunks=[],
            stats={"chunk_count": 0, "total_tokens": 0, "avg_tokens": 0},
            error=str(exc)[:300],
        )

    truncated = False
    if len(results) > MAX_CHUNKS_RETURNED:
        results = results[:MAX_CHUNKS_RETURNED]
        truncated = True

    rows: list[ChunkPreviewRow] = []
    total_tokens = 0
    for r in results:
        token_count = _count_tokens(r)
        total_tokens += token_count
        rows.append(
            ChunkPreviewRow(
                chunk_key=r.chunk_key,
                content=r.content,
                metadata=dict(r.metadata or {}),
                token_count=token_count,
            )
        )

    stats: dict[str, Any] = {
        "chunk_count": len(rows),
        "total_tokens": total_tokens,
        "avg_tokens": round(total_tokens / len(rows), 1) if rows else 0,
    }
    if truncated:
        stats["truncated_to"] = MAX_CHUNKS_RETURNED
    return StrategyPreviewResult(chunks=rows, stats=stats)


def _count_tokens(chunk_result) -> int:
    """Best-effort token count for a chunker output.

    The chunkers don't return a uniform token field — some put it on
    ``ChunkResult.token_count``, others infer at persistence time. We
    fall back to whitespace-and-CJK-char heuristic so the stats card
    always has a number.
    """
    explicit = getattr(chunk_result, "token_count", None)
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    text = chunk_result.content or ""
    # Cheap approximation: 1 token ~= 1 word OR ~1.5 CJK chars.
    # Worker uses tiktoken; this is preview-only and the frontend
    # labels it as approximate.
    words = len(text.split())
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return max(1, words + cjk // 2)

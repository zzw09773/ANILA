"""File-format → text extraction (Pillar 2 shared infrastructure).

Sprint 8 X / Phase 1 of chunking-preview: this thin wrapper moved from
``ingestion_worker.parsers`` so multiple consumers can share it:

    * ingestion-worker (Arq job pipeline, the original consumer)
    * CSP backend (POST /api/ingestion/chunking-preview, dry-run)
    * any future batch worker that needs to extract text from blobs

The HEAVY parser stack (pymupdf4llm, Docling, OCR, OpenCC, …) still lives
in AgenticRAG's ``agentic_rag.ingestion.parsers.ParserRegistry``. We
keep that arrangement on purpose:

    * AgenticRAG ships the actual file-format-specific parsers because
      that's where docling / OCR / CJK normalisation are evolved.
    * anila-core (this module) provides a stable Pillar-2-shaped
      callable that hides which parser library is in use, plus the
      adapter logic (temp file, page boundary insertion, ImageRef
      passthrough).

To call ``extract_text`` you must have ``agentic-rag[rag]`` installed in
the Python env. CSP and ingestion-worker Dockerfiles both do this.
Standalone scripts that don't have AgenticRAG installed will see a
clear ``ParseError(code='E_INTERNAL')`` rather than a cryptic
``ImportError``.

Supported inputs (delegated to ParserRegistry):

    * ``.txt`` / ``.md``                — pure-stdlib readers.
    * ``.rtf``                          — striprtf (pure Python).
    * ``.pdf``                          — pymupdf4llm (markdown-shaped)
                                          or Docling (layout-aware) when
                                          ``DOC_PARSER=docling`` is set.
    * ``.docx`` / ``.doc`` / ``.odt``   — python-docx / Word XML / odfpy.
    * ``.png`` / ``.jpg`` / ``.jpeg`` /
      ``.webp`` / ``.gif`` / ``.bmp``    — OCR via configured backend.

The ``content`` string preserves any ``[[IMAGE:<id>]]`` placeholders
ParserRegistry inserts; chunkers ignore them as opaque tokens but the
inspector renders them with the corresponding image when available.
"""

from __future__ import annotations

import os
from typing import Any

from anila_core.ingestion.errors import ParseError


def extract_text(
    filename: str, content: bytes, mime_type: str | None = None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(text, metadata, images)`` for one uploaded blob.

    ``metadata`` is the parser's own metadata dict augmented with:
    - ``format``: extension key the registry resolved (e.g. ``"pdf"``).
    - ``page_count``: present iff the parser counted pages.
    - ``has_page_boundaries``: True iff text contains ``\\f`` markers
      that the ``pdf-page`` chunker can split on.

    ``images`` is the parser's per-image map: ``{image_id: ImageRef}``
    where each ``ImageRef`` carries ``image_bytes``, ``mime``, ``page``
    and an empty ``caption``. The caller can pass these to a VLM to
    fill in ``caption`` and rewrite ``[[IMAGE:<id>]]`` placeholders
    in ``text`` before chunking. Empty dict for non-imagey formats
    (txt/md/rtf/etc.) — callers should ``if images:`` before doing
    work.

    The ``content`` parameter is the raw uploaded bytes; we materialise
    them onto a temp file (the registry's parsers are file-path based,
    not bytes-based, so they can mmap PDFs cheaply etc.).
    """
    # Lazy import: ParserRegistry pulls in pymupdf / python-docx / odfpy
    # / striprtf at import time; making it lazy keeps importing this
    # module fast (CSP imports it eagerly via app boot) and lets unit
    # tests stub specific parsers without paying the full import cost.
    try:
        from agentic_rag.ingestion.parsers import ParserRegistry
    except ImportError as e:
        raise ParseError(
            code="E_INTERNAL",
            user_message="Parser stack unavailable (agentic-rag not installed).",
            details={"missing_dep": "agentic-rag", "cause": str(e)},
        ) from e

    # Use the original filename's extension for routing — uploaders
    # sometimes lie in MIME but rarely in the extension. The registry
    # raises ValueError for unsupported extensions; map that to the
    # structured ParseError code.
    import tempfile

    suffix = os.path.splitext(filename)[1] or ""
    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, prefix="ingest-"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        try:
            parsed = ParserRegistry.parse(tmp_path)
        except ValueError as e:
            # Unsupported extension. Surface the registry's own message
            # so the dev sees the supported list.
            raise ParseError.format_unsupported(
                user_message=str(e),
                details={"filename": filename, "ext": suffix},
            ) from e
        except Exception as e:  # parser-specific errors
            raise ParseError.corrupt(
                user_message=(
                    f"檔案無法解析（{type(e).__name__}）。"
                    "如果是 PDF 可能是純圖片或損毀；如果是 DOCX 請確認非密碼保護。"
                ),
                details={
                    "cause": type(e).__name__,
                    "message": str(e)[:300],
                },
            ) from e
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    text = parsed.content
    if not text or not text.strip():
        raise ParseError.corrupt(
            user_message=(
                "檔案抽取後沒有可用文字（純圖片 / 加密 / 空檔）。"
            ),
            details={"format": parsed.format, "metadata": parsed.metadata},
        )

    # Augment metadata with the chunker-relevant fields. The registry
    # already filled per-parser fields (title / page_count / etc.).
    metadata = dict(parsed.metadata)
    metadata.setdefault("format", parsed.format)

    # AgenticRAG's PdfParser reports ``pages``; some other parsers might
    # use ``page_count``. Read whichever is present and propagate a
    # canonical ``page_count`` key so the chunker / inspector don't
    # have to know the source-parser's vocabulary.
    page_count = metadata.get("page_count") or metadata.get("pages")
    if page_count is not None:
        metadata["page_count"] = int(page_count)
    metadata["has_page_boundaries"] = bool(
        page_count and int(page_count) > 1 and "\f" in text
    )

    # parsed.images may be empty (.txt etc.) or unset on older parser
    # versions; default to {} so the caller's ``if images:`` is safe
    # without an attribute check.
    images = getattr(parsed, "images", None) or {}
    return text, metadata, images


__all__ = ["extract_text"]

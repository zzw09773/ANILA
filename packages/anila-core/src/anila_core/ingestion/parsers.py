"""File-format → text extraction (Pillar 2 shared infrastructure).

Thin façade over ``anila_core.ingestion.parser_registry.ParserRegistry``
so multiple consumers can share the same ``extract_text`` callable:

    * ingestion-worker (Arq job pipeline, the original consumer)
    * CSP backend (POST /api/ingestion/chunking-preview, dry-run)
    * any future batch worker that needs to extract text from blobs

The HEAVY parser stack (pymupdf4llm, Docling, OCR, …) lives at
``anila_core.ingestion.parser_registry``. AgenticRAG/ in this branch is
a pure starter template — production code does not depend on it. To
get the parser stack pulled into your env, install ``anila-core[rag]``
(see anila-core/pyproject.toml).

Standalone envs that didn't install the ``[rag]`` extra will see a
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

from anila_core.ingestion.errors import ParseError, RemoteParseError

# RemoteDoclingError 是 docling 客戶端(遠端算力)的傳輸錯誤,與「檔案壞了」是
# 兩回事,必須在 generic `except Exception → corrupt` 之前被分流。module-level
# import 它不拉 torch／docling／pymupdf——docling_parser module-level 只 import
# httpx(base 硬相依)＋一個純 stdlib 的 parser_registry;heavy 全是函式內 lazy。
from anila_core.ingestion.docling_parser import RemoteDoclingError


def extract_text(
    filename: str, content: bytes, mime_type: str | None = None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(text, metadata, images)`` for one uploaded blob.

    ``metadata`` is the parser's own metadata dict augmented with:
    - ``format``: extension key the registry resolved (e.g. ``"pdf"``).
    - ``page_count``: present iff the parser counted pages.
    - ``has_page_boundaries``: True iff ``page_count > 1`` **and**
      ``text.split("\\f")`` yields exactly ``page_count`` fields. The
      ``> 1`` is deliberate: a single-page document has no boundary to
      mark, so the flag is False for it even though its one field
      trivially matches — "has boundaries" is read as "has separators
      between pages", not "has pages".

      The field count is **measured here, not taken on trust from the
      parser**, so a parser that emits the wrong number of markers turns
      the flag off rather than asserting a page structure it does not
      have. Callers get one guarantee from it and only one: when it is
      True, the field count matches ``page_count``. That field N is also
      *page* N is guaranteed by ``PdfParser``'s native extraction (it
      keeps image placeholders, and any ``\\f`` carried by the source
      text, inside their own page) but **not** by its OCR fallback,
      which replaces the text wholesale with backend output having no
      page structure of its own — see ``ocr_used``.

      ⚠ Nothing in this tree reads this flag today: it is an honest
      signal published for future consumers (a chunker-picker, an
      inspector badge), **not** a control. Turning it off does not stop
      anything — a document whose markers disagree with ``page_count``
      is still chunked by ``pdf-page`` if that strategy is selected, and
      the page numbers it derives still reach the user through
      ``chunk.metadata["page"]`` / ``["total_pages"]``. The one place
      that is loud about the disagreement at runtime is
      ``PdfPageChunker.chunk``, which logs a warning.

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
    # Lazy import: parser_registry pulls in pymupdf / python-docx / odfpy
    # / striprtf at import time; making it lazy keeps importing this
    # module fast (CSP imports it eagerly via app boot) and lets unit
    # tests stub specific parsers without paying the full import cost.
    try:
        from anila_core.ingestion.parser_registry import ParserRegistry
    except ImportError as e:
        raise ParseError(
            code="E_INTERNAL",
            user_message="Parser stack unavailable (install anila-core[rag]).",
            details={"missing_dep": "anila-core[rag]", "cause": str(e)},
        ) from e

    # Use the original filename's extension for routing — uploaders
    # sometimes lie in MIME but rarely in the extension. Empty suffix
    # (for005 / README / …) is content-gated by PlainTextParser, not a
    # hard reject. The registry raises ValueError for unsupported
    # extensions; map that to the structured ParseError code.
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
        except ParseError:
            # Structured parser refusals (e.g. binary-as-text) must not be
            # collapsed into E_PARSE_CORRUPT by the generic handler below.
            raise
        except RemoteParseError:
            # 控制面讀不到、或已設定的遠端服務故障。不是檔案壞了。
            raise
        except ValueError as e:
            # Unsupported extension — plain sentence for users; full
            # registry listing stays in structured details only.
            raise ParseError.format_unsupported(
                user_message=(
                    f"不支援此檔案格式（副檔名 {suffix or '未知'}）。"
                ),
                details={
                    "filename": filename,
                    "ext": suffix,
                    "registry_message": str(e),
                },
            ) from e
        except RemoteDoclingError as e:
            # 遠端 docling 端點失敗是**基礎設施問題**,不是「檔案壞了」——把它導到
            # retryable 的 RemoteParseError,訊息指服務,不指文件。這個 except 必須
            # 排在 generic `except Exception` 之前,否則會被塌進 E_PARSE_CORRUPT。
            # usersafe 進使用者面字串;e.details(端點/上游片段)進 details = 維運面。
            raise RemoteParseError.endpoint_unavailable(
                user_message=e.usersafe,
                details={
                    "parser": "docling-remote",
                    "filename": filename,
                    **e.details,
                },
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
    # Measured, not assumed. "text contains \f" is not evidence that \f
    # means *page boundary* here: PdfParser's OCR fallback swaps the text
    # for backend output that has no page structure, and a backend whose
    # own output carries \f (Tesseract's plain-text mode does) would make
    # the marker present but meaningless. Counting the fields and
    # comparing them to page_count is the cheap check that tells a real
    # page marker from a coincidence. ⚠ Turning the flag off does not
    # suppress anything — nothing reads it yet (see the docstring); the
    # wrong page number still reaches the user via the chunker. This
    # keeps the *signal* honest so a future consumer can trust it; the
    # runtime alarm for the same disagreement is in PdfPageChunker.
    metadata["has_page_boundaries"] = bool(
        page_count
        and int(page_count) > 1
        and len(text.split("\f")) == int(page_count)
    )

    # parsed.images may be empty (.txt etc.) or unset on older parser
    # versions; default to {} so the caller's ``if images:`` is safe
    # without an attribute check.
    images = getattr(parsed, "images", None) or {}
    return text, metadata, images


__all__ = ["extract_text"]

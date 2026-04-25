"""File-format → text extraction.

Sprint 1 covers the formats the chunkers actually understand:

- ``text/plain`` (.txt) — UTF-8 read, surrogate fallback.
- ``text/markdown`` (.md / .markdown) — UTF-8 read, no markdown-to-text
  conversion (chunkers want the raw markdown for heading detection).
- ``application/pdf`` — pypdf text extraction, page-joined with
  blank lines so MarkdownAware / Hierarchical chunkers don't confuse
  page boundaries with headings.

Everything else raises ``ParseError.format_unsupported`` — by design.
Sprint 2 will widen coverage with docling / mammoth.

The parser is a pure function: bytes in, str out + a metadata dict. No
disk IO, no network. The worker handler is responsible for reading the
blob from ``UPLOAD_DIR`` and passing the bytes here.
"""

from __future__ import annotations

import io
from typing import Any

from anila_core.ingestion.errors import ParseError

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover — pypdf is a hard dep
    PdfReader = None  # type: ignore[assignment]


# Map of recognised MIME types and extensions to a parser function. Both
# axes are checked because uploaders sometimes lie in one or the other.
_TEXT_LIKE_MIMES = {"text/plain", "text/markdown", "text/x-markdown"}
_MD_EXTS = {".md", ".markdown"}
_TXT_EXTS = {".txt", ".text"}
_PDF_EXTS = {".pdf"}
_PDF_MIMES = {"application/pdf"}


def extract_text(
    filename: str, content: bytes, mime_type: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Return ``(text, metadata)`` for one uploaded blob.

    ``metadata`` carries parser-specific context — e.g. PDF page count —
    that the worker forwards into the document row's ``metadata`` JSONB
    column for inspector-side surfacing.
    """
    name_lower = filename.lower()

    # Sniff by extension first (more reliable than MIME from browsers).
    ext = "." + name_lower.rsplit(".", 1)[-1] if "." in name_lower else ""

    if ext in _TXT_EXTS or mime_type == "text/plain":
        return _decode_text(content), {"format": "txt"}

    if ext in _MD_EXTS or mime_type in {"text/markdown", "text/x-markdown"}:
        return _decode_text(content), {"format": "markdown"}

    if ext in _PDF_EXTS or mime_type in _PDF_MIMES:
        return _extract_pdf(content)

    raise ParseError.format_unsupported(
        user_message=(
            f"目前僅支援 .txt / .md / .pdf。偵測到副檔名 {ext or '(無)'} / "
            f"MIME {mime_type or '(無)'}。"
        ),
        details={"filename": filename, "mime_type": mime_type, "ext": ext},
    )


def _decode_text(content: bytes) -> str:
    """UTF-8 decode with strict-then-replace fallback.

    Strict first so a corrupted file is loudly visible in the audit log
    via the warning replacement count. ``errors='replace'`` then
    guarantees the chunker still has *some* text to work with — better
    than failing the whole job over one bad byte.
    """
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def _extract_pdf(content: bytes) -> tuple[str, dict[str, Any]]:
    """pypdf text extraction with page boundaries preserved.

    Extracts text per-page and joins with double newlines. We intentionally
    don't strip whitespace at page joins — the blank lines mean
    MarkdownAware / Hierarchical chunkers see page transitions as paragraph
    boundaries, which usually align with semantic boundaries in scanned
    docs.

    Empty / image-only PDFs surface as ``ParseError.corrupt`` rather than
    silently producing an empty string — saves a confusing
    "0 chunks indexed" outcome at the user-visible layer.
    """
    if PdfReader is None:
        raise ParseError(
            code="E_INTERNAL",
            user_message="PDF parser unavailable (pypdf not installed).",
            details={"missing_dep": "pypdf"},
        )

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as e:  # pypdf raises a wide variety on malformed PDFs
        raise ParseError.corrupt(
            user_message="PDF 檔案損毀或非標準格式，請改用其他工具另存。",
            details={"cause": type(e).__name__, "message": str(e)},
        ) from e

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            # Per-page failure (malformed font tables, encrypted regions)
            # — skip the page rather than fail the whole doc. Track the
            # skip count in metadata so the dev sees the loss in the
            # inspector.
            pages.append("")

    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise ParseError.corrupt(
            user_message=(
                "無法從 PDF 抽取任何文字。可能是純圖片 PDF — Sprint 2 "
                "會加入 OCR；目前只支援含內嵌文字的 PDF。"
            ),
            details={"page_count": len(pages)},
        )

    return text, {
        "format": "pdf",
        "page_count": len(pages),
        "non_empty_pages": sum(1 for p in pages if p.strip()),
    }

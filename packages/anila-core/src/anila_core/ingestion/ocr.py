"""Detect scanned or font-subsetted PDFs on the native parser.

Text recognition for those files is Docling's job (EasyOCR on the remote
document parser). This module only decides that the native extraction is
not usable. It does not call a vision model.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

SCANNED_PDF_NEEDS_DOCLING = (
    "這份 PDF 抽不到可用文字層，掃描件需要 Docling 文件解析服務。"
    "請在治理中心設定文件解析服務後再上傳。"
)


# ──────────────────────────────────────────────────────────────────────
# Trigger heuristic — unchanged from prior phase.
# ──────────────────────────────────────────────────────────────────────

# pymupdf4llm leaves "<?>" sentinels for glyphs whose Unicode mapping it
# could not resolve. A high density of those signals a font-subsetted
# (CID-only) PDF that needs OCR to be readable.
_PLACEHOLDER = "<?>"
_PLACEHOLDER_RATIO_THRESHOLD = 0.30  # ≥30% of all chars are <?> ⇒ run OCR

# ``parser_registry`` inserts one ``[[IMAGE:<id>]]`` token per embedded
# image. Those tokens are OUR OWN output, not extracted text — 24
# characters each with the current id format. Measuring them as "text"
# is what made this trigger dead: a two-page pure-scan PDF extracts zero
# characters of text but two placeholders = 48 characters, which cleared
# the 40-character floor and was declared "has text, no OCR needed".
# Strip them before measuring anything.
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[\[IMAGE:[^\]\n]*\]\]")

# Per-page furniture is the second half of the same problem, one level up.
# The absolute floor alone is cleared by anything the document stamps on
# every page: this is the NCSIST air-gapped intranet, so every page of every real
# document carries a classification marking ("CONFIDENTIAL - NCSIST
# Internal Use Only - Page", 45 characters), and page numbers and document
# ids stack on top of it. Chasing that with a bigger character constant is
# the blacklist game — the next stamp is always one character longer, and a
# constant big enough to beat it starts OCR-ing sparse-but-real documents
# (slide decks, drawing indexes).
#
# What separates furniture from content is not its length, it is that
# **furniture repeats and content does not**. So the measurement drops
# lines that appear on most pages, then asks how many pages still carry
# text. A document is treated as a scan when most of its pages do not.
#
# That second question also replaces the old per-document average, which
# hid mostly-scanned documents: one 10 000-character page in a 200-page
# scan averaged 50 characters a page and read as a text document.
_MIN_CHARS_PER_TEXT_PAGE = 20   # a page carries text above this many nonws chars
_MIN_TEXT_PAGE_FRACTION = 0.5   # fewer text pages than this share ⇒ it is a scan
_BOILERPLATE_PAGE_RATIO = 0.6   # a line on this share of pages is furniture

# …but repetition alone would also delete repeated *content*. A document
# whose every page ends with the same paragraph is not furnished, it is
# repetitive, and dropping that paragraph would be dropping text the user
# can read — which would turn a readable document into a "scan" and send it
# to OCR. Headers, footers, stamps and page numbers are short by
# construction; a repeated line longer than this is content.
_MAX_FURNITURE_LINE_CHARS = 80

# Digit runs are normalised so "Page 1 of 250" and "Page 2 of 250" are the
# same line for repetition counting.
_DIGIT_RUN_RE = re.compile(r"\d+")


def strip_image_placeholders(content: str) -> str:
    """Remove ``[[IMAGE:<id>]]`` tokens, leaving a space in their place."""
    return _IMAGE_PLACEHOLDER_RE.sub(" ", content)


def _normalise_line(line: str) -> str:
    """Collapse whitespace and digit runs so per-page stamps compare equal."""
    return _DIGIT_RUN_RE.sub("#", " ".join(line.split()))


def _boilerplate_lines(page_texts: Sequence[str]) -> set[str]:
    """Normalised lines that appear on most pages — headers, footers, stamps.

    Needs at least two pages: with one page nothing can repeat, and there is
    no way to tell furniture from content.
    """
    if len(page_texts) < 2:
        return set()
    counts: Counter[str] = Counter()
    for text in page_texts:
        # Per page, count a line once — a stamp repeated within one page
        # must not out-vote its presence across pages.
        counts.update({
            norm
            for norm in (_normalise_line(line) for line in text.splitlines())
            if norm and len(norm) <= _MAX_FURNITURE_LINE_CHARS
        })
    threshold = max(2, math.ceil(len(page_texts) * _BOILERPLATE_PAGE_RATIO))
    return {line for line, seen_on in counts.items() if seen_on >= threshold}


def _page_text_chars(page_text: str, boilerplate: set[str]) -> int:
    """Non-whitespace characters on one page, minus placeholders and furniture."""
    kept = [
        line
        for line in strip_image_placeholders(page_text).splitlines()
        if (norm := _normalise_line(line)) and norm not in boilerplate
    ]
    return len("".join("".join(kept).split()))


def needs_ocr_fallback(
    content: str,
    min_chars: int = 40,
    page_texts: Sequence[str] | None = None,
    min_chars_per_text_page: int = _MIN_CHARS_PER_TEXT_PAGE,
    min_text_page_fraction: float = _MIN_TEXT_PAGE_FRACTION,
) -> bool:
    """Return True when ``content`` looks like a failed text extraction.

    Everything is measured **after** ``[[IMAGE:<id>]]`` placeholders are
    removed, so only text the extractor actually recovered counts.

    ``page_texts`` is the per-page extraction *before* placeholders were
    appended (``PdfParser`` has it as ``page_chunks``). It is passed rather
    than derived by splitting ``content`` on ``\\f``, because that marker
    does not reliably delimit pages — the parser emits one between a page's
    text and each of that page's own images.

    Triggers when:
      * real text is < ``min_chars`` non-whitespace characters, OR
      * ``page_texts`` is given and fewer than ``min_text_page_fraction`` of
        the pages carry at least ``min_chars_per_text_page`` non-whitespace
        characters that are neither placeholders nor repeated-on-most-pages
        furniture, OR
      * ``<?>`` density in the real text exceeds ``_PLACEHOLDER_RATIO_THRESHOLD``.

    The decision is deliberately whole-document, not per-page: the OCR
    backend re-reads the entire file, so triggering on a single image-only
    page inside an otherwise readable document would pay the full OCR cost
    for one page's worth of gain.

    ⚠ Known, documented defeat condition: a **single-page** scan whose
    stamp alone exceeds ``min_chars``. Nothing repeats on a one-page
    document, so furniture is indistinguishable from content there. See
    ``services/ingestion-worker/README.md``.
    """
    real = strip_image_placeholders(content)
    stripped = "".join(real.split())
    if len(stripped) < min_chars:
        return True
    if page_texts:
        boilerplate = _boilerplate_lines(page_texts)
        text_pages = sum(
            1
            for page_text in page_texts
            if _page_text_chars(page_text, boilerplate) >= min_chars_per_text_page
        )
        if text_pages < len(page_texts) * min_text_page_fraction:
            return True
    placeholder_count = real.count(_PLACEHOLDER) * len(_PLACEHOLDER)
    if placeholder_count and placeholder_count / max(len(stripped), 1) >= _PLACEHOLDER_RATIO_THRESHOLD:
        return True
    return False

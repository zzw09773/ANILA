"""OCR fallback for scanned / font-subsetted PDFs.

Single backend: ``VisionApiOcrBackend`` — rasterises each PDF page and
sends it to an OpenAI-compatible vision LLM endpoint (deployment target:
``meta/llama-4-maverick`` already running on the internal model server),
asking for verbatim text extraction.

Why one backend only:
* The deployment runs on a closed internal network with 4× H100 — a
  vision LLM on the model server beats EasyOCR/Tesseract on the app
  machine on every axis (latency, quality, infra footprint).
* Re-using the existing ``VISION_URL`` means zero new server services.

Earlier multi-backend designs (EasyOCR / Tesseract) are archived under
``_archive/phase4_pre_rewrite/`` if you ever need a CPU-only fallback.
"""
from __future__ import annotations

import base64
import logging
import math
import os
import re
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


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
# every page: this is a military intranet, so every page of every real
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


# ──────────────────────────────────────────────────────────────────────
# Backend
# ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class OcrBackend(Protocol):
    def extract(self, file_path: str) -> str: ...


_DEFAULT_VISION_PROMPT = (
    "請逐字輸出此圖片中的繁體中文，保留段落結構，不要翻譯也不要摘要。"
    "若圖片中包含表格，請以 Markdown 表格輸出。"
    "若圖片中沒有任何文字，請回覆「[NO_TEXT]」。"
)


class VisionApiOcrBackend:
    """OCR by sending each rasterised page to an OpenAI-compatible vision LLM.

    Pages are processed in parallel via a thread pool. Each request hits
    ``POST {base_url}/chat/completions`` with a single user message
    containing the page PNG (base64-encoded) plus the OCR prompt.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        prompt: str = _DEFAULT_VISION_PROMPT,
        dpi: int = 200,
        concurrency: int = 4,
        max_pages: int = 100,
        timeout: float = 60.0,
        verify_ssl: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("VisionApiOcrBackend requires a non-empty base_url")
        if not model:
            raise ValueError("VisionApiOcrBackend requires a non-empty model")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._prompt = prompt
        self._dpi = dpi
        self._concurrency = max(1, concurrency)
        self._max_pages = max(1, max_pages)
        self._timeout = timeout
        self._verify_ssl = verify_ssl

    # -- public ---------------------------------------------------------------

    @property
    def max_pages(self) -> int:
        """Hard page cap. Pages past it are **not** OCR'd and their text is lost.

        Public because the caller must be able to say so: ``extract`` returns
        the OCR of pages 1..``max_pages`` only, and ``PdfParser`` replaces the
        whole native extraction with it, so a document longer than the cap
        silently loses the native text of every page beyond it. The caller
        reads this to report the loss instead of guessing.
        """
        return self._max_pages

    def extract(self, file_path: str) -> str:
        try:
            import fitz  # type: ignore[import]  # pymupdf
        except ImportError as exc:
            raise ImportError(
                "OCR fallback requires pymupdf to rasterise PDF pages. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        doc = fitz.open(file_path)
        try:
            page_images = self._rasterise_pages(doc)
        finally:
            doc.close()

        if not page_images:
            return ""

        return self._ocr_pages_in_parallel(page_images, source=file_path)

    # -- helpers --------------------------------------------------------------

    def _rasterise_pages(self, doc) -> list[tuple[int, bytes]]:
        page_count = min(len(doc), self._max_pages)
        if page_count < len(doc):
            logger.warning(
                "PDF has %d pages but PDF_OCR_MAX_PAGES=%d — truncating",
                len(doc), self._max_pages,
            )
        out: list[tuple[int, bytes]] = []
        for pno in range(page_count):
            page = doc[pno]
            pix = page.get_pixmap(dpi=self._dpi)
            out.append((pno + 1, pix.tobytes("png")))
        return out

    def _ocr_pages_in_parallel(
        self,
        pages: list[tuple[int, bytes]],
        source: str,
    ) -> str:
        """Fan out page OCR over a thread pool, then re-assemble in page order."""
        results: dict[int, str] = {}
        # httpx.Client is created once and shared across threads — it has
        # a connection pool and is documented as thread-safe for ``post``.
        client = httpx.Client(verify=self._verify_ssl, timeout=self._timeout)
        try:
            with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
                futures = {
                    pool.submit(self._ocr_single_page, client, png_bytes, page_no, source): page_no
                    for page_no, png_bytes in pages
                }
                for fut in as_completed(futures):
                    page_no = futures[fut]
                    try:
                        text = fut.result()
                    except Exception as exc:
                        logger.warning(
                            "Vision OCR page %d failed for %s: %s",
                            page_no, source, exc,
                        )
                        text = ""
                    results[page_no] = text
        finally:
            client.close()

        ordered = [results.get(n, "") for n in sorted(results)]
        return "\n\n".join(t for t in ordered if t and t.strip() != "[NO_TEXT]")

    def _ocr_single_page(
        self,
        client: httpx.Client,
        png_bytes: bytes,
        page_no: int,
        source: str,
    ) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.0,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        resp = client.post(
            f"{self._base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "Vision OCR page %d returned unexpected shape for %s: %s",
                page_no, source, exc,
            )
            return ""


# ──────────────────────────────────────────────────────────────────────
# Env-driven factory
# ──────────────────────────────────────────────────────────────────────

def build_ocr_backend_from_env() -> Optional[OcrBackend]:
    """Construct the OCR backend selected by env, or ``None`` if disabled.

    Env:
      PDF_OCR_FALLBACK         = "true" | "false"   (default: false)
      PDF_OCR_DPI              = page raster DPI    (default: 200)
      PDF_OCR_CONCURRENCY      = parallel page reqs (default: 4)
      PDF_OCR_MAX_PAGES        = safety cap         (default: 100)
      PDF_OCR_VISION_PROMPT    = prompt override
      VISION_URL               = base URL of the OpenAI-compatible
                                 vision endpoint (re-used from the
                                 vision provider config)
      VISION_MODEL             = served vision model name
      VISION_API_KEY           = optional bearer token
      VISION_VERIFY_SSL        = "true" | "false"   (default: true)
    """
    if os.getenv("PDF_OCR_FALLBACK", "false").lower() != "true":
        return None

    base_url = os.getenv("VISION_URL", "").strip()
    model = os.getenv("VISION_MODEL", "").strip()
    if not base_url or not model:
        logger.warning(
            "PDF_OCR_FALLBACK=true but VISION_URL or VISION_MODEL is missing "
            "— OCR disabled"
        )
        return None

    return VisionApiOcrBackend(
        base_url=base_url,
        model=model,
        api_key=os.getenv("VISION_API_KEY", "").strip(),
        prompt=os.getenv("PDF_OCR_VISION_PROMPT", _DEFAULT_VISION_PROMPT),
        dpi=int(os.getenv("PDF_OCR_DPI", "200")),
        concurrency=int(os.getenv("PDF_OCR_CONCURRENCY", "4")),
        max_pages=int(os.getenv("PDF_OCR_MAX_PAGES", "100")),
        verify_ssl=os.getenv("VISION_VERIFY_SSL", "true").lower() == "true",
    )

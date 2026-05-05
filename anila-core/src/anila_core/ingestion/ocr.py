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
import os
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


def needs_ocr_fallback(content: str, min_chars: int = 40) -> bool:
    """Return True when ``content`` looks like a failed text extraction.

    Triggers when:
      * extraction yielded < ``min_chars`` non-whitespace characters, OR
      * placeholder ``<?>`` density exceeds ``_PLACEHOLDER_RATIO_THRESHOLD``.
    """
    stripped = "".join(content.split())
    if len(stripped) < min_chars:
        return True
    placeholder_count = content.count(_PLACEHOLDER) * len(_PLACEHOLDER)
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

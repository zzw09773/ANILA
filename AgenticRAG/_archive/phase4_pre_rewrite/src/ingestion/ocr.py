"""OCR fallback for scanned / font-subsetted PDFs.

Two backends, both non-China origin:

- **EasyOCR**: pure Python + PyTorch (JaidedAI, international community).
  Use ``lang_list=['ch_tra','en']`` for Traditional Chinese + English.
- **Tesseract**: Google's classic engine via ``pytesseract``. Needs
  ``tesseract-ocr`` binary plus the ``chi_tra.traineddata`` language pack.

Both backends are *optional* dependencies; this module raises a clear
``ImportError`` when the chosen backend is missing rather than silently
disabling OCR.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# Heuristic: pymupdf4llm leaves "<?>" sentinels for glyphs whose Unicode
# mapping it could not resolve. A high density of those signals a font-
# subsetted (CID-only) PDF that needs OCR to be readable.
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


@runtime_checkable
class OcrBackend(Protocol):
    def extract(self, file_path: str) -> str: ...


class EasyOcrBackend:
    """EasyOCR backend (lazy-loaded; CPU is supported but slow)."""

    def __init__(self, languages: Optional[list[str]] = None) -> None:
        self._languages = languages or ["ch_tra", "en"]
        self._reader = None

    def _ensure_loaded(self) -> None:
        if self._reader is not None:
            return
        try:
            import easyocr  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "EasyOCR backend needs the 'easyocr' package. "
                "Install with: pip install 'agentic-rag[ocr]'"
            ) from exc
        # gpu=False → CPU; users with CUDA can override via env later.
        self._reader = easyocr.Reader(self._languages, gpu=False, verbose=False)
        logger.info("EasyOCR loaded with languages=%s", self._languages)

    def extract(self, file_path: str) -> str:
        self._ensure_loaded()
        assert self._reader is not None

        try:
            import fitz  # type: ignore[import]  # pymupdf
        except ImportError as exc:
            raise ImportError(
                "OCR fallback requires pymupdf to rasterise PDF pages. "
                "Install with: pip install 'agentic-rag[rag]'"
            ) from exc

        pages_text: list[str] = []
        doc = fitz.open(file_path)
        try:
            for pno in range(len(doc)):
                page = doc[pno]
                # 200 DPI gives a good speed/accuracy tradeoff for OCR
                pix = page.get_pixmap(dpi=200)
                png_bytes = pix.tobytes("png")
                # detail=0 returns plain strings, not bbox tuples
                lines = self._reader.readtext(png_bytes, detail=0, paragraph=True)
                if lines:
                    pages_text.append("\n".join(lines))
        finally:
            doc.close()
        return "\n\n".join(pages_text)


class TesseractOcrBackend:
    """Tesseract backend via pytesseract (needs tesseract binary + chi_tra)."""

    def __init__(self, lang: str = "chi_tra+eng") -> None:
        self._lang = lang

    def extract(self, file_path: str) -> str:
        try:
            import pytesseract  # type: ignore[import]
            from PIL import Image  # type: ignore[import]
            import io
        except ImportError as exc:
            raise ImportError(
                "Tesseract backend needs 'pytesseract' and 'Pillow'. "
                "Install with: pip install 'agentic-rag[ocr-tesseract]' and "
                "ensure the tesseract-ocr binary + chi_tra.traineddata are present."
            ) from exc
        try:
            import fitz  # type: ignore[import]  # pymupdf
        except ImportError as exc:
            raise ImportError(
                "OCR fallback requires pymupdf to rasterise PDF pages."
            ) from exc

        pages_text: list[str] = []
        doc = fitz.open(file_path)
        try:
            for pno in range(len(doc)):
                page = doc[pno]
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                text = pytesseract.image_to_string(img, lang=self._lang)
                if text.strip():
                    pages_text.append(text)
        finally:
            doc.close()
        return "\n\n".join(pages_text)


def build_ocr_backend_from_env() -> Optional[OcrBackend]:
    """Construct the OCR backend selected by env, or ``None`` if disabled.

    Env:
      PDF_OCR_FALLBACK = "true" | "false"            (default: false)
      PDF_OCR_BACKEND  = "easyocr" | "tesseract"     (default: easyocr)
      PDF_OCR_LANGS    = comma-separated list, EasyOCR only (default: "ch_tra,en")
      PDF_OCR_TESSERACT_LANG = tesseract --lang flag (default: "chi_tra+eng")
    """
    if os.getenv("PDF_OCR_FALLBACK", "false").lower() != "true":
        return None
    backend = os.getenv("PDF_OCR_BACKEND", "easyocr").lower()
    if backend == "easyocr":
        langs = [
            s.strip()
            for s in os.getenv("PDF_OCR_LANGS", "ch_tra,en").split(",")
            if s.strip()
        ]
        return EasyOcrBackend(languages=langs)
    if backend == "tesseract":
        return TesseractOcrBackend(
            lang=os.getenv("PDF_OCR_TESSERACT_LANG", "chi_tra+eng"),
        )
    logger.warning("Unknown PDF_OCR_BACKEND=%s — OCR disabled", backend)
    return None

"""The OCR trigger must measure recovered text, not the parser's own tokens —
and taking the OCR result must say what it costs.

The defect this pins: ``PdfParser`` inserts one ``[[IMAGE:<id>]]`` token per
embedded image — 24 characters each — and ``needs_ocr_fallback`` measured the
joined content including those tokens against a 40-character floor.  A
two-page pure scan extracts **zero** characters of text but emits two
placeholders = 48 characters, cleared the floor, and was declared "has text,
no OCR needed".  With ``PDF_OCR_FALLBACK=true`` the backend was never called
and the document was indexed with nothing retrievable in it.

Three layers of test:

* Decision-only tests call ``needs_ocr_fallback`` directly.  They need no OCR
  engine and no PDF — the question "would we OCR this?" is separable from
  "can we OCR this?", and only the first one was broken.
* Parser-level tests build a real scanned-style PDF (pages are raster images,
  no text layer) and run the real ``PdfParser``, so the placeholder injection
  under test is the production code path, not a re-implementation.
* Loss-report tests pin that the parser tells the truth about what taking the
  OCR result destroys.  ⚠ They are written so they hold under a *merge*
  policy too: they compare the report against the final content rather than
  asserting that replacement is correct.  Whether replace / merge / refuse is
  right is an owner decision, and these tests must not foreclose it.

⚠ ``pymupdf4llm`` >= 1.x runs its own bundled-Tesseract pass on pages it
thinks need OCR.  The deployment image has no Tesseract, so there the pass is
a no-op and image-only pages extract as ``""``.  A developer host with
Tesseract installed would extract *something* and the fixture would stop being
a scan.  ``_no_tesseract`` models the image, and
``test_the_no_tesseract_fixture_still_models_the_image`` asserts the *outcome*
it is supposed to produce, so a pymupdf4llm bump that changes the mechanism is
caught rather than silently making every scan fixture a lie.
"""

from __future__ import annotations

import logging
import re

import pytest

from anila_core.ingestion.errors import ParseError
from anila_core.ingestion.ocr import SCANNED_PDF_NEEDS_DOCLING, needs_ocr_fallback
from anila_core.ingestion.parser_registry import PdfParser

fitz = pytest.importorskip("fitz", reason="pymupdf is part of the [rag] extra")
pymupdf4llm = pytest.importorskip(
    "pymupdf4llm", reason="pymupdf4llm is part of the [rag] extra"
)


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

# The real token shape: ``img_`` + 10 hex chars, inside ``[[IMAGE:…]]``.
def _placeholder(n: int) -> str:
    return f"[[IMAGE:img_{n:010x}]]"


def _blind_image_ids(text: str) -> str:
    """Erase the random part of image ids so two parses can be compared.

    ``_new_image_id`` draws a fresh uuid every parse, so comparing the text
    of two runs of the same file needs the ids masked — otherwise a check
    that is really about *content* fails on identifiers.
    """
    return re.sub(r"\[\[IMAGE:[^\]]*\]\]", "[[IMAGE]]", text)


# Per-page furniture actually seen on documents this platform ingests.  The
# first one is not hypothetical: on the NCSIST air-gapped intranet every page of every
# document carries a classification marking.
STAMP_CLASSIFICATION = "CONFIDENTIAL - NCSIST Internal Use Only - Page"
STAMP_PAGINATION = "Page 1 of 250 | Doc No. A-1234-56"
STAMP_WATERMARK = "https://anila.ai.ncsist.org.tw/doc/export"
STAMP_DOCID = "ANILA-DOC-20260807-000123456789"
STAMP_CAMSCANNER = "Scanned by CamScanner"

PARAGRAPH = (
    "The contractor shall demonstrate compliance with each requirement "
    "listed in section four before the design review is held."
)


def _stamped_pages(count: int, stamp: str, body: str = "") -> list[str]:
    """Per-page extraction: a stamp that varies only by page number, plus body."""
    return [f"{stamp} {n} of {count}\n{body}" for n in range(1, count + 1)]


# ``insert_text`` does not wrap — a long line runs off the page edge and the
# layout parser clips it away, producing an empty "text" page that silently
# turns a text fixture into a scan.  Always lay body text out in a box.
_BODY_BOX = (72, 72, 523, 400)


def _text_pdf(tmp_path, pages: int, body: str, name: str = "text.pdf") -> str:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(fitz.Rect(*_BODY_BOX), f"{body} (page {i + 1})", fontsize=12)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def _scanned_pdf(tmp_path, pages: int, body: str = "", name: str = "scan.pdf") -> str:
    """A PDF whose pages are raster images — plus optional real text on top.

    ``body`` is drawn as a genuine text layer *after* the image, so the same
    helper produces both the pure scan and the mixed "short caption + image"
    page.
    """
    src = fitz.open()
    for i in range(pages):
        page = src.new_page(width=595, height=842)
        page.insert_text((72, 200), f"Rasterised page {i + 1} content", fontsize=18)

    out = fitz.open()
    for pno in range(len(src)):
        pix = src[pno].get_pixmap(dpi=72)
        page = out.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), stream=pix.tobytes("png"))
        if body:
            page.insert_textbox(fitz.Rect(72, 750, 523, 800), body, fontsize=11)
    src.close()

    path = str(tmp_path / name)
    out.save(path)
    out.close()
    return path


class _SpyBackend:
    """Records whether the parser decided to OCR; returns plausible text.

    ``max_pages`` mirrors the real backend's public cap so the parser can
    report truncation without knowing which backend it holds.
    """

    def __init__(
        self,
        text: str = "OCR recovered this sentence from the page image.",
        max_pages: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._text = text
        if max_pages is not None:
            self.max_pages = max_pages

    def extract(self, file_path: str) -> str:
        self.calls.append(file_path)
        return self._text


@pytest.fixture
def _no_tesseract(monkeypatch):
    """Model the deployed image: no Tesseract, so pymupdf4llm's pass is a no-op.

    ⚠ Mechanism differs by environment.  In the image ``pymupdf.get_tessdata()``
    *raises*, so ``import pymupdf4llm.ocr.tesseract_api`` raises ``RuntimeError``
    — not ``ImportError``.  Where the import raises, the image's behaviour is
    already what we want and there is nothing to patch; where it succeeds (a
    developer host with Tesseract), ``TESSDATA`` has to be neutralised.  Catch
    broadly on purpose: the outcome is what matters, and
    ``test_the_no_tesseract_fixture_still_models_the_image`` asserts it.
    """
    try:
        import pymupdf4llm.ocr.tesseract_api as tesseract_api
    except Exception:
        return
    monkeypatch.setattr(tesseract_api, "TESSDATA", None, raising=False)


def _use_backend(monkeypatch, backend):
    monkeypatch.setattr(PdfParser, "_ocr_backend", backend, raising=False)
    monkeypatch.setattr(PdfParser, "_ocr_initialised", True, raising=False)


@pytest.fixture
def spy(monkeypatch):
    backend = _SpyBackend()
    _use_backend(monkeypatch, backend)
    return backend


# ──────────────────────────────────────────────────────────────────────
# (d) the regression pin — placeholders alone are never "text"
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_images", [1, 2, 3, 5, 12, 59, 250, 5000])
def test_image_placeholders_alone_never_satisfy_the_threshold(n_images):
    """No number of ``[[IMAGE:…]]`` tokens may ever read as extracted text.

    Ranged rather than sampled on purpose: the old code passed at n=1 (24
    chars < 40) and failed from n=2 (48 chars) upward, so a single example
    picks the wrong side by luck.  The property is closure over the range,
    not a chosen witness.
    """
    content = "\f\n".join(_placeholder(i) for i in range(n_images))
    assert needs_ocr_fallback(content, page_texts=[""] * n_images) is True
    # …and with no page information either — the absolute floor holds alone.
    assert needs_ocr_fallback(content) is True


def test_placeholders_do_not_rescue_a_page_that_is_short_of_real_text():
    """Real text is measured on its own, never topped up by placeholders."""
    real = "Twenty-eight characters here"  # 26 non-whitespace, under the floor
    assert needs_ocr_fallback(real) is True
    padded = real + "".join(_placeholder(i) for i in range(4))
    assert needs_ocr_fallback(padded) is True


# ──────────────────────────────────────────────────────────────────────
# (b) normal text documents are left alone — the cost guard
# ──────────────────────────────────────────────────────────────────────

def test_normal_text_content_is_not_sent_to_ocr():
    assert needs_ocr_fallback(PARAGRAPH) is False
    assert needs_ocr_fallback(PARAGRAPH, page_texts=[PARAGRAPH]) is False


def test_text_document_with_images_is_still_not_sent_to_ocr():
    """Stripping placeholders must not push a real document over the edge."""
    pages = [f"{PARAGRAPH}\n" for _ in range(6)]
    content = "\f\n".join(p + _placeholder(i) for i, p in enumerate(pages))
    assert needs_ocr_fallback(content, page_texts=pages) is False


@pytest.mark.parametrize(
    "stamp",
    [STAMP_CLASSIFICATION, STAMP_PAGINATION, STAMP_WATERMARK, STAMP_DOCID],
    ids=["classification", "pagination", "watermark", "docid"],
)
@pytest.mark.parametrize("pages", [3, 5, 50, 400])
def test_a_real_document_that_carries_a_stamp_is_left_alone(stamp, pages):
    """De-furnishing must not turn a real document into a scan.

    Every page here carries the same marking *and* a real paragraph.  The
    marking is dropped as furniture; the paragraph is what decides.
    """
    pages_text = _stamped_pages(pages, stamp, PARAGRAPH)
    assert needs_ocr_fallback("\f\n".join(pages_text), page_texts=pages_text) is False


def test_a_document_whose_scanned_pages_are_the_minority_is_left_alone():
    """Cost guard: a mostly-readable document must not pay whole-document OCR."""
    pages = [PARAGRAPH] * 100 + [""] * 20
    content = "\f\n".join(p for p in pages if p)
    assert needs_ocr_fallback(content, page_texts=pages) is False


# ──────────────────────────────────────────────────────────────────────
# (c) per-page furniture must not vouch for a document — Y4 / Z3
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "stamp",
    [STAMP_CLASSIFICATION, STAMP_PAGINATION, STAMP_WATERMARK, STAMP_DOCID,
     STAMP_CAMSCANNER],
    ids=["classification", "pagination", "watermark", "docid", "camscanner"],
)
@pytest.mark.parametrize("pages", [2, 3, 5, 50, 400])
def test_per_page_furniture_never_counts_as_a_text_layer(stamp, pages):
    """The exact shape of the original bug, one level up.

    A stamp the document puts on every page clears the absolute 40-character
    floor from two pages onward while the document still has no readable
    content.  What separates furniture from content is that furniture
    repeats — that, not its length, is what the rule uses, which is why a
    45-character marking fails here as surely as a 19-character one.
    """
    pages_text = _stamped_pages(pages, stamp)
    content = "\f\n".join(
        f"{p}\n{_placeholder(i)}" for i, p in enumerate(pages_text)
    )
    assert needs_ocr_fallback(content, page_texts=pages_text) is True


@pytest.mark.parametrize(
    "total,text_pages",
    [(200, 1), (400, 20), (100, 5), (100, 10), (50, 2), (120, 20)],
)
def test_a_mostly_scanned_document_is_not_hidden_by_one_dense_page(total, text_pages):
    """Averaging hid these: one 10 000-character page in a 200-page scan
    averaged 50 characters a page and read as a text document.  The question
    is how many pages carry text, not how much text the document has."""
    pages = [PARAGRAPH * 80] * text_pages + [""] * (total - text_pages)
    content = "\f\n".join(p for p in pages if p)
    assert needs_ocr_fallback(content, page_texts=pages) is True


def test_a_single_page_scan_with_a_long_stamp_is_the_documented_hole():
    """Pinned as a known limitation, not as correct behaviour.

    Nothing repeats on a one-page document, so furniture cannot be told from
    content there.  If this ever starts returning True the limitation is
    gone and the README paragraph describing it should go with it.
    """
    page = f"{STAMP_CLASSIFICATION} 1 of 1"
    assert needs_ocr_fallback(page, page_texts=[page]) is False


def test_a_page_with_a_real_paragraph_and_an_image_is_not_sent_to_ocr():
    """Mixed page, documented rule: enough real text on the page ⇒ no OCR."""
    caption = (
        "Figure 1: cross-section of the mounting bracket, with the two "
        "load-bearing welds called out."
    )
    content = f"{caption}\n{_placeholder(1)}"
    assert needs_ocr_fallback(content, page_texts=[caption]) is False


def test_a_page_with_only_a_two_word_caption_and_an_image_is_sent_to_ocr():
    """Mixed page, other side of the same rule: a caption is not a text layer."""
    content = f"Figure 1\n{_placeholder(1)}"
    assert needs_ocr_fallback(content, page_texts=["Figure 1"]) is True


def test_glyph_placeholder_density_is_measured_on_real_text_only():
    """``<?>`` density must not be diluted by image placeholders."""
    subsetted = "<?>" * 20 + "readable tail text that survived extraction"
    assert needs_ocr_fallback(subsetted) is True
    diluted = subsetted + "".join(_placeholder(i) for i in range(20))
    assert needs_ocr_fallback(diluted) is True


# ──────────────────────────────────────────────────────────────────────
# parser level — a scan on the native parser asks for Docling
# ──────────────────────────────────────────────────────────────────────

def test_the_no_tesseract_fixture_still_models_the_image(tmp_path, _no_tesseract):
    """If this fails, every scan fixture below has quietly stopped being a scan."""
    page_chunks = pymupdf4llm.to_markdown(
        _scanned_pdf(tmp_path, pages=1), page_chunks=True
    )
    assert "".join(page_chunks[0]["text"].split()) == "", (
        "an image-only page extracted text — pymupdf4llm's own OCR pass is "
        "running here but not in the deployed image, so these fixtures no "
        "longer model production"
    )


def test_scanned_pdf_says_docling_is_required(tmp_path, _no_tesseract):
    with pytest.raises(ParseError) as exc:
        PdfParser().parse(_scanned_pdf(tmp_path, pages=2))
    assert exc.value.user_message == SCANNED_PDF_NEEDS_DOCLING
    assert "Docling" in exc.value.user_message


def test_text_pdf_is_still_parsed(tmp_path, _no_tesseract):
    parsed = PdfParser().parse(_text_pdf(tmp_path, pages=2, body=PARAGRAPH))
    assert parsed.metadata["ocr_used"] is False
    assert "contractor" in parsed.content
    assert "ocr_losses" not in parsed.metadata

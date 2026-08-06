"""The ``\\f`` page marker means *page boundary* — exactly, and nothing else.

Page numbers are the one thing a reader can check. "見 doc.pdf 第 4 頁" is
either true or it destroys trust in every other answer the platform gives,
so the number in ``chunk.metadata["page"]`` has to be the number printed on
the page. It is derived from a single character, which makes that character
load-bearing: every ``\\f`` in the parser's output must be a page boundary,
and every page boundary must be exactly one ``\\f``.

Two ways that contract was broken, both fixed here and both pinned below:

* Image placeholders were appended to the parser's ``parts`` list as
  *separate* entries, and ``parts`` was joined with ``\\f``. An N-page PDF
  with M images per page therefore reported N×(1+M) pages — a 10-page,
  3-image spec became "40 pages", and real page 10 was cited as page 37.
* Blank pages were filtered out (in the parser's join *and* again in the
  chunker), so every page after a blank verso was renumbered one too low.

The third-party text extractor (``pymupdf4llm.to_markdown``) is stubbed:
it is not what these tests are about, and its built-in OCR pass makes real
extraction depend on Tesseract language data being installed. Everything
downstream of it — the image walk, the join, the real ``pdf-page`` chunker
— runs for real, against real PDFs carrying real embedded images.
"""

from __future__ import annotations

import re
import struct
import zlib

import pytest

from anila_core.ingestion.chunking_plugins import get_chunker
import anila_core.ingestion.parser_registry as parser_registry

fitz = pytest.importorskip("fitz", reason="pymupdf not installed")
pymupdf4llm = pytest.importorskip("pymupdf4llm", reason="pymupdf4llm not installed")


# ── fixtures ────────────────────────────────────────────────────────────────


def _png(rgb: tuple[int, int, int]) -> bytes:
    """Smallest valid PNG in the given colour (distinct colour = distinct xref)."""
    w = h = 8
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _build_pdf(path, n_pages: int, images_per_page: int) -> str:
    """A real PDF: ``n_pages`` pages, each carrying ``images_per_page`` images.

    Colours differ per image so PyMuPDF cannot collapse them to one xref —
    the bug scales with the number of *extracted* images, so deduplicated
    fixtures would hide it.
    """
    doc = fitz.open()
    for pno in range(1, n_pages + 1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), f"PAGE-MARKER-{pno}", fontsize=12)
        for idx in range(images_per_page):
            page.insert_image(
                fitz.Rect(500, 95 + idx * 22, 514, 109 + idx * 22),
                stream=_png((pno * 7 % 256, idx * 53 % 256, 99)),
            )
    doc.save(str(path))
    doc.close()
    return str(path)


def _page_texts(n_pages: int) -> list[str]:
    """What the stubbed extractor returns — one markdown blob per page."""
    return [f"PAGE-MARKER-{p}\n\nBody text of page {p}.\n" for p in range(1, n_pages + 1)]


@pytest.fixture
def stub_extractor(monkeypatch):
    """Replace ``pymupdf4llm.to_markdown`` with a deterministic per-page stub.

    Also pins the parser's own OCR fallback to "no backend": OCR replaces
    ``content`` wholesale and would erase the markers under test.
    """

    def install(pages: list[str]):
        def fake_to_markdown(file_path, *args, **kwargs):
            assert kwargs.get("page_chunks") is True, (
                "PdfParser must request per-page chunks — without them there "
                "are no page boundaries to mark"
            )
            return [{"text": t} for t in pages]

        monkeypatch.setattr(pymupdf4llm, "to_markdown", fake_to_markdown)
        monkeypatch.setattr(parser_registry.PdfParser, "_ocr_initialised", True)
        monkeypatch.setattr(parser_registry.PdfParser, "_ocr_backend", None)

    return install


def _parse_and_chunk(path: str):
    parsed = parser_registry.PdfParser().parse(path)
    chunks = get_chunker("pdf-page").chunk(parsed.content, {}, {})
    return parsed, chunks


# ── the multiplier bug ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n_pages,images_per_page",
    [
        (1, 2),   # measured as 3 pages before the fix
        (3, 2),   # measured as 9 pages, 2 of 3 citations wrong
        (5, 1),   # measured as 10 pages
        (10, 3),  # measured as 40 pages, real page 10 cited as "page 37 of 40"
        (7, 5),
        (2, 1),
        (4, 0),   # control: no images, must be unaffected
        (1, 0),
    ],
)
def test_page_count_does_not_scale_with_image_count(
    tmp_path, stub_extractor, n_pages, images_per_page
) -> None:
    """N pages × M images is N pages. Before the fix it was N×(1+M)."""
    stub_extractor(_page_texts(n_pages))
    path = _build_pdf(tmp_path / "spec.pdf", n_pages, images_per_page)

    parsed, chunks = _parse_and_chunk(path)

    # The images really are in the document — otherwise this fixture would
    # pass for the wrong reason (nothing to multiply by).
    assert parsed.metadata["embedded_images"] == n_pages * images_per_page
    assert parsed.metadata["pages"] == n_pages
    # The invariant the chunker depends on, stated directly.
    assert parsed.content.count("\f") == n_pages - 1
    assert len(chunks) == n_pages
    assert all(c.metadata["total_pages"] == n_pages for c in chunks)


@pytest.mark.parametrize("n_pages,images_per_page", [(3, 2), (10, 3), (7, 5)])
def test_cited_page_number_matches_the_text_on_that_page(
    tmp_path, stub_extractor, n_pages, images_per_page
) -> None:
    """The citation-facing field: chunk N's body is really page N's body.

    A page-count assertion alone can pass while the pages are off by a
    constant, which is precisely how a reader ends up on the wrong page.
    """
    stub_extractor(_page_texts(n_pages))
    path = _build_pdf(tmp_path / "spec.pdf", n_pages, images_per_page)

    _parsed, chunks = _parse_and_chunk(path)

    seen = {}
    for c in chunks:
        marker = re.search(r"PAGE-MARKER-(\d+)", c.content)
        assert marker, f"page {c.metadata['page']} lost its marker text"
        seen[c.metadata["page"]] = int(marker.group(1))
    assert seen == {p: p for p in range(1, n_pages + 1)}


def test_image_placeholders_stay_inside_their_own_page(
    tmp_path, stub_extractor
) -> None:
    """Every ``[[IMAGE:…]]`` lands in the chunk for the page it came from."""
    stub_extractor(_page_texts(4))
    path = _build_pdf(tmp_path / "spec.pdf", 4, 2)

    parsed, chunks = _parse_and_chunk(path)

    by_page = {c.metadata["page"]: c.content for c in chunks}
    for image_id, ref in parsed.images.items():
        assert f"[[IMAGE:{image_id}]]" in by_page[ref.page], (
            f"image {image_id} is recorded on page {ref.page} but its "
            f"placeholder is not in that page's chunk"
        )


# ── documents without images must be untouched ──────────────────────────────


@pytest.mark.parametrize("n_pages", [1, 3, 8])
def test_no_image_pdf_output_is_byte_identical_to_the_old_join(
    tmp_path, stub_extractor, n_pages
) -> None:
    """For image-free PDFs the fix must be a no-op, byte for byte.

    ``expected`` is the *pre-fix* formula spelled out literally; if the new
    code path drifts by so much as a newline this fails.
    """
    pages = _page_texts(n_pages)
    stub_extractor(pages)
    path = _build_pdf(tmp_path / "plain.pdf", n_pages, 0)

    parsed, _chunks = _parse_and_chunk(path)

    expected = "\f\n".join(p.rstrip() for p in pages)
    assert parsed.content == expected


def test_non_pdf_text_is_not_split_into_pages() -> None:
    """No ``\\f`` in, one page out — the chunker must not invent boundaries."""
    text = "A plain document.\n\nNo page markers anywhere in this text."
    chunks = get_chunker("pdf-page").chunk(text, {}, {})
    assert len(chunks) == 1
    assert chunks[0].metadata == {"page": 1, "total_pages": 1, "strategy": "pdf-page"}
    assert chunks[0].content == text


# ── a form feed carried by the page's own text ──────────────────────────────


def test_form_feed_inside_page_text_is_not_a_page_boundary(
    tmp_path, stub_extractor
) -> None:
    """PDF text streams can contain U+000C; it must not become a page.

    Decision: the parser rewrites in-page ``\\f`` to ``\\n`` rather than
    escaping it. A form feed is a vertical whitespace control character —
    turning it into a newline preserves the only thing it meant for the
    reader, and keeps the delimiter unambiguous for every consumer that
    splits on it. Nothing downstream renders U+000C, so nothing is lost.
    """
    pages = ["page one\fstill page one", "page two", "page\fthree\fbody"]
    stub_extractor(pages)
    path = _build_pdf(tmp_path / "ff.pdf", 3, 1)

    parsed, chunks = _parse_and_chunk(path)

    assert parsed.content.count("\f") == 2, "only the 2 real boundaries survive"
    assert len(chunks) == 3
    assert "still page one" in chunks[0].content
    assert "three" in chunks[2].content and "body" in chunks[2].content


# ── blank pages keep their slot ─────────────────────────────────────────────


def test_blank_page_does_not_renumber_the_pages_after_it(
    tmp_path, stub_extractor
) -> None:
    """A blank verso is still a page. Dropping it shifts every later citation."""
    stub_extractor(["PAGE-MARKER-1 intro", "   ", "PAGE-MARKER-3 conclusion"])
    # 0 images on purpose: a page carrying only an image is *not* blank —
    # its placeholder is content, and it must still produce a chunk.
    path = _build_pdf(tmp_path / "blank.pdf", 3, 0)

    _parsed, chunks = _parse_and_chunk(path)

    # The blank page has nothing to embed, so it yields no chunk...
    assert len(chunks) == 2
    # ...but it still occupies page 2, so the conclusion is page 3 of 3.
    assert [c.metadata["page"] for c in chunks] == [1, 3]
    assert all(c.metadata["total_pages"] == 3 for c in chunks)
    assert "PAGE-MARKER-3" in chunks[-1].content


def test_page_holding_only_an_image_still_produces_a_chunk(
    tmp_path, stub_extractor
) -> None:
    """A textless page carrying a figure is content, not a blank page.

    Image-heavy specs have these: a full-page diagram with no caption. It
    must stay retrievable, and its page number must stay correct.
    """
    stub_extractor(["PAGE-MARKER-1 intro", "", "PAGE-MARKER-3 conclusion"])
    path = _build_pdf(tmp_path / "figure.pdf", 3, 1)

    parsed, chunks = _parse_and_chunk(path)

    assert [c.metadata["page"] for c in chunks] == [1, 2, 3]
    figure_page = next(c for c in chunks if c.metadata["page"] == 2)
    image_id = next(i for i, ref in parsed.images.items() if ref.page == 2)
    assert f"[[IMAGE:{image_id}]]" in figure_page.content


def test_chunker_counts_blank_pages_without_emitting_empty_chunks() -> None:
    """Chunker-level statement of the same rule, independent of the parser."""
    chunks = get_chunker("pdf-page").chunk("first\f\f\fLAST", {}, {})
    assert [c.metadata["page"] for c in chunks] == [1, 4]
    assert all(c.metadata["total_pages"] == 4 for c in chunks)
    assert all(c.content.strip() for c in chunks), "no empty chunk may be emitted"


def test_wholly_blank_document_yields_no_chunks() -> None:
    assert get_chunker("pdf-page").chunk("\f\f  \f", {}, {}) == []


# ── when the marker cannot be trusted, say so instead of guessing ───────────
#
# ``PdfParser`` replaces ``content`` wholesale on OCR fallback, keeping the
# native pass's page count. So a 4-page scan comes out of the parser as one
# unmarked blob still labelled ``pages=4``. Neither half is wrong on its own;
# only comparing them catches it. These tests pin that comparison at both
# seams — the flag in ``extract_text``, and the warning in the chunker. The
# OCR path itself belongs to another package and is not touched here.


class _StubOcrBackend:
    """Stands in for an ``OcrBackend``; ``OcrBackend`` is an open Protocol."""

    def __init__(self, text: str) -> None:
        self._text = text

    def extract(self, file_path: str) -> str:  # noqa: D102
        return self._text


@pytest.fixture
def force_ocr(monkeypatch):
    """Make the parser's OCR fallback fire and return the given text."""

    def install(ocr_text: str):
        monkeypatch.setattr(parser_registry.PdfParser, "_ocr_initialised", True)
        monkeypatch.setattr(
            parser_registry.PdfParser, "_ocr_backend", _StubOcrBackend(ocr_text)
        )
        # ``parse()`` imports this lazily from the module, so patching the
        # module attribute is what reaches it.
        import anila_core.ingestion.ocr as ocr_mod

        monkeypatch.setattr(ocr_mod, "needs_ocr_fallback", lambda _text: True)

    return install


@pytest.mark.parametrize(
    "ocr_text,why",
    [
        ("scanned page one two three four", "backend output has no \\f at all"),
        ("one\ftwo\fthree", "backend output carries its own \\f (Tesseract does)"),
    ],
)
def test_ocr_replacement_turns_the_page_boundary_flag_off(
    tmp_path, stub_extractor, force_ocr, ocr_text, why
) -> None:
    """``has_page_boundaries`` is measured, so it fails safe when OCR fires.

    The parser still reports ``pages=4`` — that is not this package's to fix.
    What must not happen is the flag claiming the text is page-delimited when
    its field count disagrees, because that is what puts a wrong page number
    in front of a reader.
    """
    from anila_core.ingestion.parsers import extract_text

    stub_extractor(_page_texts(4))
    path = _build_pdf(tmp_path / "scan.pdf", 4, 0)
    force_ocr(ocr_text)

    text, metadata, _images = extract_text("scan.pdf", open(path, "rb").read(), None)

    assert metadata["ocr_used"] is True
    assert metadata["page_count"] == 4
    assert len(text.split("\f")) != 4
    assert metadata["has_page_boundaries"] is False, why


def test_page_boundary_flag_stays_on_for_a_normal_pdf(
    tmp_path, stub_extractor
) -> None:
    """The failsafe must not fire on the healthy path (else it is useless)."""
    from anila_core.ingestion.parsers import extract_text

    stub_extractor(_page_texts(4))
    path = _build_pdf(tmp_path / "spec.pdf", 4, 3)

    text, metadata, _images = extract_text("spec.pdf", open(path, "rb").read(), None)

    assert metadata["has_page_boundaries"] is True
    assert len(text.split("\f")) == metadata["page_count"] == 4


def test_chunker_warns_when_text_contradicts_the_declared_page_count(caplog) -> None:
    """Disagreeing case: loud."""
    with caplog.at_level("WARNING"):
        chunks = get_chunker("pdf-page").chunk(
            "one big OCR blob with no page markers",
            {"page_count": 4, "ocr_used": True, "format": "pdf"},
            {},
        )
    assert len(chunks) == 1
    assert chunks[0].metadata["total_pages"] == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "page_count=4" in message and "1 page field" in message


@pytest.mark.parametrize(
    "meta",
    [
        {"page_count": 3, "ocr_used": False},  # agrees
        {},                                     # nothing declared
        {"page_count": None},                   # declared but unknown
        {"format": "txt"},                      # non-PDF source, no count
    ],
)
def test_chunker_is_silent_when_there_is_nothing_to_contradict(caplog, meta) -> None:
    """Agreeing (or absent) case: silent. A warning nobody can act on is noise."""
    with caplog.at_level("WARNING"):
        chunks = get_chunker("pdf-page").chunk("one\ftwo\fthree", meta, {})
    assert len(chunks) == 3
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


def test_single_page_document_reports_no_page_boundaries(
    tmp_path, stub_extractor
) -> None:
    """Documented rule: the flag means *separators between pages*, so a
    1-page PDF is False even though its single field trivially matches
    ``page_count``. Pinned as the intended rule, not an accident of ``> 1``.
    """
    from anila_core.ingestion.parsers import extract_text

    stub_extractor(_page_texts(1))
    path = _build_pdf(tmp_path / "one.pdf", 1, 2)

    text, metadata, _images = extract_text("one.pdf", open(path, "rb").read(), None)

    assert metadata["page_count"] == 1
    assert len(text.split("\f")) == 1
    assert metadata["has_page_boundaries"] is False


# ── the only check against the physical document ────────────────────────────
#
# Everything above compares the extractor to itself. If the extractor
# under-reports pages, the parser, the flag and the chunker all agree with
# each other and are wrong together: a real 5-page PDF read as 3 tells the
# reader 「第 3 頁／共 3 頁」 with nothing objecting. ``fitz`` is already open
# in ``parse()``, so the physical count costs nothing.


def test_parser_warns_when_the_extractor_under_reports_pages(
    tmp_path, stub_extractor, caplog
) -> None:
    """Disagreeing case: loud. Real 5-page PDF, extractor returns 3."""
    stub_extractor(_page_texts(3))
    path = _build_pdf(tmp_path / "under.pdf", 5, 1)

    with caplog.at_level("WARNING"):
        parsed = parser_registry.PdfParser().parse(path)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "returned 3 page(s)" in warnings[0]
    assert "document has 5" in warnings[0]
    # Warn, don't fail the document — the text extracted is still useful.
    assert parsed.metadata["pages"] == 3


@pytest.mark.parametrize(
    "n_pages,images_per_page,blank_first",
    [
        (1, 0, False),   # single page
        (1, 2, False),   # single page, image-heavy
        (3, 0, True),    # blank first page
        (5, 1, False),
        (10, 3, False),  # image-heavy
        (7, 5, False),
        (4, 0, False),
        (2, 1, True),
    ],
)
def test_parser_is_silent_for_documents_the_extractor_reads_correctly(
    tmp_path, stub_extractor, caplog, n_pages, images_per_page, blank_first
) -> None:
    """Every normal shape stays silent — a guard that cries wolf is worse than
    none. Covers single-page, blank-first and image-heavy."""
    pages = _page_texts(n_pages)
    if blank_first:
        pages[0] = "   "
    stub_extractor(pages)
    path = _build_pdf(tmp_path / "ok.pdf", n_pages, images_per_page)

    with caplog.at_level("WARNING"):
        parsed = parser_registry.PdfParser().parse(path)

    assert parsed.metadata["pages"] == n_pages
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []

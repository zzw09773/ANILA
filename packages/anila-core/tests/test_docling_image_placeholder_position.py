"""Remote docling: ``[[IMAGE:id]]`` must land where the figure actually is.

Live defect (doc 58, 14 figures): the remote reconstruct appended every
placeholder at the end of the markdown. Hierarchical chunking then put
every ``image_pks`` on the last leaf (「選手標記」). The swimming
section had the figure in the source PDF and a leftover ``<!-- image -->``
comment, and zero ``image_pks``.

Invariant:
  1. Each placeholder replaces the next ``<!-- image -->`` in document
     order.
  2. Visible text no longer contains the raw HTML comment.
  3. Marker-count ≠ image-count does not drop figures: leftovers append
     at the end (today's behaviour) and a log is written.

The unfixed reconstruct (``content = markdown + all placeholders``)
fails 1 and 2 on any markdown that still carries the comments.

Mutant that must go red: stitch every placeholder at the tail again.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx
import pytest
import respx

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.docling_parser import RemoteDoclingParser

DOCLING_URL = "http://docling:9100"
TOKEN = "docling-token-abc123"

_TINY_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _allow_http(monkeypatch) -> None:
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "docling")


def _write_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def _img(img_id: str, page: int) -> dict:
    return {
        "id": img_id,
        "page": page,
        "caption": "",
        "png_b64": _TINY_PNG,
    }


def _parse(tmp_path, monkeypatch, markdown: str, images: list[dict]):
    _allow_http(monkeypatch)
    parser = RemoteDoclingParser(base_url=DOCLING_URL, token=TOKEN)
    payload = {
        "markdown": markdown,
        "title": "contest guide",
        "page_count": 4,
        "ocr_applied": False,
        "images": images,
    }
    with respx.mock:
        respx.post("http://docling:9100/parse").mock(
            return_value=httpx.Response(200, json=payload)
        )
        return parser.parse(str(_write_pdf(tmp_path)))


_TWO_SECTION = (
    "# 游泳賽段\n\n"
    "出發後進入水域。\n\n"
    "<!-- image -->\n\n"
    "請注意泳道標記。\n\n"
    "# 選手標記\n\n"
    "號碼布縫在左側。\n\n"
    "<!-- image -->\n"
)


def test_unfixed_reconstruct_dumps_every_placeholder_at_the_tail(tmp_path, monkeypatch):
    """RED on the current code: both tokens sit after 選手標記.

    Once the fix lands this assertion is inverted by the next test;
    keep this as the documented pre-fix shape so a revert to
    ``markdown + join(placeholders)`` is visible as a behaviour flip.
    """
    parsed = _parse(
        tmp_path, monkeypatch, _TWO_SECTION,
        [_img("swim", 1), _img("bib", 2)],
    )
    before_last, _, after_last_heading = parsed.content.rpartition("# 選手標記")
    assert "[[IMAGE:swim]]" in parsed.content
    assert "[[IMAGE:bib]]" in parsed.content
    dumped_at_tail = (
        "[[IMAGE:swim]]" in after_last_heading
        and "[[IMAGE:bib]]" in after_last_heading
        and "[[IMAGE:swim]]" not in before_last
    )
    # This file's job is to go RED on the unfixed code. The unfixed
    # reconstruct makes dumped_at_tail True; the fix makes it False.
    assert not dumped_at_tail, (
        "placeholders were all appended after the last heading — "
        "swimming section has no [[IMAGE:]] of its own"
    )


def test_placeholders_replace_html_comments_in_document_order(tmp_path, monkeypatch):
    parsed = _parse(
        tmp_path, monkeypatch, _TWO_SECTION,
        [_img("swim", 1), _img("bib", 2)],
    )
    assert "<!-- image -->" not in parsed.content
    swim, _, rest = parsed.content.partition("# 選手標記")
    assert "[[IMAGE:swim]]" in swim
    assert "[[IMAGE:bib]]" in rest
    assert "[[IMAGE:bib]]" not in swim
    assert "[[IMAGE:swim]]" not in rest
    assert parsed.content.index("[[IMAGE:swim]]") < parsed.content.index("# 選手標記")
    assert parsed.content.index("# 選手標記") < parsed.content.index("[[IMAGE:bib]]")


def test_image_token_lands_in_the_section_chunk_not_the_last_leaf(
    tmp_path, monkeypatch,
):
    """Main acceptance: chunker attribution, not just string position."""
    parsed = _parse(
        tmp_path, monkeypatch, _TWO_SECTION,
        [_img("swim", 1), _img("bib", 2)],
    )
    chunker = get_chunker("hierarchical")
    chunks = chunker.chunk(parsed.content, parsed.metadata, {})
    leaves = [c for c in chunks if c.metadata.get("chunk_type") == "leaf"]
    swim_leaves = [c for c in leaves if "游泳" in c.content or "水域" in c.content]
    bib_leaves = [c for c in leaves if "號碼布" in c.content or "選手標記" in c.content]
    assert swim_leaves, [c.content[:80] for c in leaves]
    assert any("[[IMAGE:swim]]" in c.content for c in swim_leaves), (
        "swim figure did not land in the swimming section chunk"
    )
    assert all("[[IMAGE:swim]]" not in c.content for c in bib_leaves)
    last = leaves[-1]
    assert "[[IMAGE:swim]]" not in last.content or "水域" in last.content


def test_more_markers_than_images_without_ordinals_degrades_to_the_tail(tmp_path, monkeypatch):
    """2026-09-02 (P-1 ruling): counts disagree and ``only`` carries no ordinal,
    so no marker can be justified for it. The old expectation (first marker
    wins) was exactly the silent shift P-1 is about. Degrade: markers gone,
    figure appended at the end. Ordinal-carrying ids are covered in
    ``test_docling_image_skipped_middle.py``."""
    parsed = _parse(
        tmp_path, monkeypatch, _TWO_SECTION,
        [_img("only", 1)],
    )
    assert parsed.content.count("[[IMAGE:only]]") == 1
    assert "<!-- image -->" not in parsed.content
    assert parsed.content.index("[[IMAGE:only]]") > parsed.content.index("號碼布縫在左側。")


def test_more_images_than_markers_appends_leftovers_and_logs(
    tmp_path, monkeypatch, caplog
):
    """2026-09-02 (P-1 ruling): one marker, two ordinal-less images — the
    mapping is not established, so *neither* is placed by guess; both are
    appended and the log says why. Before: ``placed`` took the marker."""
    one_marker = "# 游泳賽段\n\n<!-- image -->\n\n請注意泳道標記。\n"
    with caplog.at_level(logging.WARNING):
        parsed = _parse(
            tmp_path, monkeypatch, one_marker,
            [_img("placed", 1), _img("extra", 2)],
        )
    assert "<!-- image -->" not in parsed.content
    tail = parsed.content[parsed.content.index("請注意泳道標記。") :]
    assert "[[IMAGE:placed]]" in tail and "[[IMAGE:extra]]" in tail
    assert any("not established" in r.getMessage() for r in caplog.records)


def test_zero_markers_zero_images_is_unchanged(tmp_path, monkeypatch):
    md = "# 只有文字\n\n沒有圖。\n"
    parsed = _parse(tmp_path, monkeypatch, md, [])
    assert parsed.content == md.strip()
    assert parsed.images == {}
    assert "[[IMAGE:" not in parsed.content
    assert "<!-- image -->" not in parsed.content


def test_images_without_markers_still_append_at_end(tmp_path, monkeypatch, caplog):
    md = "# 游泳賽段\n\n出發。\n\n# 選手標記\n\n號碼布。\n"
    with caplog.at_level(logging.WARNING, logger="anila_core.ingestion.docling_parser"):
        parsed = _parse(
            tmp_path, monkeypatch, md,
            [_img("a", 1), _img("b", 2)],
        )
    assert parsed.content.endswith("[[IMAGE:a]]\n\n[[IMAGE:b]]") or (
        parsed.content.rstrip().endswith("[[IMAGE:b]]")
        and "[[IMAGE:a]]" in parsed.content.rsplit("# 選手標記", 1)[-1]
    )
    assert "[[IMAGE:a]]" not in parsed.content.split("# 選手標記", 1)[0]
    assert any("marker" in r.getMessage().lower() for r in caplog.records)


def test_existing_single_image_without_comment_still_appends(tmp_path, monkeypatch):
    """Regression for test_reconstructs_document_with_images's payload shape."""
    parsed = _parse(
        tmp_path, monkeypatch, "# Title\n\nbody text",
        [_img("img1", 2)],
    )
    assert parsed.content == "# Title\n\nbody text\n\n[[IMAGE:img1]]"

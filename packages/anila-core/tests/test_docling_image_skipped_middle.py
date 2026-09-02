"""P-1 (MEDIUM): the service may skip a figure; the mapping must not shift.

``docling-service`` ``_collect_pictures`` has two ``continue`` paths (conversion
raised / empty PNG). A skipped picture never reaches ``images`` — but the
markdown still carries its ``<!-- image -->``. Positional mapping ("N-th
marker ↔ N-th delivered image") then shifts every later figure one section
up: a confident, wrong placement (image, caption vector and retrieval all land
on the wrong paragraph).

The service already encodes the *original* ordinal in the id
(``img_0001``, ``img_0002`` … assigned after the ``continue``s, so a skipped
picture shows as a gap). This test injects the skip (the ruling says: do not
chase a CMYK PDF, that path was shown not to exist) and requires either a
correct mapping from the ordinals or an explicit degrade — never a silent
shift.

Invariant: ``marker_count != len(images)`` ⇒ positional mapping is not
established; the parser must not claim positions it cannot justify.
"""

from __future__ import annotations

import logging

from tests.test_docling_image_placeholder_position import _img, _parse

_THREE_SECTION = (
    "# 泳渡\n\n"
    "出發後進入水域。\n\n"
    "<!-- image -->\n\n"
    "# 自行車\n\n"
    "轉換區在北側。\n\n"
    "<!-- image -->\n\n"
    "# 選手標記\n\n"
    "號碼布縫在左側。\n\n"
    "<!-- image -->\n"
)


def _section(content: str, heading: str) -> str:
    start = content.index(heading)
    nxt = [content.index(h) for h in ("# 泳渡", "# 自行車", "# 選手標記") if content.index(h) > start]
    return content[start : min(nxt)] if nxt else content[start:]


def test_service_skipped_middle_picture_does_not_shift_the_rest(tmp_path, monkeypatch):
    """Reviewer's three-section demo: the service skipped the *second* picture.

    Unfixed: ``[[IMAGE:img_0001]]`` under 泳渡 (right), ``[[IMAGE:img_0003]]``
    under 自行車 (WRONG — that section's figure was the skipped one), 選手標記
    empty. Kill: revert to ordinal-blind sequential mapping → red.
    """
    parsed = _parse(
        tmp_path, monkeypatch, _THREE_SECTION,
        [_img("img_0001", 1), _img("img_0003", 3)],
    )
    content = parsed.content
    assert "<!-- image -->" not in content
    assert "[[IMAGE:img_0001]]" in _section(content, "# 泳渡")
    assert "[[IMAGE:" not in _section(content, "# 自行車"), _section(content, "# 自行車")
    assert "[[IMAGE:img_0003]]" in _section(content, "# 選手標記")


def test_service_skipped_first_picture(tmp_path, monkeypatch):
    parsed = _parse(
        tmp_path, monkeypatch, _THREE_SECTION,
        [_img("img_0002", 2), _img("img_0003", 3)],
    )
    content = parsed.content
    assert "[[IMAGE:" not in _section(content, "# 泳渡")
    assert "[[IMAGE:img_0002]]" in _section(content, "# 自行車")
    assert "[[IMAGE:img_0003]]" in _section(content, "# 選手標記")


def test_ids_without_ordinals_and_a_count_mismatch_degrade_explicitly(
    tmp_path, monkeypatch, caplog
):
    """No ordinal to map by and the counts disagree: positions cannot be
    justified. Degrade = strip the markers and append the figures at the end
    (they stay retrievable) and say so in the log — not "unused markers
    dropped", which describes a shifted mapping as a tail-end leftover."""
    with caplog.at_level(logging.WARNING):
        parsed = _parse(
            tmp_path, monkeypatch, _THREE_SECTION,
            [_img("swim", 1), _img("bib", 3)],
        )
    content = parsed.content
    assert "<!-- image -->" not in content
    # nothing placed inside any section — placement is unjustified
    for heading in ("# 泳渡", "# 自行車"):
        assert "[[IMAGE:" not in _section(content, heading), heading
    # both figures survive, at the tail, after the last section's text
    tail = content[content.index("號碼布縫在左側。") :]
    assert "[[IMAGE:swim]]" in tail and "[[IMAGE:bib]]" in tail
    assert any("position" in r.getMessage() and "not established" in r.getMessage() for r in caplog.records)
    assert not any("unused markers dropped" in r.getMessage() for r in caplog.records)


def test_markers_present_but_zero_images_strips_markers(tmp_path, monkeypatch):
    """P-2: whole-batch conversion failure. Early-return path; keep it covered."""
    parsed = _parse(tmp_path, monkeypatch, _THREE_SECTION, [])
    assert "<!-- image -->" not in parsed.content
    assert "[[IMAGE:" not in parsed.content
    assert "# 選手標記" in parsed.content


def test_counts_match_keeps_the_ordinal_mapping_too(tmp_path, monkeypatch):
    """Sanity: with all three delivered the ordinal path equals the old path."""
    parsed = _parse(
        tmp_path, monkeypatch, _THREE_SECTION,
        [_img("img_0001", 1), _img("img_0002", 2), _img("img_0003", 3)],
    )
    content = parsed.content
    assert "[[IMAGE:img_0001]]" in _section(content, "# 泳渡")
    assert "[[IMAGE:img_0002]]" in _section(content, "# 自行車")
    assert "[[IMAGE:img_0003]]" in _section(content, "# 選手標記")

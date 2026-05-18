"""Studio Fix 3 — graceful degradation when LLM under-fills.

The schema floor (Stat.supporting min 20, Column.bullets min 3) is
strict, but in practice gemma4 misses one or the other on the first
attempt. `_saturate_spec_dict` runs before Pydantic validation and:

  * Auto-fills `Stat.supporting` with a 來源:<filename> placeholder
    when missing or shorter than the 20-char floor.
  * Demotes `two_column` slides with any column under 3 bullets to
    `standard` layout, flattening the columns' bullets into the
    slide's top-level bullets list.

The whole point is that the user never sees a 422 over either of these
recoverable shapes. Both transforms log a warning so we can monitor.
"""
from __future__ import annotations

import logging
from typing import Any

from app.api.studio import _saturate_spec_dict
from app.schemas.studio import SlidesSpec


# ── Stat.supporting auto-fill ────────────────────────────────────────────


def _base_slide() -> dict[str, Any]:
    """Minimal valid slide skeleton (standard layout). Tests override
    the bits they care about."""
    return {
        "title": "測試投影片",
        "bullets": ["一", "二", "三"],
        "layout_kind": "standard",
    }


def test_stat_missing_supporting_auto_filled() -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "title": "重點數據",
                "layout_kind": "stat_callout",
                "stat": {"value": "95%", "label": "準確率"},
            }
        ]
    }
    _saturate_spec_dict(spec, chunk_filenames=["paper.pdf"])
    supporting = spec["slides"][0]["stat"]["supporting"]
    assert "paper.pdf" in supporting
    assert len(supporting) >= 20


def test_stat_short_supporting_auto_filled() -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "layout_kind": "stat_callout",
                "stat": {
                    "value": "95%",
                    "label": "準確率",
                    "supporting": "重要突破",  # 4 chars — well under 20
                },
            }
        ]
    }
    _saturate_spec_dict(spec, chunk_filenames=["report.docx"])
    supporting = spec["slides"][0]["stat"]["supporting"]
    assert "report.docx" in supporting


def test_stat_ok_supporting_left_untouched() -> None:
    long_supporting = (
        "雙分支架構相比單分支基準的 78%，提升 17 個百分點；測試集 N=2,400"
    )
    spec = {
        "slides": [
            {
                **_base_slide(),
                "layout_kind": "stat_callout",
                "stat": {
                    "value": "95%",
                    "label": "準確率",
                    "supporting": long_supporting,
                },
            }
        ]
    }
    _saturate_spec_dict(spec, chunk_filenames=["x.pdf"])
    assert spec["slides"][0]["stat"]["supporting"] == long_supporting


def test_stat_fallback_when_no_chunk_filename() -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "layout_kind": "stat_callout",
                "stat": {"value": "95%", "label": "準確率"},
            }
        ]
    }
    _saturate_spec_dict(spec, chunk_filenames=[])
    assert "documents" in spec["slides"][0]["stat"]["supporting"]


# ── two_column demotion ──────────────────────────────────────────────────


def test_two_column_sparse_demoted_to_standard() -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "title": "對照",
                "layout_kind": "two_column",
                "columns": [
                    {"heading": "優點", "bullets": ["快"]},  # 1 bullet
                    {"heading": "缺點", "bullets": ["貴", "重"]},  # 2
                ],
            }
        ]
    }
    _saturate_spec_dict(spec)
    slide = spec["slides"][0]
    assert slide["layout_kind"] == "standard"
    assert "columns" not in slide
    # Flattened bullets carry the heading prefix.
    bullets_text = " ".join(slide["bullets"])
    assert "優點：快" in bullets_text
    assert "缺點：貴" in bullets_text
    assert "缺點：重" in bullets_text


def test_two_column_full_columns_left_untouched() -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "layout_kind": "two_column",
                "columns": [
                    {"heading": "A", "bullets": ["1", "2", "3"]},
                    {"heading": "B", "bullets": ["4", "5", "6"]},
                ],
            }
        ]
    }
    _saturate_spec_dict(spec)
    slide = spec["slides"][0]
    assert slide["layout_kind"] == "two_column"
    assert len(slide["columns"]) == 2


# ── End-to-end: under-filled spec roundtrips through Pydantic ────────────


def test_saturated_under_filled_spec_validates_via_pydantic() -> None:
    """The whole point of `_saturate_spec_dict` is to prevent 422. So
    take a deliberately under-filled spec, run saturation, then assert
    SlidesSpec.model_validate succeeds."""
    spec = {
        "title": "測試簡報",
        "palette": "navy_amber",
        "slides": [
            {
                "title": "封面",
                "bullets": ["副標一行"],
                "layout_kind": "section_break",
            },
            {
                "title": "關鍵數據",
                "bullets": ["主要發現"],
                "layout_kind": "stat_callout",
                # supporting omitted → schema would reject, saturation patches
                "stat": {"value": "88%", "label": "準確率"},
            },
            {
                "title": "對照",
                "bullets": ["說明"],
                "layout_kind": "two_column",
                "columns": [
                    {"heading": "A", "bullets": ["只有一條"]},
                    {"heading": "B", "bullets": ["也只有一條"]},
                ],
            },
        ],
    }
    _saturate_spec_dict(spec, chunk_filenames=["src.pdf"])
    # Should now validate without raising.
    validated = SlidesSpec.model_validate(spec)
    assert len(validated.slides) == 3
    assert validated.slides[1].layout_kind == "stat_callout"
    assert validated.slides[1].stat is not None
    assert "src.pdf" in validated.slides[1].stat.supporting
    # two_column should have been demoted
    assert validated.slides[2].layout_kind == "standard"
    assert validated.slides[2].columns is None


# ── Defensive: malformed input doesn't raise ─────────────────────────────


def test_saturate_handles_non_dict_input() -> None:
    # Non-dict input is returned unchanged; Pydantic raises downstream.
    assert _saturate_spec_dict("not a dict") == "not a dict"
    assert _saturate_spec_dict(None) is None


def test_saturate_handles_missing_slides_key() -> None:
    spec = {"title": "x"}
    _saturate_spec_dict(spec)
    assert spec == {"title": "x"}


# ── Logging: warnings are emitted on patches ─────────────────────────────


def test_warning_logged_on_stat_autofill(caplog: Any) -> None:
    spec = {
        "slides": [
            {
                **_base_slide(),
                "layout_kind": "stat_callout",
                "stat": {"value": "95%", "label": "準確率"},
            }
        ]
    }
    with caplog.at_level(logging.WARNING, logger="app.api.studio"):
        _saturate_spec_dict(spec, chunk_filenames=["doc.pdf"])
    assert any("saturation" in r.message for r in caplog.records)

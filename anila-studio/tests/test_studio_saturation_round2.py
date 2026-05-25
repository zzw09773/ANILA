"""Round 2 Patch C regression tests — sparse two_column upgrades to icon_rows.

Round 1 Fix 3 raised `Column.bullets` min_length to 3, which caused
v2 slide 5 (雙分支特徵融合) — a legitimate 2-bullets-per-column
comparison — to get demoted to standard layout. Round 2 Patch C
drops the floor back to 2 AND changes the demote target for genuinely
sparse two_column slides from `standard` to `icon_rows`, preserving
the side-by-side framing.
"""
from __future__ import annotations

from app.api.studio import _saturate_spec_dict


def test_two_column_with_2_bullets_each_stays_two_column() -> None:
    """min_length=2 means 2-bullet columns are now schema-valid; the
    saturation pass should not touch them. Regression for v2 slide 5
    (雙分支特徵融合)."""
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "雙分支特徵融合解決方案",
            "bullets": ["a", "b", "c"],
            "layout_kind": "two_column",
            "columns": [
                {"heading": "RGB 原圖", "bullets": ["捕捉形態", "空間資訊"]},
                {"heading": "Tsallis Entropy", "bullets": ["量化複雜度", "強化邊緣"]},
            ],
        }],
    }
    out = _saturate_spec_dict(spec_dict, chunk_filenames=["x.pdf"])
    assert out["slides"][0]["layout_kind"] == "two_column"
    assert len(out["slides"][0]["columns"]) == 2


def test_two_column_with_one_bullet_columns_upgrades_to_icon_rows() -> None:
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "對比一",
            "bullets": ["a"],
            "layout_kind": "two_column",
            "columns": [
                {"heading": "方法 A", "bullets": ["only one"]},
                {"heading": "方法 B", "bullets": ["only one"]},
            ],
        }],
    }
    out = _saturate_spec_dict(spec_dict, chunk_filenames=[])
    slide = out["slides"][0]
    assert slide["layout_kind"] == "icon_rows"
    assert len(slide["icon_rows"]) == 2
    assert slide["icon_rows"][0]["heading"] == "方法 A"
    assert slide["icon_rows"][0]["concept"] == "comparison"
    assert slide["icon_rows"][0]["description"] == "only one"
    assert slide["icon_rows"][1]["heading"] == "方法 B"
    assert "columns" not in slide


def test_two_column_with_missing_heading_falls_back_to_standard() -> None:
    """If column lacks heading we can't form a coherent icon_row;
    falls back to the original standard-flatten path."""
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "broken",
            "bullets": ["a"],
            "layout_kind": "two_column",
            "columns": [
                {"heading": "", "bullets": ["only"]},
                {"heading": "", "bullets": ["only"]},
            ],
        }],
    }
    out = _saturate_spec_dict(spec_dict, chunk_filenames=[])
    slide = out["slides"][0]
    assert slide["layout_kind"] == "standard"
    assert "columns" not in slide


def test_two_column_with_multibullet_joins_with_full_width_semicolon() -> None:
    """Multiple bullets in a sparse column get joined with `；` (U+FF1B)
    inside the resulting icon_row.description."""
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "joined",
            "bullets": ["a"],
            "layout_kind": "two_column",
            "columns": [
                # col A has 1 bullet → triggers
                {"heading": "A", "bullets": ["alpha"]},
                # col B has 2 bullets → joined
                {"heading": "B", "bullets": ["one", "two"]},
            ],
        }],
    }
    out = _saturate_spec_dict(spec_dict, chunk_filenames=[])
    slide = out["slides"][0]
    assert slide["layout_kind"] == "icon_rows"
    rows = slide["icon_rows"]
    assert rows[0]["description"] == "alpha"
    assert rows[1]["description"] == "one；two"

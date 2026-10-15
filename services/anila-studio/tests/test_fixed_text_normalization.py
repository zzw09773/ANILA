"""固定詞表、欄位型別與原文字元的處理邊界。"""
from __future__ import annotations

from app.services.studio_text_normalizer import _convert, apply_taiwan_terms


def test_fixed_terms_preserve_unlisted_script_and_legal_text():
    original = "视频软件 数据质量 程序保障 共同項目 王小明"
    assert _convert(original) == original
    assert apply_taiwan_terms("視頻軟件與信息網絡") == "影片軟體與資訊網路"
    assert _convert("$\\rightarrow$ 視頻 [1]") == "→ 影片"
    assert _convert("視頻 [1]", keep_citations=True) == "影片 [1]"


def test_report_and_infographic_share_fixed_terms_without_converter():
    from app.api.infographics import _normalize_string
    from app.services.report_runner import _convert_zh

    for normalize in (_normalize_string, _convert_zh):
        assert normalize("信息与视频 [1]") == "資訊与视频"
        assert normalize(None) is None
        assert normalize("") == ""


def test_datatable_keeps_cell_types_and_identifier_keys():
    from app.api.datatables import _normalize_spec_text
    from app.schemas.datatable import DatatableSpec

    source = DatatableSpec(
        title="视频與視頻", subtitle="软件與軟件", preset="key_figures",
        columns=[{"key": "視頻", "label": "視頻"}, {"key": "number", "label": "数量", "dtype": "number"}],
        rows=[{"cells": {"視頻": "信息与视频", "number": 7}},
              {"cells": {"視頻": None, "number": 0.5}}], notes="程序與質量",
    )
    result = _normalize_spec_text(source)
    assert result.title == "视频與影片"
    assert result.subtitle == "软件與軟體"
    assert result.columns[0].key == "視頻"
    assert result.columns[0].label == "影片"
    assert result.rows[0].cells == {"視頻": "資訊与视频", "number": 7}
    assert type(result.rows[0].cells["number"]) is int
    assert result.rows[1].cells == {"視頻": None, "number": 0.5}
    assert result.notes == "程序與質量"
    assert source.rows[0].cells["視頻"] == "信息与视频"


def test_mindmap_cleanup_preserves_original_script():
    from app.api.mindmaps import _normalise_label

    assert _normalise_label("视频软件 $\\rightarrow$ [1]") == "视频软件 →"

"""Schema-level tests for the datatable artifact pipeline.

Covers:
- DatatablePreset enum value contract (frontend mirrors these strings)
- DataColumn dtype / align validation (Literal enforcement)
- DataRow cell value union (str | int | float | None, bool coerced)
- DatatableSpec composite validation (column key uniqueness, count caps)

Boundary cases (None inputs, empty strings, oversized payloads) live here
so the runner / exporter modules can trust the spec once validated.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.datatable import (
    DataColumn,
    DataRow,
    DatatablePreset,
    DatatableSpec,
    GenerateDatatableRequest,
)


# ── DatatablePreset enum ──────────────────────────────────────────────────


def test_preset_enum_values_match_contract():
    """The string values are part of the wire contract — frontend
    switches on them, db tags use them, log searches grep for them.
    Changing one is a schema break, so this test pins them in place.
    """
    assert DatatablePreset.KEY_FIGURES.value == "key_figures"
    assert DatatablePreset.ENTITY_ATTRIBUTES.value == "entity_attributes"
    assert DatatablePreset.TIMELINE_TABLE.value == "timeline_table"
    assert DatatablePreset.COMPARISON_TABLE.value == "comparison_table"


def test_preset_round_trips_through_string():
    """`DatatablePreset("key_figures")` should yield the enum member, so
    a request body deserialised as a plain string still resolves to the
    typed enum without extra casting on the caller side.
    """
    assert DatatablePreset("key_figures") is DatatablePreset.KEY_FIGURES
    with pytest.raises(ValueError):
        DatatablePreset("not_a_preset")


# ── DataColumn validation ─────────────────────────────────────────────────


def test_column_defaults_text_left():
    """Defaults exist for `dtype` and `align` — keeps the LLM's job
    smaller (fewer required fields = fewer 422s)."""
    col = DataColumn(key="name", label="名稱")
    assert col.dtype == "text"
    assert col.align == "left"


@pytest.mark.parametrize("dtype", ["text", "number", "date", "percent"])
def test_column_dtype_accepts_all_enum_values(dtype):
    col = DataColumn(key="k", label="L", dtype=dtype)
    assert col.dtype == dtype


def test_column_dtype_rejects_unknown():
    """Literal validation must reject 'numeric' / 'string' synonyms
    (the LLM sometimes invents these). Strict enforcement at the schema
    so the runner doesn't have to filter.
    """
    with pytest.raises(ValidationError):
        DataColumn(key="k", label="L", dtype="numeric")
    with pytest.raises(ValidationError):
        DataColumn(key="k", label="L", dtype="string")


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_column_align_accepts_all_enum_values(align):
    col = DataColumn(key="k", label="L", align=align)
    assert col.align == align


def test_column_align_rejects_synonyms():
    with pytest.raises(ValidationError):
        DataColumn(key="k", label="L", align="middle")
    with pytest.raises(ValidationError):
        DataColumn(key="k", label="L", align="justify")


def test_column_key_strips_whitespace():
    col = DataColumn(key="  foo  ", label="L")
    assert col.key == "foo"


def test_column_key_rejects_internal_whitespace():
    """Keys go into HTML class attrs + dict lookups — internal spaces
    would either need escaping or break the lookup silently."""
    with pytest.raises(ValidationError):
        DataColumn(key="foo bar", label="L")
    with pytest.raises(ValidationError):
        DataColumn(key="foo\tbar", label="L")


def test_column_key_rejects_empty():
    with pytest.raises(ValidationError):
        DataColumn(key="   ", label="L")


# ── DataRow cell value union ──────────────────────────────────────────────


def test_row_accepts_str_int_float_none():
    """All four union arms must work — LLM mixes them per dtype."""
    row = DataRow(
        cells={
            "a": "string",
            "b": 42,
            "c": 3.14,
            "d": None,
        }
    )
    assert row.cells == {"a": "string", "b": 42, "c": 3.14, "d": None}


def test_row_bool_coerced_to_zh_chars():
    """bool slipping through gets converted to 是/否 so the resulting
    cell is unambiguous text (a True/False literal looks wrong in any
    export). See schema's `_reject_bool_cells` for rationale."""
    row = DataRow(cells={"q1": True, "q2": False, "q3": "原樣"})
    assert row.cells == {"q1": "是", "q2": "否", "q3": "原樣"}


def test_row_rejects_nested_dict():
    """Cells must be scalar — nested dicts / lists are a structural bug
    on the LLM side, not a typo."""
    with pytest.raises(ValidationError):
        DataRow(cells={"k": {"nested": "no"}})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        DataRow(cells={"k": [1, 2, 3]})  # type: ignore[dict-item]


# ── DatatableSpec composite validation ────────────────────────────────────


def _valid_spec_args(**overrides):
    base = {
        "title": "測試表",
        "preset": DatatablePreset.KEY_FIGURES,
        "columns": [
            DataColumn(key="metric", label="指標"),
            DataColumn(key="value", label="數值", dtype="number", align="right"),
        ],
        "rows": [
            DataRow(cells={"metric": "客戶數", "value": 1234}),
        ],
    }
    base.update(overrides)
    return base


def test_spec_minimal_valid():
    spec = DatatableSpec(**_valid_spec_args())
    assert spec.title == "測試表"
    assert spec.preset is DatatablePreset.KEY_FIGURES
    assert len(spec.columns) == 2
    assert len(spec.rows) == 1
    assert spec.subtitle is None
    assert spec.notes is None


def test_spec_title_strip_and_reject_empty():
    spec = DatatableSpec(**_valid_spec_args(title="  保留標題  "))
    assert spec.title == "保留標題"
    with pytest.raises(ValidationError):
        DatatableSpec(**_valid_spec_args(title="   "))


def test_spec_rejects_duplicate_column_keys():
    cols = [
        DataColumn(key="dup", label="一"),
        DataColumn(key="dup", label="二"),
    ]
    with pytest.raises(ValidationError) as exc_info:
        DatatableSpec(**_valid_spec_args(columns=cols))
    assert "重複" in str(exc_info.value)


def test_spec_rejects_too_few_columns():
    with pytest.raises(ValidationError):
        DatatableSpec(**_valid_spec_args(columns=[DataColumn(key="k", label="L")]))


def test_spec_rejects_too_many_columns():
    cols = [DataColumn(key=f"k{i}", label=f"L{i}") for i in range(11)]
    with pytest.raises(ValidationError):
        DatatableSpec(**_valid_spec_args(columns=cols))


def test_spec_accepts_zero_rows_for_topic_mismatch_fallback():
    """0 rows 是 LLM 對「chunks 主題不符」的正確 fallback ── prompt 教 LLM
    給空 rows + notes 寫「主題不符」,所以 schema 必須允許 0 rows,不要
    在 spec validate 階段就拒收(否則 retry 用盡反而走更糟的 fallback)。"""
    spec = DatatableSpec(
        **_valid_spec_args(
            rows=[],
            notes="本知識庫內容與『華航 KPI』主題不符,無法抽取對應指標。",
        )
    )
    assert spec.rows == []
    assert spec.notes is not None


def test_spec_rejects_over_200_rows():
    rows = [DataRow(cells={"metric": f"row{i}", "value": i}) for i in range(201)]
    with pytest.raises(ValidationError):
        DatatableSpec(**_valid_spec_args(rows=rows))


# ── GenerateDatatableRequest ──────────────────────────────────────────────


def test_request_defaults():
    req = GenerateDatatableRequest(
        collection_id=42,
        preset=DatatablePreset.TIMELINE_TABLE,
    )
    assert req.top_k == 15
    assert req.seed_query is None
    assert req.extra_instructions is None
    assert req.document_ids is None
    assert req.target_columns is None


def test_request_top_k_bounds():
    GenerateDatatableRequest(
        collection_id=1, preset=DatatablePreset.KEY_FIGURES, top_k=1,
    )
    GenerateDatatableRequest(
        collection_id=1, preset=DatatablePreset.KEY_FIGURES, top_k=40,
    )
    with pytest.raises(ValidationError):
        GenerateDatatableRequest(
            collection_id=1, preset=DatatablePreset.KEY_FIGURES, top_k=0,
        )
    with pytest.raises(ValidationError):
        GenerateDatatableRequest(
            collection_id=1, preset=DatatablePreset.KEY_FIGURES, top_k=41,
        )


def test_request_collection_id_must_be_positive():
    with pytest.raises(ValidationError):
        GenerateDatatableRequest(
            collection_id=0, preset=DatatablePreset.KEY_FIGURES,
        )
    with pytest.raises(ValidationError):
        GenerateDatatableRequest(
            collection_id=-1, preset=DatatablePreset.KEY_FIGURES,
        )


def test_request_preset_string_parses_to_enum():
    """API requests come in as JSON — preset shows up as plain string and
    pydantic must resolve it to the enum member."""
    req = GenerateDatatableRequest.model_validate(
        {"collection_id": 1, "preset": "comparison_table"}
    )
    assert req.preset is DatatablePreset.COMPARISON_TABLE

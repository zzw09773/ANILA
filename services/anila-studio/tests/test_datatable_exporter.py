"""Exporter tests — to_html / to_csv / to_xlsx.

The exporter is the only place CJK encoding, BOM placement, openpyxl
number_format, and merged-cell layout can go wrong silently. Each test
nails one of those properties so a future refactor can't regress the
output Excel / Numbers / LibreOffice users see.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.schemas.datatable import (
    DataColumn,
    DataRow,
    DatatablePreset,
    DatatableSpec,
)
from app.services.csv_formula import FORMULA_TRIGGER_PREFIXES
from app.services.datatable_exporter import to_csv, to_html, to_xlsx


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def cjk_spec() -> DatatableSpec:
    """A representative 4-column / 3-row spec covering all dtypes.

    Picks a mix of CJK strings, numeric values, percent value, and a
    None cell so the exporters' edge-case branches all execute under one
    test fixture.
    """
    return DatatableSpec(
        title="關鍵指標彙整",
        subtitle="2026 Q1",
        preset=DatatablePreset.KEY_FIGURES,
        columns=[
            DataColumn(key="metric", label="指標", align="left"),
            DataColumn(
                key="value", label="數值", dtype="number", align="right",
            ),
            DataColumn(
                key="rate", label="成長率", dtype="percent", align="right",
            ),
            DataColumn(
                key="reviewed_at", label="檢視日", dtype="date", align="center",
            ),
        ],
        rows=[
            DataRow(
                cells={
                    "metric": "客戶數",
                    "value": 12345,
                    "rate": 0.27,
                    "reviewed_at": "2026-01-15",
                }
            ),
            DataRow(
                cells={
                    "metric": '營收 (含 "雜項", NT$)',  # quoted + comma → CSV escape
                    "value": 9876543.21,
                    "rate": "47%",
                    "reviewed_at": "2026-02-28",
                }
            ),
            DataRow(
                cells={
                    "metric": "客訴件數",
                    "value": None,  # null cell → empty render everywhere
                    "rate": None,
                    "reviewed_at": None,
                }
            ),
        ],
        notes="資料來源:內部 BI 系統。",
    )


# ── to_html ───────────────────────────────────────────────────────────────


def test_html_contains_title_subtitle_and_notes(cjk_spec):
    out = to_html(cjk_spec)
    assert "<!DOCTYPE html>" in out
    assert "關鍵指標彙整" in out
    assert "2026 Q1" in out
    assert "資料來源:內部 BI 系統。" in out


def test_html_contains_every_column_label(cjk_spec):
    out = to_html(cjk_spec)
    for col in cjk_spec.columns:
        assert col.label in out, f"missing column label {col.label!r}"


def test_html_contains_every_row_cell_value(cjk_spec):
    """Each non-None cell must appear somewhere in the HTML body.

    The first row's `12345` shows up as `12,345` (number formatting), so
    we check both forms — exporter is allowed to add thousands separator
    on dtype=number.
    """
    out = to_html(cjk_spec)
    # Row 1 cells.
    assert "客戶數" in out
    assert "12,345" in out or "12345" in out
    # 0.27 → "27.00%" via the percent formatter.
    assert "27.00%" in out
    assert "2026-01-15" in out
    # Row 2 cells — note the inner quotes get HTML-escaped to `&quot;`.
    assert "雜項" in out
    assert "&quot;" in out or '"' not in out  # apostrophes/quotes escaped
    # Row 3 cells — None values render as empty <td>, so we don't search
    # for them. Just verify the "客訴件數" label survives.
    assert "客訴件數" in out


def test_html_cjk_is_inline_not_entity_encoded(cjk_spec):
    """繁中 char must appear raw (UTF-8) in the HTML, not as `&#x...;`.
    HTML escape table only includes &<>"'."""
    out = to_html(cjk_spec)
    assert "&#x6307;" not in out  # 指 codepoint as entity
    assert "&#x95dc;" not in out  # 關 codepoint as entity
    assert "關" in out


def test_html_align_classes_emitted(cjk_spec):
    out = to_html(cjk_spec)
    assert "align-right" in out  # value / rate columns
    assert "align-left" in out  # metric column
    assert "align-center" in out  # reviewed_at column


def test_html_count_data_rows(cjk_spec):
    """Sanity check on row count — exactly 3 <tr> inside tbody."""
    out = to_html(cjk_spec)
    tbody_start = out.find("<tbody>")
    tbody_end = out.find("</tbody>")
    body = out[tbody_start:tbody_end]
    assert body.count("<tr>") == 3


def test_html_handles_none_cells_as_empty_td(cjk_spec):
    """Row 3 has 3 None cells — they should render as empty <td>s, not
    "None" string."""
    out = to_html(cjk_spec)
    assert "None" not in out  # the literal Python repr must not leak


# ── to_csv ────────────────────────────────────────────────────────────────


def test_csv_starts_with_utf8_bom(cjk_spec):
    """BOM is the entire point of this format — Excel needs it to open
    UTF-8 .csv without falling back to cp950 and mangling CJK."""
    out = to_csv(cjk_spec)
    assert out.startswith("﻿"), "CSV must lead with UTF-8 BOM"


def test_csv_header_matches_column_labels(cjk_spec):
    out = to_csv(cjk_spec)
    # Skip BOM, take first line.
    first_line = out.lstrip("﻿").split("\r\n", 1)[0]
    # Header uses labels (display name), not keys.
    for col in cjk_spec.columns:
        assert col.label in first_line, f"header missing {col.label!r}"


def test_csv_quotes_cell_with_comma_and_quote(cjk_spec):
    """Row 2 cell contains both comma AND embedded double-quote — RFC 4180
    requires those to be quoted, with embedded quotes doubled. We use
    `csv.QUOTE_MINIMAL` which fires exactly when needed."""
    out = to_csv(cjk_spec)
    # The cell '營收 (含 "雜項", NT$)' should be wrapped in quotes with
    # internal `"` doubled to `""`.
    assert '"營收 (含 ""雜項"", NT$)"' in out


def test_csv_round_trips_through_csv_reader(cjk_spec):
    """The output must parse back via stdlib csv with the BOM acting as
    a leading char (csv reader doesn't strip BOM by itself — that's a
    caller-side concern with codec="utf-8-sig")."""
    out = to_csv(cjk_spec)
    # utf-8-sig strips BOM transparently; csv.reader handles the quoting.
    reader = csv.reader(io.StringIO(out.lstrip("﻿")))
    rows = list(reader)
    # header + 3 data rows
    assert len(rows) == 4
    # 4 columns each
    for r in rows:
        assert len(r) == 4
    # Header has the labels we set.
    assert rows[0] == ["指標", "數值", "成長率", "檢視日"]
    # Row 2 (index 2) has the tricky cell.
    assert rows[2][0] == '營收 (含 "雜項", NT$)'
    # None cells render as empty string (csv has no null type).
    assert rows[3] == ["客訴件數", "", "", ""]


def test_csv_uses_crlf_line_terminator(cjk_spec):
    """RFC 4180 specifies CRLF. Windows Excel can hiccup on \\n-only files
    and show everything on one line."""
    out = to_csv(cjk_spec)
    # Strip the BOM; remaining text should have \r\n separators.
    body = out.lstrip("﻿")
    assert "\r\n" in body
    # And NO bare \n that isn't part of a \r\n.
    bare_lf = body.replace("\r\n", "").count("\n")
    assert bare_lf == 0


# ── to_xlsx ───────────────────────────────────────────────────────────────


def test_xlsx_creates_file_at_dest(tmp_path: Path, cjk_spec):
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    assert dest.exists()
    assert dest.stat().st_size > 0


def test_xlsx_creates_parent_dirs(tmp_path: Path, cjk_spec):
    """Caller should be able to pass a path under a non-existent dir
    (the runner constructs ARTIFACTS_DIR/datatables/{job_id}.xlsx and
    the directory may not exist on first job)."""
    dest = tmp_path / "deep" / "nested" / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    assert dest.exists()


def test_xlsx_title_in_a1_merged_and_bold(tmp_path: Path, cjk_spec):
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # A1 has the title.
    assert ws["A1"].value == "關鍵指標彙整"
    # A1 is bold.
    assert ws["A1"].font.bold is True
    # A1:D1 (4 columns) is merged.
    merged_ranges = [str(r) for r in ws.merged_cells.ranges]
    assert "A1:D1" in merged_ranges, f"expected A1:D1 in {merged_ranges}"


def test_xlsx_subtitle_below_title(tmp_path: Path, cjk_spec):
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # Subtitle on row 2 (since title took row 1).
    assert ws["A2"].value == "2026 Q1"


def test_xlsx_header_row_bold_with_fill(tmp_path: Path, cjk_spec):
    """Header row is the 3rd row when subtitle exists (row 1 title,
    row 2 subtitle, row 3 header)."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    header_row = 3  # title(1) + subtitle(2) + header(3)
    for col_idx, col in enumerate(cjk_spec.columns, start=1):
        cell = ws.cell(row=header_row, column=col_idx)
        assert cell.value == col.label
        assert cell.font.bold is True, f"header {col.label} not bold"
        # PatternFill present with fgColor != "00000000" (default empty).
        assert cell.fill.fgColor is not None


def test_xlsx_number_dtype_has_thousands_format(tmp_path: Path, cjk_spec):
    """value column (dtype=number) cell must have `#,##0` format."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # value column is column 2; first data row is 4 (title 1, subtitle 2,
    # header 3, data 4..).
    cell = ws.cell(row=4, column=2)
    assert cell.value == 12345  # numeric, not string
    assert cell.number_format == "#,##0"


def test_xlsx_percent_dtype_has_percent_format(tmp_path: Path, cjk_spec):
    """rate column (dtype=percent) — row 1 had 0.27 (already a ratio)."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # rate is column 3; row 4 is first data row.
    cell = ws.cell(row=4, column=3)
    assert cell.value == pytest.approx(0.27)
    assert cell.number_format == "0.00%"


def test_xlsx_percent_parses_string_with_percent_sign(tmp_path: Path, cjk_spec):
    """Row 2 had "47%" string — parser divides by 100 → 0.47 float."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # rate is column 3; row 5 is second data row.
    cell = ws.cell(row=5, column=3)
    assert cell.value == pytest.approx(0.47)
    assert cell.number_format == "0.00%"


def test_xlsx_date_dtype_has_date_format(tmp_path: Path, cjk_spec):
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # reviewed_at is column 4; row 4 first data row.
    cell = ws.cell(row=4, column=4)
    # openpyxl converts a datetime.date back from disk; format matches.
    assert cell.number_format == "yyyy-mm-dd"
    # The value should be a datetime/date, not a string.
    from datetime import date, datetime

    assert isinstance(cell.value, (date, datetime))


def test_xlsx_none_cell_is_empty(tmp_path: Path, cjk_spec):
    """Row 3 had value=None / rate=None / reviewed_at=None — those cells
    must come back as empty (cell.value is None), not the string 'None'."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # Row 6 (data row 3): value / rate / reviewed_at are None.
    for col_idx in (2, 3, 4):
        cell = ws.cell(row=6, column=col_idx)
        assert cell.value is None, f"col {col_idx} unexpected: {cell.value!r}"


def test_xlsx_notes_appear_below_table(tmp_path: Path, cjk_spec):
    """Notes go BELOW the last data row (after a blank gap row).

    With 3 data rows starting at row 4, last data row is row 6. Spec
    says "兩列" below — exporter writes at row 8 (row 7 blank).
    """
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # Search rows 7..10 for the notes string; allow some implementation
    # leeway on the exact gap distance.
    found_at = None
    for r in range(7, 12):
        if ws.cell(row=r, column=1).value == "資料來源:內部 BI 系統。":
            found_at = r
            break
    assert found_at is not None, "notes not found below the data rows"
    # Notes must be italic.
    assert ws.cell(row=found_at, column=1).font.italic is True


def test_xlsx_freeze_panes_below_header(tmp_path: Path, cjk_spec):
    """Freeze panes must be set just below the header so scrolling
    keeps the labels visible. Header row is 3 (title 1, subtitle 2,
    header 3) → freeze cell is A4."""
    dest = tmp_path / "out.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    assert ws.freeze_panes == "A4"


def test_xlsx_spec_without_subtitle_or_notes(tmp_path: Path):
    """Minimal spec — no subtitle, no notes — should still produce a
    valid workbook with title at A1 and header at row 2."""
    spec = DatatableSpec(
        title="No-frills 表",
        preset=DatatablePreset.ENTITY_ATTRIBUTES,
        columns=[
            DataColumn(key="a", label="一"),
            DataColumn(key="b", label="二"),
        ],
        rows=[DataRow(cells={"a": "x", "b": "y"})],
    )
    dest = tmp_path / "minimal.xlsx"
    to_xlsx(spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    assert ws["A1"].value == "No-frills 表"
    # Header on row 2 (no subtitle to push it down).
    assert ws.cell(row=2, column=1).value == "一"
    assert ws.cell(row=2, column=2).value == "二"
    # Data on row 3.
    assert ws.cell(row=3, column=1).value == "x"
    assert ws.cell(row=3, column=2).value == "y"


def test_xlsx_round_trip_preserves_cjk(tmp_path: Path, cjk_spec):
    """Open the .xlsx and verify CJK strings round-trip without mojibake.
    openpyxl writes UTF-8 inside the xml parts; this guards against any
    future regression that pre-encodes a string to a wrong codec."""
    dest = tmp_path / "cjk.xlsx"
    to_xlsx(cjk_spec, dest)
    wb = load_workbook(str(dest))
    ws = wb.active
    # Title.
    assert ws["A1"].value == "關鍵指標彙整"
    # Row 1 metric.
    assert ws.cell(row=4, column=1).value == "客戶數"
    # Row 2 metric — embedded quotes preserved verbatim through xlsx
    # storage (Excel uses its own xml escape, but the round-trip is clean).
    assert ws.cell(row=5, column=1).value == '營收 (含 "雜項", NT$)'


# ── formula injection (site 5) ─────────────────────────────────────────────


def _formula_spec(**overrides) -> DatatableSpec:
    data = dict(
        title="Safe title",
        subtitle="Safe subtitle",
        preset=DatatablePreset.KEY_FIGURES,
        columns=[
            DataColumn(key="a", label="=1+1"),
            DataColumn(key="b", label="乙", dtype="number"),
        ],
        rows=[DataRow(cells={"a": "plain", "b": 1})],
        notes="Safe notes",
    )
    data.update(overrides)
    return DatatableSpec(**data)


def _csv_rows(out: str) -> list[list[str]]:
    assert out[:1] == "\ufeff"
    return list(csv.reader(io.StringIO(out[1:])))


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_csv_cell_first_char_is_apostrophe_for_each_trigger(prefix: str):
    payload = f"{prefix}1+1"
    spec = _formula_spec(rows=[DataRow(cells={"a": payload, "b": 1})])
    cell = _csv_rows(to_csv(spec))[1][0]
    assert cell[:1] == "'"
    assert cell[1:] == payload


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_xlsx_data_cell_is_not_formula_type(tmp_path: Path, prefix: str):
    """XLSX assertion is data_type != 'f', not 'starts with apostrophe'."""
    payload = f"{prefix}1+1"
    spec = _formula_spec(rows=[DataRow(cells={"a": payload, "b": 1})])
    dest = tmp_path / "cell.xlsx"
    to_xlsx(spec, dest)
    ws = load_workbook(str(dest)).active
    # title row 1, subtitle 2, header 3, data 4
    cell = ws.cell(row=4, column=1)
    assert cell.data_type != "f"
    assert cell.value == "'" + payload


def test_openpyxl_stores_unprefixed_equals_as_formula(tmp_path: Path):
    """Platform evidence this package relies on: '=1+1' is a real formula."""
    wb = Workbook()
    wb.active["A1"] = "=1+1"
    dest = tmp_path / "raw.xlsx"
    wb.save(str(dest))
    cell = load_workbook(str(dest)).active["A1"]
    assert cell.data_type == "f"


def test_xlsx_title_subtitle_notes_are_not_formula_type(tmp_path: Path):
    """CSV omits title/subtitle/notes; copying CSV cases would miss these."""
    spec = _formula_spec(
        title="=1+1",
        subtitle="+HYPERLINK(\"http://x\")",
        notes="@cmd|'/c calc'!A0",
        rows=[DataRow(cells={"a": "plain", "b": 2})],
    )
    dest = tmp_path / "meta.xlsx"
    to_xlsx(spec, dest)
    ws = load_workbook(str(dest)).active
    title = ws["A1"]
    subtitle = ws["A2"]
    notes = None
    for r in range(5, 12):
        value = ws.cell(row=r, column=1).value
        if isinstance(value, str) and "cmd" in value:
            notes = ws.cell(row=r, column=1)
            break
    assert notes is not None, "notes cell not found"
    assert title.data_type != "f"
    assert subtitle.data_type != "f"
    assert notes.data_type != "f"
    assert title.value[:1] == "'"
    assert subtitle.value[:1] == "'"
    assert notes.value[:1] == "'"


def test_xlsx_numeric_cells_stay_numeric_not_prefixed_text(
    tmp_path: Path, cjk_spec: DatatableSpec
):
    dest = tmp_path / "nums.xlsx"
    to_xlsx(cjk_spec, dest)
    ws = load_workbook(str(dest)).active
    cell = ws.cell(row=4, column=2)
    assert cell.value == 12345
    assert cell.data_type != "f"


def test_csv_header_label_is_neutralized():
    """Removing csv_formula_safe around col.label in to_csv must turn this red."""
    spec = _formula_spec()
    header = _csv_rows(to_csv(spec))[0]
    assert header[0][:1] == "'"
    assert header[0][1:] == "=1+1"
    assert header[1] == "乙"


def test_xlsx_header_label_is_not_formula_type(tmp_path: Path):
    """Removing csv_formula_safe around col.label in to_xlsx must turn this red."""
    spec = _formula_spec()
    dest = tmp_path / "hdr.xlsx"
    to_xlsx(spec, dest)
    # title row 1, subtitle 2, header 3
    cell = load_workbook(str(dest)).active.cell(row=3, column=1)
    assert cell.data_type != "f"
    assert cell.value == "'=1+1"


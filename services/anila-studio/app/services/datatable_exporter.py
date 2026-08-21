"""Datatable export — HTML / CSV / XLSX from a `DatatableSpec`.

## 為什麼三格分開的 exporter (而不是一個吃 format 參數的)

每種格式有自己的細節:HTML 要 CSS 內嵌、CSV 要 UTF-8 BOM(否則 Excel 開
中文亂碼)、XLSX 要 openpyxl 物件導向操作 + dtype-aware number_format。
分開三個函式比 if/elif 大爆炸好讀,也讓 unit test 可以單獨 cover 每種格式
的邊角(BOM 有沒有、merged cell 對不對、quoting 跑沒跑)。

## CSV BOM 的重要性

Excel 開 .csv 預設用系統 codepage(Windows: cp950 / Mac: macRoman),完全
不認 UTF-8。加 BOM(`\\ufeff`)後 Excel 會切到 UTF-8 模式,繁中才不亂碼。
其他 CSV 讀者(LibreOffice、Google Sheets、Python `csv`)會把 BOM 當成
普通字元跳過 — BOM 是 Excel-friendly 的妥協,沒有副作用。

## openpyxl 的 dtype 處理

LLM 回 cell value 時行為不穩:dtype=percent 可能是 0.47 (float)、"47%"
(string with sign)、或 47 (int 已經除過 100 嗎?無法判斷)。本 module 採
保守策略:能 parse 成 float 就存 float + apply 對應 `.number_format`,
parse 不出來就純字串存(犧牲格式化,保留可讀性)。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schemas.datatable import DataColumn, DataRow, DatatableSpec
from app.services.csv_formula import csv_formula_safe


logger = logging.getLogger(__name__)


# ── HTML ───────────────────────────────────────────────────────────────────


_HTML_TEMPLATE_HEAD = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", "Noto Sans CJK TC", "PingFang TC", sans-serif;
    color: #1f2937;
    max-width: 1100px;
    margin: 24px auto;
    padding: 0 16px;
    line-height: 1.5;
  }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  .subtitle {{ color: #6b7280; margin: 0 0 16px; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    font-size: 14px;
    margin-bottom: 12px;
  }}
  th, td {{
    border: 1px solid #d1d5db;
    padding: 8px 12px;
    vertical-align: top;
    word-break: break-word;
  }}
  thead th {{
    background: #1e3a5f;
    color: #ffffff;
    text-align: left;
    font-weight: 600;
  }}
  tbody tr:nth-child(even) {{ background: #f9fafb; }}
  tbody tr:hover {{ background: #eef4ff; }}
  td.align-left, th.align-left {{ text-align: left; }}
  td.align-center, th.align-center {{ text-align: center; }}
  td.align-right, th.align-right {{ text-align: right; }}
  td.dtype-number, td.dtype-percent {{ font-variant-numeric: tabular-nums; }}
  .notes {{
    color: #4b5563;
    font-style: italic;
    font-size: 13px;
    border-left: 3px solid #d1d5db;
    padding-left: 12px;
    margin-top: 16px;
  }}
</style>
</head>
<body>
"""


def _html_escape(text: Any) -> str:
    """Minimal HTML escape — must cover `&<>"'`. We DO NOT use html.escape
    because it leaves single quote as `&#x27;` by default which is fine but
    less readable; switch to a manual map so the output is predictable."""
    if text is None:
        return ""
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _format_cell_html(value: Any, dtype: str) -> str:
    """Pretty-print a cell value for HTML display.

    - None → empty (rendered as empty td).
    - number/percent dtype with numeric value → format with thousands sep
      / percent suffix.
    - All other paths → str(value) escaped.

    The escape happens HERE, not at the caller, so each format path can
    decide whether to insert HTML (we don't yet, but the boundary is
    cleaner this way).
    """
    if value is None or value == "":
        return ""
    if dtype == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
        # Thousands separator without forcing a specific decimal count —
        # int stays int, float keeps its meaningful precision.
        if isinstance(value, int):
            return _html_escape(f"{value:,}")
        return _html_escape(f"{value:,.4f}".rstrip("0").rstrip("."))
    if dtype == "percent" and isinstance(value, (int, float)) and not isinstance(value, bool):
        # 0.47 → "47.00%". If the LLM emitted 47 (no decimal) we still
        # multiply by 100; this is a known ambiguity called out in the
        # spec — users should hint via target_columns / extra_instructions.
        return _html_escape(f"{value * 100:.2f}%")
    return _html_escape(value)


def to_html(spec: DatatableSpec) -> str:
    """Render a self-contained HTML page with the datatable.

    No Jinja2 — one table, f-string concatenation is plenty.
    Output is UTF-8 text that can be saved to .html and opened in any
    browser. CJK is preserved verbatim (no entity escape on CJK chars).
    """
    parts: list[str] = [_HTML_TEMPLATE_HEAD.format(title=_html_escape(spec.title))]

    parts.append(f"<h1>{_html_escape(spec.title)}</h1>")
    if spec.subtitle:
        parts.append(f'<p class="subtitle">{_html_escape(spec.subtitle)}</p>')

    parts.append("<table>")
    # thead
    parts.append("<thead><tr>")
    for col in spec.columns:
        align_cls = f"align-{col.align}"
        parts.append(
            f'<th class="{align_cls} dtype-{col.dtype}" '
            f'data-key="{_html_escape(col.key)}">'
            f"{_html_escape(col.label)}"
            f"</th>"
        )
    parts.append("</tr></thead>")
    # tbody
    parts.append("<tbody>")
    for row in spec.rows:
        parts.append("<tr>")
        for col in spec.columns:
            value = row.cells.get(col.key)
            align_cls = f"align-{col.align}"
            parts.append(
                f'<td class="{align_cls} dtype-{col.dtype}">'
                f"{_format_cell_html(value, col.dtype)}"
                f"</td>"
            )
        parts.append("</tr>")
    parts.append("</tbody>")
    parts.append("</table>")

    if spec.notes:
        parts.append(f'<div class="notes">{_html_escape(spec.notes)}</div>')

    parts.append("</body></html>")
    return "".join(parts)


# ── CSV ────────────────────────────────────────────────────────────────────


def _stringify_cell_csv(value: Any) -> str:
    """Convert a cell value to its CSV string form.

    - None → empty (CSV blank, NOT "None")
    - bool → "是" / "否" (defensive — pydantic should have coerced, but
      double-guard so a raw spec passed in tests still does the right thing)
    - other → str(value); float repr handles big numbers fine.

    ``csv.writer`` with ``QUOTE_MINIMAL`` handles quoting when the value
    contains a comma, newline, or double-quote. That is field-boundary
    quoting, **not** formula neutralization. Values that start with the
    spreadsheet trigger set are prefixed by ``csv_formula_safe`` here.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return csv_formula_safe("是" if value else "否")
    if isinstance(value, (int, float)):
        return str(value)
    return csv_formula_safe(str(value))


def to_csv(spec: DatatableSpec) -> str:
    """Render the datatable as a UTF-8 BOM-prefixed CSV string.

    The leading BOM (`\\ufeff`) is what makes Excel open the file in UTF-8
    mode instead of guessing cp950. All other CSV readers treat BOM as a
    no-op leading character.

    Title / subtitle / notes are NOT included in the CSV body — CSV is a
    pure tabular format. The caller can put them in a sibling .txt if
    needed. Header row uses `column.label` (display name), not `.key`
    (internal id) — CSV is end-user-facing.

    `csv.writer` with `QUOTE_MINIMAL` quotes cells that contain commas,
    quotes, or newlines. We use `\\r\\n` line terminators per RFC 4180
    so Windows Excel doesn't show a single long line.
    """
    buf = io.StringIO()
    # `\r\n` per RFC 4180; csv.writer adds the terminator after the
    # last cell of each row.
    writer = csv.writer(
        buf,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    writer.writerow([csv_formula_safe(col.label) for col in spec.columns])
    for row in spec.rows:
        writer.writerow(
            [_stringify_cell_csv(row.cells.get(col.key)) for col in spec.columns]
        )
    # Prepend UTF-8 BOM. Done after writing so we don't have to think about
    # csv.writer touching it.
    return "﻿" + buf.getvalue()


# ── XLSX ───────────────────────────────────────────────────────────────────


# openpyxl number formats (kept as constants so they're easy to swap if a
# locale-specific format is needed later).
_FMT_NUMBER = "#,##0"
_FMT_PERCENT = "0.00%"
_FMT_DATE = "yyyy-mm-dd"


def _coerce_cell_value(value: Any, dtype: str) -> Any:
    """Convert a raw cell value to the type openpyxl should store.

    - dtype=number: try float-parse if value is a numeric-looking string;
      otherwise pass through as-is.
    - dtype=percent: try parsing "47%" → 0.47. Failure → original string.
      Numeric in [0,1] left as-is; numeric > 1 assumed to be percentage
      and divided by 100 (LLMs often output 47 meaning 47%).
    - dtype=date: try ISO 8601 / common formats; failure → original string.
    - dtype=text or unrecognised: pass through.

    All branches survive on garbage input — the worst case is a cell that
    renders as plain text without the dtype's number_format, which is the
    same fallback the HTML renderer takes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "是" if value else "否"

    if dtype == "number":
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            stripped = value.strip().replace(",", "")
            try:
                if "." in stripped:
                    return float(stripped)
                return int(stripped)
            except ValueError:
                return value
        return value

    if dtype == "percent":
        if isinstance(value, (int, float)):
            f = float(value)
            return f if 0.0 <= f <= 1.0 else f / 100.0
        if isinstance(value, str):
            stripped = value.strip().rstrip("%").replace(",", "").strip()
            try:
                f = float(stripped)
            except ValueError:
                return value
            # Trailing "%" present in the original means "raw percent
            # number" — divide by 100. Without trailing %, fall back to
            # heuristic (0..1 already a ratio, >1 assumed pct).
            if value.strip().endswith("%"):
                return f / 100.0
            return f if 0.0 <= f <= 1.0 else f / 100.0
        return value

    if dtype == "date":
        if isinstance(value, (date, datetime)):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(stripped, fmt).date()
                except ValueError:
                    continue
            return value
        return value

    return value


def _xlsx_store_value(value: Any) -> Any:
    """Store ``value`` without letting openpyxl treat a string as a formula.

    Numbers and dates pass through so number_format still applies. Strings
    (including coerced bool labels) go through ``csv_formula_safe``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return csv_formula_safe("是" if value else "否")
    if isinstance(value, (int, float, date, datetime)):
        return value
    return csv_formula_safe(value)


def _number_format_for(dtype: str) -> str | None:
    """Map our dtype enum to an openpyxl number_format string."""
    if dtype == "number":
        return _FMT_NUMBER
    if dtype == "percent":
        return _FMT_PERCENT
    if dtype == "date":
        return _FMT_DATE
    return None


def _alignment_for(align: str) -> Alignment:
    """Map our align enum to an openpyxl Alignment object.

    `wrap_text=True` keeps long CJK strings readable instead of overflowing
    into adjacent columns — slightly slower to render in Excel but the
    visual result is much better for narrative-heavy tables.
    """
    return Alignment(horizontal=align, vertical="top", wrap_text=True)


def _estimate_column_width(col: DataColumn, spec: DatatableSpec) -> float:
    """Cheap auto-fit estimate: max of label length and cell string lengths,
    clamped to a sane range.

    openpyxl can't measure rendered text width (no font metrics without a
    GUI), so we approximate by character count. CJK chars are ~2× the
    visual width of ASCII; we boost their count to account for that.

    Output is in openpyxl's "character width" unit (1 ≈ width of '0' in
    Calibri 11). Clamped to [10, 50] so very long URLs don't blow out the
    layout and very short columns don't look cramped.
    """
    def visual_len(s: str) -> int:
        # CJK Unified Ideographs + Hangul + Hiragana/Katakana → counted ×2.
        out = 0
        for ch in s:
            code = ord(ch)
            if (
                0x3000 <= code <= 0x9FFF      # CJK common + symbols
                or 0xAC00 <= code <= 0xD7A3  # Hangul syllables
                or 0xFF00 <= code <= 0xFFEF  # halfwidth/fullwidth
            ):
                out += 2
            else:
                out += 1
        return out

    max_visual = visual_len(col.label)
    for row in spec.rows:
        v = row.cells.get(col.key)
        if v is None:
            continue
        max_visual = max(max_visual, visual_len(str(v)))
    # +2 for padding so the column doesn't hug the longest content.
    return max(10.0, min(50.0, float(max_visual) + 2.0))


def to_xlsx(spec: DatatableSpec, dest_path: Path) -> None:
    """Render the datatable into an Excel workbook on disk.

    Layout:

      A1 (merged across all columns)  = spec.title          (bold, 14pt)
      A2 (merged when subtitle exists) = spec.subtitle      (grey 10pt)
      header row (next row)            = column labels      (bold, fill)
      data rows                        = cells with dtype-driven format
      notes (skip 1 blank row)         = italic              (10pt)

    Mirrors the HTML structure so a user comparing HTML and XLSX sees the
    same shape.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "datatable"

    n_cols = len(spec.columns)
    cur_row = 1  # 1-indexed in openpyxl

    # Title row — merged across all columns, bold.
    title_cell = ws.cell(
        row=cur_row, column=1, value=csv_formula_safe(spec.title),
    )
    title_cell.font = Font(bold=True, size=14)
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    if n_cols > 1:
        ws.merge_cells(
            start_row=cur_row, start_column=1,
            end_row=cur_row, end_column=n_cols,
        )
    cur_row += 1

    # Subtitle (optional).
    if spec.subtitle:
        sub_cell = ws.cell(
            row=cur_row, column=1, value=csv_formula_safe(spec.subtitle),
        )
        sub_cell.font = Font(size=10, color="6B7280", italic=False)
        sub_cell.alignment = Alignment(horizontal="left", vertical="center")
        if n_cols > 1:
            ws.merge_cells(
                start_row=cur_row, start_column=1,
                end_row=cur_row, end_column=n_cols,
            )
        cur_row += 1

    # Header row — bold + light fill.
    header_row_idx = cur_row
    header_fill = PatternFill(
        start_color="E0E7EF", end_color="E0E7EF", fill_type="solid",
    )
    header_font = Font(bold=True, color="1F2937")
    for col_idx, col in enumerate(spec.columns, start=1):
        cell = ws.cell(
            row=header_row_idx, column=col_idx, value=csv_formula_safe(col.label),
        )
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = _alignment_for(col.align)
    cur_row += 1

    # Data rows.
    for row in spec.rows:
        for col_idx, col in enumerate(spec.columns, start=1):
            raw = row.cells.get(col.key)
            coerced = _coerce_cell_value(raw, col.dtype)
            stored = _xlsx_store_value(coerced)
            cell = ws.cell(row=cur_row, column=col_idx, value=stored)
            fmt = _number_format_for(col.dtype)
            if fmt is not None and isinstance(stored, (int, float, date, datetime)):
                cell.number_format = fmt
            cell.alignment = _alignment_for(col.align)
        cur_row += 1

    # Notes (skip 1 blank row, then italic 10pt). 2 rows below as spec says.
    if spec.notes:
        notes_row = cur_row + 1  # +1 makes "兩列" 在表下方
        notes_cell = ws.cell(
            row=notes_row, column=1, value=csv_formula_safe(spec.notes),
        )
        notes_cell.font = Font(italic=True, size=10, color="4B5563")
        notes_cell.alignment = Alignment(
            horizontal="left", vertical="top", wrap_text=True,
        )
        if n_cols > 1:
            ws.merge_cells(
                start_row=notes_row, start_column=1,
                end_row=notes_row, end_column=n_cols,
            )

    # Column widths.
    for col_idx, col in enumerate(spec.columns, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = (
            _estimate_column_width(col, spec)
        )

    # Freeze the header row so scrolling keeps it visible (best practice for
    # tables > screen height). Freeze pane below header.
    ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(dest_path))

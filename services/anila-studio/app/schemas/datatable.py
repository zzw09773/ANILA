"""Pydantic schemas for the Studio Datatable artifact pipeline.

## 為什麼 datatable 跟 SlidesSpec 分開

slides 是「視覺敘事」(每張一頁,layout 多樣),datatable 是「結構化資料表」
(一張 HTML/CSV/XLSX,只有 columns + rows)。兩者 LLM prompt、欄位驗證、
渲染流程都不一樣 — schema 必須獨立,不靠擴充 SlidesSpec 共用。

## Validation 哲學

跟 studio.py 一樣的 *loose-at-boundary / strict-at-renderer* 原則:

- LLM 偶爾會回 dtype="numeric"(同義詞)、align="middle"(打錯字)、cell value
  混 string/number。schema 對 dtype / align 用 Enum / Literal 強制收斂;對
  cells 的 value type 接 `str | int | float | None`(LLM 找不到 cell 時可
  以乾脆回 None,exporter 會渲染成空字串)。
- columns 上限 10,rows 上限 200 — 超過代表 LLM 走偏(資料表不該是無限滾動的
  spreadsheet,使用者要的是「精煉摘要」),422 給 caller 重試。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DatatablePreset(str, Enum):
    """Closed set of datatable presets the LLM picks layout / column-shape from.

    String-valued enum so it serialises into JobStatus JSON cleanly and the
    frontend can switch on the literal string (no need to mirror Python).
    """

    KEY_FIGURES = "key_figures"               # 關鍵指標彙整
    ENTITY_ATTRIBUTES = "entity_attributes"   # 實體屬性表(各 X 的 Y 欄位)
    TIMELINE_TABLE = "timeline_table"         # 時間軸表(日期/事件/變化)
    COMPARISON_TABLE = "comparison_table"     # 並排比較(對照 X vs Y)


class DataColumn(BaseModel):
    """A single column definition.

    `key` is the internal identifier the LLM uses inside `DataRow.cells`;
    `label` is what end-users see in the HTML/XLSX header. Splitting them
    means renaming the visible header (CJK 翻譯、加單位) doesn't break the
    row→column lookup.

    `dtype` drives:
      - HTML rendering: number/percent get monospace + right-align by default
      - XLSX rendering: number_format on the openpyxl cell
      - CSV: no effect (CSV is dtype-blind)

    `align` lets the LLM override the default alignment for visual polish
    (e.g. a "說明" column dtype=text but center-aligned for short labels).
    """

    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=120)
    dtype: Literal["text", "number", "date", "percent"] = "text"
    align: Literal["left", "center", "right"] = "left"

    @field_validator("key")
    @classmethod
    def _key_safe(cls, v: str) -> str:
        # `key` shows up in HTML class attrs / dict lookups; reject empty
        # after strip and disallow control / whitespace chars that would
        # need escaping. Letters / digits / _ / - is plenty for what an
        # LLM-emitted internal id needs to be.
        stripped = v.strip()
        if not stripped:
            raise ValueError("column key 不可為空")
        if any(ch in stripped for ch in (" ", "\t", "\n", "\r")):
            raise ValueError(f"column key 不可含 whitespace: {v!r}")
        return stripped


class DataRow(BaseModel):
    """One row of cells. Keys MUST be the `key` of one of the parent table's
    columns; missing cells render as empty.

    Value union (`str | int | float | None`):
      - `None`: LLM signalled "找不到 / 不適用". Rendered as empty cell.
      - `str` : verbatim text. Numbers as strings ("47%", "3.5×") are
                preserved as-is by the HTML/CSV exporter; for XLSX,
                `dtype=percent` triggers a parse attempt (see
                datatable_exporter._coerce_cell_value).
      - `int` / `float`: native numeric. Used by XLSX number_format and
                by HTML right-align.

    bool is intentionally *not* allowed: LLMs that emit `true`/`false`
    should be told to use "是"/"否" or 1/0; bool slipping through gets
    converted to "True"/"False" which looks wrong in any export. Pydantic
    won't coerce bool→int because we use `strict` semantics on the union
    via `model_validator`.
    """

    cells: dict[str, str | int | float | None]

    @model_validator(mode="before")
    @classmethod
    def _reject_bool_cells(cls, values: object) -> object:
        """Strip `bool` values out of the union (pydantic's int branch
        would otherwise accept bool silently — bool is a subclass of int
        in Python).

        We coerce True/False → "是"/"否" so the LLM's intent survives but
        the resulting cell is unambiguous text. (Rejecting outright would
        make the LLM's "勝任度=true" payload 422 the whole job; coercing
        is the user-friendly choice.)
        """
        if not isinstance(values, dict):
            return values
        cells = values.get("cells")
        if not isinstance(cells, dict):
            return values
        coerced = dict(cells)
        for k, v in cells.items():
            if isinstance(v, bool):
                coerced[k] = "是" if v else "否"
        values["cells"] = coerced
        return values


class DatatableSpec(BaseModel):
    """Top-level datatable artifact — what the LLM emits, what the exporter reads."""

    title: str = Field(..., min_length=1, max_length=200)
    subtitle: str | None = Field(default=None, max_length=300)
    preset: DatatablePreset
    columns: list[DataColumn] = Field(..., min_length=2, max_length=10)
    # 允許 0 rows:LLM 看到 chunks 跟 user 指示主題不符時,被 prompt 指示
    # 「rows 留空、在 notes 寫主題不符」── schema 必須相容此 fallback,
    # 否則 spec 驗證失敗反覆 retry 也走不出來(SCHEMA_CORRECTION_PASSES 用盡)。
    rows: list[DataRow] = Field(..., min_length=0, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("title 不可為空字串")
        return stripped

    @model_validator(mode="after")
    def _unique_column_keys(self) -> "DatatableSpec":
        """Duplicate keys would silently lose data when building the row
        lookup dict — fail loudly so the correction pass can fix it.
        """
        seen: dict[str, int] = {}
        for col in self.columns:
            seen[col.key] = seen.get(col.key, 0) + 1
        dups = [k for k, n in seen.items() if n > 1]
        if dups:
            raise ValueError(
                f"columns 出現重複 key（{', '.join(dups)}）— 每個 column "
                "必須有獨一無二的 key"
            )
        return self


# ── Request / response models ─────────────────────────────────────────────


class GenerateDatatableRequest(BaseModel):
    """Input to ``POST /api/datatables/jobs``."""

    collection_id: int = Field(..., ge=1)
    preset: DatatablePreset
    seed_query: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional retrieval seed. When omitted the runner builds one "
            "from `collection.name + preset + extra_instructions`."
        ),
    )
    extra_instructions: str | None = Field(default=None, max_length=2000)
    document_ids: list[int] | None = Field(
        default=None,
        description=(
            "Restrict retrieval to these documents only. None = whole "
            "collection. Empty list is rejected by csp."
        ),
    )
    target_columns: list[str] | None = Field(
        default=None,
        max_length=10,
        description=(
            "Hints to the LLM about which columns the user wants. Free-form "
            "繁中 strings (e.g. ['品名', '價格', '原產地']). LLM may still "
            "add / rename based on chunk content."
        ),
    )
    top_k: int = Field(default=15, ge=1, le=40)
    # Slice 8b: optional ALM task binding (governance passthrough only).
    task_id: str | None = Field(default=None, max_length=64)
    source_snapshot_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class DatatableJobStatus(BaseModel):
    """Polling response for a datatable job. JSON body — no header buffer issues."""

    job_id: str
    state: Literal["pending", "running", "done", "failed", "cancelled"]
    step: str | None = None
    title: str | None = None
    preset: DatatablePreset | None = None
    row_count: int | None = None
    column_count: int | None = None
    error: str | None = None
    # Three artifact formats — populated only when state == "done". Keys:
    #   "html" → /api/datatables/jobs/{id}/download/html
    #   "csv"  → /api/datatables/jobs/{id}/download/csv
    #   "xlsx" → /api/datatables/jobs/{id}/download/xlsx
    download_urls: dict[str, str] | None = None
    # Slice 8b: CSP artifact passthrough — set once the artifact registers.
    artifact_id: str | None = None
    classification_level: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Step labels surfaced to the UI ────────────────────────────────────────
#
# Mirrors the JOB_STEP_* convention from studio.py — kept as module-level
# constants (not an enum) so additions don't require a schema bump.

JOB_STEP_QUEUED = "queued"
JOB_STEP_RETRIEVING = "retrieving"
JOB_STEP_GENERATING = "generating"
JOB_STEP_NORMALIZING = "normalizing"
JOB_STEP_EXPORTING = "exporting"
JOB_STEP_DONE = "done"

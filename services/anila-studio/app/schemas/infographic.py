"""Infographic 資訊圖表 — schema for HTML + chart PNG + PDF artifact.

Studio 的「資訊圖表」產物：

- 一張長條 HTML 頁面，內嵌 matplotlib chart (PNG base64)
- 可由 Playwright 轉成 PDF

四個 preset，差別在版面節奏與選用元件：

* ``mission_dashboard``  — 任務 dashboard。stats 主秀，配 1-2 張 chart。
* ``stats_brief``        — 數據簡報。stats 為主、可加 chart 補充。
* ``comparison_matrix``  — 比較矩陣。comparison table 為主軸。
* ``timeline_overview``  — 時間軸總覽。timeline 線為主，其他補充。

Schema 採「扁平」設計：``stats / charts / comparison / timeline`` 都是
頂層 list（最深 1 層 nest），符合 spec 要求「不要 nested 太深」。
LLM 對於越扁的 schema 命中率越高，validation pass rate 也越穩。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class InfographicPreset(str, Enum):
    """四個 preset，對應前端 Studio CommandModal 上的快選按鈕。

    Renderer 端用 preset 決定要不要展開特定 section（例：timeline_overview
    若收到空 timeline，會把 stats grid 拉滿補位）。每個 preset 仍會渲染
    所有有資料的 section — preset 只決定 "視覺重心"，不是 hard gate。
    """

    MISSION_DASHBOARD = "mission_dashboard"
    STATS_BRIEF = "stats_brief"
    COMPARISON_MATRIX = "comparison_matrix"
    TIMELINE_OVERVIEW = "timeline_overview"


class StatBlock(BaseModel):
    """一個大字數字方塊（如「47%」「3.5×」）。

    ``value`` 是主秀（大字），``label`` 是旁白（小字），``delta`` 是
    變化趨勢（如 "+12%"，renderer 會根據前綴 +/- 自動上色），``icon``
    是 renderer 內建 set 的 keyword（未列入白名單則不畫 icon、純文字
    fallback）。
    """

    value: str = Field(..., min_length=1, max_length=20)
    label: str = Field(..., min_length=1, max_length=120)
    delta: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=40)


class ChartSpec(BaseModel):
    """一張 chart 的 matplotlib-friendly spec。

    Renderer 透過 ``chart_type`` 分派到對應 matplotlib API。``x_labels``
    對應 X 軸 (bar/line/hbar) 或圓餅 slice label (pie/donut)。
    ``series`` 是 ``[{"name": "...", "values": [n, n, ...]}]`` —
    pie/donut 只取 series[0]。
    """

    chart_type: Literal["bar", "line", "pie", "donut", "hbar"]
    title: str = Field(..., min_length=1, max_length=120)
    x_labels: list[str] = Field(..., min_length=1, max_length=20)
    # series shape is intentionally loose ({"name": str, "values": list[float]})
    # — Renderer validates / coerces; keeping it dict avoids one more nested
    # Pydantic class for what is really wire-level adapter data.
    series: list[dict] = Field(..., min_length=1, max_length=6)


class ComparisonRow(BaseModel):
    """比較矩陣中的一列。columns 長度由 caller / preset 約定（通常 2-4）。"""

    label: str = Field(..., min_length=1, max_length=80)
    columns: list[str] = Field(..., min_length=1, max_length=6)


class TimelineEvent(BaseModel):
    """時間軸上的一筆事件。

    ``date`` 是顯示用字串（"2024-Q1"、"2025-05" 都行），不是 datetime
    — 真實業務上常是區間或 fiscal quarter，硬塞 datetime 沒幫助。
    """

    date: str = Field(..., min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)


class InfographicSpec(BaseModel):
    """The validated, normalised infographic intent.

    LLM 輸出後過 Pydantic 驗證 → s2twp 正規化 → renderer 出 PNG/HTML/PDF。
    所有人讀的字串欄位都會被 normalizer 過一遍（不是這裡的責任 —
    這裡只負責結構約束）。

    各 section list 都允許為空：
    - ``stats`` 0-6 個（>6 視覺上會擠，schema 上限即此）
    - ``charts`` 0-3 個（matplotlib 渲染慢，再多 PDF 就太肥）
    - ``comparison`` None 或 1+ rows
    - ``timeline`` None 或 1+ events

    ``takeaway`` 是 mandatory 一句結語 — 無論哪個 preset，最底都要一個
    結論，這是「不要 data slop」的硬規則：產出有方向感。
    """

    title: str = Field(..., min_length=1, max_length=120)
    subtitle: str | None = Field(default=None, max_length=200)
    preset: InfographicPreset
    stats: list[StatBlock] = Field(default_factory=list, max_length=6)
    charts: list[ChartSpec] = Field(default_factory=list, max_length=3)
    comparison: list[ComparisonRow] | None = Field(default=None, max_length=12)
    timeline: list[TimelineEvent] | None = Field(default=None, max_length=12)
    takeaway: str = Field(..., min_length=1, max_length=400)


class GenerateInfographicRequest(BaseModel):
    """``POST /api/infographics/jobs`` 入參。

    Mirrors GenerateSpecRequest（slide pipeline）— collection_id 是
    csp ingestion collection 的 PK；同一個 user 用同一個 bearer 鑑權。
    """

    collection_id: int = Field(..., ge=1)
    preset: InfographicPreset
    seed_query: str | None = Field(default=None, max_length=200)
    extra_instructions: str | None = Field(default=None, max_length=2000)
    document_ids: list[int] | None = None
    top_k: int = Field(default=12, ge=1, le=30)
    # Slice 8b: optional ALM task binding (governance passthrough only).
    task_id: str | None = Field(default=None, max_length=64)
    source_snapshot_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class InfographicJobStatus(BaseModel):
    """Polling 用 status payload — slide JobStatus 的鏡像版。"""

    job_id: str
    state: Literal["pending", "running", "done", "failed", "cancelled"]
    step: str | None = None
    title: str | None = None
    preset: InfographicPreset | None = None
    chart_count: int | None = None
    error: str | None = None
    # 兩個 download endpoint 的相對路徑 ── 前端拼上 host 即可直接 GET。
    # None 直到 state == "done" 才填上。
    download_urls: dict[str, str] | None = None
    # Slice 8b: CSP artifact passthrough — set once the artifact registers.
    artifact_id: str | None = None
    classification_level: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Job step labels（pollings UI 看的 "鑄造中：..." 提示字串） ─────────────

JOB_STEP_QUEUED = "queued"
JOB_STEP_RETRIEVING = "retrieving"
JOB_STEP_GENERATING = "generating"
JOB_STEP_RENDERING_CHARTS = "rendering_charts"
JOB_STEP_RENDERING_HTML = "rendering_html"
JOB_STEP_RENDERING_PDF = "rendering_pdf"
JOB_STEP_DONE = "done"

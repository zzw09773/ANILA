"""Pydantic schemas for the Studio Report (deep report) pipeline.

## Design philosophy

Mirrors the Slides schema split (see ``app/schemas/studio.py``) but with
a separate file so the two pipelines never share validators, defaults,
or normalisation rules. Slides are visual artifacts whose constraints
(palette, layout_kind, bullets[]) are not meaningful for a report.

Report is a hierarchical document:

    ReportSpec
      title          (top-level title)
      preset         (DEEP_TECH_REVIEW | KEY_SUMMARY | TEACHING_HANDOUT | EXTERNAL_COMMS)
      tldr           (100-200 char executive summary)
      sections[]     (## headings — recursive subsections[] supported)
        heading
        content_markdown  (markdown with [N] citation markers)
        subsections[]     (### / #### headings — same shape)
      references[]   (numbered citation table; [N] in content_markdown → references[n-1])
      generated_at   (UTC)

## Validation philosophy

Same dual-layer approach as Slides:
- Loose at the LLM boundary (missing optional fields fall back gracefully).
- Strict at the renderer (Jinja2 templates expect well-formed text).

The renderer never sees a partial spec — pipeline assembles a full
ReportSpec before any render call. If the LLM omits sections we fail
fast with a clear error, not a half-rendered HTML.

## Subsection recursion

``ReportSection.subsections`` is a list of the same type. Pydantic v2
supports forward refs via ``model_rebuild()`` at module load time
(invoked at the bottom of the file). The renderer caps recursion depth
at 3 levels (## / ### / ####) for readability; deeper trees are flattened.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ReportPreset(str, Enum):
    """The 4 supported report presets.

    Each preset maps to:
    - a Jinja2 template variant (different typography / colour / spacing),
    - a different LLM prompt voice (formal academic / executive / tutorial /
      enterprise),
    - a different default section structure hint.

    Adding a new preset is a 3-line change: add the enum, add the template,
    add the prompt voice in ``report_runner``.
    """

    DEEP_TECH_REVIEW = "deep_tech_review"
    KEY_SUMMARY = "key_summary"
    TEACHING_HANDOUT = "teaching_handout"
    EXTERNAL_COMMS = "external_comms"


class ReportReference(BaseModel):
    """One row in the references table.

    ``n`` is the citation number that appears inline as ``[N]`` inside
    section ``content_markdown``. It is 1-indexed (matches LaTeX/IEEE
    style). The pipeline assigns ``n`` deterministically based on chunk
    discovery order, not LLM output, so the same chunk always gets the
    same number across re-runs.
    """

    n: int = Field(ge=1, description="1-indexed citation number")
    filename: str = Field(min_length=1, max_length=255)
    chunk_id: int | None = None
    excerpt: str | None = Field(
        default=None,
        max_length=400,
        description="~100-char excerpt of the source chunk for display",
    )


class ReportSection(BaseModel):
    """One section in the report. Recursive — supports nested subsections.

    The renderer maps depth → heading level: top-level sections are
    ``<h2>``, subsections are ``<h3>``, sub-subsections are ``<h4>``.
    Deeper than that is flattened to ``<h4>`` (avoid heading inflation).

    ``content_markdown`` accepts inline citation markers like ``[3]`` which
    the renderer turns into clickable links to the references table.
    """

    heading: str = Field(min_length=1, max_length=200)
    content_markdown: str = Field(
        default="",
        description="markdown body; may contain [N] citation markers",
    )
    subsections: list["ReportSection"] = Field(default_factory=list)

    @field_validator("heading")
    @classmethod
    def _strip_heading(cls, v: str) -> str:
        """Trim leading/trailing whitespace; reject empty after strip."""
        v = v.strip()
        if not v:
            raise ValueError("heading must not be empty after strip")
        return v


class ReportSpec(BaseModel):
    """Top-level report payload.

    Built by the pipeline (retrieval → outline → drafting → assembly),
    consumed by the renderer (HTML/PDF/DOCX). Never serialised to the
    wire as-is — clients only see ``ReportJobStatus`` projection.
    """

    title: str = Field(min_length=1, max_length=300)
    preset: ReportPreset
    tldr: str = Field(
        min_length=20,
        max_length=600,
        description="100-200 char executive summary; soft cap 600 for resilience",
    )
    sections: list[ReportSection] = Field(min_length=1, max_length=20)
    references: list[ReportReference] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title must not be empty after strip")
        return v

    @field_validator("references")
    @classmethod
    def _validate_reference_numbering(
        cls, v: list[ReportReference]
    ) -> list[ReportReference]:
        """Ensure ``n`` values are unique. Gaps (1,2,5) are allowed — the
        LLM might cite [1], [2], [5] without [3]/[4] being used; we just
        need uniqueness so [N] dereferences are deterministic.
        """
        ns = [r.n for r in v]
        if len(ns) != len(set(ns)):
            raise ValueError("references[].n must be unique")
        return v


# Pydantic v2 forward-ref resolution for the recursive ReportSection.
ReportSection.model_rebuild()


# ── API request / response shapes ────────────────────────────────────────


class GenerateReportRequest(BaseModel):
    """POST /api/reports/jobs payload."""

    collection_id: int = Field(ge=1)
    preset: ReportPreset
    extra_instructions: str | None = Field(
        default=None,
        max_length=2000,
        description="user-supplied extra hints to flavour the report",
    )
    document_ids: list[int] | None = Field(
        default=None,
        description="optional list to restrict retrieval to specific docs",
    )
    top_k: int = Field(default=12, ge=1, le=30)
    # Slice 8b: optional ALM task binding (governance passthrough only).
    task_id: str | None = Field(default=None, max_length=64)
    source_snapshot_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)


class ReportJobStatus(BaseModel):
    """Response shape for POST /jobs (202) and GET /jobs/{id}.

    Mirrors ``app.schemas.studio.JobStatus`` but with report-specific
    fields (preset, sections_count, references_count, download_urls).
    Slide-specific fields (defects, qa_passes, slide_count) are not here.
    """

    job_id: str
    state: Literal["pending", "running", "done", "failed", "cancelled"]
    step: str | None = None
    title: str | None = None
    preset: ReportPreset | None = None
    references_count: int | None = None
    sections_count: int | None = None
    error: str | None = None
    download_urls: dict[str, str] | None = Field(
        default=None,
        description=(
            "{'html': ..., 'pdf': ..., 'docx': ...} — relative API paths, "
            "not absolute URLs. Frontend axios prepends its base URL."
        ),
    )
    # Slice 8b: CSP artifact passthrough — set once the artifact registers.
    artifact_id: str | None = None
    classification_level: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Pipeline step labels (string constants — same idiom as Slides) ───────


JOB_STEP_QUEUED = "queued"
JOB_STEP_RETRIEVING = "retrieving"
JOB_STEP_OUTLINING = "outlining"
JOB_STEP_DRAFTING = "drafting"
JOB_STEP_NORMALIZING = "normalizing"
JOB_STEP_RENDERING = "rendering"
JOB_STEP_DONE = "done"

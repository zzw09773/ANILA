"""Pydantic schemas for the Studio (slide generation) pipeline.

## Validation philosophy

Two principles, applied at *every* schema level:

1. **Loose at the boundary, strict at the renderer.** The LLM is fallible.
   Every extra field we make required raises the probability of a 422 →
   correction pass → another 422 → "失敗" toast on the user's screen.
   Phase 3 introduces several new optional fields (palette, layout_kind,
   layout-specific structures); none of them are *required* — if the LLM
   omits or mangles them, the renderer falls back to the safe `standard`
   layout with `bullets[]` and the default palette. The user gets a
   slightly less polished deck instead of an error.
2. **Bullets remain canonical.** Even when `layout_kind` is one of the
   non-standard variants, `bullets[]` must be present. This guarantees
   the renderer always has *something* to show: if the layout-specific
   field is missing or invalid, we degrade to standard rendering of the
   bullets without an exception.

## Phase 3 additions

- `SlidesSpec.palette`              one of 4 named palettes (default
                                    navy_amber). Renderer maps to a colour
                                    set so the LLM doesn't have to think
                                    in hex.
- `Slide.layout_kind`               selects one of 6 visual templates;
                                    fallback to "standard" if unknown.
- `Slide.stat` / `quote` / `columns` / `icon_rows`
                                    layout-specific structured payloads,
                                    used only by the renderer for the
                                    matching layout_kind.
- `IconRow.concept`                 a closed-set semantic keyword (NOT a
                                    react-icons name). Mapping from
                                    concept → specific Heroicons happens
                                    deterministically in the renderer
                                    (see pptx-skill/icons.js); the schema
                                    only constrains length, the rendering
                                    layer is the source of truth on which
                                    concepts are recognised.
"""
from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Palette / layout enums ────────────────────────────────────────────────
#
# We deliberately accept these as plain strings (not Literal) so that an
# LLM emitting "Navy_Amber" or "navy" or a misspelling doesn't fail
# validation. The field validator normalises and falls back to the
# default. The renderer ALSO checks the value before dispatch and falls
# back to "standard" / "navy_amber" if it doesn't recognise the string —
# defense in depth, since the schema is the LLM-facing contract and the
# renderer is the visual-output contract.

PALETTES: tuple[str, ...] = (
    "navy_amber",
    "forest_moss",
    "charcoal_minimal",
    "coral_energy",
)

LAYOUT_KINDS: tuple[str, ...] = (
    "standard",
    "section_break",
    "stat_callout",
    "quote",
    "two_column",
    "icon_rows",
    # Phase 5: layout that puts a real image (extracted from a source
    # PDF / docx) on one half of the slide. Requires Slide.image_ref to
    # point at an ingestion_images row; renderer falls back to
    # `standard` if image_ref is missing or unresolvable.
    "image_focus",
)


# ── Layout-specific payloads ──────────────────────────────────────────────
#
# Each layout that needs structured data beyond `bullets[]` declares a
# small Pydantic model. They're always optional on the parent Slide; the
# renderer's dispatcher reads them only when the corresponding
# layout_kind is selected. Unused payloads on a slide get serialised
# (None) and ignored — there's no enforcement that "if layout_kind=quote
# then quote != None"; that lives in the renderer's degrade-to-standard
# fallback.


class Stat(BaseModel):
    """Big-number callout. Used by layout_kind='stat_callout'.

    `value` is intentionally a string (not a number) so the LLM can
    output "47%", "12K", "3.5×" without us having to model units.
    """

    value: str = Field(..., min_length=1, max_length=20)
    label: str = Field(..., min_length=1, max_length=120)
    supporting: str | None = Field(default=None, max_length=200)


class Quote(BaseModel):
    """Pull-quote layout. Used by layout_kind='quote'."""

    text: str = Field(..., min_length=1, max_length=500)
    attribution: str | None = Field(default=None, max_length=120)


class Column(BaseModel):
    """One side of a two_column layout."""

    heading: str = Field(..., min_length=1, max_length=120)
    bullets: list[str] = Field(..., min_length=1, max_length=6)


class IconRow(BaseModel):
    """One row of icon_rows layout — icon + heading + body.

    `concept` is a *semantic keyword* (e.g. 'data_pipeline', 'security'),
    not a Heroicons name. The renderer's CONCEPT_MAP resolves it to a
    specific icon. Unknown concepts render without an icon (the row
    just shows heading + description), keeping a single failure mode
    instead of spilling LLM hallucinations onto the slide.
    """

    concept: str = Field(..., min_length=1, max_length=40)
    heading: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=200)


class Slide(BaseModel):
    """One slide — title, bullets, optional speaker notes, optional layout payloads."""

    title: str = Field(..., min_length=1, max_length=200)
    bullets: list[str] = Field(..., min_length=1, max_length=8)
    speaker_notes: str | None = Field(default=None, max_length=2000)

    # Phase 3: optional layout selection. Default "standard" keeps the
    # existing behaviour for any old prompt that doesn't know about
    # layout variants.
    layout_kind: str = Field(default="standard", max_length=40)

    # Layout-specific payloads. Each is only read when layout_kind
    # matches; absence triggers renderer fallback to standard.
    stat: Stat | None = None
    quote: Quote | None = None
    columns: list[Column] | None = Field(default=None, max_length=3)
    icon_rows: list[IconRow] | None = Field(default=None, max_length=6)
    # Phase 5: opaque ID into ingestion_images that the LLM picks from
    # the "可用圖" prompt list. The renderer-side path resolves it to
    # actual image bytes; if unresolvable we fall back to `standard`.
    # Validation is intentionally lax — we only check max_length so
    # malformed values don't crash, the renderer's `if image_data` guard
    # is the real safety net.
    image_ref: str | None = Field(default=None, max_length=64)

    @field_validator("title", "speaker_notes")
    @classmethod
    def _strip_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("layout_kind")
    @classmethod
    def _normalise_layout_kind(cls, v: str) -> str:
        # Lowercase + strip; map unknown values to "standard" so the LLM
        # mis-emitting "Stat_Callout" or "icon-rows" still produces a
        # valid spec. This is the lax-input principle in action.
        normalised = v.strip().lower().replace("-", "_")
        if normalised not in LAYOUT_KINDS:
            return "standard"
        return normalised

    @field_validator("bullets")
    @classmethod
    def _bullets_clean(cls, v: list[str]) -> list[str]:
        # Drop empty / whitespace-only bullets so the LLM emitting `["", "x"]`
        # doesn't render a blank line. If everything was empty, raise so the
        # correction pass can re-generate that slide.
        cleaned = [b.strip() for b in v if isinstance(b, str) and b.strip()]
        if not cleaned:
            raise ValueError("slide 必須至少一個 non-empty bullet")
        if len(cleaned) > 8:
            raise ValueError(f"bullet 數量超過上限 8（當前 {len(cleaned)}）")
        # Reject obvious placeholder text the LLM sometimes leaves behind
        # (Lorem ipsum, "TODO", "TBD", "<insert ...>"). The pptx-skill
        # SKILL.md explicitly calls these out as failure modes.
        forbidden = ("lorem ipsum", "<insert", "[insert", "TBD", "TODO:")
        for b in cleaned:
            low = b.lower()
            if any(p.lower() in low for p in forbidden):
                raise ValueError(f"bullet 含 placeholder 文字：{b!r}")
        return cleaned


class SlidesSpec(BaseModel):
    """Top-level slide deck spec — what the LLM emits, what the renderer reads."""

    title: str = Field(..., min_length=1, max_length=200)
    slides: list[Slide] = Field(..., min_length=1, max_length=30)

    # Phase 3: top-level palette. Default keeps existing decks visually
    # identical to Phase 2 output.
    palette: str = Field(default="navy_amber", max_length=40)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title 不可為空字串")
        return v

    @field_validator("palette")
    @classmethod
    def _normalise_palette(cls, v: str) -> str:
        # Same lax-input approach as layout_kind. The renderer ALSO
        # falls back if it doesn't know the palette name, but doing it
        # here means the field has the canonical value when other code
        # (e.g. the QA correction pass that re-prompts with the spec)
        # reads it.
        normalised = v.strip().lower().replace("-", "_")
        if normalised not in PALETTES:
            return "navy_amber"
        return normalised

    @model_validator(mode="after")
    def _check_unique_slide_titles(self) -> Self:
        # Duplicate titles read as "the LLM forgot what it already wrote"
        # — common failure mode where the planning step (step 3 in the
        # flowchart) collapsed two ideas into one. Reject so step-6
        # correction has a chance to disambiguate.
        # Section breaks are exempt: titles like "第二部分" or "結語" can
        # legitimately repeat a top-level section title; the dup check
        # would be a false positive there.
        seen: dict[str, int] = {}
        for s in self.slides:
            if s.layout_kind == "section_break":
                continue
            seen[s.title] = seen.get(s.title, 0) + 1
        dups = [t for t, n in seen.items() if n > 1]
        if dups:
            raise ValueError(
                f"slides 出現重複 title（{', '.join(dups)}）—"
                "請拆解、合併或重命名"
            )
        return self


class GenerateSpecRequest(BaseModel):
    """Input to ``/api/studio/slides/generate-spec``."""

    collection_id: int = Field(..., ge=1)
    preset: str = Field(..., min_length=1, max_length=80)
    extra_instructions: str | None = Field(default=None, max_length=2000)
    # Knob to skip retrieval entirely if the user explicitly wants
    # "just use general knowledge". Default false (always retrieve).
    skip_retrieval: bool = False


class VisualDefect(BaseModel):
    """One issue spotted by the vision QA pass on a rendered slide."""

    slide_index: int = Field(..., ge=0, description="0-indexed slide that has the defect")
    severity: str = Field(..., pattern="^(critical|warning|info)$")
    summary: str = Field(..., max_length=500)


class RenderResult(BaseModel):
    """What the render endpoint returns when it does NOT stream the binary."""

    job_id: str
    spec: SlidesSpec
    defects: list[VisualDefect] = Field(default_factory=list)
    qa_passes: int = Field(default=0, description="Number of vision-QA loops that ran")
    download_path: str


# ── Job-based async pipeline (long-term fix for header-buffer / blocking-modal) ──
#
# The original synchronous /slides/generate held the HTTP connection open for
# 60-180 s and stuffed all metadata (title, defects[]) into headers. CJK
# percent-encoding made defects blow past nginx's 8 KB upstream buffer → 502;
# the long await also forced the UI to keep its modal open.
#
# Job-based replacement:
#   POST /api/studio/slides/jobs           → 202 {job_id}; pipeline runs
#                                              in an asyncio task
#   GET  /api/studio/slides/jobs/{id}      → JobStatus JSON (cheap polling)
#   GET  /api/studio/slides/jobs/{id}/pptx → binary, only when state="done"
#
# The metadata (defects, title, error) lives in JSON bodies, not headers, so
# size is no longer an issue.


class JobState(BaseModel):
    """Possible job lifecycle states.

    Modeled as a Pydantic `BaseModel`-shaped enum-string by living on
    `JobStatus.state` rather than a dedicated enum class — it keeps the
    OpenAPI schema simple (string with pattern), and the value set is
    closed enough that the frontend can switch on it.
    """

    pass


# Step labels surfaced to the UI for "鑄造中：<step>" indicators. Kept as
# constants (not an enum) so additions don't require a schema migration.
JOB_STEP_QUEUED = "queued"
JOB_STEP_RETRIEVING = "retrieving"
JOB_STEP_GENERATING = "generating"
JOB_STEP_RENDERING = "rendering"
JOB_STEP_QA = "qa"
JOB_STEP_FIXING = "fixing"
JOB_STEP_DONE = "done"


class JobStatus(BaseModel):
    """Polling response for a Studio job. Body is JSON, header-buffer-safe."""

    job_id: str
    # state is the lifecycle phase: pending|running|done|failed|cancelled.
    # We keep this a plain string (with regex) instead of an enum so the
    # frontend doesn't need to mirror a Python enum class.
    state: str = Field(..., pattern="^(pending|running|done|failed|cancelled)$")
    # Free-form step label — see JOB_STEP_* constants above. Useful for the
    # UI to render "鑄造中：視覺檢查" without knowing internal pipeline shape.
    step: str | None = None
    # Populated once the LLM has produced a SlidesSpec (so the UI can show
    # the eventual filename even before render completes).
    title: str | None = None
    # Populated once render succeeds; visible on done state.
    slide_count: int | None = None
    # Populated once vision QA finishes. Empty list means "QA ran clean".
    defects: list[VisualDefect] = Field(default_factory=list)
    qa_passes: int = 0
    # Failure mode — only populated on state="failed". Plain string,
    # already user-safe (no traceback bytes).
    error: str | None = None
    # ISO 8601 timestamps so the UI can show "鑄造中 1m 30s" style age.
    created_at: str
    updated_at: str

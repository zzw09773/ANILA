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

from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


# ── FLUX image use-case (Stage 1 locked contract 3.1) ─────────────────────
#
# `ImageUseCase` is the cross-stage knob that decides aspect ratio, prompt
# style, and whether a use-case is even wired into hydration yet. It is a
# string-valued Enum so it serialises cleanly into `image_gen_meta` (3.5)
# and into the FLUX provider cache key, and so the renderer / frontend can
# switch on the plain string value without mirroring a Python enum.
#
# Stage 1 only ENABLES `COVER_HERO` in the hydration layer; SECTION_BAND
# and CONTENT_ILLUSTRATION are defined here (and understood by the provider
# aspect map in flux_image_provider.py) so Stage 2-4 only fill behaviour,
# never reshape this contract.
class ImageUseCase(str, Enum):
    COVER_HERO = "cover_hero"  # 封面背景意象，16:9 全幅
    SECTION_BAND = "section_band"  # 章節扉頁裝飾帶，3:1 letterbox
    CONTENT_ILLUSTRATION = "content_illustration"  # 內容頁概念插圖，4:3


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

# Round 3 Patch L: themes are the new top-level visual identity unit.
# A theme bundles palette + typography + chrome + icon treatment + density.
# The renderer (server.js) interprets theme.id and applies the bundle.
#
# Existing `palette` field is preserved as a deprecated alias — jobs that
# set palette but not theme will resolve to the equivalent theme via
# _PALETTE_TO_THEME below.
THEMES: tuple[str, ...] = (
    "corporate_navy",
    "academic_paper",
    "warm_journal",
    "executive_brief",
    "startup_pitch",
)

# Old palette → equivalent new theme. Used when a request specifies
# palette without theme (legacy clients) so they keep working.
_PALETTE_TO_THEME: dict[str, str] = {
    "navy_amber": "corporate_navy",
    "forest_moss": "warm_journal",
    "charcoal_minimal": "academic_paper",
    "coral_energy": "startup_pitch",
    # NB: executive_brief has no direct palette ancestor — new theme.
}

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

    Studio Fix 3 (2026-05-18): `supporting` is now mandatory with a 20-char
    minimum so we stop accepting LLM filler like "重要突破" — the renderer
    relies on a meaty supporting line to fill vertical space below the
    big number. `baseline` / `baseline_label` are optional; when present
    the renderer switches to a left-vs-right comparison layout.
    """

    value: str = Field(..., min_length=1, max_length=20)
    label: str = Field(..., min_length=1, max_length=120)
    supporting: str = Field(..., min_length=20, max_length=200)
    baseline: str | None = Field(default=None, max_length=20)
    baseline_label: str | None = Field(default=None, max_length=60)


class Quote(BaseModel):
    """Pull-quote layout. Used by layout_kind='quote'."""

    text: str = Field(..., min_length=1, max_length=500)
    attribution: str | None = Field(default=None, max_length=120)


class Column(BaseModel):
    """One side of a two_column layout.

    Studio Fix 3 (2026-05-18): `bullets` floor raised from 1 → 3 so the
    LLM couldn't ship two-column slides with one bullet per side leaving
    the layout 70% empty.

    Round 2 Patch C (2026-05-18): floor dropped 3 → 2. Empirically the
    "min 3" rule was too strict — legitimate technical comparisons
    (e.g. v2 slide 5 "雙分支特徵融合": RGB 原圖 vs Tsallis Entropy)
    frequently have exactly 2 clean distinguishing points per side, and
    those slides were getting demoted to standard layout, losing the
    side-by-side framing entirely. Sparse columns (<2 bullets each) are
    now upgraded to `icon_rows` (preserving the parallel-concepts feel)
    rather than flattened to `standard` — see `_saturate_spec_dict` in
    `app.api.studio`.
    """

    heading: str = Field(..., min_length=1, max_length=120)
    bullets: list[str] = Field(..., min_length=2, max_length=6)


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

    # Phase 6 (2026-05-18): English prompt that the LLM emits when it
    # wants a freshly generated illustration (FLUX.2-dev). Mutually
    # exclusive with image_ref by convention — if both are set, the
    # hydration layer prefers image_ref. Length capped at 500 to avoid
    # LLMs writing essays. Hydration silently drops on FLUX failure
    # (same fallback as image_ref).
    image_prompt: str | None = Field(default=None, max_length=500)

    # Studio Fix 2 (2026-05-18): split image_focus into two modes.
    # `image_kind` is the *discriminator* the LLM emits to declare intent:
    #
    #   illustration → FLUX.2-dev path (image_prompt, atmospheric/concept art)
    #   diagram      → Graphviz path   (diagram_dot, crisp labelled diagrams)
    #
    # FLUX is a diffusion model and can't render legible text in images
    # (the "Geneeration / KIGDKED" garbage we saw on slide 9). When the
    # LLM wants a labelled diagram (architecture, flow, ER), it writes
    # Graphviz DOT and we render server-side via `dot -Tpng`.
    #
    # When image_kind is None the field is treated as "legacy / no
    # intent declared" — image_prompt may still be present (the Phase 6
    # path) without triggering validation; the hydration layer prefers
    # image_ref > diagram_dot > image_prompt.
    image_kind: Literal["illustration", "diagram"] | None = None
    diagram_dot: str | None = Field(default=None, max_length=3000)

    # FLUX Stage 1 locked contract 3.5: audit trail back-filled by the
    # hydration layer after a FLUX image is generated. Powers the audit
    # table write, the frontend "重新生成這張圖" button (Stage 4), and
    # cache debugging. Stays None on slides that don't go through the
    # FLUX rewriter path (image_ref / diagram / legacy image_prompt).
    #
    # Shape (all keys optional; Stage 1 fills the first four, Stage 2 the
    # gate-related ones):
    #   { use_case, flux_prompt, seed, style_id, steps, guidance,
    #     clip_score, vlm_verdict, retry_count, model_sha, image_sha }
    #
    # Kept as a free-form dict (not a sub-model) on purpose: it is an
    # append-only audit blob, and forcing every Stage 2-4 field to be
    # declared here would make the schema the bottleneck the cross-stage
    # contracts are designed to avoid.
    image_gen_meta: dict | None = Field(default=None)

    # Product loop: per-slide source cites so the in-app preview can open
    # the supporting chunk (NotebookLM slides often have none). 1-based
    # indexes into JobStatus.sources. Optional — old decks / fallback
    # templates stay valid without them.
    citation_refs: list[int] = Field(default_factory=list, max_length=12)
    chunk_id: str | None = Field(default=None, max_length=128)

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

    @model_validator(mode="after")
    def _check_image_kind_consistency(self) -> Self:
        """Studio Fix 2 (2026-05-18): enforce illustration/diagram split.

        Only fires when image_kind is explicitly declared. Legacy paths
        (Phase 5 image_ref, Phase 6 bare image_prompt with no image_kind)
        remain valid — the hydration layer keeps its existing priority.

        Rules:
          - image_kind="illustration" → must have image_prompt, no diagram_dot
          - image_kind="diagram"      → must have diagram_dot, no image_prompt
        """
        if self.image_kind == "illustration":
            if not self.image_prompt:
                raise ValueError(
                    "image_kind='illustration' 必須附帶 image_prompt"
                )
            if self.diagram_dot:
                raise ValueError(
                    "image_kind='illustration' 不可附帶 diagram_dot"
                    "（請改用 image_kind='diagram'）"
                )
        elif self.image_kind == "diagram":
            if not self.diagram_dot:
                raise ValueError(
                    "image_kind='diagram' 必須附帶 diagram_dot（Graphviz DOT）"
                )
            if self.image_prompt:
                raise ValueError(
                    "image_kind='diagram' 不可附帶 image_prompt"
                    "（請改用 image_kind='illustration'）"
                )
        return self


class SlidesSpec(BaseModel):
    """Top-level slide deck spec — what the LLM emits, what the renderer reads."""

    title: str = Field(..., min_length=1, max_length=200)
    slides: list[Slide] = Field(..., min_length=1, max_length=30)

    # Phase 3: top-level palette.
    # DEPRECATED (Round 3 Patch L) — use `theme` instead. Kept for backwards
    # compat. Resolved to equivalent theme via _PALETTE_TO_THEME if `theme`
    # is unset (see `_resolve_theme_from_palette` below).
    palette: str = Field(
        default="navy_amber",
        max_length=40,
        description=(
            "DEPRECATED — use `theme` instead. Kept for backwards compat. "
            "Resolved to equivalent theme via _PALETTE_TO_THEME if `theme` "
            "is unset."
        ),
    )

    # Round 3 Patch L: theme is the new top-level visual identity unit.
    # Bundles palette + typography + chrome + icon treatment + density.
    # If None, resolves from the legacy `palette` field at validation time
    # via `_resolve_theme_from_palette`.
    theme: Literal[
        "corporate_navy",
        "academic_paper",
        "warm_journal",
        "executive_brief",
        "startup_pitch",
    ] | None = Field(
        default=None,
        description=(
            "Visual identity bundle (Round 3 Patch L). Bundles palette + "
            "typography + chrome + icon treatment + density. If None, "
            "resolves from the legacy `palette` field at validation time."
        ),
    )

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
    def _resolve_theme_from_palette(self) -> Self:
        """Round 3 Patch L: if theme is unset, derive from legacy palette.

        Runs before `_check_unique_slide_titles` (validator order = declaration
        order). Legacy clients that only set `palette` get a sensible `theme`
        automatically; modern clients setting `theme` explicitly are unaffected.
        """
        if self.theme is None:
            self.theme = _PALETTE_TO_THEME.get(self.palette, "corporate_navy")
        return self

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
    # ── Slice 8b: optional ALM task binding ──
    # ALM creates a CSP Task before launching generation and threads its
    # ids here so studio can report the artifact-job / artifact / trace
    # against the right task. All optional and purely for governance
    # passthrough — omitting them never changes generation behaviour.
    task_id: str | None = Field(default=None, max_length=64)
    source_snapshot_id: str | None = Field(default=None, max_length=64)
    trace_id: str | None = Field(default=None, max_length=64)
    # Round 3 Patch P: API-side bypass of LLM theme selection.
    # When set to a valid THEMES value, the pipeline overwrites
    # spec.theme with this value AFTER Pydantic validation but BEFORE
    # render — operators / advanced users who know the audience better
    # than the LLM can force a specific visual identity. Literal keeps
    # invalid values out at request time (422), instead of silently
    # being ignored mid-pipeline.
    theme_override: Literal[
        "corporate_navy",
        "academic_paper",
        "warm_journal",
        "executive_brief",
        "startup_pitch",
    ] | None = Field(
        default=None,
        description=(
            "If set, bypasses LLM theme selection and forces this theme. "
            "Useful when the user knows the audience better than the LLM. "
            "Must be one of THEMES; invalid values rejected by Literal."
        ),
    )
    # Product loop: restrict RAG to the documents the user picked, and
    # name the audience so the LLM can change density / tone. Both
    # optional — omitting them keeps the old "whole collection" path.
    document_ids: list[int] | None = Field(default=None, max_length=200)
    audience: str | None = Field(default=None, max_length=80)


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
# Studio Fix 1 (2026-05-18): post-validation audit + LLM rebalance pass.
# Inserted between `generating` and `rendering` so the UI can show
# "鑄造中：版型重新平衡" when the audit detects standard-overuse / missing
# stat_callout for numeric content. Skipped (transparent to UI) when no
# hard violations fire.
JOB_STEP_REBALANCING = "rebalancing"
JOB_STEP_RENDERING = "rendering"
JOB_STEP_QA = "qa"
JOB_STEP_FIXING = "fixing"
JOB_STEP_DONE = "done"
JOB_STEP_REGENERATING = "regenerating"

# User-facing generate gate. Zero indexed / zero retrieved sources
# block the job instead of shipping an ungrounded deck.
NO_INDEXED_SOURCES = "先上傳或等索引完成"


class SlideSource(BaseModel):
    """One retrieved chunk the in-app deck preview can open."""

    index: int = Field(..., ge=1)
    document_id: int | None = None
    document_name: str = ""
    chunk_id: str = ""
    snippet: str = ""
    page: int | None = None


class RegenerateSlideRequest(BaseModel):
    """Input to ``POST /slides/jobs/{id}/slides/{n}/regenerate``."""

    extra_instructions: str | None = Field(default=None, max_length=1000)


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
    # Soft warning that coexists with state="done". Used when the
    # pipeline had to ship a degraded / fallback deck (LLM schema
    # exhaustion) so the SPA can tell the user "this succeeded, but
    # it is not the real deck" instead of looking like a clean win.
    warning: str | None = None
    # Slice 8b: control-plane passthrough — populated once the produced
    # artifact is registered on CSP (POST /v1/artifacts). Absent until then.
    artifact_id: str | None = None
    classification_level: str | None = None
    # In-app deck preview + per-slide source open. Populated on done.
    spec: SlidesSpec | None = None
    sources: list[SlideSource] = Field(default_factory=list)
    # ISO 8601 timestamps so the UI can show "鑄造中 1m 30s" style age.
    created_at: str
    updated_at: str

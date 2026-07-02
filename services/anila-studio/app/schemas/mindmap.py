"""Pydantic schemas for the Studio Mindmap (心智圖) pipeline.

## Why a separate module

Mindmap output is structurally distinct from slide decks:

  * The tree is **recursive**. ``MindmapNode`` references itself; a deck
    is a flat ``list[Slide]``.
  * The render target is **SVG via Graphviz**, not .pptx via the Node
    renderer. The download endpoint streams a single file, no QA loop,
    no vision check.
  * The LLM call only needs a tiny "hierarchy from chunks" prompt — no
    layout enums, no icon whitelist, no theme picker.

Keeping it in its own schema module avoids cross-polluting the slides
schema with mindmap-only fields, and lets reviewers see exactly what the
mindmap pipeline accepts / emits.

## Validation philosophy (mirrors slides)

* **Loose at the boundary, strict at the renderer.** The LLM must emit
  valid JSON that parses into ``MindmapSpec``; if the recursive ``root``
  is too deep, missing labels, or has empty children arrays the validator
  surfaces a clear error and the pipeline retries once (handled by the
  endpoint runner, not in this file).
* **No mutation in helpers.** ``MindmapNode`` is a ``BaseModel`` with the
  default Pydantic behaviour (mutable instances, but ``model_copy`` is
  the canonical clone path); callers should not mutate ``children`` in
  place — see ``mindmap_renderer.spec_to_dot`` for the read-only walk
  pattern.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ── Presets ───────────────────────────────────────────────────────────────
#
# 4 mindmap shapes. Picking one constrains both the LLM system prompt
# (which structure to emit) and the renderer (which root-node fill colour
# to apply). The frontend's picker maps each to a human label; the
# enum value is the wire form.


class MindmapPreset(str, Enum):
    """The shape of mindmap the user wants.

    Values are stable strings — the renderer's preset → fill-colour
    mapping and the LLM system prompt's "shape hint" both switch on
    these values, so changing them is a breaking change.
    """

    CONCEPT_TREE = "concept_tree"  # 概念樹 — 從根概念展開為主題地圖
    TASK_BREAKDOWN = "task_breakdown"  # 任務拆解 — WBS / 工作分解結構
    SOP_FLOW = "sop_flow"  # SOP 流程 — 步驟導向的程序圖
    ORG_RELATIONSHIPS = "org_relationships"  # 組織 / 關係圖


# ── Recursive node ────────────────────────────────────────────────────────
#
# Pydantic v2 supports forward references natively via PEP 563 + the
# annotation-as-string fallback; we still call ``model_rebuild`` at the
# bottom of this file to make the self-reference concrete so callers
# don't see "ForwardRef('MindmapNode')" in the runtime type info.


class MindmapNode(BaseModel):
    """One node in the mindmap tree.

    Fields:
      * ``id`` — short, stable identifier used as the dot node name.
        The LLM is asked to emit short ascii ids like ``n0``, ``n0a``,
        ``n0a1`` so the renderer can deduplicate references cheaply.
      * ``label`` — display text. May contain ``\\n`` to force a line
        break inside the rendered box (Graphviz native behaviour).
      * ``children`` — zero or more child nodes; leaf nodes have an
        empty list (not None) so callers don't need to None-check during
        the recursive walk.
      * ``note`` — optional supporting context (e.g. a citation, a
        rationale). Not rendered onto the SVG today but surfaced via the
        downloaded ``.dot`` source for debug.
    """

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    children: list["MindmapNode"] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


# ── Top-level spec ────────────────────────────────────────────────────────
#
# A complete mindmap is title + preset + root + layout direction. The
# renderer derives Graphviz rankdir from layout; the LLM is asked to emit
# the same enum value the request carries (so unchanged on round-trip).


class MindmapSpec(BaseModel):
    """LLM-emitted, schema-validated mindmap structure.

    ``layout`` mirrors Graphviz's ``rankdir`` attribute:
      * ``LR`` (left-to-right, default) — most readable for concept trees
        because the root sits at the left and concepts cascade right.
      * ``TB`` (top-to-bottom) — natural for SOP / task breakdown.
      * ``BT`` / ``RL`` — included for completeness; rarely chosen.
    """

    title: str = Field(min_length=1, max_length=200)
    preset: MindmapPreset
    root: MindmapNode
    layout: Literal["TB", "LR", "BT", "RL"] = "LR"


# Resolve the forward reference inside ``MindmapNode.children``. Pydantic
# v2 requires this once a self-referencing model is fully declared.
MindmapNode.model_rebuild()


# ── Request / response wire shapes ────────────────────────────────────────


class GenerateMindmapRequest(BaseModel):
    """POST body for ``POST /api/mindmaps/jobs``.

    All retrieval knobs are bounded (max_depth ≤ 5, top_k ≤ 20) so a
    malicious or runaway caller can't force unbounded LLM context. The
    defaults (depth=3, k=8) hit the sweet spot we observed in the
    Studio slides pipeline: enough chunks for the LLM to write
    specifically, not so many the prompt budget blows up.
    """

    collection_id: int
    preset: MindmapPreset
    # No seed_query → derive a sensible default from the preset's
    # Chinese label in the runner so the embedder gets something to
    # cosine-against. The default lives in the runner, not the schema,
    # to keep the schema purely declarative.
    seed_query: str | None = Field(default=None, max_length=500)
    extra_instructions: str | None = Field(default=None, max_length=2000)
    # Optional scoping to a subset of documents within the collection.
    # csp's search endpoint accepts the same shape.
    document_ids: list[int] | None = None
    max_depth: int = Field(default=3, ge=1, le=5)
    top_k: int = Field(default=8, ge=1, le=20)


class MindmapJobStatus(BaseModel):
    """Polling response for ``GET /api/mindmaps/jobs/{job_id}``.

    Body is JSON, header-buffer-safe. ``download_urls`` is populated on
    state=done and keys vary by what got produced — at minimum
    ``"svg"``; ``"dot"`` is included for debug downloads (the DOT source
    helps users tweak the graph offline).
    """

    job_id: str
    state: Literal["pending", "running", "done", "failed", "cancelled"]
    # Free-form step label for the UI; e.g. "retrieving", "generating",
    # "rendering", "done". Kept loose so we can add steps without a
    # schema migration.
    step: str | None = None
    title: str | None = None
    preset: MindmapPreset | None = None
    node_count: int | None = None
    error: str | None = None
    download_urls: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime

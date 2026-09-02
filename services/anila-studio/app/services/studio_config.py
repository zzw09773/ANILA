"""Studio slide-pipeline tunables (god-module split).

Scalar configuration constants extracted from ``api/studio.py`` so the
retrieval / LLM / render / QA / layout modules (extracted in later steps)
can each import the few they need without dragging in the whole studio
module — and without the circular-import risk that re-importing
``app.api.studio`` would create.

These are deliberately module-level constants rather than env-driven
settings: the product contract is "Studio just works" with sensible
admin-tuned defaults, not per-request overridability. Promote any of
these to ``app.config`` / env vars only when a real per-deployment need
appears.
"""

from __future__ import annotations

import os

# ── Retrieval: images ─────────────────────────────────────────────────────
# How many image hits to surface alongside the chunks. Pulling fewer
# than chunks because (a) we have ~10× fewer images than chunks per
# document, (b) the LLM only picks 1-2 per deck, (c) prompt budget
# tightens fast when each image carries a 200-char caption.
STUDIO_IMAGE_TOP_K = 6
STUDIO_IMAGE_MIN_SCORE = 0.25

# ── Retrieval: chunks ─────────────────────────────────────────────────────
# Retrieval depth for slide deck generation. Higher than chat (top-5)
# because Studio synthesises across the whole deck, not a single Q&A turn.
# Was originally 12 / 800 chars / total ~9.6 KB context. Bumped after the
# carbon-thesis case where a 74-page paper produced 100-char/slide bullets
# — symptom of the LLM not having enough context to write specifically.
# New defaults give ~30 KB context, which is well under Gemma 4's 256K
# window but enough to surface every section of a typical paper. Going
# higher costs prompt tokens linearly with little extra value (top-20 hits
# already cover the deck's narrative space).
STUDIO_TOP_K = 20
STUDIO_MIN_SCORE = 0.25
STUDIO_CONTENT_LIMIT_CHARS = 1500

# ── Two-pass generation (2026-09-02) ─────────────────────────────────────
# Outline first, then one retrieval per slide, then content. Off → the
# legacy single call. Env-overridable so a deployment can compare.
TWO_PASS_ENABLED = (os.environ.get("ANILA_STUDIO_TWO_PASS") or "1").strip().lower() not in ("0", "false", "no")
OUTLINE_MAX_TOKENS = 2048
PER_SLIDE_TOP_K = 4
TWO_PASS_CHUNK_CAP = 36

# ── Pipeline retries ──────────────────────────────────────────────────────
# How many times to retry on Pydantic validation failure. One re-roll is
# usually enough; if the LLM emits two malformed responses in a row, the
# pipeline gives up and 422s — the user can retry the request entirely.
SCHEMA_CORRECTION_PASSES = 1

# How many vision-QA → fix → re-render cycles. Keep at 1; more iterations
# tend to produce diminishing returns and eat seconds of wall-clock.
VISUAL_QA_PASSES = 1

# Output budget for the defect-fix call (it re-emits the whole SlidesSpec).
# A 15-slide spec is ~6k tokens; without a cap a thinking-mode model ran
# 336 s on 2026-09-02 and the job died at the 300 s transport timeout.
FIX_MAX_TOKENS = 8192

# ── Services / models ─────────────────────────────────────────────────────
# Renderer service — same docker network, same compose stack.
RENDERER_BASE_URL = "http://pptx-renderer:7100"

# Default LLM for slide generation / vision QA. Env-overridable per
# deployment (variant names differ). These are **analysis-class** tasks
# (deck quality depends on reasoning) — do NOT casually point them at a
# fast／nothink variant; see docs/runbooks/model-variants.md.
SLIDES_LLM_MODEL = (
    (os.environ.get("ANILA_STUDIO_SLIDES_MODEL") or "").strip() or "gemma4"
)
VISION_LLM_MODEL = (
    (os.environ.get("ANILA_STUDIO_VISION_MODEL") or "").strip() or "gemma4"
)

# ── Flux quality gate (Stage 2 / Layer C) ─────────────────────────────────
# How many extra times to regenerate a slide's image when every candidate
# fails the quality gate. attempt 0 + MAX_RETRIES more.
FLUX_GATE_MAX_RETRIES = 3
# Candidates generated per attempt (spec 5: N=2).
FLUX_GATE_NUM_CANDIDATES = 2
# Seed stride between retry attempts so each attempt explores a different
# region of latent space (attempt k uses base_seed + k*1024).
FLUX_GATE_SEED_STRIDE = 1024

# ── Illustration routing (Stage 4) ────────────────────────────────────────
# Hard ceiling on generated images per deck. Beyond this, remaining
# illustration slides take the theme/text fallback instead of spending GPU.
# Protects against runaway latency on decks with many section breaks.
MAX_GENERATED_IMAGES_PER_DECK = 15
# A content slide only earns an illustration (and an image_focus layout)
# when it's sparse enough that "image-led, text-supporting" reads well.
# Denser slides keep their text-only standard layout (the image would
# crowd them).
CONTENT_ILLUSTRATION_MAX_BULLETS = 3

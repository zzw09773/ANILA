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

# 不是模型名稱。call_llm_chat 看到這兩個值就向 CSP 解析對應角色。
# 治理中心沒設時失敗，訊息點名角色；不要在這裡填預設模型名。
from app.services.studio_model_primary import (  # noqa: E402
    SLIDES_ROLE_SENTINEL as SLIDES_LLM_MODEL,
    VISION_ROLE_SENTINEL as VISION_LLM_MODEL,
)

# ── 生成配圖上限 ─────────────────────────────────────────────────────────
# Hard ceiling on generated images per deck. Beyond this, remaining
# illustration slides take the theme/text fallback instead of spending GPU.
# Protects against runaway latency on decks with many section breaks.
MAX_GENERATED_IMAGES_PER_DECK = 15
# A content slide only earns an illustration (and an image_focus layout)
# when it's sparse enough that "image-led, text-supporting" reads well.
# Denser slides keep their text-only standard layout (the image would
# crowd them).
CONTENT_ILLUSTRATION_MAX_BULLETS = 3

"""Studio API — slide deck generation pipeline (job-based, Phase 1 + 2 + LT).

The original sync endpoint was rewritten into a job-based async API to
fix two production issues:

1. **502 "upstream sent too big header"** — the sync endpoint stuffed
   QA defects (CJK percent-encoded, ~3-5 KB) into response headers,
   blowing past nginx's default 8 KB upstream buffer.
2. **Modal blocked for 60-180 s** — the UI couldn't return control to
   the user until the whole pipeline finished, because the response
   only resolved after step 9.

Job-based shape:

    POST /api/studio/slides/jobs        → 202 + {job_id} (returns in <50 ms)
    GET  /api/studio/slides/jobs/{id}   → JobStatus JSON (cheap polling)
    GET  /api/studio/slides/jobs/{id}/pptx → 200 .pptx binary (only when done)
    DELETE /api/studio/slides/jobs/{id} → 200 (cancel)

The pipeline (steps 3-9 of the flowchart) is unchanged; what changed is
when the HTTP response returns. POST returns immediately after
registering the asyncio.Task; the SPA polls /status every couple of
seconds and downloads /pptx once state == "done".

    request → [POST /jobs] → 202 job_id
                  │
                  └─ asyncio task ────────────────────────────────┐
                       │                                           │
                       ▼                                           │
                  [3] retrieve chunks                              │
                       │                                           │
                       ▼                                           │
                  [4-5] LLM emits SlidesSpec JSON                  │
                       │                                           │
                       ▼                                           │
                  [6] Pydantic validate (1 correction retry)       │
                       │                                           │
                       ▼                                           │
                  [7] POST pptx-renderer/render → .pptx bytes      │
                       │                                           │
                       ▼                                           │
                  [8] /screenshots → PNG[] → vision QA             │
                       │                                           │
                       │  critical defects → LLM fix → re-render   │
                       ▼                                           │
                  job state="done", pptx_bytes stored in memory ───┘

The frontend side uses an artifact-store record with state mirroring
job state, and downloads the pptx once state==done.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.auth import (
    CurrentUserIdentity,
    get_bearer_token,
    get_current_user_identity,
)
from app.clients.csp_client import (
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    CspUnauthorizedError,
    get_collection,
)
from app.schemas.studio import (
    JOB_STEP_FIXING,
    JOB_STEP_GENERATING,
    JOB_STEP_QA,
    JOB_STEP_REBALANCING,
    JOB_STEP_RENDERING,
    JOB_STEP_RETRIEVING,
    GenerateSpecRequest,
    JobStatus,
    Slide,
    SlidesSpec,
    VisualDefect,
)
from app.services import job_lifecycle
from app.services import studio_job_service as jobs
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.retrieval_status import RETRIEVAL_FAILED_WARNING
from app.services.studio_config import (
    RENDERER_BASE_URL,
    SCHEMA_CORRECTION_PASSES,
    SLIDES_LLM_MODEL,
    VISION_LLM_MODEL,
    VISUAL_QA_PASSES,
)
from app.services.studio_layout import (
    _apply_theme_title_override,
    _audit_layout_distribution,
    _rebalance_layouts,
    _should_rebalance,
)
from app.services.studio_llm import (
    StudioLLMAdapter as _StudioLLMAdapter,
    build_generation_prompt as _build_generation_prompt,
    call_llm_chat as _call_llm_chat,
)
from app.services.studio_render import (
    _apply_illustration_fallback,
    _generate_slide_illustration,
    _hydrate_images,
    _infer_image_use_case,
    _render_pptx,
    get_active_flux_provider,
    get_flux_provider,
)
from app.services.studio_retrieval import (
    retrieve_chunks as _retrieve_chunks,
    retrieve_images as _retrieve_images,
)
from app.services.studio_text_normalizer import normalize_spec
from app.services.studio_vision_qa import (
    fix_spec_with_defects as _fix_spec_with_defects,
    to_spec_indices as _to_spec_indices,
    visual_qa as _visual_qa,
)

router = APIRouter(prefix="/api/studio", tags=["Studio / Slides"])
logger = logging.getLogger(__name__)

# Soft warning shown on JobStatus when the pipeline ships the
# synthetic fallback deck (LLM schema exhaustion). Kept as a module
# constant so the API path and the regression test cannot drift.
FALLBACK_DECK_WARNING = (
    "模型無法產出合法簡報結構，已改為說明卡。請重試或精簡補充指示。"
)
# The fix-and-rerender pass failed (LLM timeout / bad JSON): the deck the
# user gets is the one rendered BEFORE the fix, with the defects listed.
FIX_FAILED_WARNING = "視覺修正這一步沒有完成，交付的是修正前的版本；缺陷清單仍附上。"
# The vision model would not inspect the slides; only geometric QA ran.
VISION_SKIPPED_WARNING = "視覺檢查未執行（模型不接受圖片輸入），只做了版面幾何檢查。"


# ── Tunables → moved to app/services/studio_config.py (god-module split) ─────
# Only the constants still referenced by the orchestration/QA code that
# remains here are imported above (RENDERER_BASE_URL, SCHEMA_CORRECTION_PASSES,
# SLIDES_LLM_MODEL, VISION_LLM_MODEL, VISUAL_QA_PASSES). SLIDES_LLM_MODEL stays
# importable from this module for mindmaps.py; the retrieval/render-only
# constants were moved straight into the modules that use them.


# ── JSON extraction → moved to app/services/llm_json.py (god-module split) ──
# _extract_json_object / _loads_lenient are imported above (aliased to keep
# the call sites in this module unchanged).


# ── Step 3: retrieval → moved to app/services/studio_retrieval.py (split) ─
# retrieve_chunks / retrieve_images (and the private _build_chunk_dicts)
# live there now; imported above under their original underscore aliases so
# the _run_pipeline call sites stay unchanged.


# ── Step 4-5: LLM helpers → moved to app/services/studio_llm.py (split) ──
# count_hint / build_generation_prompt / call_llm_chat / StudioLLMAdapter /
# Gemma4VlmGate live there now; imported above under their original
# underscore aliases so the call sites in this module stay unchanged.


# ── Step 5+6: generate + validate (with one correction pass) ─────────────


async def _generate_validated_spec(
    bearer: str,
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
    images: list[dict[str, Any]] | None = None,
    *,
    retrieval_failed: bool,
    illustrations_enabled: bool = True,
) -> tuple[SlidesSpec, bool]:
    """LLM → JSON → SlidesSpec, retrying once on validation failure.

    Returns (spec, fallback_used). When fallback_used=True, the spec is
    a synthetic safety-net deck explaining the failure to the user; the
    caller should skip vision QA (which would try to "fix" a deliberately
    minimal deck and might trigger another LLM call that also fails).

    ``retrieval_failed`` is threaded straight to the prompt builder so an
    empty ``chunks`` list caused by an error is never described to the
    model as "the knowledge base had no match".
    """

    system, user_msg = _build_generation_prompt(
        collection_name, preset, extra_instructions, chunks, images=images,
        retrieval_failed=retrieval_failed,
        illustrations_enabled=illustrations_enabled,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # temperature=0.3 — bumped from 0.2 after observing decks where
    # gemma4 picked the safest layout (standard) for every slide. Low
    # temp helps with structural fidelity (JSON validity) but starves
    # the layout-selection step of variation. 0.3 keeps it close enough
    # to deterministic for schema purposes while letting the model
    # actually USE the section_break / stat_callout / two_column knobs
    # we're describing in the prompt.
    raw = await _call_llm_chat(
        bearer, SLIDES_LLM_MODEL, messages, temperature=0.3,
    )

    last_err: ValidationError | ValueError | json.JSONDecodeError | None = None
    last_raw = raw
    # Filenames used for the fallback `supporting` placeholder when the
    # LLM under-fills a stat_callout. Passing the first chunk filename
    # makes the autoplaced supporting line look at least vaguely
    # attributable instead of "來源:documents".
    chunk_filenames = [c.get("filename") for c in chunks if c.get("filename")]
    for attempt in range(SCHEMA_CORRECTION_PASSES + 1):
        try:
            extracted = _extract_json_object(raw)
            parsed = _loads_lenient(extracted)
            # Studio Fix 3 (2026-05-18): saturation auto-correct.
            # The schema now requires Stat.supporting (min 20 chars) and
            # Column.bullets (min 3). LLMs frequently miss this on first
            # try; rather than 422 we silently patch / demote and let the
            # user see the deck. See `_saturate_spec_dict` for behaviour.
            parsed = _saturate_spec_dict(parsed, chunk_filenames=chunk_filenames)
            return SlidesSpec.model_validate(parsed), False
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            last_raw = raw
            # Force-print raw response to stdout for diagnostic — uvicorn
            # captures stdout into docker logs. Truncate to keep log size
            # bounded; the goal is to see the *shape* of what the model
            # emitted, not full content.
            print(
                f"[studio] validate fail attempt={attempt + 1} err={str(e)[:200]}\n"
                f"[studio] raw[:600]={raw[:600]!r}",
                flush=True,
            )
            if attempt >= SCHEMA_CORRECTION_PASSES:
                break

            # Correction pass: feed the failed output back with the
            # validation error and ask the model to fix only what's wrong.
            err_summary = str(e)[:1500]
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你上一次回覆無法通過 schema 驗證：\n\n"
                        f"{err_summary}\n\n"
                        "請只修正以上欄位、保留其餘內容；"
                        "重新輸出整個 JSON 物件（規則同前）。"
                        "特別注意：所有屬性名稱跟字串值都必須用「雙引號」包起來，"
                        "不要用單引號 ' 也不要用全形「」。"
                    ),
                }
            )
            raw = await _call_llm_chat(
                bearer, SLIDES_LLM_MODEL, messages, temperature=0.2,
            )

    # Both attempts failed. Per the research file (compass_artifact §F /
    # Self-Correction Bench arXiv 2507.02778), a third attempt has 64.5%
    # blind-spot rate and tends to reinforce the original error rather
    # than fix it. The right move is to FALLBACK to a sane default deck
    # so the user gets a usable .pptx with a clear explanation of what
    # went wrong, rather than a 422 toast that destroys their work.
    logger.error(
        "Studio spec validation gave up after %d passes; serving fallback deck. "
        "Last raw (first 500): %s",
        SCHEMA_CORRECTION_PASSES + 1,
        last_raw[:500].replace("\n", "⏎"),
    )
    return _build_fallback_spec(collection_name, preset, str(last_err)[:300]), True


def _saturate_spec_dict(
    spec_dict: Any,
    *,
    chunk_filenames: list[str] | None = None,
) -> Any:
    """Studio Fix 3 pre-validation pass — patch under-filled layouts.

    The schema (Stat.supporting min 20, Column.bullets min 3) is the
    long-term floor we want LLM output to clear. But early in the rollout
    gemma4 frequently misses one or the other; rather than 422-ing the
    user we silently patch the offending slides and log a warning.

    Two transforms, in order:

    1. ``stat_callout`` slide whose ``stat.supporting`` is missing /
       shorter than 20 chars → auto-fill a placeholder of the form
       ``來源:<first chunk filename or 'documents'>``. This is meant to
       be visually obvious so the user knows the LLM under-filled but
       not catastrophic enough to refuse the deck.
    2. ``two_column`` slide where ANY column has < 2 bullets → upgrade
       to ``icon_rows`` layout: each column becomes one icon_row whose
       heading comes from the column heading and description is the
       joined bullets (separated by `；`). This preserves the
       side-by-side / parallel-concepts framing of the original
       two_column intent (Round 2 Patch C: previously we demoted to
       ``standard`` which lost the parallel-concept signal entirely).
       If we can't produce >= 2 icon_rows (e.g. heading missing), fall
       back to the original standard-flatten path.

    Both transforms log a warning so we can monitor LLM saturation rates.
    Pure function — operates on a parsed dict in-place AND returns it.

    Defensive: this function MUST NOT raise. Any dict shape we can't
    interpret (missing 'slides' key, non-list slides, etc.) is left
    untouched and Pydantic validation will produce its normal error.
    """
    if not isinstance(spec_dict, dict):
        return spec_dict
    slides = spec_dict.get("slides")
    if not isinstance(slides, list):
        return spec_dict

    fallback_source = (
        chunk_filenames[0] if chunk_filenames else "documents"
    )

    for slide in slides:
        if not isinstance(slide, dict):
            continue
        layout = slide.get("layout_kind", "standard")

        # Transform 1: stat_callout supporting auto-fill
        if layout == "stat_callout":
            stat = slide.get("stat")
            if isinstance(stat, dict):
                supporting = stat.get("supporting") or ""
                if len(str(supporting)) < 20:
                    title = slide.get("title", "<untitled>")
                    placeholder = (
                        f"來源:{fallback_source}（系統補填：LLM 未提供足夠脈絡，"
                        f"請參考原文件取得 baseline、樣本數與實驗條件）"
                    )
                    stat["supporting"] = placeholder
                    logger.warning(
                        "Studio saturation: slide '%s' stat.supporting "
                        "under-filled (%d chars), auto-filled placeholder "
                        "referencing %s.",
                        title, len(str(supporting)), fallback_source,
                    )

        # Transform 2: two_column with sparse columns → upgrade to icon_rows
        # (Round 2 Patch C: was demote-to-standard, which lost the
        # side-by-side framing entirely. icon_rows preserves the
        # "N parallel concepts" shape. Trigger threshold also relaxed
        # from <3 bullets to <2 bullets per column to match the schema
        # floor drop in `app.schemas.studio.Column`.)
        if layout == "two_column":
            cols = slide.get("columns")
            if isinstance(cols, list) and cols:
                sparse = any(
                    not isinstance(c, dict)
                    or not isinstance(c.get("bullets"), list)
                    or len(c["bullets"]) < 2
                    for c in cols
                )
                if sparse:
                    title = slide.get("title", "<untitled>")
                    new_rows: list[dict[str, str]] = []
                    for c in cols:
                        if not isinstance(c, dict):
                            continue
                        heading = str(c.get("heading") or "").strip()
                        bullets = c.get("bullets") or []
                        if not isinstance(bullets, list) or not heading:
                            continue
                        desc = "；".join(
                            str(b).strip()
                            for b in bullets
                            if str(b).strip()
                        )[:200]
                        if not desc:
                            continue
                        new_rows.append({
                            "concept": "comparison",
                            "heading": heading,
                            "description": desc,
                        })
                    if len(new_rows) >= 2:
                        slide["layout_kind"] = "icon_rows"
                        slide["icon_rows"] = new_rows
                        slide.pop("columns", None)
                        logger.warning(
                            "Studio saturation: two_column slide '%s' "
                            "upgraded to icon_rows (%d rows) due to "
                            "sparse columns (<2 bullets each).",
                            title, len(new_rows),
                        )
                        continue  # done with this slide
                    # Fallback: can't form a coherent icon_rows set
                    # (e.g. column heading missing). Use the original
                    # standard-flatten path.
                    flattened: list[str] = []
                    for c in cols:
                        if not isinstance(c, dict):
                            continue
                        heading = str(c.get("heading") or "").strip()
                        bullets = c.get("bullets") or []
                        if not isinstance(bullets, list):
                            continue
                        for b in bullets:
                            text = str(b).strip()
                            if not text:
                                continue
                            prefix = f"{heading}：" if heading else ""
                            flattened.append(f"{prefix}{text}")
                    if flattened:
                        # Merge with any existing bullets, dedupe-preserving order.
                        existing = slide.get("bullets") or []
                        if not isinstance(existing, list):
                            existing = []
                        merged: list[str] = []
                        seen: set[str] = set()
                        for item in list(existing) + flattened:
                            s = str(item).strip()
                            if s and s not in seen:
                                merged.append(s)
                                seen.add(s)
                        # Schema cap is 8 bullets per slide.
                        slide["bullets"] = merged[:8]
                    slide["layout_kind"] = "standard"
                    slide.pop("columns", None)
                    logger.warning(
                        "Studio saturation: slide '%s' two_column had "
                        "sparse columns and missing headings, fell back "
                        "to standard with %d flattened bullets.",
                        title, len(slide.get("bullets") or []),
                    )

    return spec_dict


def _build_fallback_spec(
    collection_name: str,
    preset: str,
    error_summary: str,
) -> SlidesSpec:
    """A minimal but **valid** SlidesSpec used when LLM output can't be
    coerced through the schema after the correction pass.

    Design choices:
      * 4 slides — enough to feel like a real deck, few enough to render
        in <2 s; user knows immediately something went wrong.
      * Layout mix mirrors the rhythm we ask the LLM for (section_break
        opener, then standards). Demonstrates the system works; the
        problem was elsewhere.
      * `palette` stays at the schema default ("navy_amber") so this
        path doesn't accidentally surface a coral_energy red-orange when
        the user wanted a sober tone.
      * Error summary goes into speaker_notes, not bullets — operators
        will read the .pptx in PowerPoint where speaker notes are
        visible; users will see only the surface message.
    """
    return SlidesSpec(
        title=f"{collection_name} · 自動產生草稿（安全範本）",
        slides=[
            Slide(
                layout_kind="section_break",
                title="自動產生草稿",
                bullets=[
                    "AI 模型這次輸出未通過結構驗證，已改用安全範本",
                ],
                speaker_notes=(
                    "此投影片由系統自動產生，不是 LLM 完整輸出的結果。"
                    "原始錯誤摘要：" + error_summary
                ),
            ),
            Slide(
                layout_kind="standard",
                title="發生了什麼",
                bullets=[
                    "Studio 已嘗試生成 + 一次自動修正",
                    "兩次嘗試後仍無法產出符合格式的內容",
                    "為避免使用者拿到空白檔案，已套用安全範本",
                ],
            ),
            Slide(
                layout_kind="standard",
                title="建議下一步",
                bullets=[
                    "重新點擊「開始鑄造」再試一次（多數情況下重試即可成功）",
                    "若連續失敗，請在補充指示中寫得更具體（主題、目標讀者、長度）",
                    "或調整風格 preset（例如改用「重點摘要」這種較短的格式）",
                ],
            ),
            Slide(
                layout_kind="standard",
                title="技術備註",
                bullets=[
                    f"知識庫：{collection_name}",
                    f"風格 preset：{preset}",
                    "詳細錯誤已寫入 speaker notes 與後端日誌",
                ],
                speaker_notes=(
                    f"原始錯誤：{error_summary}\n\n"
                    "此檔案不是「失敗」狀態 — Studio job 已正常結案、"
                    "vision QA 也會跑過，使用者可以直接下載。"
                ),
            ),
        ],
    )


# ── Step 7: render → moved to app/services/studio_render.py (god-module split)
# get_flux_provider / get_active_flux_provider / _hydrate_images / _render_pptx /
# _generate_slide_illustration / _infer_image_use_case / _apply_illustration_fallback
# live there now and are imported above (same names). _gated_generate stays
# private to studio_render. Tests monkeypatch the render chain on
# app.services.studio_render now.


# ── Step 8: vision QA → moved to app/services/studio_vision_qa.py (split) ──
# visual_qa / fix_spec_with_defects are imported above (aliased to the
# underscore names _run_pipeline uses); the per-slide helpers stay private to
# studio_vision_qa.


# ── Studio Fix 1: layout audit/rebalance + theme overrides → moved to
#    app/services/studio_layout.py (god-module split). The four entry points
#    _run_pipeline uses (_apply_theme_title_override / _audit_layout_distribution
#    / _should_rebalance / _rebalance_layouts) are imported above; the rest is
#    private to studio_layout (tests import/patch it there).


# ── Pipeline runner (used by the job manager) ────────────────────────────


async def _run_pipeline(
    *,
    identity: "CurrentUserIdentity",
    bearer: str,
    payload: GenerateSpecRequest,
    updater: jobs.JobUpdater,
) -> None:
    """Executes steps 3-9 and pushes state transitions to the updater.

    Runs INSIDE the asyncio task spawned by the job manager. The caller's
    bearer token is captured once at POST time and threaded through every
    csp_client call so authorisation / quota / billing land on the right
    user. anila-studio holds no DB session of its own — every piece of
    state lives upstream (csp owns the row data; csp's proxy owns the LLM
    token usage rows).
    """
    coll = await get_collection(payload.collection_id, bearer=bearer)

    # FLUX Stage 1 (4.2): deterministic per-deck seed derived once from
    # the job_id, so re-running the same job yields the same images.
    # Per-slide seed = deck_base_seed + slide_index (computed in
    # _hydrate_images). The llm adapter lets the rewriter (Layer A)
    # reuse the same proxy/usage path as every other Studio LLM call.
    deck_base_seed = int(
        hashlib.sha256(updater.job_id.encode()).hexdigest()[:8], 16
    )
    flux_llm = _StudioLLMAdapter(bearer, SLIDES_LLM_MODEL)

    # ── Step 3: retrieval ──
    await updater.set(step=JOB_STEP_RETRIEVING)
    seed_query = " · ".join(
        [coll.name, payload.preset]
        + (
            [payload.extra_instructions.strip()]
            if payload.extra_instructions
            else []
        )
    )
    chunks: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    # Three outcomes, not two: hits / zero hits / failed. Only the third
    # sets this flag — zero hits is a legitimate statement about the
    # corpus and keeps the existing prompt copy.
    retrieval_failed = False
    if not payload.skip_retrieval:
        try:
            chunks = await _retrieve_chunks(
                bearer, payload.collection_id, seed_query,
            )
            if not chunks:
                logger.warning(
                    "Studio retrieval returned 0 hits: collection=%s(id=%s) "
                    "seed_query=%r — deck will be generated without context.",
                    coll.name, payload.collection_id, seed_query[:80],
                )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            # Retrieval is best-effort — the deck still ships (that is
            # the product contract; skip_retrieval is a supported mode).
            # But the degradation is DECLARED, not swallowed: honest
            # prompt copy for the model, warning on JobStatus for the
            # user. The exception text stays in this operator log only.
            retrieval_failed = True
            logger.warning(
                "Studio retrieval failed (%s); generating without context "
                "and declaring it to the user.", e,
            )
        # Phase 5: image vector search runs alongside chunk search
        # so the LLM gets both kinds of context in one prompt. Empty
        # list when the collection has no images at all (text-only
        # knowledge base) — the prompt skip-emits the section.
        try:
            images = await _retrieve_images(
                bearer, payload.collection_id, seed_query,
            )
            if images:
                logger.info(
                    "Studio retrieved %d images for deck '%s'",
                    len(images), payload.preset,
                )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Studio image retrieval failed (%s); proceeding "
                "without image suggestions.",
                e,
            )

    # Build the lookup the renderer-side hydration needs. Keyed by
    # image_id so `Slide.image_ref` resolves in O(1) without re-
    # querying the DB during render. Only images actually surfaced
    # to the LLM are eligible — this is also the security boundary
    # for "user can't reference cross-collection images".
    images_lookup: dict[str, dict[str, Any]] = {
        im["image_id"]: im for im in images
    }

    # ── Steps 4-6: LLM → JSON → SlidesSpec ──
    # Only teach the model about generated illustrations when a FLUX
    # provider is actually resolvable here; otherwise hydration would drop
    # every image_prompt it writes and leave hollow one-line slides behind.
    illustrations_enabled = await get_active_flux_provider() is not None
    await updater.set(step=JOB_STEP_GENERATING)
    spec, used_fallback = await _generate_validated_spec(
        bearer,
        coll.name,
        payload.preset,
        payload.extra_instructions,
        chunks,
        images=images,
        retrieval_failed=retrieval_failed,
        illustrations_enabled=illustrations_enabled,
    )
    # ── Step 6.5: zh-CN → zh-TW post-processing ──
    # Gemma 4 leaks simplified-Chinese phrasing into 繁體 output (研究第
    # 33-39 行有量化證據). The system prompt fights this with an explicit
    # mapping table, but enforcement isn't 100% — we run OpenCC s2twp as
    # a deterministic safety net AFTER spec validate (so the LLM-emitted
    # structural integrity check has already passed) and BEFORE render
    # / vision QA (so all downstream steps see clean Traditional Chinese).
    spec = normalize_spec(spec)
    # Round 3 Patch P: apply theme_override after spec is validated.
    # Bypasses LLM theme selection per the API request. Applied here
    # (post-validation, pre-rebalance, pre-render) so all downstream
    # steps — rebalance, render, vision QA — see the forced theme.
    # Literal on the request schema already rejected invalid values
    # at request time, so we trust the value unconditionally here.
    if payload.theme_override:
        spec.theme = payload.theme_override
    else:
        # Round 5 Patch U: deterministic title-keyword override.
        # Promotes title-based theme routing from a prompt-soft rule
        # to a hard programmatic override. Runs AFTER LLM emits theme
        # and AFTER theme_override (user wins over inference). Only
        # fires when title matches a high-confidence keyword; no-op
        # otherwise (preserves LLM choice). Must run before audit /
        # rebalance / render so all downstream steps see the final
        # theme.
        spec = _apply_theme_title_override(spec)
    # Surface the title early so the UI can show "鑄造中：<title>"
    # before render finishes.
    await updater.set(title=spec.title, slide_count=len(spec.slides))

    # ── Step 6.7 / Studio Fix 1: layout audit + LLM rebalance ──
    # Run the deterministic audit on the validated spec. If a HARD
    # violation fires (standard > 60% / numeric content without
    # stat_callout), make ONE focused LLM call to re-select layout
    # on a small candidate set. Soft violations alone don't trigger.
    # Skip the whole pass on the fallback deck (its job is "explain
    # the failure", not "look good") and on skip_retrieval (no
    # chunks_text to feed V2).
    if not used_fallback:
        chunks_str = "\n\n".join(
            str(c.get("content", "")) for c in chunks
        )
        violations = _audit_layout_distribution(spec, chunks_text=chunks_str)
        if _should_rebalance(violations):
            await updater.set(step=JOB_STEP_REBALANCING)
            try:
                spec_dict = spec.model_dump(mode="json")
                rebalanced = await _rebalance_layouts(
                    spec_dict, violations, chunks_str, bearer=bearer,
                )
                spec = SlidesSpec.model_validate(rebalanced)
                # Rebalance 透過 LLM 重生 bullets,新內容會帶 LaTeX 控制字元
                # (e.g. $\nightarrow$)跟簡體字。必須再過 normalize_spec
                # 才能 render,否則先前的 strip_latex / s2twp / 引用清理
                # 全部白做(production 觀察到 $\nightarrow$ 8 處殘留即此因)。
                spec = normalize_spec(spec)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Rebalance failed: %s — proceeding with original spec",
                    exc,
                )

    # ── Stage 3: infer the deck's visual house style from its content,
    # once per deck, so every slide shares one visual language. Only when
    # the FLUX cover-hero path will actually run; failure degrades to the
    # default style inside infer_deck_style.
    deck_style = None
    # deck_base_seed check first so a fallback-deck/skip-retrieval run
    # (deck_base_seed None) never pays for the async csp image-primary
    # round-trip inside get_active_flux_provider() (`and` short-circuits).
    if deck_base_seed is not None and await get_active_flux_provider() is not None:
        from app.services.flux_style import infer_deck_style

        style_sample = spec.title or ""
        if chunks:
            style_sample += "\n" + "\n\n".join(
                str(c.get("content", "")) for c in chunks
            )
        deck_style = await infer_deck_style(
            title=spec.title or "",
            content_sample=style_sample,
            llm=flux_llm,
        )

    # ── Step 7: render ──
    await updater.set(step=JOB_STEP_RENDERING)
    render_out = await _render_pptx(
        spec, images_lookup, bearer=bearer,
        deck_base_seed=deck_base_seed, llm=flux_llm,
        deck_style=deck_style,
    )
    pptx_bytes, pptx_path = render_out

    # ── Step 8: vision QA + (optional) one fix-and-rerender ──
    # Skip vision QA entirely when serving the fallback deck. The
    # fallback is a deliberately minimal "what went wrong" template
    # — putting it through QA risks the VLM flagging it as too
    # sparse, triggering a fix-and-rerender that calls the same
    # already-broken LLM again. Better to ship the fallback as-is.
    final_defects: list[VisualDefect] = []
    qa_passes = 0
    fix_failed = False
    vision_skipped = False
    if pptx_path and not used_fallback:
        for _ in range(VISUAL_QA_PASSES + 1):
            qa_passes += 1
            await updater.set(
                step=JOB_STEP_QA,
                qa_passes=qa_passes,
            )
            # The renderer reports each slide's rendered kind and whether it
            # prepended a cover; defects come back in RENDERED indices and are
            # mapped onto spec indices before anyone (fix prompt, UI) sees them.
            render_kinds = list(getattr(render_out, "kinds", None) or [])
            cover_prepended = bool(getattr(render_out, "cover_prepended", False))
            raw_defects = await _visual_qa(
                bearer, pptx_path, pptx_bytes=pptx_bytes,
                **({"kinds": render_kinds} if render_kinds else {}),
            )
            if getattr(raw_defects, "vision_skipped", None):
                vision_skipped = True
            defects = _to_spec_indices(list(raw_defects), cover_prepended=cover_prepended)
            critical = [d for d in defects if d.severity == "critical"]
            if not critical or qa_passes > VISUAL_QA_PASSES:
                final_defects = defects
                break
            # Critical defects exist AND we still have a fix budget —
            # ask the LLM to revise, re-render, re-QA. Any failure here
            # (timeout, bad JSON, csp error) keeps the deck we already
            # rendered: the fix is an improvement pass, not a gate.
            await updater.set(step=JOB_STEP_FIXING)
            try:
                fixed = await _fix_spec_with_defects(bearer, spec, critical)
                await updater.set(
                    step=JOB_STEP_RENDERING,
                    title=fixed.title,
                    slide_count=len(fixed.slides),
                )
                render_out = await _render_pptx(
                    fixed, images_lookup, bearer=bearer,
                    deck_base_seed=deck_base_seed, llm=flux_llm,
                    deck_style=deck_style,
                )
            except Exception as e:  # noqa: BLE001 — never lose a rendered deck
                logger.warning("Studio defect-fix pass failed, shipping the pre-fix deck: %s", e)
                fix_failed = True
                final_defects = defects
                break
            spec = fixed
            pptx_bytes, pptx_path = render_out

    # ── Step 9: terminal "done" — pptx_bytes is the artifact ──
    # Fallback deck / failed retrieval are still a downloadable .pptx (so
    # the user isn't left with a toast and nothing), but we surface a soft
    # warning so the SPA doesn't present it as a clean win. Both can fire
    # in the same run — the user needs to know about both.
    degradations = [
        note
        for note, fired in (
            (RETRIEVAL_FAILED_WARNING, retrieval_failed),
            (FALLBACK_DECK_WARNING, used_fallback),
            (FIX_FAILED_WARNING, fix_failed),
            (VISION_SKIPPED_WARNING, vision_skipped),
        )
        if fired
    ]
    await updater.mark_done(
        spec=spec,
        pptx_bytes=pptx_bytes,
        defects=final_defects,
        qa_passes=qa_passes,
        warning=("\n".join(degradations) or None),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/slides/jobs",
    response_model=JobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_slides_job(
    payload: GenerateSpecRequest,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
    bearer: str = Depends(get_bearer_token),
) -> JobStatus:
    """Register a slide-deck generation job and return its initial status.

    Returns immediately (HTTP 202) with state="pending" and a job_id.
    The pipeline runs on an asyncio.Task in the background; clients poll
    GET /jobs/{id} for state transitions and GET /jobs/{id}/pptx for the
    binary once state="done".
    """
    # Authorize collection access up-front so the user gets a synchronous
    # 403/404 instead of an opaque "failed" job seconds later.
    # csp_client.get_collection raises CspForbiddenError / CspNotFoundError
    # which propagate to HTTP 403 / 404 (mapped by FastAPI exception
    # handlers on the anila-studio side).
    try:
        await get_collection(payload.collection_id, bearer=bearer)
    except CspNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CspForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except CspUnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (CspServerError, CspClientError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def _runner(updater: jobs.JobUpdater) -> None:
        await _run_pipeline(
            identity=identity, bearer=bearer, payload=payload, updater=updater,
        )

    report_ctx = job_lifecycle.make_context(
        artifact_type="slides",
        owner_user_id=identity.id,
        requester=identity.username or str(identity.id),
        bearer=bearer,
        collection_id=payload.collection_id,
        describe=jobs.artifact_info,
        task_id=payload.task_id,
        source_snapshot_id=payload.source_snapshot_id,
        trace_id=payload.trace_id,
    )
    record = await jobs.create_job(
        user_id=identity.id,
        collection_id=payload.collection_id,
        runner=_runner,
        report_ctx=report_ctx,
    )
    return record.to_status()


@router.get("/slides/jobs/{job_id}", response_model=JobStatus)
async def get_slides_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> JobStatus | dict:
    """Cheap polling endpoint. Returns the current JobStatus or 404.

    Read-through: if the in-memory record is gone (studio restarted, or
    the job was evicted from the cache) we fall back to the durable job
    store so a pre-restart job can still answer status queries.
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is not None:
        return rec.to_status()
    persisted = await job_lifecycle.read_status(job_id, identity.id)
    if persisted is not None:
        return persisted
    # 404 covers both "doesn't exist" and "exists but belongs to
    # someone else" — the latter must NEVER leak to a different user.
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found (unknown id, evicted, or not yours).",
    )


@router.get(
    "/slides/jobs/{job_id}/pptx",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": {}
            },
            "description": "Generated .pptx file",
        }
    },
)
async def get_slides_job_pptx(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> StreamingResponse:
    """Stream the rendered .pptx for a completed job.

    Returns 404 for unknown/cross-user jobs, 409 if the job is still
    running, and 410 if it has been failed/cancelled.
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        # Studio restarted (or the job aged out of memory): the durable
        # status record still knows the owner and the outcome, and the deck
        # itself was written to the artifacts volume when the job finished.
        persisted = await job_lifecycle.read_status(job_id, identity.id)
        if persisted is None or persisted.get("state") != "done":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        disk_bytes = jobs.load_persisted_pptx(job_id)
        if disk_bytes is None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="這份簡報已不在伺服器上（超過保存期限），請重新產生。",
            )
        return _pptx_response(disk_bytes, persisted.get("title"))
    if rec.state in ("pending", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job not ready yet (state={rec.state}).",
        )
    if rec.state in ("failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Job is {rec.state}: {rec.error or '(no detail)'}",
        )
    if rec.pptx_bytes is None:
        # state == "done" but no bytes — shouldn't happen, but guard anyway.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job marked done but pptx bytes are missing.",
        )

    return _pptx_response(rec.pptx_bytes, rec.title)


def _pptx_response(pptx_bytes: bytes, title: str | None) -> StreamingResponse:
    # RFC 5987 percent-encoded filename for CJK titles — keeps the .pptx
    # download header USASCII-safe while modern browsers honour the
    # filename* parameter for the real CJK title.
    from urllib.parse import quote

    raw_title = (title or "presentation").replace('"', "")[:80]
    encoded_title = quote(raw_title, safe="")
    ascii_title = (
        raw_title.encode("ascii", "ignore").decode("ascii").strip() or "presentation"
    )

    async def _stream() -> Any:
        yield pptx_bytes

    return StreamingResponse(
        _stream(),
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_title}.pptx"; '
                f"filename*=UTF-8''{encoded_title}.pptx"
            ),
            "Content-Length": str(len(pptx_bytes)),
        },
    )


@router.delete("/slides/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_slides_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> Response:
    """Cancel an in-flight job. No-op if already terminal."""
    cancelled = await jobs.cancel_job(job_id, identity.id)
    if not cancelled:
        # Either not yours / not found / already terminal — all fine; the
        # client doesn't need to distinguish for "delete my row" UX.
        rec = jobs.get_user_job(job_id, identity.id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.",
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

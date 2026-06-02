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
import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

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
    ChunkHit,
    CollectionMeta,
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    CspUnauthorizedError,
    ImageHit,
    fetch_image_blob,
    get_collection,
    proxy_chat_completions,
    search_chunks,
    search_images,
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
from app.services import studio_job_service as jobs
from app.services.geometric_qa import GeometricDefect, run_geometric_qa
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.studio_config import (
    CONTENT_ILLUSTRATION_MAX_BULLETS,
    FLUX_GATE_MAX_RETRIES,
    FLUX_GATE_NUM_CANDIDATES,
    FLUX_GATE_SEED_STRIDE,
    MAX_GENERATED_IMAGES_PER_DECK,
    RENDERER_BASE_URL,
    SCHEMA_CORRECTION_PASSES,
    SLIDES_LLM_MODEL,
    STUDIO_CONTENT_LIMIT_CHARS,
    STUDIO_IMAGE_MIN_SCORE,
    STUDIO_IMAGE_TOP_K,
    STUDIO_MIN_SCORE,
    STUDIO_TOP_K,
    VISION_LLM_MODEL,
    VISUAL_QA_PASSES,
)
from app.services.studio_llm import (
    Gemma4VlmGate as _Gemma4VlmGate,
    StudioLLMAdapter as _StudioLLMAdapter,
    build_generation_prompt as _build_generation_prompt,
    call_llm_chat as _call_llm_chat,
)
from app.services.studio_text_normalizer import normalize_spec

if TYPE_CHECKING:
    from app.services.flux_image_provider import FluxImageProvider

router = APIRouter(prefix="/api/studio", tags=["Studio / Slides"])
logger = logging.getLogger(__name__)


# ── Tunables → moved to app/services/studio_config.py (god-module split) ─────
# All STUDIO_* / FLUX_GATE_* / SLIDES_LLM_MODEL / VISION_LLM_MODEL etc.
# constants are imported above. They stay importable from this module
# (`from app.api.studio import <CONST>`) so mindmaps.py and the tests that
# reference them keep working unchanged.


# ── JSON extraction → moved to app/services/llm_json.py (god-module split) ──
# _extract_json_object / _loads_lenient are imported above (aliased to keep
# the call sites in this module unchanged).


# ── Step 3: retrieval ─────────────────────────────────────────────────────


async def _retrieve_chunks(
    bearer: str,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant chunks for the seed_query, fetched via csp HTTP.

    csp owns the embedding model + pgvector index; we just call its
    ``/api/ingestion/collections/{id}/search`` endpoint and project the
    returned hits into the dict shape the prompt builder expects.

    Returns ``[]`` when:
      * the collection is archived (csp returns its meta but Studio
        treats archived as "no retrieval");
      * csp returns an empty result set;
      * csp surfaces ``CspNotFoundError`` (already-deleted collection).

    Raises:
      * ``CspForbiddenError`` (caller already 403'd in the POST handler;
        if we re-hit it here it's a TOCTOU race — surface as 403).
      * ``CspUnauthorizedError`` (token expired mid-job — surface as 401
        so the SPA refreshes and retries).
      * ``CspServerError`` (transient csp outage; caller catches this
        and degrades to "no retrieval" mode).
    """
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        # Studio over an archived collection is an unusual ask; treat as
        # zero hits and let the prompt fall through to "no context" mode.
        return []

    hits = await search_chunks(
        collection_id,
        seed_query,
        top_k=STUDIO_TOP_K,
        min_score=STUDIO_MIN_SCORE,
        bearer=bearer,
    )
    return _build_chunk_dicts(hits)


def _build_chunk_dicts(hits: list["ChunkHit"]) -> list[dict[str, Any]]:
    """Project ``ChunkHit`` dataclasses into the dict shape callers expect.

    The original csp implementation joined filenames out of
    ``ingestion_documents`` separately; the new csp HTTP endpoint
    embeds ``filename`` on every hit, so the join here is a no-op.
    Content is truncated client-side to ``STUDIO_CONTENT_LIMIT_CHARS``
    to keep the prompt budget bounded even if csp returned larger
    chunks than the studio target.
    """
    return [
        {
            "filename": h.filename or "<unknown>",
            "chunk_key": h.chunk_key,
            "content": h.content[:STUDIO_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
    ]


async def _retrieve_images(
    bearer: str,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant ingestion_images rows for the deck topic.

    Phase 5. Mirrors ``_retrieve_chunks`` but calls csp's
    ``/api/ingestion/collections/{id}/images/search`` endpoint instead
    of the chunk-search route. Returns a list of dicts the prompt
    builder can splat into the "可用圖" section; the renderer-side
    hydration step (``_hydrate_images``) resolves ``image_id`` to PNG
    bytes via ``csp_client.fetch_image_blob``.

    Empty list when:
      * collection has no images at all (text-only knowledge base);
      * embedder returned an empty vector;
      * pgvector match scores are all below threshold;
      * collection is archived (csp returns meta but Studio treats
        archived as "no retrieval").
    """
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        return []

    hits = await search_images(
        collection_id,
        seed_query,
        top_k=STUDIO_IMAGE_TOP_K,
        min_score=STUDIO_IMAGE_MIN_SCORE,
        bearer=bearer,
    )

    return [
        {
            "image_id": h.image_id,
            "document_id": h.document_id,
            "page": h.page,
            "storage_path": h.storage_path,
            "mime": h.mime,
            "caption": (h.caption or "").strip(),
            "filename": h.filename,
            "score": float(h.score),
        }
        for h in hits
    ]


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
) -> tuple[SlidesSpec, bool]:
    """LLM → JSON → SlidesSpec, retrying once on validation failure.

    Returns (spec, fallback_used). When fallback_used=True, the spec is
    a synthetic safety-net deck explaining the failure to the user; the
    caller should skip vision QA (which would try to "fix" a deliberately
    minimal deck and might trigger another LLM call that also fails).
    """

    system, user_msg = _build_generation_prompt(
        collection_name, preset, extra_instructions, chunks, images=images,
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


# ── Step 7: render via the Node service ──────────────────────────────────


# Module-level singleton: built once on import (or on first call), held
# until process exit. Shared semaphore inside ensures concurrency
# limit holds across all slide-generation requests.
_FLUX_PROVIDER: "FluxImageProvider | None" = None
_FLUX_PROVIDER_INITIALISED = False


def get_flux_provider() -> "FluxImageProvider | None":
    """Return the configured FluxImageProvider, or None if FLUX
    integration is disabled in this deployment.

    Configuration via env:
      FLUX_BACKEND_URL       (required to enable; e.g. http://flux2-dev:8000)
      FLUX_CACHE_DIR         (default: /var/anila/anila-studio-flux-cache)
      FLUX_MAX_CONCURRENT    (default: 4)
      FLUX_TIMEOUT_SECONDS   (default: 180)
    """
    global _FLUX_PROVIDER, _FLUX_PROVIDER_INITIALISED
    if _FLUX_PROVIDER_INITIALISED:
        return _FLUX_PROVIDER

    flux_url = os.environ.get("FLUX_BACKEND_URL", "").strip()
    if not flux_url:
        _FLUX_PROVIDER_INITIALISED = True
        return None

    from app.services.flux_image_provider import FluxImageProvider

    # anila-studio 自己的 cache volume — 不借 csp 的 INGESTION_UPLOAD_DIR
    # (那是 csp 內 ingestion 上傳目錄,anila-studio container 沒掛/沒權限)。
    cache_dir = os.environ.get(
        "FLUX_CACHE_DIR", "/var/anila/anila-studio-flux-cache"
    )
    max_concurrent = int(os.environ.get("FLUX_MAX_CONCURRENT", "4"))
    timeout = float(os.environ.get("FLUX_TIMEOUT_SECONDS", "180"))

    from pathlib import Path
    _FLUX_PROVIDER = FluxImageProvider(
        flux_url=flux_url,
        cache_dir=Path(cache_dir),
        max_concurrent=max_concurrent,
        timeout_seconds=timeout,
    )
    _FLUX_PROVIDER_INITIALISED = True
    logger.info(
        "FluxImageProvider wired: url=%s cache_dir=%s concurrent=%d",
        flux_url, cache_dir, max_concurrent,
    )
    return _FLUX_PROVIDER


async def _gated_generate(
    flux_provider: "FluxImageProvider",
    prompt: str,
    *,
    use_case: "ImageUseCase",
    seed: int,
    style_id: str,
    concept_en: str,
    vlm: "_Gemma4VlmGate",
) -> tuple["GeneratedImage | None", int]:
    """Stage 2 retry loop (spec 5.2): generate N candidates, run the quality
    gate, retry with a fresh seed on total failure.

    Returns ``(accepted_image_or_None, retry_count)``. ``retry_count`` is
    the number of EXTRA attempts beyond the first (0 = accepted first try).

    A cache hit short-circuits the whole loop: a previously gate-accepted
    image for this (prompt, use_case, seed, style) tuple is returned as-is
    so a job re-run is deterministic and free.
    """
    from app.services.flux_quality_gate import gate_candidates

    cached = flux_provider.cached_image(
        prompt, use_case=use_case, seed=seed, style_id=style_id,
    )
    if cached is not None:
        return cached, 0

    for attempt in range(FLUX_GATE_MAX_RETRIES + 1):
        attempt_seed = seed + attempt * FLUX_GATE_SEED_STRIDE
        candidates = await flux_provider.generate_candidates(
            prompt,
            use_case=use_case,
            seed=attempt_seed,
            num_candidates=FLUX_GATE_NUM_CANDIDATES,
        )
        best = await gate_candidates(
            candidates,
            concept_en=concept_en,
            vlm=vlm,
        )
        if best is not None:
            # Persist under the per-slide (deterministic) seed, not the
            # per-attempt retry seed, so a re-run hits the cache.
            flux_provider.persist_chosen(
                best, prompt=prompt, use_case=use_case, seed=seed, style_id=style_id,
            )
            logger.info(
                "[gate] accepted on attempt %d/%d (use_case=%s)",
                attempt, FLUX_GATE_MAX_RETRIES, use_case.value,
            )
            return best, attempt
        logger.info(
            "[gate] attempt %d/%d rejected all %d candidates, retrying fresh seed",
            attempt, FLUX_GATE_MAX_RETRIES, FLUX_GATE_NUM_CANDIDATES,
        )
    logger.warning(
        "[gate] all %d attempts exhausted (use_case=%s) — fallback to no image",
        FLUX_GATE_MAX_RETRIES + 1, use_case.value,
    )
    return None, FLUX_GATE_MAX_RETRIES


def _infer_image_use_case(idx: int, slide: dict) -> "ImageUseCase":
    """Map a slide to its FLUX use_case. Order matters: idx 0 / layout 'cover'
    is the hero even when also tagged section_break."""
    from app.schemas.studio import ImageUseCase

    if idx == 0 or slide.get("layout_kind") == "cover":
        return ImageUseCase.COVER_HERO
    if slide.get("layout_kind") == "section_break":
        return ImageUseCase.SECTION_BAND
    return ImageUseCase.CONTENT_ILLUSTRATION


def _apply_illustration_fallback(slide: dict, use_case: "ImageUseCase") -> None:
    """No usable image for this slide: drop image fields so the renderer
    degrades (theme cover / theme section break / text-only standard layout),
    and record the fallback in image_gen_meta. Never sets image_data.

    This is the single fallback writer — the generation helper returns False
    without writing meta, and the routing calls this for both gate-failure and
    the per-deck cap.
    """
    from app.schemas.studio import ImageUseCase

    label = {
        ImageUseCase.COVER_HERO: "solid_theme_cover",
        ImageUseCase.SECTION_BAND: "theme_section_break",
        ImageUseCase.CONTENT_ILLUSTRATION: "text_only",
    }[use_case]
    meta = slide.get("image_gen_meta") or {}
    meta.setdefault("use_case", use_case.value)
    meta["fallback"] = label
    slide["image_gen_meta"] = meta
    slide.pop("image_data", None)
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)


async def _generate_slide_illustration(
    slide: dict,
    *,
    idx: int,
    use_case: "ImageUseCase",
    deck_style: "StyleDescriptor | None",
    flux_provider: "FluxImageProvider",
    deck_base_seed: int,
    llm: "_StudioLLMAdapter",
) -> bool:
    """Rewrite → gate → write image. Returns True iff an image was placed.

    On any failure (rewriter raised / USE_GRAPHVIZ / gate raised / all
    candidates rejected) returns False WITHOUT writing fallback meta — the
    caller invokes _apply_illustration_fallback. ``concept_en`` keeps the
    Stage 1 convention (the whole flux_prompt); the slide's own image_prompt
    is intentionally ignored (decision A) — title+bullets drive the rewriter.
    """
    from app.services.flux_prompt_rewriter import derive_flux_prompt
    from app.services.flux_style import get_style_descriptor

    style = deck_style or get_style_descriptor()
    title = slide.get("title", "")
    try:
        flux_prompt = await derive_flux_prompt(
            title=title,
            bullets=slide.get("bullets", []),
            use_case=use_case,
            style=style,
            llm=llm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX rewriter failed for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if not flux_prompt:  # USE_GRAPHVIZ or stripped empty
        logger.info(
            "FLUX rewriter returned no prompt for slide '%s' (%s) — fallback.",
            title or "<untitled>", use_case.value,
        )
        return False

    seed = deck_base_seed + idx
    vlm = _Gemma4VlmGate(llm._bearer)
    try:
        best, retry_count = await _gated_generate(
            flux_provider,
            flux_prompt,
            use_case=use_case,
            seed=seed,
            style_id=style.style_id,
            concept_en=flux_prompt,
            vlm=vlm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX gated generation errored for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if best is None:
        logger.warning(
            "FLUX gate rejected all candidates for slide '%s' (%s) after %d "
            "retries — fallback.", title or "<untitled>", use_case.value, retry_count,
        )
        return False

    slide["image_data"] = (
        "data:image/png;base64," + base64.b64encode(best.png_bytes).decode("ascii")
    )
    slide["image_gen_meta"] = {
        "use_case": use_case.value,
        "flux_prompt": flux_prompt,
        "seed": best.seed,
        "style_id": style.style_id,
        "clip_score": best.clip_score,
        "vlm_verdict": best.vlm_verdict,
        "retry_count": retry_count,
    }
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)
    return True


async def _hydrate_images(
    spec_dict: dict[str, Any],
    images_lookup: dict[str, dict[str, Any]],
    *,
    bearer: str,
    flux_provider: "FluxImageProvider | None" = None,
    default_aspect: str = "16:9",
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
) -> dict[str, Any]:
    """Resolve every Slide.image_ref / diagram_dot / image_prompt into inline base64 PNG.

    Order of precedence per slide (curated > deterministic > generative):
      1. image_ref present and resolvable → inline existing PNG (fetched
         from csp's ``/api/ingestion/images/{id}/blob`` endpoint, so we
         no longer need a shared ``upload_dir`` mount).
      2. image_ref present but unresolvable → drop, fall back to next path.
      3. image_kind='diagram' + diagram_dot → render via Graphviz `dot -Tpng`.
      4. diagram render fails → drop diagram_dot/image_kind, fall back to next path.
      4b. FLUX Stage 1 cover hero: when this slide is the cover (index 0 or
          layout_kind=="cover") and the rewriter + flux_provider + seed are
          available, derive a house-styled FLUX prompt from title+bullets
          and generate a 16:9 hero. This sits ABOVE the legacy image_prompt
          path so a cover gets a deterministic, rewriter-controlled image
          rather than whatever raw prompt the LLM may have stuffed in.
      5. image_prompt present and flux_provider available → generate via FLUX.
      6. image_prompt present but flux_provider None or FLUX fails → drop, standard layout.
      7. Nothing set → leave untouched.

    Failure modes mirror each other: drop the offending field, log warning,
    let the renderer's image_focus → standard fallback take over. The
    diagram path exists because FLUX.2-dev (diffusion) cannot render
    legible text — labelled diagrams (architecture, flow, ER) get crisp
    output via Graphviz instead.

    `deck_base_seed` + `llm` enable the Stage 1 cover-hero path; when either
    is None the function behaves exactly as before (the legacy image_ref /
    diagram / image_prompt paths only). ``bearer`` is required even when
    no ``image_ref`` slides exist because the FLUX rewriter / VLM gate
    eventually flow through csp's proxy as well.
    """
    import base64

    from app.schemas.studio import ImageUseCase
    from app.services.diagram_renderer import render_dot_to_png

    slides = spec_dict.get("slides") or []
    generated_count = 0
    for idx, slide in enumerate(slides):
        # Path 1: image_ref → resolve via csp_client.fetch_image_blob
        ref = slide.get("image_ref")
        if ref:
            meta = images_lookup.get(ref)
            if not meta:
                slide.pop("image_ref", None)
                # If a fallback image_prompt is present, try that next
            else:
                try:
                    blob, fetched_mime = await fetch_image_blob(
                        int(meta["image_id"]), bearer=bearer,
                    )
                    mime = meta.get("mime") or fetched_mime or "image/png"
                    slide["image_data"] = (
                        f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
                    )
                    # ref wins over both generative paths
                    slide.pop("image_prompt", None)
                    slide.pop("diagram_dot", None)
                    slide.pop("image_kind", None)
                    continue
                except (CspNotFoundError, CspForbiddenError) as e:
                    # csp lost the row (storage purged) or the bearer no
                    # longer has access — surface the same warning pattern
                    # the old OSError branch used and degrade to the next
                    # image-resolution path.
                    logger.warning(
                        "Failed to hydrate image_ref=%s via csp blob fetch: %s — "
                        "falling back to diagram_dot / image_prompt if available.",
                        ref, e,
                    )
                    slide.pop("image_ref", None)
                except (CspServerError, CspClientError) as e:
                    # Transient csp outage — log and fall through so the
                    # deck still renders with whatever fallback path the
                    # slide has next.
                    logger.warning(
                        "csp blob fetch failed for image_ref=%s: %s — "
                        "falling back to diagram_dot / image_prompt if available.",
                        ref, e,
                    )
                    slide.pop("image_ref", None)

        # Path 2: diagram_dot → render via Graphviz
        # Studio Fix 2 (2026-05-18): deterministic labelled diagrams.
        # Wins over image_prompt because FLUX can't render text legibly.
        dot = slide.get("diagram_dot")
        if dot and slide.get("image_kind") == "diagram":
            png_bytes = await render_dot_to_png(dot)
            if png_bytes is not None:
                slide["image_data"] = (
                    "data:image/png;base64,"
                    + base64.b64encode(png_bytes).decode("ascii")
                )
                # Diagram succeeded; drop any leftover prompt fields.
                slide.pop("image_prompt", None)
                slide.pop("diagram_dot", None)
                slide.pop("image_kind", None)
                continue
            # Render failed — drop the diagram fields so the renderer's
            # image_focus → standard fallback kicks in. (image_prompt is
            # intentionally NOT tried here: the LLM decided this was a
            # diagram, not an illustration; falling through to FLUX
            # would put garbled-text output back on the slide.)
            logger.warning(
                "Studio diagram path: graphviz render returned None for slide '%s' "
                "(dot binary missing? CJK font missing? syntax error?). Slide will "
                "fall back to standard layout. Run scripts/diagnose-graphviz.sh "
                "(Round 2 Patch G runbook) to identify root cause.",
                slide.get("title", "<untitled>"),
            )
            slide.pop("diagram_dot", None)
            slide.pop("image_kind", None)
            continue

        # Path 4: FLUX illustration — cover hero / section band / content,
        # all through the same rewriter + deck_style + quality gate. Replaces
        # the Stage 1 cover-only block and the legacy image_prompt Path 3.
        wants_illustration = (
            idx == 0
            or slide.get("layout_kind") in ("cover", "section_break")
            or slide.get("image_kind") == "illustration"
            or bool(slide.get("image_prompt"))
        )
        toolchain_ready = (
            flux_provider is not None
            and deck_base_seed is not None
            and llm is not None
        )
        if wants_illustration and toolchain_ready:
            # Order matters: compute use_case BEFORE the cap check (the cap
            # fallback needs it).
            use_case = _infer_image_use_case(idx, slide)
            # Density gate: the renderer only shows a content illustration on an
            # image_focus (image-led) layout. A bullet-heavy content slide would
            # be crowded by that, so it keeps its text-only standard layout and
            # skips generation entirely (no GPU spent, doesn't count toward cap).
            if (
                use_case is ImageUseCase.CONTENT_ILLUSTRATION
                and len(slide.get("bullets") or []) > CONTENT_ILLUSTRATION_MAX_BULLETS
            ):
                _apply_illustration_fallback(slide, use_case)
                continue
            if generated_count >= MAX_GENERATED_IMAGES_PER_DECK:
                logger.warning(
                    "per-deck image cap %d reached; slide %d (%s) -> fallback",
                    MAX_GENERATED_IMAGES_PER_DECK, idx, use_case.value,
                )
                _apply_illustration_fallback(slide, use_case)
                continue
            generated_count += 1
            ok = await _generate_slide_illustration(
                slide,
                idx=idx,
                use_case=use_case,
                deck_style=deck_style,
                flux_provider=flux_provider,
                deck_base_seed=deck_base_seed,
                llm=llm,
            )
            if ok:
                # Content illustration only renders on an image_focus layout;
                # cover/section bands render full-bleed via renderSectionBreak
                # and must keep their layout.
                if use_case is ImageUseCase.CONTENT_ILLUSTRATION:
                    slide["layout_kind"] = "image_focus"
            else:
                _apply_illustration_fallback(slide, use_case)
            continue
        if wants_illustration and slide.get("image_prompt"):
            # FLUX toolchain not wired for this deployment but a legacy prompt
            # is present: drop it so the renderer doesn't act on an unused field.
            slide.pop("image_prompt", None)
            slide.pop("image_kind", None)

    return spec_dict


async def _render_pptx(
    spec: SlidesSpec,
    images_lookup: dict[str, dict[str, Any]] | None = None,
    *,
    bearer: str,
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
) -> tuple[bytes, str]:
    """POST spec → renderer → (pptx bytes, server-side path).

    Returns the path so /screenshots can refer to it without us having to
    base64 the .pptx through CSP memory.

    `images_lookup` (optional) is the dict that drove the LLM's
    image-suggestion list, keyed by image_id. When provided, every
    Slide.image_ref gets hydrated into inline `image_data` bytes via
    `_hydrate_images` before the spec leaves the anila-studio boundary.
    Hydration now pulls bytes from csp via ``fetch_image_blob`` rather
    than a shared filesystem mount, so ``bearer`` is required.

    `deck_base_seed` + `llm` (FLUX Stage 1) enable the cover-hero generation
    path inside `_hydrate_images`. They are threaded from the job pipeline
    (deck_base_seed = sha256(job_id)).
    """
    spec_dict = spec.model_dump()
    flux_provider = get_flux_provider()
    # Hydrate when there are curated images to resolve OR when the FLUX
    # cover-hero path is fully wired (provider + seed + llm). The latter
    # matters for text-only knowledge bases: no retrieved images means an
    # empty images_lookup, but a cover hero should still be generated.
    cover_hero_ready = (
        flux_provider is not None and deck_base_seed is not None and llm is not None
    )
    if images_lookup or cover_hero_ready:
        spec_dict = await _hydrate_images(
            spec_dict,
            images_lookup or {},
            bearer=bearer,
            flux_provider=flux_provider,
            default_aspect="16:9",
            deck_base_seed=deck_base_seed,
            llm=llm,
            deck_style=deck_style,
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                f"{RENDERER_BASE_URL}/render",
                json={"spec": spec_dict},
            )
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503,
                detail=f"pptx-renderer unreachable: {e}",
            ) from e
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"pptx-renderer /render returned {r.status_code}: {r.text[:300]}",
        )
    pptx_path = r.headers.get("X-Pptx-Path", "")
    return r.content, pptx_path


# ── Step 8: vision QA loop ───────────────────────────────────────────────


async def _capture_screenshots(pptx_path: str) -> list[bytes]:
    """Returns a list of PNG byte arrays, one per slide."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            r = await client.post(
                f"{RENDERER_BASE_URL}/screenshots",
                json={"pptxPath": pptx_path},
            )
        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=503,
                detail=f"pptx-renderer /screenshots unreachable: {e}",
            ) from e
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"pptx-renderer /screenshots returned {r.status_code}: {r.text[:300]}",
        )
    images = r.json().get("images", [])
    return [base64.b64decode(img["base64"]) for img in images]


async def _inspect_slide_visually(
    bearer: str,
    slide_index: int,
    png_bytes: bytes,
) -> list[VisualDefect]:
    """Ask gemma4 vision to flag defects on one rendered slide.

    Returns 0..N defects. Defects are grouped by severity so the caller
    can decide whether they're worth a re-render: only ``critical`` ones
    trigger the JSON correction loop.
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"

    system_prompt = (
        "你是簡報視覺品質檢查員。輸入是一張投影片的截圖，請只回 JSON："
        '{"defects": [{"severity": "critical|warning|info", "summary": "..."}]}。'
        "若沒有任何問題，回 {\"defects\": []}。"
        "critical 等級保留給「使用者一眼會發現的嚴重問題」："
        "文字溢出版面、文字與圖形重疊、低對比導致看不見、缺少必要內容。"
        "warning 用於可改善但不影響理解的問題。"
        "回應必須是、且只能是一個 JSON 物件，第一個字元 {、最後一個字元 }，"
        "不要 ```json 包裹，不要 thought/reasoning 前言。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"檢查第 {slide_index + 1} 張投影片："},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    raw = await _call_llm_chat(
        bearer, VISION_LLM_MODEL, messages, temperature=0.1,
    )

    try:
        parsed = json.loads(_extract_json_object(raw))
        defects_raw = parsed.get("defects") or []
    except (ValueError, json.JSONDecodeError):
        # Vision QA noise is not fatal — log and treat as "no defects
        # found" so a flaky model doesn't block the whole pipeline.
        logger.warning(
            "Vision QA returned unparseable response for slide %d; "
            "treating as no defects.",
            slide_index,
        )
        return []

    defects: list[VisualDefect] = []
    for d in defects_raw:
        if not isinstance(d, dict):
            continue
        sev = str(d.get("severity", "info")).lower()
        if sev not in ("critical", "warning", "info"):
            sev = "info"
        summary = str(d.get("summary", "")).strip()
        if not summary:
            continue
        defects.append(
            VisualDefect(slide_index=slide_index, severity=sev, summary=summary)
        )
    return defects


def _geometric_to_visual(g: GeometricDefect) -> VisualDefect:
    """Map a GeometricDefect into the VisualDefect shape the rest of the
    pipeline already consumes. Vision-QA and geometric-QA defects are
    indistinguishable downstream — the fix-and-rerender prompt is just
    given a list of summaries to act on.
    """
    severity = g.severity if g.severity in ("critical", "warning", "info") else "warning"
    summary = f"[geometric/{g.kind}] {g.detail}".strip()
    return VisualDefect(
        slide_index=g.slide_index,
        severity=severity,
        summary=summary[:500],
    )


def _merge_defects(
    geometric: list[VisualDefect],
    vision: list[VisualDefect],
) -> list[VisualDefect]:
    """Dedupe: when geometric and vision both flag the same slide with a
    similar kind, keep the higher severity. Otherwise concatenate.

    The dedupe key is (slide_index, "geometric"|"vision"-prefix) so we
    don't accidentally collapse two genuinely different findings on the
    same slide — only collapse near-duplicates from each source.
    """
    severity_rank = {"critical": 3, "warning": 2, "info": 1}
    out: list[VisualDefect] = []
    # Geometric is authoritative for layout — keep all of those.
    out.extend(geometric)
    # For vision defects, drop the ones that look redundant against a
    # geometric defect of equal-or-higher severity on the same slide.
    geometric_by_slide: dict[int, int] = {}
    for d in geometric:
        rank = severity_rank.get(d.severity, 0)
        geometric_by_slide[d.slide_index] = max(
            geometric_by_slide.get(d.slide_index, 0), rank,
        )
    for v in vision:
        g_rank = geometric_by_slide.get(v.slide_index, 0)
        v_rank = severity_rank.get(v.severity, 0)
        if g_rank >= 3 and v_rank <= g_rank:
            # Geometric already raised critical for this slide; vision
            # commentary is unlikely to add actionable info on top.
            continue
        out.append(v)
    return out


async def _visual_qa(
    bearer: str,
    pptx_path: str,
    *,
    pptx_bytes: bytes | None = None,
) -> list[VisualDefect]:
    """Run geometric + vision QA on every slide of a rendered .pptx.

    Geometric QA runs first. If it flags `critical` defects on a slide,
    we still run vision QA on the *other* slides (cheaper to short-circuit
    only the slides we already know are broken). Best-effort: any error
    in geometric QA yields empty defects and we fall back to vision-only.
    """
    # Geometric QA — deterministic, no vision tokens. Reads pptx bytes
    # straight from the renderer; if we weren't handed them, skip it.
    geom_defects: list[VisualDefect] = []
    critical_slides: set[int] = set()
    if pptx_bytes:
        try:
            raw = await run_geometric_qa(
                pptx_bytes, renderer_url=RENDERER_BASE_URL,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Geometric QA raised unexpectedly: %s", e)
            raw = []
        geom_defects = [_geometric_to_visual(g) for g in raw]
        critical_slides = {
            d.slide_index for d in geom_defects if d.severity == "critical"
        }

    pngs = await _capture_screenshots(pptx_path)
    if not pngs:
        return geom_defects

    # Sequential per-slide: gemma4 backend is single-tenant; concurrent
    # requests can starve each other on the GPU. Cap parallelism at 2 to
    # halve wall-clock without overwhelming the model. Skip slides that
    # geometric QA already marked critical — the fix-pass will handle
    # them and burning vision tokens on a known-broken slide is wasteful.
    semaphore = asyncio.Semaphore(2)

    async def _one(idx: int, b: bytes) -> list[VisualDefect]:
        if idx in critical_slides:
            return []
        async with semaphore:
            return await _inspect_slide_visually(bearer, idx, b)

    results = await asyncio.gather(
        *(_one(i, b) for i, b in enumerate(pngs)),
        return_exceptions=False,
    )
    vision_flat: list[VisualDefect] = []
    for r in results:
        vision_flat.extend(r)
    return _merge_defects(geom_defects, vision_flat)


async def _fix_spec_with_defects(
    bearer: str,
    current_spec: SlidesSpec,
    defects: list[VisualDefect],
) -> SlidesSpec:
    """Ask the LLM to revise the spec given a list of visual defects."""

    defect_summary = "\n".join(
        f"- 投影片 #{d.slide_index + 1}（{d.severity}）：{d.summary}"
        for d in defects
    )
    system = (
        "你是 ANILA LM 的簡報修訂助手。輸入是一份既有的 SlidesSpec JSON 和"
        "視覺檢查發現的缺陷清單。請輸出修正後的完整 SlidesSpec JSON。"
        "規則同生成階段：第一字 {、最後字 }、不可前言、不可代碼塊。"
        "修正策略："
        "1) 文字溢出 → 拆兩張或縮短 bullet。"
        "2) bullet 過多 → 砍到 ≤6。"
        "3) 重複 title → 重命名。"
        "4) placeholder 文字 → 用實際內容取代或刪除。"
        "保留沒問題的投影片不要動。"
    )
    user_msg = (
        "現有 SlidesSpec：\n"
        f"{current_spec.model_dump_json(indent=2)}\n\n"
        f"缺陷清單：\n{defect_summary}"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    raw = await _call_llm_chat(
        bearer, SLIDES_LLM_MODEL, messages, temperature=0.2,
    )
    extracted = _extract_json_object(raw)
    return SlidesSpec.model_validate(_loads_lenient(extracted))


# ── Studio Fix 1: layout post-validation + LLM rebalance pass ────────────
#
# The system prompt tells gemma4 "standard layout ≤ 60% of slides", but
# attention is split between content writing and layout selection, so the
# rule isn't actually enforced. Decks come back with 8-of-9 standard
# slides, no stat_callout for clearly numeric content, and 3+ consecutive
# bullet pages — visually flat. This module audits the produced spec
# deterministically and (when hard rules fire) issues ONE focused LLM
# call that only re-selects layout_kind on a small candidate set.
#
# Why not just bump temperature or rewrite the system prompt?
# - Bumping temperature degrades JSON validity (we did this once already).
# - Rewriting the prompt for the 9th time chases an asymptote — gemma4
#   doesn't follow proportional rules under cognitive load.
# Deterministic audit + surgical LLM fix is cheaper and more reliable.


# Hard rule V1: standard layout proportion must not exceed this cap.
LAYOUT_STANDARD_MAX_RATIO = 0.60

# Soft rule V3: runs of this many or more consecutive standard slides
# count as a "flat stretch" that hurts visual rhythm.
LAYOUT_CONSECUTIVE_STANDARD_LIMIT = 3

# V4: enumeration title keywords. When a slide title contains any of these
# AND it has 3+ bullets AND layout_kind=standard, it's a textbook candidate
# for icon_rows — the LLM "described 3 things" but didn't reach for the
# matching layout.
_ENUMERATION_KEYWORDS = (
    # Numeric enumeration
    "三大", "四大", "五大", "兩大", "三項", "三類",
    # Process / sequence
    "步驟", "階段", "流程", "歷程", "順序",
    "workflow", "pipeline", "process",
    # Structural enumeration
    "面向", "層面", "維度", "方面",
    # Architecture / topology (slide 13 case)
    "架構", "拓撲", "拓樸", "結構", "設計", "佈局",
    # Capability / function lists
    "核心能力", "能力", "功能", "特性", "特徵",
    # Strategy / approach
    "策略", "方案", "模式", "機制", "方法",
    # Comparison framing (space-padded vs to avoid 'previous' false matches)
    "對比", "對照", " vs ", " vs.",
)

# ── Round 5 Patch U: deterministic title-keyword theme overrides ──
#
# LLM tone detection (Patch O) is non-deterministic at signal boundaries.
# v4 picked warm_journal for "11月學習心得報告"; v5 picked corporate_navy
# on essentially the same content because chunks lean technical and the
# only warm_journal signal was the "心得" in the title.
#
# Architectural decision: title is the strongest author-intent signal —
# the framing the author explicitly chose. When title contains an
# unambiguous theme keyword, override the LLM's tone-based choice.
#
# Patterns are deliberately CONSERVATIVE (only high-confidence keywords).
# Title with no match leaves the LLM's choice intact. This is opt-in
# overriding, not blanket replacement.
_THEME_TITLE_OVERRIDES: list[tuple[re.Pattern[str], str]] = [
    # ── warm_journal: personal reflection framings ──
    (re.compile(r"心得|反思|回顧|感想|札記|手記"), "warm_journal"),

    # ── academic_paper: scholarly/conference framings ──
    (re.compile(
        r"論文|研究發表|期刊論文|workshop|conference paper|"
        r"研討會|學會發表",
        re.IGNORECASE,
    ), "academic_paper"),

    # ── startup_pitch: external pitch framings ──
    (re.compile(
        r"募資|產品發表|launch event|pitch deck|"
        r"投資人簡報|demo day",
        re.IGNORECASE,
    ), "startup_pitch"),

    # ── executive_brief: high-level briefing framings ──
    (re.compile(
        r"executive briefing|高層 review|主管 briefing|"
        r"季度 review|半年檢討|年度檢討",
        re.IGNORECASE,
    ), "executive_brief"),
]


def _apply_theme_title_override(spec: SlidesSpec) -> SlidesSpec:
    """Deterministic title-keyword override for theme selection.

    Runs AFTER schema validation succeeds so ``spec.theme`` is always a
    valid theme (either LLM-chosen or palette-derived). If ``spec.title``
    matches a high-confidence keyword pattern, force the corresponding
    theme.

    Idempotent and pure: same input → same output, no I/O beyond logging.
    No-op when title has no match (preserves LLM choice).
    """
    title = (spec.title or "").strip()
    if not title:
        return spec

    for pattern, target_theme in _THEME_TITLE_OVERRIDES:
        if pattern.search(title):
            if spec.theme != target_theme:
                logger.info(
                    "Theme title-override: '%s' matched %r → "
                    "switching theme %s → %s",
                    title, pattern.pattern, spec.theme, target_theme,
                )
                spec.theme = target_theme
            else:
                logger.debug(
                    "Theme title-override: '%s' matched %r, "
                    "theme already %s (no-op)",
                    title, pattern.pattern, target_theme,
                )
            return spec

    return spec


# Round 3 PRIMARY V4 signal: "label: description" bullet pattern.
#
# Matches CJK 2-6 char label + (half- or full-width) colon + non-empty tail.
# Tuned conservatively: requires the label to be entirely CJK so bullets like
# "Token 消耗降低" (mixed Latin) don't false-trigger.
_LABEL_BULLET_RE = re.compile(
    r"^\s*[一-鿿]{2,6}\s*[:：]\s*\S.*$",
)

# Fraction of bullets that must match _LABEL_BULLET_RE for the
# content-pattern V4 path to fire. 0.7 = 3 of 4, or 2 of 3.
_LABEL_PATTERN_THRESHOLD = 0.7

# V2: numeric-content regex. Matches percentages, big numbers, F1 scores,
# and sample sizes (N=xxx). When chunks_text matches AND spec has zero
# stat_callout slides, we missed a visual opportunity for a key statistic.
_NUMERIC_CONTENT_RE = re.compile(
    r"\d+(\.\d+)?\s*[%％]|\d{4,}|F1[-\s]?score|N\s*=\s*\d+",
    re.IGNORECASE,
)

# Cap on LLM-proposed changes. The whole point of this pass is "surgical
# layout-only edit" — letting the LLM rewrite half the deck defeats the
# purpose and risks breaking content that the original generate-step got
# right. 3 changes is enough to fix V1+V2 on a typical 9-slide deck.
LAYOUT_REBALANCE_MAX_CHANGES = 3


@dataclass(frozen=True)
class LayoutViolation:
    """One audit finding from `_audit_layout_distribution`.

    Hard violations (V1, V2) trigger the rebalance LLM call. Soft
    violations (V3, V4) are reported on candidates so the LLM has guidance
    on WHICH slides to re-layout; firing alone they don't trigger a call.
    """

    kind: str  # "V1" | "V2" | "V3" | "V4_CONTENT" | "V4_TITLE"
    severity: Literal["hard", "soft", "hint"]
    slide_indices: list[int] = field(default_factory=list)
    detail: str = ""


def _audit_layout_distribution(
    spec: SlidesSpec,
    chunks_text: str,
) -> list[LayoutViolation]:
    """Deterministic audit of layout distribution on a validated SlidesSpec.

    Pure function (no I/O, no LLM call). Walks the slides once and emits
    violations per rule:

    - V1 (hard): standard layout > 60% of slides.
    - V2 (hard): chunks contain numeric content (percentages, F1-score,
      N=xxx, big numbers) AND no slide uses `stat_callout`.
    - V3 (soft): 3 or more consecutive `standard` slides. Each run becomes
      its own violation, with the first slide of the run as the candidate
      for re-layout.
    - V4 (soft): slide title matches an enumeration keyword AND has 3+
      bullets AND layout_kind is "standard". Each matching slide is its
      own violation.

    The caller decides whether to invoke `_rebalance_layouts` — typically
    only when at least one HARD violation is present. Soft violations are
    used to seed the candidate list for the LLM call.
    """
    violations: list[LayoutViolation] = []
    slides = spec.slides
    n = len(slides)
    if n == 0:
        return violations

    # ── V1: standard layout proportion ──
    standard_count = sum(1 for s in slides if s.layout_kind == "standard")
    standard_ratio = standard_count / n
    if standard_ratio > LAYOUT_STANDARD_MAX_RATIO:
        violations.append(
            LayoutViolation(
                kind="V1",
                severity="hard",
                slide_indices=[
                    i for i, s in enumerate(slides)
                    if s.layout_kind == "standard"
                ],
                detail=(
                    f"standard 比例 {standard_ratio:.0%} 超過上限 "
                    f"{LAYOUT_STANDARD_MAX_RATIO:.0%}（{standard_count}/{n}）"
                ),
            )
        )

    # ── V2: numeric content without stat_callout ──
    has_stat = any(s.layout_kind == "stat_callout" for s in slides)
    if not has_stat and chunks_text and _NUMERIC_CONTENT_RE.search(chunks_text):
        violations.append(
            LayoutViolation(
                kind="V2",
                severity="hard",
                slide_indices=[],  # no specific candidate; LLM picks
                detail=(
                    "Chunks 含關鍵數據（百分比/F1/N=…）但 spec 沒有任何 "
                    "stat_callout 投影片"
                ),
            )
        )

    # ── V3: consecutive standard runs ──
    run_start: int | None = None
    run_len = 0
    for i, s in enumerate(slides):
        if s.layout_kind == "standard":
            if run_start is None:
                run_start = i
                run_len = 1
            else:
                run_len += 1
        else:
            if run_start is not None and run_len >= LAYOUT_CONSECUTIVE_STANDARD_LIMIT:
                violations.append(
                    LayoutViolation(
                        kind="V3",
                        severity="soft",
                        slide_indices=list(range(run_start, run_start + run_len)),
                        detail=(
                            f"連續 {run_len} 張 standard 投影片 "
                            f"(slides {run_start}-{run_start + run_len - 1})"
                        ),
                    )
                )
            run_start = None
            run_len = 0
    # Trailing run at end of deck.
    if run_start is not None and run_len >= LAYOUT_CONSECUTIVE_STANDARD_LIMIT:
        violations.append(
            LayoutViolation(
                kind="V3",
                severity="soft",
                slide_indices=list(range(run_start, run_start + run_len)),
                detail=(
                    f"連續 {run_len} 張 standard 投影片 "
                    f"(slides {run_start}-{run_start + run_len - 1})"
                ),
            )
        )

    # ── V4: enumeration title OR label-pattern bullets + 3+ bullets ──
    #
    # Round 2 used title-keyword only. Round 3 adds a primary CONTENT signal:
    # if ≥70% of bullets follow the "<CJK label>: <description>" shape, the
    # slide is an icon_rows candidate regardless of title wording. Empirically
    # this rescues slides like 「執行摘要」 whose title carries no keyword but
    # whose bullets are textbook icon_rows material.
    #
    # Round 4 Patch R: the LLM learned to dodge V4 by emitting
    # layout_kind="image_focus" with image_kind="illustration" and no real
    # image — visually identical to the standard-with-bullets case the
    # audit was meant to catch. Treat that disguise as an audit candidate
    # too. Real diagrams (diagram_dot present) and real images (image_ref
    # present) remain exempt because they actually carry a visual asset.
    for i, s in enumerate(slides):
        is_disguise = False
        if s.layout_kind == "standard":
            pass  # original V4 path
        elif (
            s.layout_kind == "image_focus"
            and getattr(s, "image_kind", None) == "illustration"
            and not getattr(s, "image_ref", None)
            and not getattr(s, "diagram_dot", None)
        ):
            # Fake image_focus: claims to be image-led but has no real
            # image bound. Audit it with the same content rules as
            # standard so it gets rebalanced into icon_rows / etc.
            is_disguise = True
        else:
            continue
        if len(s.bullets) < 3:
            continue
        title_low = s.title.lower()
        matched_kw = next(
            (kw for kw in _ENUMERATION_KEYWORDS if kw.lower() in title_low),
            None,
        )
        title_match = matched_kw is not None

        # Round 3 primary signal: bullet content pattern.
        pattern_matches = sum(
            1 for b in s.bullets if _LABEL_BULLET_RE.match(str(b))
        )
        pattern_match = (
            pattern_matches / len(s.bullets) >= _LABEL_PATTERN_THRESHOLD
        )

        # Disguise slides bypass the title/pattern gate — the LLM has
        # already declared intent to dodge the audit, so the layout
        # itself is the violation regardless of bullet shape.
        if not (title_match or pattern_match or is_disguise):
            continue

        # Round 6 Patch V: split V4 by signal strength so the audit's
        # judgement aligns with what the rebalance LLM can actually act on.
        #   - STRONG (V4_CONTENT, soft): bullets ARE label:description, or the
        #     slide is an image_focus disguise. Mechanically convertible to
        #     icon_rows → actionable → triggers rebalance.
        #   - WEAK (V4_TITLE, hint): title merely contains an enumeration
        #     keyword but the bullets are flowing narrative. Forcing icon_rows
        #     would mean fabricating headings, so the LLM correctly refuses.
        #     Logged for observability but never actioned.
        # When both signals fire, content-pattern dominates (strong wins).
        layout_marker = (
            "image_focus_disguise" if is_disguise else "standard layout"
        )
        base_detail = (
            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, {layout_marker}"
        )

        if pattern_match or is_disguise:
            detail = base_detail
            if pattern_match:
                detail += f", bullet-pattern {pattern_matches}/{len(s.bullets)}"
            if title_match:
                detail += f", title-keyword '{matched_kw}'"
            violations.append(
                LayoutViolation(
                    kind="V4_CONTENT",
                    severity="soft",
                    slide_indices=[i],
                    detail=detail,
                )
            )
        else:
            # title_match only → weak hint.
            violations.append(
                LayoutViolation(
                    kind="V4_TITLE",
                    severity="hint",
                    slide_indices=[i],
                    detail=f"{base_detail}, title-keyword '{matched_kw}' (hint only)",
                )
            )

    if violations:
        logger.info(
            "[H-DIAG] audit found %d violations: %s",
            len(violations),
            [
                f"{v.kind}({v.severity})@{v.slide_indices}"
                for v in violations
            ],
        )
        for v in violations:
            logger.info("[H-DIAG]   %s: %s", v.kind, v.detail)
    else:
        logger.info("[H-DIAG] audit found no violations")
    return violations


def _should_rebalance(violations: list[LayoutViolation]) -> bool:
    """Decide whether to invoke the LLM rebalance pass.

    Round 1 only fired on hard violations (V1, V2). Round 2 broadened this
    on a count of soft V4. Round 6 Patch V re-aligns the trigger with the
    split V4 signal:

    Triggers (any one suffices):
      - Any hard violation (V1 or V2) — original behaviour
      - 1 or more V4_CONTENT violations — a single bullet-pattern (or
        disguise) slide is an obvious icon_rows candidate worth fixing.

    V4_TITLE violations are HINTS only: the title carries an enumeration
    keyword but the bullets are flowing narrative, so the LLM correctly
    refuses to restructure. They never trigger a rebalance call (the old
    `v4_count >= 2` / `soft_count >= 3` thresholds caused false-positive
    "rebalance failed" trails when the LLM rightly skipped such slides).
    """
    has_hard = any(v.severity == "hard" for v in violations)
    content_v4 = sum(1 for v in violations if v.kind == "V4_CONTENT")
    decision = has_hard or content_v4 >= 1
    logger.info(
        "[H-DIAG] should_rebalance: hard=%s v4_content=%d → %s",
        has_hard, content_v4, decision,
    )
    return decision


def _select_rebalance_candidates(
    violations: list[LayoutViolation],
) -> list[int]:
    """Pick a focused candidate set of slide indices for the LLM to re-layout.

    Strategy:
    - All V4_CONTENT slides (label-pattern bullets / disguise — actionable).
      V4_TITLE hints are excluded: the LLM would only decline them.
    - First slide of each V3 run (1 representative per consecutive-standard
      stretch — rebalancing that one slide breaks up the run).

    Order: V4_CONTENT first (most specific), then V3 starters, deduped.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for v in violations:
        if v.kind == "V4_CONTENT":
            for idx in v.slide_indices:
                if idx not in seen:
                    seen.add(idx)
                    ordered.append(idx)
    for v in violations:
        if v.kind == "V3" and v.slide_indices:
            first = v.slide_indices[0]
            if first not in seen:
                seen.add(first)
                ordered.append(first)
    return ordered


def _build_rebalance_prompt(
    spec_dict: dict[str, Any],
    violations: list[LayoutViolation],
    chunks_text: str,
    candidates: list[int],
) -> tuple[str, str]:
    """Render the focused (system, user) prompt for the rebalance LLM call.

    Compact view per slide — only the metadata the LLM needs to choose a
    new layout_kind. Title and bullets are read-only context; the prompt
    instructs the LLM to ONLY change layout_kind and the matching payload.
    """
    slides = spec_dict.get("slides", [])
    compact_slides = [
        {
            "slide_index": i,
            "title": s.get("title", ""),
            "layout_kind": s.get("layout_kind", "standard"),
            "bullet_count": len(s.get("bullets", []) or []),
        }
        for i, s in enumerate(slides)
    ]
    violation_lines = [
        f"- {v.kind}（{v.severity}）：{v.detail}"
        for v in violations
    ]
    system = (
        "你是 ANILA LM 的版型重新平衡助手。輸入是一份已通過 schema 驗證的 "
        "SlidesSpec，以及一份違反「版型分佈規則」的清單。\n\n"
        "**唯一任務：** 只改變指定投影片的 layout_kind 與對應 payload "
        "（icon_rows / stat / two_column 等），**絕對不要動 title、"
        "bullets、speaker_notes**。\n\n"
        "輸出 JSON 物件，只包含一個 `changes` 陣列；每個元素形如：\n"
        '  {"slide_index": int, "new_layout_kind": str, '
        '"new_payload": {...}}\n\n'
        "規則：\n"
        f"1. 最多輸出 {LAYOUT_REBALANCE_MAX_CHANGES} 個 change，挑最關鍵的。\n"
        "2. new_layout_kind 只能是：standard / section_break / "
        "stat_callout / quote / two_column / icon_rows。\n"
        "3. 改成 icon_rows 時，new_payload 必須含 `icon_rows` 欄位，"
        "至少 3 列、每列 {concept, heading, description}。\n"
        "4. 改成 stat_callout 時，new_payload 必須含 `stat` 欄位，"
        "{value, label, supporting(≥20字)}。\n"
        "5. 改成 two_column 時，new_payload 必須含 `columns` 欄位，"
        "2 個 column、每個至少 3 個 bullet。\n"
        "6. 若違規 detail 含 `image_focus_disguise`（layout_kind=image_focus 但"
        "沒有 image_ref/diagram_dot 的偽裝）：必改成 icon_rows，把 bullets 轉成"
        " 3-4 列 {concept, heading, description}（concept 用英文），同時"
        "清掉 image_kind/image_ref/image_prompt/diagram_dot，保留 speaker_notes。\n"
        "7. 不要改的投影片直接不要出現在 changes 陣列。\n\n"
        "輸出第一字 {、最後字 }、不可前言、不可代碼塊。"
    )
    # Trim chunks_text — we only need the LLM to see roughly what data is
    # available, not the full retrieval payload.
    chunks_preview = (chunks_text or "")[:1500]
    user_msg = (
        f"違規清單：\n" + "\n".join(violation_lines) + "\n\n"
        f"建議優先重新選版的候選 slide_index：{candidates}\n\n"
        f"目前各投影片版型概況：\n"
        f"{json.dumps(compact_slides, ensure_ascii=False, indent=2)}\n\n"
        f"原始素材摘要（前 1500 字）：\n{chunks_preview}"
    )
    return system, user_msg


async def _call_llm_for_rebalance(
    prompt: tuple[str, str],
    *,
    bearer: str,
) -> dict[str, Any]:
    """Thin wrapper around ``_call_llm_chat`` for the rebalance pass.

    Extracted as its own helper so tests can mock the LLM round-trip
    without standing up the full csp proxy / model registry. Returns
    the parsed JSON dict (caller validates the `changes` shape).
    """
    system, user_msg = prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    raw = await _call_llm_chat(
        bearer, SLIDES_LLM_MODEL, messages, temperature=0.2,
    )
    logger.info(
        "[H-DIAG] rebalance LLM raw response (first 2KB): %s",
        str(raw)[:2000],
    )
    try:
        extracted = _extract_json_object(raw)
        parsed = _loads_lenient(extracted)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[H-DIAG] rebalance JSON parse failed: %s", exc)
        raise
    if not isinstance(parsed, dict):
        raise ValueError("rebalance LLM did not return a JSON object")
    changes = parsed.get("changes", [])
    if isinstance(changes, list):
        logger.info(
            "[H-DIAG] rebalance proposed %d changes: %s",
            len(changes),
            [
                f"slide{c.get('slide_index')}→{c.get('new_layout_kind')}"
                for c in changes
                if isinstance(c, dict)
            ],
        )
    else:
        logger.warning(
            "[H-DIAG] rebalance parsed but `changes` is not a list: %r",
            changes,
        )
    return parsed


# Payload field name keyed by layout_kind. When the LLM emits a change,
# we read the matching key out of `new_payload` and write it on the slide
# (also clearing the previous layout's payload to keep the spec clean).
_LAYOUT_PAYLOAD_FIELDS = {
    "standard": None,
    "section_break": None,
    "stat_callout": "stat",
    "quote": "quote",
    "two_column": "columns",
    "icon_rows": "icon_rows",
    "image_focus": None,
}


def _apply_rebalance_change(
    spec_dict: dict[str, Any],
    change: dict[str, Any],
) -> bool:
    """Apply one LLM change to spec_dict in place. Returns True on success.

    Defensive: any malformed change (missing keys, out-of-range index,
    unknown layout_kind) is logged and skipped — we never raise from
    inside the apply loop because one bad change shouldn't tank the
    whole rebalance pass.
    """
    try:
        idx = int(change.get("slide_index", -1))
    except (TypeError, ValueError):
        logger.warning(
            "[H-DIAG] change FAILED to apply (missing slide_index): %r",
            change,
        )
        return False
    new_layout = change.get("new_layout_kind", "").strip().lower().replace("-", "_")
    new_payload = change.get("new_payload") or {}

    slides = spec_dict.get("slides", [])
    if not (0 <= idx < len(slides)):
        logger.warning(
            "[H-DIAG] change FAILED to apply (slide_index %s out of range)"
            "\n  raw change: %r",
            idx, change,
        )
        return False
    if new_layout not in _LAYOUT_PAYLOAD_FIELDS:
        logger.warning(
            "[H-DIAG] change FAILED to apply (unknown layout_kind %r)"
            "\n  raw change: %r",
            new_layout, change,
        )
        return False

    target = slides[idx]
    old_layout = target.get("layout_kind", "standard")
    target["layout_kind"] = new_layout

    # Clear all layout-specific payload keys then set the new one. Keeping
    # leftovers around is harmless (Pydantic ignores them on the wrong
    # layout_kind) but makes the spec dict ambiguous to inspect.
    for field_name in ("stat", "quote", "columns", "icon_rows"):
        target.pop(field_name, None)

    payload_field = _LAYOUT_PAYLOAD_FIELDS[new_layout]
    if payload_field is not None:
        # Accept either the named field nested in new_payload or
        # new_payload itself being the payload object.
        value = new_payload.get(payload_field, new_payload)
        target[payload_field] = value
    logger.info(
        "[H-DIAG] applied change to slide %d: %s → %s",
        idx, old_layout, new_layout,
    )
    return True


async def _rebalance_layouts(
    spec_dict: dict[str, Any],
    violations: list[LayoutViolation],
    chunks_text: str,
    *,
    bearer: str,
) -> dict[str, Any]:
    """Run the focused LLM rebalance pass and return an updated spec_dict.

    Contract:
    - Caps applied changes at `LAYOUT_REBALANCE_MAX_CHANGES`.
    - Re-validates the resulting spec via `SlidesSpec.model_validate`.
      If validation fails, the original spec_dict is returned (caller
      proceeds with the un-rebalanced spec, gracefully degrades).
    - Re-audits after applying; if V1 STILL violates, logs a warning and
      returns the (best-effort) rebalanced dict anyway. User prefers a
      slightly-imperfect deck over a 502.
    """
    # Round 6 Patch V: hint violations (V4_TITLE) are informational only.
    # Listing them in the prompt just makes the LLM waste tokens explaining
    # why it won't restructure flowing-narrative bullets. Drop them, and if
    # nothing actionable remains, skip the LLM call entirely.
    actionable = [v for v in violations if v.severity != "hint"]
    if not actionable:
        logger.info(
            "[H-DIAG] rebalance skipped: only hint violations present"
        )
        return spec_dict

    candidates = _select_rebalance_candidates(actionable)
    prompt = _build_rebalance_prompt(
        spec_dict, actionable, chunks_text, candidates,
    )
    logger.info(
        "[H-DIAG] rebalance LLM call: prompt_len=%d, n_actionable=%d",
        len(prompt[0]) + len(prompt[1]), len(actionable),
    )
    try:
        result = await _call_llm_for_rebalance(prompt, bearer=bearer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rebalance LLM call failed: %s", exc)
        return spec_dict

    raw_changes = result.get("changes")
    if not isinstance(raw_changes, list):
        logger.warning("rebalance: response missing `changes` list: %r", result)
        return spec_dict

    # Cap before applying — we don't even want to evaluate changes beyond
    # the cap, in case a malformed-but-valid change in slot 4 wastes log
    # noise.
    capped = raw_changes[:LAYOUT_REBALANCE_MAX_CHANGES]
    if len(raw_changes) > LAYOUT_REBALANCE_MAX_CHANGES:
        logger.info(
            "rebalance: capping %d proposed changes to %d",
            len(raw_changes), LAYOUT_REBALANCE_MAX_CHANGES,
        )

    # Apply on a deep copy so a Pydantic-validation failure leaves the
    # original spec_dict intact for the caller's fallback path.
    candidate_dict = json.loads(json.dumps(spec_dict))
    applied = 0
    for change in capped:
        if not isinstance(change, dict):
            continue
        if _apply_rebalance_change(candidate_dict, change):
            applied += 1

    if applied == 0:
        logger.info("rebalance: no changes applied (LLM returned empty / invalid set)")
        # Even with 0 applied changes, fall through to the post-audit so
        # callers see the warning path consistently.

    try:
        new_spec = SlidesSpec.model_validate(candidate_dict)
    except ValidationError as exc:
        logger.warning(
            "rebalance: post-apply spec validation failed (%s);"
            " keeping original spec",
            exc,
        )
        return spec_dict

    # Re-audit. V1 STILL violated → log and continue. We deliberately
    # don't raise — degrading gracefully is the explicit product choice.
    # Note: this call re-enters _audit_layout_distribution so position-1
    # [H-DIAG] log will appear a second time in the trail. Time order in
    # the log makes the post-rebalance pass obvious.
    post_violations = _audit_layout_distribution(new_spec, chunks_text)
    pre_content = sum(1 for v in violations if v.kind == "V4_CONTENT")
    post_content = sum(1 for v in post_violations if v.kind == "V4_CONTENT")
    post_hint = sum(1 for v in post_violations if v.kind == "V4_TITLE")
    logger.info(
        "[H-DIAG] post-rebalance audit: V4_CONTENT %d→%d, V4_TITLE %d (hints)",
        pre_content, post_content, post_hint,
    )
    if any(v.kind == "V1" for v in post_violations):
        logger.warning(
            "rebalance: V1 (standard > %d%%) still violates after %d changes"
            " — proceeding with rebalanced spec anyway",
            int(LAYOUT_STANDARD_MAX_RATIO * 100),
            applied,
        )

    return new_spec.model_dump(mode="json")


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
    if not payload.skip_retrieval:
        try:
            chunks = await _retrieve_chunks(
                bearer, payload.collection_id, seed_query,
            )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            # Retrieval is best-effort — see commentary on the
            # original sync endpoint. Continue without context.
            logger.warning(
                "Studio retrieval failed (%s); generating without context.", e,
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
    await updater.set(step=JOB_STEP_GENERATING)
    spec, used_fallback = await _generate_validated_spec(
        bearer,
        coll.name,
        payload.preset,
        payload.extra_instructions,
        chunks,
        images=images,
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
    if get_flux_provider() is not None and deck_base_seed is not None:
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
    pptx_bytes, pptx_path = await _render_pptx(
        spec, images_lookup, bearer=bearer,
        deck_base_seed=deck_base_seed, llm=flux_llm,
        deck_style=deck_style,
    )

    # ── Step 8: vision QA + (optional) one fix-and-rerender ──
    # Skip vision QA entirely when serving the fallback deck. The
    # fallback is a deliberately minimal "what went wrong" template
    # — putting it through QA risks the VLM flagging it as too
    # sparse, triggering a fix-and-rerender that calls the same
    # already-broken LLM again. Better to ship the fallback as-is.
    final_defects: list[VisualDefect] = []
    qa_passes = 0
    if pptx_path and not used_fallback:
        for _ in range(VISUAL_QA_PASSES + 1):
            qa_passes += 1
            await updater.set(
                step=JOB_STEP_QA,
                qa_passes=qa_passes,
            )
            defects = await _visual_qa(
                bearer, pptx_path, pptx_bytes=pptx_bytes,
            )
            critical = [d for d in defects if d.severity == "critical"]
            if not critical or qa_passes > VISUAL_QA_PASSES:
                final_defects = defects
                break
            # Critical defects exist AND we still have a fix budget —
            # ask the LLM to revise, re-render, re-QA.
            await updater.set(step=JOB_STEP_FIXING)
            try:
                spec = await _fix_spec_with_defects(bearer, spec, critical)
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                logger.warning("Studio defect-fix LLM call failed: %s", e)
                final_defects = defects
                break
            await updater.set(
                step=JOB_STEP_RENDERING,
                title=spec.title,
                slide_count=len(spec.slides),
            )
            pptx_bytes, pptx_path = await _render_pptx(
                spec, images_lookup, bearer=bearer,
                deck_base_seed=deck_base_seed, llm=flux_llm,
                deck_style=deck_style,
            )

    # ── Step 9: terminal "done" — pptx_bytes is the artifact ──
    await updater.mark_done(
        spec=spec,
        pptx_bytes=pptx_bytes,
        defects=final_defects,
        qa_passes=qa_passes,
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

    record = await jobs.create_job(
        user_id=identity.id,
        collection_id=payload.collection_id,
        runner=_runner,
    )
    return record.to_status()


@router.get("/slides/jobs/{job_id}", response_model=JobStatus)
async def get_slides_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> JobStatus:
    """Cheap polling endpoint. Returns the current JobStatus or 404."""
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        # 404 covers both "doesn't exist" and "exists but belongs to
        # someone else" — the latter must NEVER leak to a different user.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found (unknown id, evicted, or not yours).",
        )
    return rec.to_status()


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
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

    # RFC 5987 percent-encoded filename for CJK titles. Same trick as the
    # old sync endpoint — keeps the .pptx download header USASCII-safe
    # while modern browsers honour the filename* parameter for the real
    # CJK title. Header buffer is no longer a concern (we don't ship
    # defects[] in headers anymore — the GET /status JSON has them).
    from urllib.parse import quote

    raw_title = (rec.title or "presentation").replace('"', "")[:80]
    encoded_title = quote(raw_title, safe="")
    ascii_title = (
        raw_title.encode("ascii", "ignore").decode("ascii").strip() or "presentation"
    )

    pptx_bytes = rec.pptx_bytes  # local alias so the closure doesn't read state.

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

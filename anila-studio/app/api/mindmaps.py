"""Mindmap (心智圖) API — job-based pipeline producing Graphviz-rendered SVG.

Job-based shape (mirrors slides):

    POST   /api/mindmaps/jobs                        → 202 + initial MindmapJobStatus
    GET    /api/mindmaps/jobs/{id}                   → MindmapJobStatus (poll)
    GET    /api/mindmaps/jobs/{id}/download/{fmt}    → SVG or DOT stream
    DELETE /api/mindmaps/jobs/{id}                   → 204 (cancel)

Pipeline phases (run on an asyncio.Task spawned by the POST handler):

    request → [POST /jobs] → 202 job_id
                  │
                  └─ asyncio task ────────────────────────────────┐
                       │                                           │
                       ▼                                           │
                  [1] csp.get_collection — authz/exists check      │
                       │                                           │
                       ▼                                           │
                  [2] csp.search_chunks — top-K relevant context   │
                       │                                           │
                       ▼                                           │
                  [3] LLM (gemma4) emits MindmapSpec JSON          │
                       │                                           │
                       ▼                                           │
                  [4] schema validate + s2twp normalise labels     │
                       │                                           │
                       ▼                                           │
                  [5] spec_to_dot(spec) → DOT source               │
                       │                                           │
                       ▼                                           │
                  [6] dot -Tsvg < dot_source → SVG bytes           │
                       │                                           │
                       ▼                                           │
                  [7] write {ARTIFACTS_DIR}/{job_id}.svg + .dot    │
                       │                                           │
                       ▼                                           │
                  state="done", download URLs available ───────────┘
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
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
    search_chunks,
)
from app.config import settings
from app.schemas.mindmap import (
    GenerateMindmapRequest,
    MindmapJobStatus,
    MindmapNode,
    MindmapPreset,
    MindmapSpec,
)
from app.services import mindmap_job_service as jobs
from app.services.mindmap_renderer import (
    MindmapRenderError,
    count_nodes,
    render_svg,
    spec_to_dot,
)
# JSON-extraction helpers from the canonical module (god-module split dedup);
# SLIDES_LLM_MODEL / call_llm_chat from their home modules — mindmaps no longer
# reaches into app.api.studio's internals.
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.studio_config import SLIDES_LLM_MODEL
from app.services.studio_llm import call_llm_chat as _call_llm_chat
from app.services.studio_text_normalizer import (
    strip_inline_citations,
    strip_latex,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/mindmaps", tags=["Studio / Mindmaps"])


# ── Tunables ─────────────────────────────────────────────────────────────


# Per-chunk truncation (chars) when assembling the LLM prompt. Same
# rationale as the slides pipeline: cap the prompt budget per chunk so
# top-K stays meaningful even if csp returns long chunks.
MINDMAP_CONTENT_LIMIT_CHARS = 1200

# Minimum cosine score for retrieved chunks to be included. Below this
# the chunk is more noise than signal; the LLM produces vaguer maps.
MINDMAP_MIN_SCORE = 0.25

# How many times to retry on schema-validation failure. One retry is
# usually enough; if the LLM emits two malformed responses in a row the
# job fails — the user can re-request.
SCHEMA_CORRECTION_PASSES = 1


# Default seed_query per preset, used when the request omits one. The
# embedder gets a query string; if seed_query is None we fall back to a
# preset-specific phrase that targets the kind of structure the user
# asked for.
_DEFAULT_SEED_QUERY: dict[MindmapPreset, str] = {
    MindmapPreset.CONCEPT_TREE: "核心概念與子概念展開",
    MindmapPreset.TASK_BREAKDOWN: "任務分解與工作項目",
    MindmapPreset.SOP_FLOW: "標準作業程序步驟",
    MindmapPreset.ORG_RELATIONSHIPS: "組織結構與角色關係",
}


# Human-friendly Chinese label per preset, used inside the system
# prompt to nudge the LLM toward the right structural shape.
_PRESET_LABEL: dict[MindmapPreset, str] = {
    MindmapPreset.CONCEPT_TREE: "概念樹（從根概念展開為主題地圖）",
    MindmapPreset.TASK_BREAKDOWN: "任務拆解（WBS / 工作分解結構）",
    MindmapPreset.SOP_FLOW: "SOP 流程（步驟導向的程序圖）",
    MindmapPreset.ORG_RELATIONSHIPS: "組織 / 關係圖",
}


# ── Prompt assembly ──────────────────────────────────────────────────────


def _build_prompt(
    *,
    collection_name: str,
    preset: MindmapPreset,
    max_depth: int,
    chunks: list[dict[str, Any]],
    extra_instructions: str | None,
) -> tuple[str, str]:
    """Compose (system, user) prompts for the mindmap LLM call.

    Hard rules (echoed in the system prompt to maximise schema
    compliance):

      * Output is JSON only — first char ``{``, last char ``}``.
      * Tree must be hierarchical (root → children → grandchildren),
        not a flat list disguised as one level deep.
      * Labels are short (≤ 15 chars) so the rendered SVG boxes don't
        wrap awkwardly. LLMs love verbose labels; constrain explicitly.
      * Depth respects ``max_depth`` from the request.
    """
    preset_label = _PRESET_LABEL.get(preset, preset.value)

    system = "\n".join(
        [
            "You are a JSON-only mindmap generator. Output is parsed by",
            "a strict JSON parser, NOT by a human.",
            "",
            "Output rules (any violation = automatic rejection):",
            '- The very first character of your response MUST be "{".',
            '- The very last character of your response MUST be "}".',
            "- Do NOT include the word 'thought', 'reasoning', 'analysis',",
            "  or any commentary before or after the JSON.",
            "- Do NOT wrap in ```json or ``` code fences.",
            '- Use straight double quotes only — never single quotes \',',
            '  curly quotes "", or fullwidth 「」 for keys and string values.',
            "",
            "── Required top-level shape ──",
            'Required fields: title (string), preset (string),',
            "                 root (object), layout (string).",
            f'  - preset MUST equal "{preset.value}" (do not change it).',
            '  - layout: pick from "LR" (default, left→right) or "TB"',
            '    (top→bottom). LR reads better for concept trees;',
            "    TB reads better for task/SOP flows.",
            "",
            "── Required node shape (recursive) ──",
            "Each node has:",
            "  - id (short ascii string, unique within the tree)",
            "    Use a stable pattern: n0 for root, n0a/n0b for children,",
            "    n0a1/n0a2 for grandchildren, etc.",
            "  - label (display string; 2-15 Chinese characters typical)",
            "  - children (list of nodes, empty for leaves)",
            "  - note (optional, ≤ 200 chars; supporting context)",
            "",
            "── Structural rules (these decide whether the mindmap is",
            "useful or AI slop) ──",
            f'1. The mindmap shape is "{preset_label}". Compose the tree',
            "   so the structure REFLECTS that shape.",
            f"2. Maximum tree depth: {max_depth} levels (root counts as",
            "   level 1). Trees deeper than this get truncated; shallower",
            "   is fine when the topic doesn't warrant more nesting.",
            "3. Aim for 3-6 children at the root level. Each non-leaf",
            "   child should have 2-4 children of its own. Avoid",
            "   degenerate one-child chains.",
            "4. Labels MUST be short (≤ 15 characters). A mindmap with",
            "   sentence-length labels is unreadable. Use newline \\n to",
            "   force a manual line break when truly necessary.",
            "5. Use Taiwan Traditional Chinese (台灣繁體中文) — not just",
            "   character forms but Taiwan idioms (資料 not 數據,",
            "   軟體 not 軟件, 預設 not 默認, etc.). The backend runs",
            "   OpenCC s2twp as a safety net but you writing it right",
            "   produces better output.",
            "6. NO placeholders (lorem ipsum, TBD, TODO, <insert ...>).",
            "7. The root label should be the central topic (a noun phrase,",
            "   not a sentence).",
            "",
            "If the user message provides retrieved passages, use them",
            "as the factual basis. Do NOT fabricate concepts that are",
            "absent from the passages; instead structure what IS there.",
        ]
    )

    parts: list[str] = [
        f"知識庫名稱：{collection_name}",
        f"心智圖類型：{preset_label}",
        f"最大深度（含根）：{max_depth} 層",
    ]
    if chunks:
        parts.append("")
        parts.append("以下是從知識庫檢索到的相關段落（已依相似度排序）：")
        parts.append("")
        for i, c in enumerate(chunks, start=1):
            parts.append(
                f"[{i}] 來源：{c['filename']}（chunk {c['chunk_key']}，"
                f"相似度 {c['score']:.3f}）"
            )
            parts.append(c["content"])
            parts.append("")
    else:
        parts.append(
            "（本次未檢索到相關段落；請根據心智圖類型與使用者補充指示，"
            "輸出一份結構合理的通用範本，並在 root.note 內提醒"
            "「本心智圖未取得文件支撐」。）"
        )

    if extra_instructions:
        parts.append("")
        parts.append(f"使用者補充指示：\n{extra_instructions}")

    return system, "\n".join(parts)


# ── Retrieval ────────────────────────────────────────────────────────────


async def _retrieve_chunks_for_mindmap(
    *,
    bearer: str,
    collection_id: int,
    seed_query: str,
    top_k: int,
    document_ids: list[int] | None,
) -> tuple[list[dict[str, Any]], str]:
    """Top-K chunks for the mindmap seed_query. Returns (chunks, name).

    Returns an empty list of chunks (but always a collection name) when
    csp's collection is archived or empty — the prompt falls through to
    "no context" mode rather than failing the job.
    """
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        return [], coll.name

    hits = await search_chunks(
        collection_id,
        seed_query,
        top_k=top_k,
        min_score=MINDMAP_MIN_SCORE,
        document_ids=document_ids,
        bearer=bearer,
    )
    chunks = [
        {
            "filename": h.filename or "<unknown>",
            "chunk_key": h.chunk_key,
            "content": h.content[:MINDMAP_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
    ]
    return chunks, coll.name


# ── LLM → MindmapSpec ────────────────────────────────────────────────────


def _truncate_depth(node: MindmapNode, *, remaining_depth: int) -> MindmapNode:
    """Return a copy of ``node`` whose subtree is truncated at depth.

    ``remaining_depth`` counts down from the request's max_depth, where
    root is depth 1. When it reaches 1 (or below) we keep the node but
    drop its children. Used to enforce the schema's depth contract
    even if the LLM disregarded the prompt instruction.
    """
    if remaining_depth <= 1 or not node.children:
        return node.model_copy(update={"children": []})
    new_children = [
        _truncate_depth(c, remaining_depth=remaining_depth - 1)
        for c in node.children
    ]
    return node.model_copy(update={"children": new_children})


# Pattern that recognises the prefix the diagram renderer also strips
# (per-chunk citation markers like ``(參 [5])``). We re-use the existing
# strip helpers from studio_text_normalizer so we get the OpenCC-safe
# behaviour without re-implementing.


_ALLOWED_ID_RE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _normalise_id(node_id: str, *, fallback: str) -> str:
    """Coerce an LLM-supplied id into a safe DOT identifier.

    DOT accepts quoted ids so we don't strictly need this, but a small
    cleanup avoids edge cases (empty strings, leading/trailing
    whitespace, embedded quotes). Falls back to ``fallback`` if the
    cleaned id is empty.
    """
    cleaned = _ALLOWED_ID_RE.sub("_", node_id.strip())
    cleaned = cleaned.strip("_")
    return cleaned or fallback


def _normalise_label(label: str) -> str:
    """Mirror diagram_renderer's pre-render normalisation.

    Runs strip_inline_citations then strip_latex. We deliberately do
    NOT run the OpenCC s2twp pass here because (a) the LLM is told to
    use Taiwan TC already, and (b) OpenCC is heavyweight and we run on
    every node — adding it to a 100-node tree is noticeable. If a
    follow-up rolls out s2twp on mindmap labels, it should land in
    this function so the renderer keeps a single source of truth.
    """
    cleaned = strip_inline_citations(label) or ""
    cleaned = strip_latex(cleaned) or ""
    return cleaned.strip() or label


def _normalise_node(
    node: MindmapNode, *, path_prefix: str, index: int,
) -> MindmapNode:
    """Walk a tree applying id + label normalisation in-place via copy."""
    safe_id = _normalise_id(node.id, fallback=f"{path_prefix}{index}")
    safe_label = _normalise_label(node.label)
    safe_note = node.note
    if safe_note is not None:
        safe_note = _normalise_label(safe_note) or None
    new_children = [
        _normalise_node(c, path_prefix=f"{safe_id}_", index=i)
        for i, c in enumerate(node.children)
    ]
    return node.model_copy(
        update={
            "id": safe_id,
            "label": safe_label,
            "note": safe_note,
            "children": new_children,
        }
    )


def _normalise_spec(spec: MindmapSpec, *, max_depth: int) -> MindmapSpec:
    """Apply depth truncation + recursive id/label normalisation."""
    truncated_root = _truncate_depth(spec.root, remaining_depth=max_depth)
    normalised_root = _normalise_node(
        truncated_root, path_prefix="n", index=0,
    )
    # title also goes through the cheap citation/latex strip; OpenCC
    # remains opt-in for slides.
    safe_title = _normalise_label(spec.title)
    return spec.model_copy(update={
        "title": safe_title,
        "root": normalised_root,
    })


async def _generate_validated_spec(
    *,
    bearer: str,
    collection_name: str,
    preset: MindmapPreset,
    max_depth: int,
    chunks: list[dict[str, Any]],
    extra_instructions: str | None,
) -> MindmapSpec:
    """Call the LLM, parse + validate, retry once on schema failure.

    Raises:
      * ``MindmapGenerationError`` after the retry budget is exhausted.

    On success returns a ``MindmapSpec`` already passed through
    ``_normalise_spec`` (depth-truncated, citation/latex-stripped).
    """
    system, user_msg = _build_prompt(
        collection_name=collection_name,
        preset=preset,
        max_depth=max_depth,
        chunks=chunks,
        extra_instructions=extra_instructions,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    last_err: Exception | None = None
    last_raw = ""
    for attempt in range(SCHEMA_CORRECTION_PASSES + 1):
        # temperature=0.3 — same rationale as slides: low enough for
        # structural fidelity, high enough that the LLM isn't always
        # picking the safest two-deep tree.
        raw = await _call_llm_chat(
            bearer, SLIDES_LLM_MODEL, messages, temperature=0.3,
        )
        last_raw = raw
        try:
            extracted = _extract_json_object(raw)
            parsed = _loads_lenient(extracted)
            # Force the preset to match the request (LLM sometimes
            # echoes the human label instead of the enum value).
            if isinstance(parsed, dict):
                parsed["preset"] = preset.value
            spec = MindmapSpec.model_validate(parsed)
            return _normalise_spec(spec, max_depth=max_depth)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning(
                "Mindmap validation failed attempt=%d err=%s",
                attempt + 1,
                str(e)[:200],
            )
            if attempt >= SCHEMA_CORRECTION_PASSES:
                break
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
                    ),
                }
            )

    raise MindmapGenerationError(
        f"LLM failed to produce a valid mindmap after "
        f"{SCHEMA_CORRECTION_PASSES + 1} attempts: "
        f"{str(last_err)[:200]} | raw[:200]={last_raw[:200]!r}"
    )


class MindmapGenerationError(RuntimeError):
    """Raised when the LLM can't produce a schema-valid mindmap."""


# ── Pipeline runner ──────────────────────────────────────────────────────


async def _run_pipeline(
    *,
    identity: CurrentUserIdentity,
    bearer: str,
    payload: GenerateMindmapRequest,
    updater: jobs.MindmapJobUpdater,
) -> None:
    """Orchestrate retrieval → LLM → render → persist for one mindmap job.

    Each step pushes a state update so the polling SPA can show a
    "正在檢索 / 正在生成 / 正在繪製" indicator. On any uncaught
    exception, the wrapper inside ``mindmap_job_service.create_job``
    sets state="failed" with the truncated error message.
    """
    # Step 1+2 — retrieval ----------------------------------------------------
    await updater.set(step=jobs.JOB_STEP_RETRIEVING)
    seed_query = (
        payload.seed_query
        or _DEFAULT_SEED_QUERY.get(payload.preset, payload.preset.value)
    )
    chunks, collection_name = await _retrieve_chunks_for_mindmap(
        bearer=bearer,
        collection_id=payload.collection_id,
        seed_query=seed_query,
        top_k=payload.top_k,
        document_ids=payload.document_ids,
    )

    # Step 3+4 — LLM + validate + normalise -----------------------------------
    await updater.set(step=jobs.JOB_STEP_GENERATING)
    spec = await _generate_validated_spec(
        bearer=bearer,
        collection_name=collection_name,
        preset=payload.preset,
        max_depth=payload.max_depth,
        chunks=chunks,
        extra_instructions=payload.extra_instructions,
    )

    # Step 5+6 — render -------------------------------------------------------
    await updater.set(step=jobs.JOB_STEP_RENDERING, title=spec.title)
    dot_source = spec_to_dot(spec)
    try:
        svg_bytes = await render_svg(dot_source)
    except MindmapRenderError:
        # Bubble up as a runtime error so the job-record wrapper
        # captures the message. No fallback path for mindmaps.
        raise

    # Step 7 — persist + mark done -------------------------------------------
    artifacts_dir = Path(settings.ARTIFACTS_DIR)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    svg_path = artifacts_dir / f"{updater.job_id}.svg"
    dot_path = artifacts_dir / f"{updater.job_id}.dot"
    # Write off-loop so we don't block the asyncio scheduler on disk I/O.
    # Tiny files (<100 KB typically) so a sync write inside to_thread is
    # fine and simpler than aiofiles.
    await asyncio.to_thread(svg_path.write_bytes, svg_bytes)
    await asyncio.to_thread(dot_path.write_text, dot_source, encoding="utf-8")

    await updater.mark_done(
        title=spec.title,
        node_count=count_nodes(spec),
        svg_bytes=svg_bytes,
        dot_source=dot_source,
    )


# ── Endpoint handlers ────────────────────────────────────────────────────


@router.post(
    "/jobs",
    response_model=MindmapJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_mindmap_job(
    payload: GenerateMindmapRequest,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
    bearer: str = Depends(get_bearer_token),
) -> MindmapJobStatus:
    """Register a mindmap-generation job and return its initial status.

    Returns immediately (HTTP 202) with state="pending" and a job_id.
    The pipeline runs on an asyncio.Task in the background; clients poll
    GET /jobs/{id} for state transitions and GET /jobs/{id}/download/svg
    once state="done".
    """
    # Authorize collection access up-front so the user gets a synchronous
    # 403/404 instead of an opaque "failed" job seconds later. Mirrors
    # the slides handler's pre-flight check.
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

    async def _runner(updater: jobs.MindmapJobUpdater) -> None:
        await _run_pipeline(
            identity=identity, bearer=bearer, payload=payload, updater=updater,
        )

    record = await jobs.create_job(
        user_id=identity.id,
        collection_id=payload.collection_id,
        preset=payload.preset,
        runner=_runner,
    )
    return record.to_status()


@router.get("/jobs/{job_id}", response_model=MindmapJobStatus)
async def get_mindmap_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> MindmapJobStatus:
    """Cheap polling endpoint. Returns the current MindmapJobStatus or 404."""
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
    "/jobs/{job_id}/download/{fmt}",
    responses={
        200: {
            "content": {
                "image/svg+xml": {},
                "text/vnd.graphviz": {},
            },
            "description": "Generated mindmap (SVG primary, DOT for debug)",
        }
    },
)
async def download_mindmap(
    job_id: str,
    fmt: Literal["svg", "dot"],
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> StreamingResponse:
    """Stream the rendered SVG or DOT source for a completed job.

    Returns 404 for unknown/cross-user jobs, 409 if still running, and
    410 if the job is failed/cancelled.
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.",
        )
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

    if fmt == "svg":
        if rec.svg_bytes is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Job marked done but svg bytes are missing.",
            )
        body = rec.svg_bytes
        media_type = "image/svg+xml"
        filename = "mindmap.svg"
    else:
        if rec.dot_source is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Job marked done but dot source is missing.",
            )
        body = rec.dot_source.encode("utf-8")
        # No official IANA type for DOT; use the common community value
        # and let the browser save-as do its thing.
        media_type = "text/vnd.graphviz"
        filename = "mindmap.dot"

    async def _stream() -> Any:
        yield body

    return StreamingResponse(
        _stream(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(body)),
        },
    )


@router.delete(
    "/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_mindmap_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> None:
    """Cancel an in-flight job. No-op if already terminal."""
    cancelled = await jobs.cancel_job(job_id, identity.id)
    if not cancelled:
        rec = jobs.get_user_job(job_id, identity.id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found.",
            )
    # Returning None lets FastAPI emit a 204 with no body.
    return None

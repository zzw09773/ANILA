"""Infographic API — HTML + chart PNG + PDF artifact pipeline.

Job-based, mirroring ``/api/studio/slides/jobs``:

    POST   /api/infographics/jobs               → 202 + InfographicJobStatus
    GET    /api/infographics/jobs/{id}          → InfographicJobStatus
    GET    /api/infographics/jobs/{id}/download/{fmt}
                                                → application/{html|pdf}
    DELETE /api/infographics/jobs/{id}          → 204

Pipeline:

    1. csp_client.get_collection (authz + name)
    2. csp_client.search_chunks  (top_k bounded by request, default 12)
    3. LLM(proxy_chat_completions) → InfographicSpec JSON
       system prompt: extract concrete numbers / comparisons; if a
       field can't be filled from chunks, leave it blank — DO NOT
       make up data.
    4. Validate via Pydantic. On failure, ONE correction pass.
    5. strip_latex + s2twp normalise every visible string.
    6. matplotlib render each ChartSpec → PNG bytes (in-process,
       single thread, Agg backend).
    7. Jinja2 render HTML with PNG base64 inline.
    8. Playwright headless chromium → PDF at ARTIFACTS_DIR/{job_id}.pdf.
    9. Write HTML at ARTIFACTS_DIR/{job_id}.html.
    10. updater.mark_done(...) → JobRecord.state = "done".
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
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
    proxy_chat_completions,
    search_chunks,
)
from app.config import settings
from app.schemas.infographic import (
    ChartSpec,
    GenerateInfographicRequest,
    InfographicJobStatus,
    InfographicPreset,
    InfographicSpec,
    JOB_STEP_GENERATING,
    JOB_STEP_RENDERING_CHARTS,
    JOB_STEP_RENDERING_HTML,
    JOB_STEP_RENDERING_PDF,
    JOB_STEP_RETRIEVING,
)
from app.services import infographic_job_service as jobs
from app.services import job_lifecycle
from app.services.infographic_renderer import (
    render_chart_png,
    render_html,
    render_pdf,
)
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.retrieval_status import (
    RETRIEVAL_FAILED_PROMPT_NOTE,
    RETRIEVAL_FAILED_WARNING,
)
from app.services.studio_text_normalizer import strip_inline_citations, strip_latex

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/infographics", tags=["Studio / Infographic"])


# ── Tunables ────────────────────────────────────────────────────────────────

# Default LLM same as slide pipeline — single deployed model, no per-feature
# fan-out. Could be made overridable later.
INFOGRAPHIC_LLM_MODEL = "gemma4"

# Bounded so a single chunk-pull doesn't blow the prompt context. Each
# chunk ~800 chars → top_k=12 ≈ 9.6 KB context; well within gemma4 256K.
INFOGRAPHIC_MIN_SCORE = 0.0
INFOGRAPHIC_CONTENT_LIMIT_CHARS = 1500
# Markdown horizontal rule `---` / isolated headers / empty paragraph
# chunks beat real KPI tables under semantically-weak seed queries.
# Drop anything below this length before LLM context build.
INFOGRAPHIC_MIN_CHUNK_CONTENT_CHARS = 50

# Short Chinese phrase per preset for seed_query enrichment.
# 原 seed_query 是 ``coll.name + extra_instructions``,對中文 collection
# 在 preset 沒映射的情況很弱。
_INFOGRAPHIC_SEED_PHRASES: dict[str, str] = {
    "mission_dashboard": "任務 dashboard 指標 進度 比較",
    "stats_brief": "數據 統計 數字 KPI 趨勢",
    "comparison_matrix": "比較 對照 對比 矩陣",
    "timeline_overview": "時間軸 事件 進度 變化",
}

# JSON correction pass count — 1 retry mirrors the slide side. Two
# failures in a row almost always mean the model is hallucinating
# structurally, retrying further wastes time.
SCHEMA_CORRECTION_PASSES = 1


# ── JSON extraction → canonical app/services/llm_json (dedup) ─────────────────
# _extract_json_object / _loads_lenient are imported above (aliased). This
# module's copy was byte-for-byte identical to the canonical version, so the
# convergence is a pure dedup with no behaviour change.


# ── Prompt construction ────────────────────────────────────────────────────


_PRESET_HINTS: dict[InfographicPreset, str] = {
    InfographicPreset.MISSION_DASHBOARD: (
        "任務 dashboard — 4-6 個 stats、1-2 張 chart（bar/line 為主）、"
        "簡短 timeline（可選）。要透露「現況、進度、下一步」的指揮感。"
    ),
    InfographicPreset.STATS_BRIEF: (
        "數據簡報 — stats 為主秀 4-6 個關鍵指標，可附 1 張對比 chart。"
        "comparison/timeline 通常不需要。"
    ),
    InfographicPreset.COMPARISON_MATRIX: (
        "比較矩陣 — 重心在 comparison list（建議 3-8 列、2-4 欄）；"
        "stats 0-3 個輔助；chart 可選 1 張支援結論。"
    ),
    InfographicPreset.TIMELINE_OVERVIEW: (
        "時間軸總覽 — 重心在 timeline（建議 4-8 件事件，依時間排序）；"
        "stats 0-3 個輔助；chart 可選 1 張趨勢圖。"
    ),
}


def _build_generation_prompt(
    collection_name: str,
    preset: InfographicPreset,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
    *,
    retrieval_failed: bool,
) -> tuple[str, str]:
    """Compose (system, user) prompts for the InfographicSpec LLM call.

    ``retrieval_failed=True`` means the search errored rather than
    returning nothing, so an empty ``chunks`` list is not evidence about
    the corpus — see ``app.services.retrieval_status``. Required, without
    a default — ``False`` is the value that reinstates the defect, so
    forgetting it must break the call rather than the copy.

    Hard rules echoed in the system prompt:
      - Output JSON only (first char '{', last char '}').
      - Numbers must come from chunks; do NOT fabricate.
      - takeaway is mandatory; everything else can be empty.
      - Traditional Chinese (Taiwan) — same wording rules as the slide
        pipeline.
    """
    preset_hint = _PRESET_HINTS.get(preset, "依內容自行選擇結構。")

    system = "\n".join(
        [
            "You are a JSON-only infographic generator. Output is parsed",
            "by a strict JSON parser, NOT by a human.",
            "",
            "Output rules (any violation = automatic rejection):",
            '- The very first character of your response MUST be "{".',
            '- The very last character of your response MUST be "}".',
            "- Do NOT include thoughts, reasoning, analysis preambles.",
            "- Do NOT wrap in code fences (```).",
            "- Use straight double quotes only.",
            "",
            "── 頂層欄位 ──",
            'Required: title (string ≤120), preset (string), takeaway (string ≤400).',
            'Optional: subtitle (string ≤200), stats (0-6), charts (0-3),',
            "          comparison (list[ComparisonRow]), timeline (list[TimelineEvent])",
            "",
            f'preset 必須等於 "{preset.value}"（呼叫端已指定，請原樣寫回）。',
            "",
            "── StatBlock 欄位 ──",
            'value (大字數字，如 "47%" "3.5×" "N=10,000")、label (旁白)、',
            'delta (可選，如 "+12%" "-3%")、icon (可選 keyword)',
            "",
            "── ChartSpec 欄位 ──",
            'chart_type ∈ {"bar", "line", "pie", "donut", "hbar"}',
            'title、x_labels (1-20 個)、',
            'series: [{"name": "...", "values": [數字陣列]}]',
            "",
            "── ComparisonRow 欄位 ──",
            'label、columns (1-6 個字串)',
            "",
            "── TimelineEvent 欄位 ──",
            'date (顯示用字串，如 "2024-Q1" "2025-05")、title、description (可選)',
            "",
            "── 設計心法 ──",
            f"1. preset 提示：{preset_hint}",
            "2. **不要 data slop**：數字只在「真實來自 chunks」時才放；",
            "   空缺欄位寧可留空也別瞎填。",
            "3. takeaway 是一句結語（不是 1 段，1 句即可），整份的訊息收斂於此。",
            "4. **使用台灣繁體中文**，用詞符合台灣本土慣用語。",
            "5. 每個 chart 都要有意義 — 別放陳設用的長條圖。",
            "",
            "── chunks 與使用者主題的關係(很重要,常見錯誤點)──",
            "若 chunks 提供「文字內容」但跟使用者指示的主題明顯不符",
            "(e.g. chunks 是 RAG 技術文件,使用者要『華航 (2610) KPI』),",
            "嚴禁以下兩種行為:",
            "  a) **嚴禁編造 title 反映使用者主題** —— title 必須反映 chunks",
            "     真實內容(`Collection 名稱 · 主題摘要`),不要寫成「華航 KPI 報告」",
            "     之類使用者投射但 chunks 沒佐證的標題。",
            "  b) **嚴禁說「檔案只有檔名」** —— chunks 有實際內容,你看了發現",
            "     跟 user 主題無關,要直接說「本知識庫內容與『XXX』主題不符」,",
            "     在 takeaway 明示。",
            "",
            "若段落完全空白(retrieval 0 hits),根據 collection 名稱+使用者",
            "指示寫一個保守的草稿,並在 takeaway 中暗示「本草稿未取得文件支撐」。",
        ]
    )

    parts = [
        f"知識庫名稱：{collection_name}",
        f"preset：{preset.value}",
    ]
    if chunks:
        parts.append("")
        parts.append("以下是檢索到的相關段落（已依相似度排序）：")
        parts.append("")
        for i, c in enumerate(chunks, start=1):
            parts.append(
                f"[{i}] 來源：{c['filename']}（chunk {c['chunk_key']}，"
                f"相似度 {c['score']:.3f}）"
            )
            parts.append(c["content"])
            parts.append("")
    elif retrieval_failed:
        parts.append(RETRIEVAL_FAILED_PROMPT_NOTE.format(where="takeaway"))
    else:
        parts.append(
            "（本次未檢索到相關段落；請依使用者輸入直接發揮，"
            "並在 takeaway 中提醒「本草稿未取得文件支撐」。）"
        )

    if extra_instructions:
        parts.append("")
        parts.append(f"使用者補充指示：\n{extra_instructions}")

    return system, "\n".join(parts)


# ── LLM call ──────────────────────────────────────────────────────────────


async def _call_llm_chat(
    bearer: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.3,
) -> str:
    """Invoke csp's chat-completions proxy. Same exception-mapping pattern
    as the slide pipeline's helper, so callers see consistent HTTP codes
    regardless of which artifact family they hit.
    """
    try:
        response = await proxy_chat_completions(
            model=INFOGRAPHIC_LLM_MODEL,
            messages=messages,
            temperature=temperature,
            bearer=bearer,
        )
    except CspNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Infographic LLM '{INFOGRAPHIC_LLM_MODEL}' not registered.",
        ) from exc
    except CspUnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except CspForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (CspServerError, CspClientError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"csp proxy failed: {exc}",
        ) from exc

    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail="LLM returned an unexpected payload shape.",
        ) from e


async def _generate_validated_spec(
    bearer: str,
    collection_name: str,
    preset: InfographicPreset,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
    *,
    retrieval_failed: bool,
) -> InfographicSpec:
    """LLM → JSON → InfographicSpec, with one correction pass on failure.

    Unlike the slide pipeline we do NOT fall back to a synthetic
    safety-net deck here — an infographic with garbage data is worse
    than none. After two failures, propagate the ValidationError so
    the job lands in state="failed" with a descriptive error.
    """
    system, user_msg = _build_generation_prompt(
        collection_name, preset, extra_instructions, chunks,
        retrieval_failed=retrieval_failed,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    raw = await _call_llm_chat(bearer, messages, temperature=0.3)
    last_err: ValidationError | ValueError | json.JSONDecodeError | None = None

    for attempt in range(SCHEMA_CORRECTION_PASSES + 1):
        try:
            extracted = _extract_json_object(raw)
            parsed = _loads_lenient(extracted)
            # Force preset to match the request — the LLM's value is
            # informational; we trust the caller.
            if isinstance(parsed, dict):
                parsed["preset"] = preset.value
            return InfographicSpec.model_validate(parsed)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning(
                "Infographic validate fail attempt=%d err=%s",
                attempt + 1, str(e)[:200],
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
                        "重新輸出整個 JSON 物件。"
                        "屬性名與字串值必須用雙引號 \"\"，不可用單引號 '。"
                    ),
                }
            )
            raw = await _call_llm_chat(bearer, messages, temperature=0.2)

    raise HTTPException(
        status_code=502,
        detail=f"Infographic LLM output failed validation: {last_err}",
    )


# ── Normalisation ──────────────────────────────────────────────────────────


def _normalize_string(text: str | None) -> str | None:
    """strip_latex + strip_inline_citations + s2twp on a single string.

    Re-using the slide pipeline's helpers — same dependency that's
    pinned in pyproject.toml. The OpenCC converter is module-level
    cached so this is cheap on the hot path.
    """
    if text is None or text == "":
        return text
    text = strip_inline_citations(text) or ""
    text = strip_latex(text) or ""
    # OpenCC s2twp — lazy import so the chart-only tests can mock at
    # the renderer boundary without touching this.
    from app.services.studio_text_normalizer import _get_converter

    converter = _get_converter()
    return converter.convert(text)


def _normalize_spec(spec: InfographicSpec) -> InfographicSpec:
    """Return a new InfographicSpec with every user-visible string normalised.

    ``StatBlock.value`` stays untouched (it's typically a numeric token
    like "47%" / "3.5×" where OpenCC could corrupt the units).
    ``StatBlock.icon`` and ``ChartSpec.chart_type`` are enum-like;
    likewise untouched.
    """
    patch: dict[str, Any] = {
        "title": _normalize_string(spec.title) or spec.title,
        "subtitle": _normalize_string(spec.subtitle),
        "takeaway": _normalize_string(spec.takeaway) or spec.takeaway,
    }
    if spec.stats:
        patch["stats"] = [
            s.model_copy(
                update={
                    "label": _normalize_string(s.label) or s.label,
                    "delta": _normalize_string(s.delta),
                }
            )
            for s in spec.stats
        ]
    if spec.charts:
        patch["charts"] = [
            c.model_copy(
                update={
                    "title": _normalize_string(c.title) or c.title,
                    "x_labels": [
                        _normalize_string(x) or x for x in c.x_labels
                    ],
                }
            )
            for c in spec.charts
        ]
    if spec.comparison:
        patch["comparison"] = [
            r.model_copy(
                update={
                    "label": _normalize_string(r.label) or r.label,
                    "columns": [
                        _normalize_string(c) or c for c in r.columns
                    ],
                }
            )
            for r in spec.comparison
        ]
    if spec.timeline:
        patch["timeline"] = [
            ev.model_copy(
                update={
                    "title": _normalize_string(ev.title) or ev.title,
                    "description": _normalize_string(ev.description),
                }
            )
            for ev in spec.timeline
        ]
    return spec.model_copy(update=patch)


# ── Retrieval ──────────────────────────────────────────────────────────────


async def _retrieve_chunks(
    bearer: str,
    collection_id: int,
    seed_query: str,
    *,
    top_k: int,
    document_ids: list[int] | None,
) -> list[dict[str, Any]]:
    """Same shape as the slide pipeline's helper, scoped to this module."""
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        return []

    hits = await search_chunks(
        collection_id,
        seed_query,
        top_k=top_k,
        min_score=INFOGRAPHIC_MIN_SCORE,
        document_ids=document_ids,
        bearer=bearer,
    )
    return [
        {
            "filename": h.filename or "<unknown>",
            "chunk_key": h.chunk_key,
            "content": h.content[:INFOGRAPHIC_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
        # 過濾 markdown chunking artifact / 空白 chunks(production 觀察)
        if len((h.content or "").strip()) >= INFOGRAPHIC_MIN_CHUNK_CONTENT_CHARS
    ]


# ── Pipeline runner ────────────────────────────────────────────────────────


async def _run_pipeline(
    *,
    bearer: str,
    payload: GenerateInfographicRequest,
    updater: jobs.InfographicJobUpdater,
) -> None:
    """The async task body for one infographic job.

    Steps mirror the docstring at the top of this module; each step
    pushes a step label so the polling UI can show "鑄造中：..."
    """
    coll = await get_collection(payload.collection_id, bearer=bearer)

    # Step 1: retrieve
    await updater.set(step=JOB_STEP_RETRIEVING)
    # seed_query 預設 coll.name 對中文 collection 語意太弱。補 preset
    # 中文 phrase(類比 datatable 的 _PRESET_SEED_PHRASES 修法),讓搜尋
    # 更可能命中跟 preset 主題相關的 chunks。
    if payload.seed_query and payload.seed_query.strip():
        seed_query = payload.seed_query.strip()
    else:
        parts = [coll.name]
        phrase = _INFOGRAPHIC_SEED_PHRASES.get(payload.preset.value)
        if phrase:
            parts.append(phrase)
        if payload.extra_instructions:
            parts.append(payload.extra_instructions.strip())
        seed_query = " · ".join(parts)

    # hits / zero hits / failed are three outcomes, not two. Only the
    # third sets this flag — zero hits is a real answer about the corpus.
    retrieval_failed = False
    try:
        chunks = await _retrieve_chunks(
            bearer,
            payload.collection_id,
            seed_query,
            top_k=payload.top_k,
            document_ids=payload.document_ids,
        )
        # Diagnostic 同 datatable:空表 / 無 chart 通常是 LLM 看 chunks
        # 後判定無資料,先從 log 直接判斷是 retrieval 還是 LLM 端問題。
        if chunks:
            avg_len = sum(len(c.get("content", "")) for c in chunks) / len(chunks)
            empty_count = sum(1 for c in chunks if len(c.get("content", "")) < 50)
            logger.info(
                "Infographic retrieval: collection=%s(id=%s) seed_query=%r "
                "hits=%d avg_content_len=%.0f empty_chunks=%d/%d",
                coll.name, payload.collection_id, seed_query[:80],
                len(chunks), avg_len, empty_count, len(chunks),
            )
        else:
            logger.warning(
                "Infographic retrieval returned 0 hits: collection=%s(id=%s) "
                "seed_query=%r — LLM will produce empty spec.",
                coll.name, payload.collection_id, seed_query[:80],
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # Still ship the infographic, but declare the degradation: the
        # prompt stops claiming "0 hits" and the job status carries a
        # warning. The exception text stays in this operator log only.
        retrieval_failed = True
        logger.warning(
            "Infographic retrieval failed (%s); proceeding without context "
            "and declaring it to the user.",
            e,
        )
        chunks = []

    # Step 2: LLM → InfographicSpec
    await updater.set(step=JOB_STEP_GENERATING)
    spec = await _generate_validated_spec(
        bearer, coll.name, payload.preset, payload.extra_instructions, chunks,
        retrieval_failed=retrieval_failed,
    )
    spec = _normalize_spec(spec)
    await updater.set(title=spec.title, chart_count=len(spec.charts))

    # Step 3: charts → PNG
    await updater.set(step=JOB_STEP_RENDERING_CHARTS)
    chart_pngs: dict[int, bytes] = {}
    for idx, chart in enumerate(spec.charts):
        # Run matplotlib in a thread pool — it's blocking. asyncio's
        # default executor is fine here (chart count is bounded to 3).
        png = await asyncio.to_thread(render_chart_png, chart)
        chart_pngs[idx] = png

    # Step 4: HTML render (in-process, fast)
    await updater.set(step=JOB_STEP_RENDERING_HTML)
    html = await asyncio.to_thread(render_html, spec, chart_pngs)

    # Step 5: write HTML to disk
    artifacts_dir = Path(settings.ARTIFACTS_DIR)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    html_path = artifacts_dir / f"{updater.job_id}.html"
    pdf_path = artifacts_dir / f"{updater.job_id}.pdf"
    await asyncio.to_thread(html_path.write_text, html, "utf-8")

    # Step 6: HTML → PDF via Playwright
    await updater.set(step=JOB_STEP_RENDERING_PDF)
    await render_pdf(html, pdf_path)

    # Step 7: terminal "done"
    await updater.mark_done(
        title=spec.title,
        chart_count=len(spec.charts),
        html_path=str(html_path),
        pdf_path=str(pdf_path),
        # Soft warning coexisting with done: the infographic exists, it
        # just isn't grounded in the user's documents.
        warning=(RETRIEVAL_FAILED_WARNING if retrieval_failed else None),
    )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/jobs",
    response_model=InfographicJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_infographic_job(
    payload: GenerateInfographicRequest,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
    bearer: str = Depends(get_bearer_token),
) -> InfographicJobStatus:
    """Register an infographic-generation job, return 202 immediately."""
    # Authorise up-front so the user gets a synchronous 403/404 instead
    # of an opaque "failed" job seconds later.
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

    async def _runner(updater: jobs.InfographicJobUpdater) -> None:
        await _run_pipeline(bearer=bearer, payload=payload, updater=updater)

    report_ctx = job_lifecycle.make_context(
        artifact_type="infographic",
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
        preset=payload.preset,
        runner=_runner,
        report_ctx=report_ctx,
    )
    return record.to_status()


@router.get("/jobs/{job_id}", response_model=InfographicJobStatus)
async def get_infographic_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> InfographicJobStatus | dict:
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is not None:
        return rec.to_status()
    # Read-through to the durable job store after a restart / eviction.
    persisted = await job_lifecycle.read_status(job_id, identity.id)
    if persisted is not None:
        return persisted
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found (unknown id, evicted, or not yours).",
    )


@router.get(
    "/jobs/{job_id}/download/{fmt}",
    response_class=FileResponse,
)
async def download_infographic_artifact(
    job_id: str,
    fmt: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> FileResponse:
    """Stream the HTML or PDF artifact for a completed job.

    Returns 404 for unknown/cross-user jobs, 409 if still running,
    410 if failed/cancelled, 400 for unknown ``fmt``.
    """
    if fmt not in ("html", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="fmt must be one of: html, pdf",
        )

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

    path_str = rec.html_path if fmt == "html" else rec.pdf_path
    if not path_str or not Path(path_str).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Artifact file missing for fmt={fmt}.",
        )

    media_type = "text/html; charset=utf-8" if fmt == "html" else "application/pdf"
    raw_title = (rec.title or "infographic").replace('"', "")[:80]
    encoded_title = quote(raw_title, safe="")
    ascii_title = (
        raw_title.encode("ascii", "ignore").decode("ascii").strip() or "infographic"
    )
    return FileResponse(
        path=path_str,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_title}.{fmt}"; '
                f"filename*=UTF-8''{encoded_title}.{fmt}"
            ),
        },
    )


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_infographic_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> Response:
    """Cancel + delete a job, best-effort removing the artifact files."""
    removed = await jobs.delete_job(job_id, identity.id)
    if not removed:
        # Either not yours or already gone — same DELETE semantics as
        # the slides side (return 404 only when the row truly never
        # existed for this user).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

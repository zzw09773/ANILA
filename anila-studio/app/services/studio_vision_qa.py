"""Studio vision-QA loop (Step 8 of the flowchart).

Extracted from ``api/studio.py`` (god-module split). After a deck is rendered,
this module screenshots it and runs two complementary defect checks —
deterministic geometric QA (overflow / overlap, via ``geometric_qa``) and
gemma4 vision QA (semantic, via the csp proxy) — then asks the LLM to revise
the spec when critical defects are found.

Public surface (re-exported by ``app.api.studio`` for ``_run_pipeline``):
  * ``visual_qa`` — render-time geometric + vision defect sweep.
  * ``fix_spec_with_defects`` — one LLM revision pass given a defect list.

The per-slide helpers (``_capture_screenshots`` / ``_inspect_slide_visually``
/ ``_geometric_to_visual`` / ``_merge_defects``) are module-private.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
from fastapi import HTTPException

from app.schemas.studio import SlidesSpec, VisualDefect
from app.services.geometric_qa import GeometricDefect, run_geometric_qa
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.studio_config import (
    RENDERER_BASE_URL,
    SLIDES_LLM_MODEL,
    VISION_LLM_MODEL,
)
from app.services.studio_llm import call_llm_chat as _call_llm_chat

logger = logging.getLogger(__name__)


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


async def visual_qa(
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


async def fix_spec_with_defects(
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

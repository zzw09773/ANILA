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
import re

import httpx
from fastapi import HTTPException

from pydantic import ValidationError

from app.schemas.studio import Slide, SlidesSpec, VisualDefect
from app.services.geometric_qa import GeometricDefect, run_geometric_qa
from app.services.llm_json import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
)
from app.services.studio_config import (
    FIX_MAX_TOKENS,
    RENDERER_BASE_URL,
    SLIDES_LLM_MODEL,
    VISION_LLM_MODEL,
)
from app.services.studio_llm import call_llm_chat as _call_llm_chat

logger = logging.getLogger(__name__)


VISION_SYSTEM_PROMPT = (
    "你是簡報視覺品質檢查員。輸入是一張投影片的截圖，請只回 JSON："
    '{"defects": [{"severity": "critical|warning|info", "summary": "..."}]}。'
    "若沒有任何問題，回 {\"defects\": []}。"
    "critical 等級保留給「使用者一眼會發現的嚴重問題」："
    "文字溢出版面、文字與圖形重疊、低對比導致看不見、缺少必要內容。"
    "warning 用於可改善但不影響理解的問題。"
    "版面固定會有兩個小元素，**不是缺陷，不要回報**："
    "右下角的小數字是頁碼；左下角灰色小字「資料來源：…」是來源腳註。"
    "截圖解析度低，角落的小字可能看起來像亂碼，那是頁碼，不要當成不明字元回報。"
    "回應必須是、且只能是一個 JSON 物件，第一個字元 {、最後一個字元 }，"
    "不要 ```json 包裹，不要 thought/reasoning 前言。"
)

# Vision-model complaints that the live runs showed are the page number /
# footer misread at 96 dpi ("右下角出現不明亂碼字元「唓」"). Never critical.
_KNOWN_FALSE_POSITIVE_RE = re.compile(r"右下角|左下角|頁碼|亂碼|不明字元|不明的?字|不明符號")
_REAL_PROBLEM_RE = re.compile(r"溢出|超出|overflow|重疊|看不見|遮住")


def demote_known_false_positives(defects: list[VisualDefect]) -> list[VisualDefect]:
    out: list[VisualDefect] = []
    for d in defects:
        if (
            d.severity != "info"
            and _KNOWN_FALSE_POSITIVE_RE.search(d.summary)
            and not _REAL_PROBLEM_RE.search(d.summary)
        ):
            out.append(d.model_copy(update={"severity": "info"}))
        else:
            out.append(d)
    return out


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

    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
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


class DefectReport(list):
    """``list[VisualDefect]`` plus what the QA pass could NOT do.

    ``vision_skipped`` is a human-readable reason when the vision model did
    not inspect the slides (e.g. it rejects image input); the pipeline turns
    it into a JobStatus warning instead of failing the job.
    """

    vision_skipped: str | None = None
    # The per-slide PNGs the pass looked at (rendered order) — kept so the
    # pipeline can persist them as previews instead of throwing them away.
    screenshots: list[bytes] | None = None


def to_spec_indices(
    defects: list[VisualDefect], *, cover_prepended: bool,
) -> list[VisualDefect]:
    """Map renderer slide indices back onto ``spec.slides`` indices.

    The renderer prepends its own cover when ``spec.slides[0]`` is not a
    ``section_break``; screenshots and geometric QA index the rendered deck,
    while the fix prompt and JobStatus talk about spec slides. When a cover
    was prepended, rendered 0 has no spec counterpart (its defects are
    dropped — the cover is the renderer's, not the model's) and every other
    index shifts down by one.
    """
    if not cover_prepended:
        return list(defects)
    out: list[VisualDefect] = []
    for d in defects:
        if d.slide_index == 0:
            continue
        out.append(d.model_copy(update={"slide_index": d.slide_index - 1}))
    return out


async def visual_qa(
    bearer: str,
    pptx_path: str,
    *,
    pptx_bytes: bytes | None = None,
    kinds: list[str] | None = None,
    only_slides: set[int] | None = None,
) -> DefectReport:
    """Run geometric + vision QA on every slide of a rendered .pptx.

    Indices in the returned defects are RENDERED indices; the caller maps
    them with :func:`to_spec_indices`. ``only_slides`` (rendered indices)
    restricts the vision pass — the re-check after a fix only needs to look
    at the slides that were flagged, not the whole deck again.

    Geometric QA runs first. If it flags `critical` defects on a slide,
    we still run vision QA on the *other* slides (cheaper to short-circuit
    only the slides we already know are broken). Best-effort: any error
    in geometric QA yields empty defects and we fall back to vision-only.
    """
    # Geometric QA — deterministic, no vision tokens. Reads pptx bytes
    # straight from the renderer; if we weren't handed them, skip it.
    geom_defects: list[VisualDefect] = []
    critical_slides: set[int] = set()
    report = DefectReport()
    if pptx_bytes:
        try:
            raw = await run_geometric_qa(
                pptx_bytes, renderer_url=RENDERER_BASE_URL, kinds=kinds,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Geometric QA raised unexpectedly: %s", e)
            raw = []
        geom_defects = [_geometric_to_visual(g) for g in raw]
        critical_slides = {
            d.slide_index for d in geom_defects if d.severity == "critical"
        }

    pngs = await _capture_screenshots(pptx_path)
    report.screenshots = list(pngs)
    if not pngs:
        report.extend(geom_defects)
        report.vision_skipped = "渲染器沒有回傳投影片截圖"
        return report

    # Sequential per-slide: gemma4 backend is single-tenant; concurrent
    # requests can starve each other on the GPU. Cap parallelism at 2 to
    # halve wall-clock without overwhelming the model. Skip slides that
    # geometric QA already marked critical — the fix-pass will handle
    # them and burning vision tokens on a known-broken slide is wasteful.
    semaphore = asyncio.Semaphore(2)
    skipped_reason: list[str] = []

    async def _one(idx: int, b: bytes) -> list[VisualDefect]:
        if idx in critical_slides:
            return []
        if only_slides is not None and idx not in only_slides:
            return []
        if skipped_reason:
            return []  # the model already told us it will not look at images
        async with semaphore:
            try:
                return await _inspect_slide_visually(bearer, idx, b)
            except HTTPException as exc:
                # The vision model rejected the call (no image support,
                # gateway error…). Vision QA is best-effort: keep the
                # geometric findings, tell the caller, do not fail the job.
                if not skipped_reason:
                    skipped_reason.append(str(exc.detail)[:160])
                    logger.warning(
                        "Vision QA unavailable (slide %d): %s — skipping the "
                        "vision pass for this deck.", idx, exc.detail,
                    )
                return []

    results = await asyncio.gather(
        *(_one(i, b) for i, b in enumerate(pngs)),
        return_exceptions=False,
    )
    vision_flat: list[VisualDefect] = []
    for r in results:
        vision_flat.extend(r)
    vision_flat = demote_known_false_positives(vision_flat)
    report.extend(_merge_defects(geom_defects, vision_flat))
    if skipped_reason:
        report.vision_skipped = skipped_reason[0]
    return report


async def fix_spec_with_defects(
    bearer: str,
    current_spec: SlidesSpec,
    defects: list[VisualDefect],
) -> SlidesSpec:
    """Ask the LLM to revise ONLY the flagged slides.

    The old pass sent the whole spec and asked for the whole spec back; on
    the 2026-09-02 live run the model "fixed" one overflow by rewriting
    every slide into bullet lists (tables, stat callouts and processes all
    gone). Now the model sees just the flagged slides and returns
    ``{"changes": [{"slide_index", "slide"}]}``; everything else is kept
    byte-for-byte. Raises ``ValueError`` / ``ValidationError`` when nothing
    usable comes back, so the caller ships the pre-fix deck.
    """
    flagged = sorted({d.slide_index for d in defects if 0 <= d.slide_index < len(current_spec.slides)})
    if not flagged:
        raise ValueError("no defect points at an existing slide")
    defect_lines = "\n".join(
        f"- 投影片 #{d.slide_index}（{d.severity}）：{d.summary}"
        for d in defects if d.slide_index in flagged
    )
    slides_json = json.dumps(
        [{"slide_index": i, "slide": current_spec.slides[i].model_dump(mode="json", exclude_none=True)} for i in flagged],
        ensure_ascii=False, indent=2,
    )
    system = (
        "你是 ANILA LM 的簡報修訂助手。輸入是幾張被視覺檢查點名的投影片（JSON）與各自的缺陷。"
        "只修這幾張、只回這幾張；**不要改 layout_kind、不要改版型 payload 的結構**，"
        "只調整文字：縮短 bullet、拆句、刪重複、換掉 placeholder。"
        '輸出格式：{"changes": [{"slide_index": <原本的 slide_index>, "slide": {...完整的那一張...}}]}。'
        "修不了的頁就不要放進 changes。第一字 {、最後字 }、不可前言、不可代碼塊。"
    )
    user_msg = f"被點名的投影片：\n{slides_json}\n\n缺陷：\n{defect_lines}"
    raw = await _call_llm_chat(
        bearer, SLIDES_LLM_MODEL,
        [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        temperature=0.2, max_tokens=FIX_MAX_TOKENS,
    )
    parsed = _loads_lenient(_extract_json_object(raw))
    changes = parsed.get("changes") if isinstance(parsed, dict) else None
    if not isinstance(changes, list):
        raise ValueError("fix response has no changes list")
    new_slides = list(current_spec.slides)
    applied = 0
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        try:
            idx = int(ch.get("slide_index"))
        except (TypeError, ValueError):
            continue
        if idx not in flagged or not isinstance(ch.get("slide"), dict):
            continue
        try:
            new_slides[idx] = Slide.model_validate(ch["slide"])
        except ValidationError as exc:
            logger.warning("fix: change for slide %d rejected: %s", idx, str(exc)[:200])
            continue
        applied += 1
    if applied == 0:
        raise ValueError("fix response changed nothing usable")
    return SlidesSpec.model_validate(
        {**current_spec.model_dump(mode="json"), "slides": [s.model_dump(mode="json") for s in new_slides]}
    )

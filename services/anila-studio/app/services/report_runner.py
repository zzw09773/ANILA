"""Pipeline runner for Studio reports.

## Stages

    1. retrieve          csp_client.search_chunks → list[ChunkHit]
    2. outline           LLM (small budget): seed prompt + chunk excerpts
                         → JSON {title, tldr, sections: [{heading, key_points}]}
    3. draft             LLM (larger budget): for each section heading,
                         expand into content_markdown with [N] markers
                         pointing into references[]
    4. normalize         strip_inline_citations (visible text), strip_latex,
                         OpenCC s2twp on title / headings / content
    5. assemble          build ReportSpec(...)
    6. render            HTML + PDF + DOCX → {ARTIFACTS_DIR}/{job_id}.{ext}

## Design choice: two-stage LLM (outline → draft)

We picked **two-stage** over single-call for three reasons:

- **Prompt budget control.** A single call asking for "title + tldr + 4-6
  sections × full content + references" easily blows past 8K input tokens
  once chunks are appended, and gemma4 starts truncating output. Stage 1
  packs *all* chunk excerpts (≤2000 tokens) and emits a structured outline
  only. Stage 2 sees ONE section's slice at a time, with the rest of the
  outline as light context.
- **Failure isolation.** If the outline JSON is malformed we retry once
  with a "fix this JSON" prompt and bail to a degraded outline if that
  fails. Drafting any single section is allowed to fail individually —
  the section just falls back to a placeholder "本章節撰寫失敗" line so
  the rest of the report still renders.
- **Cost.** Two short prompts (outline ~1k input + 500 output; draft
  ~800 input × N + 1k output × N) costs less than one 10k+1k call,
  because the LLM's per-token cost rises super-linearly with very long
  contexts on shared infra.

## What runs sync vs async

Everything here is async because the only blocking call is csp_client
(HTTP) and pypandoc (subprocess, offloaded via asyncio.to_thread in the
renderer). Image fetching is NOT in this runner — Phase 1 reports are
text-only. A future stage can call ``csp_client.search_images`` and
inline images into the markdown.

## Cancellation

asyncio.CancelledError surfacing through this coroutine is caught by the
report_job_service wrapper (state="cancelled"). Renderer steps that
write large files do so atomically (tempfile + rename would be safer but
add complexity; current cut: assume the cancellation window is short
enough that orphaned partial files are not a problem — the next job
overwrites by ``{job_id}.{ext}`` naming).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.csp_client import (
    ChunkHit,
    CspClientError,
    proxy_chat_completions,
    search_chunks,
)
from app.config import settings
from app.generated_preamble import ERA_RULES, NATIONAL_TERMINOLOGY
from app.schemas.report import (
    JOB_STEP_DRAFTING,
    JOB_STEP_NORMALIZING,
    JOB_STEP_OUTLINING,
    JOB_STEP_RENDERING,
    JOB_STEP_RETRIEVING,
    GenerateReportRequest,
    ReportPreset,
    ReportReference,
    ReportSection,
    ReportSpec,
)
from app.services.report_job_service import ReportJobUpdater
from app.services.report_renderer import render_all_artifacts
from app.services.studio_text_normalizer import (
    _get_converter,
    strip_inline_citations,
    strip_latex,
)

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────


# Default model. The csp proxy resolves the model name against its
# registry; gemma4 is the production CJK-capable model. Same default as
# slides pipeline.
DEFAULT_LLM_MODEL = "gemma4"


# How many chunks of chunk-content to expose to the outline prompt. Each
# excerpt is truncated to ~CHUNK_EXCERPT_CHARS chars; the outline JSON
# emits headings + key_points only.
OUTLINE_CHUNK_EXCERPT_CHARS = 600

# Per-section drafting allowance. Each section sees its own slice of the
# chunk pool (top relevance by score). 4 chunks × 1500 chars ≈ 6k tokens.
DRAFT_CHUNKS_PER_SECTION = 4
DRAFT_CHUNK_EXCERPT_CHARS = 1500


# ── Preset voicing ────────────────────────────────────────────────────────


_PRESET_VOICE: dict[ReportPreset, str] = {
    ReportPreset.DEEP_TECH_REVIEW: (
        "你是一位資深的技術綜述作者，目標讀者是同領域的技術主管。"
        "用嚴謹、技術精確的繁體中文寫作，避免行銷語言，鼓勵引用具體數據、"
        "演算法名稱、論文/技術規格出處。"
    ),
    ReportPreset.KEY_SUMMARY: (
        "你是一位高階主管的幕僚，要產出一份適合 5 分鐘閱讀完的高密度摘要。"
        "用簡潔、有力的繁體中文，每個 section 控制在 2-3 段，"
        "聚焦在「決策需要知道什麼」。"
    ),
    ReportPreset.TEACHING_HANDOUT: (
        "你是一位資深的課程講師，要寫一份給學員的教學講義。"
        "用清楚、循序漸進的繁體中文，多舉具體例子，"
        "適當穿插「📚 範例」、「✏️ 練習」等教學提示。"
    ),
    ReportPreset.EXTERNAL_COMMS: (
        "你是一位企業外部溝通文件撰寫者，文件會直接給客戶/合作夥伴閱讀。"
        "用專業、清晰、不過度技術化的繁體中文，避免縮寫和內部術語，"
        "強調業務價值與整合方式。"
    ),
}


_PRESET_TITLE_HINT: dict[ReportPreset, str] = {
    ReportPreset.DEEP_TECH_REVIEW: "深度技術綜述",
    ReportPreset.KEY_SUMMARY: "重點摘要",
    ReportPreset.TEACHING_HANDOUT: "教學講義",
    ReportPreset.EXTERNAL_COMMS: "對外溝通文件",
}


def _compose_system_prompt(voice: str) -> str:
    """Persona voice + 平台共同前導的國家用語／紀年段（單一組裝點）。"""
    return "\n\n".join(
        (
            NATIONAL_TERMINOLOGY.strip(),
            ERA_RULES.strip(),
            voice.strip(),
        )
    )


# ── Helpers ───────────────────────────────────────────────────────────────


def _derive_seed_query(preset: ReportPreset, extra: str | None) -> str:
    """Build the retrieval seed query.

    The seed is a single string used to ANN-search the collection for
    relevant chunks. We compose it from preset intent + optional user
    extra so a user asking for a "教學講義 on LangChain agents" doesn't
    pull random chunks unrelated to the topic.
    """
    base = _PRESET_TITLE_HINT[preset]
    if extra and extra.strip():
        return f"{base} — {extra.strip()}"
    return base


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a possibly-noisy LLM reply.

    gemma4 sometimes emits a preamble ("好的，以下是...") before the
    actual JSON. We pluck the largest top-level brace pair. If the result
    doesn't parse, we let JSONDecodeError surface so the caller can retry
    or fall back.
    """
    m = _JSON_RE.search(text)
    if not m:
        raise json.JSONDecodeError("no JSON object in LLM output", text, 0)
    return json.loads(m.group(0))


def _safe_truncate(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars without breaking too obviously."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _convert_zh(text: str | None, keep_citations: bool = False) -> str | None:
    """Normalizer pipeline for a single string.

    - strip citation markers from visible text (kept for the `references`
      excerpt and never for headings/content because those are also
      visible),
    - strip LaTeX,
    - OpenCC s2twp.

    We deliberately do NOT use studio_text_normalizer._convert directly
    because that helper is module-private; instead we replicate its
    short body here so the report path doesn't depend on a private
    contract.
    """
    if text is None or text == "":
        return text
    if not keep_citations:
        text = strip_inline_citations(text) or text
    text = strip_latex(text) or text
    return _get_converter().convert(text)


def _build_references(chunks: list[ChunkHit]) -> list[ReportReference]:
    """Map chunks → numbered references.

    Deterministic order: chunks come in score-descending from csp; we
    keep that order so [1] is the most relevant chunk regardless of
    which section ends up citing it.
    """
    refs: list[ReportReference] = []
    for i, c in enumerate(chunks, start=1):
        excerpt = _safe_truncate(c.content.strip().replace("\n", " "), 200)
        refs.append(
            ReportReference(
                n=i,
                filename=c.filename,
                chunk_id=c.chunk_id,
                excerpt=excerpt,
            )
        )
    return refs


# ── LLM stage A: outline ─────────────────────────────────────────────────


_OUTLINE_SCHEMA_HINT = """
請輸出一個 JSON object，欄位定義如下（除這些之外不要加任何欄位）：

{
  "title":  "string — 整份報告的標題，繁體中文，30-60 字",
  "tldr":   "string — 100-200 字的摘要，把主結論講清楚",
  "sections": [
    {
      "heading":    "string — 該 section 的標題，繁體中文",
      "key_points": ["string", "..."]    // 該 section 要展開的關鍵點，2-5 條
    }
  ]
}

務必輸出嚴格合法的 JSON。請輸出 4-6 個 sections。
"""


async def _llm_outline(
    *,
    request: GenerateReportRequest,
    chunks: list[ChunkHit],
    bearer: str,
) -> dict[str, Any]:
    """Stage A: ask the LLM for a structured outline.

    We hand it a compact view of the chunk pool (filename + excerpt) so
    it can decide section boundaries informed by what's actually in the
    collection. The schema is constrained but deliberately loose on
    section count (4-6) so the model can match the material.
    """
    voice = _compose_system_prompt(_PRESET_VOICE[request.preset])
    seed = _derive_seed_query(request.preset, request.extra_instructions)

    chunk_block_lines: list[str] = []
    for i, c in enumerate(chunks, start=1):
        excerpt = _safe_truncate(
            c.content.strip().replace("\n", " "), OUTLINE_CHUNK_EXCERPT_CHARS
        )
        chunk_block_lines.append(
            f"[{i}] ({c.filename}) {excerpt}"
        )
    chunk_block = "\n".join(chunk_block_lines)

    user_prompt = f"""你正在為一份「{seed}」格式的深度報告做大綱規劃。

以下是檢索到的素材（每段已標號為 [N]，後續正文撰寫會用同樣編號做引用）：

{chunk_block}

請依素材內容規劃一份適合該 preset 的報告大綱。{_OUTLINE_SCHEMA_HINT}
"""

    messages = [
        {"role": "system", "content": voice},
        {"role": "user", "content": user_prompt},
    ]

    response = await proxy_chat_completions(
        model=DEFAULT_LLM_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=2000,
        response_format={"type": "json_object"},
        bearer=bearer,
    )
    content = _extract_choice_content(response)
    return _extract_json(content)


def _extract_choice_content(response: dict[str, Any]) -> str:
    """Pull the first choice's message content from an OpenAI-shaped reply.

    Defensive: csp's proxy normalises this but we keep the lookup tight
    so a malformed upstream surfaces as a clear KeyError instead of a
    confusing AttributeError downstream.
    """
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response choice has no content")
    return content


# ── LLM stage B: per-section drafting ────────────────────────────────────


async def _llm_draft_section(
    *,
    preset: ReportPreset,
    heading: str,
    key_points: list[str],
    chunks_for_section: list[ChunkHit],
    references: list[ReportReference],
    bearer: str,
) -> str:
    """Stage B (per section): expand a section's outline into markdown.

    The section sees ONLY its slice of the chunks (top N by relevance)
    plus the references map so it knows which numbers to use as inline
    citations. The model is instructed to use [N] markers ONLY for the
    chunks present in its slice; this avoids spurious citations to
    unrelated references.
    """
    voice = _compose_system_prompt(_PRESET_VOICE[preset])

    chunk_lines: list[str] = []
    ref_lookup: dict[int, ReportReference] = {r.chunk_id: r for r in references if r.chunk_id is not None}
    available_ns: list[int] = []
    for c in chunks_for_section:
        ref = ref_lookup.get(c.chunk_id)
        n = ref.n if ref is not None else -1
        if n > 0:
            available_ns.append(n)
        excerpt = _safe_truncate(
            c.content.strip().replace("\n", " "), DRAFT_CHUNK_EXCERPT_CHARS
        )
        chunk_lines.append(f"[{n}] ({c.filename}) {excerpt}")
    chunk_block = "\n".join(chunk_lines)
    citations_hint = (
        ", ".join(f"[{n}]" for n in available_ns) if available_ns else "（無）"
    )

    key_points_block = "\n".join(f"- {p}" for p in key_points)

    user_prompt = f"""請撰寫下面這個 section 的正文內容。

Section 標題：{heading}

要展開的關鍵點：
{key_points_block}

可用的素材片段（請使用對應 [N] 編號做引用，僅限 {citations_hint} 這些編號）：
{chunk_block}

撰寫規範：
- 輸出純 markdown，不要 wrap 在 code block 內。
- 不要再次寫 section 標題（外層已有 ## heading）；直接寫正文。
- 段落清楚，必要時可使用 ### 子標題、bullet list、表格。
- 引用素材處請用 [N] 行內標記（例如：「...由 RAG 流程處理 [3]」）。
- 不要捏造引用編號，只能用上面列出的可用編號。
- 不需要在最後重複列出參考文獻，那由外層流程統一處理。
"""

    messages = [
        {"role": "system", "content": voice},
        {"role": "user", "content": user_prompt},
    ]

    response = await proxy_chat_completions(
        model=DEFAULT_LLM_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=1500,
        bearer=bearer,
    )
    return _extract_choice_content(response).strip()


# ── Assembly ──────────────────────────────────────────────────────────────


def _select_chunks_for_section(
    *, key_points: list[str], chunks: list[ChunkHit], n: int
) -> list[ChunkHit]:
    """Pick top-N chunks for a section.

    First cut uses *score* ordering only — every section sees the same
    top-N by ANN score. A future enhancement can score each section's
    key_points against chunks with a cheap embedding similarity, but for
    Phase 1 the global top-N is sufficient and dramatically simpler.
    """
    return chunks[:n]


def _normalize_section(section: ReportSection) -> ReportSection:
    """Apply OpenCC + strip_* recursively."""
    return section.model_copy(
        update={
            "heading": _convert_zh(section.heading) or section.heading,
            "content_markdown": _convert_zh(section.content_markdown) or "",
            "subsections": [_normalize_section(s) for s in section.subsections],
        }
    )


def _normalize_spec(spec: ReportSpec) -> ReportSpec:
    """Run all visible strings through citation strip + LaTeX strip + OpenCC."""
    return spec.model_copy(
        update={
            "title": _convert_zh(spec.title) or spec.title,
            "tldr": _convert_zh(spec.tldr) or spec.tldr,
            "sections": [_normalize_section(s) for s in spec.sections],
        }
    )


# ── Top-level runner ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RunnerDeps:
    """Optional injection seam for tests.

    Tests pass concrete fakes for ``search_chunks`` / ``proxy_chat_completions``
    via ``monkeypatch.setattr`` at the module level today; this dataclass
    is reserved for future DI if we move to constructor-based injection.
    """


async def run_report_pipeline(
    *,
    request: GenerateReportRequest,
    bearer: str,
    updater: ReportJobUpdater,
    artifacts_dir: Path | None = None,
) -> None:
    """End-to-end pipeline: retrieve → outline → draft → normalize → render.

    Writes artifacts to disk under ``artifacts_dir`` (defaults to
    ``settings.ARTIFACTS_DIR``) and calls ``updater.mark_done`` on
    success. Any exception propagates to ``report_job_service`` which
    flips the job to "failed".
    """
    target_dir = artifacts_dir or Path(settings.ARTIFACTS_DIR)
    job_id = updater.job_id

    # ── 1. retrieve ─────────────────────────────────────────────────────
    await updater.set(step=JOB_STEP_RETRIEVING)
    seed_query = _derive_seed_query(request.preset, request.extra_instructions)
    try:
        chunks = await search_chunks(
            request.collection_id,
            seed_query,
            top_k=request.top_k,
            document_ids=request.document_ids,
            bearer=bearer,
        )
    except CspClientError as exc:
        raise RuntimeError(f"檢索失敗: {exc}") from exc

    # 過濾掉太短的 chunks(markdown horizontal rule `---` / 空段落 /
    # 標題殘餘等 chunking artifact)。production 觀察 lun collection 真實
    # 華航財報 chunks 被 markdown `---` chunks 壓在後面,filter 後乾淨。
    chunks = [c for c in chunks if len((c.content or "").strip()) >= 50]

    if not chunks:
        raise RuntimeError(
            "在指定的 collection 中找不到相關內容，無法產生報告。"
            "請確認 collection 已成功索引,或調整 extra_instructions。"
        )

    references = _build_references(chunks)
    await updater.set(references_count=len(references))

    # ── 2. outline ──────────────────────────────────────────────────────
    await updater.set(step=JOB_STEP_OUTLINING)
    try:
        outline = await _llm_outline(
            request=request, chunks=chunks, bearer=bearer
        )
    except (json.JSONDecodeError, ValueError, CspClientError) as exc:
        raise RuntimeError(f"大綱生成失敗: {exc}") from exc

    outline_title = str(outline.get("title") or _PRESET_TITLE_HINT[request.preset])
    outline_tldr = str(outline.get("tldr") or "")
    outline_sections = outline.get("sections") or []
    if not isinstance(outline_sections, list) or not outline_sections:
        raise RuntimeError("大綱生成失敗: sections 為空")

    await updater.set(title=outline_title, sections_count=len(outline_sections))

    # ── 3. draft each section ──────────────────────────────────────────
    await updater.set(step=JOB_STEP_DRAFTING)
    drafted_sections: list[ReportSection] = []
    for idx, outline_sec in enumerate(outline_sections):
        if not isinstance(outline_sec, dict):
            continue
        heading = str(outline_sec.get("heading") or f"Section {idx + 1}")
        key_points_raw = outline_sec.get("key_points") or []
        if not isinstance(key_points_raw, list):
            key_points_raw = [str(key_points_raw)]
        key_points = [str(kp) for kp in key_points_raw][:6]

        section_chunks = _select_chunks_for_section(
            key_points=key_points,
            chunks=chunks,
            n=DRAFT_CHUNKS_PER_SECTION,
        )
        try:
            content = await _llm_draft_section(
                preset=request.preset,
                heading=heading,
                key_points=key_points,
                chunks_for_section=section_chunks,
                references=references,
                bearer=bearer,
            )
        except (ValueError, CspClientError) as exc:
            # Section failure is per-section graceful; the rest of the
            # report still renders. We mark the section with a placeholder
            # so the reader sees something explicit instead of an empty
            # body that looks like a layout bug.
            logger.warning(
                "report runner: section %d draft failed: %s", idx, exc
            )
            content = f"_本章節撰寫失敗：{exc}_"

        drafted_sections.append(
            ReportSection(
                heading=heading,
                content_markdown=content,
                subsections=[],
            )
        )

    if not drafted_sections:
        raise RuntimeError("所有章節撰寫均失敗，無法產生報告")

    # ── 4. assemble + normalize ────────────────────────────────────────
    await updater.set(step=JOB_STEP_NORMALIZING)
    spec_pre = ReportSpec(
        title=outline_title,
        preset=request.preset,
        tldr=outline_tldr or "（無摘要）",
        sections=drafted_sections,
        references=references,
    )
    spec = _normalize_spec(spec_pre)

    # ── 5. render ──────────────────────────────────────────────────────
    await updater.set(step=JOB_STEP_RENDERING)
    paths = await render_all_artifacts(spec, target_dir, job_id)

    download_urls = {
        fmt: f"/api/reports/jobs/{job_id}/download/{fmt}"
        for fmt in ("html", "pdf", "docx")
    }

    # Verify each file actually landed — defensive guard against silent
    # render bugs in subprocesses.
    for fmt, path in paths.items():
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"渲染失敗：{fmt} 檔案未生成 ({path})")

    await updater.mark_done(spec=spec, download_urls=download_urls)

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
from sqlalchemy.orm import Session

from anila_core.storage.adapters.pgvector_store import (
    CollectionScopedPgVectorStore,
)

from app.api.ingestion.collections import _require_collection_access
from app.api.ingestion.search import _embed_query
from app.database import get_db, SessionLocal
from app.models.ingestion import IngestionDocument
from app.models.model_registry import ModelRegistry
from app.models.user import User
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
from app.services.auth_service import get_current_user
from app.services.geometric_qa import GeometricDefect, run_geometric_qa
from app.services.ingestion_pool import get_pool
from app.services.proxy_service import proxy_request
from app.services.studio_text_normalizer import normalize_spec

if TYPE_CHECKING:
    from app.services.flux_image_provider import FluxImageProvider

router = APIRouter(prefix="/api/studio", tags=["Studio / Slides"])
logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Retrieval depth for slide deck generation. Higher than chat (top-5) because
# Studio synthesises across the whole deck, not a single Q&A turn.
# How many image hits to surface alongside the chunks. Pulling fewer
# than chunks because (a) we have ~10× fewer images than chunks per
# document, (b) the LLM only picks 1-2 per deck, (c) prompt budget
# tightens fast when each image carries a 200-char caption.
STUDIO_IMAGE_TOP_K = 6
STUDIO_IMAGE_MIN_SCORE = 0.25


# Retrieval depth was originally 12 / 800 chars / total ~9.6 KB context.
# Bumped after the carbon-thesis case where a 74-page paper produced
# 100-char/slide bullets — symptom of the LLM not having enough context
# to write specifically. New defaults give ~30 KB context, which is well
# under Gemma 4's 256K window but enough to surface every section of a
# typical paper. Going higher costs prompt tokens linearly with little
# extra value (top-20 hits already cover the deck's narrative space).
STUDIO_TOP_K = 20
STUDIO_MIN_SCORE = 0.25
STUDIO_CONTENT_LIMIT_CHARS = 1500

# How many times to retry on Pydantic validation failure. One re-roll is
# usually enough; if the LLM emits two malformed responses in a row, the
# pipeline gives up and 422s — the user can retry the request entirely.
SCHEMA_CORRECTION_PASSES = 1

# How many vision-QA → fix → re-render cycles. Keep at 1; more iterations
# tend to produce diminishing returns and eat seconds of wall-clock.
VISUAL_QA_PASSES = 1

# Renderer service — same docker network, same compose stack.
RENDERER_BASE_URL = "http://pptx-renderer:7100"

# Default LLM for slide generation. Could be made overridable per-request
# but the current product is "Studio just works" — admin-configurable
# default is enough.
SLIDES_LLM_MODEL = "gemma4"
VISION_LLM_MODEL = "gemma4"

# Stage 2 (Layer C): how many extra times to regenerate a slide's image
# when every candidate fails the quality gate. attempt 0 + MAX_RETRIES more.
FLUX_GATE_MAX_RETRIES = 3
# Candidates generated per attempt (spec 5: N=2).
FLUX_GATE_NUM_CANDIDATES = 2
# Seed stride between retry attempts so each attempt explores a different
# region of latent space (attempt k uses base_seed + k*1024).
FLUX_GATE_SEED_STRIDE = 1024
# Stage 4: hard ceiling on generated images per deck. Beyond this, remaining
# illustration slides take the theme/text fallback instead of spending GPU.
# Protects against runaway latency on decks with many section breaks.
MAX_GENERATED_IMAGES_PER_DECK = 15


# ── JSON extraction (mirrors ANILALM's frontend extractJsonObject) ─────────


_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE,
)


def _extract_json_object(raw: str) -> str:
    """Slice the *last* balanced JSON object out of a noisy LLM response.

    Why "last balanced" and not "first { to last }":
      - gemma4 / qwen / oss models often emit a "thought" preamble that
        contains literal JSON examples like ``{"title": "..."}`` — the
        naive ``find('{')`` lands inside that example, the naive
        ``rfind('}')`` lands at the end of the real answer, and the slice
        glues two unrelated regions together.
      - Walking braces from the end finds the FINAL top-level ``{...}``
        which is virtually always the actual answer (LLMs put their
        decision at the end, after reasoning).

    Implementation: skip ``<think>``/```` ``` `` blocks first to remove
    the most common forms of structured noise, then scan from the right
    counting brace nesting until we hit depth 0.
    """
    de_thought = _THINK_BLOCK_RE.sub("", raw)
    no_fences = (
        de_thought.replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

    end = no_fences.rfind("}")
    if end == -1:
        raise ValueError(
            f"Model response contained no closing brace. First 80: "
            f"{raw[:80]!r}".replace("\n", "⏎")
        )

    # Walk leftward from the closing brace, counting nesting. We respect
    # JSON string delimiters so braces inside `"..."` don't fool the
    # depth counter. Escape sequences (\\, \") are handled with a
    # one-position lookahead.
    depth = 0
    in_string = False
    i = end
    while i >= 0:
        ch = no_fences[i]
        if in_string:
            if ch == '"' and (i == 0 or no_fences[i - 1] != "\\"):
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    return no_fences[i : end + 1]
        i -= 1
    raise ValueError(
        f"Model response had unbalanced braces. First 80: "
        f"{raw[:80]!r}".replace("\n", "⏎")
    )


def _loads_lenient(text: str) -> Any:
    """``json.loads`` plus a one-shot single-quote-to-double-quote repair.

    gemma4 (and friends) sometimes emit Python-dict-style output:
        {'title': "x", 'slides': []}
    which strict ``json.loads`` rejects (line 1 col 2 error). The repair
    only flips quote characters that look like JSON delimiters
    (preceded by ``[``, ``{``, ``,``, ``:`` or whitespace) so apostrophes
    inside values aren't accidentally converted. If even that fails,
    we let json.JSONDecodeError propagate so the correction pass can
    re-prompt.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Replace `'` only at delimiter positions. Limited regex pass:
    # opening `'` after [{,:\s, closing `'` before ]},:\s.
    repaired = re.sub(r"(?<=[\[\{,:\s])'", '"', text)
    repaired = re.sub(r"'(?=[\]\},:\s]|$)", '"', repaired)
    return json.loads(repaired)


# ── Step 3: retrieval ─────────────────────────────────────────────────────


async def _retrieve_chunks(
    db: Session,
    user: User,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant chunks for the seed_query, with filename joined in.

    Returns a list of dicts (not the full SearchHit objects from
    anila_core) so the prompt-building code stays decoupled from the
    storage layer's representation.
    """
    coll = _require_collection_access(db, user, collection_id)
    if coll.status != "active":
        # Studio over an archived collection is an unusual ask; treat as
        # zero hits and let the prompt fall through to "no context" mode.
        return []

    q_vec = await _embed_query(
        db, user, coll.embedding_model, coll.embedding_dim, seed_query,
    )

    pool = get_pool()
    store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
    hits = await store.similarity_search(
        query_embedding=q_vec,
        top_k=STUDIO_TOP_K,
        min_score=STUDIO_MIN_SCORE,
    )
    if not hits:
        return []

    doc_ids = {h.chunk.document_id for h in hits}
    rows = (
        db.query(IngestionDocument.id, IngestionDocument.filename)
        .filter(IngestionDocument.id.in_(doc_ids))
        .all()
    )
    filenames = {r.id: r.filename for r in rows}

    return _build_chunk_dicts(hits, filenames)


def _build_chunk_dicts(hits, filenames):  # noqa: ANN001 — internal
    return [
        {
            "filename": filenames.get(h.chunk.document_id, "<unknown>"),
            "chunk_key": h.chunk.chunk_key,
            "content": h.chunk.content[:STUDIO_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
    ]


async def _retrieve_images(
    db: Session,
    user: User,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant ingestion_images rows for the deck topic.

    Phase 5. Mirrors ``_retrieve_chunks`` but searches the
    ``ingestion_images`` vector index instead of ``document_chunks``.
    Returns a list of dicts the prompt builder can splat into the
    "可用圖" section, plus the renderer's CSP-side helper can hydrate
    by ``image_id`` to inline the actual PNG bytes.

    Empty list when:
      * collection has no images at all (text-only knowledge base);
      * embedder returned an empty vector;
      * pgvector match scores are all below threshold.
    """
    coll = _require_collection_access(db, user, collection_id)
    if coll.status != "active":
        return []

    q_vec = await _embed_query(
        db, user, coll.embedding_model, coll.embedding_dim, seed_query,
    )
    if not q_vec:
        return []

    pool = get_pool()
    # Wrap with HalfVector — the same codec PgPool registers on every
    # connection. Passing a Python string + ::halfvec cast fails
    # because halfvec's text-input parser misreads the leading `[`
    # ("could not convert string to float"). HalfVector ships the
    # right binary wire format directly.
    from pgvector import HalfVector

    q_value = HalfVector(q_vec)

    # halfvec uses cosine distance; pgvector returns 0 = identical, so
    # similarity = 1 - distance. Filter on distance < (1 - min_score).
    max_dist = 1.0 - STUDIO_IMAGE_MIN_SCORE
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                i.image_id,
                i.document_id,
                i.page,
                i.storage_path,
                i.mime,
                i.caption,
                d.filename,
                (i.embedding <=> $2) AS dist
            FROM ingestion_images i
            JOIN ingestion_documents d ON d.id = i.document_id
            WHERE i.collection_id = $1
              AND i.embedding IS NOT NULL
              AND (i.embedding <=> $2) < $3
            ORDER BY i.embedding <=> $2
            LIMIT $4
            """,
            collection_id, q_value, max_dist, STUDIO_IMAGE_TOP_K,
        )

    return [
        {
            "image_id": r["image_id"],
            "document_id": r["document_id"],
            "page": r["page"],
            "storage_path": r["storage_path"],
            "mime": r["mime"],
            "caption": (r["caption"] or "").strip(),
            "filename": r["filename"],
            "score": float(1.0 - r["dist"]),
        }
        for r in rows
    ]


# ── Step 4-5: LLM call helpers ────────────────────────────────────────────


# Preset name → (count hint, min slides). The frontend's CommandModal
# shows these ranges as hints in the picker; without mapping them on
# the backend the prompt stays at "8-12" regardless of preset, which
# is why "經典報告結構" decks always came out at 10 instead of the
# advertised 12-15. min_slides drives the section_break-frequency rule
# below: a 5-slide Lightning Talk shouldn't be forced to insert a
# mid-deck section break.
_PRESET_COUNT: dict[str, tuple[str, int]] = {
    "經典報告結構":     ("12-15 張投影片", 12),
    "Lightning Talk":   ("5 張投影片，重點濃縮、視覺優先", 5),
    "教學投影片":       ("8-12 張投影片", 8),
}


def _count_hint(preset: str) -> tuple[str, int]:
    """Resolve a preset name to (human range, min count). Falls back to
    8-12 / min=8 for unknown / extra_instructions-only flows."""
    return _PRESET_COUNT.get(preset.strip(), ("8-12 張投影片", 8))


def _build_generation_prompt(
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
    images: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Compose (system, user) prompts for the slide-deck LLM call.

    Phase 3 expands the prompt with:
      * theme selection (5 options, tone-based; palette deprecated)
      * per-slide layout_kind (6 variants)
      * icon_rows.concept whitelist (80+ keywords grouped by domain;
        the LLM is asked to pick a domain first, then a concept from
        that domain — see Phase 6 Fix 4)

    The hard rule we communicate to the LLM is **bullets[] is always
    required** even when a non-standard layout_kind is chosen, because
    the renderer falls back to standard rendering of bullets if the
    layout-specific payload is malformed. This means the LLM can
    aspirationally choose a fancy layout AND still ship a usable slide
    if the layout-specific fields don't pan out.
    """
    count_hint, min_slides = _count_hint(preset)
    system = "\n".join(
        [
            "You are a JSON-only slide-deck generator. Output is parsed",
            "by a strict JSON parser, NOT by a human.",
            "",
            "Output rules (any violation = automatic rejection):",
            '- The very first character of your response MUST be "{".',
            '- The very last character of your response MUST be "}".',
            "- Do NOT include the word 'thought', 'reasoning', 'analysis',",
            "  or any commentary before or after the JSON.",
            "- Do NOT wrap in ```json or ``` code fences.",
            "- Use straight double quotes only — never single quotes ', ",
            "  curly quotes “”, or fullwidth 「」 for keys and string values.",
            "",
            "── 頂層欄位 ──",
            'Required: title (string), slides (list).',
            'Required: theme — 依文件 tone 而非主題類別挑選：',
            '  "corporate_navy"   嚴謹的技術／業務報告；給同事或主管看的工作產出（預設）',
            '  "academic_paper"   研究發表、論文摘要、學術會議；多量化與引用',
            '  "warm_journal"     第一人稱學習心得、回顧、softer 反思內容',
            '  "executive_brief"  給高層的 briefing、結論導向、極簡、≤ 10 張',
            '  "startup_pitch"    對外發表、產品介紹、需要視覺衝擊與情緒煽動',
            '',
            '選擇依據（在 chunks_text 中尋找這些 tone 訊號）：',
            '  - 第一人稱主觀詞（我、我的、我們、心得、反思、學到、感受）',
            '    → warm_journal',
            '  - 量化結果（百分比、N=...、F1、p-value）+ 方法論 + 引用',
            '    → academic_paper',
            '  - 「問題 / 解法 / 價值」結構 + 中性語氣 + 技術細節',
            '    → corporate_navy',
            '  - 強 call-to-action、願景語言、產品名稱反覆出現',
            '    → startup_pitch',
            '  - 只有結論沒有過程、總頁數 ≤ 10、給 C-level 看',
            '    → executive_brief',
            '訊號衝突時取最強的；無明確訊號用 corporate_navy。',
            '（注意：若 title 含「心得／反思／論文／募資」等明確 framing 詞，'
            '後端會強制 override；你可不必額外處理。）',
            '整份簡報只能挑一個 theme；不要在 slides 內切換。',
            '',
            '（舊欄位名 palette 仍接受但已 deprecated，請用 theme。）',
            "",
            "── 每張投影片欄位 ──",
            'Required: title, bullets (1-6 items), speaker_notes',
            'Required: layout_kind — 從以下挑一個：',
            '  "standard"          一般內容頁（最常用，沒事就用這個）',
            '  "section_break"     章節過渡頁；title 是章節名，bullets 用 1-2 句副標',
            '  "stat_callout"      強調單一數字／百分比；要附 stat 物件',
            '  "quote"             引用名言或客戶證言；要附 quote 物件',
            '  "two_column"        對照／互補（before/after, pros/cons）；要附 columns',
            '  "icon_rows"         3-5 個並列要點，每個有 icon；要附 icon_rows',
            "",
            "Layout-specific 物件（以下是欄位形狀，**不要把這些範例值複製到輸出**）：",
            '  stat 形狀：{"value": "47%", "label": "...", "supporting": "..."',
            '              , "baseline": "30%", "baseline_label": "..."}',
            '             value 是大字數值；**supporting 必填 20-100 字脈絡**；',
            '             baseline / baseline_label 為選填對照基準（有就觸發左右比較版型）。',
            '  quote 形狀：{"text": "...", "attribution": "..."}',
            '             attribution 為選填來源。',
            '  columns 形狀：[{"heading": "...", "bullets": ["...", "...", "..."]},',
            '                 {"heading": "...", "bullets": ["...", "...", "..."]}]',
            '             固定 2 個元素的陣列；多於 2 會被忽略、少於 2 會降級為 standard。',
            '             **每欄 bullets 2-3 條**（schema 最低 2 條；湊不到 2 條的對照結構,',
            '             改用 icon_rows，保留並列感、視覺更輕）。',
            '  icon_rows 形狀：[{"concept": "...", "heading": "...", "description": "..."}]',
            '             3-5 個元素；concept 必須來自下方白名單。',
            "",
            "重要：bullets 任何 layout 都要填（renderer 在 layout-specific 欄位",
            "缺漏時會回退用 bullets 渲染，不要省）。",
            "",
            "── icon_rows.concept 必須從以下白名單挑（未列出的會 fallback",
            "   為「不畫 icon」，所以不要自創；先想 domain，再從該 domain 挑）──",
            "[generic 資料/運算] data_storage data_pipeline dataset",
            "                automation integration deployment",
            "[generic 人/角色]   user team customer",
            "[generic 溝通]     chat email notification broadcast",
            "[generic 分析/結果] insight metrics comparison search",
            "[generic 時間]     schedule deadline history",
            "[generic 品質/安全] security validation error success achievement",
            "[generic 系統]     settings server cloud network",
            "[generic 文件/學習] document book learning",
            "[industrial 工業/製造] machine factory sensor defect",
            "                  quality_control calibration anomaly",
            "                  production_line inspection yield_rate",
            "[ml_ai 機器學習]   model training inference embedding",
            "                  classification regression overfitting",
            "                  generalization feature_extraction imbalance",
            "                  fine_tuning agent reasoning retrieval",
            "                  prompt evaluation prediction",
            "[system_arch 架構] supervisor worker orchestration",
            "                  hierarchy vertical_split fanout",
            "                  pipeline_stage module",
            "[process 流程]    perception cognition action step_one",
            "                  alert iteration decision monitoring",
            "[outcome 結果]    improvement reduction breakthrough limitation",
            "                  cost_saving risk",
            "",
            "icon 規則：",
            "- **先選 domain，再從 domain 內挑 concept**：技術內容（ML/工業）",
            "  從 ml_ai / industrial / system_arch / process / outcome 挑；",
            "  一般商業/通用內容才從 generic 挑。",
            '- 同一張 icon_rows 的 concept 抽象層級要一致（全部「功能」或全部',
            "  「角色」之類），不要混。最好同 domain 內挑。",
            "- 不要硬套陳腔：success≠創新、network≠成長、achievement≠任何進步；",
            "  挑該行真正在表達的概念。",
            "",
            "── 設計心法（重要：這幾條會決定簡報專業感） ──",
            "1. **Less is more**：能少就少。寧可 3 個 bullet 寫得清楚，不要 6 個 bullet",
            "   每個 1 行勉強塞滿。每個元素都要有它存在的理由。",
            "2. **不要 data slop**：不要硬塞數字、不要為了「看起來有資訊量」而捏",
            "   進百分比或統計。數據只在「真的關鍵」時才放，這時改用 stat_callout。",
            "3. **整份簡報要有節奏**：用 section_break 切章節（每 3-5 張用一張過渡），",
            "   用 stat_callout / quote / icon_rows 在文字頁之間製造對比。**全部用",
            "   standard 是 AI slop 的標誌**。",
            "4. **layout 服務於內容、不為花俏而花俏**：選 stat_callout 因為這個數字",
            "   是這張的核心訊息；選 two_column 因為內容真的天然有對照關係；不要為了",
            "   「我用過這幾個 layout 顯得很努力」而硬塞。",
            "5. **承諾或從簡**（commit fully or keep simple）：要花俏就整份花俏；",
            "   要簡潔就整份簡潔。一張花俏配一張無聊是最差的配對。",
            "",
            "── 最重要的硬規則（違反 = deck 不合格） ──",
            f"**規則 0 / 投影片數量**：本次 preset 要求 **{count_hint}**。"
            f"少於 {min_slides} 張視為違反規則，請務必達到下限；"
            f"上限可彈性放寬以容納所有重點。",
            "**規則 1 / 第一張投影片必須是 section_break**：以簡報主題作為 title，",
            "  bullets 第 0 條寫一句副標說明。這是整份 deck 的封面，沒有它整份簡報",
            "  讀起來像流水帳。**不要把第一張做成 standard layout**，直接 layout_kind",
            "  填 'section_break' 即可。**這是規則第 1 條，不是建議**。",
            (
                "**規則 2 / 至少再有 1 張 section_break**：放在簡報三分之一或一半處"
                "作為章節分隔（例：「方法」、「實驗結果」、「結論」）。"
                "沒有章節隔段的長簡報是 AI slop 的標誌。"
            ) if min_slides >= 8 else (
                "**規則 2 / Lightning Talk 不需中段 section_break**：5 張的短簡報"
                "已被首張封面 + 內容流自然分節，不要硬塞額外 section_break。"
            ),
            "**規則 3 / standard 不可超過 60%**：技術內容穿插 icon_rows，"
            "章節穿插 section_break，數字穿插 stat_callout。",
            "**規則 4 / 數據必須有 stat_callout 至少 1 張**：若下方 chunks 出現",
            "  **任何百分比、實驗數值、KPI、提升幅度、F1/Recall/Accuracy 數字、",
            "  樣本數 N=...、誤差降幅** 之類，**必須**挑最關鍵的那一個做 stat_callout，",
            "  把該數字大字呈現。例：「MAPE 降低 88.73%」、「F1-score 0.92」、",
            "  「N=10,000」。**沒有 stat_callout 的數據型 deck = 視覺陽春**。",
            "**規則 5 / 對照型內容必須 two_column**：若內容有「A vs B」",
            "  （例：原始 vs 融合、本研究 vs 既有方法、有無 data augmentation、",
            "  Cross-machine 之間比較），用 1 張 two_column 拆成兩欄。",
            "**規則 6 / 若可用圖清單非空，必須至少 1 張 image_focus**：把「相關性",
            "  最高的那張」做 image_focus（layout_kind='image_focus' + 設 image_ref）。",
            "  論文 / 技術文件的圖（架構圖、實驗結果圖）幾乎都比文字描述更有說服力。",
            "  **後備規則 / 即時生成（Studio Fix 2 拆兩種）**：若「可用圖」清單為空、",
            "  或全部都不夠相關，但該 slide 主題明顯需要視覺輔助，依內容選一種模式：",
            "    (A) 情境插畫、無文字 → image_kind='illustration' + image_prompt",
            "        （英文 50-500 字，主體/場景/構圖/風格），走 FLUX。",
            "    (B) 含 label 的圖示（架構/流程/ER）→ image_kind='diagram' + diagram_dot",
            "        （Graphviz DOT，最多 3000 字），走 graphviz。**FLUX 畫不出可讀文字**。",
            "  **每張 slide 只能設 image_ref / illustration / diagram 其一，三者互斥**。",
            "",
            "── 引用「圖片描述」段落（這是 deck 變具體的關鍵） ──",
            "下方檢索段落中可能含「圖片描述：...」的段落 — 那是文件原圖的",
            "VLM 描述（含軸標、數值、座標、組件等具體資訊）。**bullet 必須優先",
            "從這些段落取材**，例如「Figure 3 雙分支架構顯示左 RGB / 右 Tsallis」、",
            "「圖 4 結果柱狀圖：CT350 機台達到 88.73% 改善」這種具體寫法，",
            "而非抽象的「本研究透過資訊融合提升效能」。**沒有具體 = bullet 失敗**。",
            "",
            "── 整體內容規則 ──",
            "- 使用**台灣繁體中文**（不只字符繁體、用詞也要台灣本土）。",
            f"- 投影片數量：{count_hint}（首張固定為 section_break，規則 1）。",
            "- 每張 3-6 個 bullet（layout 不需要 bullet 也要填 1-2 句保險用）。",
            "- speaker_notes 寫 2-4 句講者口述稿。",
            "- standard slide 的 title 不可重複（section_break 例外、可重複）。",
            "",
            "── 其他 layout 條件選用 ──",
            "- **stat_callout**：文件含量化結果（百分比、實驗數值、KPI、提升幅度）",
            "  時，挑最關鍵的 1 個做 stat_callout。stat.value 是大字數字，",
            "  stat.label 是該數字代表什麼，stat.supporting 是補充細節。",
            "  **必填 supporting：寫 20-100 字的數字脈絡**（baseline、樣本數、",
            "  實驗條件、結果意義）；**不可只寫「重要突破」「顯著進步」這類空話**。",
            "  若有對照基準，**強烈建議**填 stat.baseline + stat.baseline_label，",
            "  renderer 會自動切成左右對比版型（baseline ← → value），視覺更有力。",
            "  範例：{value:\"95%\", label:\"CT350 機臺鐵屑覆蓋率偵測率\",",
            "         supporting:\"雙分支架構相比單分支 ResNet18 基準的 78%，提升 17 個百分點；",
            "         測試集為 10 個機臺切換批次，N=2,400\",",
            "         baseline:\"78%\", baseline_label:\"單分支基準\"}",
            "- **two_column**：內容天然有對照（before/after、本研究 vs 既有方法、",
            "  兩種模型架構比較）時用 1 張。columns 必須 **2 個元素**、各填 heading + bullets。",
            "  **每欄 2-3 條 bullet**（schema 最低 2 條），讓兩欄視覺密度對稱、不留大片空白；",
            "  湊不到 2 條的對照結構,改用 icon_rows（保留並列感、視覺更輕）。",
            "- **quote**：有名言、客戶證言、概念金句時用。",
            "- **icon_rows**：3-5 個並列要點各有 icon。concept 從白名單挑。",
            "- **image_focus**（Phase 5 新增）：文件原檔有相關插圖時用。例：",
            "  論文的 architecture diagram、實驗結果柱狀圖、流程圖。設",
            "  layout_kind='image_focus' 並把使用者訊息「可用圖」清單中相對應的",
            "  image_id 填到 Slide.image_ref。bullets 仍要寫 2-4 條，描述圖之外的",
            "  補充資訊；圖會佔投影片左半，bullets 在右半。一張圖只應出現在一張投影片。",
            "",
            "  ── image_focus 兩種生成模式（Studio Fix 2，2026-05-18）──",
            "  若可用圖清單為空或都不合用，可即時生成。**兩種模式擇一**：",
            "",
            "  **自動規則 — 觸發 diagram path**：若 slide 的 title 含「架構、拓撲、",
            "  拓樸、流程、Workflow、Pipeline、Topology」其中一個關鍵字，且該 slide",
            "  主題自然需要視覺輔助（例如「Multi-Agent Supervisor 拓撲設計」、",
            "  「Agentic Workflow 三階段」、「RAG 系統架構」），**必須**設",
            "  layout_kind='image_focus' + image_kind='diagram' + diagram_dot",
            "  （Graphviz DOT）。不要寫 image_prompt（FLUX 不會渲染文字 label，",
            "  結果會是亂碼）。",
            "",
            "  (A) **illustration** — 情境插畫、概念意象、**無文字**的視覺輔助。",
            "      設 image_kind='illustration' + image_prompt（**英文** 50-500 字，",
            "      含主體 / 場景 / 構圖 / 風格）。走 FLUX.2-dev 即時生成。",
            "      適合：主題情境（如「山地戰術部隊」「無人機巡邏」）、抽象概念、",
            "      氣氛圖。**注意：FLUX 無法畫出可讀的文字**，所以不要叫它畫架構圖。",
            "",
            "  (B) **diagram** — 含 label 的圖示（架構圖、流程圖、Venn、決策樹、ER）。",
            "      設 image_kind='diagram' + diagram_dot（**Graphviz DOT** 語法，",
            "      最多 3000 字元）。走 graphviz `dot -Tpng` 渲染，label 清晰可讀。",
            "      適合：系統架構圖、Multi-Agent 拓撲、資料流、實體關係、決策樹。",
            "",
            "      DOT 範例（Multi-Agent Supervisor 架構）：",
            "        digraph G {",
            "          rankdir=TB;",
            "          fontname=\"Noto Sans CJK TC\";",
            "          node [fontname=\"Noto Sans CJK TC\", shape=box, style=rounded];",
            "          Supervisor -> \"Worker A\";",
            "          Supervisor -> \"Worker B\";",
            "          Supervisor -> \"Worker C\";",
            "        }",
            "",
            "  **每張 slide 只能選一種模式**：image_ref / image_kind='illustration' /",
            "  image_kind='diagram'，三者互斥。含 label 的圖示**一定走 diagram**，",
            "  不要丟給 FLUX 畫，否則 label 會變亂碼。",
            "- **commit fully**：選了豐富版型就把欄位填好；不要半途而廢。",
            "- **layout_kind 拼寫精確**：'standard' / 'section_break' / 'stat_callout' /",
            "  'quote' / 'two_column' / 'icon_rows' / 'image_focus'。",
            "  其他寫法會被歸類為 standard。",
            "",
            "── 台灣用語對映（簡中用詞 → 台灣慣用詞，務必使用右邊） ──",
            "  視頻 → 影片        軟件 → 軟體        硬件 → 硬體",
            "  網絡 → 網路        激光 → 雷射        信息 → 資訊",
            "  數據 → 資料        默認 → 預設        登錄 → 登入",
            "  文件 → 檔案        程序 → 程式        內存 → 記憶體",
            "  分辨率 → 解析度    打印 → 列印        鼠標 → 滑鼠",
            "  優化 → 最佳化      質量 → 品質        屏幕 → 螢幕",
            "  支持 → 支援（動詞）服務器 → 伺服器    應用 → 應用程式（指 app 時）",
            "  單擊 → 點擊        雙擊 → 連點        集成 → 整合",
            "**注意**：上面只是樣本，請整體用台灣慣用語；輸出後系統會跑自動轉換做",
            "兜底，但你寫對的話品質更高。",
            "- 不可使用 placeholder（lorem ipsum / TBD / TODO / <insert ...>）。",
            "",
            "若使用者訊息提供了檢索到的段落，請以那些段落為事實依據；",
            "bullets 可在末尾用 (參 [N]) 標註來源。",
        ]
    )

    parts = [
        f"知識庫名稱：{collection_name}",
        f"風格 preset：{preset}",
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
            "（本次未檢索到相關段落；請依使用者輸入直接發揮，"
            "並在末尾的 speaker_notes 內提醒「本草稿未取得文件支撐」。）"
        )
    if images:
        parts.append("")
        parts.append("── 可用圖（從文件原始嵌入圖中依與本主題的相似度檢索） ──")
        parts.append(
            "若某張投影片用以下任一張圖更具說服力，請設 layout_kind='image_focus' "
            "並把該行的 image_id 填到 Slide.image_ref。一張圖只應被一張投影片引用；"
            "若全部圖都不夠相關，請忽略這份清單、不要硬塞。"
            "若該 slide 需要圖但此清單無合適現有圖，layout_kind='image_focus' 下兩種模式擇一："
            "（A）image_kind='illustration' + image_prompt（英文 50-500 字描述，FLUX 即時生成情境插畫）；"
            "（B）image_kind='diagram' + diagram_dot（Graphviz DOT，最多 3000 字，graphviz 渲染含 label 的架構/流程圖）。"
            "image_ref / illustration / diagram 三者互斥，一張 slide 只設其一；含文字 label 的圖一律走 diagram。"
        )
        parts.append("")
        for i, im in enumerate(images, start=1):
            cap = (im.get("caption") or "").replace("\n", " ")[:240]
            page = im.get("page")
            parts.append(
                f"[img_{i}] image_id={im['image_id']} "
                f"page={page if page is not None else '?'} "
                f"score={im.get('score', 0):.3f}"
            )
            parts.append(f"  caption: {cap}")
        parts.append("")

    if extra_instructions:
        parts.append("")
        parts.append(f"使用者補充指示：\n{extra_instructions}")

    return system, "\n".join(parts)


async def _call_llm_chat(
    db: Session,
    user: User,
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.4,
    max_tokens: int | None = None,
) -> str:
    """Invoke ``/v1/chat/completions`` via the in-process proxy.

    Returns the assistant content string. Going through ``proxy_request``
    rather than direct httpx keeps usage metering (token_usage table) in
    place — Studio calls show up in the same dashboards as user chat.
    """
    model = (
        db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    )
    if model is None or not model.is_active:
        raise HTTPException(
            status_code=503,
            detail=f"Studio LLM '{model_name}' not registered or inactive.",
        )

    body: dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    response = await proxy_request(
        model=model,
        api_key_id=None,
        user_id=user.id,
        department_id=user.department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
    )
    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM '{model_name}' returned an unexpected payload shape.",
        ) from e


class _StudioLLMAdapter:
    """Adapts ``_call_llm_chat`` to the ``complete(system, user)`` interface
    the FLUX prompt rewriter (Layer A) expects.

    The rewriter is deliberately decoupled from CSP internals (DB session,
    ModelRegistry, usage metering) — it only needs "give me one completion
    for this system+user pair". This thin wrapper binds the db/user/model
    context so rewriter calls still flow through ``proxy_request`` and land
    in the same token-usage dashboards as every other Studio LLM call.
    """

    def __init__(self, db: Session, user: User, model_name: str = SLIDES_LLM_MODEL) -> None:
        self._db = db
        self._user = user
        self._model_name = model_name

    async def complete(self, *, system: str, user: str) -> str:
        return await _call_llm_chat(
            self._db,
            self._user,
            self._model_name,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Low temp: the rewriter wants a stable, deterministic visual
            # description, not creative variance (variance comes from the
            # FLUX seed, not the prompt).
            temperature=0.2,
        )


class _Gemma4VlmGate:
    """gemma4-backed VLM for the Stage 2 quality gate (Layer C).

    gemma4 is already deployed and multimodal; ``_inspect_slide_visually``
    above proves the OpenAI vision message shape (``image_url`` with a
    ``data:image/png;base64,...`` URL) reaches it through
    ``_call_llm_chat`` -> ``proxy_request``. This adapter reuses that exact
    path for the gate's semantic + text check, so no separate VLM is
    deployed (per the confirmed Stage 2 premise).

    Exposes ``async check(png_bytes, *, concept) -> dict`` — the Vlm
    Protocol the gate expects.
    """

    def __init__(self, db: Session, user: User, model_name: str = VISION_LLM_MODEL) -> None:
        self._db = db
        self._user = user
        self._model_name = model_name

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        system_prompt = (
            "You are an image quality gate for slide illustrations. "
            "Answer with JSON only — first char {, last char }, no ```json "
            "fence, no preamble."
        )
        user_text = (
            f"Does this image depict an abstract, text-free illustration of: "
            f"{concept}?\n"
            "Does it contain ANY letters, characters, digits, logos, or "
            "readable signage?\n"
            "Also rate 0.0-1.0 how cleanly and aptly it depicts the concept "
            "(1.0 = excellent, on-concept, no text or artifacts).\n"
            'Answer JSON only: {"match": bool, "has_text": bool, '
            '"score": <0.0-1.0>, "reason": "<short>"}'
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        raw = await _call_llm_chat(
            self._db, self._user, self._model_name, messages, temperature=0.1,
        )
        try:
            parsed = json.loads(_extract_json_object(raw))
        except (ValueError, json.JSONDecodeError):
            # Unparseable verdict — fail CLOSED for the gate (treat as a
            # reject) so a flaky VLM response doesn't slip an unverified
            # image through. The retry loop / fallback handles it.
            logger.warning(
                "VLM gate returned unparseable response; treating as reject."
            )
            return {"match": False, "has_text": True, "score": 0.0, "reason": "unparseable"}
        score_raw = parsed.get("score", 0.0)
        try:
            score = max(0.0, min(1.0, float(score_raw)))
        except (TypeError, ValueError):
            score = 0.0
        return {
            "match": bool(parsed.get("match")),
            "has_text": bool(parsed.get("has_text")),
            "score": score,
            "reason": str(parsed.get("reason", "")),
        }


# ── Step 5+6: generate + validate (with one correction pass) ─────────────


async def _generate_validated_spec(
    db: Session,
    user: User,
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
        db, user, SLIDES_LLM_MODEL, messages, temperature=0.3,
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
                db, user, SLIDES_LLM_MODEL, messages, temperature=0.2,
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
      FLUX_CACHE_DIR         (default: $INGESTION_UPLOAD_DIR/flux-cache)
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

    upload_dir = os.environ.get(
        "INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads"
    )
    cache_dir = os.environ.get(
        "FLUX_CACHE_DIR", os.path.join(upload_dir, "flux-cache")
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
    vlm = _Gemma4VlmGate(llm._db, llm._user)
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
    upload_dir: str,
    *,
    flux_provider: "FluxImageProvider | None" = None,
    default_aspect: str = "16:9",
    deck_base_seed: int | None = None,
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
) -> dict[str, Any]:
    """Resolve every Slide.image_ref / diagram_dot / image_prompt into inline base64 PNG.

    Order of precedence per slide (curated > deterministic > generative):
      1. image_ref present and resolvable → inline existing PNG.
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
    diagram / image_prompt paths only).
    """
    import base64

    from app.services.diagram_renderer import render_dot_to_png

    slides = spec_dict.get("slides") or []
    generated_count = 0
    for idx, slide in enumerate(slides):
        # Path 1: image_ref (existing behavior — unchanged)
        ref = slide.get("image_ref")
        if ref:
            meta = images_lookup.get(ref)
            if not meta:
                slide.pop("image_ref", None)
                # If a fallback image_prompt is present, try that next
            else:
                try:
                    abs_path = os.path.join(upload_dir, meta["storage_path"])
                    with open(abs_path, "rb") as f:
                        blob = f.read()
                    mime = meta.get("mime") or "image/png"
                    slide["image_data"] = (
                        f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
                    )
                    # ref wins over both generative paths
                    slide.pop("image_prompt", None)
                    slide.pop("diagram_dot", None)
                    slide.pop("image_kind", None)
                    continue
                except OSError as e:
                    logger.warning(
                        "Failed to hydrate image_ref=%s for storage_path=%s: %s — "
                        "falling back to diagram_dot / image_prompt if available.",
                        ref, meta.get("storage_path"), e,
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
            if not ok:
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
    `_hydrate_images` before the spec leaves the CSP boundary.

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
        # Worker writes to share/uploads/ingestion via INGESTION_UPLOAD_DIR;
        # CSP mounts the same directory at the same path (see compose).
        # storage_path on the row is relative to that root, so we just
        # join here.
        ingest_upload = os.getenv(
            "INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads",
        )
        spec_dict = await _hydrate_images(
            spec_dict,
            images_lookup or {},
            ingest_upload,
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
    db: Session,
    user: User,
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
        db, user, VISION_LLM_MODEL, messages, temperature=0.1,
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
    db: Session,
    user: User,
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
            return await _inspect_slide_visually(db, user, idx, b)

    results = await asyncio.gather(
        *(_one(i, b) for i, b in enumerate(pngs)),
        return_exceptions=False,
    )
    vision_flat: list[VisualDefect] = []
    for r in results:
        vision_flat.extend(r)
    return _merge_defects(geom_defects, vision_flat)


async def _fix_spec_with_defects(
    db: Session,
    user: User,
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
        db, user, SLIDES_LLM_MODEL, messages, temperature=0.2,
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
    db: Session,
    user: User,
) -> dict[str, Any]:
    """Thin wrapper around ``_call_llm_chat`` for the rebalance pass.

    Extracted as its own helper so tests can mock the LLM round-trip
    without standing up the full proxy / model registry. Returns the
    parsed JSON dict (caller validates the `changes` shape).
    """
    system, user_msg = prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    raw = await _call_llm_chat(
        db, user, SLIDES_LLM_MODEL, messages, temperature=0.2,
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
    db: Session,
    user: User,
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
        result = await _call_llm_for_rebalance(prompt, db=db, user=user)
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
    user_id: int,
    payload: GenerateSpecRequest,
    updater: jobs.JobUpdater,
) -> None:
    """Executes steps 3-9 and pushes state transitions to the updater.

    Runs INSIDE the asyncio task spawned by the job manager. Owns its own
    DB session because the request-scoped session from FastAPI's
    Depends(get_db) is closed by the time the POST handler returns.
    Re-resolving the User and collection inside this session keeps the
    ORM objects attached.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise RuntimeError(f"user {user_id} disappeared mid-job")
        coll = _require_collection_access(db, user, payload.collection_id)

        # FLUX Stage 1 (4.2): deterministic per-deck seed derived once from
        # the job_id, so re-running the same job yields the same images.
        # Per-slide seed = deck_base_seed + slide_index (computed in
        # _hydrate_images). The llm adapter lets the rewriter (Layer A)
        # reuse the same proxy/usage path as every other Studio LLM call.
        deck_base_seed = int(
            hashlib.sha256(updater.job_id.encode()).hexdigest()[:8], 16
        )
        flux_llm = _StudioLLMAdapter(db, user, SLIDES_LLM_MODEL)

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
                    db, user, payload.collection_id, seed_query,
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
                    db, user, payload.collection_id, seed_query,
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
            db,
            user,
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
                        spec_dict, violations, chunks_str, db=db, user=user,
                    )
                    spec = SlidesSpec.model_validate(rebalanced)
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
            spec, images_lookup, deck_base_seed=deck_base_seed, llm=flux_llm,
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
                    db, user, pptx_path, pptx_bytes=pptx_bytes,
                )
                critical = [d for d in defects if d.severity == "critical"]
                if not critical or qa_passes > VISUAL_QA_PASSES:
                    final_defects = defects
                    break
                # Critical defects exist AND we still have a fix budget —
                # ask the LLM to revise, re-render, re-QA.
                await updater.set(step=JOB_STEP_FIXING)
                try:
                    spec = await _fix_spec_with_defects(db, user, spec, critical)
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
                    spec, images_lookup,
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
    finally:
        db.close()


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/slides/jobs",
    response_model=JobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_slides_job(
    payload: GenerateSpecRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JobStatus:
    """Register a slide-deck generation job and return its initial status.

    Returns immediately (HTTP 202) with state="pending" and a job_id.
    The pipeline runs on an asyncio.Task in the background; clients poll
    GET /jobs/{id} for state transitions and GET /jobs/{id}/pptx for the
    binary once state="done".
    """
    # Authorize collection access up-front so the user gets a synchronous
    # 403/404 instead of an opaque "failed" job seconds later.
    _require_collection_access(db, current_user, payload.collection_id)

    async def _runner(updater: jobs.JobUpdater) -> None:
        await _run_pipeline(
            user_id=current_user.id, payload=payload, updater=updater,
        )

    record = await jobs.create_job(
        user_id=current_user.id,
        collection_id=payload.collection_id,
        runner=_runner,
    )
    return record.to_status()


@router.get("/slides/jobs/{job_id}", response_model=JobStatus)
async def get_slides_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> JobStatus:
    """Cheap polling endpoint. Returns the current JobStatus or 404."""
    rec = jobs.get_user_job(job_id, current_user.id)
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
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the rendered .pptx for a completed job.

    Returns 404 for unknown/cross-user jobs, 409 if the job is still
    running, and 410 if it has been failed/cancelled.
    """
    rec = jobs.get_user_job(job_id, current_user.id)
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
    current_user: User = Depends(get_current_user),
) -> Response:
    """Cancel an in-flight job. No-op if already terminal."""
    cancelled = await jobs.cancel_job(job_id, current_user.id)
    if not cancelled:
        # Either not yours / not found / already terminal — all fine; the
        # client doesn't need to distinguish for "delete my row" UX.
        rec = jobs.get_user_job(job_id, current_user.id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.",
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

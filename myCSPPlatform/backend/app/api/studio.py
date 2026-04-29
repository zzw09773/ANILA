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
import json
import logging
import re
from typing import Any

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
from app.services.ingestion_pool import get_pool
from app.services.proxy_service import proxy_request
from app.services.studio_text_normalizer import normalize_spec

router = APIRouter(prefix="/api/studio", tags=["Studio / Slides"])
logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Retrieval depth for slide deck generation. Higher than chat (top-5) because
# Studio synthesises across the whole deck, not a single Q&A turn.
STUDIO_TOP_K = 12
STUDIO_MIN_SCORE = 0.25
STUDIO_CONTENT_LIMIT_CHARS = 800

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

    return [
        {
            "filename": filenames.get(h.chunk.document_id, "<unknown>"),
            "chunk_key": h.chunk.chunk_key,
            "content": h.chunk.content[:STUDIO_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
    ]


# ── Step 4-5: LLM call helpers ────────────────────────────────────────────


def _build_generation_prompt(
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
) -> tuple[str, str]:
    """Compose (system, user) prompts for the slide-deck LLM call.

    Phase 3 expands the prompt with:
      * palette selection (4 options)
      * per-slide layout_kind (6 variants)
      * icon_rows.concept whitelist (~30 keywords from a closed set)

    The hard rule we communicate to the LLM is **bullets[] is always
    required** even when a non-standard layout_kind is chosen, because
    the renderer falls back to standard rendering of bullets if the
    layout-specific payload is malformed. This means the LLM can
    aspirationally choose a fancy layout AND still ship a usable slide
    if the layout-specific fields don't pan out.
    """
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
            'Required: palette — 從以下挑一個（renderer 會落地成具體配色）：',
            '  "navy_amber"        商務、技術、政策、一般用途（預設）',
            '  "forest_moss"       永續、健康、教育、自然主題',
            '  "charcoal_minimal"  嚴肅報告、財務、法規',
            '  "coral_energy"      行銷、品牌、創意活力',
            '整份簡報只能挑一個 palette；不要在 slides 內切換。',
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
            '  stat 形狀：{"value": "47%", "label": "...", "supporting": "..."}',
            '             value 是大字數值；supporting 為選填細節。',
            '  quote 形狀：{"text": "...", "attribution": "..."}',
            '             attribution 為選填來源。',
            '  columns 形狀：[{"heading": "...", "bullets": ["..."]},',
            '                 {"heading": "...", "bullets": ["..."]}]',
            '             固定 2 個元素的陣列；多於 2 會被忽略、少於 2 會降級為 standard。',
            '  icon_rows 形狀：[{"concept": "...", "heading": "...", "description": "..."}]',
            '             3-5 個元素；concept 必須來自下方白名單。',
            "",
            "重要：bullets 任何 layout 都要填（renderer 在 layout-specific 欄位",
            "缺漏時會回退用 bullets 渲染，不要省）。",
            "",
            "── icon_rows.concept 必須從以下白名單挑（其他會被忽略不畫 icon）──",
            "資料/運算: data_storage data_pipeline dataset automation",
            "          integration deployment",
            "人/角色:   user team customer",
            "溝通:     chat email notification broadcast",
            "分析/結果: insight metrics comparison search",
            "時間:     schedule deadline history",
            "品質/安全: security validation error success achievement",
            "系統:     settings server cloud network",
            "文件/學習: document book learning",
            "",
            "icon 規則：",
            '- 同一張 icon_rows 的 concept 抽象層級要一致（全部「功能」或全部',
            "  「角色」之類），不要混。",
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
            "── 整體內容規則 ──",
            "- 使用**台灣繁體中文**（不只字符繁體、用詞也要台灣本土）。",
            "- 8-12 張投影片；首張可以是 section_break 介紹簡報主題。",
            "- 每張 3-6 個 bullet（如 layout 不需要 bullet 也要填 1-2 句保險用）。",
            "- speaker_notes 寫 2-4 句講者口述稿。",
            "- standard slide 的 title 不可重複（section_break 例外、可重複）。",
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


# ── Step 5+6: generate + validate (with one correction pass) ─────────────


async def _generate_validated_spec(
    db: Session,
    user: User,
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
) -> tuple[SlidesSpec, bool]:
    """LLM → JSON → SlidesSpec, retrying once on validation failure.

    Returns (spec, fallback_used). When fallback_used=True, the spec is
    a synthetic safety-net deck explaining the failure to the user; the
    caller should skip vision QA (which would try to "fix" a deliberately
    minimal deck and might trigger another LLM call that also fails).
    """

    system, user_msg = _build_generation_prompt(
        collection_name, preset, extra_instructions, chunks,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # temperature=0.2 (down from default 0.4) — slide deck generation is
    # mostly extraction, not creative writing; lower temp keeps gemma4
    # away from the "thought\n*   Role: ..." preamble pattern.
    raw = await _call_llm_chat(
        db, user, SLIDES_LLM_MODEL, messages, temperature=0.2,
    )

    last_err: ValidationError | ValueError | json.JSONDecodeError | None = None
    last_raw = raw
    for attempt in range(SCHEMA_CORRECTION_PASSES + 1):
        try:
            extracted = _extract_json_object(raw)
            return SlidesSpec.model_validate(_loads_lenient(extracted)), False
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


async def _render_pptx(spec: SlidesSpec) -> tuple[bytes, str]:
    """POST spec → renderer → (pptx bytes, server-side path).

    Returns the path so /screenshots can refer to it without us having to
    base64 the .pptx through CSP memory.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                f"{RENDERER_BASE_URL}/render",
                json={"spec": spec.model_dump()},
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


async def _visual_qa(
    db: Session,
    user: User,
    pptx_path: str,
) -> list[VisualDefect]:
    """Run vision QA on every slide of a rendered .pptx."""
    pngs = await _capture_screenshots(pptx_path)
    if not pngs:
        return []

    # Sequential per-slide: gemma4 backend is single-tenant; concurrent
    # requests can starve each other on the GPU. Cap parallelism at 2 to
    # halve wall-clock without overwhelming the model.
    semaphore = asyncio.Semaphore(2)

    async def _one(idx: int, b: bytes) -> list[VisualDefect]:
        async with semaphore:
            return await _inspect_slide_visually(db, user, idx, b)

    results = await asyncio.gather(
        *(_one(i, b) for i, b in enumerate(pngs)),
        return_exceptions=False,
    )
    flat: list[VisualDefect] = []
    for r in results:
        flat.extend(r)
    return flat


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

        # ── Steps 4-6: LLM → JSON → SlidesSpec ──
        await updater.set(step=JOB_STEP_GENERATING)
        spec, used_fallback = await _generate_validated_spec(
            db,
            user,
            coll.name,
            payload.preset,
            payload.extra_instructions,
            chunks,
        )
        # ── Step 6.5: zh-CN → zh-TW post-processing ──
        # Gemma 4 leaks simplified-Chinese phrasing into 繁體 output (研究第
        # 33-39 行有量化證據). The system prompt fights this with an explicit
        # mapping table, but enforcement isn't 100% — we run OpenCC s2twp as
        # a deterministic safety net AFTER spec validate (so the LLM-emitted
        # structural integrity check has already passed) and BEFORE render
        # / vision QA (so all downstream steps see clean Traditional Chinese).
        spec = normalize_spec(spec)
        # Surface the title early so the UI can show "鑄造中：<title>"
        # before render finishes.
        await updater.set(title=spec.title, slide_count=len(spec.slides))

        # ── Step 7: render ──
        await updater.set(step=JOB_STEP_RENDERING)
        pptx_bytes, pptx_path = await _render_pptx(spec)

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
                defects = await _visual_qa(db, user, pptx_path)
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
                pptx_bytes, pptx_path = await _render_pptx(spec)

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

"""Datatable API — RAG-driven structured table → HTML/CSV/XLSX pipeline.

Same job-based shape as the slides pipeline (POST /jobs → poll /status →
GET /download/{fmt}). All heavy work runs inside an asyncio.Task so the
POST returns in < 50 ms with a job_id; clients poll for state transitions.

    request → [POST /jobs] → 202 job_id
                  │
                  └─ asyncio task ──────────────────────────┐
                       │                                     │
                       ▼                                     │
                  [retrieve] csp /search                     │
                       │                                     │
                       ▼                                     │
                  [generate] LLM → DatatableSpec JSON        │
                       │                                     │
                       ▼                                     │
                  [normalize] s2twp on title/label/cells     │
                       │                                     │
                       ▼                                     │
                  [export] HTML + CSV + XLSX on disk ────────┘
                       │
                  job state="done", artifact_paths set

The runner is in this module (not a separate `datatable_pipeline.py`)
because it's relatively compact (one LLM call + one normalize + three
exports) and lives close to the endpoints that wire it up.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

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
from app.schemas.datatable import (
    JOB_STEP_EXPORTING,
    JOB_STEP_GENERATING,
    JOB_STEP_NORMALIZING,
    JOB_STEP_RETRIEVING,
    DataColumn,
    DataRow,
    DatatableJobStatus,
    DatatablePreset,
    DatatableSpec,
    GenerateDatatableRequest,
)
from app.services import datatable_job_service as jobs
from app.services.datatable_exporter import to_csv, to_html, to_xlsx
from app.services.studio_text_normalizer import strip_latex


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/datatables", tags=["datatables"])


# ── Tunables ──────────────────────────────────────────────────────────────


DATATABLE_LLM_MODEL = "gemma4"
# Same retrieval threshold as slides — chunks below this score are usually
# unrelated and just dilute the prompt budget.
MIN_SCORE = 0.25
# Cap on context per chunk; bigger chunks get truncated client-side so the
# LLM doesn't run out of context window with a single dense chunk.
CONTENT_LIMIT_CHARS = 1500
# One correction pass on validation failure — same heuristic as slides
# (third attempt has ~64% blind-spot rate per the research file). If two
# tries fail we surface as job state="failed" since there's no obvious
# fallback datatable (unlike slides which can show "what went wrong").
SCHEMA_CORRECTION_PASSES = 1


# ── Preset → prompt hints ─────────────────────────────────────────────────


_PRESET_HINTS: dict[DatatablePreset, str] = {
    DatatablePreset.KEY_FIGURES: (
        "目標: 從文件中萃取「關鍵指標」彙整表 — 數字、比率、年份、規模。"
        " columns 範例: 指標名稱、數值、單位、來源段落。"
        " dtype 配合: 數值欄用 number 或 percent;描述欄用 text。"
    ),
    DatatablePreset.ENTITY_ATTRIBUTES: (
        "目標: 為文件中提到的多個「實體」(產品、單位、人物、地區)列出共同屬性。"
        " columns 範例: 實體名稱、類別、地點、規模、備註。"
        " 每一列代表一個實體;若某實體缺某屬性,該 cell 留 null。"
    ),
    DatatablePreset.TIMELINE_TABLE: (
        "目標: 從文件中提取時間軸事件 — 日期、事件、變化。"
        " columns 範例: 日期、事件、影響、相關方。"
        " dtype 配合: 日期欄用 date,其餘 text。rows 按時間正序排列。"
    ),
    DatatablePreset.COMPARISON_TABLE: (
        "目標: 兩個或多個對象的「並排比較」(X vs Y)。"
        " columns 範例: 比較項目、對象 A、對象 B、(對象 C)。"
        " 每一列代表一個比較維度(價格、規模、優勢、限制等)。"
    ),
}


def _build_prompt(
    *,
    collection_name: str,
    preset: DatatablePreset,
    extra_instructions: str | None,
    target_columns: list[str] | None,
    chunks: list[dict[str, Any]],
) -> tuple[str, str]:
    """Build (system, user) messages for the LLM.

    The system message locks the response shape (JSON only, no fences,
    no preamble). The user message carries the preset hint, extra
    instructions, target columns, and the chunk evidence.

    Chunks are formatted as numbered references so the LLM can decide
    whether to cite them in cell values (we don't require citation in
    datatables — it would clutter the table; speaker_notes-style audit
    isn't part of this artifact).
    """
    preset_hint = _PRESET_HINTS.get(preset, "")
    system = (
        "你是一個結構化資料表生成器。讀取使用者提供的文件片段,根據 preset "
        "決定欄位、抽取資料,輸出一張表的 JSON。\n\n"
        "規則:\n"
        "1. 嚴格輸出 JSON object,第一個字元是 `{`,最後一個是 `}`,中間"
        "不要有 ```json fence 或任何 preamble。\n"
        "2. 屬性名稱跟字串值都用「雙引號」,不要用單引號或全形「」。\n"
        "3. cells 必須來自提供的 chunks — 找不到的 cell 寫 null,不要瞎填、"
        "不要編造數字。\n"
        "4. columns 必須在 2~10 個之間,rows 必須在 1~200 之間;每個 column"
        "需要 key (英數內部 id) 跟 label (使用者看到的中文標題)。\n"
        "5. dtype 從 [text, number, date, percent] 選一;align 從 [left, "
        "center, right] 選一。數值欄一律 number 或 percent,日期一律 date。\n"
        "6. 全文用繁體中文。\n\n"
        "JSON schema 結構:\n"
        "{\n"
        '  "title": str,\n'
        '  "subtitle": str | null,\n'
        '  "preset": "<從 enum 選>",\n'
        '  "columns": [{"key": str, "label": str, "dtype": str, "align": str}, ...],\n'
        '  "rows": [{"cells": {"<column.key>": str|int|float|null, ...}}, ...],\n'
        '  "notes": str | null\n'
        "}"
    )

    chunks_block = "\n\n".join(
        f"[{i + 1}] {c.get('filename', '<unknown>')}\n{c.get('content', '')}"
        for i, c in enumerate(chunks)
    ) or "（無檢索結果 — 你可以基於 collection 名稱 + preset 給出合理的空白範本,並在 notes 註明資料來源不足。）"

    target_cols_block = (
        f"使用者希望的欄位(僅供參考,你可以增刪): {', '.join(target_columns)}"
        if target_columns
        else "未指定目標欄位 — 由你根據 preset 選擇最能彰顯內容的欄位。"
    )
    extra_block = (
        f"使用者額外指示:\n{extra_instructions.strip()}\n"
        if extra_instructions
        else ""
    )

    user = (
        f"Collection: {collection_name}\n"
        f"Preset: {preset.value}\n"
        f"Preset 說明: {preset_hint}\n"
        f"{target_cols_block}\n"
        f"{extra_block}\n"
        f"文件片段:\n{chunks_block}\n\n"
        "請輸出完整 JSON。"
    )
    return system, user


# ── JSON extraction (matches studio.py shape) ─────────────────────────────


_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE,
)


def _extract_json_object(raw: str) -> str:
    """Slice the last balanced JSON object out of a noisy LLM response.

    Same algorithm as ``app.api.studio._extract_json_object`` — kept
    inlined here so the datatable module is self-contained (no leak
    into the slides pipeline's internal helpers).
    """
    de_thought = _THINK_BLOCK_RE.sub("", raw)
    no_fences = (
        de_thought.replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
    )
    depth = 0
    end = -1
    start = -1
    # Scan from the right counting brace nesting.
    for i in range(len(no_fences) - 1, -1, -1):
        ch = no_fences[i]
        if ch == "}":
            if depth == 0:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end != -1:
                start = i
                break
    if start == -1 or end == -1:
        # Fall through — let json.loads raise a clean error.
        return no_fences.strip()
    return no_fences[start : end + 1]


def _loads_lenient(text: str) -> Any:
    """``json.loads`` with single-quote-to-double-quote repair."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r"(?<=[\[\{,:\s])'", '"', text)
    repaired = re.sub(r"'(?=[\]\},:\s]|$)", '"', repaired)
    return json.loads(repaired)


# ── LLM helper ────────────────────────────────────────────────────────────


async def _call_llm(
    *, bearer: str, messages: list[dict[str, Any]], temperature: float = 0.3,
) -> str:
    """Wrap csp's chat-completions proxy with the datatable-side error map.

    Mirrors studio.py's ``_call_llm_chat`` but only the subset we need
    (no model_name override — datatable always uses DATATABLE_LLM_MODEL).
    Errors get raised as ``RuntimeError`` so the runner's outer except
    catches them and marks the job failed with the message; we DO NOT
    raise HTTPException here because the runner is inside an asyncio.Task
    that has no HTTP response context.
    """
    try:
        response = await proxy_chat_completions(
            model=DATATABLE_LLM_MODEL,
            messages=messages,
            temperature=temperature,
            bearer=bearer,
        )
    except CspNotFoundError as exc:
        raise RuntimeError(
            f"Datatable LLM '{DATATABLE_LLM_MODEL}' 未在 csp 註冊: {exc}"
        ) from exc
    except CspUnauthorizedError as exc:
        raise RuntimeError(f"csp 授權失效: {exc}") from exc
    except CspForbiddenError as exc:
        raise RuntimeError(f"csp 拒絕存取: {exc}") from exc
    except (CspServerError, CspClientError) as exc:
        raise RuntimeError(f"csp proxy 失敗: {exc}") from exc

    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM 回應格式異常") from exc


# ── Spec generation w/ one correction pass ────────────────────────────────


async def _generate_validated_spec(
    *,
    bearer: str,
    collection_name: str,
    payload: GenerateDatatableRequest,
    chunks: list[dict[str, Any]],
) -> DatatableSpec:
    """LLM → JSON → DatatableSpec, retrying once on validation failure.

    Unlike slides there's no synthetic fallback — if both passes fail we
    raise so the runner marks the job failed. A "failed datatable" with
    fabricated rows is worse UX than a clear error message.
    """
    system, user_msg = _build_prompt(
        collection_name=collection_name,
        preset=payload.preset,
        extra_instructions=payload.extra_instructions,
        target_columns=payload.target_columns,
        chunks=chunks,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    raw = await _call_llm(bearer=bearer, messages=messages)
    last_err: Exception | None = None

    for attempt in range(SCHEMA_CORRECTION_PASSES + 1):
        try:
            extracted = _extract_json_object(raw)
            parsed = _loads_lenient(extracted)
            spec = DatatableSpec.model_validate(parsed)
            # Defensive: the spec is structurally valid but the LLM
            # might have emitted zero useful rows (all cells null). The
            # schema accepts that — runner-level check below treats it
            # as failed.
            if not spec.rows:
                raise ValueError("LLM 回傳了 0 列 — 視為失敗")
            return spec
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            logger.warning(
                "Datatable spec validate failed (attempt %d): %s\nraw[:400]=%r",
                attempt + 1, str(exc)[:200], raw[:400],
            )
            if attempt >= SCHEMA_CORRECTION_PASSES:
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "上一次回覆無法通過 schema 驗證:\n\n"
                        f"{str(exc)[:1500]}\n\n"
                        "請只修正以上欄位、保留其餘內容,重新輸出整個 JSON。"
                        "屬性名稱跟字串值必須用「雙引號」包起來。"
                    ),
                }
            )
            raw = await _call_llm(bearer=bearer, messages=messages, temperature=0.2)

    raise RuntimeError(
        f"Datatable spec 驗證連續失敗,放棄 ({str(last_err)[:200]})"
    )


# ── Text normalisation (繁中 + LaTeX) ──────────────────────────────────────


def _normalize_spec_text(spec: DatatableSpec) -> DatatableSpec:
    """Apply strip_latex + s2twp normalisation to user-visible string fields.

    Uses the same OpenCC s2twp pipeline as the slides normaliser so output
    text is consistent across artifact kinds. Numeric / None cell values
    pass through unchanged — we ONLY touch string cells.
    """
    # Lazy import to avoid pulling OpenCC into module import time of API
    # router (OpenCC has a ~50ms dict load — fine at first call, but
    # better to pay it during the job, not during app startup).
    from opencc import OpenCC

    converter = OpenCC("s2twp")

    def _convert(text: str | None) -> str | None:
        if text is None or text == "":
            return text
        return converter.convert(strip_latex(text) or text)

    new_columns = [
        DataColumn(
            key=col.key,
            label=_convert(col.label) or col.label,
            dtype=col.dtype,
            align=col.align,
        )
        for col in spec.columns
    ]

    new_rows: list[DataRow] = []
    for row in spec.rows:
        new_cells: dict[str, str | int | float | None] = {}
        for k, v in row.cells.items():
            if isinstance(v, str):
                new_cells[k] = _convert(v)
            else:
                # Numbers / None pass through — converting them would
                # corrupt the type info openpyxl uses for number_format.
                new_cells[k] = v
        new_rows.append(DataRow(cells=new_cells))

    return spec.model_copy(
        update={
            "title": _convert(spec.title) or spec.title,
            "subtitle": _convert(spec.subtitle),
            "columns": new_columns,
            "rows": new_rows,
            "notes": _convert(spec.notes),
        }
    )


# ── Pipeline runner ───────────────────────────────────────────────────────


def _artifacts_root() -> Path:
    """Resolve & ensure the datatable artifact directory exists.

    All artifacts land under `<ARTIFACTS_DIR>/datatables/{job_id}.{ext}`
    so cleanup, ls, du queries are easy and the slides pipeline's pptx
    cache stays separate.
    """
    root = Path(settings.ARTIFACTS_DIR) / "datatables"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def _run_pipeline(
    *,
    bearer: str,
    payload: GenerateDatatableRequest,
    updater: jobs.DatatableJobUpdater,
) -> None:
    """Step 1-4 of the pipeline. Runs inside the asyncio.Task.

    Exceptions propagate to the wrapper in datatable_job_service which
    marks the job failed with the message.
    """
    coll = await get_collection(payload.collection_id, bearer=bearer)

    # ── retrieve ──
    await updater.set(step=JOB_STEP_RETRIEVING)
    seed_query = (payload.seed_query or "").strip() or " · ".join(
        [coll.name, payload.preset.value]
        + (
            [payload.extra_instructions.strip()]
            if payload.extra_instructions
            else []
        )
    )
    chunks: list[dict[str, Any]] = []
    try:
        hits = await search_chunks(
            payload.collection_id,
            seed_query,
            top_k=payload.top_k,
            min_score=MIN_SCORE,
            document_ids=payload.document_ids,
            bearer=bearer,
        )
        chunks = [
            {
                "filename": h.filename or "<unknown>",
                "chunk_key": h.chunk_key,
                "content": (h.content or "")[:CONTENT_LIMIT_CHARS],
                "score": float(h.score),
            }
            for h in hits
        ]
    except Exception as exc:  # noqa: BLE001 — retrieval is best-effort
        logger.warning(
            "Datatable retrieval failed (%s); generating without context.", exc,
        )

    # ── generate ──
    await updater.set(step=JOB_STEP_GENERATING)
    spec = await _generate_validated_spec(
        bearer=bearer,
        collection_name=coll.name,
        payload=payload,
        chunks=chunks,
    )

    # ── normalize ──
    await updater.set(step=JOB_STEP_NORMALIZING)
    spec = _normalize_spec_text(spec)
    # Surface title / preset / sizes as soon as we have them so the UI can
    # show "鑄造中:<title>" before export finishes.
    await updater.set(
        title=spec.title,
        preset=spec.preset,
        row_count=len(spec.rows),
        column_count=len(spec.columns),
    )

    # ── export ──
    await updater.set(step=JOB_STEP_EXPORTING)
    root = _artifacts_root()
    job_id = updater.job_id
    html_path = root / f"{job_id}.html"
    csv_path = root / f"{job_id}.csv"
    xlsx_path = root / f"{job_id}.xlsx"

    html_path.write_text(to_html(spec), encoding="utf-8")
    csv_path.write_text(to_csv(spec), encoding="utf-8")
    # to_xlsx writes binary via openpyxl, so it owns the file handle.
    to_xlsx(spec, xlsx_path)

    await updater.mark_done(
        title=spec.title,
        preset=spec.preset,
        row_count=len(spec.rows),
        column_count=len(spec.columns),
        artifact_paths={
            "html": html_path,
            "csv": csv_path,
            "xlsx": xlsx_path,
        },
    )


# ── Download fmt → (mime, suffix) ─────────────────────────────────────────


_DOWNLOAD_MIME: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
}


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/jobs",
    response_model=DatatableJobStatus,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_datatable_job(
    payload: GenerateDatatableRequest,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
    bearer: str = Depends(get_bearer_token),
) -> DatatableJobStatus:
    """Register a datatable job and return its initial status.

    Authorises the collection access up-front (same pattern as slides)
    so the client gets a synchronous 403/404 instead of an opaque
    "failed" job seconds later.
    """
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

    async def _runner(updater: jobs.DatatableJobUpdater) -> None:
        await _run_pipeline(bearer=bearer, payload=payload, updater=updater)

    record = await jobs.create_job(
        user_id=identity.id,
        collection_id=payload.collection_id,
        runner=_runner,
    )
    return record.to_status()


@router.get("/jobs/{job_id}", response_model=DatatableJobStatus)
async def get_datatable_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> DatatableJobStatus:
    """Polling endpoint — returns the current DatatableJobStatus or 404.

    Cross-user access returns 404 (NOT 403) so the existence of a job
    doesn't leak via status code differentiation.
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found (unknown id, evicted, or not yours).",
        )
    return rec.to_status()


@router.get("/jobs/{job_id}/download/{fmt}")
async def download_datatable_artifact(
    job_id: str,
    fmt: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> FileResponse:
    """Serve one of the three exported artifact formats.

    fmt ∈ {html, csv, xlsx}.

    Status codes:
      404 - job missing / not yours / fmt unknown / file gone from disk
      409 - job still running (artifacts not yet exported)
      410 - job failed or cancelled
    """
    if fmt not in _DOWNLOAD_MIME:
        # Unknown format gets 404 (not 400) — keeps it consistent with
        # FastAPI's path-not-found behaviour for typo'd URLs.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown format '{fmt}'. Expected one of: html, csv, xlsx.",
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

    path = rec.artifact_paths.get(fmt)
    if path is None or not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact '{fmt}' not available (file missing).",
        )

    # CJK-safe filename: ASCII fallback + RFC 5987 filename* for browsers.
    from urllib.parse import quote

    raw_title = (rec.title or "datatable").replace('"', "")[:80]
    encoded_title = quote(raw_title, safe="")
    ascii_title = (
        raw_title.encode("ascii", "ignore").decode("ascii").strip()
        or "datatable"
    )
    suffix = fmt
    content_disposition = (
        f'attachment; filename="{ascii_title}.{suffix}"; '
        f"filename*=UTF-8''{encoded_title}.{suffix}"
    )

    return FileResponse(
        path=str(path),
        media_type=_DOWNLOAD_MIME[fmt],
        headers={"Content-Disposition": content_disposition},
    )


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_datatable_job(
    job_id: str,
    identity: CurrentUserIdentity = Depends(get_current_user_identity),
) -> Response:
    """Cancel an in-flight datatable job, or delete a terminal one.

    Semantics match slides: 204 on success even if the job was already
    terminal (frontend deletes its own row regardless). 404 only when
    the job doesn't exist or belongs to someone else.
    """
    rec = jobs.get_user_job(job_id, identity.id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.",
        )
    # Cancel if running, then hard-delete the record + artifacts.
    await jobs.cancel_job(job_id, identity.id)
    await jobs.delete_job(job_id, identity.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

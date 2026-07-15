"""把 anila-agent 包成 CSP 可派工的 OpenAI-compatible HTTP 服務。

端點（平台契約，形狀逐字保留）：
    GET  /health               health 探針（CSP 以此註冊 agent）
    GET  /v1/models            manifest（model_type=agent ← CSP 註冊標記）
    POST /v1/chat/completions   主入口——CSP Router 把對話轉發到這裡

認證（見 serving.auth）：驗 X-CSP-Service-Token（csk-，fail-closed），驗過才信
X-ANILA-User-*；不驗使用者 JWT（Router 不轉發）。

RAG：走 CSP HTTP search（無直連 DB），以 build_agent(retriever=...) 注入。S-Q1 一把
金鑰：search 重用 agent 自己的 csk-（CSP_SEARCH_TOKEN 未設時退回 CSP_SERVICE_TOKEN）。

跑：``uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200``
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote

from anila_contracts import AgentManifest, Classification
from anila_contracts.events import StepKind, StepStatus
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from anila_agent.config import load_config, with_model
from anila_agent.memory.runtime import MemdirRuntime, auto_memory_enabled
from anila_agent.memory.session import build_session
from anila_agent.observability.hooks import AuditHooks
from anila_agent.observability.timeline import (
    TIMELINE_EVENT_NAME,
    TimelineEmitter,
    TimelineRetriever,
    TimelineRunHooks,
)
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.runtime.admission import (
    AgentAdmission,
    AgentAdmissionError,
    admit_startup,
    build_agent_manifest,
    canonical_manifest_json,
)
from anila_agent.runtime.agent_factory import build_agent, validate_e2e_approval_mode
from anila_agent.runtime.model import build_model
from anila_agent.runtime.run import run_once, run_once_state, run_streamed
from anila_agent.runtime.runstate import (
    approve_all,
    dump_state,
    has_interruptions,
    load_state,
    state_from_result,
)
from anila_agent.runtime.task import (
    FileTaskStore,
    IdempotencyConflict,
    TaskRecord,
    TaskStatus,
    TaskStoreConflict,
    TaskStoreCorruption,
    request_digest,
)
from anila_agent.serving.auth import trusted_user_identity, verify_service_token
from anila_agent.tracing import (
    OUTPUT,
    TraceEmitter,
    TracingRetriever,
    TracingRunHooks,
    extract_citations,
)

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("ANILA_AGENT_NAME", "anila-agent")
COLLECTION_ID = int(os.environ.get("ANILA_COLLECTION_ID", "0") or "0")
CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "https://172.16.120.35")
SSL_VERIFY = os.environ.get("ANILA_SSL_VERIFY", "1").lower() not in ("0", "false", "no", "off")
# Inbound: Router 的 X-CSP-Service-Token 必須等於這個（agent 的 csk-）。
# 空 → 預設拒絕（fail-closed）；prod 必設。
CSP_SERVICE_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
# 明確的本地開發 opt-out：未設 token 時允許 inbound。否則未設 token 的 agent 拒絕每次派工。
ALLOW_NO_SERVICE_TOKEN = os.environ.get("ANILA_ALLOW_NO_SERVICE_TOKEN", "") == "1"
# S-Q1 一把金鑰：search 重用 agent 自己的 csk-；CSP_SEARCH_TOKEN 為可選覆寫。
CSP_SEARCH_TOKEN = os.environ.get("CSP_SEARCH_TOKEN", "") or CSP_SERVICE_TOKEN
# 檢索分數門檻；與 CLI from_env 路徑讀同一個 env（ANILA_CSP_MIN_SCORE），預設 0.25。
try:
    CSP_MIN_SCORE = float(os.environ.get("ANILA_CSP_MIN_SCORE", "0.25") or "0.25")
except ValueError:
    CSP_MIN_SCORE = 0.25

# Full Trace（doc-05 §6）：CSP dispatch 帶 X-ANILA-Trace-Id 時，把 run/step/model/tool/
# retrieval/output/error spans callback POST 回 CSP。端點預設 = CSP_BASE_URL；憑證重用
# agent 自己的 csk-（與 RAG 出向同一把 CSP_SEARCH_TOKEN）。無 trace header → emitter 停用
# → 零行為變化。
TRACE_ENDPOINT = os.environ.get("ANILA_TRACE_ENDPOINT", "") or CSP_BASE_URL
TRACE_ENABLED = os.environ.get("ANILA_TRACE_ENABLED", "1").lower() not in (
    "0", "false", "no", "off"
)
# agent 的分類上限（doc-06 §6「classification level」必備 trace 屬性）；隨 run + output span 帶出。
CLASSIFICATION_LEVEL = os.environ.get("ANILA_CLASSIFICATION_LEVEL", "") or None


def _build_emitter(
    trace_id: str | None, task_id: str | None, user_id: str | None
) -> TraceEmitter:
    """由入向 trace header 建 emitter（缺 trace_id/endpoint 時自動停用）。"""
    return TraceEmitter.from_context(
        trace_id=trace_id,
        task_id=task_id,
        user_id=user_id,
        endpoint=TRACE_ENDPOINT,
        api_key=CSP_SEARCH_TOKEN,  # 與 RAG 出向同一把 Agent Integration Key（csk-）
        agent_id=MODEL_NAME,
        enabled=TRACE_ENABLED,
        verify_ssl=SSL_VERIFY,
        classification_level=CLASSIFICATION_LEVEL,
    )


# 未帶 trace header 的路徑用這顆停用 emitter，讓串流程式碼結構一致又零行為變化。
_NULL_EMITTER = TraceEmitter(trace_id=None, endpoint=None, api_key=None, enabled=False)


def _annotate_output(out: Any, answer: str, usage: dict[str, int] | None) -> None:
    """把 final output span 補上 citations / 分類等級 / 長度等屬性（doc-06 §6）。"""
    cites = extract_citations(answer)
    if cites:
        out.attributes["citations"] = cites
        out.attributes["has_citations"] = True
    if CLASSIFICATION_LEVEL:
        out.attributes["classification_level"] = CLASSIFICATION_LEVEL
    out.attributes["output_chars"] = len(answer)
    if usage:
        out.attributes["total_tokens"] = usage.get("total_tokens", 0)


def _usage_from_run_result(result: Any) -> dict[str, int] | None:
    """從 Runner 結果抽 token usage —— 真值或 ``None``（未知）。

    OpenAI Agents SDK 的 ``RunResult`` / ``RunResultStreaming`` 把 token 統計
    放在 ``context_wrapper.usage``（``input_tokens`` / ``output_tokens`` /
    ``total_tokens``）。抽不到、或值全為 0（真實完成不可能零耗用）→ 回
    ``None``，呼叫端**省略 usage 欄位／不送 usage chunk**。

    不能退回全 0 物件：CSP 消費端（proxy service 的 ``chunk.get("usage")``）
    把任何非空 usage dict 當權威值，全 0 會蓋掉它的本地 token 估算、記帳歸零。
    缺欄位時 CSP 會落回估算，才是對消費端誠實的行為。
    """
    usage_obj = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage_obj is None:
        return None
    usage = {
        "prompt_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
        "completion_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
        "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
    }
    if not any(usage.values()):
        return None
    return usage


_CONFIG: Any = None
_MODEL: Any = None  # 共用的 OpenAIChatCompletionsModel（避免每請求新建 httpx client）
_ADMISSION: AgentAdmission | None = None
_TASK_STORE: FileTaskStore | None = None
_ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}
_ACTIVE_TIMELINES: dict[str, TimelineEmitter] = {}
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
# CSP issues 32 random bytes through ``secrets.token_urlsafe(32)``.  Its
# canonical service-token wire representation is therefore exactly
# ``csk-`` + 43 base64url characters.  R5 must not promote a generic string
# (or a development placeholder) into durable-resume authority merely because
# it is non-empty.
_CSP_SERVICE_TOKEN = re.compile(r"^csk-[A-Za-z0-9_-]{43}$")
_SERVICE_TOKEN_PLACEHOLDERS = frozenset(
    {
        "",
        "not-set",
        "changeme",
        "dev-service-token",
        "dev-secret-key-change-in-prod",
        "change-me",
        "change_me",
        "placeholder",
        "replace-me",
        "replace_me",
        "none",
        "null",
        "undefined",
        "<openssl rand -hex 32>",
        "<openssl rand -base64 24>",
        "<openssl rand -base64 32>",
    }
)


def _is_canonical_csp_service_token(value: object) -> bool:
    """Return whether *value* has CSP's bounded, non-placeholder csk shape.

    This is deliberately only a local admission prerequisite: CSP remains the
    authority that validates the credential against its provisioned record at
    dispatch time.  The shape check still prevents an accidental arbitrary
    value from advertising R5 resume support before that authority boundary.
    """

    if not isinstance(value, str) or value != value.strip():
        return False
    return bool(
        value.lower() not in _SERVICE_TOKEN_PLACEHOLDERS
        and _CSP_SERVICE_TOKEN.fullmatch(value)
    )


def _validate_csp_service_token_for_admission() -> bool:
    """Validate token posture before admitting formal durable resume.

    An unset token remains an explicit local-development/non-resumable mode.
    In contrast, an explicitly configured placeholder or malformed token is a
    configuration error and must fail process startup; ``ALLOW_NO`` must never
    turn it into an authority-bearing R5 instance.
    """

    if not CSP_SERVICE_TOKEN:
        return False
    if not _is_canonical_csp_service_token(CSP_SERVICE_TOKEN):
        raise AgentAdmissionError(
            "CSP_SERVICE_TOKEN 必須是 CSP 發行的 csk- 加 43 個 base64url 字元；"
            "不得使用 placeholder 或任意字串"
        )
    return True


def _manifest_for_discovery() -> AgentManifest:
    """Return the admitted declaration, or a validated pre-start projection."""

    if _ADMISSION is not None:
        return _ADMISSION.manifest
    return build_agent_manifest(_service_config())


def _require_admission() -> AgentAdmission:
    """Keep invocation fail-closed until official startup admission succeeded."""

    if _ADMISSION is None or _MODEL is None or _TASK_STORE is None:
        raise HTTPException(status_code=503, detail="agent admission not ready")
    return _ADMISSION


def _resume_is_admitted() -> bool:
    """Return true only for CSP-authenticated durable-resume service mode."""

    return bool(
        _ADMISSION is not None
        and _ADMISSION.manifest.supports_resume
        and _is_canonical_csp_service_token(CSP_SERVICE_TOKEN)
        and not ALLOW_NO_SERVICE_TOKEN
        and _TASK_STORE is not None
    )


def _require_correlation_id(value: str | None, *, header_name: str) -> str:
    """Require a CSP-issued, single-line correlation identifier."""

    if value is None or not _CORRELATION_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"missing or invalid {header_name}")
    return value


def _require_idempotency_key(value: str | None, *, header_name: str) -> str:
    """Require a bounded idempotency key rather than accepting arbitrary input."""

    if value is None or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"missing or invalid {header_name}")
    return value


def _bind_dispatch_context(
    *,
    agent_id: str | None,
    task_id: str | None,
    run_id: str | None,
    session_id: str | None,
    invocation_id: str | None,
    trace_id: str | None,
    classification_level: str | None,
) -> tuple[AgentManifest, str, str, str, str, str, Classification]:
    """Validate the CSP-owned invocation correlation before any Agent work.

    The service token authenticates the caller; these headers bind that caller
    to exactly this admitted Agent and one durable Task/Run/Session/Invocation
    chain.  Local defaults would create an untraceable execution, so Silver
    dispatch rejects missing or malformed values instead of manufacturing them.
    """

    manifest = _require_admission().manifest
    if agent_id != manifest.agent_id:
        raise HTTPException(status_code=403, detail="agent dispatch target does not match this host")
    bound_task_id = _require_correlation_id(task_id, header_name="X-ANILA-Task-Id")
    bound_run_id = _require_correlation_id(run_id, header_name="X-ANILA-Run-Id")
    bound_session_id = _require_correlation_id(session_id, header_name="X-ANILA-Session-Id")
    bound_invocation_id = _require_correlation_id(
        invocation_id, header_name="X-ANILA-Invocation-Id"
    )
    bound_trace_id = _require_correlation_id(trace_id, header_name="X-ANILA-Trace-Id")
    if classification_level is None:
        raise HTTPException(status_code=400, detail="missing X-ANILA-Classification-Level")
    try:
        classification = Classification.from_storage(unquote(classification_level))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid X-ANILA-Classification-Level") from None
    if classification > manifest.classification.ceiling:
        raise HTTPException(status_code=403, detail="classification exceeds agent admission ceiling")
    return (
        manifest,
        bound_task_id,
        bound_run_id,
        bound_session_id,
        bound_invocation_id,
        bound_trace_id,
        classification,
    )


def _service_config() -> Any:
    """Resolve the official service model through CSP by default.

    ``ANILA_BASE_URL`` is intentionally the explicit override.  The old
    package YAML points at a local vLLM for standalone experiments; service
    mode must never silently inherit that raw endpoint.
    """

    cfg = load_config()
    if not os.environ.get("ANILA_BASE_URL"):
        cfg = with_model(cfg, base_url=f"{CSP_BASE_URL.rstrip('/')}/v1")
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _CONFIG, _MODEL, _ADMISSION, _TASK_STORE
    # A module can be reloaded by a worker supervisor.  Do not let a previous
    # failed/retired admission leak into a new readiness probe.
    _CONFIG = None
    _MODEL = None
    _ADMISSION = None
    _TASK_STORE = None
    _ACTIVE_TASKS.clear()
    _ACTIVE_TIMELINES.clear()
    try:
        # Reject an ambient E2E approval toggle before readiness is exposed;
        # production must never inherit a test-only tool policy from the host.
        validate_e2e_approval_mode()
    except ValueError as exc:
        raise AgentAdmissionError(str(exc)) from exc
    if not CSP_SERVICE_TOKEN:
        if ALLOW_NO_SERVICE_TOKEN:
            logger.warning(
                "CSP_SERVICE_TOKEN unset + ANILA_ALLOW_NO_SERVICE_TOKEN=1 — inbound "
                "service-token verification DISABLED (local dev only)."
            )
        else:
            logger.error(
                "CSP_SERVICE_TOKEN unset — every dispatch will be REJECTED (401, "
                "fail-closed). Set the agent's csk-, or ANILA_ALLOW_NO_SERVICE_TOKEN=1 "
                "for local dev only."
            )
    # A token is optional only for explicitly non-resumable local development.
    # Once configured, it must be a bounded CSP service token regardless of
    # ANILA_ALLOW_NO_SERVICE_TOKEN; accepting a placeholder here would let the
    # process falsely advertise formal R5 authority.
    formal_token_configured = _validate_csp_service_token_for_admission()
    if not SSL_VERIFY:
        # 自簽憑證時翻 litellm 的全域 ssl_verify（僅在裝了 [litellm] extra 時）。
        try:
            import litellm

            litellm.ssl_verify = False
        except ImportError:
            pass
    _CONFIG = _service_config()
    _TASK_STORE = FileTaskStore(_CONFIG.home / "tasks")
    try:
        _TASK_STORE.ensure_writable()
        _TASK_STORE.recover()
    except (OSError, TaskStoreCorruption) as exc:
        _TASK_STORE = None
        raise AgentAdmissionError("durable FileTaskStore 無法寫入或恢復") from exc
    resume_requested = formal_token_configured and not ALLOW_NO_SERVICE_TOKEN
    try:
        _ADMISSION = admit_startup(
            _CONFIG,
            csp_base_url=CSP_BASE_URL,
            supports_resume=resume_requested,
        )
    except AgentAdmissionError:
        _TASK_STORE = None
        logger.exception("official Agent startup admission failed")
        raise
    _MODEL = build_model(
        _CONFIG.model,
        csp_base_url=CSP_BASE_URL,
        require_csp_endpoint=True,
    )  # 一次建立、全程重用底層 httpx client
    if COLLECTION_ID <= 0:
        logger.info(
            "ANILA_COLLECTION_ID 未設或 <=0（got %s）——以 non-RAG Silver Agent 啟動。",
            COLLECTION_ID,
        )
    try:
        yield
    finally:
        for active in tuple(_ACTIVE_TASKS.values()):
            if not active.done():
                active.cancel()
        _ACTIVE_TASKS.clear()
        _ACTIVE_TIMELINES.clear()
        # shutdown：關閉共用 model 的底層 client，避免 fd 殘留。
        client = getattr(_MODEL, "_client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        _MODEL = None
        _ADMISSION = None
        _TASK_STORE = None


app = FastAPI(title=MODEL_NAME, lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    # OpenAI 標準也允許 content 為 parts 陣列（多模態：text/image_url/…）。
    # 這個 agent 不支援影像輸入——array content 只抽 text parts（見
    # ``_extract_text_content`` / ``_has_only_non_text_parts``），純非文字
    # parts 在 chat_completions 內擋 422。
    content: str | list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: StreamOptions | None = None


def _extract_text_content(content: str | list[dict[str, Any]] | None) -> str:
    """把 OpenAI content（純字串或多模態 parts 陣列）攤平成純文字。

    array content 只抽 ``{"type": "text", "text": ...}`` parts、依序串接；
    其他 part 型別（``image_url`` 等）——此 agent 不支援影像輸入——靜默忽略。
    整則訊息只有非文字 parts 的情況由呼叫端 ``_has_only_non_text_parts`` 先擋
    422，不會走到這裡的「攤平成空字串」。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _has_only_non_text_parts(content: str | list[dict[str, Any]] | None) -> bool:
    """content 是非空 parts 陣列、但一個 text part 都沒有（例如純圖片）。"""
    if not isinstance(content, list) or not content:
        return False
    return not any(isinstance(part, dict) and part.get("type") == "text" for part in content)


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "manifest_schema_version": "agent-manifest/v1",
    }


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Fail closed until official startup admission has completed."""

    admission = _require_admission()
    return {"status": "ready", "manifest_schema_version": admission.manifest.schema_version}


@app.get("/.well-known/anila-agent.json")
async def agent_manifest() -> Response:
    """Expose the canonical, schema-validated formal Agent declaration."""

    return Response(
        content=canonical_manifest_json(_manifest_for_discovery()),
        media_type="application/json",
    )


@app.post("/v1/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
) -> dict[str, str]:
    """Durably cancel a live Task and interrupt its local stream if present."""

    if not verify_service_token(
        x_csp_service_token, CSP_SERVICE_TOKEN, allow_unset=ALLOW_NO_SERVICE_TOKEN
    ):
        raise HTTPException(status_code=401, detail="missing or invalid X-CSP-Service-Token")
    if _TASK_STORE is None:
        raise HTTPException(status_code=503, detail="agent task store not ready")
    try:
        timeline = _ACTIVE_TIMELINES.get(task_id)
        terminal_event: dict[str, Any] | None = None
        if timeline is not None:
            before = len(timeline.events)
            timeline.cancel()
            if len(timeline.events) > before:
                terminal_event = timeline.events[-1].model_dump(mode="json")
        record = _TASK_STORE.cancel(task_id, terminal_event=terminal_event)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except (TaskStoreCorruption, ValueError):
        raise HTTPException(status_code=503, detail="durable task state is corrupt") from None
    active = _ACTIVE_TASKS.get(task_id)
    if active is not None and not active.done():
        active.cancel()
    return {"task_id": record.task_id, "status": record.status.value}


@app.post("/v1/tasks/{task_id}/approve")
@app.post("/v1/tasks/{task_id}/resume", include_in_schema=False)
async def approve_and_resume_task(
    task_id: str,
    request: Request,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_user_id: str | None = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: str | None = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: str | None = Header(default=None, alias="X-ANILA-User-Groups"),
    x_anila_trace_id: str | None = Header(default=None, alias="X-ANILA-Trace-Id"),
    x_anila_task_id: str | None = Header(default=None, alias="X-ANILA-Task-Id"),
    x_anila_agent_id: str | None = Header(default=None, alias="X-ANILA-Agent-Id"),
    x_anila_session_id: str | None = Header(default=None, alias="X-ANILA-Session-Id"),
    x_anila_run_id: str | None = Header(default=None, alias="X-ANILA-Run-Id"),
    x_anila_invocation_id: str | None = Header(
        default=None, alias="X-ANILA-Invocation-Id"
    ),
    x_anila_idempotency_key: str | None = Header(
        default=None, alias="X-ANILA-Idempotency-Key"
    ),
    x_anila_classification_level: str | None = Header(
        default=None, alias="X-ANILA-Classification-Level"
    ),
) -> Any:
    """Resume one paused SDK state after CSP has granted the HITL transition.

    This endpoint deliberately has no client-controlled ``approve`` body.  A
    direct caller cannot self-authorize a pending tool/action: only CSP can
    reach it with the per-agent service credential and the canonical dispatch
    binding.  CSP remains the authority that evaluates admin/HITL policy and
    decides whether to make this call.
    """

    if not _resume_is_admitted():
        raise HTTPException(status_code=503, detail="durable resume is not admitted for this agent")
    if not verify_service_token(x_csp_service_token, CSP_SERVICE_TOKEN, allow_unset=False):
        raise HTTPException(status_code=401, detail="missing or invalid X-CSP-Service-Token")
    # R5 uses an explicit binary approval contract.  The Agent never accepts a
    # client-provided interruption list or answer: CSP has already made the
    # policy decision, and the persisted SDK RunState remains the sole source
    # of pending interruptions.  Unknown/legacy body fields are rejected
    # before state loading rather than being silently ignored.
    # Parse the body ourselves so malformed JSON cannot be silently converted
    # into an empty approval.  A genuinely empty body is the only shorthand
    # accepted (TestClient/HTTP clients may omit Content-Length); any declared
    # non-zero body that is empty is malformed and fails closed.
    try:
        raw_resume_bytes = await request.body()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="resume body 無法讀取") from exc
    if not raw_resume_bytes.strip():
        content_length = request.headers.get("content-length")
        if content_length not in (None, "0"):
            raise HTTPException(status_code=400, detail="resume body JSON 無效")
        raw_resume_body: Any = {}
    else:
        try:
            raw_resume_body = json.loads(raw_resume_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="resume body JSON 無效") from exc
    if not isinstance(raw_resume_body, dict):
        raise HTTPException(status_code=400, detail="resume body 必須是 object")
    unknown_resume_fields = set(raw_resume_body) - {"approval_mode"}
    if unknown_resume_fields:
        raise HTTPException(status_code=400, detail="resume body 含未定義欄位")
    approval_mode = raw_resume_body.get("approval_mode", "approve_all")
    if approval_mode != "approve_all":
        raise HTTPException(status_code=400, detail="resume approval_mode 必須是 approve_all")
    (
        manifest,
        bound_task_id,
        run_id,
        session_id,
        invocation_id,
        trace_id,
        timeline_classification,
    ) = _bind_dispatch_context(
        agent_id=x_anila_agent_id,
        task_id=x_anila_task_id,
        run_id=x_anila_run_id,
        session_id=x_anila_session_id,
        invocation_id=x_anila_invocation_id,
        trace_id=x_anila_trace_id,
        classification_level=x_anila_classification_level,
    )
    if bound_task_id != task_id:
        raise HTTPException(status_code=403, detail="task path does not match dispatch binding")
    resume_idempotency_key = _require_idempotency_key(
        x_anila_idempotency_key, header_name="X-ANILA-Idempotency-Key"
    )
    if _TASK_STORE is None:
        raise HTTPException(status_code=503, detail="agent task store not ready")
    try:
        existing = _TASK_STORE.get(task_id)
        _validate_record_binding(
            existing,
            manifest=manifest,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            invocation_id=invocation_id,
            classification=timeline_classification,
        )
        record, owns_execution = _TASK_STORE.claim_resume(
            task_id,
            idempotency_key=resume_idempotency_key,
            request_hash=request_digest(
                {
                    "operation": "csp_approve_resume/v1",
                    "agent_id": manifest.agent_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "session_id": session_id,
                    "invocation_id": invocation_id,
                    "classification": timeline_classification.value,
                }
            ),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except IdempotencyConflict:
        raise HTTPException(status_code=409, detail="resume idempotency key conflicts with task") from None
    except TaskStoreCorruption:
        raise HTTPException(status_code=503, detail="durable task state is corrupt") from None
    except ValueError:
        # Validation of the header happened above; the remaining expected
        # ValueError is a lifecycle mismatch (not paused/runnable).
        raise HTTPException(status_code=409, detail="task cannot be resumed in its current state") from None

    if not owns_execution:
        if record.status is TaskStatus.COMPLETED:
            return _response_from_task(record)
        if record.status is TaskStatus.PAUSED:
            return _paused_task_response(record)
        if record.status is TaskStatus.CANCELLED:
            raise HTTPException(status_code=409, detail="task was cancelled")
        if record.status is TaskStatus.FAILED:
            raise HTTPException(status_code=409, detail="task failed before resume completed")
        raise HTTPException(status_code=409, detail="task resume is already running")

    # The service-token assertion is the authority boundary.  Only after it
    # succeeds may this process trust caller identity headers for audit/memory.
    identity = trusted_user_identity(
        True,
        user_id=x_anila_user_id,
        email=x_anila_user_email,
        groups=x_anila_user_groups,
    )
    assembled, request_session, emitter, timeline, hooks = _build_invocation_runtime(
        manifest=manifest,
        identity=identity,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        classification=timeline_classification,
    )
    timeline.set_sequence_floor(
        max((int(event.get("sequence", 0)) for event in record.events), default=0)
    )
    try:
        current = asyncio.current_task()
        if current is not None:
            _ACTIVE_TASKS[record.task_id] = current
        _ACTIVE_TIMELINES[record.task_id] = timeline
        if not record.state_string:
            raise RuntimeError("paused task lost its durable RunState")
        state = await load_state(
            assembled.agent,
            record.state_string,
            context_override=assembled.context,
        )
        # No user-supplied approval list is accepted.  CSP made this request
        # only after policy; the persisted SDK state determines exactly what
        # remains pending.
        approve_all(state, list(state.get_interruptions()))
        timeline.emit(
            step_id=f"agent:{timeline.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.RUNNING,
            safe_input_summary="CSP 核准後恢復單一 Task",
        )
        result = await run_once_state(assembled, state, session=request_session, hooks=hooks)
        if has_interruptions(result):
            paused = _pause_task(
                record,
                timeline,
                result,
                summary="等待 CSP 再次核准後恢復",
            )
            if paused is None or paused.status is not TaskStatus.PAUSED:
                raise HTTPException(status_code=409, detail="task changed while resuming")
            return _paused_task_response(paused)

        answer = getattr(result, "final_output", None) or ""
        timeline.emit(
            step_id=f"agent:{timeline.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.COMPLETED,
            safe_output_summary="Task 恢復後執行完成",
            terminal=True,
        )
        finished = _finish_task(record, timeline, status=TaskStatus.COMPLETED, result=answer)
        if finished is None or finished.status is not TaskStatus.COMPLETED:
            raise HTTPException(status_code=409, detail="task changed while resuming")
        memory = getattr(getattr(assembled, "context", None), "memory", None)
        _schedule_absorb(memory, str(record.history[-1].get("content", "")), answer)
        return _response_from_task(finished)
    except asyncio.CancelledError:
        _finish_task(
            record,
            timeline,
            status=TaskStatus.CANCELLED,
            error_code="cancelled_by_caller",
        )
        raise HTTPException(status_code=409, detail="task was cancelled") from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("durable task resume failed task_id=%s", task_id)
        timeline.fail()
        _finish_task(record, timeline, status=TaskStatus.FAILED, error_code="resume_failed")
        raise HTTPException(status_code=500, detail="task resume failed") from None
    finally:
        _ACTIVE_TASKS.pop(record.task_id, None)
        _ACTIVE_TIMELINES.pop(record.task_id, None)
        await emitter.flush()


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    # model_type=agent 是 CSP 註冊 manifest 標記；勿改欄位名/形狀。
    # created/owned_by 是 OpenAI /v1/models 規格必含欄位，補上但不動既有欄位。
    manifest = _manifest_for_discovery()
    return {
        "object": "list",
        "data": [{
            "id": MODEL_NAME,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "anila-agent",
            "model_type": "agent",
            # Keep the OpenAI model projection above for existing CSP clients;
            # the canonical declaration is nested and validated by
            # ``AgentManifest`` on both sides of registration.
            "agent_manifest": manifest.model_dump(mode="json"),
        }],
    }


# 背景抽取任務的強參考集合，避免被 GC（asyncio 只保 weakref）。
_ABSORB_TASKS: set[asyncio.Task[Any]] = set()


def _schedule_absorb(memory: MemdirRuntime | None, user_text: str, answer_text: str) -> None:
    """turn 結束後在背景抽取記憶寫入該租戶 store——不阻塞回應。

    gated by ANILA_AUTO_MEMORY；memory 為 None（無記憶/缺身分）或無回答則跳過。
    寫入走 memory.absorb_turn（已去敏、fail-closed），失敗只記 log。
    """
    if memory is None or not answer_text or not auto_memory_enabled():
        return

    async def _run() -> None:
        try:
            await memory.absorb_turn(user_text, answer_text)
        except Exception:
            logger.warning("auto-memory absorb failed", exc_info=True)

    task = asyncio.create_task(_run())
    _ABSORB_TASKS.add(task)
    task.add_done_callback(_ABSORB_TASKS.discard)


def _chunk(
    cid: str,
    created: int,
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> str:
    """組一個 OpenAI chat.completion.chunk 的 SSE 區塊（``data: ...\\n\\n``）。"""
    payload: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _usage_chunk(cid: str, created: int, usage: dict[str, int]) -> str:
    """組 usage-only 的 OpenAI chat.completion.chunk（``choices: []``）。

    OpenAI 標準行為：只在 client 帶 ``stream_options.include_usage=true`` 時才
    送這塊，位置在 finish chunk 之後、``[DONE]`` 之前；``choices`` 必須是空
    陣列（不是帶 delta 的正常 choice）。
    """
    payload: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [],
        "usage": usage,
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _begin_task(
    req: ChatCompletionRequest,
    user_prompt: str,
    *,
    task_id: str,
    run_id: str,
    session_id: str,
    invocation_id: str,
    idempotency_key: str,
    agent_id: str,
    classification: Classification,
) -> tuple[TaskRecord | None, bool]:
    """Create/replay the durable service task before any model call."""

    if _TASK_STORE is None:
        return None, True
    record = _TASK_STORE.create(
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        request_hash=request_digest(
            {
                "request": req.model_dump(mode="json"),
                # A key must not replay one session's governed answer into a
                # different session/Agent/classification context.
                "agent_id": agent_id,
                "session_id": session_id,
                "classification": classification.value,
            }
        ),
    )
    if record.terminal or record.status is TaskStatus.PAUSED:
        return record, False
    if record.status is TaskStatus.RUNNING:
        return record, False
    record.status = TaskStatus.RUNNING
    if not record.history:
        record.history = [message.model_dump(mode="json") for message in req.messages]
        if not record.history:
            record.history = [{"role": "user", "content": user_prompt}]
    return _TASK_STORE.save(record), True


def _capture_task_events(record: TaskRecord, timeline: TimelineEmitter | None) -> None:
    if timeline is None:
        return
    known = {str(item.get("event_id")) for item in record.events}
    for event in timeline.events:
        if event.event_id not in known:
            record.events.append(event.model_dump(mode="json"))


def _finish_task(
    record: TaskRecord | None,
    timeline: TimelineEmitter | None,
    *,
    status: TaskStatus,
    result: Any = None,
    error_code: str | None = None,
) -> TaskRecord | None:
    if record is None or _TASK_STORE is None:
        return record
    record.status = status
    if result is not None:
        record.result = result
        record.history.append({"role": "assistant", "content": str(result)})
    if error_code is not None:
        record.error = error_code
    _capture_task_events(record, timeline)
    try:
        return _TASK_STORE.save(record)
    except TaskStoreConflict:
        # A concurrent cancel wins over an old runner completion.  Refresh the
        # canonical record rather than surfacing a raw storage exception or
        # resurrecting a terminal task.
        try:
            current = _TASK_STORE.get(record.task_id)
        except (FileNotFoundError, TaskStoreCorruption):
            logger.warning("durable task finalization lost its record task_id=%s", record.task_id)
            return None
        logger.warning(
            "durable task finalization lost CAS race task_id=%s status=%s",
            record.task_id,
            current.status.value,
        )
        return current


def _pause_task(
    record: TaskRecord | None,
    timeline: TimelineEmitter | None,
    result: Any,
    *,
    summary: str,
) -> TaskRecord | None:
    """Persist an SDK interruption before exposing a resumable pause.

    The durable state is written before a ``202`` / terminal SSE boundary is
    emitted.  An agent must never advertise a pause that cannot survive a
    process restart.  This helper intentionally does not manufacture a
    terminal StepEvent: ``blocked`` remains resumable, while the final resumed
    outcome owns the one-way terminal latch.
    """

    if record is None:
        raise RuntimeError("durable task record is required for a resumable pause")
    record.state_string = dump_state(state_from_result(result))
    if not record.state_string:
        raise RuntimeError("SDK interruption did not produce a durable RunState")
    record.status = TaskStatus.PAUSED
    record.error = None
    if timeline is not None:
        timeline.emit(
            step_id=f"agent:{timeline.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.BLOCKED,
            safe_output_summary=summary,
        )
    return _finish_task(record, timeline, status=TaskStatus.PAUSED)


def _response_from_task(record: TaskRecord) -> dict[str, Any]:
    answer = str(record.result or "")
    return {
        "id": f"chatcmpl-{record.invocation_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        # Events have already been validated/rebound by FileTaskStore on both
        # write and read.  Returning the canonical history lets CSP rebuild its
        # own durable SessionEventStore after an approve/resume call without
        # trusting arbitrary Agent response fields.
        "anila_events": list(record.events),
        "anila_meta": {
            "task_id": record.task_id,
            "run_id": record.run_id,
            "session_id": record.session_id,
            "invocation_id": record.invocation_id,
            "status": record.status.value,
        },
    }


def _paused_task_response(record: TaskRecord) -> JSONResponse:
    """Return an explicit durable-HITL pause instead of an empty fake answer."""

    return JSONResponse(
        status_code=202,
        content={
            "object": "anila.task",
            "task_id": record.task_id,
            "run_id": record.run_id,
            "session_id": record.session_id,
            "invocation_id": record.invocation_id,
            "status": TaskStatus.PAUSED.value,
            # CSP consumes only this validated StepEvent projection; no raw
            # SDK state or interruption object crosses the trust boundary.
            "anila_events": list(record.events),
            "anila_meta": {
                "task_id": record.task_id,
                "run_id": record.run_id,
                "session_id": record.session_id,
                "invocation_id": record.invocation_id,
                "status": TaskStatus.PAUSED.value,
            },
        },
    )


async def _replay_task_stream(record: TaskRecord) -> AsyncIterator[str]:
    response = _response_from_task(record)
    cid = str(response["id"])
    created = int(response["created"])
    yield _chunk(cid, created, delta={"role": "assistant"})
    # Records are validated as canonical StepEvents by FileTaskStore before
    # this point, so a restart replay retains the exact named timeline rather
    # than reducing a governed run to plain text.
    for event in record.events:
        yield (
            f"event: {TIMELINE_EVENT_NAME}\n"
            f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
        )
    answer = str(record.result or "")
    if answer:
        yield _chunk(cid, created, delta={"content": answer})
    yield _chunk(cid, created, delta={}, finish_reason="stop")
    yield "data: [DONE]\n\n"


async def _run_once_with_session(
    assembled: Any,
    user_prompt: str | list[Any],
    *,
    session: Any,
    hooks: Any,
) -> Any:
    params = inspect.signature(run_once).parameters
    kwargs: dict[str, Any] = {"hooks": hooks}
    if "session" in params or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    ):
        kwargs["session"] = session
    return await run_once(assembled, user_prompt, **kwargs)


def _run_streamed_with_session(
    assembled: Any,
    user_prompt: str | list[Any],
    *,
    session: Any,
    hooks: Any,
) -> Any:
    params = inspect.signature(run_streamed).parameters
    kwargs: dict[str, Any] = {"hooks": hooks}
    if "session" in params or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    ):
        kwargs["session"] = session
    return run_streamed(assembled, user_prompt, **kwargs)


async def _sse_stream(
    assembled: Any,
    run_input: str | list[Any],
    hooks: AuditHooks,
    *,
    emitter: TraceEmitter | None = None,
    timeline: TimelineEmitter | None = None,
    include_usage: bool = False,
    session: Any = None,
    task_record: TaskRecord | None = None,
    memory_user_prompt: str = "",
) -> AsyncIterator[str]:
    """agent 串流輸出 → OpenAI SSE：role → content deltas → finish → [usage] → ``[DONE]``。

    只轉發 ResponseTextDeltaEvent（最終可見答案）；工具呼叫 / reasoning 軌跡不外送。
    usage 在串流跑完才定案（SDK 註明 context_wrapper.usage 末包前為 stale），故收尾才讀。
    Router 端（proxy_service.proxy_stream）以 ``resp.aiter_lines()`` 逐行解析這個格式。

    OpenAI 標準：finish chunk（``choices`` 非空、帶 ``finish_reason``）本身不帶
    usage；usage 只在 ``include_usage`` 為真（client 帶
    ``stream_options.include_usage: true``）時，才在 finish chunk 之後、
    ``[DONE]`` 之前補一個獨立的 usage-only chunk（``choices: []``）。CSP 轉發
    agent 串流時一律強制 ``stream_options.include_usage=True``（見
    ``services/csp/app/services/proxy/service.py`` 的
    ``_proxy_stream_impl``），所以正式部署（經 CSP）路徑行為不變；差別只在
    「繞過 CSP 直打 agent 且未帶 include_usage」的呼叫——現在不會再收到
    usage（符合 OpenAI 規格；先前版本無條件把 usage 掛在 finish chunk 上）。
    ``proxy_service.py`` 的解析只認 ``chunk.get("usage")``，不管 usage 掛在
    finish chunk 還是獨立 chunk，兩種形狀都相容。

    Full Trace 為 out-of-band callback（POST 回 CSP），不動 SSE 格式；``emitter`` 未給或
    停用時完全 no-op。run/model/tool/retrieval/output/error spans 於此收攏 flush。
    """
    em = emitter if emitter is not None else _NULL_EMITTER
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    if task_record is not None:
        current = asyncio.current_task()
        if current is not None:
            _ACTIVE_TASKS[task_record.task_id] = current
        if timeline is not None:
            _ACTIVE_TIMELINES[task_record.task_id] = timeline
    yield _chunk(cid, created, delta={"role": "assistant"})
    if timeline is not None:
        timeline.emit(
            step_id=f"agent:{timeline.agent_id}",
            kind=StepKind.AGENT,
            status=StepStatus.RUNNING,
            safe_input_summary="開始單一 Task 執行",
        )
        # Silver's stream carries both the OpenAI chunks and the canonical
        # named StepEvent projection.  Dropping this initial frame made a
        # stream look complete while its durable timeline was incomplete.
        for frame in timeline.drain():
            yield frame

    parts: list[str] = []  # 累積最終答案，供 turn 結束後自動抽取記憶。
    async with em.run_span(MODEL_NAME, attributes={"stream": True}):
        result = _run_streamed_with_session(
            assembled, run_input, session=session, hooks=hooks
        )
        try:
            async for event in result.stream_events():
                if timeline is not None:
                    for frame in timeline.drain():
                        yield frame
                if (
                    event.type == "raw_response_event"
                    and isinstance(event.data, ResponseTextDeltaEvent)
                    and event.data.delta
                ):
                    parts.append(event.data.delta)
                    yield _chunk(cid, created, delta={"content": event.data.delta})
            if timeline is not None:
                for frame in timeline.drain():
                    yield frame
        except GeneratorExit:
            # Starlette may cancel the task, while direct async-generator
            # consumers inject GeneratorExit via ``aclose()``.  It is unsafe
            # to yield during GeneratorExit, but the durable state still must
            # latch cancelled before the generator closes.
            result.cancel(mode="immediate")
            if timeline is not None:
                timeline.cancel()
            _finish_task(task_record, timeline, status=TaskStatus.CANCELLED, error_code="cancelled_by_caller")
            if task_record is not None:
                _ACTIVE_TASKS.pop(task_record.task_id, None)
                _ACTIVE_TIMELINES.pop(task_record.task_id, None)
            raise
        except asyncio.CancelledError:
            # A CSP cancel request targets this generator's task.  Stop the
            # SDK run and emit the canonical terminal frame once before ending
            # the OpenAI stream cleanly; do not leak a cancellation traceback.
            result.cancel(mode="immediate")
            if timeline is not None:
                timeline.cancel()
                for frame in timeline.drain():
                    yield frame
            _finish_task(
                task_record,
                timeline,
                status=TaskStatus.CANCELLED,
                error_code="cancelled_by_caller",
            )
            if task_record is not None:
                _ACTIVE_TASKS.pop(task_record.task_id, None)
                _ACTIVE_TIMELINES.pop(task_record.task_id, None)
            # The helper is also used directly by SDK-facing unit consumers.
            # Preserve their cancellation signal rather than converting it to
            # a normal completion; the HTTP route has a durable task record
            # and can end its SSE body cleanly after latching cancellation.
            if task_record is None:
                raise
            yield "data: [DONE]\n\n"
            return
        except Exception:
            # 串流中途失敗：headers 已送出、status 無法再改，記錄 + error span 後乾淨收尾。
            logger.exception("streaming run failed mid-flight")
            em.error("stream_failed")
            if timeline is not None:
                timeline.fail()
                for frame in timeline.drain():
                    yield frame
            _finish_task(task_record, timeline, status=TaskStatus.FAILED, error_code="stream_failed")
            if task_record is not None:
                _ACTIVE_TASKS.pop(task_record.task_id, None)
                _ACTIVE_TIMELINES.pop(task_record.task_id, None)
            # Keep the pre-existing OpenAI stream contract: an interrupted
            # response still has a finish frame before its terminal [DONE].
            yield _chunk(cid, created, delta={}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        if has_interruptions(result):
            if not _resume_is_admitted():
                # A no-token/dev profile must never leave an unreachable
                # paused task behind.  The stream has already started, so
                # express the fail-closed outcome through its canonical event
                # and latch it durably before ending the SSE response.
                logger.error("SDK requested pause while durable resume is not admitted")
                if timeline is not None:
                    timeline.fail()
                    for frame in timeline.drain():
                        yield frame
                _finish_task(
                    task_record,
                    timeline,
                    status=TaskStatus.FAILED,
                    error_code="resume_not_admitted",
                )
                if task_record is not None:
                    _ACTIVE_TASKS.pop(task_record.task_id, None)
                    _ACTIVE_TIMELINES.pop(task_record.task_id, None)
                yield "data: [DONE]\n\n"
                await em.flush()
                return
            paused_task = _pause_task(
                task_record,
                timeline,
                result,
                summary="等待 CSP 核准後恢復",
            )
            if timeline is not None:
                for frame in timeline.drain():
                    yield frame
            if task_record is not None:
                _ACTIVE_TASKS.pop(task_record.task_id, None)
                _ACTIVE_TIMELINES.pop(task_record.task_id, None)
            # Do not fabricate a normal OpenAI completion: CSP consumes the
            # named blocked StepEvent and subsequently calls the authenticated
            # approve/resume endpoint when policy grants execution.
            yield "data: [DONE]\n\n"
            await em.flush()
            if paused_task is None or paused_task.status is not TaskStatus.PAUSED:
                return
            return

        usage_payload = _usage_from_run_result(result)
        yield _chunk(cid, created, delta={}, finish_reason="stop")
        if include_usage and usage_payload is not None:
            yield _usage_chunk(cid, created, usage_payload)
        answer_text = "".join(parts)
        if timeline is not None:
            timeline.emit(
                step_id=f"agent:{timeline.agent_id}",
                kind=StepKind.AGENT,
                status=StepStatus.COMPLETED,
                safe_output_summary="Task 執行完成",
                terminal=True,
            )
            for frame in timeline.drain():
                yield frame
        finished_task = _finish_task(task_record, timeline, status=TaskStatus.COMPLETED, result=answer_text)
        if task_record is not None:
            _ACTIVE_TASKS.pop(task_record.task_id, None)
            _ACTIVE_TIMELINES.pop(task_record.task_id, None)
        if finished_task is not None and finished_task.status is not TaskStatus.COMPLETED:
            # A terminal cancel/failed state won the CAS race.  Do not claim a
            # successful final answer after the authoritative state changed.
            yield "data: [DONE]\n\n"
            return
        yield "data: [DONE]\n\n"
        async with em.span(OUTPUT, MODEL_NAME) as out:
            _annotate_output(out, answer_text, usage_payload)
    await em.flush()

    # 答案已全部串出 → 背景抽取記憶（不延後回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, memory_user_prompt, answer_text)


def _build_invocation_runtime(
    *,
    manifest: AgentManifest,
    identity: dict[str, str | None],
    task_id: str,
    run_id: str,
    session_id: str,
    invocation_id: str,
    trace_id: str,
    classification: Classification,
) -> tuple[Any, Any, TraceEmitter, TimelineEmitter, Any]:
    """Rebuild the same admitted runtime for an initial call or durable resume."""

    if _CONFIG is None or _MODEL is None:
        raise HTTPException(status_code=503, detail="agent admission not ready")
    request_session = build_session(_CONFIG, session_id=session_id)
    emitter = _build_emitter(trace_id, task_id, identity.get("user_id"))
    if COLLECTION_ID > 0:
        retriever: Any = CspHttpRetriever(
            csp_base_url=CSP_BASE_URL,
            collection_id=COLLECTION_ID,
            api_key=CSP_SEARCH_TOKEN,
            min_score=CSP_MIN_SCORE,
            verify_ssl=SSL_VERIFY,
        )
    else:
        # Collection scope is required only for an Agent that actually
        # declares retrieval.  A non-RAG formal Agent remains invokable.
        retriever = DummyRetriever()
    timeline = TimelineEmitter(
        task_id=task_id,
        trace_id=trace_id,
        agent_id=manifest.agent_id,
        session_id=session_id,
        run_id=run_id,
        classification=classification,
        invocation_id=invocation_id,
    )
    retriever = TimelineRetriever(retriever, timeline)
    if emitter.active:
        retriever = TracingRetriever(retriever, emitter)
    uid = (identity.get("user_id") or "").strip()
    email = (identity.get("email") or "").strip()
    assembled = build_agent(
        _CONFIG,
        retriever=retriever,
        name=MODEL_NAME,
        model=_MODEL,
        memory_tenant=uid or email or None,
        memory_requires_tenant=True,
    )
    audit = AuditHooks(user_id=identity.get("user_id"))
    timeline_hooks = TimelineRunHooks(timeline, inner=audit)
    hooks: Any = TracingRunHooks(emitter, inner=timeline_hooks) if emitter.active else timeline_hooks
    return assembled, request_session, emitter, timeline, hooks


def _validate_record_binding(
    record: TaskRecord,
    *,
    manifest: AgentManifest,
    task_id: str,
    run_id: str,
    session_id: str,
    invocation_id: str,
    classification: Classification,
) -> None:
    """Make resume reject a stale/cross-agent task before SDK state loading."""

    if (
        record.task_id != task_id
        or record.run_id != run_id
        or record.session_id != session_id
        or record.invocation_id != invocation_id
    ):
        raise HTTPException(status_code=403, detail="task correlation does not match dispatch binding")
    for event in record.events:
        if (
            event.get("agent_id") != manifest.agent_id
            or event.get("classification") != classification.value
        ):
            raise HTTPException(status_code=403, detail="task governance binding does not match dispatch")


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_user_id: str | None = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: str | None = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: str | None = Header(default=None, alias="X-ANILA-User-Groups"),
    x_anila_trace_id: str | None = Header(default=None, alias="X-ANILA-Trace-Id"),
    x_anila_task_id: str | None = Header(default=None, alias="X-ANILA-Task-Id"),
    x_anila_agent_id: str | None = Header(default=None, alias="X-ANILA-Agent-Id"),
    x_anila_session_id: str | None = Header(default=None, alias="X-ANILA-Session-Id"),
    x_anila_run_id: str | None = Header(default=None, alias="X-ANILA-Run-Id"),
    x_anila_invocation_id: str | None = Header(
        default=None, alias="X-ANILA-Invocation-Id"
    ),
    x_anila_idempotency_key: str | None = Header(
        default=None, alias="X-ANILA-Idempotency-Key"
    ),
    x_anila_classification_level: str | None = Header(
        default=None, alias="X-ANILA-Classification-Level"
    ),
) -> Any:
    allowed = verify_service_token(
        x_csp_service_token, CSP_SERVICE_TOKEN, allow_unset=ALLOW_NO_SERVICE_TOKEN
    )
    if not allowed:
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-CSP-Service-Token. The CSP Router dispatch sends "
                "the agent's csk- automatically; for a direct local test send the header "
                "(or set ANILA_ALLOW_NO_SERVICE_TOKEN=1 to disable)."
            ),
        )
    (
        manifest,
        task_id,
        run_id,
        session_id,
        invocation_id,
        trace_id,
        timeline_classification,
    ) = _bind_dispatch_context(
        agent_id=x_anila_agent_id,
        task_id=x_anila_task_id,
        run_id=x_anila_run_id,
        session_id=x_anila_session_id,
        invocation_id=x_anila_invocation_id,
        trace_id=x_anila_trace_id,
        classification_level=x_anila_classification_level,
    )
    # 驗過 service token 後才信任 X-ANILA-User-* 身分。
    identity = trusted_user_identity(
        allowed, user_id=x_anila_user_id, email=x_anila_user_email, groups=x_anila_user_groups
    )

    last_user_msg = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if last_user_msg is None:
        raise HTTPException(status_code=400, detail="no user message in `messages`")
    if _has_only_non_text_parts(last_user_msg.content):
        raise HTTPException(
            status_code=422,
            detail=(
                "此 agent 不支援影像輸入：訊息 content 只含非文字 part（例如 "
                "image_url），請改用純文字查詢，或在 array content 中附上至少一個 "
                "{\"type\": \"text\", ...} part。"
            ),
        )
    user_prompt = _extract_text_content(last_user_msg.content)
    if not user_prompt:
        raise HTTPException(status_code=400, detail="no user message in `messages`")

    idempotency_key = x_anila_idempotency_key or f"task:{task_id}"
    _require_idempotency_key(idempotency_key, header_name="X-ANILA-Idempotency-Key")
    try:
        task_record, is_new_task = _begin_task(
            req,
            user_prompt,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            invocation_id=invocation_id,
            idempotency_key=idempotency_key,
            agent_id=manifest.agent_id,
            classification=timeline_classification,
        )
    except IdempotencyConflict:
        raise HTTPException(status_code=409, detail="idempotency key conflicts with existing task") from None
    except TaskStoreCorruption:
        raise HTTPException(status_code=503, detail="durable task state is corrupt") from None
    if task_record is not None and not is_new_task:
        if task_record.status is TaskStatus.COMPLETED:
            if req.stream:
                return StreamingResponse(
                    _replay_task_stream(task_record),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return _response_from_task(task_record)
        if task_record.status is TaskStatus.RUNNING:
            raise HTTPException(status_code=409, detail="task is already running")
        if task_record.status is TaskStatus.CANCELLED:
            raise HTTPException(status_code=409, detail="task was cancelled")
        if task_record.status is TaskStatus.PAUSED:
            if _resume_is_admitted():
                return _paused_task_response(task_record)
            raise HTTPException(status_code=409, detail="task is paused; resume is not admitted")

    assembled, request_session, emitter, timeline, hooks = _build_invocation_runtime(
        manifest=manifest,
        identity=identity,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        classification=timeline_classification,
    )
    # The SDK receives the complete OpenAI history for a new session rather
    # than only the last user sentence.  Session storage then preserves the
    # same canonical input across subsequent turns/restarts.
    run_input: list[dict[str, Any]] = [message.model_dump(mode="json") for message in req.messages]

    # 串流：CSP Router 對 agent 強制 stream=true 並逐行解析 OpenAI SSE
    # （proxy_service.proxy_stream）。回 text/event-stream 的 chat.completion.chunk。
    if req.stream:
        include_usage = bool(req.stream_options and req.stream_options.include_usage)
        return StreamingResponse(
            _sse_stream(
                assembled,
                run_input,
                hooks,
                emitter=emitter,
                timeline=timeline,
                include_usage=include_usage,
                session=request_session,
                task_record=task_record,
                memory_user_prompt=user_prompt,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非串流：Router 對 agent 回應走 resp.json()（也容忍 SSE，但我們回 JSON）。
    try:
        if task_record is not None:
            current = asyncio.current_task()
            if current is not None:
                _ACTIVE_TASKS[task_record.task_id] = current
            _ACTIVE_TIMELINES[task_record.task_id] = timeline
        async with emitter.run_span(MODEL_NAME, attributes={"stream": False}):
            if timeline is not None:
                timeline.emit(
                    step_id=f"agent:{timeline.agent_id}",
                    kind=StepKind.AGENT,
                    status=StepStatus.RUNNING,
                    safe_input_summary="開始單一 Task 執行",
                )
            result = await _run_once_with_session(
                assembled, run_input, session=request_session, hooks=hooks
            )
            if has_interruptions(result):
                if not _resume_is_admitted():
                    # Do not retain a pause that no authorized CSP caller can
                    # ever resume.  This is intentionally a hard failure for
                    # no-token/dev profiles rather than a local opt-in.
                    if timeline is not None:
                        timeline.fail()
                    _finish_task(
                        task_record,
                        timeline,
                        status=TaskStatus.FAILED,
                        error_code="resume_not_admitted",
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="SDK pause requires CSP-admitted durable resume",
                    )
                paused_task = _pause_task(
                    task_record,
                    timeline,
                    result,
                    summary="等待 CSP 核准後恢復",
                )
                if paused_task is None or paused_task.status is not TaskStatus.PAUSED:
                    raise HTTPException(status_code=409, detail="task changed while pausing")
                return _paused_task_response(paused_task)
            answer = result.final_output or ""
            usage_payload = _usage_from_run_result(result)
            async with emitter.span(OUTPUT, MODEL_NAME) as out:
                _annotate_output(out, answer, usage_payload)
            if timeline is not None:
                timeline.emit(
                    step_id=f"agent:{timeline.agent_id}",
                    kind=StepKind.AGENT,
                    status=StepStatus.COMPLETED,
                    safe_output_summary="Task 執行完成",
                    terminal=True,
                )
            finished_task = _finish_task(
                task_record, timeline, status=TaskStatus.COMPLETED, result=answer
            )
            if finished_task is not None and finished_task.status is not TaskStatus.COMPLETED:
                raise HTTPException(status_code=409, detail="task was cancelled before completion")
    except asyncio.CancelledError:
        _finish_task(task_record, timeline, status=TaskStatus.CANCELLED, error_code="cancelled_by_caller")
        raise HTTPException(status_code=409, detail="task was cancelled") from None
    except HTTPException:
        raise
    except Exception:
        _finish_task(task_record, timeline, status=TaskStatus.FAILED, error_code="execution_failed")
        raise
    finally:
        if task_record is not None:
            _ACTIVE_TASKS.pop(task_record.task_id, None)
            _ACTIVE_TIMELINES.pop(task_record.task_id, None)
        await emitter.flush()
    # 回應建好後在背景抽取記憶（不延後 JSON 回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, user_prompt, answer)
    response: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
    }
    # usage 只在拿得到真值時附上；未知時省略讓 CSP 落回本地估算
    # （見 _usage_from_run_result 的說明）。
    if usage_payload is not None:
        response["usage"] = usage_payload
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8200")))

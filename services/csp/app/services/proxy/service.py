"""Proxy orchestration: forward requests to model backends with retry + timeout.

Split out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). ``proxy_request`` / ``proxy_stream`` /
``build_default_anila_meta`` bodies are verbatim; the helpers they call now
live in the sibling modules of this package (headers / sse / usage / guard).
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import HTTPException
import httpx
from anila_security import ENDPOINT_KIND_AGENT, ENDPOINT_KIND_MODEL

from app.config import settings
from app.models.model_registry import ModelRegistry
from app.models.task import Task, TaskRun
from app.services.proxy import spans
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.headers import (
    _apply_gateway_auth,
    build_agent_headers,
    build_model_gateway_headers,
    resolve_model_gateway_key,
)
from app.services.proxy.sse import _aggregate_sse_to_chat_completion, _parse_sse_block
from app.services.proxy.task_link import (
    finalize_task_run,
    finalize_task_run_in_session,
)
from app.services.proxy.usage import (
    _estimate_token_count,
    _extract_response_text,
    _extract_stream_text,
    _serialize_request_for_usage,
    enqueue_usage_task_linked,
)
from app.services.usage_writer import enqueue_usage

# Keep the pre-split logger channel ("app.services.proxy_service") so log
# routing / filtering / capture behavior is identical after the package split.
logger = logging.getLogger("app.services.proxy_service")


def _finalize_proxy_task_run(
    *, governance_db, task_run_id: int | None, status: str,
    error: dict | None = None,
) -> None:
    """Finalize on the session that owns admission locks when available."""
    if task_run_id is None:
        return
    if governance_db is None:
        finalize_task_run(task_run_id, status, error=error)
        return
    try:
        finalize_task_run_in_session(
            governance_db, task_run_id, status, error=error
        )
    except Exception:
        governance_db.rollback()
        logger.exception(
            "same-session TaskRun finalization failed run_id=%s status=%s",
            task_run_id,
            status,
        )


def _require_pilot_sink_admission(
    *,
    inference_callsite_id: str | None,
    task_id: int | None,
    task_run_id: int | None,
    governance_db,
    admitted_classification_level: str | None,
) -> None:
    """Last-hop pilot admission, before SSRF resolution or network I/O.

    API handlers are allowed to perform an earlier user-friendly check, but
    this sink is authoritative: a new caller cannot reach inference in pilot
    mode without an enabled inventory id, Task, DB authority and ceiling
    snapshot.
    """
    if not settings.ANILA_PILOT_MODE:
        return
    if not isinstance(inference_callsite_id, str) or not inference_callsite_id.strip():
        raise HTTPException(status_code=403, detail="Gate 2 pilot 缺少 inference callsite")
    from app.services.startup_security import require_pilot_callsite

    try:
        require_pilot_callsite(inference_callsite_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if task_id is None:
        raise HTTPException(status_code=403, detail="Gate 2 pilot 推論必須綁定 Task")
    if task_run_id is None:
        raise HTTPException(status_code=403, detail="Gate 2 pilot 推論必須綁定 TaskRun")
    if governance_db is None or admitted_classification_level is None:
        raise HTTPException(
            status_code=403,
            detail="Gate 2 pilot 缺少可信分類 ceiling admission",
        )


def _lock_task_run_admission(
    *, governance_db, task_id: int | None, task_run_id: int | None,
    admitted_classification_level: str | None = None,
) -> str | None:
    """Lock the active attempt so cancel/finalize cannot race outbound."""
    if governance_db is None or task_id is None or task_run_id is None:
        return admitted_classification_level
    try:
        task = (
            governance_db.query(Task)
            .filter(Task.id == task_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        run = (
            governance_db.query(TaskRun)
            .filter(TaskRun.id == task_run_id, TaskRun.task_id == task_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="TaskRun governance lock 失敗，已拒絕出向呼叫",
        ) from exc
    if task is None or run is None or task.status != "running" or run.status != "running":
        raise HTTPException(
            status_code=409,
            detail="Task/TaskRun 已非 running，禁止終態後發出推論呼叫",
        )
    from anila_contracts import Classification

    try:
        levels = [
            Classification.from_storage(task.classification_level),
            Classification.from_storage(run.classification_level),
        ]
        if admitted_classification_level is not None:
            levels.append(Classification.from_storage(admitted_classification_level))
        effective = Classification.max_of(levels)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail="Task/TaskRun 分類治理狀態無效，已拒絕出向呼叫",
        ) from exc
    if Classification.from_storage(run.classification_level) < effective:
        run.classification_level = effective.to_storage()
        run.classification_latched_at = datetime.now(timezone.utc)
        run.classification_source = "outbound_admission"
    return effective.to_storage()


def lock_task_run_admission(
    *, governance_db, task_id: int | None, task_run_id: int | None,
) -> str | None:
    """Public helper for the legacy non-streaming Agent branch."""
    return _lock_task_run_admission(
        governance_db=governance_db,
        task_id=task_id,
        task_run_id=task_run_id,
    )


def _lock_registry_admission(
    *,
    governance_db,
    registry_model_id: int | None,
    registry_agent_id: int | None,
    registry_endpoint_url: str | None,
    admitted_classification_level: str | None,
    inference_callsite_id: str | None = None,
):
    """Re-read and lock the exact registry row used for outbound.

    This closes the model-registry TOCTOU window: an endpoint swap or ceiling
    downgrade committed after the handler's first check is observed here and
    denied; once this query succeeds, ``FOR UPDATE`` remains held by the
    request session until the outbound operation completes.
    """
    if (
        governance_db is None
        or (registry_model_id is None and registry_agent_id is None)
        or admitted_classification_level is None
    ):
        return
    from anila_contracts import Classification as ClassificationLevel
    from app.modules.policy import evaluate_classification_ceiling
    from app.models.agent import Agent

    try:
        if registry_agent_id is not None:
            locked = (
                governance_db.query(Agent)
                .filter(Agent.id == registry_agent_id)
                .populate_existing()
                .with_for_update(read=True)
                .one_or_none()
            )
            active = locked is not None and locked.approval_status == "approved"
        else:
            locked = (
                governance_db.query(ModelRegistry)
                .filter(ModelRegistry.id == registry_model_id)
                .populate_existing()
                .with_for_update(read=True)
                .one_or_none()
            )
            active = locked is not None and bool(locked.is_active)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="模型治理登錄無法鎖定，已拒絕出向呼叫",
        ) from exc
    if not active:
        raise HTTPException(status_code=403, detail="推論目標已停用或治理登錄不存在")
    if locked.endpoint_url != registry_endpoint_url:
        raise HTTPException(
            status_code=409,
            detail="模型端點在 admission 後已變更，已拒絕出向呼叫",
        )
    if settings.ANILA_PILOT_MODE:
        from app.services.startup_security import (
            require_pilot_classification,
            require_pilot_target,
        )

        if not isinstance(inference_callsite_id, str):
            raise HTTPException(status_code=403, detail="Gate 2 pilot 缺少 target callsite")
        try:
            require_pilot_target(
                callsite=inference_callsite_id,
                name=str(locked.name),
                model_type=(
                    "agent" if registry_agent_id is not None else str(locked.model_type)
                ),
                endpoint_url=str(locked.endpoint_url),
                classification_ceiling=str(locked.classification_ceiling),
            )
            require_pilot_classification(str(admitted_classification_level))
        except RuntimeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        level = ClassificationLevel.from_storage(admitted_classification_level)
        ceiling = ClassificationLevel.from_storage(locked.classification_ceiling)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail="模型分類治理狀態無效，已拒絕出向呼叫",
        ) from exc
    if not evaluate_classification_ceiling(
        task_level=level.to_storage(), ceiling=ceiling.to_storage()
    ):
        raise HTTPException(
            status_code=403,
            detail="模型分類上限在 admission 後降低，已拒絕出向呼叫",
        )
    # Deliberately do not commit/rollback here: the request-owned session
    # holds this row lock across the immediately following async outbound and
    # releases it when the request transaction ends.
    return locked


def lock_agent_registry_admission(
    *, governance_db, agent_id: int, endpoint_url: str,
    admitted_classification_level: str,
) -> None:
    """Public helper for the legacy non-streaming Agent branch."""
    _lock_registry_admission(
        governance_db=governance_db,
        registry_model_id=None,
        registry_agent_id=agent_id,
        registry_endpoint_url=endpoint_url,
        admitted_classification_level=admitted_classification_level,
        inference_callsite_id="csp.agent_dispatch",
    )

def _get_timeout(model_type: str) -> float:
    if model_type == "embedding":
        return settings.EMBEDDING_TIMEOUT
    return settings.LLM_TIMEOUT


def build_default_anila_meta(
    source_name: str,
    *,
    detail: str,
    latency_ms: int | None = None,
    classified: bool = False,
    usage: dict | None = None,
) -> dict:
    """Build an anila_meta skeleton used when the downstream omits one.

    The ``classified`` flag is a one-way latch. It is driven solely by the
    resolved **agent's** ``requires_encryption`` attribute (base LLMs do not
    carry this flag). Downstreams that set ``classified=true`` in their own
    ``anila.meta`` are authoritative; this default only fills the baseline
    when nothing is emitted.
    """
    return {
        "trace_id": f"trace-{int(time.time() * 1000)}",
        "trace": [
            {
                "kind": "call",
                "label": f"呼叫 {source_name}",
                "detail": detail,
                "status": "ok",
            }
        ],
        "citations": [],
        "confidence": None,
        "handoff_chain": [],
        "follow_ups": [],
        "latency_ms": latency_ms,
        "classified": bool(classified),
        # Token usage so the chat UI can show per-message token counts. Omitted
        # (None) when unknown; the client only renders when present.
        "usage": usage,
    }


def _emit_proxy_dispatch_spans(
    *,
    trace_id: Optional[str],
    task_id: Optional[int],
    started_at: datetime,
    run_status: str,
    is_agent: bool,
    target_id: Optional[int],
    target_name: Optional[str],
    usage: Optional[dict] = None,
) -> None:
    """Slice 4a — emit the proxy.dispatch + model/agent call spans for a
    task-linked proxied call. ``run_status`` is the TaskRun terminal status
    (``completed`` → span ``ok``; anything else → ``error``). Best-effort:
    ``spans.record_proxy_dispatch`` already logs-not-raises, the extra guard
    here keeps any unexpected error from touching the proxied response."""
    span_status = "ok" if run_status == "completed" else "error"
    try:
        spans.record_proxy_dispatch(
            trace_id=trace_id,
            task_id=task_id,
            started_at=started_at,
            ended_at=datetime.now(timezone.utc),
            status=span_status,
            is_agent=is_agent,
            target_id=target_id,
            target_name=target_name,
            usage=usage,
        )
    except Exception:  # pragma: no cover - defensive; helper is log-not-raise
        logger.exception("proxy span 記錄失敗 trace_id=%s", trace_id)


async def _proxy_request_impl(
    model: ModelRegistry,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    request_body: dict,
    endpoint_path: str,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_trace_id: Optional[str] = None,
    legacy_runtime_call: bool = False,
    inference_callsite_id: str | None = None,
    governance_db=None,
    admitted_classification_level: str | None = None,
    task_run_id: int | None = None,
) -> dict:
    """Forward request to model backend with exponential backoff retry.

    Sprint 5 / Chunk W: classifies the call as ``request_type='embedding'``
    when the path looks like an embedding endpoint, ``'judge'`` when an
    explicit ``X-Anila-Request-Type: judge`` header is set upstream, else
    'chat'. The classification rides into ``token_usage`` for split-by-kind
    dashboards.

    Slice 2b-C: ``task_id`` / ``task_trace_id`` ride onto AGENT dispatch
    headers only (doc 05 §4; doc 04 AC5 forbids them toward the model
    gateway) and into the usage row; ``legacy_runtime_call`` marks task-less
    /v1 chat traffic. Run finalization lives in the ``proxy_request``
    wrapper.
    """
    _require_pilot_sink_admission(
        inference_callsite_id=inference_callsite_id,
        task_id=task_id,
        task_run_id=task_run_id,
        governance_db=governance_db,
        admitted_classification_level=admitted_classification_level,
    )
    timeout = _get_timeout(model.model_type)
    base_url = model.endpoint_url.rstrip("/")
    request_type = (
        "embedding"
        if "embedding" in endpoint_path or model.model_type == "embedding"
        else "image"
        if "images/generations" in endpoint_path or model.model_type == "image"
        else "chat"
    )

    # Sprint 5 / Chunk W: AUTO_REGISTER_MODELS has historically set
    # endpoint_url with a trailing ``/v1`` (e.g. ``http://...:11434/v1``);
    # endpoint_path also starts with ``/v1`` (``/v1/embeddings``).
    # Naive concat → ``//v1/v1/embeddings`` → upstream 404. Strip the
    # trailing version segment from base_url when endpoint_path already
    # carries one. Idempotent for already-stripped urls.
    if base_url.endswith("/v1") and endpoint_path.startswith("/v1/"):
        base_url = base_url[:-3]
    elif base_url.endswith("/v2") and endpoint_path.startswith("/v2/"):
        base_url = base_url[:-3]

    # Determine the correct path based on api_version
    if model.api_version == "v2" and "embedding" in endpoint_path:
        target_url = f"{base_url}/v2/embeddings"
    else:
        target_url = f"{base_url}{endpoint_path}"

    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    _guard_outbound(
        target_url,
        endpoint_kind=(
            ENDPOINT_KIND_AGENT if model.model_type == "agent" else ENDPOINT_KIND_MODEL
        ),
    )

    last_error = None
    start_time = time.time()

    # ``user_id`` (DB PK) is for usage attribution only; the wire identity
    # is ``user_identity`` (員編). Builder chosen by DESTINATION (not a caller
    # flag) so the model gateway can never receive the CSP service token:
    # agent → full identity + token; model/embedding → 員編-only.
    if model.model_type == "agent":
        req_headers = build_agent_headers(
            user_identity,
            user_email,
            target_agent_id=target_agent_id,
            task_id=task_id,
            trace_id=task_trace_id,
        )
    else:
        # Doc 04 §3/AC5: model gateway gets Bearer key + 員編 ONLY — no
        # task / trace headers, structurally (builder has no such params).
        req_headers = build_model_gateway_headers(user_identity)
    # gateway key 只給 model 呼叫;agent dispatch (model_type='agent') 不帶。
    # Slice 6a: per-model api_key_secret_ref 優先,退回全域 env(MVP fallback)。
    if model.model_type != "agent":
        _apply_gateway_auth(req_headers, resolve_model_gateway_key(model))

    for attempt in range(settings.PROXY_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    target_url,
                    json=request_body,
                    headers=req_headers,
                )

            duration_ms = int((time.time() - start_time) * 1000)

            if response.status_code >= 500:
                # Full upstream body stays server-side only; the client gets a
                # generic message so internal errors / stack traces never leak.
                last_error = f"後端回應 {response.status_code}: {response.text[:500]}"
                if attempt < settings.PROXY_MAX_RETRIES - 1:
                    delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"模型 {model.name} 回應 {response.status_code}，"
                        f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("模型 %s 上游 5xx: %s", model.name, last_error)
                raise HTTPException(status_code=502, detail="模型服務暫時不可用")

            if response.status_code >= 400:
                # Log the full upstream body server-side; forward only a
                # sanitized message (OpenAI-shape error.message when present),
                # never the raw text/JSON which can leak internal detail.
                logger.warning(
                    "模型 %s 上游 %s: %s",
                    model.name,
                    response.status_code,
                    response.text[:500],
                )
                detail = f"模型服務拒絕請求 (HTTP {response.status_code})"
                try:
                    body = response.json()
                    msg = (
                        body.get("error", {}).get("message")
                        if isinstance(body, dict)
                        else None
                    )
                    if isinstance(msg, str) and msg:
                        detail = msg[:300]
                except Exception:
                    pass
                raise HTTPException(status_code=response.status_code, detail=detail)

            # Fallback: upstream returned SSE despite our non-stream request
            # (some agents — e.g. asrd — only speak streaming). Aggregate the
            # deltas into one OpenAI-shape chat.completion object so the
            # caller gets JSON.
            content_type = response.headers.get("content-type", "")
            body_preview = response.text[:8].lstrip()
            if "text/event-stream" in content_type or body_preview.startswith("data:"):
                result = _aggregate_sse_to_chat_completion(response.text, model.name)
            else:
                result = response.json()

            # Extract token usage from response
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            if not usage:
                prompt_tokens = _estimate_token_count(
                    model.name, _serialize_request_for_usage(request_body)
                )
                completion_tokens = _estimate_token_count(
                    model.name, _extract_response_text(result)
                )
                total_tokens = prompt_tokens + completion_tokens
                logger.warning(
                    "模型 %s 非串流回應未提供 usage，改用伺服端估算: prompt=%s completion=%s",
                    model.name,
                    prompt_tokens,
                    completion_tokens,
                )
            existing_meta = result.get("anila_meta")
            if not existing_meta:
                result["anila_meta"] = build_default_anila_meta(
                    model.name,
                    detail=f"Proxy -> {target_url}",
                    latency_ms=duration_ms,
                    classified=requires_encryption,
                )
            elif requires_encryption and isinstance(existing_meta, dict):
                # One-way latch: upgrade to classified when the resolved model
                # requires encryption, even if the downstream omitted the flag.
                existing_meta["classified"] = True

            # Enqueue usage record (non-blocking).
            # Sprint 5 / Chunk W: ``request_type`` flows from the
            # endpoint-path classification at the top of this function so
            # embedding rows get tagged distinct from chat rows.
            # Slice 2b-C: task-linked / legacy-marked /v1 chat rows go
            # through the task-aware variant; every other caller keeps the
            # byte-identical legacy enqueue path.
            if task_id is not None or legacy_runtime_call:
                await enqueue_usage_task_linked(
                    api_key_id=api_key_id,
                    user_id=user_id,
                    department_id=department_id,
                    model_id=model.id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    request_duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    request_type=request_type,
                    caller_agent_id=caller_agent_id,
                    caller_client_id=caller_client_id,
                    task_id=task_id,
                    legacy_runtime_call=legacy_runtime_call,
                )
            else:
                await enqueue_usage(
                    api_key_id=api_key_id,
                    user_id=user_id,
                    department_id=department_id,
                    model_id=model.id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    request_duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    request_type=request_type,
                    caller_agent_id=caller_agent_id,
                    caller_client_id=caller_client_id,
                )

            return result

        except httpx.TimeoutException:
            last_error = f"請求逾時 ({timeout}s)"
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"模型 {model.name} 請求逾時，"
                    f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
                continue

        except httpx.ConnectError:
            last_error = f"無法連線到模型端點 {target_url}"
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"模型 {model.name} 連線失敗，"
                    f"{delay}s 後重試 ({attempt + 1}/{settings.PROXY_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
                continue

        except HTTPException:
            raise

        except Exception as e:
            last_error = str(e)
            logger.error(f"代理請求錯誤: {e}")
            if attempt < settings.PROXY_MAX_RETRIES - 1:
                delay = settings.PROXY_RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

    raise HTTPException(
        status_code=502,
        detail=f"模型服務不可用，已重試 {settings.PROXY_MAX_RETRIES} 次: {last_error}",
    )


async def proxy_request(
    model: ModelRegistry,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    request_body: dict,
    endpoint_path: str,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_trace_id: Optional[str] = None,
    task_run_id: Optional[int] = None,
    legacy_runtime_call: bool = False,
    inference_callsite_id: str | None = None,
    governance_db=None,
    admitted_classification_level: str | None = None,
    finalize_task_run_on_completion: bool = True,
) -> dict:
    """Public entrypoint — ``_proxy_request_impl`` plus Slice 2b-C TaskRun
    finalization: when the call belongs to a Task (``task_run_id`` set),
    the run is marked completed on success / failed on any HTTP error
    (including SSRF-guard rejections and exhausted retries). No-op — and
    byte-identical behavior — for legacy task-less callers.
    """
    span_started_at = datetime.now(timezone.utc)
    is_agent_target = model.model_type == "agent" or target_agent_id is not None
    try:
        _require_pilot_sink_admission(
            inference_callsite_id=inference_callsite_id,
            task_id=task_id,
            task_run_id=task_run_id,
            governance_db=governance_db,
            admitted_classification_level=admitted_classification_level,
        )
        effective_level = _lock_task_run_admission(
            governance_db=governance_db,
            task_id=task_id,
            task_run_id=task_run_id,
            admitted_classification_level=admitted_classification_level,
        )
        _lock_registry_admission(
            governance_db=governance_db,
            registry_model_id=getattr(model, "id", None),
            registry_agent_id=None,
            registry_endpoint_url=getattr(model, "endpoint_url", None),
            admitted_classification_level=effective_level,
            inference_callsite_id=inference_callsite_id,
        )
        result = await _proxy_request_impl(
            model=model,
            api_key_id=api_key_id,
            user_id=user_id,
            department_id=department_id,
            request_body=request_body,
            endpoint_path=endpoint_path,
            user_email=user_email,
            user_identity=user_identity,
            conversation_id=conversation_id,
            trace_id=trace_id,
            requires_encryption=requires_encryption,
            target_agent_id=target_agent_id,
            caller_agent_id=caller_agent_id,
            caller_client_id=caller_client_id,
            task_id=task_id,
            task_trace_id=task_trace_id,
            legacy_runtime_call=legacy_runtime_call,
            inference_callsite_id=inference_callsite_id,
            governance_db=governance_db,
            admitted_classification_level=admitted_classification_level,
            task_run_id=task_run_id,
        )
    except HTTPException as exc:
        if task_run_id is not None and finalize_task_run_on_completion:
            _finalize_proxy_task_run(
                governance_db=governance_db,
                task_run_id=task_run_id,
                status="failed",
                error={
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                },
            )
            # Slice 4a: record the dispatch even on failure (error status).
            _emit_proxy_dispatch_spans(
                trace_id=task_trace_id,
                task_id=task_id,
                started_at=span_started_at,
                run_status="failed",
                is_agent=is_agent_target,
                target_id=(target_agent_id if is_agent_target else model.id),
                target_name=model.name,
            )
        elif task_run_id is not None and governance_db is not None:
            governance_db.rollback()
        raise
    if task_run_id is not None and finalize_task_run_on_completion:
        _finalize_proxy_task_run(
            governance_db=governance_db,
            task_run_id=task_run_id,
            status="completed",
        )
        # Slice 4a: usage tokens (when the upstream returned them) ride into
        # the model/agent child span attributes.
        _emit_proxy_dispatch_spans(
            trace_id=task_trace_id,
            task_id=task_id,
            started_at=span_started_at,
            run_status="completed",
            is_agent=is_agent_target,
            target_id=(target_agent_id if is_agent_target else model.id),
            target_name=model.name,
            usage=(result.get("usage") if isinstance(result, dict) else None),
        )
    elif task_run_id is not None and governance_db is not None:
        # Nested retrieval/memory inference is governed by the outer TaskRun,
        # but must not terminalize it. Commit only the short admission
        # transaction so its Task/registry row locks are released.
        governance_db.commit()
    return result


async def _proxy_stream_impl(
    target_url: str,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    usage_model_id: int,
    request_body: dict,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    model_name: str | None = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_trace_id: Optional[str] = None,
    legacy_runtime_call: bool = False,
    gateway_api_key: Optional[str] = None,
    inference_callsite_id: str | None = None,
    governance_db=None,
    admitted_classification_level: str | None = None,
    task_run_id: int | None = None,
) -> AsyncIterator[str]:
    """Stream SSE response from a downstream backend through CSP proxy.

    Intercepts the final usage chunk to record token consumption, then
    forwards all SSE chunks verbatim to the caller. If usage is missing,
    performs a server-side token estimate from request/response text.

    Slice 2b-C: ``task_id`` / ``task_trace_id`` ride onto AGENT dispatch
    headers only (doc 05 §4; doc 04 AC5 forbids them toward the model
    gateway) and into the usage row; ``legacy_runtime_call`` marks task-less
    /v1 chat traffic. Run finalization lives in the ``proxy_stream`` wrapper.
    """
    _require_pilot_sink_admission(
        inference_callsite_id=inference_callsite_id,
        task_id=task_id,
        task_run_id=task_run_id,
        governance_db=governance_db,
        admitted_classification_level=admitted_classification_level,
    )
    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    _guard_outbound(
        target_url,
        endpoint_kind=(
            ENDPOINT_KIND_AGENT if target_agent_id is not None else ENDPOINT_KIND_MODEL
        ),
    )

    # ``user_id`` (DB PK) is for usage attribution only; ``user_identity``
    # (員編) is the wire identity. Builder chosen by DESTINATION
    # (target_agent_id) — never a caller flag — so the model gateway can
    # never receive the CSP service token.
    if target_agent_id is not None:
        headers = build_agent_headers(
            user_identity,
            user_email,
            target_agent_id=target_agent_id,
            task_id=task_id,
            trace_id=task_trace_id,
        )
    else:
        # Doc 04 §3/AC5: model gateway gets Bearer key + 員編 ONLY — no
        # task / trace headers, structurally (builder has no such params).
        headers = build_model_gateway_headers(user_identity)
    # gateway key 只給 model 串流;agent 串流 (target_agent_id 非 None) 不帶。
    # Slice 6a: 呼叫端已解析 per-model key(proxy.py 傳入 gateway_api_key);
    # None → _apply_gateway_auth 退回全域 env(既有行為)。
    if target_agent_id is None:
        _apply_gateway_auth(headers, gateway_api_key)
    # Force stream_options so the downstream sends usage in last chunk
    body = {**request_body, "stream": True,
            "stream_options": {"include_usage": True}}

    start_time = time.time()
    prompt_tokens = completion_tokens = 0
    usage_seen = False
    meta_seen = False
    prompt_text = _serialize_request_for_usage(body)
    completion_parts: list[str] = []
    pending_done_block: str | None = None

    try:
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            async with client.stream("POST", target_url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code,
                                        detail=f"下游回應錯誤: {resp.status_code}")
                def _emit(block: str, event_name: str | None, data: str | None) -> str:
                    """Render a block, upgrading anila.meta.classified if required."""
                    if (
                        event_name == "anila.meta"
                        and data
                        and requires_encryption
                    ):
                        try:
                            parsed = json.loads(data)
                            if isinstance(parsed, dict) and not parsed.get("classified"):
                                parsed["classified"] = True
                                return (
                                    "event: anila.meta\n"
                                    + "data: "
                                    + json.dumps(parsed, ensure_ascii=False)
                                    + "\n\n"
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    return block + "\n\n"

                block_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if block_lines:
                            block = "\n".join(block_lines)
                            event_name, data = _parse_sse_block(block)
                            if data == "[DONE]":
                                pending_done_block = block + "\n\n"
                            else:
                                if event_name == "anila.meta":
                                    meta_seen = True
                                if data and event_name in (None, "message"):
                                    try:
                                        chunk = json.loads(data)
                                        text = _extract_stream_text(chunk)
                                        if text:
                                            completion_parts.append(text)
                                        usage = chunk.get("usage") or {}
                                        if usage:
                                            usage_seen = True
                                            prompt_tokens = usage.get("prompt_tokens", 0)
                                            completion_tokens = usage.get("completion_tokens", 0)
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                                yield _emit(block, event_name, data)
                            block_lines = []
                        continue
                    block_lines.append(line)
                if block_lines:
                    block = "\n".join(block_lines)
                    event_name, data = _parse_sse_block(block)
                    if data == "[DONE]":
                        pending_done_block = block + "\n\n"
                    else:
                        if event_name == "anila.meta":
                            meta_seen = True
                        if data and event_name in (None, "message"):
                            try:
                                chunk = json.loads(data)
                                text = _extract_stream_text(chunk)
                                if text:
                                    completion_parts.append(text)
                                usage = chunk.get("usage") or {}
                                if usage:
                                    usage_seen = True
                                    prompt_tokens = usage.get("prompt_tokens", 0)
                                    completion_tokens = usage.get("completion_tokens", 0)
                            except (json.JSONDecodeError, KeyError):
                                pass
                        yield _emit(block, event_name, data)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="下游請求逾時")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"無法連線到下游端點 {target_url}")

    duration_ms = int((time.time() - start_time) * 1000)
    if not usage_seen:
        prompt_tokens = _estimate_token_count(model_name, prompt_text)
        completion_tokens = _estimate_token_count(model_name, "".join(completion_parts))
        logger.warning(
            "串流回應未提供 usage，改用伺服端估算 %s: prompt=%s completion=%s",
            model_name or target_url,
            prompt_tokens,
            completion_tokens,
        )
    total_tokens = prompt_tokens + completion_tokens
    if not meta_seen:
        yield "event: anila.meta\n"
        yield "data: " + json.dumps(
            build_default_anila_meta(
                model_name or target_url,
                detail=f"Proxy stream -> {target_url}",
                latency_ms=duration_ms,
                classified=requires_encryption,
                usage={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            ),
            ensure_ascii=False,
        ) + "\n\n"
    if pending_done_block:
        yield pending_done_block
    if total_tokens > 0:
        # Slice 2b-C: task-linked / legacy-marked /v1 chat rows go through
        # the task-aware variant; every other caller keeps the
        # byte-identical legacy enqueue path.
        if task_id is not None or legacy_runtime_call:
            await enqueue_usage_task_linked(
                api_key_id=api_key_id,
                user_id=user_id,
                department_id=department_id,
                model_id=usage_model_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_duration_ms=duration_ms,
                conversation_id=conversation_id,
                trace_id=trace_id,
                caller_agent_id=caller_agent_id,
                caller_client_id=caller_client_id,
                task_id=task_id,
                legacy_runtime_call=legacy_runtime_call,
            )
        else:
            await enqueue_usage(
                api_key_id=api_key_id,
                user_id=user_id,
                department_id=department_id,
                model_id=usage_model_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                request_duration_ms=duration_ms,
                conversation_id=conversation_id,
                trace_id=trace_id,
                caller_agent_id=caller_agent_id,
                caller_client_id=caller_client_id,
            )


async def proxy_stream(
    target_url: str,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    usage_model_id: int,
    request_body: dict,
    user_email: Optional[str] = None,
    user_identity: Optional[str] = None,
    model_name: str | None = None,
    conversation_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    requires_encryption: bool = False,
    target_agent_id: Optional[int] = None,
    caller_agent_id: Optional[int] = None,
    caller_client_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_trace_id: Optional[str] = None,
    task_run_id: Optional[int] = None,
    legacy_runtime_call: bool = False,
    gateway_api_key: Optional[str] = None,
    inference_callsite_id: str | None = None,
    governance_db=None,
    registry_endpoint_url: str | None = None,
    admitted_classification_level: str | None = None,
) -> AsyncIterator[str]:
    """Public entrypoint — ``_proxy_stream_impl`` plus Slice 2b-C TaskRun
    finalization. The stream drains AFTER the request handler returns, so
    the terminal run state is recorded here: completed on normal
    exhaustion, failed on HTTP errors / aborts (incl. client disconnects —
    the ``finally`` runs on ``GeneratorExit`` too, so no run is left
    dangling in ``running``). No-op — and byte-identical behavior — for
    legacy task-less callers (``task_run_id is None``).
    """
    status = "completed"
    error: dict | None = None
    span_started_at = datetime.now(timezone.utc)
    try:
        _require_pilot_sink_admission(
            inference_callsite_id=inference_callsite_id,
            task_id=task_id,
            task_run_id=task_run_id,
            governance_db=governance_db,
            admitted_classification_level=admitted_classification_level,
        )
        effective_level = _lock_task_run_admission(
            governance_db=governance_db,
            task_id=task_id,
            task_run_id=task_run_id,
            admitted_classification_level=admitted_classification_level,
        )
        _lock_registry_admission(
            governance_db=governance_db,
            registry_model_id=(
                None if target_agent_id is not None else usage_model_id
            ),
            registry_agent_id=target_agent_id,
            registry_endpoint_url=registry_endpoint_url,
            admitted_classification_level=effective_level,
            inference_callsite_id=inference_callsite_id,
        )
        async for chunk in _proxy_stream_impl(
            target_url=target_url,
            api_key_id=api_key_id,
            user_id=user_id,
            department_id=department_id,
            usage_model_id=usage_model_id,
            request_body=request_body,
            user_email=user_email,
            user_identity=user_identity,
            model_name=model_name,
            conversation_id=conversation_id,
            trace_id=trace_id,
            requires_encryption=requires_encryption,
            target_agent_id=target_agent_id,
            caller_agent_id=caller_agent_id,
            caller_client_id=caller_client_id,
            task_id=task_id,
            task_trace_id=task_trace_id,
            legacy_runtime_call=legacy_runtime_call,
            gateway_api_key=gateway_api_key,
            inference_callsite_id=inference_callsite_id,
            governance_db=governance_db,
            admitted_classification_level=admitted_classification_level,
            task_run_id=task_run_id,
        ):
            yield chunk
    except HTTPException as exc:
        status = "failed"
        error = {"code": f"http_{exc.status_code}", "message": str(exc.detail)}
        raise
    except BaseException as exc:  # GeneratorExit / CancelledError included
        status = "failed"
        error = {"code": "stream_aborted", "message": type(exc).__name__}
        raise
    finally:
        if task_run_id is not None:
            _finalize_proxy_task_run(
                governance_db=governance_db,
                task_run_id=task_run_id,
                status=status,
                error=error,
            )
            # Slice 4a: emit dispatch spans once the stream drains. Streaming
            # usage is tallied inside _proxy_stream_impl and not surfaced here,
            # so the child span carries timing/status but no token attributes
            # (usage rides the token_usage row instead).
            _emit_proxy_dispatch_spans(
                trace_id=task_trace_id,
                task_id=task_id,
                started_at=span_started_at,
                run_status=status,
                is_agent=target_agent_id is not None,
                target_id=(
                    target_agent_id
                    if target_agent_id is not None
                    else usage_model_id
                ),
                target_name=model_name,
            )

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
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from fastapi import HTTPException
import httpx
from sqlalchemy.orm import Session
from anila_core.security import ENDPOINT_KIND_AGENT, ENDPOINT_KIND_MODEL

from app.models.model_registry import ModelRegistry
from app.models.platform_setting import get_setting
from app.services.agent_reply_signal import attach_agent_reply_observation
from app.services.proxy.sampling import (
    apply_model_sampling_overrides,
    describe_thinking_applied,
    is_thinking_locked,
    stamp_thinking_locked,
)
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.headers import (
    _apply_gateway_auth,
    build_agent_headers,
    build_model_gateway_headers,
    resolve_model_gateway_key,
)
from app.services.proxy.sse import _aggregate_sse_to_chat_completion, _parse_sse_block
from app.services.proxy.task_link import finalize_task_run
from app.services.proxy.urls import join_upstream_path, strip_trailing_api_version
from app.services.proxy.usage import (
    _estimate_token_count,
    _extract_reasoning_text,
    _extract_response_text,
    _extract_stream_reasoning,
    _extract_stream_text,
    _merge_usage_maps,
    _serialize_request_for_usage,
    enqueue_usage_task_linked,
    resolve_reasoning_tokens,
)
from app.services.usage_writer import enqueue_usage

# Keep the pre-split logger channel ("app.services.proxy_service") so log
# routing / filtering / capture behavior is identical after the package split.
logger = logging.getLogger("app.services.proxy_service")

_CONTEXT_OVERFLOW_UPSTREAM_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "contextwindowexceedederror",
    }
)


def _stable_upstream_error_code(body: object) -> str | None:
    """Map a known upstream code to the frontend's stable semantic code."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    raw_code = error.get("code") or error.get("type")
    if not isinstance(raw_code, str):
        return None
    return (
        "context_overflow"
        if raw_code.strip().lower() in _CONTEXT_OVERFLOW_UPSTREAM_CODES
        else None
    )


def _sanitised_upstream_error_detail(body: object, status_code: int) -> str | dict:
    """Preserve display text while exposing only an allow-listed semantic code."""
    detail: str | dict = f"模型服務拒絕請求 (HTTP {status_code})"
    if isinstance(body, dict):
        error = body.get("error")
        msg = error.get("message") if isinstance(error, dict) else None
        if isinstance(msg, str) and msg:
            detail = msg[:300]
        code = _stable_upstream_error_code(body)
        if code:
            detail = {"code": code, "message": detail}
    return detail


def _http_exception_message(detail: object) -> str:
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
        return "代理請求失敗"
    return str(detail)


def strip_inline_think_from_chat_result(result: dict | object) -> dict | object:
    """非串流邊界：剝 ``message.content`` 內嵌 ``<think>`` 後再回傳／落庫。

    Fail-open：任何例外只記 warning，內容原樣返回。串流路徑不走這裡。
    """
    if not isinstance(result, dict):
        return result
    try:
        from anila_core.text.think_strip import strip_inline_think
    except Exception:
        logger.warning(
            "think_strip import failed; leaving content untouched",
            exc_info=True,
        )
        return result
    try:
        for choice in result.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue
            clean, removed = strip_inline_think(content)
            if removed:
                message["content"] = clean
                logger.info("think_strip removed_chars=%s", removed)
        return result
    except Exception:
        logger.warning(
            "think_strip failed; leaving content untouched",
            exc_info=True,
        )
        return result


def _note_proxy_outcome(
    *,
    model_id: int,
    model_name: str,
    model_type: str,
    display_name: str | None = None,
    success: bool,
) -> None:
    """P3.2 consecutive-failure hook. Best-effort; never raises into proxy."""
    try:
        from app.services.alert_detectors import record_proxy_outcome

        record_proxy_outcome(
            model_id=model_id,
            model_name=model_name,
            model_type=model_type,
            display_name=display_name,
            success=success,
        )
    except Exception:  # pragma: no cover - detection must not break serving
        logger.exception(
            "proxy alert streak update failed model_id=%s success=%s",
            model_id,
            success,
        )


def _proxy_detail(label: str, endpoint_display: str | None, *, stream: bool = False) -> str:
    """Caller-facing proxy trace / failure text.

    ``endpoint_display`` is the output of ``visible_endpoint_url`` (real
    address or sentinel). When omitted, keep the legacy name-only form so
    internal callers that have not been wired yet do not change.
    """
    prefix = "Proxy stream -> " if stream else "Proxy -> "
    if endpoint_display:
        return f"{prefix}{label}（{endpoint_display}）"
    return f"{prefix}{label}"


def _proxy_connect_failure(label: str, endpoint_display: str | None) -> str:
    if endpoint_display:
        return f"無法連線到模型「{label}」（{endpoint_display}）"
    return f"無法連線到模型「{label}」"


def stream_failure_user_message(
    exc: BaseException,
    *,
    model_name: str | None = None,
) -> str:
    """Plain-language terminal error for chat users.

    Never interpolates exception text or endpoint addresses — open-ended
    exception classes and httpx errors routinely embed hostnames / URLs.
    """
    label = (model_name or "").strip() or "模型"
    if isinstance(exc, HTTPException):
        code = int(exc.status_code)
        if code == 504:
            return f"「{label}」回應逾時，請稍後再試。"
        if code in (401, 403):
            return f"「{label}」拒絕了這次請求，請聯絡管理員。"
        if code == 404:
            return f"找不到「{label}」的服務，請聯絡管理員。"
        if code == 502 or code >= 500:
            return f"「{label}」暫時無法使用，請稍後再試。"
        return f"「{label}」目前無法完成這次回應，請稍後再試。"
    if isinstance(exc, httpx.TimeoutException):
        return f"「{label}」回應逾時，請稍後再試。"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return f"無法連線到「{label}」，請稍後再試或聯絡管理員。"
    return "產生回應時發生錯誤，請稍後再試。"


def format_anila_stream_error(message: str, *, code: str | None = None) -> str:
    """Terminal SSE frame carrying a user-visible stream failure."""
    payload: dict[str, str] = {"message": message}
    if code:
        payload["code"] = code
    return (
        "event: anila.error\n"
        + "data: "
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n"
    )


# ── 出向呼叫的四顆調節鈕 ──────────────────────────────────────────────────────
#
# ⚠ **在呼叫端解析一次、往下傳；本模組不自己查 DB。** 兩個理由都是這個檔案自己
# 的形狀，不是風格偏好：
#
# 1. ``memory_service._embed`` 與 ``api/ingestion/search.py`` 都**刻意**在呼叫
#    ``proxy_request`` 之前 ``db.commit()``，把池化連線還回池子再走出向 HTTP
#    （``tests/test_embed_query_releases_pool.py`` 量的就是這件事）。在本模組裡
#    查一次 ``platform_settings`` 會重新 checkout 一條連線，而且一路握到 HTTP
#    回來為止 —— 而那支測試 stub 掉 ``proxy_request``，**抓不到這個回歸**。
# 2. ``proxy_stream`` 是 async generator，**在 handler 回傳之後才被抽乾**；那時
#    request scope 的 session 可能已經關閉。handler 期解析是唯一安全的時點。
#
# 所以兩顆逾時值在「呼叫端還合法握著連線」的那一刻解一次，凍結成這個物件往下傳；
# 重試策略是固定的程式常數。
# 凍結的範圍是**一次出向呼叫**，不是一個行程 —— 下一個請求會再解一次。


@dataclass(frozen=True)
class ProxyTuning:
    """一次出向呼叫用的逾時與固定重試策略。"""

    llm_timeout: int
    embedding_timeout: int
    max_retries: int
    retry_base_delay: float

    @classmethod
    def from_registry_defaults(cls) -> "ProxyTuning":
        """登錄表宣告的程式預設值。

        **只給手上真的沒有 session 的呼叫端用（實務上＝測試）。** 它跳過
        ``platform_settings`` 與 env 兩層，所以 production 路徑一律走
        :func:`resolve_proxy_tuning`；值的唯一來源仍然是登錄表那一筆，不是
        ``config.py`` 再抄一份。
        """
        from app.services.settings_registry import REGISTRY

        return cls(
            llm_timeout=int(REGISTRY["proxy.llm_timeout"].default),
            embedding_timeout=int(REGISTRY["proxy.embedding_timeout"].default),
            max_retries=3,
            retry_base_delay=0.5,
        )


def resolve_proxy_tuning(db: Session) -> ProxyTuning:
    """解析兩顆逾時值（``platform_settings`` → env → 程式預設）。

    呼叫端要在**還握著連線的那一刻**呼叫它（例如 ``db.commit()`` 釋放連線之前），
    再把結果傳給 ``proxy_request`` / ``proxy_stream``。
    """
    return ProxyTuning(
        llm_timeout=int(get_setting(db, "proxy.llm_timeout")),
        embedding_timeout=int(get_setting(db, "proxy.embedding_timeout")),
        max_retries=3,
        retry_base_delay=0.5,
    )


def _get_timeout(model_type: str, tuning: ProxyTuning) -> float:
    if model_type == "embedding":
        return tuning.embedding_timeout
    return tuning.llm_timeout


def _normalize_embed_inputs(request_body: dict) -> list[str]:
    """OpenAI-shape ``input`` → list[str]."""
    raw = request_body.get("input", [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise HTTPException(
                    status_code=400, detail="embedding input 必須為字串或字串陣列"
                )
            out.append(item)
        return out
    raise HTTPException(status_code=400, detail="embedding input 必須為字串或字串陣列")


def _resolve_embedding_input_role(
    embedding_input_role: Optional[str],
) -> str:
    """Return ``query`` or ``document``. Default ``document`` for public /v1.

    Query-side call sites (search, memory, dim probe) must pass
    ``embedding_input_role='query'`` explicitly — silent default-to-document
    on those paths is the quality bug this parameter exists to prevent.
    """
    if embedding_input_role is None or embedding_input_role == "":
        return "document"
    if embedding_input_role in ("query", "document"):
        return embedding_input_role
    raise HTTPException(
        status_code=400,
        detail="embedding_input_role 僅接受 query 或 document",
    )


async def _proxy_triton_embedding(
    *,
    model: ModelRegistry,
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    request_body: dict,
    endpoint_path: str,
    conversation_id: Optional[str],
    trace_id: Optional[str],
    requires_encryption: bool,
    caller_agent_id: Optional[int],
    caller_client_id: Optional[int],
    task_id: Optional[int],
    legacy_runtime_call: bool,
    endpoint_display: Optional[str],
    embedding_input_role: Optional[str],
    timeout: float,
    request_type: str,
    tuning: ProxyTuning,
    record_usage: bool = True,
    usage_source: Optional[str] = None,
    usage_kind: str = "inference",
    invocation_id: Optional[str] = None,
    model_name_snapshot: Optional[str] = None,
    token_source: str = "estimated",
) -> dict:
    """Triton/KServe gRPC embedding path — never through join_upstream_path."""
    from app.services.triton_grpc import TritonEmbedError, embed_texts

    invocation_id = invocation_id or uuid.uuid4().hex
    model_name_snapshot = model_name_snapshot or model.name
    if request_type != "embedding" and model.model_type != "embedding":
        raise HTTPException(
            status_code=400,
            detail="protocol=triton_grpc 目前僅支援 embedding 呼叫",
        )

    # Guard the registered endpoint as-is (grpc://host:port). Path joining
    # is meaningless for gRPC — do not call join_upstream_path.
    endpoint_url = (model.endpoint_url or "").strip()
    _guard_outbound(endpoint_url, endpoint_kind=ENDPOINT_KIND_MODEL)

    role = _resolve_embedding_input_role(embedding_input_role)
    texts = _normalize_embed_inputs(request_body)
    if not texts:
        raise HTTPException(status_code=400, detail="embedding input 不可為空")
    if role == "query" and len(texts) != 1:
        raise HTTPException(
            status_code=400,
            detail="triton query embedding 每次僅接受一個字串",
        )

    start_time = time.time()
    last_error = None
    for attempt in range(tuning.max_retries):
        try:
            vectors = await asyncio.to_thread(
                embed_texts,
                endpoint_url,
                model.name,
                texts,
                role=role,
                timeout_s=float(timeout),
            )
            duration_ms = int((time.time() - start_time) * 1000)
            prompt_tokens = sum(max(1, len(t.split())) for t in texts)
            result = {
                "object": "list",
                "model": model.name,
                "data": [
                    {"object": "embedding", "index": i, "embedding": vec}
                    for i, vec in enumerate(vectors)
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "total_tokens": prompt_tokens,
                },
                "anila_meta": build_default_anila_meta(
                    model.name,
                    detail=_proxy_detail(model.name, endpoint_display),
                    latency_ms=duration_ms,
                    classified=requires_encryption,
                    usage={
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": 0,
                        "total_tokens": prompt_tokens,
                    },
                ),
            }
            if record_usage:
                if task_id is not None or legacy_runtime_call:
                    await enqueue_usage_task_linked(
                        api_key_id=api_key_id,
                        user_id=user_id,
                        department_id=department_id,
                        model_id=model.id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=0,
                        total_tokens=prompt_tokens,
                        request_duration_ms=duration_ms,
                        conversation_id=conversation_id,
                        trace_id=trace_id,
                        request_type=request_type,
                        caller_agent_id=caller_agent_id,
                        caller_client_id=caller_client_id,
                        task_id=task_id,
                        legacy_runtime_call=legacy_runtime_call,
                        usage_kind=usage_kind,
                        invocation_id=invocation_id,
                        model_name_snapshot=model_name_snapshot,
                        token_source=token_source,
                    )
                else:
                    await enqueue_usage(
                        api_key_id=api_key_id,
                        user_id=user_id,
                        department_id=department_id,
                        model_id=model.id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=0,
                        total_tokens=prompt_tokens,
                        request_duration_ms=duration_ms,
                        conversation_id=conversation_id,
                        trace_id=trace_id,
                        request_type=request_type,
                        caller_agent_id=caller_agent_id,
                        caller_client_id=caller_client_id,
                        usage_kind=usage_kind,
                        invocation_id=invocation_id,
                        model_name_snapshot=model_name_snapshot,
                        token_source=token_source,
                    )
            _note_proxy_outcome(
                model_id=model.id,
                model_name=model.name,
                model_type=model.model_type,
                display_name=getattr(model, "display_name", None),
                success=True,
            )
            return result
        except TritonEmbedError as exc:
            last_error = str(exc)
            if attempt < tuning.max_retries - 1:
                delay = tuning.retry_base_delay * (2 ** attempt)
                logger.warning(
                    "模型 %s Triton 呼叫失敗，%ss 後重試 (%s/%s): %s",
                    model.name,
                    delay,
                    attempt + 1,
                    tuning.max_retries,
                    last_error,
                )
                await asyncio.sleep(delay)
                continue
            logger.error("模型 %s Triton  upstream 失敗: %s", model.name, last_error)
            _note_proxy_outcome(
                model_id=model.id,
                model_name=model.name,
                model_type=model.model_type,
                display_name=getattr(model, "display_name", None),
                success=False,
            )
            raise HTTPException(status_code=502, detail="模型服務暫時不可用") from exc
        except HTTPException:
            raise
        except Exception as exc:
            last_error = "未預期的代理錯誤"
            logger.error("Triton 代理請求錯誤: %s", exc, exc_info=True)
            if attempt < tuning.max_retries - 1:
                delay = tuning.retry_base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            _note_proxy_outcome(
                model_id=model.id,
                model_name=model.name,
                model_type=model.model_type,
                display_name=getattr(model, "display_name", None),
                success=False,
            )
            raise HTTPException(status_code=502, detail="模型服務暫時不可用") from exc

    _note_proxy_outcome(
        model_id=model.id,
        model_name=model.name,
        model_type=model.model_type,
        display_name=getattr(model, "display_name", None),
        success=False,
    )
    raise HTTPException(
        status_code=502,
        detail=f"模型服務不可用，已重試 {tuning.max_retries} 次: {last_error}",
    )


def _conversation_thinking_tier(
    conversation_id: Optional[str],
    *,
    caller_user_id: int | None,
) -> str | None:
    """Load ``conversations.thinking_tier`` for the already-parsed header id.

    Only the conversation owner's row is used (``conv.user_id ==
    caller_user_id``). A missing caller id, missing row, or owner mismatch
    returns None so a leaked / spoofed ``X-ANILA-Conversation-Id`` cannot
    apply another user's picker setting. ``proxy.py`` already 404s foreign
    ids for non-admin callers; this second check still runs because
    admin-tier may pass that gate.

    Short-lived ``SessionLocal`` so this module does not hold the request
    session across outbound HTTP. Missing / non-numeric ids return None.
    """
    if conversation_id is None or caller_user_id is None:
        return None
    raw = str(conversation_id).strip()
    if not raw.isdigit():
        return None
    from app.database import SessionLocal
    from app.models.conversation import Conversation

    db = SessionLocal()
    try:
        row = db.get(Conversation, int(raw))
        if row is None or row.user_id != caller_user_id:
            return None
        return getattr(row, "thinking_tier", None)
    except Exception:
        logger.exception("failed to load conversation thinking_tier")
        return None
    finally:
        db.close()


def _usage_for_meta(
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    reasoning_tokens: int | None,
    reasoning_source: str | None,
) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_tokens_source": reasoning_source,
    }


def _stamp_usage_and_thinking(
    meta: dict,
    *,
    usage: dict,
    thinking_applied: dict | None,
) -> None:
    prior = meta.get("usage")
    if isinstance(prior, dict):
        merged = dict(prior)
        merged.update(usage)
        meta["usage"] = merged
    else:
        meta["usage"] = usage
    if thinking_applied is not None:
        meta["thinking_applied"] = thinking_applied


def build_default_anila_meta(
    source_name: str,
    *,
    detail: str,
    latency_ms: int | None = None,
    classified: bool = False,
    usage: dict | None = None,
    thinking_locked: bool = False,
    thinking_applied: dict | None = None,
) -> dict:
    """Build an anila_meta skeleton used when the downstream omits one.

    The ``classified`` flag is a one-way latch. It is driven solely by the
    resolved **agent's** ``requires_encryption`` attribute (base LLMs do not
    carry this flag). Downstreams that set ``classified=true`` in their own
    ``anila.meta`` are authoritative; this default only fills the baseline
    when nothing is emitted.
    """
    meta = {
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
        # 院內規章檢索的狀態（``KbState``，app/services/institutional_kb.py）。
        # **明帶在骨架裡是刻意的**：缺席不等於「沒查過」——欄位一旦可以不存在，
        # 任何一條漏接的路徑都會靜默降級成 not_searched，而使用者在畫面上看不出
        # 差別。真正的值由 ``app/api/proxy.py`` 的 ``_merge_kb_meta`` /
        # ``_sse_with_kb_meta`` 在每一個 chat 出口上蓋過去；這裡只保證欄位一定在。
        # ⚠ 字面值不從 institutional_kb 匯入：那個模組會拉進 app.api.ingestion.*
        # 與 pgvector，等於讓每一通 proxy 呼叫背上檢索層的 import。名稱與 enum
        # 的耦合改由測試釘住（test_institutional_kb_injection.py::
        # test_build_default_anila_meta_declares_the_state）。
        # ⚠ embedding 路徑（:279）也共用這個骨架，所以 embedding 回應的 meta 上
        # 也會多一個 not_searched。那是刻意接受的溢出：多一個誠實的欄位，比讓
        # chat 與 embedding 的骨架分家便宜。
        "kb_state": "not_searched",
        "confidence": None,
        "handoff_chain": [],
        "follow_ups": [],
        "latency_ms": latency_ms,
        "classified": bool(classified),
        # Token usage so the chat UI can show per-message token counts. Omitted
        # (None) when unknown; the client only renders when present.
        "usage": usage,
    }
    if thinking_locked:
        meta["thinking_locked"] = True
    if thinking_applied is not None:
        meta["thinking_applied"] = thinking_applied
    return meta


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
    endpoint_display: Optional[str] = None,
    embedding_input_role: Optional[str] = None,
    record_usage: bool = True,
    usage_source: Optional[str] = None,
    *,
    tuning: ProxyTuning,
    caller_authorization: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    usage_kind: str = "inference",
    invocation_id: Optional[str] = None,
    model_name_snapshot: Optional[str] = None,
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
    timeout = _get_timeout(model.model_type, tuning)
    invocation_id = invocation_id or uuid.uuid4().hex
    model_name_snapshot = model_name_snapshot or getattr(model, "name", None)
    conv_tier = _conversation_thinking_tier(
        conversation_id, caller_user_id=user_id
    )
    thinking_applied = describe_thinking_applied(
        request_body, model, thinking_tier=conv_tier
    )
    request_body = apply_model_sampling_overrides(
        request_body,
        model,
        thinking_tier=conv_tier,
    )
    # ``usage_source`` comes from the X-ANILA-Request-Source header: anila-studio
    # sends "studio" so the usage dashboard can split 簡報製作 from chat.
    request_type = (
        "embedding"
        if "embedding" in endpoint_path or model.model_type == "embedding"
        else ("studio" if usage_source == "studio" else "chat")
    )

    protocol = (getattr(model, "protocol", None) or "openai_compatible").strip()
    if protocol == "triton_grpc":
        return await _proxy_triton_embedding(
            model=model,
            api_key_id=api_key_id,
            user_id=user_id,
            department_id=department_id,
            request_body=request_body,
            endpoint_path=endpoint_path,
            conversation_id=conversation_id,
            trace_id=trace_id,
            requires_encryption=requires_encryption,
            caller_agent_id=caller_agent_id,
            caller_client_id=caller_client_id,
            task_id=task_id,
            legacy_runtime_call=legacy_runtime_call,
            endpoint_display=endpoint_display,
            embedding_input_role=embedding_input_role,
            timeout=timeout,
            request_type=request_type,
            tuning=tuning,
            record_usage=record_usage,
            usage_kind=usage_kind,
            invocation_id=invocation_id or uuid.uuid4().hex,
            model_name_snapshot=model_name_snapshot or model.name,
            token_source="estimated",
        )

    # Registry rows store bare host or ``.../v1``; join_upstream_path is
    # correct for both. Preserve the api_version=="v2" embedding special case
    # (strip any trailing version segment first so …/v1 + v2 does not become
    # …/v1/v2/embeddings). api_version is a URL path prefix only — not a
    # wire protocol (see docs/FAKE-CONTROLS.md).
    if model.api_version == "v2" and "embedding" in endpoint_path:
        target_url = join_upstream_path(
            strip_trailing_api_version(model.endpoint_url),
            "/v2/embeddings",
        )
    else:
        target_url = join_upstream_path(model.endpoint_url, endpoint_path)

    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    # Guard the FINAL url that will actually be requested.
    _guard_outbound(
        target_url,
        endpoint_kind=(
            ENDPOINT_KIND_AGENT if model.model_type == "agent" else ENDPOINT_KIND_MODEL
        ),
    )

    last_error = None
    start_time = time.time()

    # Builder chosen by DESTINATION (not a caller flag): agent → signed
    # dispatch JWT; model/embedding → 員編-only (never a CSP credential).
    if model.model_type == "agent":
        if target_agent_id is None:
            # Registry may seed model_type="agent" without a matching agents
            # row (AUTO_REGISTER_MODELS). Refuse rather than fall back to a
            # shared fleet token — no agent_id means no identity to sign.
            raise HTTPException(
                status_code=400,
                detail=(
                    "此模型標記為 agent 類型，但未對應已註冊的 Agent，"
                    "無法簽署派工身分 token"
                ),
            )
        req_headers = build_agent_headers(
            user_id=user_id,
            department=department_id,
            agent_id=target_agent_id,
            task_id=task_id,
            trace_id=task_trace_id,
        )
    else:
        # Doc 04 §3/AC5: model gateway gets Bearer key + 員編 ONLY — no
        # task / trace headers, structurally (builder has no such params).
        req_headers = build_model_gateway_headers(user_identity)
    # gateway key 只給 model 呼叫;agent dispatch (model_type='agent') 不帶。
    # Slice 6a: per-model api_key_secret_ref 優先,退回全域 env(MVP fallback)。
    if caller_authorization:
        req_headers["Authorization"] = caller_authorization
    elif model.model_type != "agent":
        _apply_gateway_auth(req_headers, resolve_model_gateway_key(model))
    if extra_headers:
        for key, value in extra_headers.items():
            if key.lower() in ("authorization", "content-type"):
                continue
            req_headers[key] = value

    for attempt in range(tuning.max_retries):
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
                if attempt < tuning.max_retries - 1:
                    delay = tuning.retry_base_delay * (2 ** attempt)
                    logger.warning(
                        f"模型 {model.name} 回應 {response.status_code}，"
                        f"{delay}s 後重試 ({attempt + 1}/{tuning.max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("模型 %s 上游 5xx: %s", model.name, last_error)
                _note_proxy_outcome(
                    model_id=model.id,
                    model_name=model.name,
                    model_type=model.model_type,
                    display_name=getattr(model, "display_name", None),
                    success=False,
                )
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
                body: object = None
                try:
                    body = response.json()
                except Exception:
                    pass
                detail = _sanitised_upstream_error_detail(body, response.status_code)
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
            reasoning_tokens, reasoning_source = resolve_reasoning_tokens(
                usage if usage else None,
                reasoning_text=_extract_reasoning_text(result),
                model_name=model.name,
            )
            usage_for_meta = _usage_for_meta(
                prompt_tokens,
                completion_tokens,
                total_tokens,
                reasoning_tokens,
                reasoning_source,
            )
            existing_meta = result.get("anila_meta")
            if not existing_meta:
                # Caller-facing detail uses the visibility-gated display
                # form (real address or sentinel) supplied by the API layer.
                result["anila_meta"] = build_default_anila_meta(
                    model.name,
                    detail=_proxy_detail(model.name, endpoint_display),
                    latency_ms=duration_ms,
                    classified=requires_encryption,
                    usage=usage_for_meta,
                    thinking_locked=is_thinking_locked(model),
                    thinking_applied=thinking_applied,
                )
            elif isinstance(existing_meta, dict):
                if requires_encryption:
                    # One-way latch: upgrade to classified when the resolved model
                    # requires encryption, even if the downstream omitted the flag.
                    existing_meta["classified"] = True
                stamp_thinking_locked(existing_meta, model)
                _stamp_usage_and_thinking(
                    existing_meta,
                    usage=usage_for_meta,
                    thinking_applied=thinking_applied,
                )

            token_source = "reported" if usage else "estimated"
            # Enqueue usage record (non-blocking).
            # Sprint 5 / Chunk W: ``request_type`` flows from the
            # endpoint-path classification at the top of this function so
            # embedding rows get tagged distinct from chat rows.
            # Slice 2b-C: task-linked / legacy-marked /v1 chat rows go
            # through the task-aware variant; every other caller keeps the
            # byte-identical legacy enqueue path.
            if record_usage:
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
                        usage_kind=usage_kind,
                        invocation_id=invocation_id,
                        model_name_snapshot=model_name_snapshot,
                        token_source=token_source,
                        reasoning_tokens=reasoning_tokens,
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
                        usage_kind=usage_kind,
                        invocation_id=invocation_id,
                        model_name_snapshot=model_name_snapshot,
                        token_source=token_source,
                        reasoning_tokens=reasoning_tokens,
                    )

            # 量測／衛生：usage 入帳後、回傳前剝內嵌 think（不影響 metering）。
            result = strip_inline_think_from_chat_result(result)

            _note_proxy_outcome(
                model_id=model.id,
                model_name=model.name,
                model_type=model.model_type,
                display_name=getattr(model, "display_name", None),
                success=True,
            )
            return result

        except httpx.TimeoutException:
            last_error = f"請求逾時 ({timeout}s)"
            if attempt < tuning.max_retries - 1:
                delay = tuning.retry_base_delay * (2 ** attempt)
                logger.warning(
                    f"模型 {model.name} 請求逾時，"
                    f"{delay}s 後重試 ({attempt + 1}/{tuning.max_retries})"
                )
                await asyncio.sleep(delay)
                continue

        except httpx.ConnectError:
            # Caller-facing text identifies the model; endpoint_display is
            # the visibility-gated form (real or sentinel) from the API.
            last_error = _proxy_connect_failure(model.name, endpoint_display)
            if attempt < tuning.max_retries - 1:
                delay = tuning.retry_base_delay * (2 ** attempt)
                logger.warning(
                    "模型 %s 連線失敗（%s），%ss 後重試 (%s/%s)",
                    model.name,
                    target_url,
                    delay,
                    attempt + 1,
                    tuning.max_retries,
                )
                await asyncio.sleep(delay)
                continue

        except HTTPException:
            raise

        except Exception as e:
            # Never forward exception text to callers — open-ended
            # exception classes can embed hostnames / URLs.
            last_error = "未預期的代理錯誤"
            logger.error("代理請求錯誤: %s", e, exc_info=True)
            if attempt < tuning.max_retries - 1:
                delay = tuning.retry_base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

    _note_proxy_outcome(
        model_id=model.id,
        model_name=model.name,
        model_type=model.model_type,
        display_name=getattr(model, "display_name", None),
        success=False,
    )
    raise HTTPException(
        status_code=502,
        detail=f"模型服務不可用，已重試 {tuning.max_retries} 次: {last_error}",
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
    endpoint_display: Optional[str] = None,
    embedding_input_role: Optional[str] = None,
    record_usage: bool = True,
    usage_source: Optional[str] = None,
    *,
    tuning: ProxyTuning,
    caller_authorization: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    usage_kind: str = "inference",
    invocation_id: Optional[str] = None,
    model_name_snapshot: Optional[str] = None,
) -> dict:
    """Public entrypoint — ``_proxy_request_impl`` plus Slice 2b-C TaskRun
    finalization: when the call belongs to a Task (``task_run_id`` set),
    the run is marked completed on success / failed on any HTTP error
    (including SSRF-guard rejections and exhausted retries). No-op — and
    byte-identical behavior — for legacy task-less callers.

    ``embedding_input_role`` is in-process only (``query`` / ``document``);
    it selects the Triton input tensor for ``protocol=triton_grpc`` and is
    ignored on the OpenAI-compatible path. Public ``/v1/embeddings`` leaves
    it unset → documents; search/memory/probe must pass ``query``.

    ``record_usage=False`` skips token_usage enqueue (dim probe / internal
    checks that must not pollute dashboards).
    """
    try:
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
            endpoint_display=endpoint_display,
            embedding_input_role=embedding_input_role,
            record_usage=record_usage,
            usage_source=usage_source,
            tuning=tuning,
            caller_authorization=caller_authorization,
            extra_headers=extra_headers,
            usage_kind=usage_kind,
            invocation_id=invocation_id,
            model_name_snapshot=model_name_snapshot,
        )
    except HTTPException as exc:
        if task_run_id is not None:
            finalize_task_run(
                task_run_id,
                "failed",
                error={
                    "code": f"http_{exc.status_code}",
                    "message": _http_exception_message(exc.detail),
                },
            )
        raise
    if task_run_id is not None:
        finalize_task_run(task_run_id, "completed")
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
    endpoint_display: Optional[str] = None,
    *,
    tuning: ProxyTuning,
    model: ModelRegistry | None = None,
    caller_authorization: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    usage_kind: str = "inference",
    invocation_id: Optional[str] = None,
    model_name_snapshot: Optional[str] = None,
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
    invocation_id = invocation_id or uuid.uuid4().hex
    # Call-time SSRF re-validation (TOCTOU / DNS-rebinding defense).
    _guard_outbound(
        target_url,
        endpoint_kind=(
            ENDPOINT_KIND_AGENT if target_agent_id is not None else ENDPOINT_KIND_MODEL
        ),
    )

    # Builder chosen by DESTINATION (target_agent_id) — never a caller
    # flag — so the model gateway can never receive a CSP credential.
    if target_agent_id is not None:
        headers = build_agent_headers(
            user_id=user_id,
            department=department_id,
            agent_id=target_agent_id,
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
    if caller_authorization:
        headers["Authorization"] = caller_authorization
    elif target_agent_id is None:
        _apply_gateway_auth(headers, gateway_api_key)
    if extra_headers:
        for key, value in extra_headers.items():
            if key.lower() in ("authorization", "content-type"):
                continue
            headers[key] = value
    thinking_applied = None
    if model is not None:
        conv_tier = _conversation_thinking_tier(
            conversation_id, caller_user_id=user_id
        )
        thinking_applied = describe_thinking_applied(
            request_body, model, thinking_tier=conv_tier
        )
        request_body = apply_model_sampling_overrides(
            request_body,
            model,
            thinking_tier=conv_tier,
        )
    # Force stream_options so the downstream sends usage in last chunk
    body = {**request_body, "stream": True,
            "stream_options": {"include_usage": True}}

    start_time = time.time()
    prompt_tokens = completion_tokens = 0
    usage_seen = False
    last_usage: dict = {}
    prompt_text = _serialize_request_for_usage(body)
    completion_parts: list[str] = []
    reasoning_parts: list[str] = []
    pending_done_block: str | None = None
    pending_named_meta: dict | None = None

    try:
        async with httpx.AsyncClient(timeout=tuning.llm_timeout) as client:
            async with client.stream("POST", target_url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    body: object = None
                    try:
                        raw_body = await resp.aread()
                        body = json.loads(raw_body.decode("utf-8"))
                    except Exception:
                        pass
                    raise HTTPException(status_code=resp.status_code,
                                        detail=_sanitised_upstream_error_detail(
                                            body, resp.status_code
                                        ))
                def _hold_named_meta(data: str | None) -> bool:
                    """Hold a named anila.meta object until usage is complete.

                    Returns True when the frame was absorbed (do not yield yet).
                    """
                    nonlocal pending_named_meta
                    if not data:
                        return False
                    try:
                        parsed = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        return False
                    if not isinstance(parsed, dict):
                        return False
                    if pending_named_meta is None:
                        pending_named_meta = dict(parsed)
                    else:
                        pending_named_meta.update(parsed)
                    return True

                def _ingest_stream_payload(data: str | None, event_name: str | None) -> None:
                    nonlocal prompt_tokens, completion_tokens, usage_seen, last_usage
                    if not data or event_name not in (None, "message"):
                        return
                    try:
                        chunk = json.loads(data)
                    except (json.JSONDecodeError, TypeError, KeyError):
                        return
                    text = _extract_stream_text(chunk)
                    if text:
                        completion_parts.append(text)
                    reason = _extract_stream_reasoning(chunk)
                    if reason:
                        reasoning_parts.append(reason)
                    usage = chunk.get("usage") or {}
                    if usage:
                        usage_seen = True
                        last_usage = _merge_usage_maps(last_usage, usage)
                        if usage.get("prompt_tokens") is not None:
                            prompt_tokens = usage["prompt_tokens"]
                        if usage.get("completion_tokens") is not None:
                            completion_tokens = usage["completion_tokens"]

                def _emit(block: str, event_name: str | None, data: str | None) -> str:
                    """Render a block, upgrading anila.meta.classified if required."""
                    if event_name == "anila.meta" and data:
                        try:
                            parsed = json.loads(data)
                            if isinstance(parsed, dict):
                                changed = False
                                if requires_encryption and not parsed.get("classified"):
                                    parsed["classified"] = True
                                    changed = True
                                if (
                                    model is not None
                                    and is_thinking_locked(model)
                                    and not parsed.get("thinking_locked")
                                ):
                                    parsed["thinking_locked"] = True
                                    changed = True
                                if thinking_applied is not None and "thinking_applied" not in parsed:
                                    parsed["thinking_applied"] = thinking_applied
                                    changed = True
                                if changed:
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
                            elif event_name == "anila.meta" and _hold_named_meta(data):
                                pass
                            else:
                                _ingest_stream_payload(data, event_name)
                                yield _emit(block, event_name, data)
                            block_lines = []
                        continue
                    block_lines.append(line)
                if block_lines:
                    block = "\n".join(block_lines)
                    event_name, data = _parse_sse_block(block)
                    if data == "[DONE]":
                        pending_done_block = block + "\n\n"
                    elif event_name == "anila.meta" and _hold_named_meta(data):
                        pass
                    else:
                        _ingest_stream_payload(data, event_name)
                        yield _emit(block, event_name, data)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="下游請求逾時")
    except httpx.ConnectError:
        # Identify by registered model name; endpoint_display is gated.
        label = model_name or "未知模型"
        logger.warning("串流連線失敗 model=%s url=%s", label, target_url)
        raise HTTPException(
            status_code=502,
            detail=_proxy_connect_failure(label, endpoint_display),
        )

    duration_ms = int((time.time() - start_time) * 1000)
    if not usage_seen:
        prompt_tokens = _estimate_token_count(model_name, prompt_text)
        completion_tokens = _estimate_token_count(model_name, "".join(completion_parts))
        logger.warning(
            "串流回應未提供 usage，改用伺服端估算 %s: prompt=%s completion=%s",
            model_name or "未知模型",
            prompt_tokens,
            completion_tokens,
        )
    total_tokens = prompt_tokens + completion_tokens
    usage_source = "reported" if usage_seen else "estimated"
    reasoning_tokens, reasoning_source = resolve_reasoning_tokens(
        last_usage if usage_seen else None,
        reasoning_text="".join(reasoning_parts),
        model_name=model_name,
    )
    usage_for_meta = _usage_for_meta(
        prompt_tokens,
        completion_tokens,
        total_tokens,
        reasoning_tokens,
        reasoning_source,
    )
    if pending_named_meta is not None:
        terminal_meta = pending_named_meta
        if requires_encryption:
            terminal_meta["classified"] = True
        if model is not None and is_thinking_locked(model):
            terminal_meta["thinking_locked"] = True
        _stamp_usage_and_thinking(
            terminal_meta,
            usage=usage_for_meta,
            thinking_applied=thinking_applied,
        )
        if target_agent_id is not None:
            try:
                attach_agent_reply_observation(
                    terminal_meta,
                    completion_tokens=completion_tokens,
                    usage_source=usage_source,
                )
            except Exception:  # pragma: no cover - defensive serving boundary
                logger.exception("agent anila.meta observation rewrite failed")
    else:
        # Caller-facing stream meta uses the visibility-gated display form.
        stream_label = model_name or "未知模型"
        terminal_meta = build_default_anila_meta(
            stream_label,
            detail=_proxy_detail(
                stream_label, endpoint_display, stream=True
            ),
            latency_ms=duration_ms,
            classified=requires_encryption,
            usage=usage_for_meta,
            thinking_locked=is_thinking_locked(model) if model is not None else False,
            thinking_applied=thinking_applied,
        )
        if target_agent_id is not None:
            attach_agent_reply_observation(
                terminal_meta,
                completion_tokens=completion_tokens,
                usage_source=usage_source,
            )
    yield "event: anila.meta\n"
    yield "data: " + json.dumps(
        terminal_meta,
        ensure_ascii=False,
        separators=(",", ":"),
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
                usage_kind=usage_kind,
                invocation_id=invocation_id,
                model_name_snapshot=model_name_snapshot,
                token_source=usage_source,
                reasoning_tokens=reasoning_tokens,
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
                usage_kind=usage_kind,
                invocation_id=invocation_id,
                model_name_snapshot=model_name_snapshot,
                token_source=usage_source,
                reasoning_tokens=reasoning_tokens,
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
    endpoint_display: Optional[str] = None,
    *,
    tuning: ProxyTuning,
    model: ModelRegistry | None = None,
    caller_authorization: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    usage_kind: str = "inference",
    invocation_id: Optional[str] = None,
    model_name_snapshot: Optional[str] = None,
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
    model_type = "agent" if target_agent_id is not None else "llm"
    try:
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
            endpoint_display=endpoint_display,
            tuning=tuning,
            model=model,
            caller_authorization=caller_authorization,
            extra_headers=extra_headers,
            usage_kind=usage_kind,
            invocation_id=invocation_id,
            model_name_snapshot=model_name_snapshot,
        ):
            yield chunk
        _note_proxy_outcome(
            model_id=usage_model_id,
            model_name=model_name or f"id:{usage_model_id}",
            model_type=model_type,
            success=True,
        )
    except (GeneratorExit, asyncio.CancelledError) as exc:
        # Client disconnect / cancellation — no body left to write into.
        # Do NOT count as gateway/agent consecutive failure (user hung up).
        status = "failed"
        error = {"code": "stream_aborted", "message": type(exc).__name__}
        raise
    except HTTPException as exc:
        # StreamingResponse already committed HTTP 200 before the first
        # chunk; re-raising here becomes "response already started" and the
        # user sees silence. Emit a terminal SSE error instead.
        status = "failed"
        detail_code = (
            exc.detail.get("code")
            if isinstance(exc.detail, dict)
            and isinstance(exc.detail.get("code"), str)
            else None
        )
        error = {
            "code": detail_code or f"http_{exc.status_code}",
            "message": _http_exception_message(exc.detail),
        }
        logger.warning(
            "stream failure model=%s status=%s",
            model_name or "未知模型",
            exc.status_code,
        )
        if int(exc.status_code) >= 500:
            _note_proxy_outcome(
                model_id=usage_model_id,
                model_name=model_name or f"id:{usage_model_id}",
                model_type=model_type,
                success=False,
            )
        yield format_anila_stream_error(
            stream_failure_user_message(exc, model_name=model_name),
            code=detail_code,
        )
    except Exception as exc:
        status = "failed"
        error = {"code": "stream_error", "message": type(exc).__name__}
        logger.exception(
            "stream failure model=%s", model_name or "未知模型"
        )
        _note_proxy_outcome(
            model_id=usage_model_id,
            model_name=model_name or f"id:{usage_model_id}",
            model_type=model_type,
            success=False,
        )
        yield format_anila_stream_error(
            stream_failure_user_message(exc, model_name=model_name)
        )
    finally:
        if task_run_id is not None:
            finalize_task_run(task_run_id, status, error=error)

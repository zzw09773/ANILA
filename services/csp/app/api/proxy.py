"""OpenAI-compatible API proxy endpoints."""
import asyncio
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.middleware.caller import Caller, get_caller
from app.models.agent import Agent, UserAgentPermission
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.model_registry import ModelRegistry
from app.schemas.contracts.classification import ClassificationLevel
from app.services import memory_service
from app.services.api_key_service import check_model_permission, check_agent_permission
from app.services.auth_service import is_admin_tier
from app.services.proxy.ceiling import enforce_agent_ceiling, enforce_model_ceiling
from app.services.proxy.headers import resolve_model_gateway_key
from app.services.proxy.task_link import begin_task_run, finalize_task_run
from app.services.proxy.urls import join_upstream_path
from app.services.proxy_service import (
    build_default_anila_meta,
    downstream_identity,
    proxy_request,
    proxy_stream,
)
from app.services.endpoint_author_service import visible_endpoint_url

logger = logging.getLogger(__name__)


def _endpoint_display_for(
    db: Session,
    caller_user,
    endpoint_url: str,
    *,
    is_internal: bool = False,
) -> str:
    """Visibility-gated address for proxy trace / failure faces."""
    return visible_endpoint_url(
        endpoint_url,
        is_internal=is_internal,
        db=db,
        caller=caller_user,
    )

def _coerce_conversation_id(raw: str | None) -> int | None:
    """Convert the X-ANILA-Conversation-Id header to int for FK use.

    The header is free-form per the proxy contract — clients send the
    int row PK as a string today, but legacy / external callers may
    send non-numeric ids (e.g. UUIDs). Memory write paths need a real
    FK, so non-coercible values disable the writer for this turn but
    still allow the reader (which only depends on user_id).
    """
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _require_conversation_access(
    db: Session, caller: Caller, conversation_id: int
) -> None:
    """Ensure caller-supplied conversation id belongs to this caller.

    ``X-ANILA-Conversation-Id`` drives memory writes and classification
    latching, so accepting an arbitrary numeric id would let one user mutate
    another user's conversation metadata. Admin-tier callers retain the
    existing operational bypass.
    """
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.user_id != caller.user.id and not is_admin_tier(caller.user):
        raise HTTPException(status_code=403, detail="無權使用此 conversation")


def _agent_policy_level(agent) -> ClassificationLevel:
    """The agent's own four-level classification floor (SYSTEM-MAP §8).

    ``default_classification_level`` (set by the 3a migration bridge / manual
    inventory) is the source of truth; a ``requires_encryption=true`` agent
    without a manual level still floors at RESTRICTED(密) — preserves the old
    rank-2 floor that previously was 機密 in the five-level set
    (SYSTEM-MAP §8), so the legacy boolean stays byte-compatible.
    """
    level = ClassificationLevel.from_storage(
        getattr(agent, "default_classification_level", None) or "無機密"
    )
    if bool(getattr(agent, "requires_encryption", False)):
        level = ClassificationLevel.max_of(
            [level, ClassificationLevel.RESTRICTED]
        )
    return level


def _latch_agent_classification(
    db: Session, conversation_id: int, level: str = ClassificationLevel.RESTRICTED.to_storage()
) -> None:
    """Latch the routed agent's classification onto the conversation row.

    Slice 3b: routes through the four-level one-way core
    (``apply_classification`` reason=``agent_policy``) instead of the old raw
    ``UPDATE ... SET classified=TRUE``. The core mirrors the legacy boolean
    (``classified = level >= 密`` / RESTRICTED; old rank-2 floor,
    SYSTEM-MAP §8) so a hard refresh still latches the UI back into encrypted
    mode; it never lowers (single-direction), and leaves
    ``classification_inherited`` untouched (the source is agent policy, not
    memory inheritance — that path is handled separately below).
    """
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conversation_id),
        new_level=level,
        actor_type="service",
        actor_id="agent-policy",
        reason="agent_policy",
        source="agent_policy",
    )


def _latch_inherited_classification(db: Session, conversation_id: int) -> None:
    """Mark the conversation as classified-via-inheritance (memory recall).

    Slice 3b: routes through the four-level one-way core
    (``apply_classification`` reason=``memory_inherited``), which floors the
    row at RESTRICTED(密) — preserves the old rank-2 floor (SYSTEM-MAP §8),
    mirrors the legacy boolean AND flips ``classification_inherited=TRUE``
    on the raising event. One-way — never lowers a row already at 密 or higher.
    """
    from app.modules.policy import apply_classification
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conversation_id),
        new_level=ClassificationLevel.RESTRICTED.to_storage(),
        actor_type="service",
        actor_id="memory",
        reason="memory_inherited",
        source="memory_inherited",
    )


def _propagate_conversation_level_to_task(
    db: Session, task_id: int, conversation_id: int
) -> None:
    """Slice 3b: carry the conversation's effective level onto the linked
    task so later ceiling checks (doc 08 §4/§10) see it.

    reason=``source_selected`` — the runtime conversation is the selected
    source context feeding the task (doc 08 §4 task.level = max(...,
    source_snapshot.level, ...)). One-way core → never lowers the task.
    No-op when the conversation is unclassified (nothing to raise to).
    """
    from app.modules.policy import apply_classification, effective_level
    conv_level = effective_level(
        db, resource_type="conversation", resource_id=str(conversation_id)
    )
    # Not an OE-4 outbound gate: one-way latch/propagation skip when there
    # is nothing above 無機密 to raise the task to (SYSTEM-MAP §8 latch).
    if conv_level <= ClassificationLevel.UNCLASSIFIED:
        return
    apply_classification(
        db,
        resource_type="task",
        resource_id=str(task_id),
        new_level=conv_level.to_storage(),
        actor_type="service",
        actor_id="task-link",
        reason="source_selected",
        task_id=task_id,
        source="conversation_propagation",
    )


def _extract_assistant_text(payload: dict | None) -> str | None:
    """Pull the assistant message text out of an OpenAI chat response."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices") or []
    for c in choices:
        if not isinstance(c, dict):
            continue
        msg = c.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    return None


def _extract_latest_user_message(body: dict) -> str | None:
    """Pull the most recent user-role message text out of an OpenAI body."""
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        # Multimodal content: concatenate text parts only — image / audio
        # parts are dropped because the embedder is text-only.
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            joined = " ".join(p for p in parts if p)
            return joined or None
    return None


async def _inject_memory(
    db: Session,
    user_id: int,
    body: dict,
    *,
    exclude_conversation_id: int | None,
) -> memory_service.MemoryReadResult | None:
    """Mutate ``body`` in-place to prepend a memory block to system msg.

    Returns the read result (so the caller can inspect
    ``encryption_inherited``) or None when there's no user message to
    embed against. Failures are swallowed and logged — memory must
    not break chat.
    """
    user_text = _extract_latest_user_message(body)
    if not user_text:
        return None
    try:
        result = await memory_service.build_memory_block(
            db,
            user_id=user_id,
            latest_user_message=user_text,
            exclude_conversation_id=exclude_conversation_id,
        )
    except Exception:
        logger.exception("memory_service: build_memory_block failed user_id=%s", user_id)
        return None

    if not result.block:
        return result

    messages = list(body.get("messages") or [])
    # Find a leading system message to prepend the memory block to.
    # Some clients send the system role as messages[0]; if there isn't
    # one, we insert a fresh system message at index 0 so the memory
    # block always lands BEFORE the assistant sees user content.
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        existing = messages[0].get("content") or ""
        if isinstance(existing, str):
            messages[0] = {**messages[0], "content": f"{result.block}\n\n{existing}"}
        else:
            # Multimodal system content — push memory as a sibling text
            # part rather than touching the existing parts list.
            messages[0] = {
                **messages[0],
                "content": [{"type": "text", "text": result.block}, *list(existing)],
            }
    else:
        messages.insert(0, {"role": "system", "content": result.block})
    body["messages"] = messages
    return result


def _inject_attachments(
    db: Session,
    conversation_id: int | None,
    body: dict,
    model_name: str | None,
) -> "attachment_context.AttachmentInjectResult | None":
    """Mutate ``body`` to append conversation attachments to the system msg.

    Unlike memory injection, failures are recorded (not swallowed) so the
    chat handler can put a trace entry on ``anila_meta``. Chat still proceeds.
    Returns None when there is no conversation id (nothing to do).

    Admission is derived via ``admit()`` against this turn's model window —
    nothing is written back to extract_status. Loads metadata first, then
    ``extracted_text`` only for admitted ids.
    """
    from app.services import attachment_context

    if conversation_id is None:
        return None

    try:
        # Metadata only — do not pull unbounded extracted_text for every row.
        meta_rows = (
            db.query(
                Attachment.id,
                Attachment.filename,
                Attachment.page_count,
                Attachment.token_count,
                Attachment.extract_status,
                Attachment.extract_error,
                Attachment.created_at,
            )
            .filter(Attachment.conversation_id == conversation_id)
            .order_by(Attachment.created_at.asc(), Attachment.id.asc())
            .all()
        )
        if not meta_rows:
            return None

        # model_name None → explicit default-window fallback.
        context_window = attachment_context.get_context_window(db, model_name)
        budget = attachment_context.attachment_budget_tokens(context_window)
        admitted_list, excluded_list = attachment_context.admit(meta_rows, budget)
        admitted_set = set(admitted_list)

        text_by_id: dict[int, str | None] = {}
        if admitted_list:
            text_by_id = dict(
                db.query(Attachment.id, Attachment.extracted_text)
                .filter(Attachment.id.in_(admitted_list))
                .all()
            )

        class _PromptRow:
            __slots__ = (
                "id", "filename", "page_count", "token_count",
                "extract_status", "extract_error", "extracted_text",
            )

            def __init__(self, row, text: str | None):
                self.id = row.id
                self.filename = row.filename
                self.page_count = row.page_count
                self.token_count = row.token_count
                self.extract_status = row.extract_status
                self.extract_error = row.extract_error
                self.extracted_text = text

        views = [
            _PromptRow(
                r,
                text_by_id.get(r.id) if r.id in admitted_set else None,
            )
            for r in meta_rows
        ]
        block = attachment_context.build_attachment_prompt_block(
            views, admitted_ids=admitted_set,
        )
        if block is None:
            return None

        messages = list(body.get("messages") or [])
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            existing = messages[0].get("content") or ""
            if isinstance(existing, str):
                messages[0] = {
                    **messages[0],
                    "content": f"{existing}\n\n{block}" if existing else block,
                }
            else:
                messages[0] = {
                    **messages[0],
                    "content": [
                        *list(existing),
                        {"type": "text", "text": block},
                    ],
                }
        else:
            messages.insert(0, {"role": "system", "content": block})
        body["messages"] = messages

        ok_n = len(admitted_list)
        pending_n = sum(
            1 for a in views if (a.extract_status or "") == "pending"
        )
        omitted_n = (
            len(excluded_list)
            + sum(
                1
                for a in views
                if (a.extract_status or "") in (
                    "failed", "unsupported", "too_large",
                )
            )
        )
        detail_parts = [f"納入 {ok_n} 份"]
        if pending_n:
            detail_parts.append(f"處理中 {pending_n} 份")
        if omitted_n:
            detail_parts.append(f"未納入 {omitted_n} 份")
        return attachment_context.AttachmentInjectResult(
            status="ok" if omitted_n == 0 and pending_n == 0 else "partial",
            label="附件注入",
            detail="；".join(detail_parts),
            injected_count=ok_n,
        )
    except Exception as exc:
        logger.exception(
            "attachment inject failed conv_id=%s", conversation_id,
        )
        return attachment_context.AttachmentInjectResult(
            status="error",
            label="附件注入",
            detail=f"注入失敗：{type(exc).__name__}",
            skipped=True,
        )


def _merge_attachment_trace(payload: dict, inject_result) -> dict:
    """Append an attachment trace entry onto ``payload['anila_meta']``."""
    if inject_result is None or not isinstance(payload, dict):
        return payload
    meta = payload.get("anila_meta")
    if not isinstance(meta, dict):
        meta = build_default_anila_meta(
            "attachments",
            detail="attachment inject",
        )
        payload["anila_meta"] = meta
    trace = meta.get("trace")
    if not isinstance(trace, list):
        trace = []
        meta["trace"] = trace
    trace.append(inject_result.to_trace_entry())
    return payload


async def _sse_with_attachment_trace(
    upstream: AsyncIterator[str],
    inject_result,
) -> AsyncIterator[str]:
    """Inject the attachment trace entry into streaming ``anila.meta`` frames.

    Mirrors ``_merge_attachment_trace`` for the SSE path: the terminal
    metadata frame (synthesised by proxy_stream when the downstream omits
    one, or forwarded when present) carries the same attachment entry as
    the non-streaming ``anila_meta.trace``. Stays in proxy.py so the
    P1.5 change set does not touch ``proxy/service.py``.
    """
    import json

    if inject_result is None:
        async for chunk in upstream:
            yield chunk
        return

    entry = inject_result.to_trace_entry()
    buf = ""
    async for chunk in upstream:
        buf += chunk
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            block_out = block + "\n\n"
            event_name = None
            data_line = None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_line = line[5:].strip()
            if event_name == "anila.meta" and data_line and data_line != "[DONE]":
                try:
                    meta = json.loads(data_line)
                except (json.JSONDecodeError, TypeError):
                    yield block_out
                    continue
                if isinstance(meta, dict):
                    trace = meta.get("trace")
                    if not isinstance(trace, list):
                        trace = []
                        meta["trace"] = trace
                    trace.append(entry)
                    yield (
                        "event: anila.meta\n"
                        + "data: "
                        + json.dumps(meta, ensure_ascii=False)
                        + "\n\n"
                    )
                    continue
            yield block_out
    if buf:
        yield buf


def _schedule_memory_write(
    *,
    user_id: int,
    conversation_id: int | None,
    user_message: str | None,
    assistant_message: str | None,
    is_encrypted: bool,
) -> None:
    """Fire-and-forget the post-turn memory writer.

    Skips silently if the conversation FK is missing (legacy header
    formats) or either side of the turn is empty.
    """
    if conversation_id is None or not user_message or not assistant_message:
        return
    try:
        asyncio.create_task(
            memory_service.persist_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
                is_encrypted=is_encrypted,
            )
        )
    except RuntimeError:
        # No running event loop (shouldn't happen inside FastAPI but
        # be defensive — proxy.py is also imported in test contexts).
        logger.warning("memory_service: no event loop, skipping persist_turn")


async def _tee_stream_capture_assistant(
    upstream: AsyncIterator[str],
    *,
    on_complete: callable,
) -> AsyncIterator[str]:
    """Pass SSE chunks through while collecting assistant text.

    The upstream generator (``proxy_stream``) emits server-sent-event
    blocks; we forward them verbatim and inspect ``data:`` lines to
    pull out the assistant delta text. After the stream finishes,
    ``on_complete`` is called with the assembled assistant string so
    the memory writer can persist the turn.
    """
    import json

    def _choices(chunk: dict) -> list:
        choices = chunk.get("choices")
        if isinstance(choices, list):
            return choices
        choice = chunk.get("choice")
        if isinstance(choice, list):
            return choice
        if isinstance(choice, dict):
            return [choice]
        return []

    def _content(value) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            return ""
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    out.append(text)
        return "".join(out)

    parts: list[str] = []
    try:
        async for block in upstream:
            # SSE block format: "event: foo\ndata: {...}\n\n" — extract
            # the data payload and pull assistant-visible text if present.
            for line in block.split("\n"):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # OpenAI streaming format plus registered agents that send
                # complete assistant messages inside SSE frames.
                for c in _choices(chunk):
                    if not isinstance(c, dict):
                        continue
                    delta = c.get("delta") if isinstance(c.get("delta"), dict) else {}
                    message = (
                        c.get("message") if isinstance(c.get("message"), dict) else {}
                    )
                    txt = _content(delta.get("content")) or _content(
                        message.get("content")
                    )
                    if txt:
                        parts.append(txt)
            yield block
    finally:
        try:
            on_complete("".join(parts))
        except Exception:
            logger.exception("memory_service: on_complete callback failed")

router = APIRouter(tags=["API 代理"])


def _resolve_model(db: Session, caller: Caller, model_name: str) -> ModelRegistry:
    """Resolve model name to registry entry and check caller permissions."""
    model = db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 未註冊")
    if not model.is_active:
        raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 已停用")
    if not check_model_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, model_id=model.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權使用模型 '{model_name}'",
        )
    return model


def _resolve_agent(db: Session, caller: Caller, agent_name: str) -> Agent | None:
    """Return the Agent if agent_name matches an approved agent, else None."""
    agent = (
        db.query(Agent)
        .filter(Agent.name == agent_name, Agent.approval_status == "approved")
        .first()
    )
    if agent is None:
        return None
    if not check_agent_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, agent_id=agent.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權呼叫 agent '{agent_name}'",
        )
    return agent


@router.get("/v1/agents")
def list_available_agents(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """Data-plane endpoint: return approved agents available to the caller.

    Used by RemoteAgentRegistry in the Router to discover agents.
    Response mirrors OpenAI /v1/models shape.
    """
    user = caller.user

    # admin + owner 都看得到所有 approved agent;一般 user 必須有
    # UserAgentPermission 顯式授權才看得到。先前漏掉 owner,讓 owner
    # 在 ANILA UI 看到的 agent 清單可能跟 CSP UI (受同樣 bug 影響) 對不上。
    if is_admin_tier(user):
        agents = db.query(Agent).filter(Agent.approval_status == "approved").all()
    else:
        agents = (
            db.query(Agent)
            .join(UserAgentPermission, UserAgentPermission.agent_id == Agent.id)
            .filter(
                UserAgentPermission.user_id == user.id,
                Agent.approval_status == "approved",
            )
            .all()
        )

    # Encryption is an agent-level policy only. Base models (LLMs) do NOT carry
    # a requires_encryption flag — classification is decided per-agent so the
    # same LLM can serve both classified and non-classified agents.
    data = [
        {
            "id": a.name,
            "object": "agent",
            "name": a.name,
            "description_for_router": a.description_for_router,
            "endpoint_url": a.endpoint_url,
            "capabilities": a.capabilities or {},
            "input_schema": a.input_schema,
            "requires_encryption": bool(getattr(a, "requires_encryption", False)),
        }
        for a in agents
    ]
    return JSONResponse({"object": "list", "data": data})


@router.get("/v1/models")
async def list_models_openai(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """OpenAI-compatible model discovery.

    Mirrors ``GET https://api.openai.com/v1/models`` so off-the-shelf clients
    (OpenWebUI, LangChain, official openai-python, LlamaIndex) can point at
    ``https://<host>/v1`` with an API key and auto-discover usable models —
    without our custom ``/api/models`` shape. Returns only models the caller
    is permitted to use via ``check_model_permission`` (same gate as
    ``/v1/chat/completions``), so the discovery list cannot be used to widen
    a key's effective scope.
    """
    rows = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.id)
        .all()
    )
    visible = [
        m for m in rows
        if check_model_permission(
            db, user=caller.user, api_key_id=caller.api_key_id, model_id=m.id
        )
    ]
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": m.name,
                "object": "model",
                "created": int(m.created_at.timestamp()) if m.created_at else 0,
                "owned_by": "anila",
            }
            for m in visible
        ],
    })


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    stream: bool = body.get("stream", False)
    user = caller.user
    department_id = user.department_id
    user_email = user.email
    # 員編 forwarded as the downstream wire identity (None for non-card
    # accounts → identity header omitted, never forged; the request still
    # proceeds). user.id (PK) is still used for usage rows.
    user_identity = downstream_identity(user)

    # Audit fields from optional client headers
    conversation_id: str | None = request.headers.get("X-ANILA-Conversation-Id")
    trace_id: str | None = request.headers.get("X-ANILA-Trace-Id")

    # ── Memory: read path (sync, ~150ms) ─────────────────────────────────────
    # Inject the user's long-term memory block into the system prompt
    # BEFORE forwarding downstream. We need this regardless of agent/model
    # path so do it once here. The conv_id (if numeric) is excluded from
    # RAG because the active conversation's history is already in the
    # messages array — re-injecting would just waste prompt tokens.
    conv_id_int = _coerce_conversation_id(conversation_id)
    if conv_id_int is not None:
        _require_conversation_access(db, caller, conv_id_int)
    memory_read = await _inject_memory(
        db,
        user.id,
        body,
        exclude_conversation_id=conv_id_int,
    )
    # P1.5: whole-document attachment injection (after memory). Failures are
    # recorded on attach_inject for anila_meta.trace; chat still proceeds.
    attach_inject = _inject_attachments(
        db, conv_id_int, body, model_name,
    )
    # P3: latch the consuming conversation into classified state when
    # memory recall pulled at least one encrypted chunk. One-shot — once
    # set, never cleared by a later non-encrypted turn (would otherwise
    # let a single clean turn launder the classification). Only writes
    # when we actually have a conversation FK and the row exists.
    if (
        conv_id_int is not None
        and memory_read
        and memory_read.encryption_inherited
    ):
        try:
            _latch_inherited_classification(db, conv_id_int)
        except Exception:
            logger.exception(
                "memory_service: classification latch failed conv_id=%s",
                conv_id_int,
            )
    # Capture the user message text NOW (after memory injection but
    # before any downstream mutation) so the post-turn writer has the
    # exact string the user sent.
    captured_user_text = _extract_latest_user_message(
        # _inject_memory may have altered the messages list; use the
        # last user message which is unchanged across that path.
        body
    )

    # Try agent first, fallback to model_registry
    agent = _resolve_agent(db, caller, model_name)
    if agent:
        agent_requires_encryption = bool(getattr(agent, "requires_encryption", False))
        # P3 hook: if any retrieved memory chunk was encrypted at write
        # time, inherit that classification onto this turn even if the
        # current agent isn't itself encrypted (Bell-LaPadula no-write-
        # down). For P1 we just OR them — UI / latch wiring lands in P3.
        if memory_read and memory_read.encryption_inherited:
            agent_requires_encryption = True
        # Persist classified state to the conversation row so it survives
        # hard refresh. ROUTER routing to an encrypted downstream agent
        # is the canonical case: conversation.agent_id stays NULL (router)
        # but the row's classified flag must record the encrypted turn so
        # the next GET /api/conversations latches the UI back into
        # encrypted mode.
        # Slice 3b: latch the agent's OWN four-level classification onto the
        # conversation (reason=agent_policy) through the one-way core. Uses
        # the agent's default level, floored at RESTRICTED(密) when
        # requires_encryption — old rank-2 floor preserved (SYSTEM-MAP §8).
        # The OR'd ``agent_requires_encryption`` still drives the wire meta
        # below; the memory-inheritance contribution is latched separately.
        if conv_id_int is not None:
            agent_level = _agent_policy_level(agent)
            # Not an OE-4 outbound gate: classification latch onto the
            # conversation row when the agent policy level is above floor.
            if agent_level > ClassificationLevel.UNCLASSIFIED:
                try:
                    _latch_agent_classification(
                        db, conv_id_int, agent_level.to_storage()
                    )
                except Exception:
                    logger.exception(
                        "agent classification latch failed conv_id=%s",
                        conv_id_int,
                    )
        # Slice 2b-C: optional X-ANILA-Task-Id — validate access, record
        # the task.run PolicyDecision and open a TaskRun BEFORE dispatch.
        # None → legacy traffic (usage row marked legacy_runtime_call).
        task_ctx = begin_task_run(
            db,
            caller=caller,
            request_headers=request.headers,
            dispatch_target="agent",
            resource_type="agent",
            resource_id=str(agent.id),
        )
        # Slice 3b: propagate the conversation's effective level onto the
        # linked task (reason=source_selected) so later ceiling checks see it.
        if task_ctx is not None and conv_id_int is not None:
            try:
                _propagate_conversation_level_to_task(
                    db, task_ctx.task_id, conv_id_int
                )
            except Exception:
                logger.exception(
                    "task classification propagation failed task_id=%s",
                    task_ctx.task_id,
                )
        enforce_agent_ceiling(
            db,
            agent=agent,
            caller=caller,
            task_ctx=task_ctx,
            conv_id_int=conv_id_int,
        )
        # Usage attribution: inbound X-ANILA-Trace-Id wins (legacy
        # contract); a task-linked call without one falls back to the
        # task row's trace id (doc 04 AC10 歸戶).
        usage_trace_id = trace_id or (task_ctx.trace_id if task_ctx else None)
        if stream:
            upstream = proxy_stream(
                target_url=join_upstream_path(
                    agent.endpoint_url, "/v1/chat/completions"
                ),
                api_key_id=caller.api_key_id,
                user_id=user.id,
                department_id=department_id,
                usage_model_id=agent.id,
                request_body=body,
                user_email=user_email,
                user_identity=user_identity,
                model_name=agent.name,
                conversation_id=conversation_id,
                trace_id=usage_trace_id,
                requires_encryption=agent_requires_encryption,
                # Sprint 8 X / Phase G — caller attribution.
                #   target_agent_id  → proxy_service picks the per-agent
                #                      service token from agent_credentials
                #                      (5-min in-memory cache) instead of
                #                      the legacy fleet-shared env var.
                #   caller_agent_id  → token_usage row for this LLM call
                #                      gets attributed to the agent so
                #                      "top-agents" / "by-base-model"
                #                      dashboards can rollup correctly.
                target_agent_id=agent.id,
                caller_agent_id=agent.id,
                # Slice 2b-C — task linkage (headers + usage + run finish).
                task_id=task_ctx.task_id if task_ctx else None,
                task_trace_id=task_ctx.trace_id if task_ctx else None,
                task_run_id=task_ctx.task_run_id if task_ctx else None,
                legacy_runtime_call=task_ctx is None,
                endpoint_display=_endpoint_display_for(
                    db, user, agent.endpoint_url
                ),
            )
            # Tee the SSE so we can capture the final assistant text and
            # schedule the memory writer once the stream drains.
            teed = _tee_stream_capture_assistant(
                upstream,
                on_complete=lambda assistant_text: _schedule_memory_write(
                    user_id=user.id,
                    conversation_id=conv_id_int,
                    user_message=captured_user_text,
                    assistant_message=assistant_text,
                    is_encrypted=agent_requires_encryption,
                ),
            )
            # Same attachment trace entry as non-streaming anila_meta.
            traced = _sse_with_attachment_trace(teed, attach_inject)
            return StreamingResponse(
                traced,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # Non-streaming agent call — use proxy_request with a synthetic ModelRegistry-like obj
        # by forwarding to the agent endpoint directly
        import httpx
        from fastapi import HTTPException as _HTTPException
        target = join_upstream_path(agent.endpoint_url, "/v1/chat/completions")
        from anila_core.security import ENDPOINT_KIND_AGENT
        from app.services.proxy_service import (
            _aggregate_sse_to_chat_completion,
            build_agent_headers,
            _guard_outbound,
        )
        _guard_outbound(
            target, endpoint_kind=ENDPOINT_KIND_AGENT
        )  # call-time SSRF re-validation (TOCTOU defense) — FINAL url
        # Phase G: also pass target_agent_id so the per-agent token + cache
        # path applies to non-streaming calls. usage_writer attribution for
        # this branch is still TODO — non-streaming agent forwards don't
        # currently emit a token_usage row at all (orthogonal pre-existing
        # gap, tracked in Sprint 9 X follow-ups).
        headers = build_agent_headers(
            user_identity,
            user_email,
            target_agent_id=agent.id,
            # Slice 2b-C (doc 05 §4): task/trace ids ride on agent dispatch.
            task_id=task_ctx.task_id if task_ctx else None,
            trace_id=task_ctx.trace_id if task_ctx else None,
        )
        started_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
                resp = await client.post(target, json=body, headers=headers)
                resp.raise_for_status()
                # SSE-only agents (e.g. asrd) ignore ``stream: false`` and
                # respond with event-stream regardless. Aggregate in that
                # case so the caller still gets JSON.
                ct = resp.headers.get("content-type", "")
                preview = resp.text[:8].lstrip()
                if "text/event-stream" in ct or preview.startswith("data:"):
                    payload = _aggregate_sse_to_chat_completion(resp.text, agent.name)
                else:
                    payload = resp.json()
                existing_meta = payload.get("anila_meta")
                if not existing_meta:
                    # Caller-facing detail names the agent, never the
                    # upstream address (twin of the model-proxy fix).
                    _ep = _endpoint_display_for(
                        db, user, agent.endpoint_url
                    )
                    payload["anila_meta"] = build_default_anila_meta(
                        agent.name,
                        detail=f"CSP proxy -> {agent.name}（{_ep}）",
                        latency_ms=int((time.time() - started_at) * 1000),
                        classified=agent_requires_encryption,
                    )
                elif agent_requires_encryption and isinstance(existing_meta, dict):
                    existing_meta["classified"] = True
                # Memory write (non-streaming agent path)
                assistant_text = _extract_assistant_text(payload)
                _schedule_memory_write(
                    user_id=user.id,
                    conversation_id=conv_id_int,
                    user_message=captured_user_text,
                    assistant_message=assistant_text,
                    is_encrypted=agent_requires_encryption,
                )
                # Slice 2b-C: run finished. (This branch still writes no
                # usage row — orthogonal pre-existing gap, see above.)
                if task_ctx is not None:
                    finalize_task_run(task_ctx.task_run_id, "completed")
                return _merge_attachment_trace(payload, attach_inject)
        except httpx.HTTPStatusError as e:
            logger.error(
                "Agent %s 上游 HTTP 錯誤 url=%s: %s",
                agent.name,
                target,
                e,
            )
            if task_ctx is not None:
                finalize_task_run(
                    task_ctx.task_run_id,
                    "failed",
                    error={
                        "code": f"http_{e.response.status_code}",
                        "message": f"Agent「{agent.name}」上游回應錯誤",
                    },
                )
            raise _HTTPException(
                status_code=e.response.status_code,
                detail=(
                    f"Agent「{agent.name}」上游回應錯誤"
                    f"（HTTP {e.response.status_code}）"
                ),
            )
        except Exception as e:
            # Fixed caller-facing text; exception may embed the URL.
            logger.error(
                "Agent %s 呼叫失敗 url=%s: %s",
                agent.name,
                target,
                e,
                exc_info=True,
            )
            if task_ctx is not None:
                finalize_task_run(
                    task_ctx.task_run_id,
                    "failed",
                    error={
                        "code": "agent_call_failed",
                        "message": f"Agent「{agent.name}」呼叫失敗",
                    },
                )
            raise _HTTPException(
                status_code=502,
                detail=f"Agent「{agent.name}」呼叫失敗",
            )

    model = _resolve_model(db, caller, model_name)
    # Direct LLM calls (not through an agent) do NOT trigger CSP-side classified
    # latch. Encryption is agent-level policy; the same LLM can back both
    # classified and non-classified agents. Downstream-reported classified=True
    # still latches via proxy_service's normal meta merge.
    # Inheritance: if memory injected encrypted material, latch this
    # direct-LLM call as encrypted too (matches agent path semantics).
    inherited_encryption = bool(memory_read and memory_read.encryption_inherited)
    # Slice 2b-C: optional X-ANILA-Task-Id — same wiring as the agent
    # branch, dispatch_target/resource_type = "model". Outbound headers to
    # the model gateway stay minimal (doc 04 §3/AC5) — the task ids below
    # only reach the usage row + run lifecycle, never the gateway headers.
    task_ctx = begin_task_run(
        db,
        caller=caller,
        request_headers=request.headers,
        dispatch_target="model",
        resource_type="model",
        resource_id=str(model.id),
    )
    # Slice 3b: propagate the conversation's effective level onto the linked
    # task (reason=source_selected). On the direct-model path the conversation
    # may still be classified via memory inheritance (latched above).
    if task_ctx is not None and conv_id_int is not None:
        try:
            _propagate_conversation_level_to_task(
                db, task_ctx.task_id, conv_id_int
            )
        except Exception:
            logger.exception(
                "task classification propagation failed task_id=%s",
                task_ctx.task_id,
            )
    # Slice 6a (doc 04 §5/§8) + OE-4/G4: classification ceiling check BEFORE
    # the outbound model call. Covers task-linked AND legacy traffic. A
    # violation raises 403 + records a model.invoke deny row and never
    # dispatches upstream; a pass records an allow row when task-linked OR
    # level ≥ 營業秘密.
    enforce_model_ceiling(
        db,
        model=model,
        caller=caller,
        task_ctx=task_ctx,
        conv_id_int=conv_id_int,
    )
    usage_trace_id = trace_id or (task_ctx.trace_id if task_ctx else None)
    if stream:
        chat_path = (
            "/v2/chat/completions"
            if model.api_version == "v2"
            else "/v1/chat/completions"
        )
        target_url = join_upstream_path(model.endpoint_url, chat_path)
        upstream = proxy_stream(
            target_url=target_url,
            api_key_id=caller.api_key_id,
            user_id=user.id,
            department_id=department_id,
            usage_model_id=model.id,
            request_body=body,
            user_email=user_email,
            user_identity=user_identity,
            model_name=model.name,
            conversation_id=conversation_id,
            trace_id=usage_trace_id,
            requires_encryption=inherited_encryption,
            task_id=task_ctx.task_id if task_ctx else None,
            task_trace_id=task_ctx.trace_id if task_ctx else None,
            task_run_id=task_ctx.task_run_id if task_ctx else None,
            legacy_runtime_call=task_ctx is None,
            # Slice 6a: per-model gateway key (secret ref first, env fallback).
            gateway_api_key=resolve_model_gateway_key(model),
            endpoint_display=_endpoint_display_for(
                db,
                user,
                model.endpoint_url,
                is_internal=bool(getattr(model, "is_internal", False)),
            ),
        )
        teed = _tee_stream_capture_assistant(
            upstream,
            on_complete=lambda assistant_text: _schedule_memory_write(
                user_id=user.id,
                conversation_id=conv_id_int,
                user_message=captured_user_text,
                assistant_message=assistant_text,
                is_encrypted=inherited_encryption,
            ),
        )
        # Same attachment trace entry as non-streaming anila_meta.
        traced = _sse_with_attachment_trace(teed, attach_inject)
        return StreamingResponse(
            traced,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    payload = await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=user.id,
        user_identity=user_identity,
        department_id=department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
        conversation_id=conversation_id,
        trace_id=usage_trace_id,
        requires_encryption=inherited_encryption,
        task_id=task_ctx.task_id if task_ctx else None,
        task_trace_id=task_ctx.trace_id if task_ctx else None,
        task_run_id=task_ctx.task_run_id if task_ctx else None,
        legacy_runtime_call=task_ctx is None,
        endpoint_display=_endpoint_display_for(
            db,
            user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
    )
    assistant_text = _extract_assistant_text(payload)
    _schedule_memory_write(
        user_id=user.id,
        conversation_id=conv_id_int,
        user_message=captured_user_text,
        assistant_message=assistant_text,
        is_encrypted=inherited_encryption,
    )
    return _merge_attachment_trace(payload, attach_inject)


@router.post("/v1/agents/{agent_name}/sessions/{session_id}/answer")
async def resume_agent_session(
    agent_name: str,
    session_id: str,
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """Sprint 13 PR A2 — Router-driven resume proxy for paused agent runs.

    The Router persists ``session_id → agent_id`` per dispatch and uses
    this endpoint to forward the user's answer to the agent that owns
    the paused run. Identity injection + per-agent service-token swap
    use the same machinery as agent ``chat_completions`` so audit and
    token attribution are consistent.

    Body shape mirrors the agent's ``/sessions/{id}/answer``::

        { "interrupt_id": str,
          "answer": str | dict,
          "max_turns": int (optional),
          "model": str (optional),
          "system_prompt": str (optional) }

    Response: SSE stream of the resumed turn, passed through verbatim.
    """
    body = await request.json()
    agent = _resolve_agent(db, caller, agent_name)
    if agent is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_name}' 未註冊或未審核",
        )

    user = caller.user
    target = (
        f"{agent.endpoint_url.rstrip('/')}/sessions/{session_id}/answer"
    )
    from anila_core.security import ENDPOINT_KIND_AGENT
    from app.services.proxy_service import build_agent_headers, _guard_outbound
    _guard_outbound(
        target, endpoint_kind=ENDPOINT_KIND_AGENT
    )  # call-time SSRF re-validation (TOCTOU defense)
    headers = build_agent_headers(
        downstream_identity(user), user.email, target_agent_id=agent.id,
    )

    import httpx

    async def _passthrough_stream():
        try:
            async with httpx.AsyncClient(timeout=float(settings.LLM_TIMEOUT)) as client:
                async with client.stream(
                    "POST", target, json=body, headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        err = await resp.aread()
                        # Surface the upstream error inline so the
                        # caller's SSE framing stays valid.
                        msg = err[:300].decode("utf-8", errors="replace")
                        yield (
                            f"event: error\n"
                            f"data: {{\"status\": {resp.status_code}, "
                            f"\"detail\": {msg!r}}}\n\n"
                        )
                        return
                    async for raw_line in resp.aiter_lines():
                        if raw_line == "":
                            yield "\n"
                        else:
                            yield raw_line + "\n"
        except httpx.RequestError as exc:
            yield (
                f"event: error\n"
                f"data: {{\"status\": 502, "
                f"\"detail\": \"agent connection error: "
                f"{type(exc).__name__}\"}}\n\n"
            )

    return StreamingResponse(
        _passthrough_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/embeddings")
async def embeddings_v1(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, caller, model_name)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        user_identity=downstream_identity(caller.user),
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v1/embeddings",
        endpoint_display=_endpoint_display_for(
            db,
            caller.user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
    )


@router.post("/v2/embeddings")
async def embeddings_v2(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, caller, model_name)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        user_identity=downstream_identity(caller.user),
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v2/embeddings",
        endpoint_display=_endpoint_display_for(
            db,
            caller.user,
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
        ),
    )

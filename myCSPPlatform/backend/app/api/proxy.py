"""OpenAI-compatible API proxy endpoints."""
import asyncio
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.caller import Caller, get_caller
from app.models.agent import Agent, UserAgentPermission
from app.models.model_registry import ModelRegistry
from app.services import memory_service
from app.services.api_key_service import check_model_permission, check_agent_permission
from app.services.auth_service import is_admin_tier
from app.services.proxy_service import build_default_anila_meta, proxy_request, proxy_stream

logger = logging.getLogger(__name__)


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


def _latch_agent_classification(db: Session, conversation_id: int) -> None:
    """Persist conversations.classified=true when the routed agent has
    ``requires_encryption=true``. Idempotent — no-ops on rows already
    classified. Distinct from ``_latch_inherited_classification`` in that
    it leaves ``classification_inherited=FALSE`` (the source is the
    agent's own policy, not memory inheritance).

    Without this latch the classified flag only lived on the SSE
    ``anila_meta`` payload — front-ends could latch the in-memory
    conversation, but a hard refresh re-read the row from the DB and
    found ``classified=false``, silently dropping encryption mode.
    """
    from sqlalchemy import text as sql_text
    db.execute(
        sql_text(
            """
            UPDATE conversations
               SET classified = TRUE,
                   classified_at = COALESCE(classified_at, CURRENT_TIMESTAMP)
             WHERE id = :conv_id
               AND classified = FALSE
            """
        ),
        {"conv_id": conversation_id},
    )
    db.commit()


def _latch_inherited_classification(db: Session, conversation_id: int) -> None:
    """Mark the conversation as classified-via-inheritance, one-shot.

    Idempotent — calling twice on the same row is a no-op (the WHERE
    clause filters out rows already in the inherited state). Doesn't
    overwrite a manual / agent-driven classification that didn't go
    through inheritance: those rows already have classified=true and
    classification_inherited=false, and the WHERE clause skips them.
    Net effect: ``classification_inherited`` only becomes TRUE when
    the latch path is the first thing to flip it.

    The current behaviour is "either path can flip classified=true,
    only the first path stamps the timestamp / source flags". A
    cleaner design would model classification as an event log rather
    than a snapshot, but a single boolean + timestamp is enough for
    P3's UI needs and avoids a much larger schema migration.
    """
    from sqlalchemy import text as sql_text
    db.execute(
        sql_text(
            """
            UPDATE conversations
               SET classified = TRUE,
                   classification_inherited = TRUE,
                   classified_at = COALESCE(classified_at, CURRENT_TIMESTAMP)
             WHERE id = :conv_id
               AND classification_inherited = FALSE
            """
        ),
        {"conv_id": conversation_id},
    )
    db.commit()


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
    parts: list[str] = []
    try:
        async for block in upstream:
            # SSE block format: "event: foo\ndata: {...}\n\n" — extract
            # the data payload and pull "delta.content" if present.
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
                # OpenAI streaming format
                choices = chunk.get("choices") or []
                for c in choices:
                    delta = c.get("delta") or {}
                    txt = delta.get("content")
                    if isinstance(txt, str) and txt:
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
    memory_read = await _inject_memory(
        db,
        user.id,
        body,
        exclude_conversation_id=conv_id_int,
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
        if agent_requires_encryption and conv_id_int is not None:
            try:
                _latch_agent_classification(db, conv_id_int)
            except Exception:
                logger.exception(
                    "agent classification latch failed conv_id=%s",
                    conv_id_int,
                )
        if stream:
            upstream = proxy_stream(
                target_url=f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions",
                api_key_id=caller.api_key_id,
                user_id=user.id,
                department_id=department_id,
                usage_model_id=agent.id,
                request_body=body,
                user_email=user_email,
                inject_identity=True,
                model_name=agent.name,
                conversation_id=conversation_id,
                trace_id=trace_id,
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
            return StreamingResponse(
                teed,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # Non-streaming agent call — use proxy_request with a synthetic ModelRegistry-like obj
        # by forwarding to the agent endpoint directly
        import httpx
        from fastapi import HTTPException as _HTTPException
        target = f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions"
        from app.services.proxy_service import _build_downstream_headers, _aggregate_sse_to_chat_completion
        # Phase G: also pass target_agent_id so the per-agent token + cache
        # path applies to non-streaming calls. usage_writer attribution for
        # this branch is still TODO — non-streaming agent forwards don't
        # currently emit a token_usage row at all (orthogonal pre-existing
        # gap, tracked in Sprint 9 X follow-ups).
        headers = _build_downstream_headers(user.id, user_email, target_agent_id=agent.id)
        started_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
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
                    payload["anila_meta"] = build_default_anila_meta(
                        agent.name,
                        detail=f"CSP proxy -> {target}",
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
                return payload
        except httpx.HTTPStatusError as e:
            raise _HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise _HTTPException(status_code=502, detail=f"Agent 呼叫失敗: {e}")

    model = _resolve_model(db, caller, model_name)
    # Direct LLM calls (not through an agent) do NOT trigger CSP-side classified
    # latch. Encryption is agent-level policy; the same LLM can back both
    # classified and non-classified agents. Downstream-reported classified=True
    # still latches via proxy_service's normal meta merge.
    # Inheritance: if memory injected encrypted material, latch this
    # direct-LLM call as encrypted too (matches agent path semantics).
    inherited_encryption = bool(memory_read and memory_read.encryption_inherited)
    if stream:
        target_url = (
            f"{model.endpoint_url.rstrip('/')}/v2/chat/completions"
            if model.api_version == "v2"
            else f"{model.endpoint_url.rstrip('/')}/v1/chat/completions"
        )
        upstream = proxy_stream(
            target_url=target_url,
            api_key_id=caller.api_key_id,
            user_id=user.id,
            department_id=department_id,
            usage_model_id=model.id,
            request_body=body,
            user_email=user_email,
            inject_identity=False,
            model_name=model.name,
            conversation_id=conversation_id,
            trace_id=trace_id,
            requires_encryption=inherited_encryption,
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
        return StreamingResponse(
            teed,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    payload = await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=user.id,
        department_id=department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
        conversation_id=conversation_id,
        trace_id=trace_id,
        requires_encryption=inherited_encryption,
    )
    assistant_text = _extract_assistant_text(payload)
    _schedule_memory_write(
        user_id=user.id,
        conversation_id=conv_id_int,
        user_message=captured_user_text,
        assistant_message=assistant_text,
        is_encrypted=inherited_encryption,
    )
    return payload


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
    from app.services.proxy_service import _build_downstream_headers
    headers = _build_downstream_headers(
        user.id, user.email, target_agent_id=agent.id,
    )

    import httpx

    async def _passthrough_stream():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
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
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v1/embeddings",
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
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v2/embeddings",
    )

"""FastAPI server — thin shell over the AgenticRAG engine.

Endpoints:
  POST /chat                     — start a query loop, return SSE stream
  GET  /sessions/{id}/away_summary — generate away recap
  POST /sessions/{id}/compact    — trigger manual compact

The server is intentionally thin — business logic lives in QueryEngine.
Deployment configuration (vLLM URL, auth, monitoring) is injected via
dependency injection, not hardcoded here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..engine.query_engine import QueryConfig, QueryEngine
from ..models.message import Usage, UserMessage
from ..providers.base import Provider
from ..router.tool_router import ToolRegistry
from ..runtime.user_memory import (
    AgenticRagCallerContext,
    extract_caller_context,
    fetch_user_facts,
    format_user_facts_block,
)
from .documents import router as documents_router, set_ingestion_service
from .search import router as search_router, set_search_providers
from .middleware.auth import ApiKeyMiddleware
from .middleware.loader import install_csp_middleware
from .events import (
    ErrorPayload,
    EventType,
    MessageDeltaPayload,
    ServerEvent,
    ToolCallStartedPayload,
    UsageUpdatePayload,
)

logger = logging.getLogger(__name__)


def _parse_history(history: list[dict[str, Any]]) -> list[Any]:
    """Convert raw history dicts into typed Message objects.

    Only "user" and "assistant" roles are supported.  Unknown or malformed
    entries are logged and skipped so a single bad item does not abort the
    whole request.
    """
    from ..models.message import AssistantMessage

    messages: list[Any] = []
    for idx, item in enumerate(history):
        role = item.get("role", "")
        content = item.get("content", "")
        if not content:
            logger.debug("history[%d] has no content, skipping", idx)
            continue
        if role == "user":
            messages.append(UserMessage(content=content))
        elif role == "assistant":
            messages.append(AssistantMessage(content=content))
        else:
            logger.warning(
                "history[%d] has unsupported role %r, skipping", idx, role
            )
    return messages


class ChatRequest(BaseModel):
    """Request body for POST /chat and POST /agentic-chat."""

    session_id: str
    user_message: str
    model: str = settings.model  # 從 .env MODEL= 讀取，預設 google/gemma4
    max_turns: int = 10
    system_prompt: str = ""
    history: list[dict[str, Any]] = []
    agent_type: str = "default"
    user_id: str = "default"
    project_id: str = "default"


class AwaySummaryResponse(BaseModel):
    summary: str
    session_id: str


async def _enrich_system_prompt_with_user_memory(
    base_prompt: Optional[str],
    caller: AgenticRagCallerContext,
) -> Optional[str]:
    """Prepend a "user background" block to the agent's system prompt.

    Pulls the caller's facts from CSP via ``fetch_user_facts``
    (route-3 cross-tenant read). Failures are absorbed inside the
    fetch — empty list means "no enrichment", chat proceeds as-is.

    Returns the enriched prompt (or the original when there's
    nothing to add). Caller passes the result through to
    :class:`QueryConfig` unchanged.
    """
    if not caller.can_read_user_memory:
        return base_prompt
    facts = await fetch_user_facts(caller)
    block = format_user_facts_block(facts)
    if not block:
        return base_prompt
    if base_prompt:
        return f"{block}\n\n{base_prompt}"
    return block


def create_app(
    provider: Provider,
    tool_registry: ToolRegistry,
    away_summary_fn: Optional[Any] = None,
    ingestion_service: Optional[Any] = None,
    document_store: Optional[Any] = None,
    embedding_provider: Optional[Any] = None,
    retrieval_provider: Optional[Any] = None,
    db_pool: Optional[Any] = None,
    reranker: Optional[Any] = None,
    rerank_pool_multiplier: int = 3,
    api_key: Optional[str] = None,
    api_dev_mode: bool = False,
    upload_dir: str = "/tmp/anila_uploads",
    csp_service_token: Optional[str] = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        provider:            LLM provider for completions.
        tool_registry:       Registered tools available to agents.
        away_summary_fn:     Optional async function for away summary.
        ingestion_service:   RAG document ingestion service.
        document_store:      Document chunk store (for listing/retrieval).
        embedding_provider:  Embedding provider for search endpoint.
        retrieval_provider:  Vector retrieval provider for search endpoint.
        db_pool:             asyncpg pool for keyword_search / read_document.
        reranker:            Optional cross-encoder reranker applied after
                             vector_search / keyword_search tool calls.
        rerank_pool_multiplier: When reranker is set, fetch
                             ``top_k * multiplier`` candidates first.
        api_key:             Bearer token for API auth (None = disabled).
        api_dev_mode:        Disable auth when True.
        upload_dir:          Directory for uploaded files.
        csp_service_token:   Expected ``X-CSP-Service-Token`` value when this
                             agent runs behind myCSPPlatform. When None/empty
                             the CSP middleware runs in pass-through dev mode.
    """
    app = FastAPI(
        title="AgenticRAG",
        description="Agent Runtime — query loop, tools, memory, compact, RAG",
        version="0.2.0",
    )

    # CSP service-to-service auth (platform integration).
    # Installed FIRST so it runs before the per-agent API-key check below.
    # Loader prefers anila-core's implementation and falls back to the
    # in-package copy when running standalone.
    install_csp_middleware(app, csp_service_token)

    # Per-agent Bearer token auth (for direct clients / OpenWebUI etc).
    app.add_middleware(ApiKeyMiddleware, api_key=api_key, dev_mode=api_dev_mode)

    # Register RAG routers
    app.include_router(documents_router)
    app.include_router(search_router)

    # Inject RAG dependencies
    if ingestion_service is not None and document_store is not None:
        set_ingestion_service(ingestion_service, document_store, upload_dir)
    if embedding_provider is not None and retrieval_provider is not None:
        set_search_providers(embedding_provider, retrieval_provider)

    @app.post("/chat")
    async def chat(
        request: ChatRequest,
        caller: AgenticRagCallerContext = Depends(extract_caller_context),
    ) -> StreamingResponse:
        """Start a query loop and stream SSE events back to the client.

        ``caller`` is populated from CSP-forwarded headers
        (X-ANILA-User-Id / X-CSP-Service-Token / etc.) when this
        AgenticRAG instance is fronted by the CSP proxy. We use it
        to enrich the system prompt with the user's long-term facts
        (route-3 cross-tenant read). When the caller lacks
        credentials (dev curl, no proxy) the prompt passes through
        unchanged.
        """
        enriched_prompt = await _enrich_system_prompt_with_user_memory(
            request.system_prompt, caller
        )
        config = QueryConfig(
            max_turns=request.max_turns,
            model=request.model,
            system_prompt=enriched_prompt,
        )
        engine = QueryEngine(provider, tool_registry, config)

        # Build initial messages: parse history then append new user message
        from ..models.message import UserMessage as UM
        messages = _parse_history(request.history) + [UM(content=request.user_message)]

        async def event_generator() -> AsyncIterator[str]:
            turn_tokens_total = Usage()
            engine_failed = False
            engine_error_msg = ""

            async def on_delta(delta: Any) -> None:
                nonlocal turn_tokens_total
                if delta.type == "text" and delta.text:
                    event = ServerEvent(
                        type=EventType.MESSAGE_DELTA,
                        session_id=request.session_id,
                        payload=MessageDeltaPayload(text=delta.text).model_dump(),
                    )
                    # Note: can't yield from nested async fn, so we put it in a queue
                    await _queue.put(event.to_sse())
                elif delta.type == "tool_call" and delta.tool_call:
                    event = ServerEvent(
                        type=EventType.TOOL_CALL_STARTED,
                        session_id=request.session_id,
                        payload=ToolCallStartedPayload(
                            tool_call_id=delta.tool_call.id,
                            tool_name=delta.tool_call.name,
                        ).model_dump(),
                    )
                    await _queue.put(event.to_sse())
                elif delta.type == "stop" and delta.usage:
                    turn_tokens_total = turn_tokens_total.add(delta.usage)

            _queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

            async def run_engine() -> None:
                nonlocal engine_failed, engine_error_msg
                try:
                    await engine.run(messages, on_stream_delta=on_delta)
                except Exception as exc:
                    engine_failed = True
                    engine_error_msg = str(exc)
                    logger.error("Engine error: %s", exc)
                    error_event = ServerEvent(
                        type=EventType.ERROR,
                        session_id=request.session_id,
                        payload=ErrorPayload(message=engine_error_msg, code="engine_error").model_dump(),
                    )
                    await _queue.put(error_event.to_sse())
                finally:
                    await _queue.put(None)  # sentinel

            asyncio.create_task(run_engine())

            while True:
                item = await _queue.get()
                if item is None:
                    break
                yield item

            if not engine_failed:
                # Final usage event only on success
                usage_event = ServerEvent(
                    type=EventType.USAGE_UPDATE,
                    session_id=request.session_id,
                    payload=UsageUpdatePayload(
                        input_tokens=turn_tokens_total.input_tokens,
                        output_tokens=turn_tokens_total.output_tokens,
                    ).model_dump(),
                )
                yield usage_event.to_sse()

            terminal_event = ServerEvent(
                type=EventType.STREAM_DONE,
                session_id=request.session_id,
                payload={"status": "error" if engine_failed else "completed"},
            )
            yield terminal_event.to_sse()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/agentic-chat")
    async def agentic_chat(
        request: ChatRequest,
        caller: AgenticRagCallerContext = Depends(extract_caller_context),
    ) -> StreamingResponse:
        """Tool-driven AgenticRAG endpoint, framework-runtime path.

        Backed by ``agentic_rag.runtime.framework`` (Action / Agent / Runner) via
        ``agentic_rag.runtime``. Wraps the host-registered AgenticRAG
        ``ToolDefinition`` objects into framework ``Action``s; the same
        tools the legacy QueryEngine path (``/chat``) sees are exposed
        to this runner.

        Wire format matches ``/chat`` (same ``ServerEvent`` envelope and
        SSE event types) so frontend code is endpoint-agnostic. The
        framework Runner is unary in v0.1, so MESSAGE_DELTA fires once
        per turn rather than per-token; token-level streaming returns in
        Sprint 2 alongside the Middleware framework.

        ``request.system_prompt`` is required (422 on missing) — there
        is no built-in default RAG prompt; the policy belongs to the
        deployment.
        """
        from agentic_rag.runtime import (
            FrameworkProviderAdapter,
            build_rag_agent,
            run_agentic_chat_sse,
        )
        from agentic_rag.runtime.bridge.rag_actions import wrap_tool_definition
        from ..models.message import UserMessage as UM

        if not request.system_prompt:
            raise HTTPException(
                status_code=422,
                detail=(
                    "system_prompt is required. The default RAG prompt was "
                    "removed in v0.5.0; pass an explicit one suited to your "
                    "agent."
                ),
            )

        # Route-3 cross-tenant read: prepend user-memory block to the
        # caller-supplied system prompt when CSP forwarded credentials.
        # Falls through unchanged for dev curl with no proxy in front.
        enriched_prompt = await _enrich_system_prompt_with_user_memory(
            request.system_prompt, caller
        )

        # Lift host-registered AgenticRAG ToolDefinitions into framework
        # Actions so the same tool surface reaches the new runner.
        extra_actions = [
            wrap_tool_definition(tool_registry.get(name))
            for name in tool_registry.list_tools()
        ]

        adapter = FrameworkProviderAdapter(provider)
        agent = build_rag_agent(
            name=request.agent_type or "rag-agent",
            instructions=enriched_prompt,
            provider=adapter,
            model=request.model,
            extra_actions=extra_actions,
            max_turns=request.max_turns,
        )

        history_msgs = _parse_history(request.history)
        seed_user = UM(content=request.user_message)
        # Convert AgenticRAG history → framework Message list. The
        # adapter's converters are the inverse direction (framework →
        # AgenticRAG); for the seed we just pass plain text via the
        # framework's convenience constructors.
        from agentic_rag.runtime.framework.items import Message as FwMessage

        seed: list[FwMessage] = []
        for m in history_msgs:
            text = m.get_text() if hasattr(m, "get_text") else ""
            if not text:
                continue
            if m.role == "user":
                seed.append(FwMessage.user(text))
            elif m.role == "assistant":
                seed.append(FwMessage.assistant(text))
        seed.append(FwMessage.user(seed_user.get_text()))

        return StreamingResponse(
            run_agentic_chat_sse(agent, seed, session_id=request.session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/sessions/{session_id}/away_summary")
    async def away_summary(session_id: str) -> AwaySummaryResponse:
        """Generate a 1-3 sentence recap of what happened while user was away."""
        if away_summary_fn is None:
            raise HTTPException(status_code=501, detail="Away summary not configured")

        summary_text = await away_summary_fn(session_id)
        return AwaySummaryResponse(summary=summary_text, session_id=session_id)

    @app.post("/sessions/{session_id}/compact")
    async def compact_session(session_id: str) -> dict[str, Any]:
        """Trigger manual compact for a session.

        Not yet implemented — returns 501 until a compact service is wired.
        """
        raise HTTPException(status_code=501, detail="Compact service not yet implemented")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app

"""FastAPI server — thin shell over the ANILA Core engine.

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

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..context.agent_context import AgentContext, set_current_context
from ..engine.approvals import RunPaused
from ..engine.query_engine import QueryConfig, QueryEngine
from ..memory.session import Session
from ..memory.sqlite_session import SqliteSession
from ..models.message import Usage, UserMessage
from ..providers.base import Provider
from ..router.tool_router import ToolRegistry
from .middleware.auth import ApiKeyMiddleware
from .events import (
    ErrorPayload,
    EventType,
    InterruptRequestedPayload,
    MessageDeltaPayload,
    ResumedPayload,
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


class AnswerRequest(BaseModel):
    """Sprint 9 PR 3 — body for ``POST /sessions/{id}/answer``.

    ``answer`` shape depends on the interrupt kind; see
    :func:`anila_core.engine.approvals._render_answer` for the
    structured-vs-string split. Examples:

    - ``ask_user``: ``{"selected": ["blue"], "other_text": "hot pink"}``
    - ``plan``: ``{"approved": true, "comment": "lgtm"}``
    - any: ``"raw user text"`` (string fallback)
    """

    interrupt_id: str
    answer: Any
    # Resume runs reuse the same QueryConfig knobs as /chat. Most callers
    # pass nothing here (defaults are fine).
    model: str = settings.model
    max_turns: int = 10
    system_prompt: str = ""


def create_app(
    provider: Provider,
    tool_registry: ToolRegistry,
    away_summary_fn: Optional[Any] = None,
    api_key: Optional[str] = None,
    api_dev_mode: bool = False,
    session_db_path: Optional[str] = None,
    session_factory: Optional[Any] = None,
) -> FastAPI:
    """Create and return the FastAPI application.

    Sprint 1 boundary cleanup (anila-core-boundary.md §2.3) removed the
    RAG kwargs (ingestion_service / document_store / embedding_provider /
    retrieval_provider / db_pool / upload_dir) and the corresponding
    routers. The runtime is now a pure agent loop — hosts that need
    RAG endpoints fork the AgenticRAG template, which carries its own
    /upload, /ingest, /status, /search routers.

    Args:
        provider:            LLM provider for completions.
        tool_registry:       Pre-configured registry of tools the agent
                             can call. anila-core does not register any
                             tools by default; callers wire whatever
                             they need.
        away_summary_fn:     Optional async function for away summary.
        api_key:             Bearer token for ApiKeyMiddleware (None = no
                             auth in production; use api_dev_mode for local).
        api_dev_mode:        Disable auth when True.
        session_db_path:     Override SQLite path for the default Session
                             adapter. Defaults to ``settings.session_db_path``.
                             Ignored when ``session_factory`` is provided.
        session_factory:     Optional ``(session_id) -> Session`` factory
                             for tests / Postgres / Redis adapters. When
                             None, a SqliteSession on ``session_db_path``
                             is used.
    """
    app = FastAPI(
        title="ANILA Core",
        description="Agent Runtime — query loop, tools, memory, compact",
        version="0.5.0",
    )

    # Auth middleware. The ``api_key`` kwarg is the legacy name; the
    # underlying ``CspServiceTokenMiddleware`` was renamed but kept
    # back-compat aliased. Pass via ``service_token=`` to match the
    # current signature.
    app.add_middleware(
        ApiKeyMiddleware, service_token=api_key, dev_mode=api_dev_mode
    )

    resolved_db_path = session_db_path or settings.session_db_path

    def _make_session(session_id: str) -> Session:
        if session_factory is not None:
            return session_factory(session_id)  # type: ignore[no-any-return]
        return SqliteSession(resolved_db_path, session_id)

    async def _stream_engine_run(
        *,
        session_id: str,
        engine: QueryEngine,
        coro_factory: Any,
    ) -> AsyncIterator[str]:
        """Run an engine coroutine and stream SSE events.

        ``coro_factory(on_delta) -> Awaitable`` is what's awaited; this
        decoupling lets both ``run`` and ``resume_from_interrupt`` share
        the same SSE plumbing without duplicating it.

        Sprint 9 PR 4: an :class:`AgentContext` is bound for the duration
        of the run so tools can:

        - read / write ``ctx.todos`` (TodoWrite);
        - emit SSE events via ``ctx.event_emitter`` without coupling to
          this transport.
        """
        turn_tokens_total = Usage()
        engine_failed = False
        engine_error_msg = ""
        paused: Optional[RunPaused] = None
        _queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

        async def emit_event(event_name: str, payload: dict[str, Any]) -> None:
            """Tool-facing emitter. Maps event_name → typed envelope."""
            try:
                event_type = EventType(event_name)
            except ValueError:
                # Unknown event name — fall through with a string type so
                # the UI can still display something rather than us
                # silently dropping.
                logger.warning(
                    "Unknown event_name from tool: %r", event_name
                )
                event_type = EventType.MESSAGE_DELTA  # least-bad fallback
            await _queue.put(
                ServerEvent(
                    type=event_type,
                    session_id=session_id,
                    payload=payload,
                ).to_sse()
            )

        # Bind AgentContext for this run so tools (todo_write, plan_mode,
        # etc.) can pick it up via get_current_context(). The contextvar
        # propagates into the asyncio.create_task call below.
        ctx = AgentContext(
            session_id=session_id,
            event_emitter=emit_event,
        )
        set_current_context(ctx)

        async def on_delta(delta: Any) -> None:
            nonlocal turn_tokens_total
            if delta.type == "text" and delta.text:
                event = ServerEvent(
                    type=EventType.MESSAGE_DELTA,
                    session_id=session_id,
                    payload=MessageDeltaPayload(text=delta.text).model_dump(),
                )
                await _queue.put(event.to_sse())
            elif delta.type == "tool_call" and delta.tool_call:
                event = ServerEvent(
                    type=EventType.TOOL_CALL_STARTED,
                    session_id=session_id,
                    payload=ToolCallStartedPayload(
                        tool_call_id=delta.tool_call.id,
                        tool_name=delta.tool_call.name,
                    ).model_dump(),
                )
                await _queue.put(event.to_sse())
            elif delta.type == "stop" and delta.usage:
                turn_tokens_total = turn_tokens_total.add(delta.usage)

        async def run_engine() -> None:
            nonlocal engine_failed, engine_error_msg, paused
            try:
                await coro_factory(on_delta)
            except RunPaused as p:
                paused = p
                # Look up the persisted interrupt's full payload so the
                # UI can render directly from the SSE event without an
                # extra GET roundtrip.
                interrupt_payload: dict[str, Any] = {}
                if engine._session is not None:  # noqa: SLF001
                    pending = await engine._session.pending_interrupts()  # noqa: SLF001
                    for rec in pending:
                        if rec.id == p.interrupt_id:
                            interrupt_payload = rec.payload.get("data", {})
                            break
                event = ServerEvent(
                    type=EventType.INTERRUPT_REQUESTED,
                    session_id=session_id,
                    payload=InterruptRequestedPayload(
                        interrupt_id=p.interrupt_id,
                        kind=p.kind,
                        payload=interrupt_payload,
                    ).model_dump(),
                )
                await _queue.put(event.to_sse())
            except Exception as exc:
                engine_failed = True
                engine_error_msg = str(exc)
                logger.error("Engine error: %s", exc)
                error_event = ServerEvent(
                    type=EventType.ERROR,
                    session_id=session_id,
                    payload=ErrorPayload(
                        message=engine_error_msg, code="engine_error"
                    ).model_dump(),
                )
                await _queue.put(error_event.to_sse())
            finally:
                await _queue.put(None)

        asyncio.create_task(run_engine())

        while True:
            item = await _queue.get()
            if item is None:
                break
            yield item

        if not engine_failed:
            usage_event = ServerEvent(
                type=EventType.USAGE_UPDATE,
                session_id=session_id,
                payload=UsageUpdatePayload(
                    input_tokens=turn_tokens_total.input_tokens,
                    output_tokens=turn_tokens_total.output_tokens,
                ).model_dump(),
            )
            yield usage_event.to_sse()

        if paused is not None:
            terminal_status = "paused"
        elif engine_failed:
            terminal_status = "error"
        else:
            terminal_status = "completed"
        yield ServerEvent(
            type=EventType.STREAM_DONE,
            session_id=session_id,
            payload={"status": terminal_status},
        ).to_sse()

    @app.post("/chat")
    async def chat(request: ChatRequest) -> StreamingResponse:
        """Start a query loop and stream SSE events back to the client.

        Sprint 9: an automatic SqliteSession (or ``session_factory``) is
        attached so tools that return :class:`InterruptItem` can pause the
        loop. The persisted history lets ``POST /v1/sessions/{id}/answer``
        resume in a fresh process / engine instance.
        """
        config = QueryConfig(
            max_turns=request.max_turns,
            model=request.model,
            system_prompt=request.system_prompt,
        )
        sess = _make_session(request.session_id)
        engine = QueryEngine(provider, tool_registry, config, session=sess)

        from ..models.message import UserMessage as UM
        messages = _parse_history(request.history) + [
            UM(content=request.user_message)
        ]

        return StreamingResponse(
            _stream_engine_run(
                session_id=request.session_id,
                engine=engine,
                coro_factory=lambda on_delta: engine.run(
                    messages, on_stream_delta=on_delta
                ),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/agentic-chat")
    async def agentic_chat(request: ChatRequest) -> StreamingResponse:
        """Tool-driven chat endpoint — runs the agent loop with whatever
        tools the host registered into the global ToolRegistry.

        Sprint 1 boundary cleanup (anila-core-boundary.md Grey Zone B)
        moved RAG tool wiring out of core: this endpoint no longer knows
        about vector_search / keyword_search / read_document. Callers
        building RAG agents (e.g. AgenticRAG template) register their
        own tools at app-factory time, and the request body must supply
        a system prompt — there's no longer a RAG-specific default.
        """
        if not request.system_prompt:
            raise HTTPException(
                status_code=422,
                detail=(
                    "system_prompt is required — anila-core no longer ships "
                    "a default RAG prompt. Use AgenticRAG template or pass "
                    "your own."
                ),
            )

        config = QueryConfig(
            max_turns=request.max_turns,
            model=request.model,
            system_prompt=request.system_prompt,
        )
        sess = _make_session(request.session_id)
        engine = QueryEngine(provider, tool_registry, config, session=sess)

        from ..models.message import UserMessage as UM
        messages = _parse_history(request.history) + [
            UM(content=request.user_message)
        ]

        return StreamingResponse(
            _stream_engine_run(
                session_id=request.session_id,
                engine=engine,
                coro_factory=lambda on_delta: engine.run(
                    messages, on_stream_delta=on_delta
                ),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # -------------------------------------------------------------------
    # Sprint 9: pause / resume — interrupt answer + session state
    # -------------------------------------------------------------------

    @app.post("/sessions/{session_id}/answer")
    async def submit_answer(
        session_id: str, request: "AnswerRequest"
    ) -> StreamingResponse:
        """Resume a paused run with the user's answer.

        Streams the resumed turn back as SSE — same envelope as ``/chat``,
        starting with a ``RESUMED`` event so the UI can clear its
        "paused" affordance.
        """
        sess = _make_session(session_id)
        config = QueryConfig(
            max_turns=request.max_turns,
            model=request.model,
            system_prompt=request.system_prompt,
        )
        engine = QueryEngine(provider, tool_registry, config, session=sess)

        # Emit RESUMED first so the UI sees lifecycle context, then run.
        async def coro_factory(on_delta: Any) -> Any:
            # No SSE plumbing here — _stream_engine_run handles it. We
            # still want a RESUMED event before any model output. Fold it
            # into the on_delta queue by piggy-backing on the first
            # provider call. Simpler: prepend it via a wrapper.
            return await engine.resume_from_interrupt(
                request.interrupt_id,
                request.answer,
                on_stream_delta=on_delta,
            )

        async def stream_with_resume_marker() -> AsyncIterator[str]:
            # Emit RESUMED event before any model deltas.
            yield ServerEvent(
                type=EventType.RESUMED,
                session_id=session_id,
                payload=ResumedPayload(
                    interrupt_id=request.interrupt_id
                ).model_dump(),
            ).to_sse()
            async for chunk in _stream_engine_run(
                session_id=session_id,
                engine=engine,
                coro_factory=coro_factory,
            ):
                yield chunk

        return StreamingResponse(
            stream_with_resume_marker(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/sessions/{session_id}/state")
    async def session_state(session_id: str) -> dict[str, Any]:
        """Snapshot of conversation history + pending interrupts.

        Use this when the UI needs to rehydrate after a reload, or wants
        to show pending interrupts without subscribing to a stream.
        """
        sess = _make_session(session_id)
        items = await sess.get_items()
        pending = await sess.pending_interrupts()
        return {
            "session_id": session_id,
            "messages": [m.model_dump(mode="json") for m in items],
            "pending_interrupts": [
                {
                    "id": p.id,
                    "kind": p.kind,
                    "payload": p.payload.get("data", {}),
                    "created_at": p.created_at.isoformat(),
                }
                for p in pending
            ],
        }

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

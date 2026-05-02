# anila-core CHANGELOG

All notable changes to this package. anila-core is **not yet 1.0** — internal
breaking changes are acceptable but always documented here. SemVer kicks in
once we cut v1.0 (no concrete date).

## v0.10.0 (2026-05-02) — Sprint 11 · Governance & observability

Sprint 11 layers governance + observability on top of Sprints 9-10.
QueryEngine now exposes synchronous lifecycle hooks; an OTel-style
hierarchical tracing module ships in-tree; tools gain a per-call
permission policy with an interactive ASK mode; and the Router's
multi-turn loop supports streaming.

### Added — primitives

* **Lifecycle hooks** (`anila_core.engine.lifecycle`):
  - `RunHooks` base class with no-op defaults — subclass and override
    only what you need. Pass instance to `QueryEngine(hooks=…)`.
  - Hook points: `on_run_start` / `on_run_end` / `on_agent_start` /
    `on_agent_end` / `on_tool_start` / `on_tool_end` / `on_run_paused`
    / `on_run_resumed` / `on_handoff`.
  - All hooks are async; exceptions are caught + logged via
    `_safe_call`, never abort the run loop.
  - Distinct from QueryEngine's existing fire-and-forget
    `post_turn_hooks` (lifecycle hooks are synchronous, see real-time
    state, fire at multiple points).

* **Hierarchical tracing** (`anila_core.tracing`):
  - `Span` — frozen-on-close timed unit with parent / children, status,
    attributes, events.
  - `SpanKind` — `RUN` / `AGENT` / `LLM` / `TOOL` / `HANDOFF` /
    `INTERRUPT` / `INTERNAL`.
  - `SpanStatus` — `UNSET` / `OK` / `ERROR`.
  - `Tracer` — owns the current-span stack via contextvars; sync
    `tracer.span(...)` and async `tracer.async_span(...)` context
    managers; exceptions inside the block automatically mark the span
    `ERROR` with the exception text.
  - `SpanProcessor` Protocol + `InMemoryProcessor` reference impl
    (`.spans` for collected list, `.to_tree()` for nested
    parent-rooted dict tree).
  - `TracingHooks(Tracer)` — `RunHooks` adapter that emits spans
    automatically for every run / agent / tool / handoff / pause /
    resume event. `QueryEngine(hooks=TracingHooks(tracer))` is the
    one-line wire-up.

* **Per-tool permission policy** (`anila_core.models.tool.ToolPermission`):
  - `ALLOW` (default) / `DENY` / `ASK`.
  - DENY rejects with an error result.
  - ASK pauses the run via an `InterruptItem(kind="tool_approval")` —
    Sprint 9's pause-resume primitive does the heavy lifting.
  - On approve, the resume helper re-executes the original tool with
    `bypass_gates=True` (also bypasses Sprint 9's plan-mode gate); on
    deny, emits a synthetic `is_error=True` ToolResult so the model
    can change strategy.
  - New `ToolRegistry.execute(..., bypass_gates: bool = False)` kwarg
    + `engine.approvals.resume_tool_approval(session, registry,
    interrupt_id, *, approved, comment)` helper.

### Added — HTTP / Router

* **Streaming multi-turn Router** — Sprint 10's `anila_multi_turn`
  body field now works with `stream: true`. Implementation: trace
  events fire at each iteration step (LLM call, dispatch, agent
  reply); content chunks are emitted only for the *final* synthesised
  answer, soft-chunked for natural-feeling delivery. Single-shot
  streaming (`anila_multi_turn` omitted or `1`) keeps the existing
  real-time token-by-token path with all DISPATCH parsing intact.

### Modified

* `engine.query_engine.QueryEngine.__init__` accepts `hooks:
  RunHooks | None = None`. Stage 4 fires `on_tool_start` /
  `on_tool_end` per call (matched by `tool_call_id`); Stage 1 fires
  `on_run_start` + `on_agent_start`; close-out fires `on_agent_end` +
  `on_run_end`. `_pause_on_interrupt` fires `on_run_paused`,
  `_handoff` fires `on_handoff`, `resume_from_interrupt` fires
  `on_run_resumed`.
* `router.tool_router.ToolRegistry.execute` gained the
  `bypass_gates: bool = False` kw-only arg. Plan-mode gate (Sprint 9)
  and permission gate both honour it.
* `engine.query_engine.QueryEngine.resume_from_interrupt` peeks at
  the pending interrupt; `tool_approval` kind is routed to the new
  `resume_tool_approval` helper instead of the generic `resume_with`.
* `models.tool.ToolDefinition` gained `permission: ToolPermission =
  ToolPermission.ALLOW`.
* `models/__init__.py` exports `ToolPermission`. `engine/__init__.py`
  exports `RunHooks`, `RunHooksProtocol`, `resume_tool_approval`.
* `api/router_server.py` `chat_completions` routes streaming requests
  with `anila_multi_turn > 1` to the new
  `_router_streaming_multi_turn` helper. Helper emits soft-chunked
  final content via `_emit_soft_chunks`.

### Tests

44 new tests across `test_lifecycle_hooks` (11), `test_tracing` (16),
`test_tool_permission` (13), `test_router_streaming_multi_turn` (4).
Full suite **432 passed** (up from 388). Lint clean, mypy net-zero
added, 5 pre-existing CJK / unrelated failures unchanged.

### Migration

* Existing callers see no behavioural change: `hooks` defaults to
  None, tool `permission` defaults to ALLOW, streaming multi-turn is
  opt-in through the same `anila_multi_turn` field that already
  defaults to `1`.
* Forks that registered tools with destructive side effects can now
  set `permission=ToolPermission.ASK` to require user approval per
  call. The HTTP layer (api/server.py from Sprint 9) already returns
  `interrupt_requested` SSE for this kind; web frontends just need to
  handle the `tool_approval` kind in their renderer.
* New tools that want hierarchical tracing should pass
  `hooks=TracingHooks(tracer)` to `QueryEngine`. The flat
  `anila_meta.trace` stays — spans are an additional view, not a
  replacement.

### What's next (Sprint 12 plan)

Tier D + Tier E from the Sprint 9 design: per-tool input/output
guardrails (openai-agents `tool_guardrails.py`), agent persona /
output style configuration (claude-code `outputStyles/`), file /
shell tool suite (Bash / FileRead / FileWrite / Glob / Grep), and
optional sandbox runtime (openai-agents `sandbox/`). Streaming
multi-turn may also gain true per-turn streaming once the synthesis
UX is well-understood.

## v0.9.0 (2026-05-02) — Sprint 10 · Multi-agent control flow

Built on Sprint 9's Session + Approvals foundation. Sprint 10 lets the
Router and individual agents move beyond the single-shot dispatch
shape: agents can hand off control to a specialist, the Router can
chain multiple dispatches per user turn, and dispatch is now stateful
(session and filtered context cross the boundary).

### Added — primitives

* **Handoff primitive** (`anila_core.engine.handoff`,
  `anila_core.models.handoff`) — control transfer to another agent.
  - `HandoffRequest` — Pydantic model returned by a tool to signal
    handoff. Carries `target_agent_id`, `message`, pre-filtered
    `context_messages`, optional `reason` + `metadata`.
  - `RunHandoff` — exception QueryEngine raises after persisting the
    source agent's history; the Router catches it and dispatches the
    target.
  - `HandoffFilter` Protocol + built-ins:
    - `NoFilter` — pass full conversation through.
    - `LastNFilter(n)` — keep last N visible turns; tool_result
      user-messages are skipped automatically.
    - `SummaryFilter(summary)` — replace history with a single
      assistant note (LLM-driven variant deferred).
* **Agent-as-tool wrapper** (`anila_core.tools.agent_as_tool`) —
  `make_agent_tool(manifest, …)` turns a `RemoteAgentManifest` into a
  callable `ToolDefinition`. Lets one agent *consult* a specialist as a
  normal tool call (sync sub-call, distinct from `Handoff`'s control
  transfer). Tool body forwards via `dispatch_to_agent_response` and
  pulls `session_id` from the bound `AgentContext` by default.

### Added — HTTP

* **Session-aware Router** (`api/router_server.py`):
  - `create_router_app(session_db_path=…, session_factory=…)` — same
    Session integration shape as `api/server.py` (Sprint 9).
  - `POST /v1/chat/completions` accepts optional `session_id` /
    `anila_session_id` (auto-generated when absent).
  - `X-Anila-Session-Id` response header on every reply so the caller
    can pin subsequent calls.
  - User turn persisted to the Router's Session for cross-turn
    orchestration.
  - `GET /v1/sessions/{id}/state` — Router-side snapshot
    (conversation history + pending interrupts).
* **Multi-turn Router orchestration** — opt-in via
  `anila_multi_turn: <int>` request field (default `1` = single-shot,
  preserves existing behaviour). When `> 1`, after the first dispatch
  the Router LLM is re-invoked with the agent's reply and may either
  produce a final synthesised answer or `DISPATCH:<other>:<query>` for
  another iteration. Trace records each round; reasoning fold
  accumulates per-iteration analysis. Streaming path keeps single-shot
  for now (multi-turn streaming deferred).

### Added — dispatch_tool extensions

`dispatch_to_agent` / `dispatch_to_agent_response` (`tools/dispatch_tool`)
gained kw-only fields:

- `context_messages: list[dict] | None` — pre-filtered prior turns
  inserted before the new user query.
- `session_id: str | None` — embedded in request body as
  `anila_session_id` extension field; CSP forwards verbatim, agents
  that recognise it attach the same Session adapter.
- `handoff_meta: dict | None` — embedded as `anila_handoff` extension
  field for agents that want to render "handed off from X" UI.

New convenience: `dispatch_for_handoff(request, …)` unpacks a
`HandoffRequest` into the right parameters — what the Router calls
when it catches `RunHandoff`.

### Modified

* `models.message.ToolResult` gained `handoff: HandoffRequest | None`
  (mirrors the Sprint 9 `interrupt` field).
* `router.tool_router.ToolRegistry.execute` detects `HandoffRequest`
  returns alongside `InterruptItem` and tags the `ToolResult`.
* `engine.query_engine.QueryEngine` Stage 4 now also raises
  `RunHandoff` when any tool returned a handoff (one-handoff-per-turn
  contract; sibling tools execute normally).
* Router's internal `_dispatch_safe` and `_stream_agent_sse` both
  forward `session_id` to the dispatched agent.
* `models/__init__.py` exports `HandoffRequest`, `InterruptItem`,
  `InterruptKind`. `engine/__init__.py` exports `RunHandoff` +
  filters. `tools/__init__.py` exports `make_agent_tool`.

### Tests

51 new tests across `test_handoff` (19), `test_dispatch_stateful` (8),
`test_router_session` (6), `test_router_multi_turn` (4),
`test_agent_as_tool` (14). Full suite **388 passed** (up from 337).
The 5 pre-existing CJK / unrelated failures remain unchanged. lint
clean, mypy net-zero added.

### Migration

* Existing callers of `create_router_app()` keep working — `session_id`
  is auto-generated when omitted, new headers / endpoints are
  additive, multi-turn loop is opt-in.
* `dispatch_to_agent*` signatures are backward-compatible (all new
  params are kw-only with defaults).
* Forks that have their own Router/agent server should add
  `anila_session_id` / `anila_handoff` to their request schemas (and
  optionally honour `anila_multi_turn`) to participate in the new
  flow. anila-core's reference Router does this automatically.

### What's next (Sprint 11 plan)

Tier C from the Sprint 9 design: lifecycle hooks (on_agent_start /
on_handoff / on_tool_*), per-tool guardrails, OTel-style hierarchical
tracing, and per-tool permission policy. Streaming multi-turn Router
also lands here.

## v0.8.0 (2026-05-02) — Sprint 9 · Web 對話 protocol

Vendored five primitives from `runtime_logic/` (Claude Code +
openai-agents-python) that turn an anila-core agent into a Claude.ai-
style chat partner. No CLI / TUI surface — every interaction is shaped
to be rendered by a web frontend.

### Added — primitives

* **Session protocol** (`anila_core.memory.session.Session`) — stores
  conversation history + pending interrupts for one chat session.
  Two adapters ship: `MemorySession` (tests / dev) and `SqliteSession`
  (default, single-process; uses `aiosqlite` and writes to
  `settings.session_db_path`). Multi-process / HA deployments can
  drop in their own adapter.
* **Approvals primitive** (`anila_core.engine.approvals`) — pause /
  resume the run loop on a tool's request. A tool implementation
  returns `InterruptItem`; QueryEngine persists conversation + the
  interrupt to the active Session and raises `RunPaused`. The new
  `QueryEngine.resume_from_interrupt(interrupt_id, answer)`
  rehydrates from session, stitches the user's answer + sibling
  tool results into one `tool_result` message, and continues the
  loop. Mandates **one interrupt per turn** (Sprint 10 may relax).
* **`ask_user` tool** (`anila_core.tools.ask_user`) — multiple-choice
  question to the user mid-run.
* **`enter_plan_mode` / `exit_plan_mode` tools** (`anila_core.tools.plan_mode`)
  — propose-then-execute. `enter_plan_mode` flips
  `AgentContext.plan_mode = True`; `ToolRegistry` then rejects any
  `DESTRUCTIVE` tool until `exit_plan_mode(plan)` surfaces the plan
  via an `InterruptItem` for user approval.
* **`todo_write` tool** (`anila_core.tools.todo_write`) — agent-managed
  task board. Validates "exactly one in_progress" and writes to
  `AgentContext.todos`; emits `todos_updated` SSE.
* **PromptSuggestion post-turn hook**
  (`anila_core.post_turn.prompt_suggestion`) — small LLM call after a
  successful turn produces 3 follow-up question chips; emits
  `follow_ups` SSE.

### Added — HTTP

`api/server.py` (`create_app`) gained:

* Automatic Session attachment (configurable via
  `session_db_path=` or fully overrideable via
  `session_factory=` for Postgres / Redis adapters / tests).
* `RunPaused` is caught and surfaced as `interrupt_requested` SSE,
  with `stream_done.status = "paused"` rather than `"error"`.
* `POST /sessions/{id}/answer` — resume a paused run with the
  user's answer. Streams the resumed turn back as SSE starting
  with a `resumed` event.
* `GET /sessions/{id}/state` — snapshot of conversation history +
  pending interrupts; for UI rehydration after reload.
* `AgentContext` is bound around each run loop with an
  `event_emitter` so tools can push SSE events without coupling
  to the transport layer.

### Added — SSE event types

In `anila_core.api.events.EventType`:

* `interrupt_requested` (+ `InterruptRequestedPayload`)
* `resumed` (+ `ResumedPayload`)
* `todos_updated` (+ `TodosUpdatedPayload`)
* `follow_ups` (+ `FollowUpsPayload`)

### Added — supporting model

* `anila_core.models.interrupt.InterruptItem` — runtime form of an
  interrupt (lives under `models/` so `ToolResult.interrupt` can
  reference it without an inter-package import cycle).
* `anila_core.models.agent.Todo` (+ `TodoStatus`).

### Modified

* `models.message.ToolResult` gained `interrupt: InterruptItem | None`.
* `router.tool_router.ToolRegistry.execute` now (a) detects
  `InterruptItem` returns and tags the ToolResult, (b) gates
  `DESTRUCTIVE` tools when `AgentContext.plan_mode` is active.
* `engine.query_engine.QueryEngine.__init__` accepts an optional
  `session=` kwarg; Stage 4 raises `RunPaused` when an interrupt
  was returned by any tool in the turn.
* `context.agent_context.AgentContext` gained `plan_mode`, `todos`,
  `event_emitter`. `create_subagent_context` propagates all three.
* `config.settings.session_db_path` (`ANILA_SESSION_DB_PATH` env).
* `api/server.py` `create_app(api_key=...)` now correctly forwards
  to `CspServiceTokenMiddleware(service_token=...)` (latent bug
  exposed by Sprint 9 test coverage; legacy `api_key` kwarg name
  preserved for back-compat).

### Dependencies

* `aiosqlite>=0.20` (base dep — backs the default Session adapter).

### Tests

109 new tests across `test_session_memory`, `test_session_sqlite`,
`test_approvals`, `test_engine_interrupt`, `test_tool_ask_user`,
`test_tool_plan_mode`, `test_tool_todo_write`,
`test_prompt_suggestion`, `test_server_interrupt_flow`. Full suite
337 passed (5 pre-existing unrelated failures unchanged).

### Migration

* Existing callers of `create_app()` get session integration
  automatically (default SQLite under `./.anila/sessions.db`).
  Override with `session_db_path=` or `session_factory=` if you
  need a different store.
* No changes required for callers that don't register `ask_user` /
  `plan_mode` / `todo_write` tools — pause-resume only fires when
  one of those tools is invoked.

### What's next (Sprint 10 plan)

Tier B from the Sprint 9 design: multi-turn router orchestration,
handoff-with-context-filter (openai-agents `extensions/handoff_filters`),
and Session-aware Router so dispatched agents can share context.

## Unreleased — Sprint 8 X boundary correction (doc-only, 2026-05-01)

No code changes. Sprint 8 X audit found that v0.5.0's release notes
claimed `ingestion/` and `storage/adapters/{pg_pool,pgvector_store}.py`
had been removed, but they were not — `ingestion-worker` imports them
in production. The architecture is therefore **two-pillar**:

* **Pillar 1 — Agent runtime**: api / engine / coordinator / registry /
  context / tools / router / providers / memory / compact / models /
  cli / config. In-process; consumed by Router and every agent.
* **Pillar 2 — Shared infrastructure**: security / storage (incl.
  pg_pool, pgvector_store, memory_file_store) / ingestion (errors +
  chunking_plugins). Fleet-level; consumed by Router, agents, AND
  batch workers (`ingestion-worker` today; future PII / scoring /
  refresh workers tomorrow).

`__init__.py` docstring rewritten to surface this split. `__version__`
synced from stale `0.1.0` to `pyproject.toml`'s `0.7.0`. README's
boundary diagram, file tree, and v0.5.0 release-notes block all
corrected to reflect actual scope. No imports moved, no APIs changed.

## v0.7.0 (2026-04-27) — Collection-as-first-class (Sprint 4, Chunks O–T)

### BREAKING

The Sprint 1–3 architecture treated every collection as the property of
exactly one agent (``ingestion_collections.agent_id NOT NULL``). Smoke-
testing on real workflows showed this was over-coupling: ANILA's posture
is "platform = pgvector infrastructure", agent backends just configure
``DB_URL + COLLECTION_ID`` and the platform doesn't care which agent
reads what. v0.7 drops the agent coupling entirely.

#### Schema

| Concept | v0.6 | v0.7 |
|---|---|---|
| Collection ownership | `agent_id` FK to agents | `created_by` FK to users (NOT NULL) |
| Chunk RLS scope | `anila.agent_id` GUC | `anila.collection_id` GUC |
| Chunk `agent_id` column | denormalised | dropped |
| LLM credentials FK | agents | users (table renamed `agent_llm_credentials` → `user_llm_credentials`) |

Migration 0019 (CSP) handles all of the above plus a "csp lifespan
fallback ``Base.metadata.create_all`` re-creates orphan tables" gotcha
discovered during the refactor.

#### SDK

- `AgentScopedPgVectorStore` → **`CollectionScopedPgVectorStore`**.
  Constructor takes `collection_id: int` (positive int guard kept).
  ``_acquire`` sets ``anila.collection_id`` GUC.
- ``index_chunks(document_id, chunks, embeddings)`` — dropped redundant
  ``collection_id`` per-call argument.
- ``similarity_search`` / ``keyword_search`` — dropped optional
  ``collection_id`` per-call arguments. RLS does the scoping.
- ``list_in_collection(limit, offset)`` — Sprint 4 rename of
  ``list_by_collection``; parameter is implicit now.
- ``delete_all()`` — Sprint 4 rename of ``delete_collection``.
- All SQL paths drop ``agent_id`` from SELECT projections.
- ``IngestionChunk`` Pydantic model: ``agent_id`` field removed.

#### Back-compat

- ``AgentScopedPgVectorStore`` aliases ``CollectionScopedPgVectorStore``
  for one transition cycle. Old callers fail at the call site with
  the constructor kwarg name change (``agent_id`` → ``collection_id``)
  rather than an obvious import error — by design, the forcing function
  for them to update.

### Tests

- ``test_collection_scoped_pgvector_store.py`` — 13 constructor-guard
  tests rewritten around ``collection_id``. New test pins the
  back-compat alias invariant.
- ``test_g1_collection_isolation.py`` — Sprint 1 G1 rebase; 5
  collections × 50 chunks × 30 random queries = 750 leakage probes.
- ``test_g2_rls_bypass.py`` — Sprint 1 G2 rebase; FORCE RLS posture
  + collection-scoped GUC bypass attempts (4 paths).
- Old ``test_g1_agent_isolation.py`` and
  ``test_agent_scoped_pgvector_store.py`` deleted.

### Sprint 4 G1/G2/G3 results

| Gate | v0.6 scope | v0.7 scope | Result |
|---|---|---|---|
| G1 random workload, zero leakage | 5 agents | **5 collections** | ✅ 1.96s |
| G2 raw asyncpg without GUC sees 0 rows | `anila.agent_id` | **`anila.collection_id`** | ✅ |
| G3 single SQL entry point | unchanged | unchanged | ✅ |

### Migration

| If you …                                         | Do this                                                                                                |
|---|---|
| Were using ``AgentScopedPgVectorStore``           | Switch to ``CollectionScopedPgVectorStore``; constructor kwarg ``agent_id`` → ``collection_id``.       |
| Had ``RAG_AGENT_ID`` env on AgenticRAG / forks    | Switch to ``RAG_COLLECTION_ID``. The collection it points at must already exist in CSP UI.            |
| Had per-tenant `agent_llm_credentials` rows       | They became ``user_llm_credentials`` rows scoped to ``created_by``. Re-issue if FK chain was broken. |
| Were calling `index_chunks(collection_id=..., document_id=..., ...)` | Drop the redundant `collection_id` kwarg. The store already knows.                                    |

---

## v0.6.0 (2026-04-25) — Ingestion Platform foundation (Sprint 1, Chunks A–G)

### Added

The boundary v0.5.0 left was "anila-core is a pure runtime". v0.6.0 adds
the ingestion-platform SDK layer back on top, this time as a thin,
agent-scoped facade rather than the per-deployment runtime that was
deleted. Sprint 1 ships:

- **`anila_core.ingestion`** — ingestion support layer for the central
  worker service.
  - `errors.IngestionError` taxonomy (5 codes: `E_PARSE_FORMAT_UNSUPPORTED`,
    `E_PARSE_CORRUPT`, `E_EMBED_TIMEOUT`, `E_PG_CONNECT`,
    `E_PG_RLS_VIOLATION`). Each carries `retryable` / `severity`.
    `E_PG_RLS_VIOLATION` is hard-coded as `severity=critical, retryable=False`
    — RLS bypass is a security incident, never auto-recovered.
  - `chunking_plugins` — Protocol + idempotent registry + 3 built-in
    strategies (`hierarchical`, `fixed`, `markdown-aware`). The 3
    remaining strategies from the design doc (`pdf-page`, `cjk-sentence`,
    `semantic`) live in the worker service alongside their heavier deps.

- **`anila_core.storage.adapters`** — agent-scoped pgvector access.
  - `PgPool` returns. Same name as the v0.5.0-deleted class but the new
    one auto-registers `vector` + `halfvec` + `sparsevec` + `jsonb`
    codecs on every connection (the legacy adapter only did `vector`).
  - `AgentScopedPgVectorStore` is the only sanctioned read/write path
    into `document_chunks`. Constructor refuses non-positive int
    `agent_id` (rejects None / str / float / bool / 0 / negative).
    Every method wraps work in `BEGIN ... SET LOCAL anila.agent_id = N
    ... COMMIT` — without the explicit transaction, asyncpg autocommits
    each statement and Layer 2 RLS is silently bypassed.
  - Methods: `index_chunks`, `similarity_search`,
    **`keyword_search`** (FTS via `plainto_tsquery` against
    `content_tsv`), `list_by_document`, `list_by_collection`,
    `delete_document`, `delete_collection`.

- **`anila_core.models.ingestion`** — `IngestionChunk` + `SearchHit`
  Pydantic models for the new schema. The legacy
  `models.storage.DocumentChunk` (TEXT chunk_id, user_id/project_id)
  remains for back-compat but is no longer the canonical chunk type.

### BREAKING (since v0.5.0)

- `pyproject.toml`: `asyncpg>=0.29` and `pgvector>=0.3` are core deps
  again (v0.5.0 demoted them to optional). The central SDK needs them.
- Dependency footprint up by ~15 MB installed; v0.5.0's clean-runtime
  promise is intentionally relaxed because the central SDK lives here now.

### Tests

- 35 unit tests added (errors / chunking_plugins / store constructor
  guards / G3 static gate) — total 209 passing on this branch.
- 6 integration tests under `tests/integration/` (G1 random workload,
  G2 RLS bypass × 4) — runtime ~2s against a live pgvector. Auto-skip
  when no DB is reachable.

### Sprint 1 G1/G2/G3 gates

| Gate | Result |
|---|---|
| G1: 5 agents × random workload, zero cross-agent leakage | ✅ |
| G2: raw asyncpg without GUC sees 0 rows; RLS holds | ✅ |
| G3: actual SQL on `document_chunks` lives in 1 file (the SDK) | ✅ |

### Migration

| If you …                                              | Do this                                                                                                                  |
|---|---|
| Already shipped a fork on v0.5.0                       | Add `RAG_AGENT_ID` env to the deployment; switch to `csp_app` runtime DSN; drop your own `pgvector_store.py` if cloned. |
| Were importing from `anila_core.storage.adapters` v0.5 | Imports still work. `AgentScopedPgVectorStore` and `PgPool` are new. The MemoryFileStore re-export is unchanged.            |
| Were calling old `models.DocumentChunk`               | Still there. New code uses `models.ingestion.IngestionChunk`.                                                            |

---

## v0.5.0 (2026-04-25) — Boundary cleanup (Sprint 1)

### BREAKING

Sprint 1 of the Phase 2 boundary cleanup ([anila-core-boundary spec](../docs/anila-core-boundary.md)) split the RAG-flavour runtime out of core. anila-core is now a strictly chat / agent / memory / dispatch runtime; everything RAG-flavour now lives in [AgenticRAG](../AgenticRAG/) (per-agent template) or, for the future centralised path, the [Ingestion Platform](../docs/ingestion-platform-design.md).

#### Modules removed

| Path | Replacement |
|---|---|
| `anila_core.ingestion.*` | AgenticRAG ships a 2017-line pipeline (`docling_parser` + `parsers` + `chunker` + `ocr` + `tokenize_zh` + `service`). For multi-agent shared ingestion, the Ingestion Platform service supersedes anila-core's local pipeline. |
| `anila_core.storage.adapters.pg_pool` | Use `agentic_rag.storage.adapters.pg_pool_v2` once the Ingestion Platform's v2 pool lands. |
| `anila_core.storage.adapters.pgvector_store` | Use `agentic_rag.storage.adapters.pgvector_store_v2`. |
| `anila_core.storage.adapters.postgres_store` (PgSessionStore / PgMessageStore / PgRetrievalTraceStore + `initialize_schema`) | Same — RAG schema bootstrap belongs in the ingestion service, not the agent runtime. |
| `anila_core.providers.embedding_nvidia.NvidiaEmbeddingProvider` | Embedding now happens inside the ingestion-worker; agents do not embed inline. |
| `anila_core.engine.rag_preprocessor.RagPreprocessor` | Pre-process injection pattern is dead. The new model is **tool-driven retrieval**: the LLM calls `vector_search` / `keyword_search` as registered tools when it decides to. |
| `anila_core.api.documents` (`/upload` `/ingest` `/status` endpoints) | CSP `/api/ingestion/*` endpoints (Ingestion Platform). |
| `anila_core.api.search` (`POST /search`) | Same. |
| `anila_core.tools.{create_vector_search_tool, create_keyword_search_tool, create_read_document_tool}` | These factories now live in `agentic_rag.tools` only. anila-core does not register any tools by default — callers wire whatever they need. |
| `anila_core.tools.prompts.AGENTIC_RAG_SYSTEM_PROMPT` | AgenticRAG carries its own system prompt; `/agentic-chat` no longer ships a RAG default. |

#### `storage.ports` Protocols — UNCHANGED

`anila_core.storage.ports.{DocumentStore, RetrievalProvider, ...}` Protocol definitions stay in core. They are the interface contract any future backend (qdrant, milvus, chroma, pgvector v2) implements.

#### `storage.adapters.MemoryFileStore` — KEPT

Filesystem `MemoryStore` implementation used by `anila_core.memory.*`. Not RAG-specific; this is platform memory infra. A `PostgresMemoryStore` impl will land in Phase 3+ for production deploys.

#### `app_factory.build_app()` — slimmed (143 → 60 lines)

The factory now does:

```python
llm_provider  = OpenAICompatProvider(...)
tool_registry = ToolRegistry()         # caller registers tools
return create_app(provider=llm_provider, tool_registry=tool_registry, ...)
```

The previous lifespan (PG pool init, pgvector schema bootstrap), `LazyStoreProxy` plumbing, `IngestionService` composition, `NvidiaEmbeddingProvider` wiring, chunker construction — all gone. Forks like AgenticRAG carry their own `app_factory.py` with the full ingestion stack.

#### `create_app()` signature — 6 RAG kwargs removed

```diff
 def create_app(
     provider: Provider,
     tool_registry: ToolRegistry,
     away_summary_fn: Optional[Any] = None,
-    ingestion_service: Optional[Any] = None,
-    document_store: Optional[Any] = None,
-    embedding_provider: Optional[Any] = None,
-    retrieval_provider: Optional[Any] = None,
-    db_pool: Optional[Any] = None,
     api_key: Optional[str] = None,
     api_dev_mode: bool = False,
-    upload_dir: str = "/tmp/anila_uploads",
 ) -> FastAPI:
```

#### `/agentic-chat` endpoint — RAG wiring removed (Grey Zone B resolution)

The endpoint stays. Inside, it no longer imports the RAG factories or wires per-request RAG tools. It just runs the agent loop with whatever ToolRegistry the host configured at app-factory time. `request.system_prompt` is now **required** (422 on missing) — anila-core no longer ships a RAG default.

#### `query_engine.QueryEngine.__init__` — `rag_preprocessor` arg removed

The optional `rag_preprocessor: Optional[RagPreprocessor] = None` constructor arg is gone, along with the `_pre_process` injection path. `_pre_process` stays as a passthrough hook for future preprocessing concerns (token gates, redaction).

#### `config.Settings` — 11 fields removed

```diff
-embedding_url, embedding_api_key, embedding_model,
-embedding_dimension, embedding_verify_ssl,
-database_url, pg_pool_min, pg_pool_max, pg_ssl,
-chunk_size, chunk_overlap,
-rag_top_k, rag_min_score,
-upload_dir
```

Settings now has 8 fields covering LLM provider, CSP plumbing, and auth.

### Migration guide

| If you …                                                | Do this                                       |
|---|---|
| Were building a RAG agent on top of anila-core directly | Fork the [AgenticRAG](../AgenticRAG/) template; it carries the full ingestion + tool wiring. |
| Were using `pip install anila-core[rag]`                | The `[rag]` extras no longer exist (the dependencies they pulled — pgvector / docling / pypdf — moved to AgenticRAG's `pyproject.toml`). |
| Were importing `anila_core.tools.create_vector_search_tool` etc. | Switch to `agentic_rag.tools.create_vector_search_tool`. AgenticRAG's version is also slightly more recent (383 vs 274 lines). |
| Hit `/agentic-chat` without a `system_prompt`           | Now returns 422. Pass your prompt explicitly. |
| Were calling `create_app(embedding_provider=…)` etc.     | Drop the RAG kwargs. Pre-register your tools in the `tool_registry` you pass in. |

### Verification

- `pytest anila-core/tests/`: 166 passed, 5 pre-existing failures (test_router_runtime_contract + test_dispatch_tool — middleware / router-server issues unrelated to this work). 0 regressions.
- G3 gate: `grep document_chunks anila-core/` = **0 hits**. The RAG schema reference is now concentrated in AgenticRAG (3 hits) + Ingestion Platform docs.
- Footprint: −3998 lines of RAG dead code removed across Chunks 1+2+3.

### Commits

| Chunk | Days | Commit | What |
|---|---|---|---|
| 1 | 1–3 | `afc3c9f` | RAG tool factories + AGENTIC_RAG_SYSTEM_PROMPT + the 2 RAG-only test files |
| 2 | 4–6 | `371881d` | `ingestion/` + `api/{documents,search}.py` + `app_factory` slim + `query_engine` rag_preprocessor cleanup |
| 3 | 7–9 | `d7ae6b5` | pg adapters + embedding_nvidia + rag_preprocessor.py file + config RAG fields |
| 4 | 10  | (this commit) | README rewrite + CHANGELOG + G3 gate verification |

---

## v0.4.x and earlier

See `README.md` § Release Notes for Wave A / Wave B history.

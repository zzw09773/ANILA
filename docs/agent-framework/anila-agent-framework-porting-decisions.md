# anila-agent-framework — Porting Decisions

> **⚠ SUPERSEDED — 2026-05-02 v2**
> **This doc was the first-draft porting plan**. After review with the team, scope shifted from "selective port of openai-agents-python" to "synthesis of openai-agents + claude-code patterns into a clean-slate design." See **[`anila-agent-framework-architecture.md`](anila-agent-framework-architecture.md)** for the canonical architecture.
> Keep this doc as **reference material** for what each upstream subsystem does and where to look in the source — useful when implementing each Sprint. The classification table (port-as-is / rewrite / skip) still applies for files we DO use as inspiration; it's just no longer the implementation plan.
>
> Key changes in v2 (architecture doc):
> - **Permission** primitive removed (SSE-mode deployments don't need runtime ASK; auth lives at API gateway)
> - **`USER_SKILL` ActionKind** removed (skills become a v0.2 frontmatter loader producing SYNC_TOOL Actions)
> - 6 primitives → **5** (Action / Middleware / StateMachine / Memory / Provider)
> - 4 ActionKinds → **3** (SYNC_TOOL / BG_TASK / HANDOFF)
> - v0.1 LOC estimate dropped from ~13k (port) to ~3.4k (synthesis)
> - 5 sprints → 4 sprints v0.1 (~8 weeks)

---

> **Status**: original draft, superseded but kept as reference
> **Date**: 2026-05-02
> **Decision context**: AgenticRAG (Phase 0) is now decoupled from anila-core. The next leap is **agent mechanism, not just RAG quality** — lifecycle hooks, guardrails, tracing, handoffs, session protocol, MCP, retry. We pull these from `runtime_logic/openai-agents-python/` (the OpenAI reference implementation) but **selectively port** rather than vendoring whole.
> **Independence rule**: the resulting framework is its own PyPI package, so AgenticRAG (and anila-core, and anyone) depend on it without coupling. AgenticRAG-as-fork-template promise still holds.

## Package shape

```
D:\ANILA\anila-agent-framework\        (new top-level)
├── pyproject.toml                      → name = "anila-agent-framework"
├── src/
│   └── anila_agent\                    (package root)
│       ├── agent.py                    Agent class
│       ├── run_state.py                RunState v1.9 + NextStep union
│       ├── runner.py                   Runner + AgentRunner (refactored)
│       ├── exceptions.py
│       ├── usage.py
│       ├── items.py                    generic msg / event types
│       ├── lifecycle/                  ★ P0 framework
│       ├── guardrails/                 ★ P0 framework
│       ├── handoffs/                   ★ P0 framework
│       ├── tracing/                    ★ P0 framework
│       ├── memory/                     Session Protocol + decorators
│       ├── mcp/                        MCP server framework
│       ├── retry/                      retry advice
│       ├── providers/                  ProviderProtocol (where OAI was hard-wired)
│       │   ├── protocol.py             abstract LLMProvider
│       │   └── openai_compat.py        OpenAI Chat Completions impl
│       └── tools/                      generic ToolDefinition / ToolRegistry
└── tests/
```

**Naming**: keep `anila_agent` short — devs `pip install anila-agent-framework` then `from anila_agent.lifecycle import RunHooks`.

**Constraint**: zero hard imports from `anila_core`, `agentic_rag`, or `openai`. The OpenAI provider is one optional implementation among many — Anthropic, vLLM, local TGI all plug in via the same Protocol.

---

## Classification: port-as-is / rewrite / skip

Each subsystem from `runtime_logic/openai-agents-python/` is classified.

| Subsystem | LOC | Verdict | Why |
|---|---:|---|---|
| `lifecycle.py` | 199 | **port-as-is** | 12 hook callbacks, pure abstract — no OpenAI dependency |
| `guardrail.py` | 343 | **port-as-is** | Two-tier (input/output) framework, pydantic-based, zero OAI |
| `handoffs/__init__.py` | 349 | **port-as-is** | Handoff dataclass + HandoffInputData reducer pattern |
| `handoffs/history.py` | 275 | **port-as-is** | History reduction utilities |
| `memory/session.py` | 150 | **port-as-is** | Session Protocol abstract — pure interface |
| `memory/sqlite_session.py` | 348 | **port-as-is** | Reference SQLite impl, no OAI |
| `memory/openai_*.py` | ~600 | **skip** | OpenAI Conversations / Responses-API specific |
| `mcp/server.py` | 1620 | **port-as-is** | MCP transport (stdio / sse / streaming-http), spec-driven |
| `mcp/manager.py` | 411 | **port-as-is** | Server registry / lifecycle |
| `mcp/util.py` | 488 | **port-as-is** | tool conversion helpers |
| `tracing/spans.py` + `traces.py` | 932 | **port-as-is** | Span tree data model, no OAI |
| `tracing/processors.py` + `scope.py` + `setup.py` + `provider.py` | ~600 | **port-as-is** | Async batch processor, multi-backend dispatch |
| `tracing/create.py` | ~50 | **rewrite** | OpenAI traces upload — replace with pluggable BackendProtocol |
| `tracing/span_data.py` | ~150 | **rewrite** | Strip OpenAI Responses-API span types; keep generic ones (LLM / tool / handoff / guardrail / custom) |
| `agent.py` | 941 | **port-as-is + trim** | Agent class with all the right knobs; trim OpenAI-specific Realtime tool handles |
| `exceptions.py` | 164 | **port-as-is** | Generic exception hierarchy |
| `items.py` | 864 | **port-as-is + trim** | Message / event types — most generic, trim OAI Responses-API items |
| `tool.py` | ~400 | **port-as-is + trim** | ToolDefinition; trim OpenAI Code Interpreter / Web Search built-ins (those are provider-specific) |
| `usage.py` | ~100 | **port-as-is** | Token usage tracking |
| `run_state.py` | 3304 | **port-as-is + trim** | RunState v1.9 schema + serialization. Trim OAI-specific item types |
| `run_internal/run_loop.py` | 1910 | **rewrite** | Heart of execution loop — but every model call goes through OpenAI client. Rewrite around `ProviderProtocol` abstraction |
| `run_internal/turn_resolution.py` | 1958 | **rewrite** | Same — OpenAI Responses-API tied. Generalise around generic Message/ToolCall types |
| `run_internal/tool_execution.py` | 2329 | **rewrite** | Tool dispatch logic; OAI hosted-tool path is provider-specific. Generic dispatcher + provider-specific extension |
| `run_internal/tool_planning.py` | 682 | **rewrite** | Generally portable but has OAI hosted-tool branches |
| `run_internal/guardrails.py` | 191 | **port-as-is** | Just a runner wrapper around guardrail framework |
| `run_internal/agent_runner_helpers.py` | ~200 | **rewrite** | OAI client setup — provider-agnostic equivalent |
| `run_internal/model_retry.py` | ~150 | **rewrite** | OAI-specific retry classification → generic RetryAdvice |
| `run_internal/run_steps.py` | 207 | **port-as-is** | Step model |
| `run_internal/streaming.py` | 70 | **port-as-is** | Three-tier event router (run / agent / model) |
| `run_internal/turn_preparation.py` | 132 | **port-as-is** | Turn boundary / preparation |
| `run_internal/items.py` | ~150 | **rewrite** | OAI item normalisation; keep abstract version |
| `models/` (whole dir) | ~5000 | **skip + extract one provider** | OpenAI provider goes into `providers/openai_compat.py` (≤500 LOC); rest dropped. Other providers (Anthropic / Bedrock) added later as separate files |
| `realtime/` | large | **skip** | OpenAI Realtime API specific |
| `extensions/` | various | **skip initially** | OpenAI-specific extensions — re-evaluate per item |
| `repl.py` | small | **skip** | CLI nicety, not core |
| `prompts.py` | small | **port-as-is** | Generic prompt templating |

**Total port-as-is**: ~7800 LOC
**Total rewrite**: ~5500 LOC (mostly run_loop + turn_resolution + tool_execution generalised around providers)
**Total skip**: ~75k LOC (OpenAI-specific Realtime / Responses API / models / extensions)

So the framework lands at **~13k LOC of focused code** out of the original 89k — same surface area as AgenticRAG itself.

---

## Provider abstraction (the rewrite hot zone)

The rewrite work is concentrated in **how the run loop talks to LLMs**. OpenAI's reference code assumes the OpenAI Responses API everywhere. We pull that into one Protocol:

```python
# anila_agent/providers/protocol.py
class LLMProvider(Protocol):
    """Minimal surface every LLM provider must satisfy."""

    async def chat_completion(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str,
        stream: bool = False,
        **kwargs,
    ) -> ChatCompletionResponse | AsyncIterator[ChatCompletionChunk]:
        ...

    async def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        **kwargs,
    ) -> list[list[float]]:
        ...
```

Three flavours of `chat_completion` to support:
- **OpenAI-compatible** (Chat Completions API — covers OAI, vLLM, NIM, TGI, Ollama)
- **Anthropic Messages API** (different request/response shape)
- **OpenAI Responses API** (different from Chat Completions; agentic-tool-built-in)

Rather than try to unify all three, we ship **OpenAI-compatible** as the default in v0.1 (covers ~80% of self-host setups). Anthropic / Responses API are post-v0.1 follow-ups.

---

## What ports cleanly (the foundation)

These 5 are **port-as-is** and form the v0.1 deliverable. None of them depend on the run loop being rewritten — they sit alongside or above it.

### 1. lifecycle (199 LOC)

```python
class RunHooks(Protocol):
    async def on_run_start(self, ctx, agent): ...
    async def on_run_complete(self, ctx, agent, result): ...
    async def on_agent_start(self, ctx, agent): ...
    async def on_agent_complete(self, ctx, agent, output): ...
    async def on_handoff(self, ctx, from_agent, to_agent): ...
    async def on_tool_start(self, ctx, agent, tool, params): ...
    async def on_tool_end(self, ctx, agent, tool, result): ...
    async def on_llm_start(self, ctx, agent, messages): ...
    async def on_llm_end(self, ctx, agent, response): ...
    async def on_guardrail_start(self, ctx, agent, guardrail): ...
    async def on_guardrail_end(self, ctx, agent, guardrail, result): ...
    async def on_step(self, ctx, agent, step): ...
```

12 hooks; consumers register either a single `RunHooks` impl on the Runner or per-Agent `AgentHooks`. 

**RAG-quality demo**: `on_llm_start` runs query rewriting (HyDE / multi-query), `on_tool_end` for `vector_search` runs reranker cascade.

### 2. guardrail (343 LOC + 191 LOC runner)

```python
@dataclass
class InputGuardrailResult:
    output: GuardrailOutput
    reasoning: str | None = None

class InputGuardrail(Protocol):
    async def run(self, ctx, agent, input: list[Message]) -> InputGuardrailResult: ...

class OutputGuardrail(Protocol):
    async def run(self, ctx, agent, output: AgentOutput) -> OutputGuardrailResult: ...

class GuardrailOutput(BaseModel):
    tripwire_triggered: bool
    info: dict | None = None
```

Two-tier (whole-run before & after) + per-tool (allow / reject_content / raise_exception).

**RAG-quality demo**: 
- `InputGuardrail`: query length, PII scan, query injection detect
- `OutputGuardrail`: citation enforcement (every claim must have a source)
- per-tool: `vector_search` results filter (drop chunks below threshold)

### 3. handoffs (624 LOC)

```python
@dataclass
class Handoff:
    target_agent: Agent
    tool_name: str
    tool_description: str | None = None
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None
    
@dataclass
class HandoffInputData:
    input_history: list[Message]
    pre_handoff_items: list[Message]
    new_items: list[Message]
```

Reducer pattern: when handing off, the receiving agent gets a curated history (you choose: keep all / keep last 3 / summarise / drop tool calls).

**RAG-quality demo**:
- `RetrievalAgent` -(handoff)-> `AnswerAgent` -(handoff)-> `VerifierAgent`
- Self-RAG reflection: `AnswerAgent` -(handoff)-> `RefineQueryAgent` if grounding score < 0.6
- Multi-step decomposition: `DecomposeAgent` -(handoff)-> N x `SubQueryAgent` -(handoff)-> `SynthesisAgent`

### 4. memory / session (498 LOC port + skip OAI variants)

Pure Protocol + reference SQLite impl. Ports nearly verbatim:

```python
class Session(Protocol):
    @property
    def session_id(self) -> str: ...
    async def get_items(self, limit: int | None = None) -> list[Message]: ...
    async def add_items(self, items: list[Message]) -> None: ...
    async def pop_item(self) -> Message | None: ...
    async def clear_session(self) -> None: ...
```

`SQLiteSession` reference impl ships out of the box; AgenticRAG already has `PgSessionStore` / `PgMessageStore` — adapt those to satisfy this Protocol (~30 LOC of wrapper).

### 5. tracing framework (~1500 LOC port + small rewrite)

Span tree + multi-backend processors. The OAI-specific tracing/create.py + span_data.py is the rewrite zone:

```python
class TracingBackend(Protocol):
    async def export(self, traces: list[Trace]) -> None: ...

class StdoutBackend: ...      # default for dev
class FileBackend: ...         # writes JSONL
class HttpBackend: ...         # POST to a collector
```

Drop OAI's `traces/{trace_id}` upload; provide pluggable backend.

**RAG-quality demo**: every retrieval / rerank / answer span lands in tracing; `EvaluatorView` reads from tracing storage.

---

## v0.1 scope — the smallest framework that's usable

```
anila-agent-framework v0.1
├── agent.py            (port-as-is, trimmed)
├── exceptions.py       (port-as-is)
├── usage.py            (port-as-is)
├── lifecycle/          ★ port-as-is
├── guardrails/         ★ port-as-is
├── handoffs/           ★ port-as-is
├── memory/             ★ port-as-is (skip OAI flavors)
├── tracing/            ★ port-as-is + rewrite create.py + span_data.py
├── runner.py           NEW — minimal Runner class wraps simple loop
└── providers/
    ├── protocol.py     NEW — LLMProvider Protocol
    └── openai_compat.py NEW — Chat Completions impl
```

**Deliberately deferred to v0.2**:
- Full `run_state.py` v1.9 (use simpler in-memory state for v0.1)
- `run_internal/run_loop.py` rewrite (use simpler 1-shot agent loop in v0.1)
- MCP framework (heavy, ~2500 LOC, ship in v0.2)
- Retry framework (use httpx default for v0.1; structured retry advice in v0.2)
- Streaming three-tier event router (use simple SSE in v0.1)

**v0.1 LOC budget**: ~6000 LOC ported + ~1500 LOC rewritten = ~7500 LOC.

**v0.2 adds**: full RunState, run_loop rewrite, MCP framework, retry framework, streaming. ~5500 more LOC.

**v0.3 onwards**: Anthropic / Bedrock providers, OpenAI Responses API support, additional MCP backends.

---

## Integration with AgenticRAG

After v0.1 ships, AgenticRAG depends on it via:

```toml
# AgenticRAG/pyproject.toml
dependencies = [
    "pydantic>=2.0",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",
    "python-frontmatter>=1.1",
    "pyyaml>=6.0",
    "anyio>=4.0",
    "sse-starlette>=2.0",
    "aiofiles>=23.0",
    "anila-agent-framework>=0.1",  # ★ NEW
]
```

AgenticRAG migrates piece-by-piece (not big-bang):

| AgenticRAG file | Migration |
|---|---|
| `engine/query_engine.py` | Replace internal loop with `from anila_agent.runner import Runner` |
| `models/message.py` | Re-export from `anila_agent.items` (or alias) |
| `router/tool_router.py` | Replace with `anila_agent.tools.ToolRegistry` |
| Memory adapters | Implement `anila_agent.memory.Session` Protocol on top of `PgSessionStore` |
| `engine/rag_preprocessor.py` | Becomes a `RunHooks.on_llm_start` impl |

**Backwards compat**: AgenticRAG keeps shipping its current API surface during the migration; the old `QueryEngine` calls the framework underneath. Devs forking pre-migration still work; new dev forks get the framework-based path.

---

## Risks

| Risk | Mitigation |
|---|---|
| OpenAI's run_loop / turn_resolution complexity hides edge cases we miss in rewrite | Port test suite from openai-agents-python alongside; if a test exists, our rewrite must pass it |
| RunState v1.9 schema is what enables checkpointable runs (saved state, resume); skipping in v0.1 means no resume | Document it as v0.1 limitation; v0.2 priority. Use simple in-memory state |
| Three providers (OAI / Anthropic / Responses) diverge enough that a unified protocol leaks | v0.1 ships only OpenAI-compatible. Build Anthropic in v0.3 with willingness to extend Protocol if needed |
| Devs already on AgenticRAG see a churn cycle | Ship migration alongside framework — old QueryEngine wraps new Runner internally so existing /agentic-chat calls don't break |
| AgenticRAG's evaluator + ingestion pipeline assume specific message shapes | Adapter layer at `agentic_rag.compat` translates framework Message ↔ legacy DocumentChunk pipeline |

---

## Decisions still open (need your call)

1. **Package name**: `anila-agent-framework` vs shorter `anila-agent` vs neutral `agent-runtime`. I recommend `anila-agent-framework` — clear, brand-anchored, distinguishes from `anila-core` (platform infra) and `agentic-rag` (template).
2. **Repo layout**: do we want this as a separate Git repo (`zzw09773/anila-agent-framework`) or as a sibling top-level inside the ANILA monorepo? I recommend monorepo for now (faster iteration cycle, shared CI), can split later.
3. **License**: openai-agents-python is MIT. We need to retain MIT attribution in ported files (`# Originally ``…`` from openai-agents-python (MIT)`). Can you confirm this is OK?

---

**Last updated**: 2026-05-02 · **Total LOC plan**: ~13k for the framework (vs 89k upstream) · **v0.1 → v0.2 → v0.3** ladder

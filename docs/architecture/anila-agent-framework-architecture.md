# anila-agent-framework — Architecture

> **Status**: design spec, awaiting review
> **Date**: 2026-05-02
> **Authors**: synthesis of patterns from `runtime_logic/openai-agents-python` (MIT) and `runtime_logic/claude-code-src` (reference)
> **Supersedes**: `docs/anila-agent-framework-porting-decisions.md` (still useful as a source-by-source reference, but the v0.1 implementation follows this architecture, not a verbatim port).

---

## 0. Why a synthesis, not a port

Both reference implementations are mature and battle-tested, but each has structural problems that hurt long-term evolution:

| openai-agents-python | claude-code-src |
|---|---|
| 12 lifecycle hooks are a **fixed callback list** — extending requires forking | Permissions logic spread across `hooks/` + `services/` + `tools/` — but ASK-style permissions are CLI-only |
| guardrails / lifecycle / tracing are **three independent frameworks** doing structurally identical work (intercept around an action) | Memory is `memdir + autoDream + extractMemories + SessionMemory` — four overlapping subsystems |
| `run_loop.py` is 1,910 LOC — hard to test, hard to checkpoint | Tool / Task / Skill / Background-Task are conceptually overlapping but each lives in a separate subsystem |
| `run_state.py` is 3,304 LOC — schema bloat from OpenAI Responses API legacy | UI (`Ink`) is baked deep into the runtime, hard to extract for headless use |
| RunState mutability — race conditions between concurrent middleware | TodoV2 / Tasks / Skills are surfaced as built-in tools without a unifying abstraction |

The synthesis **does not** try to be both at once. It picks one core insight that resolves both sets of pain:

> **Everything an agent does is an Action. Lifecycle, guardrails, tracing, cost — all of these are *middleware around Actions*, not separate frameworks.**

If we get the Action + middleware + state machine right, every other subsystem becomes a thin layer on top.

**Deployment-shape constraint** (decided 2026-05-02): the framework targets **HTTP / SSE-mode agents**, not interactive CLI. That means:

- No runtime "ASK the user" decision — the request has already been authorized when it arrived. Authentication / authorization happens at the API gateway (CSP service tokens, RLS scoping), **before** the agent runtime sees the request.
- No plan-mode flow that pauses execution waiting for human approval — there's nobody at the other end of the SSE stream to click an approval button mid-run.
- Permission / authorization is **deployment policy** (gateway), not framework feature. The framework ships Action metadata (`side_effect_class`) for tracing / observability but does not ship a runtime permission resolver.
- If a deployment wants extra runtime gating (e.g., admin-side audit hooks), they write it as a Middleware. The framework does not ship a built-in permission system.

---

## 1. Goals and non-goals

### Goals

- **Provider-agnostic** — works with OpenAI / Anthropic / vLLM / NIM / TGI / Ollama via one Protocol
- **Production-grade defaults** — cost tracking, permissions, tracing, retry are first-class, not bolt-ons
- **Composable** — every subsystem (lifecycle, guardrails, tracing, etc.) is the same machinery (middleware), so devs only learn one pattern
- **Checkpointable** — runs can be paused, persisted, and resumed (state machine + immutable snapshots)
- **No anila-core / agentic_rag imports** — the framework is a clean PyPI package both can consume independently
- **Small surface** — ~3,000 LOC for v0.1 (vs 89k LOC upstream OpenAI / 1,902 files claude-code)
- **MIT-attributed where applicable** — files inspired by upstream carry provenance headers

### Non-goals (deferred or out of scope)

- **No UI** — terminal renderers (Ink), web UIs, IDE plugins are downstream concerns
- **No model training / fine-tuning** — we orchestrate inference, not training
- **Not a workflow engine** — Airflow / Temporal / Prefect serve different needs
- **Not a multi-agent OS** — coordinator pattern lands in v0.2; full swarm orchestration is later
- **Not a tool marketplace** — MCP integration ships, but no curation or registry hosting

---

## 2. Core insight: the Action abstraction

Every "thing an agent does" is an **Action**. There are four kinds, but they share one shape:

```python
@dataclass(frozen=True)
class Action:
    """Anything an agent does — tool call, background task, handoff to
    another agent. One shape, one machinery. No more separate
    frameworks for each kind."""

    name: str
    description: str
    kind: ActionKind                    # sync_tool | bg_task | handoff
    input_schema: dict[str, Any]        # JSON schema for params
    output_schema: dict[str, Any] | None
    cost_estimate: CostEstimate         # tokens / dollars / time
    side_effect_class: SideEffectClass  # PURE | LOCAL | NETWORKED | IRREVERSIBLE
    middleware: tuple[Middleware, ...]  # composed at registration time
    handler: Callable[[ActionContext], Awaitable[ActionResult]]


class ActionKind(StrEnum):
    SYNC_TOOL = "sync_tool"   # LLM-callable, blocks the run loop until done
    BG_TASK   = "bg_task"     # long-running, killable, output-to-file
    HANDOFF   = "handoff"     # transfer control to another Agent


class SideEffectClass(StrEnum):
    """Pure metadata for tracing / observability. Not used as a runtime
    gate — authorization happens at the API gateway before the request
    reaches the framework. Useful for: tracing dashboards, audit logs,
    cost attribution, rate-limiting middleware that consumers may add."""

    PURE         = "pure"         # no observable effect outside its return
    LOCAL        = "local"        # filesystem / process state
    NETWORKED    = "networked"    # remote API / DB writes
    IRREVERSIBLE = "irreversible" # once done, can't undo
```

**What's gone vs original draft**: `USER_SKILL` ActionKind is removed — the framework targets SSE-mode agents where users don't directly invoke skills. Skill discovery (loading frontmatter markdown into Actions) returns in v0.2 as a registration helper that produces `SYNC_TOOL` Actions; the runtime kernel will never see "skill" as a kind. `PermissionClass` is also removed — gating happens at the gateway, not the runtime.

### Why one abstraction instead of three

In openai-agents-python, "tool" / "handoff" / "guardrail" are different types with different machinery. In claude-code, "Tool" / "Task" / "Skill" / "Background Task" are four parallel subsystems. **All of them are: a thing with a name, schema, side-effect profile, and a handler that runs.**

Unifying them means:

- **One tracing format** captures all kinds (handoffs appear in the trace tree alongside tool calls and bg tasks)
- **One cost-tracking pipe** counts spend whether it's an LLM call inside a tool, inside a bg task, or across a handoff
- **One middleware chain** applies whether you're guardrail-checking input, cost-budgeting, retrying, or tracing

### The three kinds, exemplified

```python
# 1. sync_tool — what openai-agents calls a "tool"
vector_search = Action(
    name="vector_search",
    kind=ActionKind.SYNC_TOOL,
    side_effect_class=SideEffectClass.PURE,
    handler=lambda ctx: do_vector_search(ctx.params),
    ...
)

# 2. bg_task — what claude-code calls a "Task"
ingest_corpus = Action(
    name="ingest_corpus",
    kind=ActionKind.BG_TASK,
    side_effect_class=SideEffectClass.LOCAL,
    handler=lambda ctx: spawn_worker(ctx.params),
    ...
)

# 3. handoff — what openai-agents calls a "Handoff"
to_verifier = Action(
    name="handoff_to_verifier",
    kind=ActionKind.HANDOFF,
    side_effect_class=SideEffectClass.PURE,
    handler=lambda ctx: HandoffResult(target=verifier_agent, reduced_history=...),
    ...
)
```

---

## 3. The five primitives

Five primitives compose into the entire framework. Each has one job; nothing else does that job.

```
1. Action       — what an agent can do (above)
2. Middleware   — what wraps an Action's execution
3. StateMachine — how the agent transitions between phases
4. Memory       — what the agent knows beyond conversation history
5. Provider     — how the agent talks to LLMs
```

**Permission deliberately not a primitive** — see §0 deployment-shape constraint. Authorization is a gateway concern. If a deployment wants extra runtime gating, they ship it as a Middleware (the Middleware framework is exactly the right place for "intercept around an action").

### 3.1 Middleware — one chain to rule them all

Lifecycle, guardrails, tracing, cost, retry — same chain.

```python
class Middleware(Protocol):
    """Wraps an Action's execution. Returns the result, can short-circuit,
    can mutate input/output. Same shape for every concern."""

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: Callable[[ActionContext], Awaitable[ActionResult]],
    ) -> ActionResult:
        ...
```

**Built-in middleware** (each ~50-150 LOC):

```python
TraceMiddleware(backend)     # opens a span, populates input/output, closes span
CostMiddleware(tracker)      # accumulates token / $ cost on the run
TimeoutMiddleware(seconds)   # cancels the action if it exceeds timeout
RetryMiddleware(advice)      # retries with backoff per RetryAdvice
SchemaValidationMiddleware() # validates input/output against JSON schemas
LoggingMiddleware(logger)    # structured logs at enter/exit/error
GuardrailMiddleware(rules)   # input/output content checks (PII, citation, etc.)
```

**User-defined middleware** is just a Python callable matching the Protocol. Shell-command hooks (claude-code style) are one specific Middleware impl:

```python
class ShellHookMiddleware:
    """Runs a shell command before/after the action. The command receives
    a JSON payload on stdin and may emit a JSON decision (allow / deny /
    modify) on stdout. Identical contract to claude-code's PreToolUse /
    PostToolUse hooks — but it's just one Middleware among many, not a
    separate subsystem."""

    def __init__(self, when: Literal["before", "after"], command: str): ...
    async def __call__(self, action, context, next_): ...
```

**Why this beats both sources**:

- openai-agents has 12 fixed lifecycle hook names; users who want "after-tool-completes-with-error-of-this-type" have to grep through every callback. Here it's one Middleware that subscribes to whatever it cares about.
- claude-code's hooks are powerful (shell commands!) but live in a separate subsystem from guardrails. Here both are the same chain.
- Middleware **composes** — you can stack a TraceMiddleware on top of a PermissionMiddleware on top of a RetryMiddleware. Order is explicit at registration.

### 3.2 StateMachine — the run loop, made legible

openai-agents-python's `run_loop.py` is 1,910 LOC because every transition is implicit (in a giant `while True`). Make transitions explicit:

```python
class RunPhase(StrEnum):
    PLANNING    = "planning"     # LLM call to decide next step
    ACTING      = "acting"       # executing an Action
    OBSERVING   = "observing"    # capturing tool / task results
    REFLECTING  = "reflecting"   # optional self-evaluation step
    HANDING_OFF = "handing_off"  # switching active agent
    DONE        = "done"
    ERROR       = "error"


@dataclass(frozen=True)
class RunState:
    """Immutable snapshot of an agent run. Each phase transition produces
    a new RunState. Old states are kept for tracing / debugging /
    checkpointing. Never mutated in place."""

    run_id: str
    agent: Agent
    phase: RunPhase
    history: tuple[Message, ...]
    pending_actions: tuple[Action, ...]
    completed_actions: tuple[ActionResult, ...]
    usage: Usage
    cost: CostAccumulator
    parent_run_id: str | None  # for handoff chains
    created_at: datetime
    updated_at: datetime


class StateMachine:
    """Drives an agent through phases. Explicit transition table. Every
    transition emits a tracing event. Resumable from any RunState
    snapshot (next sprint deliverable for v0.1, full impl in v0.2)."""

    async def step(self, state: RunState) -> RunState:
        """One transition. Pure function modulo provider / handler I/O."""
        match state.phase:
            case RunPhase.PLANNING:    return await self._plan(state)
            case RunPhase.ACTING:      return await self._act(state)
            case RunPhase.OBSERVING:   return await self._observe(state)
            case RunPhase.REFLECTING:  return await self._reflect(state)
            case RunPhase.HANDING_OFF: return await self._handoff(state)
            case RunPhase.DONE | RunPhase.ERROR: return state
```

**Why this beats both sources**:

- openai-agents-python's run_loop is a giant `while True` with dozens of branches. Hard to test phases in isolation. Here each `_plan / _act / _observe` is independently testable.
- claude-code's run loop assumes terminal UI rendering happens between phases — coupling that headless deployments fight. Here phases are pure modulo I/O; UI plugs in via Middleware.
- Immutable RunState makes **checkpoint / resume free** — serialize a RunState, restart later, call `step()`. openai-agents took 3,304 LOC of `run_state.py` to get there; ours is ~150 LOC because the design assumed it from day 1.

### 3.3 Memory — semantic store separate from message history

claude-code's mistake: `memdir` (semantic memory) and message history live in different subsystems but interact through `extractMemories` + `findRelevantMemories`. The boundary leaks.

openai-agents' mistake: Session Protocol IS the message history. There's no semantic store at all.

**Our split**:

```python
class MessageHistory(Protocol):
    """Conversation log. Lives inside RunState. NOT memory."""
    async def append(self, msg: Message) -> None: ...
    async def get(self, limit: int | None = None) -> list[Message]: ...
    async def truncate(self, n: int) -> None: ...


class SemanticMemory(Protocol):
    """Long-lived facts about the user / project / agent itself. Survives
    across runs. Has typed slots — not a flat blob."""
    async def remember(self, entry: MemoryEntry) -> None: ...
    async def recall(self, query: str, kind: MemoryKind | None = None) -> list[MemoryEntry]: ...
    async def forget(self, entry_id: str) -> None: ...


class MemoryKind(StrEnum):
    USER       = "user"        # who the user is, role, expertise
    FEEDBACK   = "feedback"    # corrections / rules they want followed
    PROJECT    = "project"     # ongoing work state, deadlines, decisions
    REFERENCE  = "reference"   # pointers to external systems
    WORKING    = "working"     # this-session scratch (auto-expires)
```

`MessageHistory` is per-RunState and ephemeral. `SemanticMemory` is per-Agent (or per-User) and persistent. They don't bleed into each other.

**Why this beats both sources**:

- The two concerns have very different access patterns (history is append-only sequence, memory is keyed semantic recall). Separating them lets each be optimized independently.
- claude-code's 4-type memdir taxonomy is great; we adopt it but cleanly.

### 3.4 Provider — same as before

Already designed (see Sprint 1 stage A). Protocol with `chat_completion` + `embeddings`, OpenAI-compatible default impl.

### 3.5 Action × the others

Putting it all together:

```python
async def execute_action(
    action: Action,
    context: ActionContext,
    *,
    middleware_stack: list[Middleware],
) -> ActionResult:
    """The one path every Action runs through. Middleware composes
    tracing, cost, retry, guardrails, etc. all on top of this."""

    # Build the chain: middleware wraps middleware wraps the handler.
    handler = action.handler
    for mw in reversed(middleware_stack):
        prev = handler
        async def wrapped(ctx, _mw=mw, _prev=prev, _act=action):
            return await _mw(_act, ctx, _prev)
        handler = wrapped

    return await handler(context)
```

That's the kernel. Everything else is middleware.

---

## 4. What this looks like to consumers

### A minimal agent

```python
from anila_agent import Agent, Runner, Action, ActionKind, SideEffectClass
from anila_agent.middleware import TraceMiddleware, CostMiddleware
from anila_agent.providers.openai_compat import OpenAICompatProvider

# Define an action
async def search_handler(ctx):
    results = my_db.search(ctx.params["query"])
    return ActionResult(output={"results": results})

vector_search = Action(
    name="vector_search",
    description="Semantic search over the user's documents",
    kind=ActionKind.SYNC_TOOL,
    side_effect_class=SideEffectClass.PURE,
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    cost_estimate=CostEstimate(tokens=0, dollars=0.0001),
    middleware=(),
    handler=search_handler,
)

# Build agent
agent = Agent(
    name="rag-agent",
    instructions="You answer questions using the search tool.",
    actions=[vector_search],
    provider=OpenAICompatProvider(base_url="http://vllm:8000/v1", model="gemma4"),
)

# Run-level middleware (fires for every Action in the run)
runner = Runner(
    middleware=[
        TraceMiddleware(backend=StdoutBackend()),
        CostMiddleware(tracker=cost_tracker),
    ],
)

result = await runner.run(agent, user_message="Find docs about parent-child RAG")
print(result.final_output)
print(f"Total cost: ${result.cost.dollars:.4f}")
```

### Adding a guardrail

```python
class CitationGuardrail:
    """Output guardrail — every claim must have a citation."""

    async def __call__(self, action, context, next_):
        if action.kind != ActionKind.SYNC_TOOL or action.name != "_finalize_answer":
            return await next_(context)
        result = await next_(context)
        if not has_citations(result.output["text"]):
            return ActionResult(error="Answer missing citations; please retry with sources.")
        return result

runner = Runner(middleware=[..., CitationGuardrail()])
```

No new framework. Just middleware.

### Adding a shell hook (claude-code style)

```python
runner = Runner(
    middleware=[
        ShellHookMiddleware(
            when="before",
            command="./scripts/audit_tool_use.sh",
        ),
        ...
    ],
)
```

The shell command receives `{action: {name, kind, ...}, params: {...}}` on stdin and emits `{"decision": "allow"|"deny"|"modify", "reason": "..."}` on stdout. Same Middleware Protocol as Python middleware.

---

## 5. Comparison: framework size vs sources

| Component | openai-agents | claude-code | **anila-agent v0.1** |
|---|---:|---:|---:|
| Core types (Action / Message / etc.) | 864 LOC `items.py` | scattered | **~400** |
| Lifecycle / guardrails / tracing | 199 + 343 + 1500 = 2042 | scattered | **~600 (Middleware framework)** |
| Run loop | 1910 LOC | scattered | **~500 (StateMachine)** |
| Run state schema | 3304 LOC | scattered | **~200** |
| Permission | n/a (just guardrails) | 800+ LOC scattered | **0 (gateway concern, not framework)** |
| Memory | 150 (Session) + 348 (SQLite) | ~1500 (memdir + autoDream + extract) | **~400 (split history / semantic)** |
| Provider abstraction | n/a — OpenAI hardwired | n/a | **~600** |
| Cost tracking | n/a (Usage only) | ~400 LOC | **(middleware, ~80 LOC)** |
| **Total** | ~10,000+ LOC | ~5,000+ LOC | **~2,800 LOC** |

We're much smaller because:

- **One framework (Middleware) instead of three** (lifecycle / guardrail / tracing) saves ~1500 LOC alone
- **No OpenAI Responses API legacy** — we don't need `run_state.py`'s 3304 LOC of schema serialization
- **No UI** — claude-code carries Ink + components everywhere; we don't
- **No CLI permission system** — gateway-style auth means the framework doesn't ship a runtime permission resolver
- **One Action abstraction** instead of separate Tool / Task / Skill / Handoff frameworks

---

## 6. v0.1 sprint plan (revised)

Each sprint ships **one primitive + one RAG demo using it**. Rough cuts:

| Sprint | Primitive | LOC | Demo |
|---|---|---:|---|
| 1 (done — stage A) | Foundation: pyproject + exceptions + usage + provider Protocol | 700 | (none) |
| 1 (stage B) | Provider impl + minimal Runner + Agent + Action types | 1100 | Hello-world agent against vLLM |
| 2 | Middleware framework + TraceMiddleware + CostMiddleware + ShellHookMiddleware | 700 | Audit log + cost tracking + claude-code-style shell hooks |
| 3 | StateMachine + RunState immutable snapshots | 500 | Checkpoint / resume demo |
| 4 | MessageHistory + SemanticMemory + MemoryKind | 400 | Self-RAG using semantic recall |

**v0.1 total: ~3400 LOC, 4 sprints, ~8 weeks.**

v0.2 adds: handoff/coordinator, BG tasks, Skill loader (frontmatter → SYNC_TOOL Actions), MCP, retry framework, Anthropic provider. Another ~3000 LOC across 5-6 sprints.

---

## 7. RAG-quality demos as Middleware-on-top

The 7 RAG-quality items from the original plan slot in like this:

| Original demo | Lands as | Sprint |
|---|---|---|
| Query rewriting | Middleware that rewrites params before retrieval | 2 |
| Self-RAG reflection | StateMachine REFLECTING phase | 3 |
| Citation enforcement | GuardrailMiddleware (output) | 2 |
| RAG guardrails | Stack of input / post-retrieval / pre-answer middleware | 2 |
| Multi-step decomposition | Sub-agent handoff chain | v0.2 |
| Reranker cascade | RetryMiddleware on the reranker action | 2 |
| Audit log | TraceMiddleware → PostgresBackend | 2 |

So 5 of 7 land in v0.1. Last 2 land in v0.2 with handoff framework.

---

## 8. Open questions (need user call before Sprint 1 stage B starts)

1. **Action `frozen=True` strict immutability** — is this OK with the team? Forces a more functional style. Pro: thread-safe by construction. Con: more typing.
2. **`StrEnum`** — requires Python 3.11+. We've been targeting 3.10+. Acceptable to bump to 3.11 minimum?
3. **`match` statement** in StateMachine — same Python 3.10+ requirement; sticks.
4. **MIT attribution boundary** — files where we take >30% of the original logic carry an "Originally from openai-agents-python (MIT)" header. Files with <30% just reference the upstream in a comment. OK as policy?
5. **Naming**: `Action` vs `Operation` vs `Capability`. I prefer `Action` (matches RL terminology, short, clear). Open to alternatives.
6. **Sprint cadence** — synthesis is more demanding (greenfield design) than verbatim port. 2-week sprints OK, or stretch to 3?

---

## 9. What stays from Sprint 1 stage A

Already shipped, all reusable:

- `exceptions.py` — generic exception hierarchy (universal)
- `usage.py` — Usage / RequestUsage / token details (universal — works for any provider)
- `providers/protocol.py` — LLMProvider Protocol skeleton (will be widened in stage B as Action types land)

Stage B work changes scope: instead of porting `agent.py` from openai-agents wholesale, we **write fresh** following this architecture. Maybe ~600 LOC instead of porting 941 LOC.

---

## 10. What I need from you to start Sprint 1 stage B

Three quick decisions:

| # | Question | My recommendation |
|---|---|---|
| 1 | Approve this architecture? | (your call) |
| 2 | Python 3.10 vs 3.11 minimum? | **3.11** (StrEnum, match, better typing) |
| 3 | Action / Middleware as final names? | **Action** + **Middleware** (no synonyms) |

After you answer, I'll:
- Update `docs/anila-agent-framework-porting-decisions.md` to note it's superseded
- Rewrite `docs/agenticrag-phase1-plan.md` with the new sprint structure
- Resume Sprint 1 stage B with clean implementation

---

**Last updated**: 2026-05-02 (v2 — permission removed, user_skill removed, 5 primitives) · **Total framework v0.1 size estimate**: ~3,400 LOC · **Read alongside**: `docs/runtime-logic-openai-agents-deep-dive.md`, `docs/anila-agent-framework-porting-decisions.md` (now reference material, not the plan)

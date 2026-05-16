# Phase 1 Plan — anila-agent-framework + AgenticRAG agent-mechanism (v2)

> **Status**: revised plan, ready to execute
> **Date**: 2026-05-02
> **v2 changes**: rewritten to follow the synthesis architecture (not the port plan). 5 primitives, 4 sprints v0.1.
> **Reads alongside**: `docs/anila-agent-framework-architecture.md` (canonical architecture spec)

---

## Why this plan exists

After Phase 0 (AgenticRAG decoupled from anila-core), the next leap is **agent mechanism, not just RAG quality**. Two reference systems were studied:

- `runtime_logic/openai-agents-python/` — mature OpenAI Agents SDK
- `runtime_logic/claude-code-src/` — claude-code's runtime

Synthesis (not verbatim port) chosen because both have structural problems we can avoid (see architecture doc §0).

---

## What we ship

A new PyPI package: **`anila-agent-framework`** at `D:\ANILA\anila-agent-framework\` (Sprint 1 stage A already in place).

Core abstractions (5 primitives, see architecture doc):
1. **Action** — unified abstraction for sync_tool / bg_task / handoff (3 kinds)
2. **Middleware** — one chain unifies lifecycle / guardrails / tracing / cost / retry
3. **StateMachine** — explicit phase transitions, RunState immutable
4. **Memory** — split message-history (per-run) from semantic-store (cross-run)
5. **Provider** — LLMProvider Protocol + OpenAI-compatible default

What's NOT a primitive (and why):
- **Permission** — gateway concern (CSP service tokens / RLS), not framework feature. SSE-mode agents don't ASK at runtime.
- **UI / CLI / Ink** — out of scope; framework is headless
- **Skills (frontmatter loader)** — v0.2 feature, just a registration helper that produces SYNC_TOOL Actions

---

## Constraints (locked decisions)

| Decision | Resolution |
|---|---|
| Package home | `D:\ANILA\anila-agent-framework\` (monorepo sibling) |
| Module name | `anila_agent` |
| PyPI name | `anila-agent-framework` |
| Python version | **3.11+** (StrEnum, match, modern typing) |
| License | MIT (with provenance headers on files inspired by openai-agents-python) |
| Naming | `Action` + `Middleware` (no synonyms) |
| Permission | NOT in framework — deployment-policy concern |
| `USER_SKILL` ActionKind | Removed (SSE mode only) |
| AgenticRAG dependency on this package | OK — no different from depending on httpx |
| anila-core dependency on this package | OK if/when needed (separate project decision) |

---

## Sprint structure — v0.1 (4 sprints, ~8 weeks)

Each sprint ships **one primitive + one RAG demo using it**. Cadence: 2 weeks/sprint (stretch to 3 if synthesis demands).

### Sprint 1 — Foundation (W0-W2)

**Stage A — DONE** (commit `b8a06d2`):
- `pyproject.toml`, `README.md`, package skeleton
- `anila_agent.exceptions` (generic hierarchy)
- `anila_agent.usage` (provider-agnostic Usage / RequestUsage / token details)
- `anila_agent.providers.protocol` (LLMProvider Protocol skeleton)
- 10 smoke tests, `pip install -e .` clean

**Stage B — TODO**:
- `anila_agent.items` — Message / ToolCall / ToolResult types (~300 LOC, fresh-write per architecture)
- `anila_agent.action` — `Action` dataclass + `ActionKind` enum + `SideEffectClass` (~200 LOC)
- `anila_agent.tool` — `ToolRegistry`, `ToolDefinition` (~150 LOC)
- `anila_agent.agent` — `Agent` class (~250 LOC, fresh-write — not porting agent.py wholesale)
- `anila_agent.runner` — minimal `Runner.run(agent, messages)` doing single-pass loop (~200 LOC)
- `anila_agent.providers.openai_compat` — Chat Completions provider (~500 LOC)
- Tests: agent runs against fake provider, completes one tool call

**Acceptance**: hello-world agent against vLLM with one tool call works end-to-end. ~1100 LOC stage B.

### Sprint 2 — Middleware framework (W2-W4)

**Primitive**: `anila_agent.middleware`
- `Middleware` Protocol + chain composition (~150 LOC)
- `TraceMiddleware` + `Span` + `TracingBackend` Protocol + `StdoutBackend` / `FileBackend` (~250 LOC)
- `CostMiddleware` + `CostTracker` + per-model price table (~80 LOC)
- `ShellHookMiddleware` (claude-code-style PreToolUse / PostToolUse, JSON over stdin/stdout) (~120 LOC)
- `GuardrailMiddleware` + `Guardrail` Protocol (input / output content checks) (~100 LOC)

**RAG demos** (in AgenticRAG):
- Audit log of every action via `TraceMiddleware → PostgresBackend`
- Cost tracking surfaced to `EvaluatorView`
- Citation enforcement as `GuardrailMiddleware` (output)
- Query rewriting as middleware that mutates retrieval params before vector_search action
- Reranker cascade as `RetryMiddleware` on the reranker action

**LOC**: ~700 framework + ~500 demo wiring.

### Sprint 3 — StateMachine + RunState (W4-W6)

**Primitive**: `anila_agent.state_machine`
- `RunPhase` enum (PLANNING / ACTING / OBSERVING / REFLECTING / HANDING_OFF / DONE / ERROR)
- `RunState` immutable dataclass (~150 LOC)
- `StateMachine.step(state) → state` pure transitions (~250 LOC)
- `RunSerializer` for checkpoint / resume (~100 LOC)

**RAG demo**: long-form generation that survives container restart — checkpoint mid-run, restart pod, resume from saved RunState. Self-RAG reflection enters `REFLECTING` phase explicitly.

**LOC**: ~500 framework + ~200 demo.

### Sprint 4 — Memory (W6-W8)

**Primitive**: `anila_agent.memory`
- `MessageHistory` Protocol — append-only conversation log per-RunState (~80 LOC)
- `InMemoryHistory` reference impl (~50 LOC)
- `SemanticMemory` Protocol — long-lived facts, keyed recall (~80 LOC)
- `MemoryKind` enum (USER / FEEDBACK / PROJECT / REFERENCE / WORKING)
- `MemoryEntry` dataclass with frontmatter-style metadata
- `SQLiteMemoryStore` reference impl with TTL + kind-filter recall (~190 LOC)

**RAG demo**: agent uses `SemanticMemory.recall("user prefers concise answers")` to inject persistent preferences into prompt — survives across SSE sessions.

**LOC**: ~400 framework + ~150 demo.

### v0.1 GA gate (after Sprint 4)

- 5 primitives shipped, ~3400 LOC framework + ~850 LOC RAG demos
- AgenticRAG depends on `anila-agent-framework>=0.1` in `pyproject.toml`
- Existing `/agentic-chat` works either with framework (new path) or without (legacy `QueryEngine` kept in parallel during v0.1)
- `pip install anila-agent-framework` works
- Devs can fork AgenticRAG, install framework, ship a custom agent in <1 day

---

## Sprint structure — v0.2 (~10 more weeks)

Brief — details depend on v0.1 learnings:

- **Sprint 5** — Handoff + Coordinator (1-to-1 + 1-to-N delegation)
- **Sprint 6** — `BG_TASK` ActionKind support (long-running, killable, output-to-file)
- **Sprint 7** — Skill loader (frontmatter markdown → SYNC_TOOL Actions, conditional, dynamic discovery)
- **Sprint 8** — MCP framework (server / manager / util — claude-code MCP integration)
- **Sprint 9** — RetryMiddleware advanced advice patterns + Anthropic provider

After v0.2: Sprint 5-7 RAG demos (multi-step decomposition / reflection-loop with handoffs / dynamic skills).

---

## Migration into AgenticRAG (per-sprint)

| Sprint | AgenticRAG migration |
|---|---|
| 1 stage B | `pyproject.toml` adds `anila-agent-framework>=0.1.0a1`; new `agentic_rag/runtime/` wraps existing `QueryEngine` to use framework underneath. Old endpoints unchanged. |
| 2 | Existing query-engine event emitter → use `TraceMiddleware`. Existing token logging → `CostMiddleware`. Add `GuardrailMiddleware` for citation enforcement. |
| 3 | Long-form `/agentic-chat` switches to `StateMachine`-driven runner. Resume support added behind feature flag. |
| 4 | `SemanticMemory` impl backed by Postgres (re-using existing `PgSessionStore`). |

**Old `QueryEngine` stays in parallel during v0.1** (deprecation flag, full removal in v0.2). No big-bang rewrite.

---

## Risks (from architecture doc + sprint specifics)

| Risk | Mitigation |
|---|---|
| Synthesis means greenfield design — design mistakes surface late | Each Sprint demo MUST be a real production-quality use case; if a primitive can't carry it, ratchet design before next sprint |
| Sprint 1 stage B is biggest single batch (~1100 LOC) | Already de-risked by stage A; rest is fresh-write following architecture, not blind port |
| Provider Protocol leaks Chat Completions assumptions | v0.1 ships only OpenAI-compatible; Anthropic enters v0.2 with willingness to widen Protocol |
| Existing AgenticRAG /agentic-chat regresses | Old QueryEngine in parallel until v0.2; CI gate runs old + new paths |
| Devs forking AgenticRAG see churn | Migration is per-sprint, opt-in via feature flag; framework dependency in pyproject.toml is the only mandatory change |

---

## Sprint 1 stage A retrospective

What landed: ~700 LOC of foundation, 10 smoke tests, clean `pip install`.

What changed during stage A:
- v0 was "verbatim port from openai-agents-python." After deep-dive doc review, scope expanded to claude-code-src. After SSE-mode deployment review, Permission primitive removed. After USER_SKILL review, ActionKind reduced to 3.
- Net: the foundation files (exceptions / usage / provider Protocol) are unchanged from stage A. They were universal; both port plan and synthesis plan use them as-is.

**No rework needed for stage A code** — proceed directly to stage B with synthesis architecture.

---

**Last updated**: 2026-05-02 (v2) · **v0.1 estimate**: ~3,400 LOC + RAG demos in 4 sprints (~8 weeks) · **Architecture spec**: `docs/anila-agent-framework-architecture.md`

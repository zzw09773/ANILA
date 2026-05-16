# ANILA Core Runtime Design Note

## Purpose

This document defines how `anila-core` operates as the **Python runtime foundation** for the ANILA platform — not a RAG pipeline, but a general-purpose multi-agent runtime that agents (including RAG ones) are built on top of. It records what is borrowed from `claude-code-src`, what is ANILA's own layer, and what the responsibility boundaries are for each runtime subsystem.

---

## 1. What anila-core Is

`AgenticRAG/src/anila_core/` has been re-positioned away from "RAG-first" toward being the Python runtime foundation for all ANILA agents:

- **Router** is built on it (as a `CoordinatorAgent` in dispatcher mode)
- **Knowledge / RAG agents** are built on it (with optional vector retrieval)
- **Future workflow agents** (Onyx or others) will be built on it

The RAG pipeline (`rag_preprocessor.py`, `api.py`) is one *optional* capability layered on top — not the runtime itself.

---

## 2. Session State: How anila-core Carries Conversation Context

### Core abstraction: `AgentContext`

**File**: `src/anila_core/context/agent_context.py`

Each agent invocation runs in an isolated `AgentContext`, scoped via Python `contextvars.ContextVar`. This means:

- Every `asyncio.Task` running a turn loop gets its own context view
- Subagents **fork** from the parent context (`create_subagent_context()`), copying messages and memory snapshot without sharing mutable references
- Abort signals are per-context — a parent can cancel a subagent without affecting siblings

Key fields:
```
context_id       unique per invocation
session_id       links turns belonging to the same user session
messages         full conversation history for this context
memory_snapshot  injected persistent memory (from memdir, session memory)
abort_signal     asyncio.Event — triggers clean shutdown of this context
is_forked        True for subagent contexts
parent_context_id back-reference for tracing
```

**ANILA-specific extension**: `AgentContext` carries `session_id` which the Router derives from the incoming CSP API Key + user identity headers (`X-ANILA-User-Id`). This links multi-turn conversations to the same user across Router restarts.

### Origin

`AgentContext` is a Python port of `claude-code-src/src/context.ts` (`AsyncLocalStorage`-based context). The key difference is that Python's `contextvars` module provides the same per-task isolation that Node.js `AsyncLocalStorage` provides.

---

## 3. Memory Compact / Context Budget

### 3.1 Budget Tracker

**File**: `src/anila_core/engine/budget_tracker.py`

Every query runs with a `BudgetTracker` that tracks:
- `current_tokens` — estimated tokens consumed this session
- `budget_tokens` — optional hard limit (set by Router or caller)
- `context_window` — provider's context window size

At each turn end, `check_token_budget()` returns a `ContinueDecision`:
- `CONTINUE` — within budget, proceed
- `COMPACT` — approaching context window limit, trigger compact before next turn
- `STOP` — hard budget exceeded, terminate

### 3.2 AutoCompact

**File**: `src/anila_core/compact/auto_compact.py`

Threshold formula (ported from `claude-code-src/src/utils/autoCompact.ts`):
```
effective_window = context_window - MAX_OUTPUT_TOKENS_FOR_SUMMARY (20 000)
threshold = effective_window - AUTOCOMPACT_BUFFER_TOKENS (13 000)

if rough_token_count(messages) >= threshold:
    trigger compaction
```

When triggered, a **forked background agent** reads the full conversation and writes a compressed summary. The original messages are replaced with the summary + injected session memory. Subsequent turns continue with reduced context.

Key design constraint: compact summaries **must preserve tool call/result pairs** — orphaned tool results cause provider errors. `micro_compact.py` handles incremental compaction of individual tool-heavy segments.

### 3.3 Session Memory (Persistent Across Compacts)

**File**: `src/anila_core/compact/session_memory.py`

Session memory is a markdown file that survives compact operations. It is updated by a background extraction agent that fires post-turn when:
1. First extraction: `accumulated_tokens >= MIN_TOKENS_TO_INIT` (10 000)
2. Subsequent: `delta_tokens >= MIN_TOKENS_BETWEEN_UPDATES` (5 000)

The session memory file is **injected into every new turn's context** (in the `pre_process` stage of `QueryEngine`), so even after a compact, the agent has access to key facts from earlier in the session.

### 3.4 Memory Directory (Long-term Persistent Memory)

**File**: `src/anila_core/memory/extract_memories.py`

A separate post-turn hook runs a restricted subagent to extract durable facts and write them to the memory directory (`memdir`). These persist across sessions. The subagent has access only to read tools + memory-dir write tools — it cannot modify the main conversation or trigger side effects.

**Relationship summary**:
```
turn history          → auto_compact (summarize when hitting context limit)
                      → session_memory (running notes, survives compact)
memory dir (memdir)   → long-term facts, injected at session start
```

### Origin

`auto_compact.py` and `session_memory.py` are Python ports of `claude-code-src/src/utils/autoCompact.ts` and `claude-code-src/src/utils/sessionMemory.ts`. The threshold constants are matched to the TypeScript originals. `memory/extract_memories.py` mirrors `claude-code-src/src/utils/extractMemories.ts`.

---

## 4. QueryEngine: The 7-Stage Turn Loop

**File**: `src/anila_core/engine/query_engine.py`

Every agent (Router, knowledge agent, workflow agent) runs through the same turn loop:

```
Stage 1: pre_process
  - Inject memory snapshot (memdir + session memory) into messages
  - Check compact threshold — trigger if needed
  - Append budget message if budget_tokens is set

Stage 2: api_call
  - Call provider (CSPPlatformProvider or direct)
  - Stream response chunks

Stage 3: completion_check
  - Inspect finish_reason: end_turn / tool_use / max_tokens / stop

Stage 4: tool_execution
  - Route tool calls through ToolRegistry.execute_batch()
  - Read-only tools: parallel
  - Write tools: sequential

Stage 5: attachments
  - Append tool results back to message history as ToolResult blocks

Stage 6: limit_check
  - Check max_turns, token budget, context window
  - Return early if limits reached

Stage 7: continue_or_stop
  - Ask BudgetTracker for ContinueDecision
  - Loop back to Stage 1 or return final TurnResult

Post-turn hooks (non-blocking, fire-and-forget):
  - memory_extraction_hook    → writes to memdir
  - session_memory_hook       → updates session memory file
  - auto_dream_check_hook     → placeholder for background reflection
```

**Usage in Router**: The Router's `router_server.py` runs `QueryEngine` with a `CoordinatorAgent` as the root agent. Its `ToolRegistry` includes `dispatch_to_agent` and `list_available_agents`. The Router never does RAG in Stage 1 — `rag_preprocessor.py` is disabled for Router mode.

---

## 5. Coordinator / Sub-agent / Task Decomposition

**File**: `src/anila_core/coordinator/coordinator.py`

### Responsibility boundary

| Layer | Responsibility |
|---|---|
| `QueryEngine` | Single agent's turn loop — one context, one provider, one tool set |
| `Coordinator` | Multi-agent orchestration — decomposes task, spawns workers, collects results |
| `AgentContext` | Isolation primitive — each worker gets a forked context |

### How it works

```
Coordinator receives user request
  │
  ├─ Calls main LLM to decompose into tasks
  │    Task list: [task_1: read-only, task_2: read-only, task_3: write]
  │
  ├─ Spawn workers for read-only tasks in parallel (asyncio.gather)
  │    Each worker: QueryEngine(config=worker_config, context=forked_context)
  │
  ├─ Execute write task sequentially after reads complete
  │
  └─ Synthesize results → final response to caller
```

Worker results are delivered back via `<task-notification>` XML blocks, which the coordinator's LLM can parse and synthesize. `SendMessage` resumes a completed worker (e.g., for follow-up clarification). `TaskStop` cancels a running worker via its `abort_signal`.

### Router's use of Coordinator

In Router mode, the `CoordinatorAgent` does **not** decompose into parallel tasks by default. Instead it runs in single-dispatch mode:
- Calls main LLM with list of available remote agents (`RemoteAgentRegistry`)
- LLM decides: answer directly OR call `dispatch_to_agent(agent_id, query)`
- `dispatch_to_agent` routes through CSP proxy (never directly to agent endpoint)

Multi-step routing (e.g., "query agent A first, then pass result to agent B") is future work — the coordinator is architecturally ready for it.

---

## 6. What Is Borrowed from claude-code-src vs. ANILA's Own Layer

### Borrowed (Python ports of TypeScript logic)

| ANILA module | claude-code-src equivalent | Notes |
|---|---|---|
| `context/agent_context.py` | `src/context.ts` (AsyncLocalStorage) | `contextvars.ContextVar` replaces AsyncLocalStorage |
| `compact/auto_compact.py` | `src/utils/autoCompact.ts` | Threshold constants matched |
| `compact/session_memory.py` | `src/utils/sessionMemory.ts` | Trigger thresholds matched |
| `memory/extract_memories.py` | `src/utils/extractMemories.ts` | Restricted tool set logic matched |
| `compact/sliding_window.py` | Sliding window context logic | Inline utility |
| `memory/memdir.py` | `src/memdir/` | Per-project memory directory |
| `coordinator/coordinator.py` | `src/coordinator/` + `tasks.ts` | Worker spawn, TaskStop, SendMessage protocol |
| `engine/budget_tracker.py` | `src/utils/costHook.ts` + token budget logic | Budget as ContinueDecision |
| `engine/query_engine.py` | `QueryEngine.ts` main agent loop | 7-stage loop aligned |

### ANILA's Own Layer (not in claude-code-src)

| ANILA module | What it does |
|---|---|
| `providers/cspplatform_provider.py` | All LLM calls route through CSP; API key = CSP API Key |
| `registry/remote_agent_manifest.py` | Fetches available agents from CSP `/v1/agents` with TTL cache |
| `tools/dispatch_tool.py` | `dispatch_to_agent()` calls CSP `/v1/chat/completions` with `model=<agent_id>` |
| `api/middleware/auth.py` (CspServiceTokenMiddleware) | Agents only accept requests from CSP (service-to-service token) |
| `api/router_server.py` | OpenAI-compatible entry point for the ANILA Core Router |
| 3-tier credential model | JWT (control), CSP API Key (data), service token (agent-to-agent) |
| `AgentContext.session_id` | Ties multi-turn sessions to CSP user identity headers |

### What ANILA deliberately does NOT borrow

- `claude-code-src` tool set (Bash, file read/write, etc.) — ANILA tools are domain-specific per agent
- `claude-code-src` MCP layer — ANILA uses `ToolRegistry` with registered Python callables
- Next.js / Ink terminal rendering — ANILA is a headless API service; UI is separate

---

## 7. Platform Runtime Concerns (not per-agent)

These are responsibilities of the Router or CSP, not individual agents:

| Concern | Owner | Current state |
|---|---|---|
| Usage accounting | CSP proxy (`proxy_service.py`) | ✅ Done |
| Token budget enforcement | `BudgetTracker` in QueryEngine | ✅ Done |
| Service credential injection | CSP proxy (`_build_downstream_headers`) | ✅ Done |
| Agent availability (TTL cache) | `RemoteAgentRegistry` | ✅ Done |
| Context compaction | `AutoCompact` in QueryEngine | ✅ Done |
| Session memory persistence | `SessionMemoryService` | ✅ Done |
| Agent health-check | Background loop in CSP | ⬜ Stub, needs Celery beat |
| Quota / rate-limit | Not yet implemented | ⬜ Phase 7 |
| Formal DB migration | startup backfill only | ⬜ Phase 7 (Alembic) |

---

## 8. Design Invariants

1. **No direct upstream calls**: Router and agents always use `CSPPlatformProvider` — upstream LLM keys are never held by Router or agents.
2. **No cross-context mutation**: Subagents receive a forked `AgentContext` copy. Writing to `messages` or `memory_snapshot` in a subagent does not affect the parent.
3. **Tool result pairing**: Every `ToolCall` in messages must have a matching `ToolResult`. The QueryEngine Stage 5 enforces this before appending to history.
4. **Compact before stop**: `BudgetTracker` returns `COMPACT` before `STOP`. Agents always attempt one compact before giving up on a session.
5. **Router does not pre-fetch RAG**: `rag_preprocessor` is disabled in Router mode. Knowledge retrieval only happens inside the dispatched knowledge agent.

# runtime_logic — agent runtime reference source snapshots

> A **design-reference directory** for agent runtimes: it holds source-tree snapshots of two production-grade runtimes that ANILA studies, borrows from, and ports (as re-written Python) into `anila-core/` and the agent template. **This is NOT executable code.**

> 繁體中文 primary: [`README.md`](./README.md)

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** Reference snapshots are gitignored; identical between branches.

---

## Overview / 簡介

This directory holds **source-tree snapshots of two production-grade agent runtimes**, used purely as design references:

- **Not ANILA's runtime code.** Runtime code lives in the `anila-core/` Python tree; the agent template lives in a separate agent project.
- The goal is to **study, borrow, and translate mature design patterns into Python**, then fold them into `anila-core/`.
- Rule: **read the pattern, learn the interface, re-write it yourself** — never copy verbatim. See each codebase's own LICENSE.

> ⚠️ **The full source trees of `claude-code-src/` and `openai-agents-python/` are excluded by `.gitignore`** (see the repo-root `.gitignore`, entries `runtime_logic/claude-code-src/`, `runtime_logic/openai-agents-python/`, etc.) and never enter the repo. The only tracked files are this README and its zh-TW counterpart. The source lives in local copies.

Together the two references cover two dimensions:

| Reference | Strengths | ANILA pain point addressed |
|---|---|---|
| **`claude-code-src/`** (TypeScript Claude Code CLI) | Long single-conversation turn-loop detail (three-tier compact, PTL retry, background memory extraction, stop hooks, tool-as-folder separation) | Already ported 7-stage QueryEngine, coordinator, three-tier compact, SessionMemory, Memdir 4-type into `anila-core/`; still pending: **PTL retry, preventContinuation hook, prompt-cache fork prefix** |
| **`openai-agents-python/`** (official OpenAI Agents SDK) | Multi-agent orchestration (handoffs / agents-as-tools / sub-agent state), tool guardrails, structured tracing, sandboxed tool execution, session abstraction, retry semantics, MCP integration | Today's RAG agent is single-agent + linear tool loop; the path toward "multi-agent handoff RAG", "sandboxed tool execution", "structured tracing" has a ready-made blueprint here |

"single-agent depth" + "multi-agent breadth".

---

## Contents / 內容

| Item | Description |
|---|---|
| `README.md` | zh-TW primary — reference purpose, dual-codebase overview, enhancement map, porting workflow, maintenance rules |
| `README.en.md` | This file (English mirror) |
| `claude-code-src/` (**gitignored**) | Local snapshot of the TypeScript Claude Code CLI source tree. Key submodules: `QueryEngine.ts` (7-stage turn loop), `query/` (config / deps / stopHooks / tokenBudget), `tools/` (43 tools, one folder each, incl. `AgentTool/`), `services/compact/` (micro / auto / sessionMemory), `services/extractMemories/`, `memdir/`, `coordinator/`, `hooks/toolPermission/`. `ink/`, `components/`, `screens/` are terminal UI (**do not port**). |
| `openai-agents-python/` (**gitignored**) | Local snapshot of the OpenAI Agents SDK source tree. Key submodules: `src/agents/handoffs/` (multi-agent control transfer), `tool_guardrails.py` / `guardrail.py`, `tracing/`, `sandbox/`, `memory/` (`session.py` / `sqlite_session.py` / `*_compaction_session.py`), `models/` (provider abstraction + retry), `mcp/`, `lifecycle.py`, `stream_events.py`, `run.py`. `realtime/` is the voice path (mostly not needed for RAG). |

> Both source trees exist only on the local machine (gitignored). `ls runtime_logic/` in the repo shows only the two tracked files: `README.md`, `README.en.md`. See the local copies for the full file structure.

---

## Purpose & audience / 用途與讀者

- **Audience**: engineers about to grow new agent-runtime capabilities on `anila-core/` or the RAG agent template.
- **When to read**: whenever adding a capability (multi-agent handoff, tracing, guardrails, session, PTL retry…), first consult the enhancement map below to find "which file in which reference already has the pattern", then read its contract and re-write it yourself in Python.
- **Not**: a deployment manifest, runbook, or importable package.

### Enhancement map (excerpt) / 強化對應地圖（節選）

#### Already landed (implemented in `anila-core`)

| Capability | Source reference | Current location |
|---|---|---|
| 7-stage turn loop | `claude-code-src/src/QueryEngine.ts` + `query/config.ts` | `anila-core/src/anila_core/engine/query_engine.py` |
| BudgetTracker + diminishing returns | `claude-code-src/src/query/tokenBudget.ts` | `anila-core/src/anila_core/engine/budget_tracker.py` |
| ExtractMemories + cursor | `claude-code-src/src/services/extractMemories/` | `anila-core/src/anila_core/memory/extract_memories.py` |
| AutoCompact / MicroCompact / SessionMemory | `claude-code-src/src/services/compact/` | `anila-core/src/anila_core/compact/` |
| Memdir 4-type taxonomy | `claude-code-src/src/memdir/memoryTypes.ts` | `anila-core/src/anila_core/memory/memdir.py` |
| Coordinator XML notifications | `claude-code-src/src/coordinator/coordinatorMode.ts` | `anila-core/src/anila_core/coordinator/coordinator.py` |

#### Enhancement backlog (by priority; ⭐ = high ROI)

| P | Capability | Reference location | Estimate |
|---|---|---|---|
| P0 ⭐ | Multi-agent handoff (retrieve → answer → cite-verify pipeline) | `openai-agents-python/src/agents/handoffs/` + `extensions/handoff_filters.py` | 3–5 d |
| P0 ⭐ | Tracing framework | `openai-agents-python/src/agents/tracing/` | 2 d |
| P0 ⭐ | Tool guardrails | `openai-agents-python/src/agents/tool_guardrails.py` + `guardrail.py` | 1.5 d |
| P0 | PTL (Prompt Too Long) retry | `claude-code-src/src/services/compact/compact.ts` (`truncateHeadForPTLRetry`, `MAX_PTL_RETRIES=3`) | 0.5 d |
| P0 | `stripImagesFromMessages` for compact | `claude-code-src/src/services/compact/compact.ts` | 0.3 d |
| P0 | `preventContinuation` stop hook semantic | `claude-code-src/src/query/stopHooks.ts` | 0.5 d |
| P1 | Session abstraction (auto-compact persistence) | `openai-agents-python/src/agents/memory/session.py` et al. | 2 d |
| P1 | MCP server as tool source | `openai-agents-python/src/agents/mcp/` | 2 d |
| P1 | AgentTool fork — byte-identical prefix (prompt-cache sharing) | `claude-code-src/src/tools/AgentTool/` | 1 d |
| P1 | Lifecycle hooks (`on_start`/`on_tool_start`/`on_handoff`/`on_end`) | `openai-agents-python/src/agents/lifecycle.py` | 1.5 d |
| P2 | Magic Docs / PromptSuggestion / AwaySummary / Realtime | `claude-code-src/src/services/*`, `openai-agents-python/src/agents/realtime/` | as needed |

#### Explicitly not portable

- `claude-code-src/src/ink/`, `components/`, `screens/`, `buddy/`, `voice/`, `keybindings/` — terminal UI / CLI bound.
- `openai-agents-python/src/agents/realtime/` — voice / WebSocket, out of scope for this project.

### Porting workflow / 移植工作流

1. **Consult the map** — "we want to add X" → scan the backlog table → find the reference module.
2. **Read the contract, not the implementation** — function signatures / type shapes / config options / error handling / module dependencies; do not translate logic verbatim, do not haul over internal helpers.
3. **Design ANILA's own version** — factor in pgvector / multi-tenant / Chinese corpus / on-prem constraints; references often over-design, so you can fit tighter.
4. **Write the Python version** — re-write it yourself, name it yourself, write your own docstrings; comments may cite the reference path as design provenance (e.g. `# Pattern from runtime_logic/openai-agents-python/src/agents/handoffs/`) but **must not quote concrete code**.
5. **Cross-reference verify** — for modules present in both (compact / memory), compare how each solves the same problem; the differences are the real design decisions.

---

## Setup & Run / 啟動與部署

**N/A (reference / design directory).** This directory has no `package.json` / `Dockerfile` / `pyproject.toml`; it cannot be built, deployed, or imported. It is a documentation directory for design reference and the porting workflow; the actual runnable code lives in `../anila-core/`.

---

## Related docs / 相關文件

- Platform overview: [`../README.md`](../README.md)
- Python runtime (porting destination): [`../anila-core/README.md`](../anila-core/README.md)
- `openai-agents-python` deep dive (12 subsystems dissected + P0–P2 starting files): [`../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md`](../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md)
- Agent framework architecture & porting decisions: [`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md), [`../docs/agent-framework/anila-agent-framework-porting-decisions.md`](../docs/agent-framework/anila-agent-framework-porting-decisions.md)
- Parent-child RAG design: [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- Service-token cutover plan: [`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)

> No separate deep dive is written for `claude-code-src` — that codebase ships its own complete `docs/` and `docs.zip` (offline snapshot); grep the local copy when needed.

---

## Maintenance / 維護規則

- **Adding a new reference codebase**: place under `runtime_logic/<name>/` → add `runtime_logic/<name>/` to `.gitignore` → add a section under "Contents" and the enhancement map in this README → commit only the README + `.gitignore`; the source tree never enters the repo.
- **Porting a pattern**: note the source reference path in the PR description; **do not quote** concrete code chunks; after porting, add a row to the "Already landed" table in this README.
- **Removing a reference**: confirm all desired patterns have landed → move the codebase to a local archive → update `.gitignore` and this README.

---

**Status**: both source trees are gitignored; the repo tracks only `README.md` and `README.en.md`.

**Last updated**: 2026-05-26 (sync PR #16 + add prod banner)

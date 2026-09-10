# runtime_logic — agent runtime reference source snapshots

> ⚠ 2026-08-17 盤點：本檔為【已被取代】——`runtime_logic/` 是 reference-only 參考快照(見 `AGENTS.md` §2),不可當 runtime import 或部署來源。現行狀態與執行順序見 `PLAN.md`。

> The agent runtime "design reference directory": holds source snapshots of two production-grade runtimes for ANILA to study, borrow from, and translate good design patterns into Python for `packages/anila-core/` and the agent template. **This is not executable code.**

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This directory exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`. **The `trial-military` slim build does not include it.** See the root [`README.md`](../README.md) branch matrix (current line is a single `main`; the old seven-branch model is retired).

---

## Overview

This directory holds source snapshots of two **production-grade agent runtimes** as design references:

- **Not ANILA's executable code.** The executable code is in `packages/anila-core/`'s Python tree; the agent template is in [`anila-agent`](../packages/anila-agent/).
- Purpose: **study, borrow, and translate mature design patterns into Python** for `packages/anila-core/`.
- Rule: **read the pattern, learn the interface, rewrite it yourself**; never copy verbatim. Licensing follows each codebase's own LICENSE.

> ⚠️ **The entire source trees of `claude-code-src/` and `openai-agents-python/` are excluded by `.gitignore`** and never enter the repo. The only tracked files are this README and its English mirror; the source lives locally.

The two references cover two dimensions:

| Reference | Strength | ANILA pain point it maps to |
|---|---|---|
| **`claude-code-src/`** (TypeScript Claude Code CLI) | Turn-loop details for a long single conversation (three-tier compact, PTL retry, background memory extraction, stop hooks, tool-as-folder) | Already ported: 7-stage QueryEngine, coordinator, three-tier compact, SessionMemory, Memdir; still to port: PTL retry, preventContinuation hook, prompt-cache fork prefix |
| **`openai-agents-python/`** (official OpenAI Agents SDK) | Multi-agent orchestration (handoffs / agents-as-tools / sub-agent state), tool guardrails, structured tracing, sandbox tool execution, session abstraction, MCP integration | Currently single-agent + linear tool loop; to move toward "multi-agent handoff RAG", "sandboxed tools", "structured tracing", this is the ready-made blueprint |

"single-agent depth" + "multi-agent breadth".

---

## Contents

| Item | Notes |
|---|---|
| `README.md` | this file (Traditional Chinese primary) |
| `README.en.md` | English mirror |
| `claude-code-src/` (**gitignored**) | local snapshot of the TS Claude Code CLI source. Key: `QueryEngine.ts` (7-stage), `query/`, `tools/` (43 tools, incl. `AgentTool/`), `services/compact/`, `services/extractMemories/`, `memdir/`, `coordinator/`, `hooks/toolPermission/`. `ink/` `components/` `screens/` (terminal UI) — **do not port**. |
| `openai-agents-python/` (**gitignored**) | local snapshot of the OpenAI Agents SDK source. Key: `handoffs/`, `tool_guardrails.py` / `guardrail.py`, `tracing/`, `sandbox/`, `memory/`, `models/`, `mcp/`, `lifecycle.py`, `run.py`. `realtime/` (voice) is mostly not needed. |

> Both source trees exist locally only (gitignored). `ls runtime_logic/` in the repo shows just `README.md` and `README.en.md`.

---

## Purpose & audience

- **Audience**: engineers adding new agent-runtime capabilities to `packages/anila-core/` or the RAG agent template.
- **When to read**: before adding a capability (multi-agent handoff, tracing, guardrails, session, PTL retry…), check the "enhancement map" below for "which reference's which file has a ready-made pattern", then read the contract and rewrite in Python.
- **Not**: a deployment list, an ops manual, or an importable package.

### Enhancement map (excerpt)

#### Landed (already in `anila-core`)

| Capability | Source reference | Current location |
|---|---|---|
| 7-stage turn loop | `claude-code-src/src/QueryEngine.ts` + `query/config.ts` | `packages/anila-core/.../engine/query_engine.py` |
| BudgetTracker + diminishing returns | `claude-code-src/src/query/tokenBudget.ts` | `.../engine/budget_tracker.py` |
| ExtractMemories + cursor | `claude-code-src/src/services/extractMemories/` | `.../memory/extract_memories.py` |
| AutoCompact / MicroCompact / SessionMemory | `claude-code-src/src/services/compact/` | `.../compact/` |
| Memdir 4-type taxonomy | `claude-code-src/src/memdir/memoryTypes.ts` | `.../memory/memdir.py` |
| Coordinator XML notifications | `claude-code-src/src/coordinator/coordinatorMode.ts` | `.../coordinator/coordinator.py` |

#### Enhancement backlog (⭐ = high ROI)

| P | Capability | Reference location | Est. |
|---|---|---|---|
| P0 ⭐ | Multi-agent handoff | `openai-agents-python/src/agents/handoffs/` + `extensions/handoff_filters.py` | 3–5 d |
| P0 ⭐ | Tracing framework | `openai-agents-python/src/agents/tracing/` | 2 d |
| P0 ⭐ | Tool guardrails | `openai-agents-python/src/agents/tool_guardrails.py` + `guardrail.py` | 1.5 d |
| P0 | PTL (Prompt Too Long) retry | `claude-code-src/src/services/compact/compact.ts` | 0.5 d |
| P0 | `stripImagesFromMessages` for compact | same | 0.3 d |
| P0 | `preventContinuation` stop hook | `claude-code-src/src/query/stopHooks.ts` | 0.5 d |
| P1 | Session abstraction (auto-compact persistence) | `openai-agents-python/src/agents/memory/session.py` | 2 d |
| P1 | MCP server as a tool source | `openai-agents-python/src/agents/mcp/` | 2 d |
| P1 | AgentTool fork — byte-identical prefix (prompt-cache sharing) | `claude-code-src/src/tools/AgentTool/` | 1 d |
| P1 | Lifecycle hooks | `openai-agents-python/src/agents/lifecycle.py` | 1.5 d |

#### Explicitly not for porting

- `claude-code-src/src/{ink,components,screens,buddy,voice,keybindings}/` — terminal UI / CLI bound.
- `openai-agents-python/src/agents/realtime/` — voice / WebSocket, out of scope.

### Porting workflow

1. **Check the map** — "we want to add X" → consult the backlog → find the reference module.
2. **Read the contract, not the implementation** — signatures / types / config / error handling / dependencies; do not translate logic verbatim.
3. **Design ANILA's own version** — account for pgvector / multi-tenant / Chinese corpus / on-prem constraints.
4. **Write the Python version** — rewrite, name, and docstring it yourself; comments may cite a reference path as design provenance but **never quote concrete code**.
5. **Cross-reference verify** — for modules present in both, compare how each handles the same problem; the difference is the real design decision.

---

## Setup & Run

**N/A (reference / design directory).** No `package.json` / `Dockerfile` / `pyproject.toml`; nothing to build, deploy, or import. The actual executable code is in [`../packages/anila-core/`](../packages/anila-core/).

---

## Related docs

- Platform: [`../README.md`](../README.md) · current `main` (old seven-branch model retired)
- Python runtime (porting destination): [`../packages/anila-core/README.md`](../packages/anila-core/README.md)
- `openai-agents-python` deep-dive: [`../docs/archive/agent-framework/runtime-logic-openai-agents-deep-dive.md`](../docs/archive/agent-framework/runtime-logic-openai-agents-deep-dive.md)
- Agent framework architecture & porting decisions: [`../docs/archive/agent-framework/anila-agent-framework-architecture.md`](../docs/archive/agent-framework/anila-agent-framework-architecture.md), [`../docs/archive/agent-framework/anila-agent-framework-porting-decisions.md`](../docs/archive/agent-framework/anila-agent-framework-porting-decisions.md)

---

## Maintenance

- **Add a new reference codebase**: place under `runtime_logic/<name>/` → add `runtime_logic/<name>/` to `.gitignore` → add a section to this README → commit only README + `.gitignore`; the source tree never enters the repo.
- **Port a pattern**: note the source reference path in the PR description; **do not quote** concrete code chunks; add a row to the "Landed" table when done.
- **Remove a reference**: confirm every wanted pattern has landed → move the codebase to a local archive → update `.gitignore` and this README.

---

**Status**: both source trees gitignored; the repo tracks only `README.md` and `README.en.md`.

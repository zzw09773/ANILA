# Institutional AgenticRAG Template

> **關係定位**：本目錄是 [`../../`](../../) 底下的一個進階 sub-template。當你 fork 完
> [官方 AgenticRAG template](../../) 作為起點後，如果你的場景屬於下列高信任、
> 高問責情境（國防 / 金融 / 醫療 / 政府 / 法律），可以把這份 spec 的角色定義
> （coordinator / mission-analyst / rag-researcher / document-drafter / reviewer）
> 用 `agentic_rag.registry` 載入，組成多 agent 編排。
>
> 本 README 描述 **多 agent 編排的設計目標與 agent YAML 格式**，不是 quickstart。
> 要上手 RAG agent template 本身，先看 [`../../README.md`](../../README.md)。

This template is intended for teams building Agentic RAG systems or multi-agent
assistants in a high-trust, high-accountability environment.

It is designed for scenarios such as:

- Document processing across multiple internal units
- Technical knowledge retrieval and drafting
- Multi-agent engineering support for complex defense projects
- Human-in-the-loop planning and review for high-risk tasks

## Why This Template Exists

The current `AgenticRAG` codebase already has useful building blocks:

- `QueryEngine` for multi-turn tool loops
- `ToolRegistry` for tool contracts
- `AgentDefinition.model` for per-agent model override
- `Coordinator` and `AgentContext` for subagent-style execution

But it also has technical debt that makes scale-out risky:

- Two runtime truths: `src/agentic_rag` and root `api.py`
- Module-level mutable state in API modules
- No formal separation between main model, advisor model, and worker model
- No stable contract for "model as tool"
- Partial event schema that is not fully emitted at runtime

This template is the target shape for future refactors.

## Core Design Principles

### 1. One Runtime Truth

There should be one authoritative runtime path for:

- model calling
- retrieval
- tool execution
- event emission
- task persistence

If an OpenWebUI proxy is needed, keep it thin. Do not duplicate RAG logic,
system prompt assembly, hybrid retrieval, or model transport behavior in a
separate root script.

### 2. Separate Capability From Policy

The system should distinguish:

- what a model or agent can do
- what it is allowed to do in a specific environment

This means agent definitions, tool allowlists, permission mode, and approval
rules must stay explicit. Do not hide policy in prompt text alone.

### 3. Separate Model Roles

At minimum, define four model roles:

- `main_model`: strong coordinator and final synthesizer
- `advisor_model`: stronger reviewer or second-opinion tool
- `worker_default_model`: cheaper model for delegated sub-tasks
- `embedding_model`: retrieval-only model

Do not keep a single undifferentiated `MODEL` value once the system starts
using multiple agent roles.

### 4. Start With Advisor Before Full Worker Swarms

The first multi-model pattern to add should be a server-side review tool:

- `advisor_review`

This is lower risk than full async workers because it does not require:

- task persistence
- mailbox / resume semantics
- background lifecycle management
- cross-agent transcript handling

Only after `advisor_review` is stable should the system add:

- `spawn_agent`
- `send_agent_message`
- `stop_agent`

### 5. Human Approval For High-Risk Boundaries

In this environment, the system should never silently finalize high-impact
outputs. Human confirmation should be required before:

- releasing formal documents outside the drafting boundary
- publishing technical conclusions that affect procurement or design
- generating or approving operational recommendations
- executing external side effects

### 6. Auditability Over Cleverness

Every important action should be reconstructable:

- which model generated it
- which sources were consulted
- which tools were called
- which subagent performed work
- what final human-approved output was used

### 7. Goal-Driven Prompting

Adopt the behavioral discipline captured in `andrej-karpathy-skills`:

- think before coding
- simplicity first
- surgical changes
- goal-driven execution

For agent systems, this becomes:

- do not silently assume task meaning
- do not spawn workers for trivial work
- do not over-build abstractions before real need
- define success criteria and verification before delegation

## Recommended Reference Architecture

### Layer 1: Main Coordinator

The user-facing agent should be a strong model with responsibility for:

- understanding intent
- deciding whether retrieval is needed
- deciding whether review is needed
- deciding whether worker delegation is needed
- producing the final user-visible answer

It should not be overloaded with raw retrieval logic or low-level tool output.

### Layer 2: Direct Tools

These are low-latency tools the coordinator can call synchronously:

- `vector_search`
- `keyword_search`
- `read_document`
- `advisor_review`

These tools should return structured, bounded outputs.

### Layer 3: Worker Agents

Workers are only justified when a task is:

- multi-step
- parallelizable
- noisy enough to pollute the main context
- worth isolating by model or permission profile

Target agent contracts:

- `spawn_agent`
- `send_agent_message`
- `stop_agent`

Each worker must have:

- an explicit `agent_type`
- a clear tool scope
- an optional model override
- task state persistence
- resumable identity

## Target Tool Contracts

These are the recommended future contracts, even if the current codebase does
not yet implement all of them.

### Retrieval Tools

- `vector_search(query, top_k=5)`
- `keyword_search(query, top_k=5)`
- `read_document(document_id)`

### Model Review Tool

- `advisor_review(task, context_hint="", output_format="text")`

Expected behavior:

- runs on `advisor_model`
- receives the relevant conversation or synthesized context
- returns critique, risk list, or structured verdict
- has no external side effects

### Worker Control Tools

- `spawn_agent(agent_type, prompt, model_override="", max_turns=0)`
- `send_agent_message(task_id, message)`
- `stop_agent(task_id)`

Expected behavior:

- workers must be resumable
- workers must emit task notifications
- workers must carry their own usage and duration metrics

## Example Institutional Workflows

### A. Multi-Unit Document Processing

Recommended flow:

1. `main_model` interprets the request and identifies the unit context.
2. Retrieval tools collect relevant regulations, prior documents, and templates.
3. `document-drafter` produces a draft.
4. `reviewer` checks traceability, ambiguity, and unsupported claims.
5. Human approves the final outgoing version.

Why this works:

- drafting and review are separated
- source traceability stays explicit
- a final human gate remains intact

### B. UAV Swarm Project Support

Recommended flow:

1. `main_model` decomposes the task into research, constraint review, and synthesis.
2. `rag-researcher` retrieves doctrine, prior experiments, interface specs, and design notes.
3. `mission-analyst` examines constraints, assumptions, and conflict points.
4. `reviewer` or `advisor_review` challenges weak assumptions and missing evidence.
5. Human experts approve any design or operational output.

Why this works:

- no single model is trusted as the only reasoning path
- evidence gathering is separated from recommendation synthesis
- human approval stays at the command boundary

## Technical Debt Remediation Checklist For Current AgenticRAG

Use this checklist before attempting large-scale multi-agent expansion.

1. Merge root `api.py` behavior back into `src/agentic_rag`.
2. Move mutable API globals into injected services or app state.
3. Introduce formal model-role config instead of a single main model field.
4. Add `advisor_review` before implementing full worker tools.
5. Persist task state instead of relying on in-memory status tables.
6. Emit the full SSE lifecycle actually declared in `api/events.py`.
7. Make local test startup reproducible without hidden `PYTHONPATH` assumptions.
8. Decouple optional upload dependencies from import-time API module loading.

## Files In This Template

- `model-roles.example.yaml`
- `agents/coordinator.yaml`
- `agents/rag-researcher.yaml`
- `agents/document-drafter.yaml`
- `agents/reviewer.yaml`
- `agents/mission-analyst.yaml`

Use them as starting points, not as final production policy.

## Suggested Implementation Order

### Phase 1

- unify runtime
- add model role config
- add `advisor_review`

### Phase 2

- add persisted task state
- add `spawn_agent`
- add `send_agent_message`
- add `stop_agent`

### Phase 3

- add domain-specific tools
- add compliance policies
- add richer audit and replay support

## Practical Rule For Teams

If a colleague cannot answer these five questions from the system design, the
architecture is still too implicit:

- Which model is the main coordinator?
- Which model acts as reviewer?
- Which tasks may be delegated?
- Which outputs require human approval?
- Where is the single runtime truth?


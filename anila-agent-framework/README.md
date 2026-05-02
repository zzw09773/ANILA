# anila-agent-framework

> **Status**: v0.1.0-alpha — skeleton in place, foundation only. See `docs/` upstream for sprint plan.

A provider-agnostic agent runtime. Lifecycle hooks, guardrails, handoffs, session
protocol, tracing, MCP integration. Selectively ported from
[openai-agents-python](https://github.com/openai/openai-agents-python) (MIT) and
de-coupled from the OpenAI Responses API so any LLM provider plugs in.

## Why this exists

ANILA's two main consumers — `anila-core` (the platform brain) and AgenticRAG
(the developer template) — both want the same agent primitives but neither
should be coupled to the other. This package extracts those primitives into a
PyPI release both can `pip install` independently. AgenticRAG depending on
this package is no different from depending on `httpx` — it does not break
the fork-template promise (no anila-core imports leak in).

## Status — v0.1.0-alpha

This is a **skeleton release**. The full Sprint 1 deliverable (Agent class,
items, tools, runner, OpenAI-compat provider) is in progress. What lands here
first is a stable foundation:

- `anila_agent.exceptions` — generic exception hierarchy
- `anila_agent.usage` — provider-agnostic token / request usage tracking
- `anila_agent.providers.protocol` — `LLMProvider` Protocol skeleton

Heavier subsystems (lifecycle, guardrails, handoffs, tracing, etc.) ship in
later sprints. See the porting plan upstream:

- `docs/anila-agent-framework-porting-decisions.md`
- `docs/agenticrag-phase1-plan.md`

## Install

```bash
pip install -e .                    # framework only (no provider impls)
pip install -e '.[openai]'          # bundled OpenAI-compat provider
pip install -e '.[dev]'             # tests, linting, etc.
```

## Provenance

Files marked `# Originally <module> from openai-agents-python (MIT)` are
adapted from the upstream OpenAI Agents SDK. The original `LICENSE` is at
`runtime_logic/openai-agents-python/LICENSE` in this repo.

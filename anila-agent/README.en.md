# anila-agent

> Agentic RAG starter built on the **OpenAI Agents SDK v0.17.5**, hardened for
> ANILA's air-gapped intranet. Clone, plug in your retriever and prompt, and run.

[繁體中文](README.md) · English · Full rebuild blueprint: [REBUILD_PLAN.md](REBUILD_PLAN.md).

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** anila-agent is a sub-agent template — not coupled to prod deployment mode. Contents identical to main.

## Design highlights

- **Base = openai-agents 0.17.5** (pinned). Provider-agnostic, points at a local vLLM / OpenAI-compatible endpoint; default LLM `gpt-oss-20b`.
- **Air-gapped by construction**: force Chat Completions (vLLM has no Responses API), disable the tracing exporter (no egress to platform.openai.com), no external CDNs, placeholder api_key, optional self-signed TLS, and a compat layer that strips the `strict` tool field at the wire level (self-hosted endpoints reject it).
- **Reasoning-model guards**: intranet LLMs are reasoning models — `max_tokens` floored to ≥512; structured output strips ```json fences and fails closed. Structured side-queries use `response_format: json_object` (not the unreliable json_schema path).
- **Thin, not heavy**: the SDK now ships Sessions/MCP/guardrails/retry/HITL natively; custom code is only the differentiators.

## Features

| Layer | What |
|-------|------|
| Retrieval | Retriever Protocol + 4 backends (dummy / ANILA-native pgvector / generic pgvector / CSP HTTP) with env auto-select precedence |
| Tool policy | **deny-all default** + explicit read-only allow; capability map + fail-closed startup guard (wired into the SDK tool-guardrail) |
| Short-term memory | Native Session (SQLite default / Postgres optional / summarizing compaction) |
| Long-term memory | **memdir**: typed taxonomy + always-on index + hybrid recall (embed shortlist + LLM select, fail-closed) + redacted auto-extract + freshness tags |
| HITL | RunState serialize/resume (schema 1.10) |
| Multi-agent | `/deep-research` (planner → parallel retrieve → writer) |
| CLI | Streaming REPL + slash commands + switchable output styles |
| Extensions | SKILL.md skills, event triggers, MCP client (config-gated) |
| Serving | OpenAI-compatible service wrapper (CSP-dispatchable, service-token auth) |
| Observability | RunHooks audit + token/cost metering (caveat when self-hosted price unknown) |

## Quickstart

```bash
git clone <repo> && cd anila-agent
make install
cp .env.example .env       # set ANILA_BASE_URL / ANILA_MODEL
make test
make run
```

Minimal `.env`:

```ini
ANILA_BASE_URL=http://gpt-oss-20b:8000/v1
ANILA_MODEL=gpt-oss-20b
ANILA_API_KEY=EMPTY
ANILA_SSL_VERIFY=1     # set 0 for self-signed intranet certs
```

With no retriever configured it uses the built-in `DummyRetriever` (in-memory, zero infra).

## Air-gapped offline install

`openai-agents` pulls a full dependency tree. For a true air-gap, use the offline
wheelhouse: see [offline/README.md](offline/README.md).

## Status

P0–P5 complete and verified end-to-end against local gpt-oss-20b / NV-embed-V2
(memdir hybrid recall, deny-all policy, multi-turn session, deep-research, service
wrapper). See [REBUILD_PLAN.md](REBUILD_PLAN.md).

## License

Apache-2.0

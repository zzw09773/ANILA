# anila-agent

> ANILA's official **air-gapped Agentic RAG starter**, built on the **OpenAI Agents SDK v0.17.5** and
> hardened for the NCSIST air-gapped intranet. Clone, plug in your retriever and prompt, and run.
> It is the reference runtime the CSP **Agent Registry** approves, with native Full Trace built in.

[繁體中文](README.md) · English · Full rebuild blueprint: [REBUILD_PLAN.md](REBUILD_PLAN.md).

## Where it sits in the redesign

- **Location**: monorepo `packages/anila-agent/` (§17.1 layout: `services/` · `apps/` · `packages/` · `infra/`).
- **Standalone & portable**: a single dependency tree rooted at `openai-agents==0.17.5`, with **no dependency
  on `anila-core`** by design; the whole package can be dropped onto an MLSteam Lab or any intranet host.
- **Role in the platform**: approved by the **Agent Registry** in `services/csp` (the CSP governance service)
  and registered via the developer wizard in `apps/csp-governance-ui` (the governance UI). Its native Full
  Trace directly satisfies the `pending_trace_test` gate of the 7-state approval flow.
- **Deployment-mode agnostic**: login/deploy deltas live on branches; the template is identical across them.
  It is not part of the platform compose — the CSP Router dispatches to it as an agent endpoint.

## Design highlights

- **Base = openai-agents 0.17.5** (pinned). Provider-agnostic, points at a local vLLM / OpenAI-compatible
  endpoint (on the intranet, the Model Gateway at `.12`); default LLM `gpt-oss-20b`.
- **Air-gapped by construction**: force Chat Completions (vLLM has no Responses API), disable the tracing
  exporter (no egress to platform.openai.com), no external CDNs, placeholder api_key, optional self-signed
  TLS, and a wire-level compat layer that strips the `strict` tool field (self-hosted endpoints reject it).
- **Reasoning-model guards**: intranet LLMs are reasoning models — `max_tokens` floored to ≥512; structured
  output strips ```json fences and fails closed; structured side-queries use `response_format: json_object`
  (not the unreliable json_schema path).
- **Thin, not heavy**: the SDK ships Sessions/MCP/guardrails/retry/HITL natively; custom code is only the
  differentiators.

## Features

| Layer | What |
|-------|------|
| Retrieval | Retriever Protocol + 4 backends (dummy / ANILA-native pgvector / generic pgvector / CSP HTTP) with a fixed env auto-select precedence (`csp_http` > `anila_pgvector` > `pgvector` > `dummy`) |
| Tool policy | **deny-all default** + explicit read-only allow; capability map + fail-closed startup guard (wired into the SDK tool-guardrail) |
| Short-term memory | Native Session (SQLite default / Postgres optional / summarizing compaction) |
| Long-term memory | **memdir**: typed taxonomy + always-on index + hybrid recall (embed shortlist + LLM select, fail-closed) + redacted auto-extract + freshness tags |
| HITL | RunState serialize/resume |
| Multi-agent | `/deep-research` (planner → parallel retrieve → writer) |
| CLI | Streaming REPL + slash commands + switchable output styles |
| Extensions | `SKILL.md` skills, event triggers, MCP client (config-gated) |
| Serving | OpenAI-compatible service wrapper (CSP-dispatchable, `csk-` service-token auth) |
| Observability | RunHooks audit + token/cost metering + **native Full Trace** (see below) |

## Quickstart

```bash
git clone <repo> && cd packages/anila-agent

make install                 # create .venv and editable install (incl. dev)
cp .env.example .env         # set ANILA_BASE_URL / ANILA_MODEL
make test                    # 198 unit tests (offline)
make lint                    # ruff (anila_agent + tests)
make run                     # start the interactive CLI
```

Minimal `.env` (current intranet: gpt-oss-20b):

```ini
ANILA_BASE_URL=http://gpt-oss-20b:8000/v1
ANILA_MODEL=gpt-oss-20b
ANILA_API_KEY=EMPTY
ANILA_SSL_VERIFY=1     # set 0 for self-signed intranet certs
```

With no retriever configured it uses the built-in `DummyRetriever` (in-memory, zero infra) so you can chat
and verify connectivity immediately.

Enable differentiators (opt-in):

```ini
ANILA_MEMORY=1                 # long-term memory memdir (needs an embed endpoint)
ANILA_EMBED_BASE_URL=http://nv-embed-proxy:8000/v1
ANILA_CITED=1                  # inline source citations
ANILA_OUTPUT_STYLE=zh-tw-formal
```

CLI commands: `/help`, `/memory [query]`, `/style`, `/clear`, `/deep-research <question>`, plus a
`/<name>` per file in `configs/commands/*.md` (example: `/summarize`).

## Serving (CSP dispatch)

```bash
make install            # already includes [serving]
make serve              # python app.py (loads .env, then serves on :8200)
# or explicitly:
uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200
```

The service wrapper exposes 3 endpoints: `GET /health`, `GET /v1/models` (manifest, tagged
`model_type=agent`), and `POST /v1/chat/completions` (main entry, streaming supported). This `host:port`
is the agent endpoint you register with the CSP.

Auth uses the doc-08 `csk-` single key: the CSP Router dispatches with `X-CSP-Service-Token` (`csk-`) and
**only after verifying it** trusts `X-ANILA-User-Id/-Email/-Groups` (the Router does not forward the user
JWT; verifying `Authorization: Bearer <jwt>` would be wrong). With `CSP_SERVICE_TOKEN` unset it fails closed
and rejects (for local testing set `ANILA_ALLOW_NO_SERVICE_TOKEN=1`).

## Full Trace (doc-05 §6 / doc-06 §6, L3 approval blocker)

The template ships a native `anila_agent/tracing.py` demonstrating the full span set the CSP requires, so
**derived agents can copy it verbatim**. It auto-enables when a CSP dispatch carries `X-ANILA-Trace-Id`:
`agent.run/step/model_call/tool_call/retrieval/output/error` spans are batched (≤256/batch) and
callback-shipped to `POST {CSP}/v1/traces/{trace_id}/spans` (CSP returns 202), reusing the agent's own
`csk-` (the same key used for RAG egress). **No trace header or no endpoint → fully disabled, zero egress,
zero behavior change**; ship failures are always drop-and-logged and never crash the agent.

Three wiring pieces — reuse them as-is when you swap in your own tools/retriever, no core changes needed:

- **`TracingRunHooks`**: wraps `AuditHooks`; openai-agents' `on_agent_*` / `on_llm_*` / `on_tool_*` events
  auto-map to step/model_call/tool_call spans, so adding a `@function_tool` needs no extra code.
- **`TracingRetriever`**: wraps any retriever and emits an `agent.retrieval` span around `search()` (with
  `collection_ids` / `chunk_ids` / `document_ids` / `top_k`).
- **`TraceEmitter`**: the buffering, batching emitter; `async with emitter.span(...)` adds custom sub-spans
  that auto-nest under the current span (concurrency-isolated via `contextvars`).

Env: `ANILA_TRACE_ENDPOINT` (default = `CSP_BASE_URL`), `ANILA_TRACE_ENABLED` (default 1),
`ANILA_CLASSIFICATION_LEVEL` (five-level classification, carried on run/output spans to satisfy the
classification item of the doc-06 §8 trace-test); `X-ANILA-Task-Id` is also carried on the run span to
attribute back to a Task in the Task Center.

> **Non-anila-agent runtimes** (LangChain / custom HTTP) can join the same pipeline via the copy-paste
> `AnilaTraceAdapter` in [`examples/trace-adapters/`](../../examples/trace-adapters/README.md).

## Registration (CSP Agent Registry)

Running the template is only step one; to enter real tasks an agent must pass the Agent Registry's
**7-state approval** (`draft` → `pending_connection_test` → `pending_trace_test` → `pending_security_review`
→ `approved`, plus `rejected` / `disabled`). Two registration paths:

- **Wizard**: the two-step wizard at `/developer/agents` in the governance UI `apps/csp-governance-ui` —
  fill endpoint / runtime type / classification ceiling → issue a `csk-` → test-connection → trace-test.
- **CLI**: `anila-core register` (reads `anila.yaml` → `POST /api/agents/register`), with
  `--base-model` (base model NAME, resolved to an id by CSP) / `--base-model-id` /
  `--runtime-type` / `--classification-level` / `--version` flags. `--draft` and
  `--classification-ceiling` were removed — the server discarded both.

## Docker / MLSteam image

Alternative path into an air-gapped intranet: build an "environment" image (packages + JupyterLab, no
source), upload it to MLSteam and let it spin up a Lab; the source is cloned into the workspace.
`make docker-build` / `make docker-save` (writes a tar) / `make docker-run` (JupyterLab locally on :8888).
Full flow: see [DOCKER.md](DOCKER.md).

## Air-gapped offline install

`openai-agents` pulls a full dependency tree. For a true air-gap, use the offline wheelhouse
(`pip download` → `--no-index` install): see [offline/README.md](offline/README.md).

## Status

P0–P5 complete and verified end-to-end against local gpt-oss-20b / NV-embed-V2 (memdir hybrid recall,
deny-all policy, multi-turn session, deep-research, service wrapper, Full Trace). `make test` collects
**198** unit tests (plus 1 `live`-marked test that needs a real endpoint, 199 total). See
[REBUILD_PLAN.md](REBUILD_PLAN.md).

## License

Apache-2.0

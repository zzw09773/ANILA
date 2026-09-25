# anila-agent — advanced implementation example

> This project illustrates how to combine OpenAI Agents SDK v0.17.5 with tools, retrieval, memory, and a serving wrapper. It is not the shortest onboarding path: use the separately downloadable quickstart scaffold for a first agent. Air-gapped deployment still requires prepared dependencies, model access, and appropriate trust configuration.

[繁體中文](README.md) · English · Full rebuild blueprint: [REBUILD_PLAN.md](REBUILD_PLAN.md).

## Where it sits in the redesign

- **Location**: monorepo `packages/anila-agent/` (§17.1 layout: `services/` · `apps/` · `packages/` · `infra/`).
- **Dependencies and deployment**: this advanced example depends on both `openai-agents==0.17.5` and `anila-core>=0.14,<0.15`. Standalone deployment requires compatible offline wheels, trust configuration, and a working model endpoint; this is not a zero-dependency scaffold.
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
| Serving | OpenAI-compatible service wrapper (CSP-dispatchable; verify platform dispatch JWT / JWKS) |
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

> ⚠ **Where `ANILA_EMBED_BASE_URL` points decides whether the query/document
> split does anything.** Memory shortlisting and pgvector retrieval send an
> `input_type` field (`query` / `document`) — a Triton-class embedder puts the
> two on **different input tensors**, and getting it wrong does not error, it
> just quietly degrades ranking. The `nv-embed-proxy:8000` above is the **model
> container**: its shim does not declare that field and pydantic defaults to
> `extra="ignore"`, so the field is **neither rejected nor read** and the query
> is still embedded as a document. To actually get the split, point this at
> **CSP**'s `/v1` (e.g. `https://<platform>/v1`) and register the embedder with
> `protocol=triton_grpc`. See `docs/FAKE-CONTROLS.md` #35.

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

Auth is the **dispatch JWT**: the CSP Router sends `Authorization: Bearer <JWT>`
(5 minutes, RS256); the agent verifies it against the platform JWKS
(`/.well-known/jwks.json`). Claims include `user_id` / `department` / `agent_id`.
Outbound RAG search and trace reuse **that same** dispatch JWT. There is no `csk-`
onboarding and no `CSP_SERVICE_TOKEN` credential to collect. Point `ANILA_CA_FILE` at
the platform CA PEM — **do not** set `SSL_CERT_FILE`. The quickstart zip already
contains `anila_verify.py` and `ca.pem`. The governance center can serve them again
(`GET /api/agents/anila-verify/download`, `GET /api/agents/platform-ca/download`);
a 503 means this deployment is missing the file (contact ops).

## Trace spans (not an approval gate)

The template includes `anila_agent/tracing.py`. When a dispatch carries
`X-ANILA-Trace-Id`, spans can be batched outbound under the same dispatch JWT,
not `CSP_SERVICE_TOKEN`. There is no seven-state approval and no trace-test gate.
No trace header or no endpoint → disabled, zero egress; ship failures are
drop-and-logged and never crash the agent.

Three wiring pieces — reuse them as-is when you swap in your own tools/retriever, no core changes needed:

- **`TracingRunHooks`**: wraps `AuditHooks`; openai-agents' `on_agent_*` / `on_llm_*` / `on_tool_*` events
  auto-map to step/model_call/tool_call spans, so adding a `@function_tool` needs no extra code.
- **`TracingRetriever`**: wraps any retriever and emits an `agent.retrieval` span around `search()` (with
  `collection_ids` / `chunk_ids` / `document_ids` / `top_k`).
- **`TraceEmitter`**: the buffering, batching emitter; `async with emitter.span(...)` adds custom sub-spans
  that auto-nest under the current span (concurrency-isolated via `contextvars`).

Env: `CSP_BASE_URL` and `ANILA_CA_FILE` (trust anchor). Optional trace switches:
`ANILA_TRACE_ENDPOINT` (default = `CSP_BASE_URL`) and `ANILA_TRACE_ENABLED` (default 1).
`ANILA_CLASSIFICATION_LEVEL` (`無機密` / `營業秘密` / `密` / `機密`) may ride on a span.
Do not set `CSP_SERVICE_TOKEN` as an onboarding credential.

> **Non-anila-agent runtimes** (LangChain / custom HTTP) can join the same pipeline via the copy-paste
> `AnilaTraceAdapter` in [`examples/trace-adapters/`](../../examples/trace-adapters/README.md).

## Registration (CSP Agent Registry)

Approval is three states: `registered` / `approved` / `disabled`. There is no
seven-state machine, no trace-test gate, and no `anila-core register` CLI.
Register in the governance center at `/developer/agents` (name, endpoint,
description) or `POST /api/agents/register`. No long-lived secret is issued;
the platform signs a 5-minute JWT on each dispatch.

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

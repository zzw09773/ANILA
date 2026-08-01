# anila-core

> **ANILA Core** — Python agent runtime foundation (SDK). The in-process runtime base shared by every ANILA agent and the Router, plus the shared infrastructure used across the whole backend fleet.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This SDK exists on every ANILA deployment branch and is identical across branches (the runtime base does not vary by deployment context). New features always land in `main` first, then sync downstream. See the root [`README.md`](../../README.md) and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

`anila-core` is ANILA's **Python runtime base**, positioned as pure runtime — not tied to a specific RAG pipeline, vector store, or model provider. It carries two pillars:

- **Pillar 1 · Agent runtime** — the in-process components the Router and each agent service need to handle a single chat turn (QueryEngine, Coordinator, providers, memory, tools, context, tracing, API server…).
- **Pillar 2 · Shared infrastructure** — base components not specific to one agent process, shared across the whole backend fleet (Router, agents, ingestion-worker, future batch workers): credential crypto, SSRF url_guard, pg / pgvector adapters, chunking plugins, ingestion error taxonomy.

How each role relates to anila-core:

- **Router deployment** ([`anila-core-router`](../../services/anila-core-router/)): directly `import`s Pillar 1 + Pillar 2.
- **Agent developers**: `anila-core init` produces a non-RAG starter, or `pip install "anila-core[rag]"` and fork [`anila-agent`](../anila-agent/) as the official RAG agent starter template. The `[rag]` extra provides the heavyweight document-parsing packages.
- **ingestion-worker** (Arq async pipeline): consumes only Pillar 2 (`chunking_plugins`, `IngestionError`, `pg_pool`, `pgvector_store`, `credential_crypto`); never touches Pillar 1.

> **Post-redesign repo layout (§17.1)**: the monorepo uses four tiers — `services/` (deployable services, incl. `csp` / `anila-core-router`), `apps/` (frontends), `packages/` (importable packages; this SDK lives here), `infra/` (compose / deploy scripts / nginx / models). The root [`compose.yaml`](../../compose.yaml) is a shim → `include: infra/compose/platform.yml`; deploy scripts live under `infra/deployment/{scripts,intranet}/`. Repo-root positioning: [`../../README.md`](../../README.md).

---

## Architecture & Stack

- **Language**: Python `>=3.11`
- **Build**: `hatchling` (package source `src/anila_core`)
- **Package / version**: `anila-core` v0.14.0 ([`pyproject.toml`](./pyproject.toml) is authoritative for version & extras)

### Core dependencies (`pyproject.toml`)

| Package | Use |
|------|------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + `Settings` (env loading; field name maps directly to the env var) |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server (Router / agent server) |
| `httpx>=0.27` | provider upstream calls + trace export |
| `sse-starlette>=2.0` | streaming (SSE turn streaming) |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / config loading |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 `CollectionScopedPgVectorStore` (central ingestion vector store) |
| `cryptography>=42` | `security.credential_crypto` (AES-GCM + PBKDF2, encrypts `user_llm_credentials`) |
| `aiosqlite>=0.20` | default `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — heavyweight parsing stack: `pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`. The heavyweight parser / vision implementations live in `anila_core.ingestion.parser_registry` and `anila_core.providers.vision`; `anila-agent/` is a pure starter template (no production-code dependency).
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`.

### Console script

```
anila-core = anila_core.cli.main:main   # init / register / status / agent bootstrap (legacy; superseded by P2.1 dispatch JWT)
```

---

## Layout

Key modules under `src/anila_core/` (grouped by pillar):

```
packages/anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md
├── CHANGELOG.md              # detailed sprint release notes (latest entry v0.13.0)
├── e2e_smoke.py              # manual e2e smoke (needs OPENAI_API_KEY)
├── examples/                 # router-mode / simple-agent
├── tests/                    # pytest (testpaths=["tests"]; integration/ subpackage needs live pgvector+RLS)
└── src/anila_core/
    ├── config.py             # Settings (pydantic-settings; CSP_BASE_URL / MODEL / API_DEV_MODE …)
    ├── app_factory.py        # FastAPI app factory
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server (create_router_app) + events
    │   ├── session_owner.py · caller_context.py   # resume session→agent table + CallerContext (reads X-ANILA-Task-Id)
    │   └── middleware/
    │       ├── auth.py            # LEGACY: CSP service-token + rotating token (old path; new agents must not use)
    │       ├── dispatch_auth.py   # P2.1 dispatch-JWT middleware (JWKS verify, fail-closed)
    │       ├── dispatch_jwt.py    # P2.1 JWT parse / verify helpers
    │       └── jwks_client.py     # P2.1 JWKS fetch / cache
    ├── engine/               # query_engine (multi-stage turn loop) + budget_tracker
    │                         #   + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router (ToolRegistry, plan-mode / permission gate)
    ├── tools/                # dispatch · ask_user · plan_mode · todo_write
    │                         #   · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider · vision · mock · embedding_mock
    ├── memory/               # short_term/ (Session Protocol + in_memory / sqlite)
    │                         # long_term/ (adapter · embedding · extraction · backends/{filesystem,postgres})
    │                         # + memdir · consolidation · relevance_selector · user
    ├── compact/              # micro / auto / session_memory / sliding_window
    ├── context/              # AgentContext (turn-scope contextvars, incl. classified_latch)
    ├── post_turn/            # prompt_suggestion (follow-up chips)
    ├── tracing/              # span · tracer · processor · hooks
    │                         #   + sdk (anila_trace_sdk: TraceExporter / TraceSession / ExportingProcessor / SPAN_TYPES)
    ├── workspace/            # capability-scoped sandbox (workspace + caps + safe_path)
    ├── registry/             # agent_registry + remote_agent_manifest (fetches from CSP /v1/agents)
    ├── runtime_config/       # snapshot · poller · apply (hot-reload)
    ├── models/               # pydantic DTOs
    ├── cli/                  # init / register / status / bootstrap (legacy) + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # credential_crypto (AES-GCM + PBKDF2) + url_guard (SSRF, incl. endpoint_kind split)
        ├── storage/          # ports.py (Protocol) + adapters/ (pg_pool · pgvector_store · memory_file_store)
        └── ingestion/        # errors (IngestionError taxonomy) · parser_registry · parsers · docling_parser
            │                 #   · ocr · citation_extractor · relation_resolution
            └── chunking_plugins/  # base · registry (@register_chunker) · builtins
```

> Full module responsibilities & boundaries: [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md).

---

## Redesign capability mapping (what anila-core owns)

Most of the platform's Slice 0–9 capabilities live in CSP / the frontends; anila-core provides only the **runtime producer surface**. Design authority is [`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (constitution `00`; runtime/registry domain doc `05`; frozen contracts `09`).

| Capability | What anila-core owns | Code / doc |
|---|---|---|
| **Full Trace** (spans + `/v1/traces` ingest) | `anila_trace_sdk` producer: batch-export spans to the CSP endpoint and mirror them into the `anila.spans` SSE event | `tracing/sdk.py`; doc `05` §6 / `09` §10 |
| **Task spine** (`X-ANILA-Task-Id`) | the runtime reads it via `CallerContext` and threads the task-id through the turn | `api/caller_context.py` |
| **Five-level classification + one-way latch** | the agent runtime honours the per-turn classified one-way latch (`ctx.classified_latch` → `anila_meta.classified`); `register` carries `--classification-level` (written to `default_classification_level`). **Latch enforcement / declassification authority is CSP** | `context/agent_context.py`; doc `08` |
| **Agent Registry** (OE-1 three states: registered / approved / disabled) | `register` / `status` CLI submit into the CSP registry; the base model may be given by NAME (`base_model`) and CSP resolves it to an id. **The state machine lives in CSP** | `cli/register_cmd.py`; doc `05` |
| **Model Gateway** (`ANILA_ENV` http fail-closed) | `url_guard` hard-rejects http for `endpoint_kind='model'` in production (no flag can rescue it). **Per-model keys / 5-state health live in CSP** | `security/url_guard.py`; doc `04` §8 |

---

## Setup & Run

anila-core is a **library / SDK**, not a long-running service. It's consumed by import (Router / agents / ingestion-worker) and exposes a FastAPI app via `create_router_app()` / `create_app()` for uvicorn.

### Install & test

```bash
pip install -e "./packages/anila-core"          # full Pillar 1 + Pillar 2 core deps
pip install -e "./packages/anila-core[rag]"     # + heavyweight parsing stack
pip install -e "./packages/anila-core[rag,dev]" # + pytest / ruff / mypy (to run the full suite)

cd packages/anila-core
.venv/bin/python -m pytest            # asyncio_mode=auto; testpaths=["tests"]
.venv/bin/python -m pytest -m integration   # needs a live pgvector + RLS database
```

### Router mode (OpenAI-compatible dispatcher)

```python
# main.py
from anila_core.api.router_server import create_router_app
app = create_router_app()
```

```bash
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000
```

### Drive one QueryEngine turn directly

```python
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.providers.openai_compat import OpenAICompatProvider
from anila_core.router.tool_router import ToolRegistry
from anila_core.models.message import StreamDelta, UserMessage

provider = OpenAICompatProvider(base_url="http://csp:8000/v1", api_key="sk-...")
engine = QueryEngine(provider=provider, tool_registry=ToolRegistry(), config=QueryConfig())

async def on_delta(delta: StreamDelta) -> None:
    if delta.type == "text" and delta.text:
        print(delta.text, end="", flush=True)

# run() drives the full turn loop and returns a TurnResult; stream deltas arrive via the on_stream_delta callback
result = await engine.run([UserMessage(content="say hi")], on_stream_delta=on_delta)
print(result.stop_reason, result.turn_count)
```

> ⚠️ QueryEngine has **no** `run_stream()`; the entrypoint is `await engine.run(messages, on_stream_delta=...)` (see [`e2e_smoke.py`](./e2e_smoke.py)).

### Full Trace export (opt-in)

Tracing is **additive and fail-open**: with `ANILA_TRACE_ENDPOINT` unset the whole trace path is a no-op and behaviour is byte-identical to before it was wired. When set, spans are POSTed by a background `TraceExporter` to CSP `POST {base}/v1/traces/{trace_id}/spans` (body `{"spans":[…]}`, ≤256/batch, authenticated with `X-CSP-Service-Token`), AND mirrored into the `anila.spans` SSE event.

- `ANILA_TRACE_ENDPOINT`: a bare flag (`1`/`true`/`on`/`yes`/`default`) → reuse the router's known `CSP_BASE_URL`; any other value → an explicit trace base URL.
- `ANILA_TRACE_TOKEN`: service token for the router's / platform-internal s2s trace export (falls back to `CSP_SERVICE_TOKEN` when unset). This is the credential for **anila-core's `TraceExporter` (used by the Router)** — not agent dispatch identity. If unset, the exporter sends **no** auth header and spans are silently drop-and-logged. Third-party agent in-task callbacks may use a dispatch JWT on the CSP side; that path is separate from this exporter.

Three pieces in code: `TraceExporter` (thread-safe, batching, bounded queue, drop-and-log), `TraceSession` (per-`trace_id` span factory; `span()` / `async_span()` context managers auto-time / mark ok/error / auto-parent), `ExportingProcessor` (bridges the in-tree `Tracer`/`Span` onto the exporter, mapping `SpanKind` → doc `05` §6 span-types). All exported from `anila_core.tracing`.

### Scaffold a new agent + register

```bash
anila-core init my-agent      # scaffolds a non-RAG starter from cli/templates/agent-template
anila-core register \
  --csp http://localhost:8000 --endpoint http://your-host:9100 \
  --base-model gemma4 \
  --runtime-type anila_agent --classification-level 機密 \
  --version 1.0.0
```

`register` reads `anila.yaml`, logs into CSP with JWT, then `POST /api/agents/register`. Each flag overrides the manifest and is validated against a closed set:

| Flag | Notes |
|---|---|
| `--base-model` | **required (or `base_model` in `anila.yaml`)**: the base model NAME. CSP resolves the name to an id, so a developer never has to copy a numeric database id out of the governance UI |
| `--base-model-id` | only needed when two models share a display name: pass the numeric id directly |
| `--runtime-type` | 5 values (doc `05` §3): `anila_agent` / `langchain` / `openwebui_pipe_compatible` / `openai_compatible_agent` / `custom_http` |
| `--classification-level` | four levels (doc `08`): `無機密` / `營業秘密` / `密` / `機密`. Written to `default_classification_level` |
| `--version` | agent version string (e.g. `1.0.0`) |

> `--draft` (shadow registration) and `--classification-ceiling` were removed. Since OE-1
> `approval_status` has three states (registered / approved / disabled) — there is no draft —
> and agents have no classification ceiling; what is stored and enforced is
> `default_classification_level`. Both flags only ever sent a field the server discarded.

---

## Security: outbound URL guard (SSRF)

`security.url_guard.validate_outbound_url(url, endpoint_kind="generic")` is the central allow-list for user-supplied endpoint URLs (validated once by CSP at credential create and again by the worker at call time — defense in depth). **Slice 6a** domain-splits the http-relaxation flag by `endpoint_kind` (scheme only; host / IP / DNS / trusted-host checks are identical across kinds):

- **`model`** — rejects http by default; admitted only via an explicit `ANILA_ALLOW_HTTP_ENDPOINT=1` (PLAN.md P0.2, decided 2026-07-29: uniform across production and dev, superseding the original doc `04` §8 hard rule).
- **`agent`** — http is allowed via `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1` (for on-prem MLSteam plain-http NodePort agents); legacy `ANILA_ALLOW_HTTP_ENDPOINT` remains a deprecated fallback.
- **`generic`** (default) — original global semantics; `ANILA_ALLOW_HTTP_ENDPOINT` relaxes it; existing callers are unaffected.

Fixed host rules: deny list (loopback / `169.254.169.254` metadata / mDNS), internal-zone suffixes (`.internal` / `.local` / `.svc` …), always-unsafe IPs (loopback / link-local / multicast / reserved — hard reject), RFC 1918 private ranges (allowed only under `ANILA_ALLOW_PRIVATE_ENDPOINT=1`), single-label hostnames rejected. `ANILA_TRUSTED_HOSTS` (env) ∪ DB-backed providers form an admin allow-list that bypasses host checks (scheme is still validated). Failures raise `UnsafeEndpointError` (with `reason` / `fixable_by_trust_host`).

---

## Integration

| Consumer | Scope | What it gets |
|--------|----------|----------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`, QueryEngine, Coordinator, `RemoteAgentRegistry`, dispatch-JWT / JWKS middleware, trace SDK |
| **anila-agent template** (fork point) | Pillar 1 + Pillar 2 + `[rag]` | full runtime + parsing / vision provider |
| **ingestion-worker** (Arq + Redis) | Pillar 2 only | `chunking_plugins`, `IngestionError`, `pg_pool`, `CollectionScopedPgVectorStore`, `credential_crypto` |
| **services/csp** (CSP backend) | Pillar 2 (partial) | `credential_crypto` (encrypts `user_llm_credentials`), `url_guard` (SSRF) and other shared primitives |

---

## Related docs

- Redesign design authority: [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) — constitution [`00`](../../docs/anila-redesign-docs/00-product-constitution.md), runtime/registry protocol [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md), API/event contracts [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md), classification latch [`08`](../../docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md), Model Gateway [`04`](../../docs/anila-redesign-docs/04-model-gateway-design.md)
- anila-core boundary: [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md) · runtime design: [`../../docs/anila-core/anila-core-runtime-design.md`](../../docs/anila-core/anila-core-runtime-design.md)
- Ingestion platform design: [`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md) · Release notes: [`CHANGELOG.md`](./CHANGELOG.md)
- RAG agent template: [`../anila-agent/README.md`](../anila-agent/README.md) · Router shell: [`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)
- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

> Version authority is `pyproject.toml` (v0.14.0); the latest `CHANGELOG.md` entry is v0.13.0. ⚠️ Known code inconsistency (not a README issue): `src/anila_core/__init__.py` still hard-codes `__version__ = "0.7.0"`, so reading `anila_core.__version__` programmatically returns the stale value.

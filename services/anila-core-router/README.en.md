# anila-core-router

> **ANILA Router** — an OpenAI-compatible automatic request dispatcher. A thin **deployment unit** (`main.py`); the actual dispatch logic lives in the [`anila-core`](../../packages/anila-core/README.md) SDK's `anila_core.api.router_server`. In the stack it appears as the `router` service.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This service exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../../README.md) and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

The Router exposes an OpenAI-compatible `POST /v1/chat/completions` and advertises a pseudo-model `anila-router`. Behaviour (confirmed against `main.py` and the SDK `router_server`):

- A client sets the request body `model` to `anila-router` to route traffic here. In practice `chat_completions` (`router_server.py`) **does not inspect the `model` field**: regardless of its value, every request runs the full dispatch flow. `anila-router` is just an advertised pseudo-model (`GET /v1/models`).
- **Agent discovery has exactly one source**: the Router's `RemoteAgentRegistry` pulls the agent manifest from CSP `GET /v1/agents` and TTL-caches it — there is no local registry and no direct upstream. It then calls the primary routing LLM with the caller's API key; that LLM decides whether to dispatch to an agent (e.g. an `image-generator` drawing agent).
- On dispatch the request is forwarded to that agent's `endpoint_url`; the agent's SSE stream is forwarded chunk by chunk to the caller.
- The primary routing model is decided by CSP at runtime: `main.py` pulls the current primary LLM from CSP `GET /api/models/router-primary` every 60s. When CSP has no primary set, the middleware blocks `/v1/chat/completions` with a **503**, avoiding a silent fall-back to the wrong upstream.

> Positioning: the Router is a **deployment example** of "assemble a dispatcher from the anila-core runtime foundation", not the core itself. CSP ([`services/csp`](../csp/), formerly `myCSPPlatform`) is the platform's authoritative control + data plane.

---

## Architecture & Stack

| Item | Details (confirmed against Dockerfile + source) |
|---|---|
| Language | Python 3.11 (`python:3.11-slim`) |
| Web framework | FastAPI, app built by `anila_core.api.router_server.create_router_app()` |
| ASGI server | `uvicorn` (`uvicorn main:app --host 0.0.0.0 --port 9000`) |
| HTTP client | `httpx` (async, calls CSP / agents) |
| Core dependency | `anila-core` SDK (pure runtime, **without** `[rag]` extras) + `pydantic-settings` |
| Port | `9000` (compose only `expose`s it; no host port) |

`main.py` is more than a thin shell; beyond the app factory it also handles:

1. **Primary routing model TTL refresh** (`_refresh_primary` / `_ensure_primary`, `PRIMARY_TTL_SECONDS=60`) + the 503 gate middleware on `/v1/chat/completions`. No background timer: triggered once at startup, then lazily by the gate middleware when stale.
2. **Three-tier service-token resolution** (`_load_service_token` / `_self_bootstrap` / `_initialise_token_source`): precedence is **state file → `CSP_SERVICE_TOKEN` (legacy env) → `CSP_BOOTSTRAP_TOKEN` (only reached when both are empty)**; startup logs make explicit which path was taken. Note that `_self_bootstrap` is currently a **v1 pass-through**: it writes the env value into the state file (mode 0600) and does **no HTTP exchange**.
3. **Hot-reload of the state file once on CSP 401/403** then retry (zero downtime after an admin rotates the router-primary credential in CSP).

> `main.py` actively makes exactly one CSP call, `GET /api/models/router-primary` (with `X-CSP-Service-Token`); `GET /v1/agents`, `POST /v1/chat/completions`, and agent dispatch + SSE forward all live in the SDK `router_server.py`. The Router holds **no** user API key of its own: it calls back to the CSP data plane with the caller's (UI / OpenAI SDK) Bearer API key, so the agents a caller can see equal the agents the Router can dispatch to (no privilege amplification).

---

## Layout

```
services/anila-core-router/
├── main.py        # deployment entrypoint: create_router_app() + primary-model TTL refresh
│                  #   + 3-tier service-token state-file resolution + /router/primary-status debug endpoint
├── Dockerfile     # multi-stage; build context must be the repo root (COPYs packages/anila-core/)
└── README.md / README.en.md

# Actual dispatch logic lives in the anila-core SDK:
packages/anila-core/src/anila_core/api/router_server.py   # create_router_app() + dispatch / SSE forward + trace production
```

---

## Setup & Run

### Option 1: repo-root compose (recommended)

The Router image is built from this directory's `Dockerfile` and runs as service `router`. Both root compose files are shims:

```bash
# from repo root
docker compose -f compose.dev.yaml up -d router   # dev  → include infra/compose/dev.yml
docker compose -f compose.yaml     up -d router   # prod → include infra/compose/platform.yml
```

In compose `router` only uses `expose: 9000` (**no** host port); external traffic reaches it via nginx `/router/*` → `router:9000` (the UI's `VITE_ROUTER_BASE_URL` defaults to `/router`). The Router waits for `csp` to be healthy first.

### Option 2: build the image yourself (build context must be the repo root)

```bash
docker build -f services/anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 -e CSP_BASE_URL=http://csp:8000 -e CSP_SERVICE_TOKEN=dev-service-token anila-core-router
```

### Option 3: single-host uvicorn (dev)

```bash
pip install -e "../../packages/anila-contracts" \
  -e "../../packages/anila-security" \
  -e "../../packages/anila-core"                  # pure runtime, no RAG extras
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### Environment variables

Read by `main.py` (raw `os.environ`):

| Variable | Notes | Default |
|---|---|---|
| `CSP_BASE_URL` | CSP base URL; `http://csp:8000` inside containers | `http://csp:8000` |
| `CSP_BOOTSTRAP_TOKEN` | first-start bootstrap token; entrypoint writes it into the state file | `""` |
| `CSP_SERVICE_TOKEN` | legacy fleet-shared shared-secret; fallback when no state file | `""` |
| `ANILA_ROUTER_STATE_DIR` | directory persisting the service token (state file `service_token.json`, mode 0600) | `/var/lib/anila-router` |

The SDK (`router_server`) additionally reads the **Full Trace opt-in** env (doc `09` §10 frozen contract):

| Variable | Notes | Default |
|---|---|---|
| `ANILA_TRACE_ENDPOINT` | unset → the whole trace path is a no-op (byte-identical to before). A bare flag (`1`/`true`/`on`/`yes`/`default`) → reuse `CSP_BASE_URL`; any other value → an explicit trace base URL. Spans are POSTed to `POST {base}/v1/traces/{trace_id}/spans` and mirrored into the `anila.spans` SSE event | `""` (off) |
| `ANILA_TRACE_TOKEN` | service token for trace export; falls back to `CSP_SERVICE_TOKEN` | `""` |

> `main.py` **does NOT read `MODEL`** (the primary routing model is decided entirely by CSP `/api/models/router-primary` at runtime; the compose `router` service still carries `MODEL: ${LLM_MODEL:-gemma4}`, a vestigial env var with no effect).

---

## Integration

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents                  ──▶ CSP   fetch agent manifest (the only discovery source)
   ├── GET /api/models/router-primary  ──▶ CSP   fetch primary LLM (X-CSP-Service-Token)
   ├── POST /v1/chat/completions       ──▶ CSP   call primary LLM to decide dispatch
   ├── dispatch → agent endpoint_url    ──▶ e.g. image-generator → http://flux2-dev-agent:8000
   └── (optional) POST /v1/traces/{id}/spans ──▶ CSP  Full Trace export (when ANILA_TRACE_ENDPOINT is set)
```

- **CSP (`CSP_BASE_URL`)**: all upstream interactions go through CSP — fetch agent list, resolve primary model, call the primary LLM. Router→CSP internal endpoints authenticate with `X-CSP-Service-Token`.
- **Agents**: registered via CSP (e.g. `image-generator`, `endpoint_url: http://flux2-dev-agent:8000`). When the primary LLM decides, the Router dispatches and forwards the SSE stream.
- **`/router/primary-status`** (debug): returns the cached primary model name, last error, `service_token_source`, and the state-file path.

---

## Related docs

- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- Redesign design authority: constitution [`../../docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md) · runtime/registry protocol [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md) · API/event contracts (incl. SSE + `/v1/traces`) [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md)
- Multi-service integration (incl. Router role): [`../../docs/platform/multi-service-integration-plan.md`](../../docs/platform/multi-service-integration-plan.md)
- Agent framework architecture: [`../../docs/agent-framework/anila-agent-framework-architecture.md`](../../docs/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation (SDK): [`../../packages/anila-core/README.md`](../../packages/anila-core/README.md) · CSP: [`../csp/README.md`](../csp/README.md) · Shell: [`../../apps/anila-shell/README.md`](../../apps/anila-shell/README.md)

---

## License

See repo-root [`LICENSE`](../../LICENSE).

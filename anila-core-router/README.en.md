# anila-core-router

> **ANILA Router** — an OpenAI-compatible automatic request dispatcher. A thin deployment entrypoint (`main.py`); the actual dispatch logic lives in the [`anila-core`](../anila-core/README.md) SDK's `anila_core.api.router_server`. In the stack it appears as the `router` service.

> 中文為主版；中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This service exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md).

---

## Overview

The Router exposes an OpenAI-compatible `POST /v1/chat/completions` and provides a pseudo-model `anila-router`. Behaviour (confirmed against `main.py` and `router_server`):

- A client sets the request body `model` to `anila-router` to route traffic here. In practice `chat_completions` (`router_server.py`) **does not inspect the `model` field**: regardless of its value, every request runs the full dispatch flow (fetch agents → call the primary LLM → let that LLM decide whether to dispatch). `anila-router` is just an advertised pseudo-model (`GET /v1/models`).
- The Router pulls the agent manifest (incl. `requires_encryption`) from CSP `GET /v1/agents` and caches it, then calls the primary routing LLM with the caller's API key; that LLM decides whether to dispatch to an agent (e.g. an `image-generator` drawing agent).
- On dispatch the request is forwarded to that agent's `endpoint_url`; the agent's SSE stream is forwarded chunk by chunk to the caller.
- The primary routing model is decided by CSP at runtime: `main.py` pulls the current primary LLM from CSP `GET /api/models/router-primary` every 60s. When CSP has no primary set, the middleware blocks `/v1/chat/completions` with a **503**, avoiding a silent fall-back to the wrong upstream.

> Positioning: the Router is a deployment example of "assemble a dispatcher from the anila-core runtime foundation", not the core itself. CSP (myCSPPlatform) is the platform's authoritative control + data plane.

---

## Architecture & Stack

| Item | Details (confirmed against Dockerfile + source) |
|---|---|
| Language | Python 3.11 (`python:3.11-slim`) |
| Web framework | FastAPI, app built by `anila_core.api.router_server.create_router_app()` |
| ASGI server | `uvicorn` (`uvicorn main:app --host 0.0.0.0 --port 9000`) |
| HTTP client | `httpx` (async, calls CSP / agents) |
| Core dependency | `anila-core` SDK (pure runtime, **without** `[rag]` extras) + `pydantic-settings` |
| Port | `9000` |

`main.py` is more than a thin shell; beyond the app factory it also handles:

1. **Primary routing model TTL refresh** (`_refresh_primary` / `_ensure_primary`, 60s TTL) + the 503 gate middleware on `/v1/chat/completions`.
2. **Three-tier service-token resolution** (`_load_service_token` / `_self_bootstrap` / `_initialise_token_source`): the actual precedence is **state file → `CSP_SERVICE_TOKEN` (legacy env, inside `_load_service_token`) → `CSP_BOOTSTRAP_TOKEN` (bootstrap, only reached when both are empty)**; startup logs make explicit which path was taken. Note that in the current version `_self_bootstrap` is a **v1 pass-through**: it writes the env value into the state file (mode 0600) and does **no HTTP exchange**.
3. **Hot-reload of the state file once on CSP 401/403** then retry (zero downtime after an admin rotates the router-primary credential in CSP).

---

## Layout

```
anila-core-router/
├── main.py        # deployment entrypoint: create_router_app() + primary-model TTL refresh
│                  #   + 3-tier service-token state-file resolution + /router/primary-status debug endpoint
├── Dockerfile     # multi-stage; build context must be the repo root (COPYs anila-core/)
└── README.md / README.en.md

# Actual dispatch logic lives in the anila-core SDK:
anila-core/src/anila_core/api/router_server.py   # create_router_app() + dispatch / SSE forward
```

---

## Setup & Run

### Option 1: repo-root compose (recommended)

The Router image is built from this directory's `Dockerfile` and runs as service `router`:

```bash
# from repo root
docker compose -f docker-compose-dev.yml up -d router   # dev
docker compose -f docker-compose.yml     up -d router   # prod (repo-root default compose, also runs as service router)
```

In compose `router` only uses `expose: 9000` (**no** host port); the UI exposes it via the `/router` reverse proxy (see the UI's `VITE_ROUTER_BASE_URL` default `/router`). The Router waits for `csp` to be healthy first.

### Option 2: build the image yourself (build context must be the repo root)

```bash
docker build -f anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 -e CSP_BASE_URL=http://csp:8000 -e CSP_SERVICE_TOKEN=dev-service-token anila-core-router
```

### Option 3: single-host uvicorn (dev)

```bash
pip install -e "../anila-core"        # pure runtime, no RAG extras
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### Environment variables (confirmed against `main.py`)

| Variable | Notes | Default |
|---|---|---|
| `CSP_BASE_URL` | CSP base URL; `http://csp:8000` inside containers | `http://csp:8000` |
| `CSP_BOOTSTRAP_TOKEN` | first-start bootstrap token; entrypoint writes it into the state file | `""` |
| `CSP_SERVICE_TOKEN` | legacy fleet-shared shared-secret; fallback when no state file | `""` |
| `ANILA_ROUTER_STATE_DIR` | directory persisting the service token (state file `service_token.json`, mode 0600) | `/var/lib/anila-router` |

> `main.py` reads only the four env vars above and **does NOT read `MODEL`** (the primary routing model is decided entirely by CSP `/api/models/router-primary` at runtime; the compose `router` service still carries `MODEL: ${LLM_MODEL:-gemma4}`, but `main.py` never reads it — a vestigial env var with no effect). The primary model is refreshed lazily every **60s** (`PRIMARY_TTL_SECONDS=60`) with no background timer: triggered once at startup, then by the `/v1/chat/completions` gate middleware when stale. `main.py` makes exactly one CSP call, `GET /api/models/router-primary` (with `X-CSP-Service-Token`); `GET /v1/agents`, `POST /v1/chat/completions`, and agent dispatch + SSE forward all live in the SDK `router_server.py`.
>
> The Router holds **no** user API key of its own: it calls back to the CSP data plane with the caller's (UI / OpenAI SDK) Bearer API key, so the agents a caller can see equal the agents the Router can dispatch to.

---

## Integration

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents                  ──▶ CSP   fetch agent manifest
   ├── GET /api/models/router-primary  ──▶ CSP   fetch primary LLM (X-CSP-Service-Token)
   ├── POST /v1/chat/completions       ──▶ CSP   call primary LLM to decide dispatch
   └── dispatch → agent endpoint_url    ──▶ e.g. image-generator → http://flux2-dev-agent:8000
```

- **CSP (`CSP_BASE_URL`)**: all upstream interactions go through CSP — fetch agent list, resolve primary model, call the primary LLM. Router→CSP internal endpoints authenticate with `X-CSP-Service-Token`.
- **Agents**: registered via CSP (e.g. `image-generator`, `endpoint_url: http://flux2-dev-agent:8000`). When the primary LLM decides, the Router dispatches and forwards the SSE stream.
- **`/v1/agents` dispatch**: the agents a caller can dispatch to equal that API key's allowed agents in CSP — the Router never amplifies privilege.

---

## Related docs

- Platform: [`../README.md`](../README.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- Multi-service integration (incl. Router role): [`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- Agent framework architecture: [`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation (SDK): [`../anila-core/README.md`](../anila-core/README.md) · CSP: [`../myCSPPlatform/README.md`](../myCSPPlatform/README.md) · UI: [`../ANILA_UI/anila-ui/README.md`](../ANILA_UI/anila-ui/README.md)

---

## License

See repo-root [`LICENSE`](../LICENSE).

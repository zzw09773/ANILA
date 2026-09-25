# anila-core-router

> **ANILA Router** — an OpenAI-compatible automatic request dispatcher. A thin **deployment unit** (`main.py`); the actual dispatch logic lives in the [`anila-core`](../../packages/anila-core/README.md) SDK's `anila_core.api.router_server`. In the stack it appears as the `router` service.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This service exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../../README.md) (current line is a single `main`; the old seven-branch model is retired).

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

1. **Primary routing model TTL refresh** (`_refresh_primary` / `_ensure_primary`, `PRIMARY_TTL_SECONDS=60`) + the 503 gate middleware on `/v1/chat/completions`. The primary model has no background timer: one refresh at startup, then a lazy refresh from the gate when the TTL expires. The credential file has its own periodic re-read (default 30s, `ANILA_SERVICE_TOKEN_RELOAD_SECONDS`).
2. **Service-token resolution** (`_load_service_token` / `_initialise_token_source`): when `ANILA_SERVICE_TOKEN_FILE` is set, that file is the only credential. A missing file is `file_missing`; an unreadable or empty file is `file_error`. Neither falls back, and both are re-read on the timer so the Router recovers after CSP writes the file. State file, `CSP_BOOTSTRAP_TOKEN`, and `CSP_SERVICE_TOKEN` are used only when `ANILA_SERVICE_TOKEN_FILE` is **unset**. When `CSP_BOOTSTRAP_TOKEN` is set and the state file does not exist yet, the value is copied into the state file (mode 0600). That copy does **no HTTP exchange**. Startup logs the source name and never the plaintext. `/health` reports `token_source` as `file`, `file_missing`, `file_error`, `state_file`, `bootstrap`, `legacy_env`, or `none`.
3. **The credential file is re-read when it changes.** On CSP 401/403 the Router forces one more read and retries once, then gives up.

> `main.py` actively makes exactly one CSP call, `GET /api/models/router-primary` (with `X-CSP-Service-Token`); `GET /v1/agents`, `POST /v1/chat/completions`, and agent dispatch + SSE forward all live in the SDK `router_server.py`. The Router holds **no** user API key of its own: it calls back to the CSP data plane with the caller's (UI / OpenAI SDK) Bearer API key, so the agents a caller can see equal the agents the Router can dispatch to (no privilege amplification).

---

## Layout

```
services/anila-core-router/
├── main.py        # deployment entrypoint: create_router_app() + primary-model TTL refresh
│                  #   + credential-file/state-file token resolution + /router/primary-status debug endpoint
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
pip install -e "../../packages/anila-core"        # pure runtime, no RAG extras
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### Environment variables

Read by `main.py` (raw `os.environ`):

| Variable | Notes | Default |
|---|---|---|
| `CSP_BASE_URL` | CSP base URL; `http://csp:8000` inside containers | `http://csp:8000` |
| `ANILA_SERVICE_TOKEN_FILE` | Credential file written by CSP. Compose mounts `/run/anila/service-clients/router-primary.token` | unset |
| `ANILA_SERVICE_TOKEN_RELOAD_SECONDS` | How often to re-read the credential file. Floor is 5 seconds | `30` |
| `CSP_BOOTSTRAP_TOKEN` | Used only when `ANILA_SERVICE_TOKEN_FILE` is unset and the state file is empty. Copied into the state file | `""` |
| `CSP_SERVICE_TOKEN` | Last fallback, and only when the token-file path is unset: the old fleet secret. Router-only CSP endpoints reject it once `router-primary` has its own credential | `""` |
| `ANILA_ROUTER_STATE_DIR` | Directory for `service_token.json` (mode 0600). Not read when the token-file path is set | `/var/lib/anila-router` |

Span upload is removed. Do not set `ANILA_TRACE_ENDPOINT`. `tasks.trace_id` in CSP is still a correlation id.

> `main.py` **does NOT read `MODEL`**. The primary routing model is the Console `router_primary` role (`GET /api/models/router-primary` at runtime). Compose does not pass `LLM_MODEL`.

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
   └── dispatch → agent endpoint_url    ──▶ e.g. image-generator → http://flux2-dev-agent:8000
```

- **CSP (`CSP_BASE_URL`)**: all upstream interactions go through CSP — fetch agent list, resolve primary model, call the primary LLM. Router→CSP internal endpoints authenticate with `X-CSP-Service-Token`.
- **Agents**: registered via CSP (e.g. `image-generator`, `endpoint_url: http://flux2-dev-agent:8000`). When the primary LLM decides, the Router dispatches and forwards the SSE stream.
- **`/router/primary-status`** (debug): returns the cached primary model name, last error, `service_token_source`, and the state-file path.

---

## Related docs

- Platform: [`../../README.md`](../../README.md) · current `main` (old seven-branch model retired)
- Redesign design lineage (historical): constitution [`../../docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md) · runtime/registry protocol [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md) · API/event contracts [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md). Current authority: [`PLAN.md`](../../PLAN.md) (state + order of work); spec: [`SYSTEM-MAP.md`](../../SYSTEM-MAP.md). Span upload is not part of the current router.
- Multi-service integration (incl. Router role): [`../../docs/platform/multi-service-integration-plan.md`](../../docs/platform/multi-service-integration-plan.md)
- Agent framework architecture: [`../../docs/archive/agent-framework/anila-agent-framework-architecture.md`](../../docs/archive/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation (SDK): [`../../packages/anila-core/README.md`](../../packages/anila-core/README.md) · CSP: [`../csp/README.md`](../csp/README.md) · Shell: [`../../apps/anila-shell/README.md`](../../apps/anila-shell/README.md)

---

## License

See repo-root [`LICENSE`](../../LICENSE).

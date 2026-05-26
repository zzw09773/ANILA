# anila-core-router

**ANILA Router** — an OpenAI-compatible request dispatcher service. It is a thin deployment shell (`main.py`); the actual dispatch logic lives in the [`anila-core`](../anila-core/README.md) SDK (`anila_core.api.router_server`). In the dev stack it runs under the service name `router`.

> The Traditional Chinese version is primary: [README.md](./README.md). Technical terms, commands, and code stay in English in both versions.

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** Router contents identical to main (not in the fork zone); on prod, `CSP_SERVICE_TOKEN` is fail-loud (no dev fallback).

---

## Overview / 簡介

The Router exposes an OpenAI-compatible `POST /v1/chat/completions` plus a pseudo-model `anila-router`. Its behaviour (confirmed against `main.py` and `router_server`):

- A client sets the request body `model` to `anila-router` to trigger automatic dispatch; any other `model` value is forwarded straight to CSP without going through the dispatch logic.
- The Router pulls the agent manifest (including `requires_encryption`) from CSP's `GET /v1/agents` and caches it, then calls the primary routing LLM using the caller's API key so the primary LLM can decide whether to dispatch to an agent (e.g. the `image-generator` drawing agent registered in the dev stack).
- When dispatch is chosen, the request is forwarded to that agent's `endpoint_url`, and the agent's SSE stream is forwarded chunk-by-chunk back to the caller.
- The primary routing model is resolved by CSP at runtime: `main.py` fetches the currently designated primary LLM name from CSP `GET /api/models/router-primary` every 60 seconds. When CSP has no primary routing model configured, the middleware gates `/v1/chat/completions` with a **503** rather than silently falling back to the wrong upstream.

> Positioning: the Router is a deployment example of "assembling a dispatcher service on top of the ANILA Core runtime foundation", not core itself. CSP (myCSPPlatform) is the platform's authoritative control + data plane.

---

## Architecture & Stack / 架構與技術棧

| Item | Detail (confirmed from Dockerfile + source) |
|---|---|
| Language | Python 3.11 (`python:3.11-slim`) |
| Web framework | FastAPI; app built by `anila_core.api.router_server.create_router_app()` |
| ASGI server | `uvicorn` (`uvicorn main:app --host 0.0.0.0 --port 9000`) |
| HTTP client | `httpx` (async; calls CSP / agents) |
| Core dependency | `anila-core` SDK (pure runtime install, **no** `[rag]` extras) + `pydantic-settings` |
| Port | `9000` |

`main.py` is not a one-line shell. Beyond the app factory it also handles:

1. **Primary routing-model TTL refresh** (`_refresh_primary` / `_ensure_primary`, 60s TTL) plus the 503 gate middleware on `/v1/chat/completions`.
2. **Three-tier service-token resolution** (`_load_service_token` / `_self_bootstrap` / `_initialise_token_source`): state file → `CSP_BOOTSTRAP_TOKEN` auto-bootstrap → `CSP_SERVICE_TOKEN` legacy env; the startup log states which path was taken.
3. **Hot-reload the state file once on a CSP 401/403** then retry (zero downtime after an admin rotates the router-primary credential in CSP).

---

## Layout / 目錄結構

```
anila-core-router/
├── main.py        # Deployment entrypoint: create_router_app() + primary-model TTL refresh
│                  # + three-tier service-token state-file resolution + /router/primary-status debug endpoint
├── Dockerfile     # Multi-stage build; build context must be the repo root (it COPYs anila-core/)
├── README.md      # Traditional Chinese (primary)
└── README.en.md   # This file (English mirror)

# The actual dispatch logic lives in the anila-core SDK, not in this directory:
anila-core/src/anila_core/api/router_server.py   # create_router_app() + dispatch / SSE forward
```

---

## Setup & Run / 啟動與部署

### Option 1: repo-root dev compose (recommended)

The Router image is built from this directory's `Dockerfile` and runs under the service name `router` in `docker-compose-dev.yml`:

```yaml
# docker-compose-dev.yml (excerpt; treat the file as the source of truth)
router:
  build:
    context: .
    dockerfile: anila-core-router/Dockerfile
  expose:
    - "9000"
  environment:
    CSP_BASE_URL: http://csp:8000
    CSP_SERVICE_TOKEN: ${CSP_SERVICE_TOKEN:-dev-service-token}
    MODEL: ${LLM_MODEL:-gemma4}
  depends_on:
    csp:
      condition: service_healthy
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://localhost:9000/health || exit 1"]
```

```bash
# from the repo root
docker compose -f docker-compose-dev.yml up -d router
```

Note: in the dev compose, `router` only uses `expose: 9000` (there is **no** host port mapping); the UI exposes it via a `/router` reverse proxy (see the UI's `VITE_ROUTER_BASE_URL`, defaulting to `/router`). The Router starts only after `csp` is healthy.

### Option 2: build the image yourself

The build context must be the repo root (the Dockerfile `COPY`s `anila-core/`):

```bash
# from the repo root
docker build -f anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 \
  -e CSP_BASE_URL=http://csp:8000 \
  -e CSP_SERVICE_TOKEN=dev-service-token \
  anila-core-router
```

### Option 3: standalone uvicorn (development)

Install the `anila-core` SDK first (pure runtime, no RAG extras):

```bash
pip install -e "../anila-core"
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### Environment variables (confirmed from `main.py`)

| Variable | Description | Default |
|---|---|---|
| `CSP_BASE_URL` | CSP (myCSPPlatform) base URL; inside the network it is `http://csp:8000` | `http://csp:8000` |
| `CSP_BOOTSTRAP_TOKEN` | First-start bootstrap token; the entrypoint writes it into the state file | `""` |
| `CSP_SERVICE_TOKEN` | Legacy fleet-shared shared secret; fallback when the state file is absent | `""` |
| `ANILA_ROUTER_STATE_DIR` | Directory persisting the service token | `/var/lib/anila-router` |
| `MODEL` | (Deprecated) The Router now pulls the primary model from CSP `/api/models/router-primary` at runtime, overriding this on startup; kept for historical compatibility | — |

> The Router holds **no** user API key of its own: it uses the caller's (UI / OpenAI SDK) Bearer API key to call the CSP data plane, so the agents a caller can see and the agents the Router can dispatch to are both scoped to that API key's permissions.
> Only the service token (Router→CSP internal endpoints such as `/api/models/router-primary`) goes through the three-tier resolution above.

---

## Integration / 與其他服務的關係

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents               ──▶ CSP (CSP_BASE_URL)   fetch agent manifest
   ├── GET /api/models/router-primary ─▶ CSP   resolve primary routing LLM (X-CSP-Service-Token)
   ├── POST /v1/chat/completions    ──▶ CSP   call primary LLM to decide dispatch
   └── dispatch → agent endpoint_url ─▶ e.g. image-generator → http://flux2-dev-agent:8000
```

- **CSP (`CSP_BASE_URL`)**: all upstream interaction goes through CSP — fetching the agent list, resolving the primary routing model, and calling the primary LLM. Router→CSP internal endpoints authenticate with an `X-CSP-Service-Token` header (token source per the three-tier resolution).
- **Agents**: the dev stack registers `image-generator` via CSP (the FLUX.2-dev drawing agent, `endpoint_url: http://flux2-dev-agent:8000`). When the primary LLM decides a drawing is needed, the Router dispatches the request to that agent and forwards its SSE stream.
- **`/v1/agents` dispatch**: the agents a caller can dispatch to equal that caller's API key's allowed agents in CSP — the Router does not amplify permissions.

---

## Related docs / 相關文件

(All paths below are verified to exist.)

- Platform overview: [repo-root README](../README.md)
- Multi-service integration plan (covers the Router's role): [`docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- Agent framework architecture: [`docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)
- Agent runtime deep dive (covers Router interaction): [`docs/agent-framework/runtime-logic-openai-agents-deep-dive.md`](../docs/agent-framework/runtime-logic-openai-agents-deep-dive.md)
- Runtime foundation (SDK): [`anila-core/README.md`](../anila-core/README.md)
- CSP platform: [`myCSPPlatform/README.md`](../myCSPPlatform/README.md)
- UI: [`ANILA_UI/anila-ui/README.md`](../ANILA_UI/anila-ui/README.md)

> Note: the `AgenticRAG/README.md` link from the old README is no longer at the repo root (only `docs/agenticrag/` remains), so it is not listed here.

---

## License

See the repo-root [`LICENSE`](../LICENSE).

---

**Last updated**: 2026-05-26 (sync PR #16 + add prod banner; Router contents identical to main)

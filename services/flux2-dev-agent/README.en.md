# flux2-dev-agent

> **Image-generation agent shim** — wraps the raw `flux2-dev` inference backend as an OpenAI `/v1/chat/completions`-compatible agent. CSP registers it in the model registry as `model_type=agent` named `image-generator`; the Router dispatches here when it decides a user wants an image. Appears in the stack as the service `flux2-dev-agent` on the external network `anila-models-net`.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This service is built and served by the `anila-models` model stack (see [`infra/models/`](../../infra/models/README.en.md)) and exists on the ANILA deployment branches that use that stack. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## What it is

In the chat flow, when the Router detects the user wants an image it dispatches the `image-generator` agent, forwarding the OpenAI chat request (via CSP proxy) to this shim. The shim turns a casual request into a persisted image plus a markdown link:

1. **`prompt_translator.py`** — calls `gemma4` through the CSP proxy to rewrite casual Chinese into a FLUX-friendly English prompt. Any error (non-200, malformed, None) **falls back to the original text** rather than failing the whole request; with `enabled=False` it is a pure pass-through (kill switch).
2. **`flux_client.py`** — an async context manager that calls the FLUX backend's **OpenAI-compatible Images API** `POST {base}/v1/images/generations` (body `{model, prompt, n:1, size, response_format:"b64_json"}`; a base without `/v1` gets it appended; `Authorization: Bearer` only when `FLUX_API_KEY` is set) and decodes the first `b64_json` back to PNG bytes. Aspect ratios map to `size` via a built-in table (16:9→1792x1024, 1:1→1024x1024, …; unknown ratios fall back to `1024x1024`). Non-200 / no data / non-JSON → `FluxBackendError`.
3. **`image_store.py`** — validates PNG magic bytes, writes to the share volume with a `uuid4` name, returns a public URL.
4. **`chat_handler.py`** — chains the three above and assembles an OpenAI-shape response whose assistant content is `已為您繪製：\n\n![](url)`.

```
Router → CSP proxy → flux2-dev-agent  POST /v1/chat/completions
                       ├─ prompt_translator → CSP /v1/chat/completions (gemma4)   ← outbound Bearer
                       ├─ flux_client       → FLUX backend POST {base}/v1/images/generations (OpenAI-compatible)
                       ├─ image_store       → write /share/flux, return /uploads/flux/<uuid>.png
                       └─ chat_handler      → OpenAI response (markdown image link)
```

> `flux_client` only sends `{prompt, aspect_ratio}` (no seed / num_candidates), so each call takes back a single candidate at the agent's `DEFAULT_ASPECT_RATIO`; chat content is not parsed for an aspect ratio.

---

## Public endpoints

`build_app` takes the four collaborators as parameters (tests inject mocks); the module-level `app` is built from env and loaded via `uvicorn app.main:app`.

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/health` | GET | `{"status": "ok"}` |
| `/v1/models` | GET | returns `image-generator` (OpenAI list shape: `id`/`object`/`created`/`owned_by`, plus a `model_type:"agent"` marker matching the CSP registration) |
| `/v1/chat/completions` | POST | JSON or SSE (below) |

**Request schema** (`ChatCompletionRequest`): a standard OpenAI body (`model` + non-empty `messages`) plus the ANILA extensions `anila_session_id` and `anila_handoff` (accepted because CSP forwards the body verbatim; the handler actually uses only `last_user_text()` and `model`). Empty `messages` → `422`.

**JSON vs SSE**: `stream=false` (default) returns a single `chat.completion`; `stream=true` returns `text/event-stream` — emitting a role chunk, a content chunk (with the markdown image), a finish chunk, then `[DONE]`. The Router **expects SSE** when it dispatches a streaming request; returning JSON in stream mode makes the Router emit zero content chunks and the chat shows empty (guarded by a regression test).

**Errors**: if any of flux / translator / store raises, the endpoint uniformly converts it to `502` (`detail: "image generation failed"`) and does **not** leak the exception text into the response body (guarded by a regression test).

---

## Auth boundary (important)

- **This shim performs no inbound authentication / authorization.** All three endpoints are unauthenticated; the design assumes it is reachable only inside `anila-models-net` and **only from the CSP proxy** (no host port is exposed). This is an intranet / CSP-only trust model — do **not** expose this service directly.
- The only Bearer credential is **outbound**: `prompt_translator` sends `Authorization: Bearer <CSP_API_KEY>` when calling CSP `/v1/chat/completions` (translation).
- The Agent Registry ([doc 05](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)) 7-state approval and trace-test gate governance lives **in the CSP governance plane (治理中心)**, not in this shim; the shim is merely the governed, dispatched execution endpoint. In the seed, `image-generator` is registered with `approval_status` = `approved`.

---

## Environment variables

| Variable | Default (code) | Notes |
|----------|----------------|-------|
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | OpenAI-compatible Images API base URL (server root or with `/v1`; the client normalises the version segment) |
| `FLUX_MODEL` | `flux.2-dev` | `model` field of the Images API request |
| `FLUX_API_KEY` | `""` | when set, sends `Authorization: Bearer`; empty = no header (local flux2-dev needs none) |
| `CSP_BASE_URL` | `http://csp:8000` | translation callback target |
| `CSP_API_KEY` | `""` (models compose injects `INTERNAL_PLATFORM_API_KEY`, fail-loud) | empty → translation auto-disabled with a warning, FLUX gets raw text |
| `GEMMA_MODEL` | `gemma4` | translation LLM |
| `ENABLE_PROMPT_TRANSLATION` | `1` | effective only when `CSP_API_KEY` is non-empty |
| `SHARE_DIR` | `/share/flux` | PNG landing path (bind mount) |
| `PUBLIC_URL_PREFIX` | `/uploads/flux` | URL prefix returned to the frontend |
| `DEFAULT_ASPECT_RATIO` | `16:9` | fixed aspect ratio sent to flux2-dev |
| `FLUX_TIMEOUT_SECONDS` | `180` (models compose overrides to `240`) | backend timeout |

---

## Directory layout

```
services/flux2-dev-agent/
├── app/
│   ├── main.py              # build_app + /health + /v1/models + /v1/chat/completions (incl. SSE)
│   ├── schemas.py           # OpenAI chat shape + anila_session_id / anila_handoff extensions
│   ├── prompt_translator.py # gemma4 via CSP proxy; falls back to original on error
│   ├── flux_client.py       # calls the OpenAI-compatible /v1/images/generations, decodes first PNG
│   ├── image_store.py       # PNG validation + write share volume + public URL
│   └── chat_handler.py      # chains all four → OpenAI response
├── Dockerfile               # python:3.11-slim (no GPU; urllib healthcheck, image has no curl)
├── requirements.txt · pyproject.toml
└── tests/                   # 27 tests (pytest-asyncio + respx; asyncio_mode=auto)
```

---

## Local development & testing

Tests intercept HTTP with `respx` and inject collaborators with `AsyncMock` — **no GPU, no live backend**:

```bash
python -m venv .venv
.venv/bin/pip install -e 'services/flux2-dev-agent[test]'   # from repo root; or cd in and pip install -e '.[test]'
cd services/flux2-dev-agent && ../../.venv/bin/python -m pytest -q
# → 27 passed
```

> The `[test]` extra is `pytest` + `pytest-asyncio` + `respx`; `pyproject.toml` sets `asyncio_mode = "auto"`, so async tests need no per-test `@pytest.mark.asyncio`. This repo commits no `.venv`.

---

## Deployment

Built and served by the `anila-models` model stack (build context `../../services/flux2-dev-agent`); it is **not** part of the platform stack, and the two connect via the external network `anila-models-net`. Day-to-day operation is in [`infra/models/README.en.md`](../../infra/models/README.en.md).

- `python:3.11-slim`, no GPU; `depends_on: flux2-dev (service_healthy)`; only `expose: "8000"`, no host port.
- Healthcheck uses Python `urllib` (the slim image has no `curl`; a curl check would exit 127 and falsely report unhealthy).
- Share volume: the host's `share/uploads/flux` is bind-mounted to the container's `/share/flux`; nginx serves it under `location /uploads/` (see `infra/nginx/anila.conf`), so the link returned to the frontend is `/uploads/flux/<uuid>.png`.

### dev / prod sharing semantics (verified against the current compose)

- Both flux services are provided by the **model stack** (`infra/models/docker-compose.yml`, project `anila-models`), not the platform stack; the platform dev (`infra/compose/dev.yml`) and prod (`infra/compose/platform.yml`) both join the same external network `anila-models-net` to reach them.
- **Both dev and prod** seed-register the `image-generator` agent (endpoint `http://flux2-dev-agent:8000`) and wire `FLUX_BACKEND_URL` for `anila-studio`. The difference: prod uses `${FLUX_AGENT_BASE_URL-…}` / `${FLUX_BACKEND_URL-…}` (the `${VAR-default}` no-colon form) as a **toggle** — setting them to an empty string makes `get_flux_provider()` return `None` (Studio drawing OFF) or points the agent endpoint elsewhere; dev defaults them on.

---

## Related docs

- Inference backend: [`flux2-dev`](../flux2-dev/README.en.md) · Model stack: [`infra/models`](../../infra/models/README.en.md)
- FLUX spec: [`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md) (§3.2 `/generate` contract)
- Router dispatch: [`anila-core-router`](../anila-core-router/README.en.md)
- Redesign docs: [`05-agent-registry-and-runtime-protocol.md`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md), [`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)

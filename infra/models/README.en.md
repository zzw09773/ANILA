# ANILA models — standalone inference compose (FLUX image generation)

> A standalone compose project (`name: anila-models`) whose lifecycle is decoupled from the platform stack, housing ANILA's inference workloads; this document focuses on local FLUX.2-dev image generation (air-gapped, intranet-only).

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: `infra/models/` (formerly `models/`) exists on every ANILA deployment branch (identical across branches). The models stack has a separate lifecycle from the platform stack and is reached cross-stack via the external network `anila-models-net`. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

`infra/models/` (formerly `models/`) houses the standalone compose project (`name: anila-models`). The two image-generation services (source now at `services/flux2-dev` and `services/flux2-dev-agent`) are:

- **`flux2-dev`** — the FLUX.2-dev text-to-image inference service. It wraps `diffusers`' `Flux2Pipeline` into a minimal HTTP server (`server.py`) exposing `POST /generate`. Input `prompt / aspect_ratio / seed / ...`, returns JSON (base64 PNG list + audit meta).
- **`flux2-dev-agent`** — an agent wrapper (OpenAI `/v1/chat/completions` compatible). The router / CSP forward image-generation requests here; it handles prompt translation (zh→en via gemma4), calls `flux2-dev`, lands the PNG to the share volume, and returns a Markdown image link to the frontend.

Data flow:

```
user chat → router → DISPATCH:image-generator
          → CSP proxy → flux2-dev-agent  (OpenAI chat compatible)
                          → (prompt translation via gemma4)
                          → flux2-dev  POST /generate  (FLUX inference)
                          → land PNG to /share/flux
                          ← Markdown image link
```

The frontend UX never sees `flux2-dev` directly; only the agent shim is its client.

---

## Architecture & Stack

### flux2-dev (inference service)

- **Language/framework**: Python 3.11, FastAPI + uvicorn.
- **Inference backend**: `diffusers`' `Flux2Pipeline` (`diffusers>=0.36,<0.40`), `transformers>=4.50,<5.0`, `accelerate`.
- **Precision & multi-GPU**: `torch_dtype=torch.bfloat16` (BF16), `device_map="balanced"` (weights sharded across GPUs).
- **Base image**: `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`, `torch==2.6.0` (cu124 wheel).
- **Offline**: `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`, weights mounted read-only, no HF connection.

`POST /generate` contract (spec §3.2, model-agnostic):

| Field | Type | Notes |
|------|------|------|
| `prompt` | `str` | required |
| `aspect_ratio` | `Literal["1:1","16:9","9:16","4:3","3:4","3:1"]` | default `16:9` |
| `seed` | `int \| None` | `None` = random (actual value returned for audit) |
| `num_candidates` | `int (1–4)` | default `1`, always returns a list |
| `num_inference_steps` | `int \| None` | `None` = env `FLUX_NUM_STEPS` (default 28) |
| `guidance_scale` | `float \| None` | `None` = env `FLUX_GUIDANCE_SCALE` (default 4.0) |

Returns `GenerateResponse`:

```json
{"images": ["<base64 PNG>", "..."], "seed": 123456,
 "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}}
```

Design notes: always returns `list[str]` (even for `num_candidates=1`); aspect ratios map to fixed resolutions (incl. `3:1` letterbox, ≤0.8MP FLUX stable zone); seeds use a **CPU generator** (with `device_map="balanced"` weights span GPUs, so pinning a cuda device is unsafe); `GET /health` → `{"status":"ok"}`; `FLUX_SKIP_LOAD=1` uses a stub pipeline (no real weights, no GPU) for integration smoke tests.

### flux2-dev-agent (agent wrapper)

- **Language/framework**: Python 3.11, FastAPI + uvicorn, httpx; base `python:3.11-slim` (no GPU; healthcheck via urllib).
- **Endpoints**: `GET /health`, `GET /v1/models` (returns `image-generator`), `POST /v1/chat/completions` (JSON + SSE).
- **Four collaborators**: `prompt_translator.py` (calls gemma4 via the CSP proxy to rewrite colloquial Chinese into FLUX-friendly English; any error falls back to the original text), `flux_client.py` (calls `flux2-dev` `/generate`, extracts the first candidate PNG), `image_store.py` (validates PNG magic bytes, writes the share volume, returns a public URL), `chat_handler.py` (chains the three, builds an OpenAI-shaped response `已為您繪製：\n\n![](url)`).

---

## Layout

```
infra/models/
├── docker-compose.yml          # anila-models project (LLM/embedding/FLUX services)
└── src/                        # build context for non-FLUX services (e.g. the embedding proxy)

services/flux2-dev/             # FLUX.2-dev inference service (compose references ../../services/flux2-dev)
├── Dockerfile                  # CUDA 12.4 base + torch 2.6 + diffusers
├── requirements.txt · pyproject.toml
├── server.py                   # build_app + /generate + /health + pipeline loading
└── tests/                      # pytest (mock pipeline injected, no GPU needed)

services/flux2-dev-agent/       # agent wrapper (OpenAI compatible)
├── Dockerfile                  # python:3.11-slim
├── app/{main,schemas,prompt_translator,flux_client,image_store,chat_handler}.py
└── tests/                      # pytest (pytest-asyncio + respx)
```

> The same `docker-compose.yml` also defines non-image services `gpt-oss-20b` / `gemma4` / `nv-embed-triton` / `nv-embed-proxy`; this document focuses on the two FLUX services.
>
> The repo-root `models/` directory still holds `inference/` (local build context & load-test logs for the TensorRT-LLM / Triton images) and `model/` (HuggingFace weight symlinks, **unchanged location**); both are git-untracked, out of FLUX scope, and not covered here.

---

## Setup & Run

All services only use `expose:` (intranet), with **no host port**; CSP reaches them via the shared external network `anila-models-net` by DNS.

```bash
# one-time bootstrap
docker network create anila-models-net
docker compose restart csp                              # run from the repo root; CSP joins the network

# day-to-day (run from the repo root)
docker compose -f infra/models/docker-compose.yml up -d
docker compose -f infra/models/docker-compose.yml logs -f flux2-dev
docker compose -f infra/models/docker-compose.yml restart flux2-dev-agent
docker compose -f infra/models/docker-compose.yml down   # touches models only; platform unaffected
```

GPU / resources (from compose): `flux2-dev` GPUs `["1","2"]`, `shm_size: 32g`, `ipc: host`, healthcheck `/health` (`start_period: 300s`); `flux2-dev-agent` no GPU, `depends_on: flux2-dev (service_healthy)`.

Key environment variables:

| Service | Variable | Default / value | Notes |
|------|------|-----------|------|
| flux2-dev | `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | weights path |
| flux2-dev | `FLUX_DEVICE_MAP` | `balanced` | multi-GPU sharding |
| flux2-dev | `FLUX_NUM_STEPS` | `28` | inference steps |
| flux2-dev | `FLUX_GUIDANCE_SCALE` | `3.5` (compose) / `4.0` (code fallback) | guidance |
| flux2-dev | `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |
| flux2-dev-agent | `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | backend inference service |
| flux2-dev-agent | `CSP_BASE_URL` / `CSP_API_KEY` | `http://csp:8000` / env | translation callback via CSP |
| flux2-dev-agent | `GEMMA_MODEL` / `ENABLE_PROMPT_TRANSLATION` | `gemma4` / `1` | translation LLM / off = send original |
| flux2-dev-agent | `SHARE_DIR` / `PUBLIC_URL_PREFIX` | `/share/flux` / `/uploads/flux` | landing path & public URL |
| flux2-dev-agent | `DEFAULT_ASPECT_RATIO` / `FLUX_TIMEOUT_SECONDS` | `16:9` / `240` (code fallback `180`, compose overrides to 240) | default aspect ratio / backend timeout |

> Also: `flux2-dev` has `FLUX_MODEL_SHA` (default `""`, written into `meta.model_sha`) and `FLUX_SKIP_LOAD=1` (stub pipeline — no weights, no GPU — for integration smoke tests); `FLUX_MAX_CONCURRENT`(4) is controlled by the upstream csp/studio, not this service. In compose, `flux2-dev` is bound to GPUs `["1","2"]` and `gpt-oss-20b` to `["2"]` — they share GPU 2, so watch VRAM when deploying.

**Air-gapped weights**: FLUX.2-dev weights are mounted read-only (`.../FLUX.2-dev:/workspace/model/FLUX.2-dev:ro`) with `HF_HUB_OFFLINE=1`, so the container never fetches weights externally. `flux2-dev-agent` binds the host `share-dev/uploads/flux` to `/share/flux`; nginx serves it at `/uploads/flux/` (frontend image links point there).

---

## Testing

Both services ship a pytest suite that needs **no GPU** (mock pipeline injected / respx-stubbed HTTP), runnable straight on a dev box:

```bash
# flux2-dev (conftest sets FLUX_SKIP_LOAD=1 — no weights loaded)
cd services/flux2-dev       && pip install -e '.[test]' && pytest

# flux2-dev-agent (pytest-asyncio + respx; asyncio_mode=auto)
cd services/flux2-dev-agent && pip install -e '.[test]' && pytest
```

> Pinned test deps live in each `pyproject.toml` under `[project.optional-dependencies].test`; torch / diffusers are **not** in test deps, so CI/dev never installs the GPU stack.

---

## Integration

- When gemma4 emits `DISPATCH:image-generator:...`, the platform router has **CSP proxy** forward the request to `flux2-dev-agent` (registered in CSP `model_registry` as `model_type=agent`).
- `flux2-dev-agent` connects directly to `flux2-dev` `/generate` via `FLUX_BACKEND_URL`; CSP's studio can hit `flux2-dev` along the same path.
- The three share the external network `anila-models-net`, reachable only by container-to-container DNS; no host port is opened.
- Translation callback: the agent calls CSP `/v1/chat/completions` (model=`gemma4`) via `CSP_BASE_URL` + `CSP_API_KEY` to rewrite colloquial prompts into English.

---

## Related docs

- FLUX spec: [`../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md) (§3.2 `/generate` contract, §9 licensing minefield)
- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

---

## Licensing note (important)

Per spec §9:

- **`black-forest-labs/FLUX.2-dev` is under the BFL Non-Commercial License** — internal commercial use is a grey area. **Confirm with legal before going to production** whether an "internal company tool" falls within the license.
- **`FLUX.2-klein-4B` is Apache-2.0**, the cleanest local alternative legally (slightly lower quality, lower latency). If legal deems internal commercial use of the dev version doubtful, **switch the primary to klein-4B**.
- `FLUX.2-klein-9B` is Non-Commercial like dev and does not solve the licensing issue.

The compose `flux2-dev` service also carries a matching caveat comment above it.

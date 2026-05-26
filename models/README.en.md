# ANILA models — FLUX image-generation model services

> ANILA's local FLUX.2-dev image-generation model services (air-gapped, internal-network only).

> English mirror of [`README.md`](./README.md) (Traditional Chinese primary).

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** `models/docker-compose.yml`'s flux2-dev-agent has `CSP_API_KEY` / `volumes` aligned with prod conventions (`${INTERNAL_PLATFORM_API_KEY:?must be set}` + `/share/uploads/flux`, no `_DEV` fallback or `share-dev/`).

---

## Overview

`models/` is a standalone compose project (`name: anila-models`) whose lifecycle is decoupled from the platform stack; it hosts ANILA's inference workloads. Two services relate to image generation:

- **`flux2-dev`** — the FLUX.2-dev text-to-image inference service. A minimal HTTP server (`server.py`) wrapping `diffusers`' `Flux2Pipeline`, exposing `POST /generate`. It takes `prompt / aspect_ratio / seed / ...` and returns JSON (a base64 PNG list + audit meta).
- **`flux2-dev-agent`** — the agent wrapper (OpenAI `/v1/chat/completions` compatible). The platform's router / CSP forwards image-generation requests here; it handles prompt translation (zh→en via gemma4), calls `flux2-dev`, persists the produced PNG to a share volume, and returns a Markdown image link to the frontend.

How they relate (data flow):

```
user chat → router → DISPATCH:image-generator
         → CSP proxy → flux2-dev-agent  (OpenAI chat compatible)
                         → (prompt translation via gemma4)
                         → flux2-dev  POST /generate  (FLUX inference)
                         → persist PNG to /share/flux
                         ← Markdown image link
```

Soldier-facing UX never sees `flux2-dev` directly; only the agent shim is its client.

---

## Architecture & Stack

### flux2-dev (inference service)

- **Language/framework**: Python 3.11, FastAPI + uvicorn.
- **Inference backend**: `diffusers`' `Flux2Pipeline` (`diffusers>=0.36,<0.40`), `transformers>=4.50,<5.0`, `accelerate`.
- **Precision & multi-GPU**: `torch_dtype=torch.bfloat16` (BF16), `device_map="balanced"` (weights sharded across GPUs).
- **Base image**: `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`, `torch==2.6.0` (cu124 wheel).
- **Offline**: `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`; weights are mounted read-only into the container, no HF access.

`POST /generate` contract (per spec §3.2, model-agnostic, shared by flux2-dev and klein-4B):

| Field | Type | Notes |
|-------|------|-------|
| `prompt` | `str` | required |
| `aspect_ratio` | `Literal["1:1","16:9","9:16","4:3","3:4","3:1"]` | default `16:9` |
| `seed` | `int \| None` | `None` = random (resolved value echoed back for audit) |
| `num_candidates` | `int (1–4)` | default `1`; response is always a list |
| `num_inference_steps` | `int \| None` | `None` = env `FLUX_NUM_STEPS` (default 28) |
| `guidance_scale` | `float \| None` | `None` = env `FLUX_GUIDANCE_SCALE` (default 4.0) |

`GenerateResponse`:

```json
{
  "images": ["<base64 PNG>", "..."],
  "seed": 123456,
  "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}
}
```

Design notes:

- **Always returns list[str]** (even when `num_candidates=1`), so Stage 2's N-candidate gate does not force a response-type rewrite.
- Each aspect ratio maps to a fixed resolution (`_ASPECT_RATIOS`), including a `3:1` section-band letterbox (≤0.8MP, FLUX stable zone).
- Seeding uses a **CPU generator** (`torch.Generator(device="cpu")`): because `device_map="balanced"` shards weights across GPUs, a fixed cuda device is unsafe; a CPU generator gives deterministic, device-independent seeding.
- `GET /health` → `{"status": "ok"}`.
- `FLUX_SKIP_LOAD=1` uses a stub pipeline (no real weights, no GPU) for integration smoke tests.

### flux2-dev-agent (agent wrapper)

- **Language/framework**: Python 3.11, FastAPI + uvicorn, httpx (async).
- **Base image**: `python:3.11-slim` (no GPU, no curl; healthcheck uses urllib).
- **Endpoints**: `GET /health`, `GET /v1/models` (returns `image-generator`), `POST /v1/chat/completions` (supports both JSON and SSE streaming).
- **Four collaborators**:
  - `prompt_translator.py` — calls `gemma4` through the CSP proxy to rewrite casual Chinese into a FLUX-friendly English prompt; on any error it **falls back to the original text** (partial degradation beats total failure).
  - `flux_client.py` — calls `flux2-dev`'s `/generate` and decodes the first candidate to PNG bytes.
  - `image_store.py` — validates the PNG magic bytes, writes to the share volume, returns the public URL.
  - `chat_handler.py` — wires the above three together and assembles an OpenAI-shaped `ChatCompletionResponse` whose content is `已為您繪製：\n\n![](url)`.

---

## Layout

```
models/
├── docker-compose.yml          # anila-models compose project (LLM/embedding/FLUX services)
├── flux2-dev/                  # FLUX.2-dev inference service
│   ├── Dockerfile              # CUDA 12.4 base + torch 2.6 + diffusers
│   ├── requirements.txt        # fastapi / diffusers / transformers / accelerate ...
│   ├── pyproject.toml          # tests inject a mock pipeline, no GPU stack needed
│   ├── server.py               # build_app + /generate + /health + pipeline loading
│   └── tests/                  # pytest (test_server.py, conftest.py)
└── flux2-dev-agent/            # agent wrapper (OpenAI compatible)
    ├── Dockerfile              # python:3.11-slim
    ├── requirements.txt        # fastapi / httpx / pydantic / python-multipart
    ├── pyproject.toml
    ├── app/
    │   ├── main.py             # build_app + endpoints (/health, /v1/models, /v1/chat/completions)
    │   ├── schemas.py          # OpenAI chat-compatible pydantic models
    │   ├── prompt_translator.py
    │   ├── flux_client.py
    │   ├── image_store.py
    │   └── chat_handler.py
    └── tests/                  # pytest (pytest-asyncio + respx)
```

> Note: the same `docker-compose.yml` also defines non-image services (`gpt-oss-20b`, `gemma4`, `nv-embed-triton`, `nv-embed-proxy`). This document focuses on the two FLUX services.

---

## Setup & Run

These services run via `models/docker-compose.yml`. Every service uses `expose:` (internal-only) and opens **no host port**; external NICs cannot reach them, and CSP connects via DNS on the shared external docker network `anila-models-net`.

One-time bootstrap (on the host):

```bash
docker network create anila-models-net
docker compose -f docker-compose.yml restart csp   # CSP joins the net
```

Day-to-day:

```bash
docker compose -f models/docker-compose.yml up -d
docker compose -f models/docker-compose.yml logs -f flux2-dev
docker compose -f models/docker-compose.yml ps
docker compose -f models/docker-compose.yml restart flux2-dev-agent
docker compose -f models/docker-compose.yml down          # models only; platform untouched
```

GPU / resource config (from compose):

- `flux2-dev`: GPU `["1","2"]`, `shm_size: 32g`, `ipc: host`, healthcheck hits `/health` (`start_period: 300s`).
- `flux2-dev-agent`: no GPU, `depends_on: flux2-dev (service_healthy)`.

Key environment variables:

| Service | Variable | Default / value | Notes |
|---------|----------|-----------------|-------|
| flux2-dev | `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | weights path |
| flux2-dev | `FLUX_DEVICE_MAP` | `balanced` | multi-GPU sharding |
| flux2-dev | `FLUX_NUM_STEPS` | `28` | default inference steps |
| flux2-dev | `FLUX_GUIDANCE_SCALE` | `3.5` (compose) / `4.0` (code fallback) | default guidance |
| flux2-dev | `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |
| flux2-dev-agent | `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | inference backend |
| flux2-dev-agent | `CSP_BASE_URL` / `CSP_API_KEY` | `http://csp:8000` / env | translation callback via CSP |
| flux2-dev-agent | `GEMMA_MODEL` | `gemma4` | LLM used for translation |
| flux2-dev-agent | `ENABLE_PROMPT_TRANSLATION` | `1` | disable to send raw input |
| flux2-dev-agent | `SHARE_DIR` / `PUBLIC_URL_PREFIX` | `/share/flux` / `/uploads/flux` | persist path & public URL |
| flux2-dev-agent | `DEFAULT_ASPECT_RATIO` | `16:9` | default aspect ratio |
| flux2-dev-agent | `FLUX_TIMEOUT_SECONDS` | `240` | backend call timeout |

**Air-gapped weight handling**: FLUX.2-dev weights are mounted read-only into the container
(`/home/aia/c1147259/project/Huggingface/FLUX.2-dev:/workspace/model/FLUX.2-dev:ro`)
together with `HF_HUB_OFFLINE=1`, so the container never fetches weights over the network.

`flux2-dev-agent` binds the host's `share-dev/uploads/flux` to the container's `/share/flux`; the same directory is served by nginx under `/uploads/flux/` (the frontend image links point here).

---

## Integration

- When gemma4 emits `DISPATCH:image-generator:...`, the platform router has the **CSP proxy** forward the request to `flux2-dev-agent` (CSP's `model_registry` registers the agent as `model_type=agent`).
- `flux2-dev-agent` calls `flux2-dev`'s `/generate` directly via `FLUX_BACKEND_URL`; CSP's studio can also reach `flux2-dev` along the same path.
- All three share the external docker network `anila-models-net`, reachable only via container-to-container DNS; no host port is opened.
- Translation callback: the agent calls CSP's `/v1/chat/completions` (model=`gemma4`) via `CSP_BASE_URL` + `CSP_API_KEY` to rewrite the user's casual request into an English prompt.

---

## Related docs

- FLUX spec: [`docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md) (existence verified).
  - §3.2 defines the `/generate` contract (this service implements it).
  - §9 covers the licensing caveats.

---

## Licensing note

Per spec §9:

- **`black-forest-labs/FLUX.2-dev` is a BFL Non-Commercial License**; internal commercial use is a grey area — **get legal confirmation before going to production** on whether an "internal company tool" falls within the license.
- **`FLUX.2-klein-4B` is Apache-2.0**, the cleanest local alternative (slightly lower quality, lower latency). If legal deems dev-version internal commercial use questionable, **switch the primary model to klein-4B**.
- `FLUX.2-klein-9B` shares the same Non-Commercial license as dev and does not resolve the issue.

The `flux2-dev` service in compose carries a matching caveat comment: confirm authorization before enabling in production.

---

**Last updated**: 2026-05-26 (sync PR #16 + add prod banner; compose flipped from dev fallback back to prod fail-loud)

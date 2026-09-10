# flux2-dev

> **FLUX.2-dev text-to-image inference service** — a minimal HTTP wrapper (`server.py`) around `diffusers`' `Flux2Pipeline`, exposing only `POST /generate`, `POST /v1/images/generations` (OpenAI-compatible) and `GET /health`. Air-gapped, intranet-only (no host port, no inbound auth); appears in the stack as the service `flux2-dev` on the external network `anila-models-net`.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: This service is built and served by the `anila-models` model stack (see [`infra/models/`](../../infra/models/README.en.md)) and exists on the ANILA deployment branches that use that stack. See the root [`README.md`](../../README.md) branch matrix (current line is a single `main`; the old seven-branch model is retired).

---

## What it is

`flux2-dev` is the **raw inference backend** for FLUX.2-dev: give it a prompt, get back base64 PNGs. It is deliberately thin — no queue, no job management, no auth; those belong to the layers above. The pipeline is constructed once at module import and injected into `build_app`; tests inject a mock pipeline instead, so **development / CI never load real weights and need no GPU**.

End users never see this service directly; it has two internal clients:

- **`flux2-dev-agent`** ([`../flux2-dev-agent`](../flux2-dev-agent/README.en.md)) — the wrapper for the chat "image drawing" flow (Router dispatches the `image-generator` agent); it takes back a single candidate image.
- **`anila-studio`**'s `FluxImageProvider` (`services/anila-studio/app/services/flux_image_provider.py`) — the Studio (產出中心) slide / infographic illustration pipeline, which calls the OpenAI-compatible `/v1/images/generations` endpoint.

```
user chat → Router → DISPATCH:image-generator
                   → CSP proxy → flux2-dev-agent  (OpenAI chat compatible)
                                  → (prompt translation via gemma4)
                                  → flux2-dev  POST /v1/images/generations   ← this service
Studio    → anila-studio FluxImageProvider ──────┘
```

---

## `/v1/images/generations` (OpenAI-compatible, added 2026-07)

With the on-prem models consolidated onto the cloud compute centre, both platform clients (`anila-studio`'s `FluxImageProvider` and `flux2-dev-agent`'s `FluxClient`) now speak the standard **OpenAI Images API**. This dev backend exposes the same endpoint so local development matches the new contract; **the legacy `/generate` is kept unchanged** (backward compatibility).

Request (standard OpenAI fields):

```json
{"model": "flux.2-dev", "prompt": "...", "n": 1, "size": "1024x1024", "response_format": "b64_json"}
```

- `size`: a `"WxH"` string (64–2048); malformed → `422`. Parsed into width/height and fed to the same generation core as `/generate`.
- `response_format` supports only `"b64_json"` (this service hosts no static files, so it cannot issue urls) → anything else returns `400`.
- steps / guidance come from env defaults (`FLUX_NUM_STEPS` / `FLUX_GUIDANCE_SCALE`); the seed is random per request (the OpenAI contract has no seed field).

Response:

```json
{"created": 1720000000, "data": [{"b64_json": "<base64 PNG>"}]}
```

---

## `/generate` contract

Per ANILA Studio FLUX Stage 1 ([spec §3.2](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)), `/generate` returns **JSON** (base64 PNG list + audit meta), **not** raw `image/png` bytes. The contract is model-agnostic (same for flux2-dev and klein-4B).

| Endpoint | Method | Notes |
|----------|--------|-------|
| `/health` | GET | `{"status": "ok"}` |
| `/generate` | POST | custom JSON contract (request / response below) — kept for backward compatibility |
| `/v1/images/generations` | POST | OpenAI Images API compatible (next section) |

**`GenerateRequest`**:

| Field | Type | Default / notes |
|-------|------|-----------------|
| `prompt` | `str` | required |
| `aspect_ratio` | `Literal["1:1","16:9","9:16","4:3","3:4","3:1"]` | default `16:9`; anything else → `422` |
| `seed` | `int \| None` | `None` = random (actual value echoed back for audit; `seed=0` handled correctly) |
| `num_candidates` | `int` (1–4) | default `1`, response is always a list |
| `num_inference_steps` | `int \| None` | `None` = env `FLUX_NUM_STEPS` (default `28`) |
| `guidance_scale` | `float \| None` | `None` = env `FLUX_GUIDANCE_SCALE` (code fallback `4.0`) |

**`GenerateResponse`**:

```json
{"images": ["<base64 PNG>", "..."], "seed": 123456,
 "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}}
```

Inference failure returns `500` (`detail: "inference failed: ..."`).

**aspect_ratio → resolution** (fixed table in `server.py`, all within the ≤0.8MP FLUX stable zone):

| ratio | `1:1` | `16:9` | `9:16` | `4:3` | `3:4` | `3:1` |
|-------|-------|--------|--------|-------|-------|-------|
| W×H | 1024×1024 | 1408×768 | 768×1408 | 1216×896 | 896×1216 | 1536×512 |

`3:1` (1536×512) is a section-band letterbox.

### Design notes

- **Always returns `list[str]`** (even for `num_candidates=1`) so Stage 2's N-candidate gate needs no response-type rewrite.
- **CPU generator for seeding**: the pipeline shards weights across GPUs via `device_map="balanced"`, so pinning a fixed cuda device is unsafe; a CPU generator gives deterministic, device-independent seeding.
- **`FLUX_SKIP_LOAD=1`** uses a no-op stub pipeline (returns an all-black image) — the container comes up healthy with no weights and no GPU, for integration smoke tests. `conftest` sets this flag by default, so pytest never loads real weights.

---

## Tech stack

- **Language / framework**: Python 3.11 (container), FastAPI + `uvicorn`; started in-container with `uvicorn server:app`.
- **Inference backend**: `diffusers`' `Flux2Pipeline` (`diffusers>=0.36,<0.40`; `Flux2Pipeline` added in 0.36), `transformers>=4.50,<5.0`, `accelerate`.
- **Precision & multi-GPU**: `torch_dtype=torch.bfloat16` (BF16), `device_map="balanced"` (weights sharded across GPUs).
- **Base image**: `nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04` + `torch==2.6.0` (cu124 wheel; torch 2.4 cannot infer the PEP 604 union custom_op schemas diffusers/transformers use).
- **Offline**: `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`, weights mounted read-only, no HuggingFace access.

### Key environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | weights path (read-only mount) |
| `FLUX_DEVICE_MAP` | `balanced` | multi-GPU sharding strategy |
| `FLUX_NUM_STEPS` | `28` | inference steps (request can override) |
| `FLUX_GUIDANCE_SCALE` | `4.0` (code) / `3.5` (models compose) | guidance (request can override) |
| `FLUX_MODEL_SHA` | `""` | written into `meta.model_sha` for audit |
| `FLUX_SKIP_LOAD` | unset | `1` = stub pipeline, no weights, no GPU |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |

> Concurrency / timeout knobs like `FLUX_MAX_CONCURRENT` / `FLUX_TIMEOUT_SECONDS` live in the **upstream** clients (`anila-studio`, `flux2-dev-agent`), **not** in this service.

---

## Directory layout

```
services/flux2-dev/
├── server.py           # build_app + /generate + /v1/images/generations + /health + pipeline load
├── Dockerfile          # CUDA 12.4 base → torch 2.6 (cu124) → requirements.txt
├── requirements.txt    # runtime deps (incl. GPU stack)
├── pyproject.toml      # minimal runtime + [test] extra (no torch/diffusers)
└── tests/
    ├── conftest.py     # sets FLUX_SKIP_LOAD=1 by default
    └── test_server.py  # 8 tests, mock pipeline injected (no GPU/weights)
```

---

## Local development & testing

Tests inject a mock pipeline and need **no GPU and no 80GB weights** — run them straight on a dev box:

```bash
python -m venv .venv
.venv/bin/pip install -e 'services/flux2-dev[test]'   # from repo root; or cd in and pip install -e '.[test]'
cd services/flux2-dev && ../../.venv/bin/python -m pytest -q
# → 8 passed
```

> This repo commits no `.venv`; the example creates a throwaway one. The `[test]` extra is only `pytest` + `httpx` (for FastAPI `TestClient`) — `torch` / `diffusers` are **not** test deps.

Bring the service up with no weights and no GPU (returns all-black images):

```bash
FLUX_SKIP_LOAD=1 uvicorn server:app --port 8000   # in services/flux2-dev/
curl -s localhost:8000/health                       # {"status":"ok"}
```

---

## Deployment

Built and served by the `anila-models` model stack (build context `../../services/flux2-dev`); it is **not** part of the platform stack, and the two connect via the external network `anila-models-net`. Day-to-day operation and GPU/resource details are in [`infra/models/README.en.md`](../../infra/models/README.en.md).

- Only `expose: "8000"`, **no host port**; the agent / studio reach it via docker DNS `http://flux2-dev:8000`.
- The models compose binds GPU `["1","2"]`, `shm_size: 32g`, `ipc: host`; healthcheck hits `/health` (`start_period: 300s`).
- Note that `gpt-oss-20b` in the models compose binds GPU `["2"]` — it **shares GPU 2** with `flux2-dev`; watch VRAM when deploying.

---

## License caveat (important)

Per [spec §9](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md):

- **`black-forest-labs/FLUX.2-dev` is under the BFL Non-Commercial License**; internal commercial use is a grey area — have legal confirm whether an "internal company tool" falls within scope before going to production.
- **`FLUX.2-klein-4B` is Apache-2.0**, the cleanest local alternative (slightly lower quality, lower latency). This service's contract is model-agnostic, so swapping models needs no `/generate` change.
- `FLUX.2-klein-9B` is Non-Commercial like dev and does not resolve the licensing issue.

---

## Related docs

- FLUX spec: [`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md) (§3.2 `/generate` contract, §9 licensing landmines)
- Agent wrapper: [`flux2-dev-agent`](../flux2-dev-agent/README.en.md) · Model stack: [`infra/models`](../../infra/models/README.en.md)
- Redesign docs: [`05-agent-registry-and-runtime-protocol.md`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md), [`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)

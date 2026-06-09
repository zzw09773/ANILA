# ANILA models — inference model services (LLM + Embedding)

> ANILA's local inference model services (air-gapped, internal-network only): two OpenAI-compatible LLMs + one embedding pipeline.

> Chinese is primary; this English mirror is [`README.md`](./README.md). Technical terms, commands, and code stay in English.

---

## Overview

`models/` is a standalone compose project (`name: anila-models`) decoupled from the platform stack's lifecycle, hosting ANILA's inference workloads. Every service uses `expose:` only (internal); **no host ports** are opened. CSP reaches them over the shared external docker network `anila-models-net` via DNS.

| Service | Role | Backend | GPU | CSP-registered endpoint |
|---------|------|---------|-----|-------------------------|
| **`gpt-oss-20b`** | LLM | TensorRT-LLM (OpenAI-compatible server) | GPU 2 | `http://gpt-oss-20b:8000/v1` |
| **`gemma4`** | LLM (Router primary) | vLLM + MTP speculative decoding | GPU 3 | `http://gemma4:8000/v1` |
| **`nv-embed-triton`** | Embedding inference backend | Triton (its own protocol, **fully internal**) | GPU 0 | (not connected directly, see below) |
| **`nv-embed-proxy`** | Embedding OpenAI-compatible front | FastAPI shim → Triton | none | `http://nv-embed-proxy:8000/v1` |

> Of the host's 4 GPUs, 3 are used (GPU 0 / 2 / 3); GPU 1 is reserved for scale-out. `nv-embed-triton` speaks Triton's own protocol, not OpenAI v1, so CSP **never connects to it directly** — only its sibling `nv-embed-proxy` reaches it via internal DNS. Triton therefore opens neither `expose:` nor `ports:`; container-to-container traffic only.

---

## Architecture & Stack

```
        CSP /v1/* proxy  (token_usage metering)
                 │  docker DNS (anila-models-net)
     ┌───────────┼───────────────────────┐
     ▼           ▼                        ▼
 gpt-oss-20b   gemma4                nv-embed-proxy  (OpenAI /v1/embeddings)
 (TRT-LLM)     (vLLM + MTP)               │ Triton protocol
  GPU 2         GPU 3                      ▼
                                     nv-embed-triton  (no expose, internal-only)
                                          GPU 0
```

### gpt-oss-20b (LLM, TensorRT-LLM)

- **Image**: `tensorrt-llm-hf:1.3.0rc10`; `trtllm-serve` exposes the OpenAI-compatible `/v1/*`.
- **GPU / resources**: `device_ids: ["2"]`, `ipc: host`, `memlock: -1`.
- **Offline**: `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1`; weights are volume-mounted, never fetched from HF.
- **Launch args**: `--extra_llm_api_options .../gpt-oss-20b-throughput.yaml`, `--kv_cache_free_gpu_memory_fraction 0.5`, `--port 8000`.
- **Healthcheck**: `GET /v1/models` (`start_period: 120s`).

### gemma4 (LLM, vLLM + MTP)

- **Image**: `vllm-gemma4:latest`; `--served-model-name gemma4` keeps the same id across CSP `model_registry`, Router, and agent fan-out.
- **Model**: `gemma-4-31B-it`, `--max-model-len 131072`, `--dtype bfloat16`, `--kv-cache-dtype fp8`, `--gpu-memory-utilization 0.97`, `--enable-prefix-caching`.
- **MTP speculative decoding**: `--speculative-config '{"method":"mtp","model":".../gemma-4-31B-it-assistant","num_speculative_tokens":2}'`; the small draft head shares the target's KV cache, measured 1.7–2.2× speedup (conversational > code). Needs vLLM ≥ 0.18 (with the Gemma4 MTP spec-decode PR); older builds exit with `unknown speculative method 'mtp'`. The assistant draft model must be downloaded offline to the host once.
- **tool / reasoning**: `--enable-auto-tool-choice`, `--tool-call-parser gemma4`, `--reasoning-parser gemma4`, `--default-chat-template-kwargs '{"enable_thinking": true}'`.
- **GPU / resources**: `device_ids: ["3"]`, `shm_size: 64g`, `ipc: host`.
- **Healthcheck**: `GET /v1/models` (`start_period: 180s`).

### nv-embed-triton (embedding backend, Triton)

- **Image**: `tritonserver:25.04-nv-embed-v2`, model `NV-Embed-v2`.
- **GPU**: `device_ids: ["0"]`; `--model-control-mode=poll --repository-poll-secs=1`.
- **Network**: **no `expose:` and no `ports:`** — only `nv-embed-proxy` reaches it via docker DNS inside `anila-models-net`, minimizing attack surface.
- **Healthcheck**: `GET /v2/health/ready` (Triton protocol).

### nv-embed-proxy (embedding OpenAI-compatible front)

- **Image**: `embedding-proxy:migration` (FastAPI shim). Bridges the OpenAI `/v1/embeddings` shape to Triton. **This is the embedding endpoint CSP actually registers.**
- **Env**: `TRITON_URL=http://nv-embed-triton:8000`, `MODEL_NAME=nv-embed-v2`, `REQUEST_TIMEOUT=60`.
- **Dependency**: `depends_on: nv-embed-triton (service_healthy)`; no GPU.
- **Healthcheck**: `GET /v1/models` (`start_period: 30s`).

---

## Layout

```
models/
├── docker-compose.yml   # anila-models compose project (the 4 services above)
├── README.md
└── README.en.md
```

> Model weights, the Triton repository, TensorRT engines, etc. are volume-mounted from **absolute host paths** (`/home/aia/c1147259/project/Huggingface/...`, `.../Docker/...`), not stored in the repo; migrate or fix the compose paths when moving hosts.

---

## Setup & Run

One-time bootstrap (on the host):

```bash
# 1. create the cross-project external network
docker network create anila-models-net

# 2. the platform csp container already declares this network — restart it once to actually join
docker compose -f docker-compose.yml restart csp
```

Day-to-day:

```bash
docker compose -f models/docker-compose.yml up -d
docker compose -f models/docker-compose.yml ps
docker compose -f models/docker-compose.yml logs -f gemma4
docker compose -f models/docker-compose.yml restart gemma4
docker compose -f models/docker-compose.yml down          # models only; the platform is untouched
```

GPU / resource layout (from compose):

| Service | GPU | Notes |
|---------|-----|-------|
| `gpt-oss-20b` | 2 | TensorRT-LLM, `ipc: host`, kv_cache 0.5 |
| `gemma4` | 3 | vLLM + MTP, large `shm_size`, max-model-len 131072 |
| `nv-embed-triton` | 0 | Triton, no expose (internal-only) |
| `nv-embed-proxy` | — | FastAPI shim, `depends_on` triton |

---

## Integration

- **CSP**: register the three endpoints (`http://gpt-oss-20b:8000/v1`, `http://gemma4:8000/v1`, `http://nv-embed-proxy:8000/v1`) into `model_registry` via the `/models` UI or `AUTO_REGISTER_MODELS`, marking them `is_internal`. CSP's `/v1/chat/completions` / `/v1/embeddings` proxy routes to these endpoints and meters `token_usage`.
- **Router**: the primary routing LLM defaults to `gemma4`.
- **ingestion-worker**: embeds chunks through CSP `/v1/embeddings` (i.e. `nv-embed-proxy`).
- All three share the external docker network `anila-models-net`, reachable only container-to-container; no host ports are opened.

### End-to-end connectivity check

```bash
# from an external host (or the host shell) everything should be refused (no host ports)
curl http://<host-LAN-IP>:8000/v1/models     # connection refused

# from inside the CSP container, via docker DNS, should be 200
docker compose exec csp curl http://gpt-oss-20b:8000/v1/models
docker compose exec csp curl http://gemma4:8000/v1/models
docker compose exec csp curl http://nv-embed-proxy:8000/v1/models
```

---

## Licensing note

Each model carries its own upstream license; confirm terms with legal before internal commercial use:

- **`gpt-oss-20b`**: OpenAI gpt-oss, Apache-2.0.
- **`gemma-4-31B-it`**: Google Gemma Terms of Use (not a standard OSI license; review the terms).
- **`NV-Embed-v2`**: NVIDIA license (includes non-commercial terms) — internal commercial use is a grey area; get legal confirmation before production.

# ANILA models — standalone inference compose

> A standalone compose project (`name: anila-models`) whose lifecycle is **decoupled** from the platform stack, housing ANILA's inference workloads: local LLMs, embedding, and FLUX image generation. Everything is air-gapped and intranet-only (no host port); cross-stack traffic goes over the external network `anila-models-net` via docker DNS.

> 中文版本：[`README.md`](./README.md). Technical terms, commands and code stay in English.

> 🌿 **Branch note**: `infra/models/` (formerly `models/`, moved under `infra/` in the §17.1 reorg) exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../../README.md) branch matrix (current line is a single `main`; the old seven-branch model is retired).

---

## Position

`infra/models/` holds one `docker-compose.yml` (project `anila-models`) plus the build contexts for the non-FLUX services (`src/`). It is a **separate project** from the platform stack (`anila`, see the root `compose.yaml` → `infra/compose/platform.yml`):

- Running `docker compose down` at the repo root only stops the platform — it does **not** kill the model containers here.
- The two projects communicate over the shared external network `anila-models-net`; CSP / Router / Studio reach the models via docker DNS (e.g. `http://gemma4:8000`), with no host port exposed.
- **Weights location is unchanged**: `ANILA_HF_DIR` defaults to `../../models/model` (resolved relative to this compose file). The §17.1 reorg only moved the compose and `src/`, not the weights directory.

---

## Services in this compose

**Default group (no profile)** — the currently active set on the trial box / intranet:

| Service | Role | GPU | Exposure |
|---------|------|-----|----------|
| `gpt-oss-20b` | TensorRT-LLM OpenAI server | `["2"]` | `expose 8000` |
| `gemma4` | vLLM (gemma-4-31B-it + MTP speculative decode, `served-model-name=gemma4`) | `["3"]` | `expose 8000` |
| `nv-embed-triton` | Triton (NV-Embed-v2 backend) | `["0"]` | **no expose/port** (fully cluster-internal) |
| `nv-embed-proxy` | OpenAI `/v1/embeddings` shim (`build ./src/embedding_proxy`, bridges Triton) | — | `expose 8000` |
| `flux2-dev` | FLUX.2-dev text-to-image backend | `["1","2"]` | `expose 8000` |
| `flux2-dev-agent` | image-generation agent wrapper (OpenAI chat compatible) | — | `expose 8000` |

**`--profile intranet` group** — added for the intranet H100s (generic vLLM image, GPU overridable via env):

| Service | Weights | GPU (env default) |
|---------|---------|-------------------|
| `gemma-4-26b-a4b` | MoE ~48G bf16 | `${GEMMA_A4B_GPU:-1}` |
| `gemma-4-12b` | ~22G bf16 | `${GEMMA_12B_GPU:-2}` |
| `gpt-oss-120b` | native MXFP4 ~63G | `${GPT_OSS_120B_GPU:-3}` |

> The FLUX pair's contract, env vars, tests and licensing are **not** covered here — see their own READMEs: [`services/flux2-dev`](../../services/flux2-dev/README.en.md) (the `/generate` inference backend) and [`services/flux2-dev-agent`](../../services/flux2-dev-agent/README.en.md) (the agent shim; their build contexts live under `services/`, referenced by this compose as `../../services/flux2-dev*`).

---

## Layout & weights

```
infra/models/
├── docker-compose.yml   # project anila-models (LLM / embedding / FLUX)
└── src/                 # build contexts / config for the non-FLUX services
    ├── tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/   # trtllm serve config + encodings + perf logs
    ├── tritonserver-25.04-nv-embed-v2/            # Triton model repository + export
    └── embedding_proxy/                           # nv-embed-proxy FastAPI (app.py + Dockerfile)

models/model/            # HuggingFace weights (ANILA_HF_DIR points here by default; a dev symlink works too, not moved)
models/inference/        # TensorRT-LLM / Triton local build context + perf logs (git-untracked, not part of this compose)
```

- `ANILA_HF_DIR` (default `../../models/model`) and `ANILA_MODELSRC_DIR` (default `./src`) are overridable — set the env when paths differ, so weights and config need no pinned host absolute paths.
- All models set `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1`, with weights mounted as volumes; containers never reach HuggingFace.

---

## Startup & deployment

Prefer the `infra/deployment/intranet/model-serve.sh` wrapper (auto-`source`s the repo-root `.env`, auto-creates `anila-models-net`, and always passes `--profile intranet` so both in- and out-of-profile services can be named):

```bash
# One-time bootstrap: create the net and re-apply networking to the platform stack
# (restart does NOT attach a newly-added network — use up -d).
docker network create anila-models-net
docker compose up -d csp            # at repo root, via the root shim compose.yaml; csp joins anila-models-net

# Day-to-day (at repo root)
bash infra/deployment/intranet/model-serve.sh up trial          # active set: gpt-oss-20b gemma4 nv-embed flux
bash infra/deployment/intranet/model-serve.sh up intranet       # H100 set: gemma4 26b-a4b 12b 120b nv-embed
bash infra/deployment/intranet/model-serve.sh up gemma4         # a single service (up always needs a group or service name)
bash infra/deployment/intranet/model-serve.sh status            # health overview
bash infra/deployment/intranet/model-serve.sh logs flux2-dev    # tail -f
bash infra/deployment/intranet/model-serve.sh down              # stop all (models only; platform untouched)
```

Equivalent bare compose commands:

```bash
docker compose -f infra/models/docker-compose.yml --profile intranet up -d gemma4
docker compose -f infra/models/docker-compose.yml logs -f flux2-dev
docker compose -f infra/models/docker-compose.yml down
```

> ⚠️ Do not use `START-HERE.sh` / `intranet-deploy.sh` for small model-layer tweaks — those are the one-time card bootstrap. Use `model-serve.sh` for model lifecycle and `infra/deployment/scripts/deploy-prod.sh` for the platform lifecycle. Apply config changes with `up -d` (`docker restart` does not reload `.env` / compose).

---

## Platform / Model Gateway boundary

CSP's **Model Gateway (治理中心, [doc 04](../../docs/anila-redesign-docs/04-model-gateway-design.md))** is where "registration, routing, per-model API keys, 5-state health, `ANILA_ENV` http fail-closed, classification limits, usage trace" live. This compose only **provides the upstream endpoints**; the two layers separate cleanly:

- **Same-host docker-DNS upstreams (this compose)**: `gpt-oss-20b` / `gemma4` / `nv-embed-proxy` (three `model_type`s) and `image-generator` (`model_type=agent`, endpoint `flux2-dev-agent:8000`) are seed-registered into the CSP model registry by the platform; no API key needed inside the network. `nv-embed-triton` and `flux2-dev` are **not registered directly** — the former is reached only by `nv-embed-proxy` internally, the latter only by the agent / Studio via URL.
- **Cross-host models (doc 04's main scenario)**: models on other intranet hosts (e.g. the `.12` gateway) go over HTTPS + per-model API key, proxied by the CSP Model Gateway — that path's credential / health / fail-closed governance lives in CSP, not in this compose.

---

## Key environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `ANILA_HF_DIR` | `../../models/model` | weights root (relative to this compose file) |
| `ANILA_MODELSRC_DIR` | `./src` | config / build contexts for non-FLUX services |
| `VLLM_IMAGE` | `vllm/vllm-openai:v0.22.1-cu129-ubuntu2404` | generic vLLM image for the intranet profile |
| `GEMMA_A4B_GPU` / `GEMMA_12B_GPU` / `GPT_OSS_120B_GPU` | `1` / `2` / `3` | per-model GPU for the intranet profile |
| `INTERNAL_PLATFORM_API_KEY` | (required) | injected as `flux2-dev-agent`'s `CSP_API_KEY` (translation callback; fail-loud if missing) |

---

## Related docs

- FLUX services: [`services/flux2-dev`](../../services/flux2-dev/README.en.md), [`services/flux2-dev-agent`](../../services/flux2-dev-agent/README.en.md) · FLUX spec & licensing: [`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md) (§9 licensing landmines: FLUX.2-dev is BFL Non-Commercial, klein-4B is the clean Apache-2.0 alternative)
- Redesign docs: [`04-model-gateway-design.md`](../../docs/anila-redesign-docs/04-model-gateway-design.md), [`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)
- Deploy scripts: `infra/deployment/intranet/model-serve.sh` (model lifecycle), `infra/deployment/scripts/deploy-prod.sh` (platform lifecycle) · Platform overview: [`../../README.md`](../../README.md)

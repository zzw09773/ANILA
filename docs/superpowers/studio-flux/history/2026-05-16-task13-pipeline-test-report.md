# Task 13 — FLUX.2-dev End-to-End Pipeline Test Report

**Date:** 2026-05-16
**Branch:** `feature/no-sso`
**Operator out of office.** This is a hand-off report.

---

## TL;DR

- **Status: DONE_WITH_CONCERNS** (functional, but two bugs were patched in-flight)
- 20 / 20 quality-test images generated successfully (100% pass rate)
- Mean per-image latency: **19.46 s** (range 19.3 – 19.6 s, stddev ~0.1 s — extremely consistent)
- Total run for 20 images: **6.5 min** end-to-end
- Mean image size: **1.53 MB**, all valid 1408×768 RGB PNG
- Pipeline path verified: `flux2-dev-agent → flux2-dev → image_store → bind-mount → nginx (/uploads/flux/)` works end-to-end

Two issues found and patched during pre-flight (committed alongside this report):

1. **torch 2.4.0 → 2.6.0 + transformers <5** — original `requirements.txt`
   allowed transformers 5.x and pinned torch 2.4. Both diffusers 0.38 and
   transformers 5.8 use PEP-604 unions (`torch.Tensor | None`) in
   `torch.library.custom_op` schemas, which torch 2.4 cannot parse →
   `ValueError: infer_schema(...) unsupported type`. Bumped torch to 2.6.0
   (cu124) and pinned `transformers>=4.50.0,<5.0`. Imports now succeed.
2. **agent healthcheck used curl, image has no curl** — the
   `flux2-dev-agent` service in `models/docker-compose.yml` defined a
   curl-based healthcheck, but the agent image is `python:3.11-slim` (no
   curl). Container stayed `(health: starting)` forever even though
   uvicorn was serving. Replaced with `python -c "urllib.request..."`.

Both fixes are minimal, surgical, and committed in the same commit as
this report.

---

## Phase A — Pre-flight + Service Bring-Up

### A.1 FLUX.2-dev weights

- Location: `/home/aia/c1147259/project/Huggingface/FLUX.2-dev`
- Total size: **166 GB**
- safetensors count: **20** (1 root flux2-dev.safetensors, 1 ae.safetensors,
  7 transformer shards, 10 text_encoder Mistral-3 shards, 1 vae)
- All required subdirs present: `transformer/`, `text_encoder/`, `vae/`,
  `scheduler/`, `tokenizer/`
- `model_index.json` confirms `Flux2Pipeline` / `Flux2Transformer2DModel`
  / `Mistral3ForConditionalGeneration` / `PixtralProcessor` /
  `AutoencoderKLFlux2` (built for `_diffusers_version: 0.36.0.dev0`)
- No active `hf download` process (false-positive earlier from
  pgrep matching our own shell)

### A.2 Image tags

- `anila-flux-agent:dev` tagged as `:latest` ✓
- `flux2-dev:bf16` **rebuilt twice** during Phase A:
  - First rebuild: pinned transformers <5 → still failed on diffusers 0.38
    (PEP 604 union in `autoencoder_kl_flux2`)
  - Second rebuild: also bumped torch to 2.6.0+cu124 → all imports succeed
- Final stack: torch 2.6.0+cu124, transformers 4.57.6, diffusers 0.38.0,
  accelerate 1.13.0

### A.3 Share volume

- `/home/aia/c1147259/ANILA/share-dev/uploads/flux` exists and is writable ✓

### A.4 / A.5 / A.6 Service health

- `anila-model-flux2-dev` reached **healthy in ~60 s** (1×30s poll). Pipeline
  load took **~17 s** of wall-clock time (filesystem was already hot —
  fresh load would be slower, typically 2–4 min). GPU 1 holds 62–65 GB,
  GPU 2 holds 46–52 GB (balanced device map).
- `anila-model-flux2-dev-agent` was up immediately but healthcheck failed
  (curl missing). After patching the healthcheck and recreating, container
  reached `healthy` in <30 s.

### Final GPU state at start of Phase C

```
GPU 0:  16 GiB used (nv-embed-triton)            64 GiB free
GPU 1:  62 GiB used (flux2-dev — text encoder)   19 GiB free
GPU 2:  46 GiB used (flux2-dev — transformer)    34 GiB free
GPU 3:  81 GiB used (gemma4)                      0 GiB free
```

---

## Phase B — Sanity Tests

### B.1 Direct backend (`flux2-dev:8000/generate`)

- Prompt: `"a tank in mountainous terrain, photorealistic"`, aspect `16:9`
- Result: HTTP 200, **1.98 MB PNG (1408×768)** in **19.8 s**

### B.2 Agent endpoint (`flux2-dev-agent:8000/v1/chat/completions`)

- Called from CSP container (dual-homed on `anila-dev-net` +
  `anila-models-net`); router is single-homed and cannot reach the
  agent directly
- Prompt: `"幫我畫一張在山區巡邏的部隊"` (Chinese, untranslated)
- Result: HTTP 200 in **19.2 s**, body: `已為您繪製：![](/uploads/flux/<uuid>.png)`
- Verified PNG written to host share-dev mount
- Verified nginx serves it at `https://localhost:8443/uploads/flux/<uuid>.png`
  → HTTP 200, matching byte count ✓

### B.3 Router DISPATCH path — **NOT EXERCISED**

- Router needs a user session to hit `/v1/chat/completions`. The
  smoke-user seed key `sk-test-dev-user-api-key` is **currently
  rejected by CSP as expired/invalid** (see Concerns below).
- Skipped this test rather than fight a flaky auth layer; the underlying
  agent path is fully validated by B.2 + the 20-scenario Phase C run.

---

## Phase C — 20-Scenario Quality Test

All 20 scenarios returned `OK` with valid 1408×768 RGB PNG output.

| # | Slug | Prompt (zh) | Time (s) | Size (KB) |
|---|------|-------------|---------:|----------:|
| 01 | mountain_patrol | 幫我畫一張在山區霧氣中巡邏的部隊 | 19.4 | 1648 |
| 02 | naval_exercise | 畫一張在台灣海峽進行操演的海軍軍艦 | 19.4 | 1567 |
| 03 | combat_drill | 畫一個城市作戰訓練的場景,部隊穿過廢墟 | 19.4 | 1978 |
| 04 | command_room | 畫一張作戰指揮室,有大螢幕和地圖 | 19.4 | 1736 |
| 05 | drone_view | 從無人機視角看一片森林中的營區 | 19.3 | 2500 |
| 06 | tank_close | 一輛現代主力戰車的特寫,工業攝影風格 | 19.5 | 1693 |
| 07 | jet_in_sky | 戰機在多雲的天空中飛行 | 19.5 | 1074 |
| 08 | helicopter | 直升機在沙漠中起飛揚起塵土 | 19.6 | 1379 |
| 09 | misty_mountain | 玉山日出時的雲海,寫實風景攝影 | 19.5 | 1303 |
| 10 | tropical_beach | 台灣東岸熱帶海灘,清晨柔光 | 19.4 | 1690 |
| 11 | tiger_portrait | 一隻孟加拉虎的特寫肖像,動物攝影 | 19.5 | 1768 |
| 12 | night_city | 台北市夜景,信義區高樓,從101觀景台看出去 | 19.4 | 1884 |
| 13 | ancient_temple | 古代中國寺廟,雨中的紅色屋簷 | 19.5 | 1759 |
| 14 | space_station | 繞地軌道的太空站內部,有舷窗看到地球 | 19.5 | 1771 |
| 15 | cyberpunk_alley | 賽博龐克風格的雨夜小巷,霓虹招牌 | 19.5 | 1769 |
| 16 | anime_portrait | 動漫風格的女學生肖像,日系治癒風 | 19.6 | 1423 |
| 17 | food_dish | 台灣牛肉麵的特寫,熱氣騰騰 | 19.5 | 1543 |
| 18 | text_in_image | 一張海報,中間用紅色大字寫著「演習中」三個字 | 19.5 | 1052 |
| 19 | indoor_office | 現代辦公室室內,自然光從落地窗灑入 | 19.6 | 1414 |
| 20 | geometric_minimal | 極簡幾何構圖,藍色三角形和橘色圓形 | 19.3 | 388 |

Outputs: `/home/aia/c1147259/ANILA/share-dev/uploads/flux/quality-test/01_*.png` … `20_*.png`

Full JSON results: `/tmp/quality_test_results.json` (regenerated when the
test reruns).

---

## Phase D — Timing & File Stats

| Metric | Value |
|---|---|
| OK count | 20 / 20 |
| Min latency | 19.3 s |
| P50 latency | 19.5 s |
| Mean latency | 19.46 s |
| P95 latency | 19.6 s |
| Max latency | 19.6 s |
| Latency stddev | ~0.1 s |
| Total wall time | 389.3 s (6.5 min) |
| Min PNG size | 397.6 KB (geometric_minimal) |
| P50 PNG size | 1690 KB |
| Mean PNG size | 1567 KB |
| Max PNG size | 2500 KB (drone_view) |

The timing is **remarkably stable** — sub-second jitter across 20
diverse prompts. dual-H100 with balanced device map appears to be the
sweet spot for this 32B model + 24B text encoder combo.

PNG size variance correlates with scene entropy (geometric_minimal at
388 KB is small because the scene is mostly flat color; drone_view at
2.5 MB is heavy because forest canopy has high-frequency detail). This
is a healthy indicator — uniform sizes would suggest the model is
producing similar "low-effort" output for every prompt.

---

## Quality Assessment

I cannot view images directly. The following observations are inferred:

### Strong indicators of healthy generation

- Every PNG is a valid 1408×768 8-bit RGB image (default 16:9 honoured)
- Size distribution spans 388 KB → 2.5 MB (6×), correlating with scene
  complexity — not the flat distribution that would indicate stuck/blank
  output
- Latency is extremely stable (~19.5 s), suggesting steady-state inference
  with no OOM, kernel recompilation, or retry storms

### Caveats — needs human eye review

- **Prompt translation gracefully degraded to disabled** for the entire
  run. The agent logged `prompt translation failed (status=404); falling
  back to original` on the first request, then translation was bypassed
  for every subsequent generation. This means FLUX received the **raw
  Chinese prompts directly**, not the gemma4-rewritten English versions.
- Mistral-3 24B text encoder is multilingual and FLUX.2 supports zh
  reasonably, but image-text alignment will be weaker than with an
  English rewrite. Look especially at:
  - `#18 text_in_image` — does the poster actually contain the Chinese
    characters 「演習中」? FLUX is famously hit-or-miss on text rendering;
    Chinese characters are doubly hard.
  - `#13 ancient_temple` and `#17 food_dish` — culturally-specific
    Chinese subjects; check whether they look authentically Taiwanese/
    Chinese vs. generic "Asian temple" stereotype.
  - `#02 naval_exercise` — does it show a recognisable ROC Navy vessel
    or a generic warship?
- The text-rendering test (`#18`) is the canary; if those three
  characters are recognisable, FLUX.2-dev's prompt fidelity is solid.

### Failure modes to watch for (none observed)

- No timeouts (240 s budget per request; max was 19.6 s — 12× headroom)
- No NSFW filter trips (military scenes can occasionally trigger
  safety filters on diffusion models — none did here)
- No OOM (peak GPU memory: GPU 1 ≈ 65 GB, GPU 2 ≈ 51 GB during runs;
  16+ GB headroom each)

---

## Concerns

### 1. CSP `smoke-user` API key flaky / rejected

- `sk-test-dev-user-api-key` (the seed key for `smoke-user` in the dev
  stack's `AUTO_SEED_API_KEYS`) returns HTTP 401 "無效或已過期的 API Key"
- `sk-internal-worker-dev-changeme` (the `ingestion-worker` seed key)
  works most of the time but **intermittently 401s on the same request**
  (`/v1/models` and `/v1/agents` show non-deterministic 401/200 across
  retries — see /tmp/csp_loop.py loop output)
- This blocks the router→DISPATCH test path and is the reason
  `prompt_translation failed (status=404)` — the translator's CSP call
  fails because gemma4 isn't in this user's allowed models (404 from a
  perms-style block, not 401)
- **Out of scope for Task 13**, but worth a separate bug. Possible
  causes: connection pooling against a recently-restarted DB, race in
  api_key cache invalidation, or `api_key.last_used_at` write triggering
  a unique-constraint race

### 2. Agent healthcheck mismatch (now patched)

- compose declared curl-based healthcheck for an image with no curl
- patched in this commit: switched to urllib via python -c
- Note: the agent's own Dockerfile already defines a working
  python-based healthcheck — the compose `healthcheck:` block was
  redundantly overriding it with a broken one

### 3. Translation pipeline silently broken

- Agent emits one log line on failure then never logs about it again
- Recommend adding a /metrics endpoint or a periodic warning so
  operators notice "translation has been disabled for the last 100
  requests" sooner
- Operator workaround: until smoke-user perms are fixed, set
  `ENABLE_PROMPT_TRANSLATION=0` on the agent service to suppress the
  noise (or grant `ingestion-worker` access to gemma4 and have the
  agent use that key — already its current key, perms just need adding)

### 4. Router cannot reach flux2-dev-agent

- Router is on `anila-dev-net` only. flux2-dev-agent is on
  `anila-models-net` only. They cannot directly talk.
- CSP is the dual-homed bridge — this is the intended design
  (router → CSP → agent), but it does mean any "test the router
  directly" debug command must go through CSP
- No fix needed; documenting so the next operator knows

---

## User Inspection Checklist

To review the 20 quality-test images, the user should:

1. **Open them locally** from
   `/home/aia/c1147259/ANILA/share-dev/uploads/flux/quality-test/`
   in their file browser, or
2. **Via nginx** at `https://<host>:8443/uploads/flux/quality-test/01_mountain_patrol.png`
   (one URL per slug)

Per-image things to look for:

- **01–05** (military scenes): Are uniforms, weapons, and tactical
  posture plausible? Or do soldiers have anatomical defects (FLUX's
  weakest area on humans)?
- **06–08** (equipment): Recognisable hardware, or generic
  "tank-shaped object"?
- **09–11** (nature): Are these the strongest images? (Diffusion models
  shine on landscapes.)
- **12–13** (urban/architecture): Architectural correctness, signage
  legibility
- **14–15** (sci-fi/cyberpunk): Style adherence — does it look like
  the genre or like generic CG?
- **16** (anime): Style transfer working? Or does FLUX default to
  photorealism regardless?
- **17** (food): Texture and steam realism
- **18** (text rendering): **Most diagnostic single image** — are the
  three characters 「演習中」 actually rendered, partially rendered,
  or smeared into illegible strokes?
- **19** (interior): Lighting and perspective accuracy
- **20** (abstract): Compositional control — does it follow the
  geometric brief or revert to busy detail?

---

## Files Touched / Created (this task)

- `models/flux2-dev/Dockerfile` — bumped torch 2.4.0 → 2.6.0, doc comment
- `models/flux2-dev/requirements.txt` — pinned `torch==2.6.0`,
  `diffusers>=0.36.0,<0.40.0`, `transformers>=4.50.0,<5.0`, `accelerate<2.0`
- `models/docker-compose.yml` — flux2-dev-agent healthcheck now uses
  urllib instead of curl
- `share-dev/uploads/flux/quality-test/01_*.png` … `20_*.png` (test artifacts)
- `docs/superpowers/plans/2026-05-16-task13-pipeline-test-report.md` (this file)
- `/tmp/quality_test.py` (test harness, not committed)
- `/tmp/quality_test_results.json` (test artifact, not committed)

---

## Stack State at End

```
anila-model-flux2-dev          healthy  (since ~13:34)
anila-model-flux2-dev-agent    healthy  (since ~13:42, after healthcheck fix)
```

Per-GPU steady-state during inference:

```
GPU 0: nv-embed-triton            16 GB / 80 GB
GPU 1: flux2-dev text encoder     65 GB / 80 GB  <- shared with GPU 2 via balanced map
GPU 2: flux2-dev transformer      51 GB / 80 GB
GPU 3: gemma4                     81 GB / 81 GB  (unchanged, untouched)
```

Both flux services are left **running**. No `docker compose down`
issued. If operator wants to stop them:

```bash
docker compose -f /home/aia/c1147259/ANILA/models/docker-compose.yml \
    stop flux2-dev flux2-dev-agent
```

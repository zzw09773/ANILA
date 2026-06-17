# ANILA models — 推論模型獨立 compose（FLUX 圖像生成）

> 與平台 stack 生命週期解耦的獨立 compose project（`name: anila-models`），收容 ANILA 的推論工作負載；本文件聚焦本地 FLUX.2-dev 圖像生成（air-gapped、僅內網可達）。

> 中文為主、English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼維持英文。

> 🌿 **分支對照**：`models/` 存在於所有 ANILA 部署分支（內容跨分支一致）。模型 stack 與平台 stack 拆 lifecycle，跨 stack 走 external network `anila-models-net`。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

`models/` 是獨立 compose project（`name: anila-models`），與圖像生成相關的兩個服務：

- **`flux2-dev`** — FLUX.2-dev 文生圖推論服務。以 `diffusers` 的 `Flux2Pipeline` 包成極簡 HTTP server（`server.py`），對外 `POST /generate`。輸入 `prompt / aspect_ratio / seed / ...`，回 JSON（base64 PNG list + audit meta）。
- **`flux2-dev-agent`** — agent 包裝層（OpenAI `/v1/chat/completions` 相容）。router / CSP 把圖像生成請求轉送到這裡；它負責 prompt 翻譯（zh→en，透過 gemma4）、呼叫 `flux2-dev`、把 PNG 落地到 share volume，再以 Markdown 圖片連結回前端。

資料流：

```
使用者聊天 → router → DISPATCH:image-generator
          → CSP proxy → flux2-dev-agent  (OpenAI chat 相容)
                          → (prompt 翻譯 via gemma4)
                          → flux2-dev  POST /generate  (FLUX 推論)
                          → 落地 PNG 到 /share/flux
                          ← Markdown 圖片連結
```

前端 UX 不直接看到 `flux2-dev`；只有 agent shim 是它的 client。

---

## 架構與技術棧

### flux2-dev（推論服務）

- **語言/框架**：Python 3.11、FastAPI + uvicorn。
- **推論後端**：`diffusers` 的 `Flux2Pipeline`（`diffusers>=0.36,<0.40`）、`transformers>=4.50,<5.0`、`accelerate`。
- **精度與多卡**：`torch_dtype=torch.bfloat16`（BF16），`device_map="balanced"`（權重分片到多 GPU）。
- **基底映像**：`nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`，`torch==2.6.0`（cu124 wheel）。
- **離線**：`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，權重以唯讀 volume mount，不連 HF。

`POST /generate` 合約（對應 spec §3.2，model-agnostic）：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `prompt` | `str` | 必填 |
| `aspect_ratio` | `Literal["1:1","16:9","9:16","4:3","3:4","3:1"]` | 預設 `16:9` |
| `seed` | `int \| None` | `None` = 隨機（回傳實際值供 audit） |
| `num_candidates` | `int (1–4)` | 預設 `1`，回傳一律為 list |
| `num_inference_steps` | `int \| None` | `None` = env `FLUX_NUM_STEPS`（預設 28） |
| `guidance_scale` | `float \| None` | `None` = env `FLUX_GUIDANCE_SCALE`（預設 4.0） |

回傳 `GenerateResponse`：

```json
{"images": ["<base64 PNG>", "..."], "seed": 123456,
 "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}}
```

設計重點：一律回 `list[str]`（即使 `num_candidates=1`）；aspect ratio 對應固定解析度（含 `3:1` letterbox，≤0.8MP FLUX 穩定區）；seed 用 **CPU generator**（`device_map="balanced"` 權重跨卡，固定 cuda device 不安全）；`GET /health` → `{"status":"ok"}`；`FLUX_SKIP_LOAD=1` 走 stub pipeline（不載真權重、無需 GPU），供整合 smoke test。

### flux2-dev-agent（agent 包裝層）

- **語言/框架**：Python 3.11、FastAPI + uvicorn、httpx；基底 `python:3.11-slim`（無 GPU，healthcheck 用 urllib）。
- **對外端點**：`GET /health`、`GET /v1/models`（回 `image-generator`）、`POST /v1/chat/completions`（JSON + SSE）。
- **四個協作元件**：`prompt_translator.py`（CSP proxy 呼 gemma4 改寫中文 → FLUX 友善英文，任何錯誤 fallback 原文）、`flux_client.py`（呼 `flux2-dev` `/generate` 解第一張 PNG）、`image_store.py`（校驗 PNG magic bytes、寫 share volume、回 public URL）、`chat_handler.py`（串接前三者，組 OpenAI 形狀回應 `已為您繪製：\n\n![](url)`）。

---

## 目錄結構

```
models/
├── docker-compose.yml          # anila-models project（含 LLM/embedding/FLUX 服務）
├── flux2-dev/                  # FLUX.2-dev 推論服務
│   ├── Dockerfile              # CUDA 12.4 base + torch 2.6 + diffusers
│   ├── requirements.txt · pyproject.toml
│   ├── server.py               # build_app + /generate + /health + pipeline 載入
│   └── tests/                  # pytest（以 mock pipeline 注入，不需 GPU）
└── flux2-dev-agent/            # agent 包裝層（OpenAI 相容）
    ├── Dockerfile              # python:3.11-slim
    ├── app/{main,schemas,prompt_translator,flux_client,image_store,chat_handler}.py
    └── tests/                  # pytest（pytest-asyncio + respx）
```

> 同一 `docker-compose.yml` 還定義 `gpt-oss-20b` / `gemma4` / `nv-embed-triton` / `nv-embed-proxy` 等非圖像服務，本文件僅聚焦 FLUX 兩個服務。
>
> `models/` 下另有 `inference/`（TensorRT-LLM / Triton 的本地建置脈絡與壓測 log）與 `model/`（HuggingFace 權重 symlink），兩者 git 未追蹤、不屬 FLUX 範圍，本文件不展開。

---

## 啟動與部署

所有服務只用 `expose:`（內網），**不開 host port**；CSP 走共用 external network `anila-models-net` 以 DNS 連接。

```bash
# 一次性 bootstrap
docker network create anila-models-net
docker compose -f docker-compose.yml restart csp        # CSP 加入該網路

# 日常操作
docker compose -f models/docker-compose.yml up -d
docker compose -f models/docker-compose.yml logs -f flux2-dev
docker compose -f models/docker-compose.yml restart flux2-dev-agent
docker compose -f models/docker-compose.yml down         # 只動模型，平台不受影響
```

GPU / 資源（取自 compose）：`flux2-dev` GPU `["1","2"]`、`shm_size: 32g`、`ipc: host`、healthcheck `/health`（`start_period: 300s`）；`flux2-dev-agent` 無 GPU，`depends_on: flux2-dev (service_healthy)`。

關鍵環境變數：

| 服務 | 變數 | 預設 / 值 | 說明 |
|------|------|-----------|------|
| flux2-dev | `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | 權重路徑 |
| flux2-dev | `FLUX_DEVICE_MAP` | `balanced` | 多卡分片 |
| flux2-dev | `FLUX_NUM_STEPS` | `28` | inference steps |
| flux2-dev | `FLUX_GUIDANCE_SCALE` | `3.5`（compose）/ `4.0`（程式碼 fallback） | guidance |
| flux2-dev | `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |
| flux2-dev-agent | `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | 後端推論服務 |
| flux2-dev-agent | `CSP_BASE_URL` / `CSP_API_KEY` | `http://csp:8000` / env | 翻譯 callback 走 CSP |
| flux2-dev-agent | `GEMMA_MODEL` / `ENABLE_PROMPT_TRANSLATION` | `gemma4` / `1` | 翻譯用 LLM / 關閉則直送原文 |
| flux2-dev-agent | `SHARE_DIR` / `PUBLIC_URL_PREFIX` | `/share/flux` / `/uploads/flux` | 落地路徑與對外 URL |
| flux2-dev-agent | `DEFAULT_ASPECT_RATIO` / `FLUX_TIMEOUT_SECONDS` | `16:9` / `240`（程式碼 fallback `180`，compose 蓋成 240） | 預設長寬比 / 後端逾時 |

> 另：`flux2-dev` 還有 `FLUX_MODEL_SHA`（預設 `""`，寫進 `meta.model_sha`）與 `FLUX_SKIP_LOAD=1`（走 stub pipeline、不載權重、無需 GPU，供整合 smoke test）；`FLUX_MAX_CONCURRENT`(4) 由上層 csp/studio 控、不在本服務。compose 中 `flux2-dev` 綁 GPU `["1","2"]`、`gpt-oss-20b` 綁 `["2"]`——兩者共用 GPU 2，部署時留意顯存。

**Air-gapped 權重**：FLUX.2-dev 權重以唯讀 volume mount（`.../FLUX.2-dev:/workspace/model/FLUX.2-dev:ro`）+ `HF_HUB_OFFLINE=1`，容器不連外抓權重。`flux2-dev-agent` 把 host 的 `share-dev/uploads/flux` bind 到 `/share/flux`；nginx 在 `/uploads/flux/` 對外服務（前端圖片連結指向此）。

---

## 測試

兩個服務皆有 pytest 套件，**完全不需 GPU**（mock pipeline 注入 / respx 攔截 HTTP），可在 dev box 直接跑：

```bash
# flux2-dev（conftest 自動設 FLUX_SKIP_LOAD=1，不載權重）
cd models/flux2-dev       && pip install -e '.[test]' && pytest

# flux2-dev-agent（pytest-asyncio + respx；asyncio_mode=auto）
cd models/flux2-dev-agent && pip install -e '.[test]' && pytest
```

> 測試相依（pinned）見各自 `pyproject.toml` 的 `[project.optional-dependencies].test`；torch / diffusers **不在** test deps，CI/dev 不必裝 GPU stack。

---

## 與其他服務的關係

- 平台 router 在 gemma4 發 `DISPATCH:image-generator:...` 時，由 **CSP proxy** 把請求轉送 `flux2-dev-agent`（CSP `model_registry` 註冊為 `model_type=agent`）。
- `flux2-dev-agent` 透過 `FLUX_BACKEND_URL` 直連 `flux2-dev` `/generate`；CSP 的 studio 也循同路徑直打 `flux2-dev`。
- 三者共用 external network `anila-models-net`，僅 container-to-container DNS 可達；host port 完全沒開。
- 翻譯 callback：agent 透過 `CSP_BASE_URL` + `CSP_API_KEY` 呼 CSP `/v1/chat/completions`（model=`gemma4`）把口語改寫成英文 prompt。

---

## 相關文件

- FLUX 規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)（§3.2 `/generate` 合約、§9 授權雷區）
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

---

## 授權注意（重要）

依 spec §9：

- **`black-forest-labs/FLUX.2-dev` 為 BFL Non-Commercial License**，內部商用屬灰色地帶 — **開工 / 上 production 前請法務確認**「公司內部工具」是否落在授權範圍。
- **`FLUX.2-klein-4B` 為 Apache-2.0**，是法律上最乾淨的本地替代（畫質略降、延遲更低）。若法務認定 dev 版內部商用有疑慮，**主力應換 klein-4B**。
- `FLUX.2-klein-9B` 與 dev 同為 Non-Commercial，不解決授權問題。

compose 內 `flux2-dev` 服務上方亦有對應 caveat 註解。

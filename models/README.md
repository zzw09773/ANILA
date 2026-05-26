# ANILA models — FLUX 圖像生成模型服務

> ANILA 平台的本地 FLUX.2-dev 圖像生成模型服務（air-gapped、僅內網可達）。

> 中文為主、英文鏡像見 [`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律維持英文。

> 📌 **此檔屬 `prod` 分支(中科院內網部署版)**。`models/docker-compose.yml` 內 flux2-dev-agent 的 `CSP_API_KEY` / `volumes` 已對齊 prod 慣例(`${INTERNAL_PLATFORM_API_KEY:?must be set}` + `/share/uploads/flux`,不走 `_DEV` fallback 或 `share-dev/`)。

---

## 簡介 / Overview

`models/` 是與平台 stack 生命週期解耦的獨立 compose project（`name: anila-models`），收容 ANILA 的推論工作負載。其中與圖像生成相關的有兩個服務：

- **`flux2-dev`** — FLUX.2-dev 文生圖推論服務。以 `diffusers` 的 `Flux2Pipeline` 包成一個極簡 HTTP server（`server.py`），對外提供 `POST /generate`。輸入 `prompt / aspect_ratio / seed / ...`，回傳 JSON（base64 PNG list + audit meta）。
- **`flux2-dev-agent`** — agent 包裝層（OpenAI `/v1/chat/completions` 相容）。平台的 router / CSP 把圖像生成請求轉送到這裡；它負責 prompt 翻譯（zh→en，透過 gemma4）、呼叫 `flux2-dev`、把產出的 PNG 落地到 share volume，再以 Markdown 圖片連結回給前端。

兩者關係（資料流）：

```
使用者聊天 → router → DISPATCH:image-generator
          → CSP proxy → flux2-dev-agent  (OpenAI chat 相容)
                          → (prompt 翻譯 via gemma4)
                          → flux2-dev  POST /generate  (FLUX 推論)
                          → 落地 PNG 到 /share/flux
                          ← Markdown 圖片連結
```

前端 UX 不會直接看到 `flux2-dev`；只有 agent shim 是它的 client。

---

## 架構與技術棧 / Architecture & Stack

### flux2-dev（推論服務）

- **語言/框架**：Python 3.11、FastAPI + uvicorn。
- **推論後端**：`diffusers` 的 `Flux2Pipeline`（`diffusers>=0.36,<0.40`）、`transformers>=4.50,<5.0`、`accelerate`。
- **精度與多卡**：`torch_dtype=torch.bfloat16`（BF16），`device_map="balanced"`（權重分片到多張 GPU）。
- **基底映像**：`nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04`，`torch==2.6.0`（cu124 wheel）。
- **離線**：`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，權重以唯讀 volume mount 進容器，不連 HF。

`POST /generate` 合約（對應 spec §3.2，model-agnostic，flux2-dev 與 klein-4B 共用）：

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
{
  "images": ["<base64 PNG>", "..."],
  "seed": 123456,
  "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}
}
```

設計重點：

- **一律回 list[str]**（即使 `num_candidates=1`），避免 Stage 2 的 N 候選 gate 改寫回傳型別。
- aspect ratio 對應固定解析度（`_ASPECT_RATIOS`），含 `3:1` section band letterbox（≤0.8MP，FLUX 穩定區）。
- seed 使用 **CPU generator**（`torch.Generator(device="cpu")`），因 `device_map="balanced"` 權重跨卡，固定 cuda device 不安全；CPU generator 給確定性、與裝置無關的種子。
- `GET /health` → `{"status": "ok"}`。
- `FLUX_SKIP_LOAD=1` 走 stub pipeline（不載真權重、無需 GPU），供整合 smoke test。

### flux2-dev-agent（agent 包裝層）

- **語言/框架**：Python 3.11、FastAPI + uvicorn、httpx（非同步）。
- **基底映像**：`python:3.11-slim`（無 GPU、無 curl，healthcheck 用 urllib）。
- **對外端點**：`GET /health`、`GET /v1/models`（回 `image-generator`）、`POST /v1/chat/completions`（支援 JSON 與 SSE streaming）。
- **四個協作元件**：
  - `prompt_translator.py` — 透過 CSP proxy 呼叫 `gemma4` 把口語中文改寫成 FLUX 友善英文 prompt；任何錯誤都 **fallback 原文**（部分降級優於完全失敗）。
  - `flux_client.py` — 呼叫 `flux2-dev` 的 `/generate`，解出第一張候選 PNG bytes。
  - `image_store.py` — 校驗 PNG magic bytes、寫入 share volume、回傳 public URL。
  - `chat_handler.py` — 串接以上三者，組出 OpenAI 形狀的 `ChatCompletionResponse`，content 為 `已為您繪製：\n\n![](url)`。

---

## 目錄結構 / Layout

```
models/
├── docker-compose.yml          # anila-models compose project（含 LLM/embedding/FLUX 服務）
├── flux2-dev/                  # FLUX.2-dev 推論服務
│   ├── Dockerfile              # CUDA 12.4 base + torch 2.6 + diffusers
│   ├── requirements.txt        # fastapi / diffusers / transformers / accelerate ...
│   ├── pyproject.toml          # 測試以 mock pipeline 注入，不需 GPU stack
│   ├── server.py               # build_app + /generate + /health + pipeline 載入
│   └── tests/                  # pytest（test_server.py, conftest.py）
└── flux2-dev-agent/            # agent 包裝層（OpenAI 相容）
    ├── Dockerfile              # python:3.11-slim
    ├── requirements.txt        # fastapi / httpx / pydantic / python-multipart
    ├── pyproject.toml
    ├── app/
    │   ├── main.py             # build_app + 端點（/health, /v1/models, /v1/chat/completions）
    │   ├── schemas.py          # OpenAI chat 相容 pydantic models
    │   ├── prompt_translator.py
    │   ├── flux_client.py
    │   ├── image_store.py
    │   └── chat_handler.py
    └── tests/                  # pytest（pytest-asyncio + respx）
```

> 註：同一個 `docker-compose.yml` 還定義了 `gpt-oss-20b`、`gemma4`、`nv-embed-triton`、`nv-embed-proxy` 等非圖像服務，本文件僅聚焦 FLUX 兩個服務。

---

## 啟動與部署 / Setup & Run

這些服務透過 `models/docker-compose.yml` 啟動。所有服務只用 `expose:`（內網），**不開 host port**；外部 NIC 無法直接連到，CSP 走共用的 external docker network `anila-models-net` 以 DNS 連接。

一次性 bootstrap（host 上）：

```bash
docker network create anila-models-net
docker compose -f docker-compose.yml restart csp   # CSP 加入該網路
```

日常操作：

```bash
docker compose -f models/docker-compose.yml up -d
docker compose -f models/docker-compose.yml logs -f flux2-dev
docker compose -f models/docker-compose.yml ps
docker compose -f models/docker-compose.yml restart flux2-dev-agent
docker compose -f models/docker-compose.yml down          # 只動模型,平台不受影響
```

GPU / 資源配置（取自 compose）：

- `flux2-dev`：GPU `["1","2"]`、`shm_size: 32g`、`ipc: host`、healthcheck 打 `/health`（`start_period: 300s`）。
- `flux2-dev-agent`：無 GPU，`depends_on: flux2-dev (service_healthy)`。

關鍵環境變數：

| 服務 | 變數 | 預設 / 值 | 說明 |
|------|------|-----------|------|
| flux2-dev | `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | 權重路徑 |
| flux2-dev | `FLUX_DEVICE_MAP` | `balanced` | 多卡分片 |
| flux2-dev | `FLUX_NUM_STEPS` | `28` | inference steps 預設 |
| flux2-dev | `FLUX_GUIDANCE_SCALE` | `3.5`（compose）/ `4.0`（程式碼 fallback） | guidance 預設 |
| flux2-dev | `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |
| flux2-dev-agent | `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | 後端推論服務 |
| flux2-dev-agent | `CSP_BASE_URL` / `CSP_API_KEY` | `http://csp:8000` / env | 翻譯 callback 走 CSP |
| flux2-dev-agent | `GEMMA_MODEL` | `gemma4` | 翻譯用 LLM |
| flux2-dev-agent | `ENABLE_PROMPT_TRANSLATION` | `1` | 關閉則直送原文 |
| flux2-dev-agent | `SHARE_DIR` / `PUBLIC_URL_PREFIX` | `/share/flux` / `/uploads/flux` | 落地路徑與對外 URL |
| flux2-dev-agent | `DEFAULT_ASPECT_RATIO` | `16:9` | 預設長寬比 |
| flux2-dev-agent | `FLUX_TIMEOUT_SECONDS` | `240` | 呼叫後端逾時 |

**Air-gapped 權重處理**：FLUX.2-dev 權重以唯讀 volume mount 進容器
（`/home/aia/c1147259/project/Huggingface/FLUX.2-dev:/workspace/model/FLUX.2-dev:ro`），
搭配 `HF_HUB_OFFLINE=1`，容器不會連外抓權重。

`flux2-dev-agent` 把 host 的 `share-dev/uploads/flux` bind 到容器 `/share/flux`；此目錄同時由 nginx 在 `/uploads/flux/` 對外服務（前端圖片連結即指向此）。

---

## 與其他服務的關係 / Integration

- 平台 router 在 gemma4 發出 `DISPATCH:image-generator:...` 時，由 **CSP proxy** 把請求轉送到 `flux2-dev-agent`（CSP `model_registry` 把 agent 註冊為 `model_type=agent`）。
- `flux2-dev-agent` 透過 `FLUX_BACKEND_URL` 直連 `flux2-dev` 的 `/generate`；CSP 的 studio 也可循同一路徑直接打 `flux2-dev`。
- 三者共用 external docker network `anila-models-net`，僅 container-to-container DNS 可達；host port 完全沒開。
- 翻譯 callback：agent 透過 `CSP_BASE_URL` + `CSP_API_KEY` 呼叫 CSP 的 `/v1/chat/completions`（model=`gemma4`），把使用者口語改寫成英文 prompt。

---

## 相關文件 / Related docs

- FLUX 規格：[`docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)（已確認存在）。
  - §3.2 定義 `/generate` 合約（本服務據此實作）。
  - §9 為授權雷區。

---

## 授權注意 / Licensing note

依 spec §9：

- **`black-forest-labs/FLUX.2-dev` 為 BFL Non-Commercial License**，內部商用屬灰色地帶——**開工 / 上 production 前請法務確認**「公司內部工具」是否落在授權範圍。
- **`FLUX.2-klein-4B` 為 Apache-2.0**，是法律上最乾淨的本地替代（畫質略降、延遲更低）。若法務認定 dev 版內部商用有疑慮，**主力應換 klein-4B**。
- `FLUX.2-klein-9B` 與 dev 同為 Non-Commercial，不解決授權問題。

compose 內 `flux2-dev` 服務上方亦有對應 caveat 註解：啟用 production 前先確認授權。

---

**Last updated**: 2026-05-26(同步 PR #16 + 加 prod banner;compose 已從 dev fallback 改回 prod fail-loud)

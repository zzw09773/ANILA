# flux2-dev

> **FLUX.2-dev 文生圖推論服務** — 把 `diffusers` 的 `Flux2Pipeline` 包成一支極簡 HTTP server（`server.py`），對外只有 `POST /generate`、`POST /v1/images/generations`（OpenAI 相容）與 `GET /health`。air-gapped、僅內網可達（不開 host port、無入向認證），在 stack 裡以服務名 `flux2-dev` 出現於 external network `anila-models-net`。

> 中文為主版；English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**：本服務由 `anila-models` 模型 stack 建置與供裝（見 [`infra/models/`](../../infra/models/README.md)），存在於使用該 stack 的 ANILA 部署分支。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表（現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 這是什麼

`flux2-dev` 是 FLUX.2-dev 的**原始推論後端**：接受一段 prompt、回傳 base64 PNG。它刻意保持極薄——沒有佇列、沒有工作管理、沒有認證，這些都由上層負責。管線在 module import 時建立一次並注入 `build_app`，測試改注入 mock pipeline，因此**開發／CI 完全不必載真權重、不需 GPU**。

前端使用者不會直接看到本服務；它有兩個內部 client：

- **`flux2-dev-agent`**（[`../flux2-dev-agent`](../flux2-dev-agent/README.md)）— 聊天「圖像繪製」流程（Router 分派 `image-generator` agent）的包裝層，只取回單一候選圖。
- **`anila-studio`** 的 `FluxImageProvider`（`services/anila-studio/app/services/flux_image_provider.py`）— Studio 產出中心的簡報／資訊圖插圖管線，走 OpenAI 相容 `/v1/images/generations`。

```
使用者聊天 → Router → DISPATCH:image-generator
                     → CSP proxy → flux2-dev-agent  (OpenAI chat 相容)
                                    → (prompt 翻譯 via gemma4)
                                    → flux2-dev  POST /v1/images/generations   ← 本服務
Studio 產出 → anila-studio FluxImageProvider ──────┘
```

---

## `/v1/images/generations`（OpenAI 相容,2026-07 新增）

院內模型統一部署在雲端算力中心後,平台的兩個 client(`anila-studio` 的 `FluxImageProvider`、`flux2-dev-agent` 的 `FluxClient`)改走標準 **OpenAI Images API**。本 dev 後端提供同款端點對齊新契約;**既有 `/generate` 保留不動**(向後相容)。

Request(標準 OpenAI 欄位):

```json
{"model": "flux.2-dev", "prompt": "...", "n": 1, "size": "1024x1024", "response_format": "b64_json"}
```

- `size`:`"WxH"` 字串(64–2048),格式錯誤 → `422`;內部解析成 width/height,與 `/generate` 共用同一個生成核心。
- `response_format` 僅支援 `"b64_json"`(本服務不架靜態檔案伺服器,無從簽發 url)→ 其他值回 `400`。
- steps / guidance 走 env 預設(`FLUX_NUM_STEPS` / `FLUX_GUIDANCE_SCALE`);seed 每次隨機(OpenAI 契約無 seed 欄位)。

Response:

```json
{"created": 1720000000, "data": [{"b64_json": "<base64 PNG>"}]}
```

---

## `/generate` 合約

依 ANILA Studio FLUX Stage 1（[spec §3.2](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)），`/generate` 回傳 **JSON**（base64 PNG list + audit meta），**非**原始 `image/png` bytes。合約 model-agnostic（flux2-dev 與 klein-4B 通用）。

| 端點 | 方法 | 說明 |
|------|------|------|
| `/health` | GET | `{"status": "ok"}` |
| `/generate` | POST | 自訂 JSON 合約（見下方 request / response）— 向後相容保留 |
| `/v1/images/generations` | POST | OpenAI Images API 相容（見下一節） |

**`GenerateRequest`**：

| 欄位 | 型別 | 預設 / 說明 |
|------|------|-------------|
| `prompt` | `str` | 必填 |
| `aspect_ratio` | `Literal["1:1","16:9","9:16","4:3","3:4","3:1"]` | 預設 `16:9`；其他值 → `422` |
| `seed` | `int \| None` | `None` = 隨機（回傳實際值供 audit；`seed=0` 亦正確處理） |
| `num_candidates` | `int` (1–4) | 預設 `1`，回傳一律為 list |
| `num_inference_steps` | `int \| None` | `None` = env `FLUX_NUM_STEPS`（預設 `28`） |
| `guidance_scale` | `float \| None` | `None` = env `FLUX_GUIDANCE_SCALE`（程式碼 fallback `4.0`） |

**`GenerateResponse`**：

```json
{"images": ["<base64 PNG>", "..."], "seed": 123456,
 "meta": {"steps": 28, "guidance": 4.0, "width": 1408, "height": 768, "model_sha": ""}}
```

推論失敗回 `500`（`detail: "inference failed: ..."`）。

**aspect_ratio → 解析度**（`server.py` 內固定對照，皆落在 ≤0.8MP 的 FLUX 穩定區）：

| ratio | `1:1` | `16:9` | `9:16` | `4:3` | `3:4` | `3:1` |
|-------|-------|--------|--------|-------|-------|-------|
| W×H | 1024×1024 | 1408×768 | 768×1408 | 1216×896 | 896×1216 | 1536×512 |

`3:1`（1536×512）為 section band letterbox 用途。

### 設計重點

- **一律回 `list[str]`**（即使 `num_candidates=1`）——讓 Stage 2 的 N-candidate gate 不必改動 response 型別。
- **seed 用 CPU generator**：管線以 `device_map="balanced"` 把權重分片到多張 GPU，固定某張 cuda device 不安全；CPU generator 給出裝置無關、可重現的 seeding。
- **`FLUX_SKIP_LOAD=1`** 走 no-op stub pipeline（回全黑圖）——容器不載權重、無需 GPU 即可 healthy，供整合 smoke test。conftest 已預設此旗標，故 pytest 不載真權重。

---

## 技術棧

- **語言／框架**：Python 3.11（container）、FastAPI + `uvicorn`；容器內以 `uvicorn server:app` 啟動。
- **推論後端**：`diffusers` 的 `Flux2Pipeline`（`diffusers>=0.36,<0.40`，`Flux2Pipeline` 自 0.36 起提供）、`transformers>=4.50,<5.0`、`accelerate`。
- **精度與多卡**：`torch_dtype=torch.bfloat16`（BF16）、`device_map="balanced"`（權重跨 GPU 分片）。
- **基底映像**：`nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04` + `torch==2.6.0`（cu124 wheel；torch 2.4 無法推斷 diffusers/transformers 用到的 PEP 604 union custom_op schema）。
- **離線**：`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，權重以唯讀 volume mount，容器不連 HuggingFace。

### 關鍵環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `FLUX_MODEL_PATH` | `/workspace/model/FLUX.2-dev` | 權重路徑（唯讀 mount） |
| `FLUX_DEVICE_MAP` | `balanced` | 多卡分片策略 |
| `FLUX_NUM_STEPS` | `28` | inference steps（可被 request 覆寫） |
| `FLUX_GUIDANCE_SCALE` | `4.0`（程式碼）/ `3.5`（models compose） | guidance（可被 request 覆寫） |
| `FLUX_MODEL_SHA` | `""` | 寫進 `meta.model_sha` 供 audit |
| `FLUX_SKIP_LOAD` | 未設 | `1` = stub pipeline、不載權重、無需 GPU |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | air-gapped |

> `FLUX_MAX_CONCURRENT` / `FLUX_TIMEOUT_SECONDS` 等併發／逾時旋鈕在**上層** client（`anila-studio`、`flux2-dev-agent`），**不在本服務**。

---

## 目錄結構

```
services/flux2-dev/
├── server.py           # build_app + /generate + /v1/images/generations + /health + 管線載入
├── Dockerfile          # CUDA 12.4 base → torch 2.6（cu124） → requirements.txt
├── requirements.txt    # 執行期相依（含 GPU stack）
├── pyproject.toml      # 極簡 runtime + [test] extra（不含 torch/diffusers）
└── tests/
    ├── conftest.py     # 預設 FLUX_SKIP_LOAD=1
    ├── test_server.py  # /generate 端點測試，注入 mock pipeline（不需 GPU/權重）
    └── test_openai_images.py  # /v1/images/generations（OpenAI 相容）端點測試
```

---

## 本機開發與測試

測試注入 mock pipeline，**完全不需 GPU、不載 80GB 權重**，可在 dev box 直接跑：

```bash
python -m venv .venv
.venv/bin/pip install -e 'services/flux2-dev[test]'   # 於 repo 根；或先進目錄再 pip install -e '.[test]'
cd services/flux2-dev && ../../.venv/bin/python -m pytest -q
# → 8 passed
```

> 本 repo 未提交 `.venv`；上例臨時建一個。`[test]` extra 只含 `pytest` + `httpx`（給 FastAPI `TestClient`）——`torch` / `diffusers` **不在** test deps。

不載真權重、無 GPU 也能起服務探端點（回全黑圖）：

```bash
FLUX_SKIP_LOAD=1 uvicorn server:app --port 8000   # 於 services/flux2-dev/
curl -s localhost:8000/health                       # {"status":"ok"}
```

---

## 部署

由 `anila-models` 模型 stack 建置與供裝（build context `../../services/flux2-dev`），**不屬平台 stack**，兩者以 external network `anila-models-net` 互通。日常操作與 GPU/資源細節見 [`infra/models/README.md`](../../infra/models/README.md)。

- 只用 `expose: "8000"`，**不開 host port**；agent／studio 以 docker DNS `http://flux2-dev:8000` 連。
- models compose 綁 GPU `["1","2"]`、`shm_size: 32g`、`ipc: host`；healthcheck 打 `/health`（`start_period: 300s`）。
- 注意 models compose 中 `gpt-oss-20b` 綁 GPU `["2"]`——與 `flux2-dev` 共用 GPU 2，部署時留意顯存。

---

## 授權注意（重要）

依 [spec §9](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)：

- **`black-forest-labs/FLUX.2-dev` 為 BFL Non-Commercial License**，內部商用屬灰色地帶——上 production 前請法務確認「公司內部工具」是否落在授權範圍。
- **`FLUX.2-klein-4B` 為 Apache-2.0**，是法律上最乾淨的本地替代（畫質略降、延遲更低）。本服務合約 model-agnostic，換模型不需改 `/generate` 契約。
- `FLUX.2-klein-9B` 與 dev 同為 Non-Commercial，不解決授權問題。

---

## 相關文件

- FLUX 規格：[`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)（§3.2 `/generate` 合約、§9 授權雷區）
- agent 包裝層：[`flux2-dev-agent`](../flux2-dev-agent/README.md) · 模型 stack：[`infra/models`](../../infra/models/README.md)
- 重設計文件：[`05-agent-registry-and-runtime-protocol.md`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)、[`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)

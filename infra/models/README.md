# ANILA models — 推論模型獨立 compose

> 與平台 stack **生命週期解耦**的獨立 compose project（`name: anila-models`），收容 ANILA 的推論工作負載：本地 LLM、embedding、以及 FLUX 圖像生成。全部 air-gapped、僅內網可達（不開 host port），跨 stack 走 external network `anila-models-net` 的 docker DNS。

> 中文為主版；English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**：`infra/models/`（前身 `models/`，§17.1 重組後遷入 `infra/`）存在於所有 ANILA 部署分支，內容跨分支一致。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表（現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 定位

`infra/models/` 收容一份 `docker-compose.yml`（project `anila-models`）與非 FLUX 服務的 build context（`src/`）。它與平台 stack（`anila-platform`，見根 `compose.yaml` → `infra/compose/platform.yml`）是**兩個獨立 project**：

- 在 repo 根跑 `docker compose down` 只會停平台，**不會誤殺**這裡的模型容器。
- 兩個 project 靠共用的 external network `anila-models-net` 互通；CSP／Router／Studio 以 docker DNS（如 `http://gemma4:8000`）連上模型，host port 完全沒開。
- **權重位置未變**：`ANILA_HF_DIR` 預設 `../../models/model`（相對本 compose 檔解析），§17.1 重組只搬了 compose 與 `src/`，沒動權重目錄。

---

## 這份 compose 有哪些服務

**預設組（無 profile）** — 試用機／內網現役：

| 服務 | 角色 | GPU | 對外 |
|------|------|-----|------|
| `gpt-oss-20b` | TensorRT-LLM OpenAI server | `["2"]` | `expose 8000` |
| `gemma4` | vLLM（gemma-4-31B-it + MTP 投機解碼，`served-model-name=gemma4`） | `["3"]` | `expose 8000` |
| `nv-embed-triton` | Triton（NV-Embed-v2 後端） | `["0"]` | **無 expose／port**（純 cluster-internal） |
| `nv-embed-proxy` | OpenAI `/v1/embeddings` shim（`build ./src/embedding_proxy`，橋接 Triton） | — | `expose 8000` |
| `flux2-dev` | FLUX.2-dev 文生圖後端 | `["1","2"]` | `expose 8000` |
| `flux2-dev-agent` | 圖像繪製 agent 包裝層（OpenAI chat 相容） | — | `expose 8000` |

**`--profile intranet` 組** — 內網 H100 新增（generic vLLM image，GPU 以 env 覆寫）：

| 服務 | 權重 | GPU（env 預設） |
|------|------|-----------------|
| `gemma-4-26b-a4b` | MoE ~48G bf16 | `${GEMMA_A4B_GPU:-1}` |
| `gemma-4-12b` | ~22G bf16 | `${GEMMA_12B_GPU:-2}` |
| `gpt-oss-120b` | 原生 MXFP4 ~63G | `${GPT_OSS_120B_GPU:-3}` |

> FLUX 兩服務的合約、環境變數、測試與授權細節**不在本文件展開**，見各自 README：[`services/flux2-dev`](../../services/flux2-dev/README.md)（`/generate` 推論後端）與 [`services/flux2-dev-agent`](../../services/flux2-dev-agent/README.md)（agent shim；build context 在 `services/`，本 compose 以 `../../services/flux2-dev*` 引用）。

---

## 目錄與權重

```
infra/models/
├── docker-compose.yml   # project anila-models（LLM / embedding / FLUX）
└── src/                 # 非 FLUX 服務的 build context / 設定
    ├── tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/   # trtllm serve 設定 + encodings + 壓測
    ├── tritonserver-25.04-nv-embed-v2/            # Triton model repository + export
    └── embedding_proxy/                           # nv-embed-proxy FastAPI（app.py + Dockerfile）

models/model/            # HuggingFace 權重（ANILA_HF_DIR 預設指這；dev 機放 symlink 也通，不搬）
models/inference/        # TensorRT-LLM / Triton 本地建置脈絡與壓測 log（git 未追蹤，不屬本 compose）
```

- `ANILA_HF_DIR`（預設 `../../models/model`）與 `ANILA_MODELSRC_DIR`（預設 `./src`）可覆寫，路徑不同時設 env 即可，權重與設定不必釘 host 絕對路徑。
- 所有模型皆 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1`，權重以 volume mount，容器不連 HuggingFace。

---

## 啟動與部署

建議用 `infra/deployment/intranet/model-serve.sh` 包裝（自動 `source` repo 根 `.env`、自動建 `anila-models-net`、一律帶 `--profile intranet` 故 profile 內外服務都可點名）：

```bash
# 一次性 bootstrap：建網，並讓平台 stack 重新套用網路（restart 不會重掛新網路）
docker network create anila-models-net
docker compose up -d csp            # 於 repo 根，經 root shim compose.yaml；csp 加入 anila-models-net

# 日常操作（於 repo 根）
bash infra/deployment/intranet/model-serve.sh up trial          # 現役組：gpt-oss-20b gemma4 nv-embed flux
bash infra/deployment/intranet/model-serve.sh up intranet       # H100 組：gemma4 26b-a4b 12b 120b nv-embed
bash infra/deployment/intranet/model-serve.sh up gemma4         # 單一服務（up 一定要給 group 或服務名）
bash infra/deployment/intranet/model-serve.sh status            # 全部 health 一覽
bash infra/deployment/intranet/model-serve.sh logs flux2-dev    # tail -f
bash infra/deployment/intranet/model-serve.sh down              # 全停（只動模型，平台不受影響）
```

等價的裸 compose 指令：

```bash
docker compose -f infra/models/docker-compose.yml --profile intranet up -d gemma4
docker compose -f infra/models/docker-compose.yml logs -f flux2-dev
docker compose -f infra/models/docker-compose.yml down
```

> ⚠️ 別用 `START-HERE.sh` / `intranet-deploy.sh` 做模型層小修改——那是 card 一次性 bootstrap。日常生命週期用 `model-serve.sh`；平台側則用 `infra/deployment/scripts/deploy-prod.sh`。改設定一律 `up -d`（`docker restart` 不重載 `.env`／compose）。

---

## 與平台 / Model Gateway 的邊界

CSP 的 **Model Gateway（治理中心，[doc 04](../../docs/anila-redesign-docs/04-model-gateway-design.md)）** 才是「註冊、路由、per-model API Key、5-state 健康、`ANILA_ENV` http fail-closed、分類限制、usage trace」的所在。本 compose 只**提供上游端點**，兩件事分層：

- **同機 docker DNS 上游（本 compose）**：`gpt-oss-20b` / `gemma4` / `nv-embed-proxy`（`model_type` 三種）與 `image-generator`（`model_type=agent`，端點 `flux2-dev-agent:8000`）由平台 seed 註冊進 CSP model registry；同網內免 API Key。`nv-embed-triton` 與 `flux2-dev` **不直接註冊**——前者只由 `nv-embed-proxy` 內部連、後者只由 agent／Studio 以 URL 直打。
- **跨機模型（doc 04 的主場景）**：不同內網主機（如 `.12` gateway）的模型走 HTTPS + per-model API Key，由 CSP Model Gateway 代理——那條路徑的憑證／健康／fail-closed 治理在 CSP，不在本 compose。

---

## 關鍵環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `ANILA_HF_DIR` | `../../models/model` | 權重根目錄（相對本 compose 檔） |
| `ANILA_MODELSRC_DIR` | `./src` | 非 FLUX 服務設定／build context |
| `VLLM_IMAGE` | `vllm/vllm-openai:v0.22.1-cu129-ubuntu2404` | intranet profile 的 generic vLLM image |
| `GEMMA_A4B_GPU` / `GEMMA_12B_GPU` / `GPT_OSS_120B_GPU` | `1` / `2` / `3` | intranet profile 各模型 GPU |
| `INTERNAL_PLATFORM_API_KEY` | （必填） | 帶入 `flux2-dev-agent` 的 `CSP_API_KEY`（翻譯 callback；缺值 fail-loud） |

---

## 相關文件

- FLUX 服務：[`services/flux2-dev`](../../services/flux2-dev/README.md)、[`services/flux2-dev-agent`](../../services/flux2-dev-agent/README.md) · FLUX 規格與授權：[`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)（§9 授權雷區：FLUX.2-dev 為 BFL Non-Commercial，klein-4B 為 Apache-2.0 的乾淨替代）
- 重設計文件：[`04-model-gateway-design.md`](../../docs/anila-redesign-docs/04-model-gateway-design.md)、[`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)
- 部署腳本：`infra/deployment/intranet/model-serve.sh`（模型生命週期）、`infra/deployment/scripts/deploy-prod.sh`（平台生命週期） · 平台整體：[`../../README.md`](../../README.md)

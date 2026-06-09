# ANILA models — 推論模型服務（LLM + Embedding）

> ANILA 平台的本地推論模型服務（air-gapped、僅內網可達）：兩個 OpenAI 相容 LLM + 一條 embedding 管線。

> 中文為主、英文鏡像見 [`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律維持英文。

---

## 簡介 / Overview

`models/` 是與平台 stack 生命週期解耦的獨立 compose project（`name: anila-models`），收容 ANILA 的推論工作負載。所有服務只用 `expose:`（內網），**不對 host 開 port**；CSP 透過共用的 external docker network `anila-models-net` 以 DNS 連接。

| 服務 | 角色 | 後端 | GPU | CSP 註冊 endpoint |
|------|------|------|-----|-------------------|
| **`gpt-oss-20b`** | LLM | TensorRT-LLM（OpenAI 相容 server） | GPU 2 | `http://gpt-oss-20b:8000/v1` |
| **`gemma4`** | LLM（Router primary） | vLLM + MTP speculative decoding | GPU 3 | `http://gemma4:8000/v1` |
| **`nv-embed-triton`** | Embedding 推論後端 | Triton（自家協定，**完全內網**） | GPU 0 | （不直連，見下） |
| **`nv-embed-proxy`** | Embedding OpenAI 相容前端 | FastAPI shim → Triton | 無 | `http://nv-embed-proxy:8000/v1` |

> 同主機 4 張 GPU 用其中 3 張（GPU 0 / 2 / 3），GPU 1 保留作 scale-out。`nv-embed-triton` 講 Triton 自家協定不是 OpenAI v1，CSP **從不直接連**它 — 只有同 network 的 `nv-embed-proxy` 透過 docker DNS 找它；因此 Triton 連 `expose:` 都不開，container-to-container only。

---

## 架構與技術棧 / Architecture & Stack

```
        CSP /v1/* proxy  (token_usage 計量)
                 │  docker DNS (anila-models-net)
     ┌───────────┼───────────────────────┐
     ▼           ▼                        ▼
 gpt-oss-20b   gemma4                nv-embed-proxy  (OpenAI /v1/embeddings)
 (TRT-LLM)     (vLLM + MTP)               │ Triton 協定
  GPU 2         GPU 3                      ▼
                                     nv-embed-triton  (無 expose,純內網)
                                          GPU 0
```

### gpt-oss-20b（LLM，TensorRT-LLM）

- **映像**：`tensorrt-llm-hf:1.3.0rc10`，`trtllm-serve` 提供 OpenAI 相容 `/v1/*`。
- **GPU / 資源**：`device_ids: ["2"]`、`ipc: host`、`memlock: -1`。
- **離線**：`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` / `HF_DATASETS_OFFLINE=1`，權重以 volume mount，不連 HF。
- **啟動參數**：`--extra_llm_api_options .../gpt-oss-20b-throughput.yaml`、`--kv_cache_free_gpu_memory_fraction 0.5`、`--port 8000`。
- **健康檢查**：`GET /v1/models`（`start_period: 120s`）。

### gemma4（LLM，vLLM + MTP）

- **映像**：`vllm-gemma4:latest`；`--served-model-name gemma4` 讓 CSP `model_registry`、Router、agent fan-out 全程同一個 id。
- **模型**：`gemma-4-31B-it`，`--max-model-len 131072`、`--dtype bfloat16`、`--kv-cache-dtype fp8`、`--gpu-memory-utilization 0.97`、`--enable-prefix-caching`。
- **MTP speculative decoding**：`--speculative-config '{"method":"mtp","model":".../gemma-4-31B-it-assistant","num_speculative_tokens":2}'`，跟 target 共用 KV cache，實測 1.7–2.2× 加速（conversational > code）。需要 vLLM ≥ 0.18（含 Gemma4 MTP spec-decode PR）；舊版會報 `unknown speculative method 'mtp'` 退出。assistant 小頭模型需一次性離線下載到本機。
- **tool / reasoning**：`--enable-auto-tool-choice`、`--tool-call-parser gemma4`、`--reasoning-parser gemma4`、`--default-chat-template-kwargs '{"enable_thinking": true}'`。
- **GPU / 資源**：`device_ids: ["3"]`、`shm_size: 64g`、`ipc: host`。
- **健康檢查**：`GET /v1/models`（`start_period: 180s`）。

### nv-embed-triton（Embedding 後端，Triton）

- **映像**：`tritonserver:25.04-nv-embed-v2`，model `NV-Embed-v2`。
- **GPU**：`device_ids: ["0"]`；`--model-control-mode=poll --repository-poll-secs=1`。
- **網路**：**沒有 `expose:` 也沒有 `ports:`** — 只在 `anila-models-net` 內讓 `nv-embed-proxy` 透過 docker DNS 找到，attack surface 最小。
- **健康檢查**：`GET /v2/health/ready`（Triton 協定）。

### nv-embed-proxy（Embedding OpenAI 相容前端）

- **映像**：`embedding-proxy:migration`（FastAPI shim）。把 OpenAI `/v1/embeddings` 形狀橋接到 Triton。**這才是 CSP 實際註冊的 embedding endpoint。**
- **環境**：`TRITON_URL=http://nv-embed-triton:8000`、`MODEL_NAME=nv-embed-v2`、`REQUEST_TIMEOUT=60`。
- **依賴**：`depends_on: nv-embed-triton (service_healthy)`；無 GPU。
- **健康檢查**：`GET /v1/models`（`start_period: 30s`）。

---

## 目錄結構 / Layout

```
models/
├── docker-compose.yml   # anila-models compose project（上述 4 個服務）
├── README.md
└── README.en.md
```

> 模型權重、Triton repository、TensorRT engine 等都以**主機絕對路徑** volume mount 進容器（`/home/aia/c1147259/project/Huggingface/...`、`.../Docker/...`），不在 repo 內；換 host 時記得遷移或修 compose 內的路徑。

---

## 啟動與部署 / Setup & Run

一次性 bootstrap（host 上）：

```bash
# 1. 建 cross-project external network
docker network create anila-models-net

# 2. 平台 csp 容器已宣告加入此 network — 重啟一次讓它真的進去
docker compose -f docker-compose.yml restart csp
```

日常操作：

```bash
docker compose -f models/docker-compose.yml up -d
docker compose -f models/docker-compose.yml ps
docker compose -f models/docker-compose.yml logs -f gemma4
docker compose -f models/docker-compose.yml restart gemma4
docker compose -f models/docker-compose.yml down          # 只動模型,平台不受影響
```

GPU / 資源配置（取自 compose）：

| 服務 | GPU | 重點 |
|------|-----|------|
| `gpt-oss-20b` | 2 | TensorRT-LLM、`ipc: host`、kv_cache 0.5 |
| `gemma4` | 3 | vLLM + MTP、`shm_size: 32g`+、max-model-len 131072 |
| `nv-embed-triton` | 0 | Triton、無 expose（純內網） |
| `nv-embed-proxy` | — | FastAPI shim，`depends_on` triton |

---

## 與其他服務的關係 / Integration

- **CSP**：在 `/models` UI 或 `AUTO_REGISTER_MODELS` 把三條 endpoint（`http://gpt-oss-20b:8000/v1`、`http://gemma4:8000/v1`、`http://nv-embed-proxy:8000/v1`）註冊進 `model_registry`，標 `is_internal`。CSP 的 `/v1/chat/completions` / `/v1/embeddings` proxy 走這些 endpoint 並計量 `token_usage`。
- **Router**：主路由 LLM 預設 `gemma4`。
- **ingestion-worker**：embedding 經 CSP `/v1/embeddings`（亦即 `nv-embed-proxy`）做 chunk 向量化。
- 三者共用 external docker network `anila-models-net`，僅 container-to-container DNS 可達；host port 完全沒開。

### 端到端連線驗證

```bash
# 從外網主機（或主機 shell）打應該全部 refused（沒開 host port）
curl http://<本機-LAN-IP>:8000/v1/models     # connection refused

# 從 CSP container 內走 docker DNS 應該 200
docker compose exec csp curl http://gpt-oss-20b:8000/v1/models
docker compose exec csp curl http://gemma4:8000/v1/models
docker compose exec csp curl http://nv-embed-proxy:8000/v1/models
```

---

## 授權注意 / Licensing note

各模型沿用其上游授權，內部商用前請法務確認：

- **`gpt-oss-20b`**：OpenAI gpt-oss，Apache-2.0。
- **`gemma-4-31B-it`**：Google Gemma Terms of Use（非標準 OSI 授權，使用前確認條款）。
- **`NV-Embed-v2`**：NVIDIA 授權（含非商業條款）—— 內部商用屬灰色地帶，上 production 前請法務確認。

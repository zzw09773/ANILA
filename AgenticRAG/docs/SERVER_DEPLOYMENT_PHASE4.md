# AgenticRAG Phase 4 — Server-Side Deployment Plan

> **Audience**: 模型伺服器運維 / MLOps（Triton + vLLM + TensorRT-LLM stack 維護者）
> **Status**: AgenticRAG 端 (`v0.4.x` Phase 4) 已就緒，等待 server 端部署完成後即可啟用
> **Estimated effort**: 1–2 個工程小時（不含驗收）

---

## 1. 背景

AgenticRAG 在 Phase 4 把原本「Jina cloud reranker + 本地 EasyOCR/Tesseract」
全部改為**統一從內網模型伺服器取用**，理由：

* 部署環境是封閉內網，沒有對外出口可呼叫 Jina cloud
* 應用機沒有 GPU，本地 PyTorch reranker 沒意義
* 模型伺服器有 4× H100，跑 reranker 跟 vision OCR 都綽綽有餘

**這份文件描述 server 端需要做的兩件事**：

| 工作項目 | 變更 | 必要性 |
|---|---|---|
| (1) 在 vLLM stack 新增一個 reranker 模型實例 | **新增** | ✅ 必須 |
| (2) 確認既有 vision endpoint 可承接 OCR 流量 | **無變更**，僅驗證 | ✅ 必須 |

---

## 2. 部署目標總覽

```
Triton Inference Server (既有)
├── llm/        google/gemma4              (既有，不動)
├── embedding/  nvidia/NV-embed-V2         (既有，不動)
├── vision/     meta/llama-4-maverick      (既有，不動 — OCR 重用此 endpoint)
└── reranker/   mxbai-rerank-large-v1      ← 本次新增
```

**新增模型規格**

| 項目 | 值 |
|---|---|
| HuggingFace ID | `mixedbread-ai/mxbai-rerank-large-v1` |
| 來源 / License | mixedbread.ai（德國），Apache-2.0 |
| 架構 | DeBERTa-v3 cross-encoder, sequence classification |
| 參數量 | 435M |
| VRAM (FP16) | ~2 GB（含 KV cache 餘裕約 4 GB） |
| 推論延遲 (H100 FP16) | < 10 ms / query 對 100 候選文件 |
| 任務類型 | vLLM `--task score` |

---

## 3. Reranker 部署

### 3.1 方案 A：Standalone vLLM（建議先驗證用）

最簡路徑，獨立 process，30 秒就能起來驗證 model 跑得起來。

```bash
python -m vllm.entrypoints.openai.api_server \
  --model mixedbread-ai/mxbai-rerank-large-v1 \
  --task score \
  --port 8001 \
  --host 0.0.0.0 \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.15 \
  --served-model-name mxbai-rerank-large-v1 \
  --max-model-len 8192
```

**參數說明**

| 旗標 | 為什麼這樣設 |
|---|---|
| `--task score` | 指定 cross-encoder pairwise scoring 模式（vLLM 0.6+） |
| `--gpu-memory-utilization 0.15` | 此模型只需 1 張 H100 的 ~15%，留空間給其他服務 |
| `--max-model-len 8192` | mxbai 預訓練 max length 8192 tokens；繁中 query+doc 對通常 < 1500 |
| `--served-model-name` | AgenticRAG 環境變數會用這個名稱呼叫 |
| `--tensor-parallel-size 1` | 單卡足夠，無需 TP |

模型權重會在第一次啟動時自動從 HuggingFace 下載到 `~/.cache/huggingface/hub`。
**內網無外網時**請事先在有網路的機器下載後 `rsync` 到 server，並用
`HF_HUB_OFFLINE=1` 啟動。

---

### 3.2 方案 B：併入 Triton 的 vLLM backend（正式部署）

跟現有 LLM / embedding / vision 同樣的部署模式，由 Triton 統一管理生命週期、
監控、autoscaling。

**model repository 結構**
```
/models/
└── mxbai-rerank-large-v1/
    ├── config.pbtxt
    └── 1/
        └── model.json
```

**`config.pbtxt`** 範例骨架：

```protobuf
backend: "vllm"
max_batch_size: 32

model_transaction_policy {
  decoupled: false
}

input [
  {
    name: "text_input"
    data_type: TYPE_STRING
    dims: [ 1 ]
  }
]

output [
  {
    name: "text_output"
    data_type: TYPE_STRING
    dims: [ 1 ]
  }
]

instance_group [
  {
    count: 1
    kind: KIND_GPU
    gpus: [ 0 ]
  }
]

parameters: {
  key: "model"
  value: { string_value: "mixedbread-ai/mxbai-rerank-large-v1" }
}
parameters: {
  key: "task"
  value: { string_value: "score" }
}
parameters: {
  key: "served_model_name"
  value: { string_value: "mxbai-rerank-large-v1" }
}
```

**`model.json`** （Triton vLLM backend 的 config 檔）：

```json
{
  "model": "mixedbread-ai/mxbai-rerank-large-v1",
  "task": "score",
  "tensor_parallel_size": 1,
  "gpu_memory_utilization": 0.15,
  "max_model_len": 8192,
  "served_model_name": "mxbai-rerank-large-v1"
}
```

> **注意**：請確認 Triton 的 vLLM backend 版本支援 `task=score`。
> 截至 vLLM 0.6.x，`score` task 已 GA；若你們是更早版本請先升級或暫用方案 A。

---

### 3.3 端點驗證

無論方案 A / B，部署完成後請**手動跑一次 sanity check**：

```bash
curl -X POST http://172.16.120.35:8001/v1/score \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mxbai-rerank-large-v1",
    "text_1": "申誡的條件",
    "text_2": [
      "違反軍紀者，視情節輕重申誡之",
      "今天天氣很好我們去吃午餐",
      "陸海空軍懲罰法第八條規定"
    ]
  }'
```

**預期回傳**（順序固定為 `text_2` 輸入順序，AgenticRAG 端自己 sort）：

```json
{
  "id": "score-...",
  "object": "list",
  "model": "mxbai-rerank-large-v1",
  "data": [
    {"index": 0, "object": "score", "score": 0.92},
    {"index": 1, "object": "score", "score": 0.04},
    {"index": 2, "object": "score", "score": 0.88}
  ],
  "usage": {...}
}
```

**驗收 checklist**

- [ ] 第 0 筆（直接相關）跟第 2 筆（題目來源）分數應顯著高於第 1 筆（不相關）
- [ ] 分數一律落在 `[0, 1]` 區間
- [ ] response shape 跟上面範例一致（特別是 `data[].index` 跟 `data[].score`）
- [ ] 重複呼叫 100 次平均 latency < 50 ms（H100 上預期 < 10 ms）

---

## 4. OCR — Vision Endpoint 確認

**結論：server 端無需任何變更**，AgenticRAG 直接重用既有 `VISION_URL`
(`meta/llama-4-maverick`) 處理 PDF OCR fallback。

只需要確認以下三件事：

### 4.1 Vision endpoint 接受 image_url + base64 data URI

AgenticRAG OCR 流程會把每頁 PDF raster 成 PNG，base64 編碼後塞進 OpenAI-style
`chat/completions` 的 `image_url` 欄位：

```json
{
  "model": "meta/llama-4-maverick",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "請逐字輸出此圖片中的繁體中文..."},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0..."}}
    ]
  }],
  "temperature": 0.0
}
```

請以一張任意 PNG 驗證 endpoint 可以正確解析這個 shape。Llama-4-Maverick
原生支援，但若你們前面有 proxy / gateway 可能需要放寬 request body size
（單頁 200 DPI A4 PNG base64 後約 200–500 KB，整份 100 頁 PDF 序列化後可能 ~30 MB —
但 AgenticRAG 是逐頁送，每次 request body 最大 ~1 MB）。

### 4.2 並行容量

AgenticRAG 預設 `PDF_OCR_CONCURRENCY=4`，意即一份 PDF 同時打 server 4 路。
如果 60 人團隊有 1–3 個 admin 在做 ingestion，尖峰期 vision 並行需求約：

```
ingestion: 1–2 PDFs × 4 concurrent = 4–8 並行 vision 請求
chat: 用戶 vision 查詢的常態流量
```

4× H100 跑 llama-4-maverick 應有 16+ 並行容量，理論不衝突。
**但如果 chat 流量本來就很重**，請考慮：

- 把 vision endpoint 拆成兩個 instance（chat 用高優、OCR 用低優）
- 或請 AgenticRAG 端把 `PDF_OCR_CONCURRENCY` 降到 2

### 4.3 Token quota / billing

每頁 vision OCR 請求約：
- input: 1500–3000 tokens（圖 + prompt）
- output: 500–3000 tokens（視文字密度）

100 頁 PDF 一次 ingestion 約 **400K input tokens + 100K output tokens**。
請確認你們的 token quota 系統能承受批次 ingestion 時的尖峰用量。

---

## 5. 防火牆 / 網路

確認以下連線可達（從 AgenticRAG 應用機 → server）：

| 來源 | 目標 | 用途 |
|---|---|---|
| AgenticRAG app server | `172.16.120.35:8001/v1/score` | Reranker（**新增**） |
| AgenticRAG app server | `172.16.120.35/v1/chat/completions` | LLM + Vision OCR（既有） |
| AgenticRAG app server | `172.16.120.35/v1/embeddings` | Embedding（既有） |

如果 reranker 走獨立 port (8001)，記得**通防火牆 inbound rule**。

如果走方案 B（併入 Triton），通常會復用 Triton 既有的 port (`8000` HTTP /
`8001` gRPC / `8002` metrics），那就只需要在 Triton model repo 新增 entry，
**不需要動防火牆**。

---

## 6. 容量規劃

| 資源 | 預估占用 | 備註 |
|---|---|---|
| GPU VRAM | ~4 GB（峰值，含 KV cache） | < 5% of 1× H100 |
| GPU compute | < 5%（待機）/ ~30%（短爆發） | rerank 是短作業，幾十 ms |
| RAM | ~4 GB | model 載入後常駐 |
| Disk | ~1 GB | model weights cache |
| Network | < 100 KB/req | 純文字 |

**結論：不會干擾現有 LLM / Vision 服務**，可以跟其他模型共用 1 張 H100。

---

## 7. AgenticRAG 端對應設定

Server 部署完成後，請通知 AgenticRAG 開發/運維團隊更新應用機的 `.env`：

```bash
# Reranker — 新增區塊
RAG_RERANKER_ENABLED=true
RAG_RERANKER_URL=http://172.16.120.35:8001/v1
RAG_RERANKER_MODEL=mxbai-rerank-large-v1
RAG_RERANKER_API_KEY=                       # 內網無認證留空即可
RAG_RERANKER_VERIFY_SSL=false
RAG_RERANK_POOL_MULTIPLIER=3

# OCR — 重用既有 VISION_* 不需新增
PDF_OCR_FALLBACK=true                       # 將來想開 OCR 時改 true
PDF_OCR_CONCURRENCY=4
PDF_OCR_MAX_PAGES=100
```

> 若方案 B（併入 Triton）port 跟 LLM 共用，`RAG_RERANKER_URL` 跟
> `LLM_URL` 會是同一個 base URL — `http://172.16.120.35/v1`。

---

## 8. 驗收 Checklist（Server 端 + 跨團隊）

### Server 端自驗
- [ ] vLLM `/v1/score` endpoint 回應 sanity-check curl，shape 正確
- [ ] 重複壓測 100 次，p50 latency < 50 ms，無錯誤
- [ ] GPU memory 監控確認占用 < 5 GB
- [ ] 重啟 server 後 model 自動載入無人為介入
- [ ] vision endpoint 接受 base64 data URI 圖片 input

### 跨團隊聯合驗收
- [ ] AgenticRAG 端跑 `pytest tests/test_reranker.py` 全綠
- [ ] AgenticRAG 端用一份真實繁中 PDF 跑 ingest，確認 OCR fallback 觸發成功
- [ ] AgenticRAG 端 `/agentic-chat` 對 reranker 開/關 兩種狀態各跑 5 個查詢，
      開啟 reranker 後檢索結果排序明顯改善
- [ ] 監控 dashboard 加上新 endpoint 的 latency / error rate / qps

---

## 9. 監控建議

如果你們有 Prometheus / Grafana：

| Metric | 來源 | Alert 條件 |
|---|---|---|
| `vllm_score_request_latency_seconds` | vLLM 0.6+ 內建 | p99 > 200 ms |
| `vllm_score_request_failures_total` | vLLM 0.6+ 內建 | rate > 1/min |
| GPU utilization (reranker GPU) | `nvidia-smi` exporter | sustained > 80% for 5 min |
| Vision endpoint OCR-pattern qps | 從 request body 內含 `image_url` 區分 | 視 SLA 而定 |

**AgenticRAG 端**會記錄 reranker 失敗（catch + warning log），所以即使 server
短暫壞掉也不會 500 給用戶 — 但 log 數量可作為 server 不穩的早期 signal。

---

## 10. 回滾策略

### Reranker 出問題
1. AgenticRAG 端把 `RAG_RERANKER_ENABLED=false` 重啟即可（30 秒）
2. 系統會自動退回 RRF hybrid search，搜尋仍可用，只是排序略差
3. Server 端可暫停 reranker model 卸載 GPU

### OCR 出問題
1. AgenticRAG 端把 `PDF_OCR_FALLBACK=false` 重啟（30 秒）
2. PDF 仍可入庫，只是字型子集化的 PDF 抽不到字而已
3. Server 端 vision endpoint 不需動

---

## 附錄 A：替代方案（不建議，僅供參考）

| 方案 | 為什麼不選 |
|---|---|
| TEI (HuggingFace Text Embeddings Inference) | 又多一套 server framework 要維護，跟現有 vLLM stack 不一致 |
| FlashRank wrapped in FastAPI | CPU only，浪費 H100；繁中只能用 MultiBERT，品質弱於 mxbai |
| BGE-reranker-v2-m3 | 中國來源，依政策排除 |
| Jina-reranker-v2 cloud | 內網無法呼叫外網 |

## 附錄 B：替代模型（如 mxbai-large 不可用）

| 模型 | 規格 | 何時選 |
|---|---|---|
| `mixedbread-ai/mxbai-rerank-base-v1` | 184M, ~800 MB VRAM | mxbai-large 無法載入時的小規格替代，繁中品質仍佳 |
| `jinaai/jina-reranker-v2-base-multilingual` | 278M, ~1.2 GB VRAM | 另一個非中國來源頂層多語 reranker |

兩者都接 vLLM `--task score`，AgenticRAG 端只需要改 `RAG_RERANKER_MODEL`
即可切換，無需 code change。

---

## 附錄 C：問題回報

部署過程任何問題請聯繫 AgenticRAG 開發團隊，附上：

1. vLLM 版本（`vllm --version`）
2. Triton 版本（如走方案 B）
3. 完整啟動指令 / `config.pbtxt`
4. 失敗時的 server log 段落
5. sanity-check curl 的完整 request + response

AgenticRAG 端 reranker 的 source 在
[`src/agentic_rag/providers/reranker.py`](../src/agentic_rag/providers/reranker.py)，
可作為對接 contract 的參考。

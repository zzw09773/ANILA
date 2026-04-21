# AgenticRAG — RAG Sample Agent 樣板

> **這是什麼**：一份可運行、可 fork 的 **RAG 範例 agent**，示範如何基於
> [`anila-core`](../anila-core/) runtime foundation 組一個可被 myCSPPlatform
> 註冊與分派的 agent。
>
> **這不是什麼**：不是 ANILA 平台本身，也不是 ANILA Core runtime。
> 平台主軸是 **runtime-first**，RAG 只是眾多可能 agent 類型的其中一種。
> 平台整體路線圖見 repo 根的 [`anila_plan.md`](../anila_plan.md)。

---

## 一眼看懂

```
  使用者
    │ OpenAI-compat /v1/chat/completions
    ▼
┌─────────────────────────────────────────────────────┐
│   api.py (port 24786)                               │
│                                                     │
│   1. CspServiceTokenMiddleware        (anila-core)  │
│      ← 驗 CSP 簽過來的 s2s token                      │
│                                                     │
│   2. retrieve_context(last_user_msg)                │
│      ├─ embed query   (NV-Embed-V2 via CSP / 直連)   │
│      ├─ vector search (pgvector cosine)             │
│      ├─ keyword search (ILIKE + 中文空格展開)         │
│      └─ RRF merge → top-k                           │
│                                                     │
│   3. inject_context(messages, rag_context)          │
│                                                     │
│   4. 轉發至 LLM (直連 or 透過 CSP proxy)              │
│      ← SSE 串流回傳 + thinking block + 來源清單       │
└─────────────────────────────────────────────────────┘
        │
        ▼
   pgvector (document_chunks 表)
        ▲
        │  離線以 index_documents.py 批次寫入
        │  (使用 anila-core.ingestion + NvidiaEmbeddingProvider)
   data/documents/*.{pdf,docx,md,txt,odt}
```

## 為什麼是「樣板」，而不是「平台元件」

| 檔案 | 角色 | 可以改嗎？ |
|---|---|---|
| `api.py` | 範例 agent endpoint（OpenAI-compat 代理 + RAG 注入） | **全部可改**。這就是 fork 時你要動的東西 |
| `index_documents.py` | 範例資料攝取 CLI | **全部可改**。依你自己的資料來源重寫 |
| `data/documents/` | 範例文件集（空的） | 塞你自己的文件進去 |
| `Dockerfile` + `docker-compose.yml` | 範例部署 | 依部署環境調整 |
| `pyproject.toml` | `anila-core` 依賴宣告 | 保留 anila-core 依賴；其他可加可減 |
| `.env.example` | 設定範本 | 改成你的 agent 實際需要的變數 |

> **Rule of thumb**：`anila-core` 提供的 primitives（auth middleware、
> embedding provider、pgvector store、ingestion pipeline、agent registry 等）
> **不要重造輪子**，直接引用。其他都可以換掉。

---

## Fork → 部署 流程（開發者觀點）

```
 1. git clone AgenticRAG my-new-agent/
 2. 改 api.py：替換 retrieve_context() 為你的檢索邏輯
    （例：接你自己的 knowledge graph / 外部 API / 另一個模型）
 3. 改 .env：填 LLM endpoint 或 CSP_* 變數
 4. docker compose build && docker compose up -d
 5. 在 CSP UI「Developer」分頁註冊：
       name:                my-new-agent
       endpoint_url:        http://<your-host>:24786
       base_model:          CSP 已註冊的底層 LLM
       requires_encryption: 依需求
 6. admin 核准後 → Router 自動 discover → UI 可對話
```

**不需要動 `anila-core/`。** 那是平台的共用 runtime foundation，升級是平台團隊的責任。

---

## 目錄

- [快速啟動](#快速啟動)
- [環境變數](#環境變數)
- [批次索引文件](#批次索引文件)
- [Endpoint 列表](#endpoint-列表)
- [OpenWebUI 整合](#openwebui-整合)
- [與 anila-core 的依賴關係](#與-anila-core-的依賴關係)
- [測試](#測試)

---

## 快速啟動

### 方式 A：Docker Compose（推薦）

```bash
# 1. 設定環境變數
cp .env.example .env
# 編輯 .env：至少要有 LLM_URL / LLM_API_KEY，或 CSP_BASE_URL / CSP_API_KEY

# 2. 啟動（自動建 image，連 anila-core 一起安裝進去）
docker compose up -d

# 3. 確認
curl http://localhost:24786/health
# {"status":"ok","model":"google/gemma4","rag":true}
```

Build context 是 **父目錄** `D:/ANILA/`，因此 `anila-core/` 會被一起複製進 image。

### 方式 B：本機開發

```bash
# 從 AgenticRAG/ 目錄
pip install -e ../anila-core          # 先裝 runtime foundation
pip install -e ".[dev]"                # 再裝本 agent 的依賴

# 啟 pgvector（或連遠端 DB）
docker compose up -d db

# 跑 agent
python api.py
# 或
uvicorn api:app --host 0.0.0.0 --port 24786 --reload
```

---

## 環境變數

完整見 [`.env.example`](./.env.example)。重點：

| 變數 | 必填？ | 說明 |
|---|---|---|
| `CSP_BASE_URL` / `CSP_API_KEY` | ⭕ 任擇一組 | 走 myCSPPlatform 代理 LLM + embedding |
| `LLM_URL` / `LLM_API_KEY` / `MODEL` | ⭕ | 直連 LLM（非 CSP 部署時） |
| `EMBEDDING_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | ⭕ | embedding endpoint |
| `DATABASE_URL` | ⭕ | pgvector 連線字串 |
| `RAG_TOP_K` / `RAG_MIN_SCORE` / `RAG_MIN_SCORE_RETRY` | ❌ | 檢索調參 |
| `CSP_SERVICE_TOKEN` | ❌ | CSP 部署時的 s2s 驗證 token（空值 → dev mode） |
| `RAG_SYSTEM_PROMPT` | ❌ | 覆寫預設 zh-TW system prompt |

**關於 SSL：** 內網 LLM endpoint 常用自簽憑證，`EMBEDDING_VERIFY_SSL=false` 用於
embedding；LLM 側的 httpx client 也固定 `verify=VERIFY_SSL`。Production 應接
有效憑證並設為 `true`。

---

## 批次索引文件

`index_documents.py` 是**獨立 CLI**，直接寫 pgvector，不透過 HTTP API。
使用 `anila-core` 的 ingestion pipeline（parser → chunker → embedder → store）。

```bash
# 把文件丟進 data/documents/
cp ~/your-pdfs/*.pdf data/documents/

# 執行批次索引
python index_documents.py

# 指定其他資料夾 / user / project scope
python index_documents.py --dir ~/other-docs --user alice --project legal

# 列出目前已索引內容
python index_documents.py --list

# 刪除（可用檔名部分匹配或 document_id）
python index_documents.py --delete 刑法.pdf
python index_documents.py --delete-all --yes
```

支援格式：`.txt` `.md` `.pdf` `.docx` `.doc` `.odt`

> **要改成你自己的資料源？** 這個腳本是整個樣板中最值得改的部分。
> 保留 `anila-core.storage.pgvector_store` + `NvidiaEmbeddingProvider`，
> 把 parser/chunker 層換成你的文件來源即可（直接從 API 拉、從 S3 拉、從 MSSQL 拉...）。

---

## Endpoint 列表

本樣板**只提供 OpenAI-compatible chat 介面**，保持介面面積最小：

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/v1/models` | 回報可用模型（`rag/${MODEL}`） |
| `POST` | `/v1/chat/completions` | 主要端點；支援 `stream=true/false` |
| `GET` | `/health` | 健康探針（用於 CSP discovery + docker healthcheck） |

CSP 會透過 `X-CSP-Service-Token` header 呼叫 `/v1/*`；`/health` 對所有來源開放。

**若你的 agent 需要更多端點**（例如管理 UI、檔案上傳、自訂 tool endpoint），
直接在 `api.py` 加 `@app.get(...)` / `@app.post(...)`；`CspServiceTokenMiddleware`
會自動保護 `/v1/*` 之外的路徑，`/health|/docs|/openapi.json|/redoc` 則自動放行。

---

## OpenWebUI 整合

```
Settings → Connections → OpenAI API
  URL:     http://<host>:24786
  API Key: (任意，若無 CSP_SERVICE_TOKEN 則不驗證)
```

OpenWebUI 送來的 `model` 欄位會被忽略（它常送連線名稱而非實際模型 ID）；
`api.py` 永遠使用 `.env` 的 `MODEL`。

**思考塊與來源清單：** 回覆前會先在 `delta.reasoning_content` 注入 RAG 檢索
軌跡（各 chunk 的檔名、匹配方式、RRF 分數），回覆末尾附「參考來源」列表。

---

## 與 anila-core 的依賴關係

本樣板**只使用** anila-core 的以下 primitive（保持依賴面最小）：

| 用途 | anila-core 模組 | 本樣板引用位置 |
|---|---|---|
| CSP s2s auth | `api.middleware.auth.CspServiceTokenMiddleware` | `api.py:96-110` |
| 離線 ingestion | `ingestion.service.IngestionService` | `index_documents.py` |
| pgvector store | `storage.adapters.pgvector_store` | `index_documents.py` |
| NV-Embed-V2 | `providers.embedding_nvidia.NvidiaEmbeddingProvider` | `index_documents.py` |

> anila-core 還提供：QueryEngine turn loop、Coordinator 多 worker、Compact
> 服務、Session Memory、Agent Registry、Router、Extended tools 等。本樣板
> **沒有**用到，因為 OpenAI-compat proxy 不需要多輪 tool calling。
>
> 如果你的 agent 需要 tool-driven RAG（讓 LLM 自行決定何時搜、搜什麼），
> 請引用 `anila_core.engine.query_engine` 與 `anila_core.tools.*`，將 `api.py`
> 的 pre-process 注入改為 turn loop。anila-core 本身就有完整的樣板可以參考。

---

## 測試

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

目前測試覆蓋：
- `_expand_tokens` — 中文法條空格變體展開
- `_rrf_merge` — 語意 + 關鍵字 rank fusion 排序
- `inject_context` — str / list content 兩種格式注入
- `_last_user_text` — 訊息往回找最後 user 文字

> **重要**：這些測試不需要 DB 也不需要 LLM，全部 mock。真實整合測試請在
> 你 fork 之後針對自己的資料與模型補齊（建議用 respx 攔 httpx 呼叫）。

---

## 進階主題

### 加上 tool-driven RAG（multi-turn）

把 `retrieve_context()` 改造成 `anila-core` 的 `VectorSearchTool` 等 ToolDefinition，
然後在 `/v1/chat/completions` handler 內改用 `QueryEngine`：

```python
from anila_core.engine.query_engine import QueryEngine, QueryConfig
from anila_core.tools import VectorSearchTool, KeywordSearchTool, ReadDocumentTool

engine = QueryEngine(QueryConfig(
    model=MODEL,
    allowed_tools=[VectorSearchTool(_pool), KeywordSearchTool(_pool), ReadDocumentTool(_pool)],
))
async for event in engine.run_turn(messages):
    ...
```

詳見 [`../anila-core/README.md`](../anila-core/README.md) 的 QueryEngine 章節。

### 加上更多 agent 特有的行為

- 自訂系統提示：set `RAG_SYSTEM_PROMPT` 或改 `api.py:60-67` 的預設
- 自訂 thinking block 內容：改 `_stream_with_rag_trace`
- 自訂來源清單格式：改 `retrieve_context` 的 `src_lines` 組裝段落
- 接不同向量庫（Milvus / Weaviate / Qdrant）：替換 `_vector_search` 並在
  `index_documents.py` 換掉 `pgvector_store` adapter

---

## License

見 [`LICENSE`](./LICENSE)。基於上游 ANILA 專案授權。

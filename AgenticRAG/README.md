# ANILA Core

ANILA Core 是一個面向 **Agentic RAG** 的 Python Runtime，
提供「可組裝、可測試、可替換基礎設施」的架構。

支援文件上傳、語意向量檢索、RAG 上下文注入，並整合完整的 Agent 協調框架。

## 目錄

- [功能亮點](#功能亮點)
- [架構總覽](#架構總覽)
- [模組地圖](#模組地圖)
- [快速啟動（Docker）](#快速啟動docker)
- [本機開發啟動](#本機開發啟動)
- [批次索引文件](#批次索引文件)
- [OpenWebUI 整合（api.py）](#openwebui-整合apipy)
- [AgenticRAG 端點（/agentic-chat）](#agenticrag-端點agentic-chat)
- [System Prompt 設定](#system-prompt-設定)
- [API 端點](#api-端點)
- [SSE 事件](#sse-事件)
- [環境變數](#環境變數)
- [測試](#測試)
- [Release Notes](#release-notes)
- [License](#license)

## 功能亮點

**RAG 管線**
- 文件解析：`.txt` `.md` `.pdf` `.docx` `.doc` `.odt`（6 種格式）
- 自製 `RecursiveTextSplitter`（heading → 段落 → 句子 → 字元層級分割）
- Embedding：Nvidia NV-Embed-V2（dim=4096）via OpenAI-compatible endpoint
- 向量儲存：PostgreSQL + pgvector（cosine distance，IVFFlat index）
- RAG Pre-processor：在每次 LLM 呼叫前自動注入相關段落

**AgenticRAG（v0.3.0 新增）**
- **Tool-driven RAG**：LLM 自主決定何時搜尋、搜什麼、是否需要多輪檢索
- 3 個 RAG 工具：`vector_search`（語意搜尋）、`keyword_search`（關鍵字搜尋）、`read_document`（讀取完整文件）
- `POST /agentic-chat` 端點：SSE 串流，支援多輪 tool calling
- Layer 3 滑動窗口壓縮：超長對話的 hard truncation fallback
- JSON Schema `integer` → `number` 自動正規化（gemma4 相容）
- System prompt 預設注入 + `RAG_MIN_SCORE_RETRY` 動態門檻

**Agent 協調**
- Agent Registry（YAML / Markdown frontmatter，欄位驗證與覆蓋策略）
- Tool Router（allow/deny/wildcard，並行執行 concurrency-safe 工具）
- Query Engine：7 階段 turn loop + BudgetTracker + diminishing returns 停止條件
- Context Isolation：contextvars 實作 subagent 隔離
- Coordinator Mode：多 worker 並行或序列執行
- Compact + Session Memory：降低 token 壓力並保留會話脈絡
- Memory Lifecycle：萃取 → 相關性選擇 → 跨 session 整合

**OpenWebUI 整合（api.py）**
- OpenAI-compatible `/v1/chat/completions` proxy（port 24786）
- **Hybrid Search**：語意搜尋（pgvector）+ 關鍵字搜尋（ILIKE）並行，RRF 融合排名
  - 語意搜尋：擅長概念性問題（「記過的條件是什麼」）
  - 關鍵字搜尋：擅長精確詞彙（「第8條」「第三章」），自動處理 PDF 字間空格
- RAG 檢索結果顯示於 thinking block（`reasoning_content`），標注匹配方式
- 回覆末尾附來源清單：高相關度粗體、低相關度加提示
- 可直接在 OpenWebUI 新增連線：`http://host:24786`

**基礎設施**
- FastAPI + SSE 事件串流
- Bearer token 驗證 middleware
- PostgreSQL 儲存（Session / Message / RetrievalTrace）
- Docker Compose 一鍵啟動（3 個服務：`api` port 8000 + `rag-api` port 24786 + `db`）

## 架構總覽

```
┌────────────────────────────────────────────────────────────────┐
│                        FastAPI / SSE                           │
│   /chat   /agentic-chat   /documents/*   /search   /health     │
├─────────────────────┬──────────────────────────────────────────┤
│   QueryEngine       │   IngestionService                       │
│   (7-stage loop)    │   Parser → Chunker → Embed → pgvector    │
│   + RagPreprocessor │                                          │
├─────────────────────┼──────────────────────────────────────────┤
│   RAG Tools (new)   │                                          │
│   vector_search     │                                          │
│   keyword_search    │                                          │
│   read_document     │                                          │
├─────────────────────┴──────────────────────────────────────────┤
│  Coordinator │ Registry │ Router │ Compact │ Memory            │
├────────────────────────────────────────────────────────────────┤
│  Storage Adapters                                              │
│  PgVectorStore  │  PostgresStore  │  MemoryFileStore           │
├────────────────────────────────────────────────────────────────┤
│  Providers                                                     │
│  NvidiaEmbeddingProvider  │  OpenAICompatProvider  │  Mock     │
├────────────────────────────────────────────────────────────────┤
│  PostgreSQL + pgvector                                         │
└────────────────────────────────────────────────────────────────┘
```

## 模組地圖

```text
src/anila_core/
  app_factory.py          # ASGI 入口點，完整 RAG stack 接線
  config.py               # pydantic-settings（環境變數 / .env）
  api/
    server.py             # FastAPI create_app + /chat + /health
    documents.py          # POST /documents/upload, /ingest, GET/DELETE
    search.py             # POST /search（語意搜尋）
    events.py             # SSE 事件類型定義
    middleware/auth.py    # Bearer token 驗證
  ingestion/
    parsers.py            # 6 格式文件解析器 + ParserRegistry
    chunker.py            # RecursiveTextSplitter
    service.py            # IngestionService（解析→分塊→向量→索引）
  engine/
    query_engine.py       # 7 階段 turn loop
    rag_preprocessor.py   # RAG 上下文注入
    budget_tracker.py     # Token 預算管理
  providers/
    embedding_nvidia.py   # NV-Embed-V2（batch=50，dim=4096）
    embedding_mock.py     # 測試用 mock embedding
    openai_compat.py      # OpenAI-compatible LLM provider
    mock.py               # 測試用 mock LLM provider
  storage/
    ports.py              # Protocol 介面定義
    adapters/
      pg_pool.py          # asyncpg 連線池
      pgvector_store.py   # DocumentStore + RetrievalProvider
      postgres_store.py   # SessionStore + MessageStore + TraceStore
      memory_file_store.py# 檔案系統 MemoryStore
  compact/                # auto/micro compact + session memory + sliding window
  coordinator/            # 多 worker 協調
  context/                # contextvars agent 隔離
  memory/                 # 萃取、相關性選擇、整合、memdir
  models/                 # Pydantic v2 domain models
  registry/               # Agent 定義載入
  router/                 # Tool 策略與執行
  tools/
    __init__.py           # RAG 工具定義（vector_search, keyword_search, read_document）
    prompts.py            # AgenticRAG system prompt

api.py                    # OpenWebUI-compatible RAG proxy（port 24786）
index_documents.py        # 批次文件索引腳本
docker-compose.yml        # 3 服務：api(8000) + rag-api(24786) + db(5432)
Dockerfile                # Multi-stage build（antiword + asyncpg）
.env.example              # 完整環境變數範本
```

## 快速啟動（Docker）

### 前置需求

- Docker + Docker Compose
- Nvidia NIM API Key 或自架 embedding endpoint

### 步驟

```bash
# 1. 複製並填入環境變數
cp .env.example .env
# 編輯 .env，至少設定：
#   LLM_URL / LLM_API_KEY / MODEL
#   EMBEDDING_URL / EMBEDDING_API_KEY

# 2. 啟動所有服務
docker compose up -d

# 3. 確認健康狀態
curl http://localhost:8000/health
# {"status":"ok"}
```

服務啟動後：

| 服務 | 位址 | 說明 |
|------|------|------|
| `api` | `http://localhost:8000` | ANILA Core（/chat, /documents, /search） |
| `rag-api` | `http://localhost:24786` | OpenWebUI-compatible RAG proxy |
| `db` | `localhost:5432` | PostgreSQL + pgvector |

停止：

```bash
docker compose down
```

## 本機開發啟動

### 安裝

```bash
pip install -e ".[rag,dev]"
```

### 啟動 PostgreSQL（需 pgvector）

```bash
docker compose up -d db
```

### 啟動 API

```bash
uvicorn anila_core.app_factory:app --host 0.0.0.0 --port 8000 --reload
```

## 批次索引文件

`index_documents.py` 是一個獨立腳本，透過 API 批次上傳、索引、管理文件。

### 索引文件

```bash
# 基本：索引 data/documents/ 下所有文件
python3 index_documents.py

# 指定資料夾
python3 index_documents.py --dir /path/to/docs

# 指定 user / project scope
python3 index_documents.py --user alice --project myproject

# 指定 API 位址（預設 localhost:8000）
python3 index_documents.py --api http://192.168.1.10:8000
```

支援格式：`.txt` `.md` `.pdf` `.docx` `.doc` `.odt`

### 列出已索引文件

```bash
python3 index_documents.py --list
```

輸出範例：

```
#    document_id                            chunks  檔名
--------------------------------------------------------------------------------
1    3f2a1b4c-...                               42  陸海空軍懲罰法.pdf
2    9e8d7c6b-...                               18  操作手冊.docx
```

### 刪除文件

```bash
# 依檔名刪除（支援部分匹配）
python3 index_documents.py --delete 陸海空軍懲罰法.pdf

# 依 document_id 刪除（支援多個）
python3 index_documents.py --delete 3f2a1b4c-... 9e8d7c6b-...

# 混用檔名與 ID
python3 index_documents.py --delete 陸海空軍 9e8d7c6b-...

# 刪除全部（需確認）
python3 index_documents.py --delete-all

# 刪除全部（跳過確認，適合自動化腳本）
python3 index_documents.py --delete-all --yes
```

### 索引後語意查詢

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "你的問題", "user_id": "alice", "project_id": "myproject", "top_k": 5}'
```

## OpenWebUI 整合（api.py）

`api.py` 是一個獨立的 OpenAI-compatible RAG proxy，讓 **OpenWebUI** 或任何 OpenAI-compatible client 透過標準 `/v1/chat/completions` 端點使用帶有 RAG 的 LLM 對話。

### 架構

```
OpenWebUI
  │  POST /v1/chat/completions
  ▼
api.py (port 24786)
  ├─ 取最後一則 user message
  ├─ [並行] NV-Embed-V2 向量化 → pgvector 語意搜尋
  ├─ [並行] ILIKE 關鍵字搜尋（含字間空格展開）
  ├─ RRF（Reciprocal Rank Fusion）融合兩種結果
  ├─ 注入 RAG context 到 messages
  └─ 轉發至後端 LLM → stream 回傳
       ├─ thinking block：顯示各 chunk 匹配方式 + RRF 分數
       └─ 回覆末尾：附來源清單（標注匹配方式與相關度）
```

### 啟動

Docker Compose（隨 `docker compose up` 自動啟動）：
```yaml
rag-api:
  ports: ["24786:24786"]
```

或單獨執行：
```bash
python3 api.py
# 或
uvicorn api:app --host 0.0.0.0 --port 24786
```

### OpenWebUI 設定

1. 進入 **Settings → Connections → OpenAI API**
2. 新增連線：
   - URL：`http://<host>:24786`
   - API Key：任意字串（不驗證）
3. 選擇模型 `rag/google/gemma4`（依 `.env` 的 `MODEL` 自動命名）
4. 開始對話，RAG 結果會自動注入

### 相關設定（`.env`）

```bash
RAG_TOP_K=5              # 每次檢索幾筆（語意 + 關鍵字各取此數，RRF 後再取 top-k）
RAG_MIN_SCORE=0.7        # 語意搜尋主要門檻；低於此分的來源會標注「低相關度」
RAG_MIN_SCORE_RETRY=0.3  # 語意搜尋無結果時的 fallback 門檻
```

> **注意：** NV-Embed-V2（4096 維）在高維空間下，不同 chunk 的相似度會集中在相近範圍
> （例如 0.52 ~ 0.56），這是正常現象。建議優先看**排名**而非絕對分數，
> 並透過 Hybrid Search 讓關鍵字查詢補足語意搜尋的不足。

## AgenticRAG 端點（/agentic-chat）

`POST /agentic-chat` 是 **tool-driven RAG** 端點。與 `/chat`（pre-process injection）不同，
此端點讓 LLM **自主決定**何時搜尋知識庫、用哪種搜尋、是否需要多輪檢索。

### 兩種 RAG 模式對比

| 模式 | 端點 | 決策者 | 搜尋策略 |
|------|------|--------|---------|
| Pre-process（被動） | `POST /chat` + `api.py` | 系統自動 | 每次對話前 auto embed → search → inject |
| AgenticRAG（主動） | `POST /agentic-chat` | LLM 自行判斷 | 不搜 / 單次 / 多輪 / 混合搜尋 |

### AgenticRAG 流程

```
用戶提問 → LLM 思考 → 需要搜尋？
  ├─ 是 → LLM 呼叫 vector_search("語意查詢")
  │        → 結果不夠？→ LLM 呼叫 keyword_search("關鍵字")
  │        → 想看全文？→ LLM 呼叫 read_document(doc_id)
  │        → 足夠 → LLM 生成答案（引用來源）
  └─ 否 → LLM 直接回答
```

### RAG 工具

| 工具名稱 | 說明 | 適用場景 |
|----------|------|---------|
| `vector_search` | 語意向量搜尋（pgvector cosine） | 概念性問題、模糊查詢 |
| `keyword_search` | 關鍵字匹配（pg_trgm / ILIKE） | 特定術語、名稱、代碼 |
| `read_document` | 讀取完整文件內容 | 搜尋結果中的片段想看全文 |

### 使用範例

```bash
curl -X POST http://localhost:8000/agentic-chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "s-001",
    "user_message": "請說明系統架構的設計原則",
    "user_id": "user-1",
    "project_id": "proj-1"
  }'
```

回傳 SSE 串流，事件格式與 `/chat` 相同（`message_delta`, `tool_call_started`, `usage_update`, `stream_done`）。

### 自訂 System Prompt

預設使用內建的 AgenticRAG system prompt（指示 LLM 如何使用搜尋工具）。
可在 request body 中帶 `system_prompt` 覆蓋：

```json
{
  "session_id": "s-001",
  "user_message": "...",
  "system_prompt": "你是法律助理。搜尋時優先使用 keyword_search 找法規條文。"
}
```

預設 prompt 位於 `src/anila_core/tools/prompts.py`。

## System Prompt 設定

System prompt 在主架構中共有 **三個層次**，可依使用情境選擇：

| 設定位置 | 方式 | 適用場景 |
|----------|------|---------|
| Agent `.md` 檔 | Markdown body = system_prompt | 多 Agent 架構，各 agent 角色各異 |
| `POST /chat` request body | 帶 `system_prompt` 欄位 | 使用 ANILA Core `/chat` 端點時 |
| `api.py` | 修改 messages 插入 system message | OpenWebUI RAG proxy |

---

### 層次一：Agent 定義層（Agent `.md` 檔）

`src/anila_core/models/agent.py:41`、`registry/agent_registry.py:103-105`

在 `agents/` 目錄下建立 Markdown 檔，**Markdown body 自動成為該 Agent 的 system prompt**：

```markdown
---
name: legal-assistant
model: google/gemma4
---
你是一個專業的法律助理。請根據檢索到的文件內容回答問題，
引用具體條文時請標注來源。若文件中找不到相關資訊，請明確告知。
```

> 目前 `agents/` 資料夾不存在，代表只有 default agent，system prompt 為空字串。

---

### 層次二：API 請求層（POST /chat）

`src/anila_core/api/server.py:53` → `query_engine.py:52` → `openai_compat.py:120`

呼叫 `/chat` 時在 request body 帶 `system_prompt`，最終組裝成
`{"role": "system", "content": "..."}` 放到 messages 最前面送給 LLM：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "s-001",
    "user_message": "請分析這份文件",
    "system_prompt": "你是一個專業的法律助理，請根據檢索到的文件內容回答問題。"
  }'
```

傳遞路徑：`ChatRequest.system_prompt` → `QueryConfig.system_prompt` → LLM `system` 欄位

---

### 層次三：api.py（OpenWebUI RAG Proxy）

`api.py` 在 server 端**自動注入** system prompt（`api.py` 頂部讀取 `RAG_SYSTEM_PROMPT` 環境變數）。
若 client 的 messages 中已有 `role=system`，則不覆蓋（client 優先）。

預設 system prompt 指示 LLM 使用繁體中文、優先引用 RAG context 並標注來源。

自訂方式：

```bash
# .env 中設定（永久生效）
RAG_SYSTEM_PROMPT="你是一個專業的法律助理，請根據檢索到的文件內容回答，引用具體條文時請標注來源。"
```

或在 OpenWebUI 的 **Model Settings → System Prompt** 欄位設定（client 端覆蓋）。

## API 端點

### Agent Chat

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/chat` | 啟動 agent query loop（pre-process RAG），回傳 SSE 串流 |
| `POST` | `/agentic-chat` | 啟動 AgenticRAG（tool-driven RAG），回傳 SSE 串流 |
| `GET` | `/sessions/{id}/away_summary` | 取得離開期間的摘要 |
| `POST` | `/sessions/{id}/compact` | 手動觸發 compact |
| `GET` | `/health` | 服務健康探針 |

**POST /chat 範例**

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "s-001",
    "user_message": "請介紹這份文件的主要內容",
    "model": "google/gemma4"
  }'
```

### 文件管理

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/documents/upload` | 上傳文件檔案（multipart/form-data） |
| `POST` | `/documents/ingest` | 解析 + 分塊 + 向量化 + 索引 |
| `GET` | `/documents` | 列出所有已索引文件 |
| `GET` | `/documents/{id}` | 取得文件詳情 |
| `GET` | `/documents/{id}/status` | 取得攝取狀態 |
| `DELETE` | `/documents/{id}` | 刪除文件及所有向量 |

**上傳並攝取文件範例**

```bash
# 上傳
curl -X POST http://localhost:8000/documents/upload \
  -F "file=@report.pdf" \
  -F "user_id=user-1" \
  -F "project_id=proj-1"

# 回傳 {"file_path": "/tmp/anila_uploads/...", "filename": "report.pdf", ...}

# 攝取（向量化 + 索引）
curl -X POST http://localhost:8000/documents/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "/tmp/anila_uploads/report.pdf",
    "document_id": "doc-report-001",
    "user_id": "user-1",
    "project_id": "proj-1"
  }'
```

### 語意搜尋

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/search` | 語意向量搜尋（NV-Embed-V2 + pgvector） |

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "系統架構設計原則",
    "user_id": "user-1",
    "project_id": "proj-1",
    "top_k": 5
  }'
```

### Auth（可選）

設定 `.env` 中的 `API_KEY` 後，所有端點需帶 Bearer token：

```bash
curl -H "Authorization: Bearer <your-api-key>" http://localhost:8000/health
```

留空則不啟用驗證（開發模式）。

## SSE 事件

`POST /chat` 回傳 `text/event-stream`，事件類型：

| 事件 | 說明 |
|------|------|
| `message_delta` | LLM 回應文字片段 |
| `reasoning_delta` | 推理過程文字（若模型支援） |
| `tool_call_started` | 工具呼叫開始 |
| `tool_call_finished` | 工具呼叫結束（含結果） |
| `task_notification` | 多 worker 任務狀態通知 |
| `agent_summary` | Subagent 完成摘要 |
| `usage_update` | Token 使用量更新 |
| `memory_saved` | 記憶寫入通知 |
| `compact_triggered` | Compact 觸發通知 |
| `away_summary` | 離開期間摘要 |
| `stream_done` | 串流結束 |
| `error` | 錯誤事件 |

## 環境變數

完整設定請參考 `.env.example`。主要變數：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `LLM_URL` | `https://172.16.120.35/v1` | LLM endpoint base URL |
| `LLM_API_KEY` | `not-set` | LLM API key |
| `MODEL` | `google/gemma4` | 預設模型名稱 |
| `EMBEDDING_URL` | `https://172.16.120.35/v1` | Embedding endpoint |
| `EMBEDDING_API_KEY` | `not-set` | Embedding API key |
| `EMBEDDING_MODEL` | `Nvidia/NV-embed-V2` | Embedding 模型 |
| `EMBEDDING_DIMENSION` | `4096` | 向量維度 |
| `EMBEDDING_VERIFY_SSL` | `false` | 是否驗證 TLS |
| `DATABASE_URL` | `postgresql://anila:anila@localhost:5432/anila_rag` | PostgreSQL DSN |
| `CHUNK_SIZE` | `512` | 分塊大小（tokens） |
| `CHUNK_OVERLAP` | `50` | 分塊重疊（tokens） |
| `RAG_TOP_K` | `5` | 每次檢索筆數 |
| `RAG_MIN_SCORE` | `0.7` | 最低相似度門檻 |
| `RAG_MIN_SCORE_RETRY` | `0.3` | api.py fallback 重試門檻（找不到結果時自動降低再試一次）|
| `RAG_SYSTEM_PROMPT` | （內建中文 RAG 助理） | api.py 預設 system prompt；client 帶 system message 時自動跳過 |
| `API_KEY` | （空） | 留空不啟用驗證 |
| `API_DEV_MODE` | `false` | `true` 則跳過驗證 |
| `UPLOAD_DIR` | `/tmp/anila_uploads` | 上傳暫存目錄 |

## 測試

```bash
# 安裝開發依賴
pip install -e ".[rag,dev]"

# 執行全部測試（不需要資料庫，全部使用 mock）
pytest tests/ -v

# 程式碼品質
ruff check src tests
mypy src
```

目前測試：**182 tests，全部通過**，無需真實 LLM / Embedding / DB 連線。

測試覆蓋：

| 檔案 | 測試對象 |
|------|---------|
| `test_parsers.py` | 6 種文件格式解析 |
| `test_chunker.py` | RecursiveTextSplitter 分塊邏輯 |
| `test_ingestion_service.py` | IngestionService 整合流程 |
| `test_embedding_mock.py` | Mock Embedding Provider |
| `test_rag_preprocessor.py` | RAG 上下文注入 |
| `test_engine.py` | QueryEngine turn loop |
| `test_coordinator.py` | 多 worker 協調 |
| `test_compact.py` | Compact 服務 |
| `test_memory.py` | Memory lifecycle |
| `test_registry.py` | Agent Registry |
| `test_router.py` | Tool Router |
| `test_rag_tools.py` | RAG 工具（vector_search, keyword_search, read_document） |
| `test_agentic_chat.py` | AgenticRAG /agentic-chat 端點 |
| `test_sliding_window.py` | 滑動窗口壓縮 + JSON Schema 正規化 |

## Release Notes

### v0.3.0

**AgenticRAG — Tool-driven RAG loop**

核心新增：
- `POST /agentic-chat` 端點 — LLM 自主決定搜尋策略的 AgenticRAG
- `VectorSearchTool` — 語意向量搜尋工具（wraps pgvector）
- `KeywordSearchTool` — 關鍵字搜尋工具（pg_trgm / ILIKE）
- `ReadDocumentTool` — 完整文件讀取工具

增強：
- Layer 3 滑動窗口壓縮 (`compact/sliding_window.py`) — 超長對話 hard truncation
- JSON Schema `integer` → `number` 自動正規化（gemma4 相容性）
- `api.py` system prompt 預設注入（env `RAG_SYSTEM_PROMPT`）
- `api.py` `RAG_MIN_SCORE_RETRY` — 低門檻 fallback 重試

測試：182 tests 全部通過（新增 28 tests）

### v0.2.3

**Hybrid Search — 語意 + 關鍵字並行搜尋**

- `api.py` 改為 Hybrid Search：語意搜尋（pgvector）與關鍵字搜尋（ILIKE）並行執行，RRF 融合排名
- 關鍵字搜尋自動展開 PDF 字間空格變體（「第8條」→ 也搜「第 8 條」）
- 來源清單標注匹配方式：`語意 0.xxx + 關鍵字` / `關鍵字匹配` / `相似度 0.xxx`
- 全部結果低於 `RAG_MIN_SCORE` 時，整個來源區塊加提示「相關度較低，僅供參考」
- `GET /documents` 改從 DB 讀取（不再依賴 in-memory status，容器重啟後清單仍正確）
- `index_documents.py` 新增 `--list` / `--delete` / `--delete-all` / `--yes`

### v0.2.2

System Prompt 三層架構文件更新：
- Agent 定義層（.md 檔）
- API 請求層（/chat request body）
- api.py 層（OpenWebUI proxy）

### v0.2.1

新增 OpenWebUI 整合：

- `api.py`：OpenAI-compatible RAG proxy（port 24786）
  - 自動 embed → pgvector 檢索 → context inject
  - RAG 結果輸出至 thinking block（`reasoning_content`）
  - 回覆末尾附來源清單（文件名 + 相似度）
- `docker-compose.yml` 新增 `rag-api` 服務（port 24786）

### v0.2.0

完整 Agentic RAG 後端實作：

- 文件解析管線（6 格式）+ RecursiveTextSplitter
- Nvidia NV-Embed-V2 embedding provider
- pgvector 向量儲存（cosine search + IVFFlat）
- RAG Pre-processor（query → embed → search → context inject）
- PostgreSQL 儲存 adapter（Session / Message / RetrievalTrace）
- 文件管理 API + 語意搜尋 API
- 統一 pydantic-settings 配置
- Bearer token auth middleware
- Docker Compose 部署（API + pgvector DB）
- 全端 app_factory 接線

### v0.1.0

初始版本，包含：

- Agent 協調核心（registry、router、engine、coordinator）
- Compact + Memory lifecycle 服務
- Provider abstraction + FastAPI/SSE 介面

## License

This repository includes a `LICENSE` file. See it for terms.

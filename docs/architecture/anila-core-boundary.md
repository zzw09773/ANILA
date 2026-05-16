# anila-core 邊界 — Task 3 執行 Spec

**Status**: Ready for execution as part of Ingestion Platform Sprint 1
**Date**: 2026-04-25
**Parent doc**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) §12
**Sibling doc**: [`multi-service-integration-plan.md`](./multi-service-integration-plan.md) — 多服務整合計畫（含 codeserver dev credentials 與 RLS 整合，§5.3 引用本文件）
**Driver**: ANILA Ingestion Platform 中央化需要 anila-core 退回 pure runtime；同時 README 早就標記「Task 3 pending」

---

## 1. 問題陳述

`anila-core/README.md` 的「Note (Task 3 pending)」段落寫：

> 現階段 `ingestion/`、`storage/adapters/{pg_pool,pgvector_store,postgres_store}.py`、`providers/embedding_nvidia.py`、`engine/rag_preprocessor.py` 等仍留在 anila-core tree 裡（透過 `[rag]` extras 啟用）。**下一輪會把這些 RAG-specific 檔案搬回 AgenticRAG template，讓 core 真正成為 pure runtime。**

這個「下一輪」一直沒做。同時 ANILA Ingestion Platform 設計（[`ingestion-platform-design.md`](./ingestion-platform-design.md)）將會提供中央化的 ingestion 服務，**現在不做 anila-core 瘦身就會出現三套 ingestion**：

```
1. anila-core/ingestion/         (歷史遺物，README 早就要刪)
2. AgenticRAG/ingestion/         (template 自用)
3. CSP /api/ingestion/*          (新平台)
```

雙軌已是錯誤，三軌不可接受。本 spec 定義要刪什麼、要留什麼、判定原則、執行步驟。

---

## 2. anila-core 該長什麼樣子

### 2.1 一句話原則

> **anila-core = 「任何 agent type 都會用到」的 runtime primitives；不是 「90% agent 是 RAG，所以 RAG 用得到的就放進 core」。**

判定法：問自己

> 做一個 **workflow agent / API-orchestration agent / search-bot agent / chatbot agent**，會用到這個檔嗎？

- **會** → 留在 anila-core
- **只有 RAG agent 會用** → 搬出去（搬到 AgenticRAG template 或刪除）

### 2.2 KEEP — 留在 anila-core 的 pure runtime

| 模組 | 為什麼是 runtime 通用 |
|---|---|
| `engine/query_engine.py` + `engine/budget_tracker.py` | Turn loop / token budget — 任何 agent 都需要 |
| `coordinator/` | Multi-step decomposition / sub-agent dispatch — 通用 |
| `compact/` 全部（micro / auto / session_memory / sliding_window）| Context window 管理 — 任何長對話 agent 都需要 |
| `memory/` | Memdir / extract / relevance — 通用 memory infra |
| `context/agent_context.py` | Turn-scope DI / subagent fork — 通用 |
| `registry/` | Agent definition load + remote agent manifest cache — 通用 |
| `router/tool_router.py` | ToolRegistry / 並行工具執行 — 通用 |
| `tools/dispatch_tool.py` | Agent 之間互呼叫 — 通用（**不是 RAG**）|
| `tools/prompts.py` 內的非 RAG-specific prompt 片段 | 通用 prompts |
| `providers/base.py` | LLM provider 抽象 — 通用 |
| `providers/openai_compat.py` | OpenAI 相容 provider — 通用 |
| `providers/csp_platform.py` | CSP proxy provider — 通用 |
| `providers/mock.py` | 測試用 — 通用 |
| `models/` 全部 Pydantic models（agent / message / memory / tool / storage Protocol）| 通用 domain types |
| `storage/ports.py` | **Protocol 定義**（介面 contract）— 通用，但 implementation 不留 |
| `api/router_server.py` | Router service factory — 通用（dispatcher 不是 RAG 專屬）|
| `api/middleware/auth.py` | CSP service-token 驗證 — 通用 |
| `api/server.py` 的 `/chat` `/agentic-chat` `/health` 三個 endpoints | Chat loop runtime — 通用（chat 是 agent 共通能力）|
| `api/events.py` | SSE event types — 通用 |
| `cli/` (init / status / register) + `cli/templates/agent-template/` | Dev tooling — 通用 |
| `config.py` | Settings — 通用（但要拔掉 RAG 相關欄位，見 §2.3）|

### 2.3 REMOVE — 從 anila-core 刪除

| 模組 / 檔案 | 為什麼是 RAG-specific | 處置 |
|---|---|---|
| `ingestion/` 整個目錄（`parsers.py` / `chunker.py` / `service.py` / `__init__.py`）| 文件解析、chunking、ingestion orchestration — 純 RAG 概念 | **直接刪除**（無 production caller，AgenticRAG template 已有完整版） |
| `storage/adapters/pg_pool.py` | asyncpg 連線池，只給 pgvector 用 | **刪除** |
| `storage/adapters/pgvector_store.py` | 特定向量庫 implementation | **刪除**（新平台會建 v2 在自己模組裡） |
| `storage/adapters/postgres_store.py` | RAG 用的 SessionStore / TraceStore | **刪除** |
| `storage/adapters/memory_file_store.py` | `MemoryStore` Protocol 的檔案系統 implementation；存 Markdown + frontmatter 到 `{base_dir}/{user_id}/{project_id}/*.md`，給 anila-core 的 `memory/` module（memdir / extract / relevance / consolidation）做 backend | **KEEP**（dev / standalone mode 用）— 並非 RAG-specific，是平台 memory infra 的一個 storage 選項。**Phase 3+ 補一個 `PostgresMemoryStore` impl** 給 prod 用（中央化、可加 RLS 機敏隔離），兩個 impl 並存讓 deployment 自選。Memory module 本身（memdir / extract / relevance / consolidation）完全留 anila-core，見 §2.2 KEEP list |
| `providers/embedding_nvidia.py` | 特定 embedding 模型 (NV-Embed-V2) | **刪除**（新平台會在 ingestion-worker 內接） |
| `engine/rag_preprocessor.py` | RAG context injection（pre-process pattern）| **刪除**（pattern 不再使用，新平台改 tool-driven） |
| `tools/__init__.py` 內的 `create_vector_search_tool` / `create_keyword_search_tool` / `create_read_document_tool` | 三個 RAG-specific tool factory | **搬到** `AgenticRAG/src/agentic_rag/tools/`（template 自帶；anila-core 不認） |
| `api/documents.py` (`/upload` `/ingest` `/status`) | RAG 文件管理 endpoints | **刪除** |
| `api/search.py` (`POST /search`) | RAG 語意搜尋 endpoint | **刪除** |
| `app_factory.py` 內的 RAG 接線（`IngestionService` / `HierarchicalChunker` / `VisionProvider` / `RagPreprocessor`）| RAG-specific composition | **拔掉**（保留 router-only 與 chat-only 兩種 mode） |
| `config.py` 內 RAG-specific 欄位（chunk_size / chunk_overlap / rag_top_k / rag_min_score / rag_include_parent_context / vision_* / pg_pool_* / pg_ssl / embedding_*）| RAG-specific settings | **刪除**（搬到 AgenticRAG template 的 config） |

### 2.4 兩個 Grey Zone（值得明確拍板）

#### Grey Zone A：Storage Protocol（介面）vs Adapter（實作）

`storage/ports.py` 定義的 Protocol **留 core**（這是 interface contract，未來若要支援 milvus / qdrant 也是同一個 Protocol）。任何 adapter 實作（pgvector / memory file / future qdrant）都搬出去。

→ 結論：**Protocol 留，adapter 全搬。**

#### Grey Zone B：`api/server.py` 的 `/agentic-chat` endpoint

這個 endpoint 內部會 wire RAG tools（vector_search 等）。endpoint 本身是通用的（chat loop），但 wiring 是 RAG-specific。

→ 結論：**endpoint 本身留 core，但 RAG tools 的 wiring 從 endpoint 內部抽出去**，改成「caller 傳入已 register 的 ToolRegistry」。core 不需要知道有 RAG 這回事。

具體改動：
```python
# anila_core/api/server.py — BEFORE (current)
@app.post("/agentic-chat")
async def agentic_chat(request: ChatRequest):
    from ..tools import (
        create_vector_search_tool,    # ← RAG-specific import
        create_keyword_search_tool,
        create_read_document_tool,
    )
    agentic_registry = ToolRegistry()
    if embedding_provider is not None and retrieval_provider is not None:
        agentic_registry.register(create_vector_search_tool(...))
    ...

# anila_core/api/server.py — AFTER (target)
@app.post("/agentic-chat")
async def agentic_chat(request: ChatRequest):
    # core 只認 ToolRegistry，不認 RAG tool 名字
    # 由 caller (AgenticRAG template / 其他 agent) 自己 register tools
    # 進來的 tool_registry 是已配置好的
    ...
```

---

## 3. 執行計畫（Sprint 1，與 Ingestion Platform 同步進行）

### 3.1 順序與相依

```
Day 1-3   ─── ① RAG tools 從 anila-core/tools/ 搬到 AgenticRAG/tools/
              ② AgenticRAG/api.py 的 inline SQL 改寫前先確認還能跑

Day 4-6   ─── ③ 刪除 anila-core/ingestion/ + api/{documents,search}.py
              ④ 拔掉 anila-core/app_factory.py 的 RAG wiring
              ⑤ pytest anila-core 全綠

Day 7-9   ─── ⑥ 刪除 anila-core/storage/adapters/{pg_pool,pgvector_store,postgres_store}.py
              ⑦ 刪除 anila-core/providers/embedding_nvidia.py
              ⑧ 刪除 anila-core/engine/rag_preprocessor.py
              ⑨ 刪除 config.py 的 RAG 欄位
              ⑩ pytest anila-core + AgenticRAG 全綠

Day 10    ─── ⑪ Update anila-core README + CHANGELOG (BREAKING)
              ⑫ Sprint 1 G3 gate 驗證：grep document_chunks 只剩 1 檔
```

### 3.2 anila-core 自身測試的影響

刪除 ingestion 模組後，下列 anila-core test 會 break：

```
anila-core/tests/test_chunker.py
anila-core/tests/test_parsers.py
anila-core/tests/test_ingestion_service.py
anila-core/tests/test_rag_preprocessor.py
anila-core/tests/test_embedding_mock.py  # 部分（embedding mock 還是要留）
anila-core/tests/test_rag_tools.py
anila-core/tests/test_agentic_chat.py    # 部分（需 RAG tools 的測試項）
anila-core/tests/test_citation_and_vision.py
anila-core/tests/test_chunker_cjk.py
anila-core/tests/test_ocr_fallback.py
anila-core/tests/test_normalize.py
anila-core/tests/test_docling_parser.py
anila-core/tests/test_tokenize_zh.py
```

→ 全部從 `anila-core/tests/` **搬到** `AgenticRAG/tests/`（事實上 AgenticRAG/tests/ 已經有同名版本，只要 reconcile diff、保留較新版即可）

### 3.3 anila-core 升版

由於 anila-core 還沒 1.0 release（內部使用），**直接 breaking change** 是可接受的。但要做兩件事：

**a. CHANGELOG 明確標 BREAKING**

```markdown
# anila-core CHANGELOG

## v0.5.0 (2026-XX-XX) — Boundary cleanup (Task 3)

### BREAKING

- **Removed `anila_core.ingestion.*`** — use ANILA Ingestion Platform via CSP
  `/api/ingestion/*`. Standalone agents that need ingestion should fork the
  AgenticRAG template, which retains a complete ingestion pipeline for dev mode.
- **Removed `anila_core.storage.adapters.{pg_pool,pgvector_store,postgres_store}`** —
  use the new `pgvector_store_v2` shipped with the Ingestion Platform.
  `storage/ports.py` Protocol definitions are unchanged.
- **Removed `anila_core.api.{documents,search}`** — replaced by
  CSP `/api/ingestion/*` endpoints.
- **Removed `anila_core.providers.embedding_nvidia`** — embedding now happens
  inside ingestion-worker, agents do not embed directly.
- **Removed `anila_core.engine.rag_preprocessor`** — superseded by
  tool-driven RAG via the agentic-chat endpoint.
- **Moved** `vector_search` / `keyword_search` / `read_document` tool factories
  from `anila_core.tools` to `agentic_rag.tools`. Existing imports break.
- **Removed RAG-specific config fields** from `anila_core.config.Settings`:
  `chunk_size`, `chunk_overlap`, `rag_top_k`, `rag_min_score`,
  `rag_include_parent_context`, `vision_*`, `pg_pool_*`, `pg_ssl`, `embedding_*`.
  These now live in `agentic_rag.config.Settings`.

### Migration guide

If you forked AgenticRAG before 2026-04-25, your fork:
- ✅ Already has its own `ingestion/`、`tools/` 等目錄，不受影響
- ⚠️ 但若你的 fork import 過 `from anila_core.tools import create_vector_search_tool`，
  改為 `from agentic_rag.tools import ...`
- ⚠️ 若你的 fork 直接 `pip install "anila-core[rag]"`，改為
  `pip install -e ./AgenticRAG[rag]`（RAG extras 已搬到 template）

If you built a custom non-RAG agent on top of `anila-core`:
- ✅ Pure runtime (engine / coordinator / compact / memory / tools.dispatch_tool)
  unchanged. No migration needed.
```

**b. `pyproject.toml` 的 `[rag]` extras 移除**

```toml
# anila-core/pyproject.toml — BEFORE
[project.optional-dependencies]
rag = [
    "pymupdf4llm>=0.0.10",
    "asyncpg>=0.29",
    "pgvector>=0.3",
    ...
]

# AFTER
# (整段移除；rag extras 改在 AgenticRAG/pyproject.toml)
```

---

## 4. 風險與 Mitigation

| 風險 | 嚴重度 | Mitigation |
|---|---|---|
| 漏改某個 import，runtime 才發現 | 🟡 中 | Sprint 1 G3 gate（grep document_chunks 只剩 1 檔）會抓到；同時 pytest 兩邊全綠才能 merge |
| 有未知的外部 caller（內部 fork agent）使用了被刪除的 API | 🟡 中 | Slack 公告 + grep 整個 monorepo + onyx 也 grep 一次（雖然 onyx 不該依賴 anila-core）|
| `app_factory.py` 拔掉 RAG wiring 後，AgenticRAG 起不來 | 🔴 高 | AgenticRAG 改用自己的 `app_factory.py`（事實上已經有了）；anila-core 的 `app_factory.py` 只服務 router-only 部署 |
| Chat loop 的 `/agentic-chat` 需要 RAG tool registry 但 caller 沒帶 | 🟡 中 | `/agentic-chat` 改為「需 caller 帶 ToolRegistry」；無 tool 時退化成 `/chat` |
| Storage Protocol 變動破壞既有 mock | 🟡 中 | Protocol 本身**不動**；只動 implementation。Mock 都是用 Protocol，不受影響 |

---

## 5. 完成條件 / Definition of Done

Sprint 1 結束時 anila-core 必須符合：

- [ ] `find anila-core/src/anila_core -name '*.py'` 不再列出 `ingestion/` `storage/adapters/{pg_pool,pgvector_store,postgres_store}.py` `providers/embedding_nvidia.py` `engine/rag_preprocessor.py` `api/{documents,search}.py`
- [ ] `grep -rn "document_chunks" anila-core/src` 零命中
- [ ] `grep -rn "create_vector_search_tool\|create_keyword_search_tool\|create_read_document_tool" anila-core/src` 零命中
- [ ] `pip install -e ./anila-core` （無 extras）成功，import 任何 KEEP list 的模組都成功
- [ ] anila-core/tests 全綠（已搬走 RAG 相關 test）
- [ ] AgenticRAG template 在新版 anila-core 之上 `pytest tests/` 全綠
- [ ] anila-core README 的「Task 3 pending」段落改為「Task 3 (2026-XX-XX): completed — see CHANGELOG」
- [ ] anila-core CHANGELOG 寫好 BREAKING entry + migration guide

---

## 6. 與 Ingestion Platform 設計的關聯

| 本 doc | Ingestion Platform Doc |
|---|---|
| §2 KEEP/REMOVE list | §13 收斂方案 A++ 詳細展開 |
| §3 執行步驟 | §9 Sprint 1 task list 內含「anila-core 瘦身」項 |
| §3.2 測試搬移 | §9 Sprint 1 G3 gate（retrieval path 唯一性）|
| §4 風險 mitigation | §10 整體風險表 |

兩份 doc 共同前提：**anila-core 瘦身與 Ingestion Platform sprint 1 同步進行，不能拆開做**。

---

**Last updated**: 2026-04-25 · **Owner**: ANILA 平台團隊 · **Parent**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) §13

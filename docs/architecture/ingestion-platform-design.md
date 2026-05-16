# ANILA Ingestion Platform — Design Doc v0.2

**Status**: Draft for review
**Date**: 2026-04-25 (v0.2)
**Author**: ANILA 平台團隊
**Reviewers**: (待指派)
**Target delivery**: 3 × 2-week sprints after sign-off
**See also**:
- [`anila-core-boundary.md`](./anila-core-boundary.md) — anila-core 瘦身 Task 3 詳細清單，與本文件 §12 同步
- [`multi-service-integration-plan.md`](../planning/multi-service-integration-plan.md) — 組內既有服務（ANILA LM / ComfyUI / codeserver / n8n / gitlab）整合進 ANILA 的計畫，與本平台 sprint 排程交織

---

## 0. Decisions Log

### v0.4 (2026-04-27) — Sprint 4 collection-as-first-class

Sprint 1–3 假設「100 agent = 100 個獨立租戶」，所以 collection 跟 agent 1:1 綁定（`ingestion_collections.agent_id NOT NULL`、RLS keyed on `anila.agent_id`）。實際 dev workflow 證明這是 over-coupling — 平台的角色是 **pgvector + chunking infrastructure**，agent backend 自己寫 `RAG_COLLECTION_ID` 指過來就能用。

| 議題 | v0.3 立場 | v0.4 修訂 |
|---|---|---|
| Collection 擁有者 | 屬於 agent | **屬於 user**（`created_by NOT NULL`，platform-shared resource） |
| RLS GUC | `anila.agent_id` | **`anila.collection_id`**（每 connection 一個 collection 範圍；FORCE RLS + non-bypass csp_app role 不變） |
| SDK 命名 | `AgentScopedPgVectorStore(pool, agent_id=N)` | **`CollectionScopedPgVectorStore(pool, collection_id=N)`**；舊名一個 cycle alias |
| Agent backend env | `RAG_AGENT_ID` | **`RAG_COLLECTION_ID`** |
| LLM judge 憑證範圍 | per-agent (`agent_llm_credentials`) | **per-user (`user_llm_credentials`)**；一個 dev 一把 OpenAI key 通用所有自家 collection |
| CSP UI agent picker | 必須先選 agent 才看 collection | **拿掉**；landing 頁直接列 my collections；admin 可切「全部」 |
| `_require_agent_access` | 走 `UserAgentPermission` | **`_require_collection_access`** = admin 或 owner（`created_by`） |

並加入：
- **Migration 0019** drop agent_id 跟 RLS 重 key（含一個 ops trap：CSP 失敗 fallback `Base.metadata.create_all` 會 re-create orphan tables，migration 開頭要先 `DROP TABLE IF EXISTS user_llm_credentials CASCADE` 清乾淨）
- **G1/G2 重命名**：`test_g1_collection_isolation` / `test_g2_rls_bypass`（GUC 從 `anila.agent_id` 改 `anila.collection_id`）
- **CHANGELOG v0.7.0** 對應這次 7 個 chunks 的 commits（O→T）

### v0.3 (2026-04-25) — Sprint 1 實作回報 + schema/role 修訂

Sprint 1（Chunks A–G，commits `567bd9c` → `e9e913a`）實際實作後發現 4 個 design vs reality gap，本版同步修訂：

| # | 議題 | v0.2 立場 | v0.3 修訂 |
|---|---|---|---|
| 1 | Migration 編號 | 「migration 0012」 | **0012 已被 service_access_control 用走**；ingestion 用 **0014（4 張表 + RLS 啟用）+ 0015（halfvec(4000) 升級）**。design doc §3.1 對照表更新 |
| 2 | embedding 維度 | `vector(4096)` 配 ivfflat | **`halfvec(4000)` 配 HNSW**。pgvector 0.8.x 的 `vector` HNSW/IVFFlat 上限只到 2000-d；NV-embed-V2 native 4096 → worker truncate 到 4000 → halfvec 索引。drop 96 dim 在 Matryoshka 噪訊以下，halfvec 半 storage |
| 3 | RLS 啟用條件 | `ENABLE ROW LEVEL SECURITY` + Policy 即可 | **ENABLE + FORCE + 非 superuser/非 BYPASSRLS role 三者缺一不可**。預設 postgres image 的 `csp` role 是 superuser+BYPASSRLS 一律 bypass；遷出新的 `csp_app` runtime role + ownership transfer 才讓 RLS 真的擋。見新增的 §3.3 Layer 2-prime |
| 4 | Sprint 1 範圍 | 1 個 chunk size 覆蓋 | 實際拆 **A→G 七 chunk** 落地：A schema、B chunkers、C SDK、D CSP CRUD、E worker+halfvec、F AgenticRAG SDK 切換、G G1/G2/G3 pytest gate。見 §9 Sprint 1 修訂版 |

並加入：
- **§3.1 表頭** 標明 alembic 編號實際對應
- **§3.2 schema 修正版** halfvec(4000) + HNSW
- **§3.3 Layer 2-prime** csp_app role 拆分（PG superuser/BYPASSRLS 強制 bypass 的 trap）
- **§9 Sprint 1 已完成清單** 對應 commit hash
- **G1/G2/G3 pytest 落地路徑** 跟 stress 參數

### v0.2 (2026-04-25) — 4 議題 review 後的修正

`@reviewer` 提出 4 個高風險議題，經事實調查後修訂：

| # | 議題 | v0.1 立場 | v0.2 修訂 |
|---|---|---|---|
| 1 | 安全邊界要真正「不可繞過」 | 一個 `AgentScopedPgVectorStore` enforcement 點 | **4 層強制**（schema NOT NULL → Postgres RLS → code single entry → pytest random workload）。見 §3.3 |
| 2 | Evaluator 成本與吞吐 | 估算 ~$30/run 但無治理 | 加 **Fast/Standard/Deep 三段式 + per-agent 互斥 + platform budget gate + result cache + local judge 強烈推薦**。見 §6.5 |
| 3 | 狀態機 / 重試 | 抽象的 retry policy | 加完整 **Error Taxonomy**（4 大類 12 個 code，禁止 `E_UNKNOWN`，`E_PG_RLS_VIOLATION` 觸發告警）。見 §8.1 |
| 4 | anila-core 邊界（避免雙軌）| 方案 B：anila-core 改為 client SDK | **方案 A++：直接刪除 anila-core 內 ingestion**。事實調查發現 anila-core/ingestion/ 只有 tests 引用、production 無 caller，刪了零風險。見 §13 與 [`anila-core-boundary.md`](./anila-core-boundary.md) |

並加入：
- **§1.4 現況真實 footprint**（grep 結果，非敘述）
- **§3.4 現有 retrieval path deprecation 計畫**（7 條 path → 1 條 single entry）
- **Sprint 1 必過 gate × 3**（agent 隔離測試 / RLS staging 驗證 / retrieval path 唯一性）

### v0.1 (2026-04-24) — 初版

設計目標決議：
- 規模假設：10 → 100 agent
- Collection per-agent 嚴格隔離（機敏資料）
- Chunking 走 6 預設 + plug-in interface（自訂 code 不允許上傳）
- 「自訂 chunking」改解為 **Chunking Evaluator**（上傳 sample + queries → 自動找最佳）
- LLM-as-judge 由 dev 自選 + 自帶 API key
- 不限 quota（組內使用）

---

## 1. 動機與定位

### 1.1 現況問題

平台目前已有 **~10 個 agent**（含 AgenticRAG 與各 fork 變體），規劃一年內擴張到 **~100 個全院 agent**。在現況架構下，每個 agent 開發者都要：

- 自己連 pgvector、自己寫 schema migration
- 自己跑 `index_documents.py` 或等效 CLI 處理文件
- 自己選 chunking strategy（通常是「預設就好」，沒有 benchmark）
- 自己管 embedding endpoint、重試、錯誤處理
- 自己處理檔案上傳 UI（如果有）

**這會撞到三道牆**：
1. **不可擴展**：100 個 agent × 每人 2 週摸 pgvector = 4 人年的重複工
2. **不可控**：平台無從 audit「誰上傳了什麼到哪」；機敏資料洩漏時無 trace
3. **不可優化**：每個 dev 靠直覺挑 chunking，沒有 data 證據；檢索品質天差地遠

### 1.2 本 Design 要解的事

提供 **Ingestion-as-a-Service**：

- Dev 在 CSP UI 的「Knowledge Collections」分頁上傳檔案、選 chunking、看 embed 進度
- Chunking 有 6 種 built-in preset + plug-in interface 讓未來擴充
- **Chunking Evaluator**：上傳 sample docs + eval queries → 跑所有 strategies → data 證據挑最佳
- 資料 per-agent 嚴格隔離（機敏 guarantee）
- LLM-as-judge 的 LLM 由 dev 自選 + 自帶 API key（平台不替 dev 背付費）

### 1.3 明確排除

- ❌ Dev 上傳任意 Python code 跑在平台 infra（gVisor sandboxing 成本 > 收益；改走 chunking evaluator 這條路）
- ❌ 跨 agent collection 共享（機敏隔離要求）
- ❌ Token / chunk / storage quota（組內使用不限制，per 你的決策）
- ❌ 多租戶 pgvector cluster（單一 pg cluster + `agent_id` filter 夠用至 100 agent × 10M chunks）

### 1.4 現況真實 footprint（grep 出來的事實，非 README 敘述）

設計這個平台前必須先承認當前 ingestion 程式碼的真實樣貌，否則「整合」不知從何下手。

**實際存在的 ingestion 模組**：

| 位置 | 檔案 | 真實 caller |
|---|---|---|
| `anila-core/src/anila_core/ingestion/` | parsers / chunker / service / __init__ (4 檔) | **只有 tests** — production 無 caller |
| `anila-core/src/anila_core/api/{documents,search}.py` | `/upload` `/ingest` `/search` HTTP endpoints | tests only |
| `anila-core/src/anila_core/tools/__init__.py` | `vector_search` / `keyword_search` / `read_document` 工具 | 透過 `app_factory` 接到 `/agentic-chat` |
| `AgenticRAG/src/agentic_rag/ingestion/` | parsers / chunker / service / normalize / tokenize_zh / docling / ocr (8 檔) | **template 自用 — 真正在跑** |
| `AgenticRAG/src/agentic_rag/api/{documents,search}.py` | duplicate of anila-core | template 內 dev 上傳用 |
| `AgenticRAG/api.py` (port 24786) | inline SQL `_vector_search` / `_keyword_search` | OpenWebUI proxy chat 路徑 |
| `AgenticRAG/index_documents.py` | CLI（透過 HTTP API 餵資料） | dev 批次上傳 |

**真實 caller graph**：

```
┌────────────────────────────────────────────────────────────┐
│ Path 1: Dev 用 CLI 餵文件（最常見）                         │
│   index_documents.py → HTTP /documents/upload+/ingest      │
│   → uvicorn agentic_rag.app_factory:app  (port 8000)       │
│   → AgenticRAG/ingestion/{parsers,chunker,service,...}     │
│   → pgvector document_chunks                               │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ Path 2: Chat 對話的 retrieval（不是 ingestion）             │
│   OpenWebUI → AgenticRAG/api.py (port 24786)               │
│   → 只用 normalize + tokenize_zh 處理 query                 │
│   → inline SQL hybrid search                               │
│   → pgvector document_chunks                               │
└────────────────────────────────────────────────────────────┘

❌ anila-core/ingestion/ 沒有 production caller — 純死 code
```

**三個關鍵事實**：

1. **ingestion 是 「template 自用」、不是「平台共享」** — 每個 fork AgenticRAG 的 agent 都帶一份完整 ingestion，dev 自己跑 `index_documents.py` 餵資料。**目前完全沒有跨 agent 的共用機制。**
2. **anila-core 的 ingestion 是死 code** — README 寫「Task 3 pending」要搬走，事實上 AgenticRAG 已有完整且更豐富版本，差「正式刪除 anila-core 那份」。詳見 [`anila-core-boundary.md`](./anila-core-boundary.md)
3. **chat 路徑跟 ingestion 解耦** — `api.py` 只用 normalize + tokenize_zh（query 處理），不碰 IngestionService / parsers / chunker / ocr。設計新平台時可以暫時不動 chat 路徑（除了 retrieval 補上 `agent_id` scope）。

**對本 design 的影響**：
- §13 收斂方案從 v0.1 的「方案 B：anila-core 改 SDK」**改為「方案 A++：直接刪除 anila-core ingestion」**（沒 caller、刪了無人受傷）
- §3.4 retrieval path deprecation 範圍**只需清 AgenticRAG 內** 7 條 path
- Sprint 1 不需做雙軌相容，只要做「刪除舊 + 新建中央化」

#### Memory ≠ Ingestion（v0.2 補充澄清）

設計討論中常出現的混淆：「ANILA 平台的記憶（memory）功能跟新做的 Ingestion Platform 有衝突嗎？」

**不衝突，兩者解的問題完全不同**：

| 面向 | Ingestion（本 doc）| Platform Memory |
|---|---|---|
| 解的問題 | 文件 → vector chunks | 跨 session 對話 facts / preferences |
| 寫入時機 | Dev 主動上傳（一次性 batch）| 每 chat turn 自動 background 萃取 |
| 資料量 | 大（PDF 切數百 chunks）| 小（per user 數百個 markdown）|
| 程式入口 | `agentic_rag.ingestion.*`（搬走後）| `anila_core.memory.*`（留在 anila-core）|
| Schema | `document_chunks` table | `MEMORY.md` 索引 + 個別 `.md` files |

詳細討論見 [`multi-service-integration-plan.md`](../planning/multi-service-integration-plan.md) §1.4。**本 doc 設計範圍不包含 platform memory**；memory module 的演進（補 `PostgresMemoryStore`、未來可能的 central memory service）走獨立 design doc。

---

## 2. 架構總覽

```mermaid
flowchart TB
    dev["🧑‍💻 Agent Developer"]

    subgraph csp["myCSPPlatform :8000 (既有)"]
        api_core["既有 /api/* Control<br/>/v1/* Data"]
        api_ing["/api/ingestion/* ← 新<br/>collections · documents · jobs<br/>evaluator · apply"]
    end

    subgraph ingest["ingestion-worker ← 新服務"]
        queue["Arq + Redis<br/>(async job queue)"]
        worker["Worker pool<br/>parse → chunk → embed → index"]
        eval["Evaluator runner<br/>6 strategies in parallel"]
    end

    db[("Postgres + pgvector")]
    emb["Embedding endpoint<br/>(LOCAL_EMBEDDING_BASE_URL)"]
    judge_llm["LLM-as-judge<br/>(dev-provided<br/>endpoint + key)"]

    dev -->|瀏覽器 / JWT| api_core
    api_ing <--> dev
    api_ing -->|enqueue| queue
    queue --> worker
    queue --> eval
    worker -->|write chunks| db
    worker -->|embed| emb
    eval -->|retrieval test| db
    eval -->|score relevance| judge_llm
    api_core --> db

    classDef new fill:#dbeafe,stroke:#3b82f6
    class api_ing,queue,worker,eval new
```

### 2.1 為什麼拆 `ingestion-worker` 服務？

把 ingestion 放 FastAPI request handler 裡會要命：

- 100 頁繁中 PDF 的完整處理 ≈ 1-3 分鐘（parse + chunk + embed 800 chunks × NV-Embed 延遲）
- Chunking Evaluator 跑 6 strategies ≈ 5-10 分鐘
- 10 個 dev 同時送 = CSP FastAPI worker 全部卡住、整個 control plane 掛掉

獨立 worker 給我們：
- 背景 async（dev UI 看進度條而非阻塞）
- Horizontal scale（Redis queue 天然支援多 worker）
- 失敗重試 / dead letter 機制
- Ingestion 慢不會影響 CSP control plane

### 2.2 技術選型

| 層面 | 選 | 理由 |
|---|---|---|
| Job queue | **Arq** (https://github.com/samuelcolvin/arq) | async-native、FastAPI 作者寫的、依賴只有 Redis、無 Celery 的 broker hell |
| Queue broker | **Redis 7** | CSP 部署已有 PostgreSQL，加 Redis 成本低；advisory lock 做 idempotency |
| Worker runtime | Python 3.11 + asyncio | 跟 anila-core 一致；用 `anila-core.ingestion` primitives |
| Deployment | Docker container + compose service `ingestion-worker` | 跟 CSP 一起跑，共享 Postgres 網路 |

---

## 3. 資料 Schema

### 3.1 CSP 新增 tables（Alembic migration `0014` + `0015`）

> v0.3 修訂：design doc 原寫 `0012`，但 0012 編號已被 ServiceAccessGrant
> 用走（`0012_add_service_access_control.py`）。ingestion 平台實際拿到的是
> `0014_add_ingestion_platform.py`（4 張表 + RLS + csp_app role + ownership
> transfer）+ `0015_chunks_halfvec_4000.py`（vector(1536) → halfvec(4000)
> 升級）。下方 SQL 是合併後的最終 schema。

```sql
-- 每個 agent 的 ingestion collection（一個 agent 可以多個 collection，例如「法規」「SOP」分開）
CREATE TABLE ingestion_collections (
    id                    BIGSERIAL PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    description           TEXT,
    chunking_config       JSONB NOT NULL,
        -- { "strategy": "hierarchical", "max_leaf_tokens": 1024, "overlap_tokens": 64, ... }
    embedding_model       TEXT NOT NULL,
        -- 通常 = platform default (nvidia/NV-embed-V2)，可 override
    embedding_dim         INT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'active',
        -- active / archived / indexing / failed
    document_count        INT NOT NULL DEFAULT 0,
    chunk_count           INT NOT NULL DEFAULT 0,
    bytes_stored          BIGINT NOT NULL DEFAULT 0,
    created_by            BIGINT NOT NULL REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, name)
);

CREATE INDEX idx_collections_agent ON ingestion_collections(agent_id);

-- 每一個上傳的文件
CREATE TABLE ingestion_documents (
    id                    BIGSERIAL PRIMARY KEY,
    collection_id         BIGINT NOT NULL REFERENCES ingestion_collections(id) ON DELETE CASCADE,
    filename              TEXT NOT NULL,
    sha256                CHAR(64) NOT NULL,   -- dedup within collection
    mime_type             TEXT,
    bytes                 BIGINT,
    status                TEXT NOT NULL DEFAULT 'pending',
        -- pending / parsing / chunking / embedding / indexed / failed
    chunk_count           INT DEFAULT 0,
    error_message         TEXT,
    uploaded_by           BIGINT REFERENCES users(id),
    uploaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at            TIMESTAMPTZ,
    UNIQUE (collection_id, sha256)
);

CREATE INDEX idx_documents_collection_status ON ingestion_documents(collection_id, status);

-- Async ingestion jobs (Arq 的 job 也記錄到這，方便 UI 看進度)
CREATE TABLE ingestion_jobs (
    id                    BIGSERIAL PRIMARY KEY,
    arq_job_id            TEXT UNIQUE,   -- Arq 內部 job id
    collection_id         BIGINT NOT NULL REFERENCES ingestion_collections(id) ON DELETE CASCADE,
    document_id           BIGINT REFERENCES ingestion_documents(id),   -- NULL for batch
    job_type              TEXT NOT NULL,   -- ingest / reindex / evaluate / apply_strategy
    status                TEXT NOT NULL DEFAULT 'queued',
        -- queued / running / succeeded / failed / cancelled
    progress_pct          SMALLINT DEFAULT 0,
    progress_message      TEXT,
    error_message         TEXT,
    enqueued_by           BIGINT REFERENCES users(id),
    enqueued_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ
);

CREATE INDEX idx_jobs_collection_status ON ingestion_jobs(collection_id, status);

-- Chunking Evaluator runs
CREATE TABLE ingestion_eval_runs (
    id                    BIGSERIAL PRIMARY KEY,
    collection_id         BIGINT NOT NULL REFERENCES ingestion_collections(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    sample_document_ids   BIGINT[] NOT NULL,
    strategies_tried      JSONB NOT NULL,
        -- [{ "name": "hierarchical", "params": {...} }, ...]
    queries               JSONB NOT NULL,
        -- [{ "query": "...", "expected_doc_id": N, "expected_chunk_id": "...", "source": "manual|llm_synth|judge_only" }, ...]
    judge_llm_config      JSONB NOT NULL,
        -- { "endpoint": "...", "model": "...", "api_key_ref": "csp_user_key:123" }
    status                TEXT NOT NULL DEFAULT 'queued',
    results               JSONB,
        -- { "strategy": { "hit_at_1": 0.72, "hit_at_5": 0.91, "mrr": 0.81, "ndcg_10": 0.85, "avg_chunk_tokens": 512, "ingest_seconds": 45 }, ... }
    recommended_strategy  TEXT,
    created_by            BIGINT REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ
);

-- Dev 提供的 LLM-as-judge credentials（加密儲存）
CREATE TABLE agent_llm_credentials (
    id                    BIGSERIAL PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    endpoint_url          TEXT NOT NULL,
    model_name            TEXT NOT NULL,
    api_key_encrypted     BYTEA NOT NULL,   -- AES-256-GCM，key = CSP_SECRET_KEY derived
    api_key_nonce         BYTEA NOT NULL,
    last_used_at          TIMESTAMPTZ,
    created_by            BIGINT REFERENCES users(id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, name)
);
```

### 3.2 pgvector 資料表（per-collection physical isolation optional）

> v0.3 修訂：原本 `vector(4096)` + IVFFlat 在 pgvector 0.8.2 撐不住 — `vector`
> 跟 `halfvec` 兩種 type 的 HNSW/IVFFlat 上限分別是 **2000-d** 跟 **4000-d**。
> NV-embed-V2 native 4096-d 沒辦法直接索引。實際採用 **halfvec(4000)** + HNSW：
> worker 在 client 端 truncate 4096 → 4000（drop 96 dim 在 Matryoshka 噪訊
> 之下，相當於 free upgrade），halfvec 又把 storage 砍半。

```sql
-- 所有 agent 共享同一張 document_chunks，靠 (agent_id, collection_id) 做 logical filter
CREATE TABLE document_chunks (
    id                    BIGSERIAL PRIMARY KEY,
    collection_id         INTEGER NOT NULL REFERENCES ingestion_collections(id) ON DELETE CASCADE,
    agent_id              INTEGER NOT NULL,   -- denormalized from collections, for fast filter + RLS
    document_id           INTEGER NOT NULL REFERENCES ingestion_documents(id) ON DELETE CASCADE,
    chunk_key             TEXT NOT NULL,      -- hierarchical id, e.g. "leaf-0007-Section-Title"
    content               TEXT NOT NULL,
    content_tsv           tsvector,           -- FTS for keyword_search; built via to_tsvector at INSERT
    embedding             halfvec(4000),      -- NV-embed-V2 truncated client-side
    metadata              JSONB NOT NULL DEFAULT '{}',
    token_count           INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chunks_agent_required CHECK (agent_id IS NOT NULL),
    CONSTRAINT uq_chunks_collection_chunk_key UNIQUE (collection_id, chunk_key)
);

-- 過濾索引：retrieval 走完 agent_id+collection_id 才到 vector 距離計算
CREATE INDEX ix_chunks_agent_collection ON document_chunks(agent_id, collection_id);
CREATE INDEX ix_chunks_document_id      ON document_chunks(document_id);

-- HNSW (pgvector 0.5+, halfvec_cosine_ops 對 halfvec)
CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
    USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Keyword search GIN index (hybrid)
CREATE INDEX ix_chunks_content_tsv ON document_chunks
    USING gin (content_tsv);
```

### 3.3 機敏隔離強制：4 層 defense-in-depth

> v0.1 只列了一個 `AgentScopedPgVectorStore` enforcement 點。Review 後發現現有 7 條 retrieval path 各自打 SQL，光改新 schema 不會自動補強舊 path。**本節擴展為 4 層強制**，4 層獨立有效，任何一層失守還有後續層擋。

#### Layer 1 — Schema：強制 NOT NULL

```sql
ALTER TABLE document_chunks ADD COLUMN agent_id BIGINT NOT NULL;
ALTER TABLE document_chunks ADD CONSTRAINT chunks_agent_required
    CHECK (agent_id IS NOT NULL);
CREATE INDEX idx_chunks_agent_required
    ON document_chunks(agent_id, collection_id);

-- 寫入時若遺漏 agent_id，constraint 直接拒絕。
```

#### Layer 2 — Postgres Row Level Security（資料庫引擎層擋）

```sql
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY chunks_agent_isolation ON document_chunks
    FOR ALL
    USING (agent_id = current_setting('anila.agent_id', true)::bigint);
```

連線初始化時：
```python
async with pool.acquire() as conn:
    await conn.execute(f"SET LOCAL anila.agent_id = {agent_id}")
    # ... 之後所有 query 自動受 RLS 限制
```

**這一層是 v0.1 漏掉的關鍵**：即使 application code 寫錯（忘了加 `WHERE agent_id =`），PG 引擎也會擋。SQL injection 也擋得住，因為 RLS 是引擎強制。

#### Layer 2-prime — RLS 真正啟用的三個必要條件（v0.3 補充）

**v0.2 漏寫的 trap：PG 預設條件下 RLS policy 會被一聲不響地 bypass**。Sprint 1
實作時 smoke test 才發現「policy 設了但 query 還是看得到所有 row」。原因是
`policy.USING` 對下列 caller 自動短路：

| Bypass 條件 | 預設 postgres image 是否中槍 |
|---|---|
| `rolsuper = true` (superuser) | ✅ 中：`POSTGRES_USER=csp` 自動是 superuser |
| `rolbypassrls = true` | ✅ 中：superuser 自動帶 BYPASSRLS |
| Table owner（沒 `FORCE ROW LEVEL SECURITY` 時） | ✅ 中：migration 跑完 csp owns 表 |

任一條件成立 RLS 都當沒事。所以「ENABLE + Policy」是必要不充分。Sprint 1 落地的
**三個必要條件**：

1. **`ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY`** — 把 owner 也納入 policy 範圍。
2. **拆 runtime role**：建 `csp_app` 帶 `NOSUPERUSER NOBYPASSRLS NOCREATEDB
   NOCREATEROLE`。應用程式（CSP backend、ingestion-worker、AgenticRAG）的
   DATABASE_URL 切到 `csp_app`；migration 留在 `csp` superuser（CREATE EXTENSION
   vector / CREATE ROLE 需要 superuser）。
3. **Ownership transfer**：`csp_app` 不只 GRANT 還要 OWN 所有 public 表，否則
   ALTER TABLE（startup migrations）會失敗。migration 0014 結尾跑：

   ```sql
   FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
       EXECUTE format('ALTER TABLE %I OWNER TO csp_app', r.tablename);
   END LOOP;
   ```

migration 0014 把以上三條同包進去。docker-compose 拆兩條 env：
`DATABASE_URL=postgresql://csp_app:csp@...` 給 runtime；`MIGRATION_DATABASE_URL=postgresql://csp:csp@...`
給 alembic upgrade。alembic env.py 優先讀 MIGRATION_DATABASE_URL。

驗證走的是 G2 pytest（`test_g2_csp_app_role_lacks_bypass_attributes` +
`test_g2_force_rls_enabled_on_document_chunks`），catalog 直接 query
`pg_roles.rolsuper / rolbypassrls` 與 `pg_class.relforcerowsecurity`。

#### Layer 3 — Code：刪除舊路徑、單一入口

- 把 `anila_core.api.{documents,search}.py` 與 `anila_core.tools.create_*_tool` 的 RAG-specific 部分**直接刪除**（見 §13、§3.4 與 [`anila-core-boundary.md`](./anila-core-boundary.md)）
- AgenticRAG 內 `api.py` 的 inline `_vector_search` / `_keyword_search` SQL **改走新的 SDK**
- pgvector_store constructor 強制 `agent_id: int`（拒絕 None / Optional）：
  ```python
  class AgentScopedPgVectorStore:
      def __init__(self, pool, agent_id: int):
          if not isinstance(agent_id, int) or agent_id <= 0:
              raise ValueError(f"agent_id must be positive int, got {agent_id!r}")
          self._pool = pool
          self._agent_id = agent_id

      async def _acquire(self):
          conn = await self._pool.acquire()
          await conn.execute(f"SET LOCAL anila.agent_id = {self._agent_id}")
          return conn
  ```
- `RetrievalProvider` Protocol 移除「無 scope」的 method signature（不允許出現「不帶 agent_id 的 search」）

#### Layer 4 — 強制驗證測試（pytest 必過 gate）

Sprint 1 必須交付以下測試，沒過不能 merge：

```python
# tests/test_agent_isolation.py
async def test_agent_isolation_random_workload():
    """Random workload across agents — no agent ever sees another's chunk."""
    async with sandbox_pgvector() as pool:
        # Create 5 agents, each ingests 200 chunks
        agents = [await create_agent(pool, name=f"agent-{i}") for i in range(5)]
        for a in agents:
            await ingest_random_chunks(pool, a, count=200)

        # Run 1000 random queries from each agent's perspective
        for _ in range(1000):
            agent = random.choice(agents)
            store = AgentScopedPgVectorStore(pool, agent_id=agent.id)
            results = await store.similarity_search(random_vec(), k=20)
            # 強斷言：所有結果必須屬於該 agent
            for r in results:
                assert r.agent_id == agent.id, \
                    f"LEAK: agent {agent.id} saw chunk from agent {r.agent_id}"

async def test_rls_blocks_raw_sql_bypass():
    """Even with raw SQL bypass attempt, RLS holds."""
    async with sandbox_pgvector() as pool:
        agent_a, agent_b = await setup_two_agents_with_chunks(pool)
        async with pool.acquire() as conn:
            await conn.execute(f"SET LOCAL anila.agent_id = {agent_a.id}")
            # Try to bypass via raw SQL — RLS should still filter
            rows = await conn.fetch("SELECT * FROM document_chunks")
            assert all(r['agent_id'] == agent_a.id for r in rows)

async def test_constructor_rejects_invalid_agent_id():
    with pytest.raises(ValueError):
        AgentScopedPgVectorStore(pool, agent_id=None)
    with pytest.raises(ValueError):
        AgentScopedPgVectorStore(pool, agent_id=0)
    with pytest.raises(ValueError):
        AgentScopedPgVectorStore(pool, agent_id=-1)
```

#### 違反告警

`E_PG_RLS_VIOLATION` 是 §8.1 Error Taxonomy 內**最高優先級告警**：代表 layer 1+2 失守，立即送 alerts 中心並 page on-call。

---

### 3.4 現有 retrieval path deprecation 計畫

§1.4 列出 7 條既有 path 直接打 `document_chunks` 表。Sprint 1 結束時必須收斂為**單一 entry point**：

| 編號 | 既有 path | Sprint 1 處置 |
|---|---|---|
| 1 | `anila_core.api.documents` `/upload` `/ingest` | **刪除**（無 production caller，見 §13） |
| 2 | `anila_core.api.search` `POST /search` | **刪除** |
| 3 | `anila_core.tools.create_vector_search_tool` | **刪除** |
| 4 | `anila_core.tools.create_keyword_search_tool` | **刪除** |
| 5 | `anila_core.tools.create_read_document_tool` | **刪除** |
| 6 | `AgenticRAG/api.py` 的 inline `_vector_search` / `_keyword_search` SQL | **改寫**為呼叫新 ingestion SDK |
| 7 | `AgenticRAG/index_documents.py` CLI | **改寫**為呼叫 CSP `/api/ingestion/*` |
| 8 | `AgenticRAG/src/agentic_rag/api/{documents,search}.py` | **刪除**（dev mode 改走 CSP API） |
| 9 | `AgenticRAG/src/agentic_rag/tools/__init__.py` 的 RAG tools | **改寫**為呼叫新 SDK |

**Gate 驗證**：
```bash
# Sprint 1 結束時必須通過：
$ grep -rn "document_chunks" --include="*.py" \
    /home/aia/c1147259/ANILA/anila-core /home/aia/c1147259/ANILA/AgenticRAG \
    | grep -v "_archive\|tests\|__pycache__"
# 期望輸出：只剩一個檔案 — 新的 anila_core.storage.adapters.pgvector_store_v2
```

只允許**一個檔案**直接 query `document_chunks` 表，其他全部走 SDK / API。

---

## 4. API Contract

### 4.1 Collection 管理

```http
POST /api/ingestion/collections
Content-Type: application/json
Authorization: Bearer <jwt>

{
  "agent_id": 42,
  "name": "legal-regulations",
  "description": "陸海空軍懲罰法、國軍要則",
  "chunking_config": {
    "strategy": "hierarchical",
    "params": { "max_leaf_tokens": 1024, "overlap_tokens": 64 }
  },
  "embedding_model": "nvidia/NV-embed-V2"
}

→ 201 Created
{ "id": 17, "agent_id": 42, "name": "legal-regulations", ... }
```

```http
GET /api/ingestion/collections?agent_id=42
→ 200 [ { "id": 17, ..., "document_count": 34, "chunk_count": 2981 } ]
```

```http
DELETE /api/ingestion/collections/{id}
→ 202 Accepted  (enqueue cascade delete job)
```

### 4.2 文件上傳（單檔 / 資料夾 zip）

```http
POST /api/ingestion/collections/{id}/documents
Content-Type: multipart/form-data

files=@doc1.pdf
files=@doc2.docx
files=@folder.zip
preserve_folder_structure=true

→ 202 Accepted
{
  "accepted": 3,
  "rejected": 0,
  "jobs": [
    { "job_id": 1001, "document_id": 501, "filename": "doc1.pdf", "status": "queued" },
    { "job_id": 1002, "document_id": 502, "filename": "doc2.docx", "status": "queued" },
    { "job_id": 1003, "document_id": 503, "filename": "folder.zip", "status": "queued", "note": "zip will be expanded" }
  ]
}
```

### 4.3 Job 進度查詢（UI polling 用）

```http
GET /api/ingestion/jobs/{job_id}
→ 200
{
  "id": 1001,
  "status": "running",
  "progress_pct": 47,
  "progress_message": "embedding chunks 235/500",
  "started_at": "2026-04-24T10:22:15Z"
}
```

或 SSE 串流（避免 polling）：

```http
GET /api/ingestion/jobs/{job_id}/stream
→ 200 Content-Type: text/event-stream

data: {"status": "parsing", "progress_pct": 10}
data: {"status": "chunking", "progress_pct": 25, "chunks_so_far": 156}
data: {"status": "embedding", "progress_pct": 47, "chunks_embedded": 235}
data: {"status": "succeeded", "progress_pct": 100, "total_chunks": 500}
```

### 4.4 Chunking Evaluator

**Step 1 — 啟動 eval**：
```http
POST /api/ingestion/collections/{id}/evaluator
{
  "name": "baseline vs semantic chunker",
  "sample_document_ids": [501, 502, 503],
  "strategies": [
    { "name": "hierarchical", "params": {} },
    { "name": "fixed", "params": { "size": 512, "overlap": 64 } },
    { "name": "fixed", "params": { "size": 1024, "overlap": 128 } },
    { "name": "markdown-aware", "params": {} },
    { "name": "pdf-page", "params": {} },
    { "name": "cjk-sentence", "params": {} }
  ],
  "queries": [
    { "query": "記過的條件", "expected_doc_id": 501, "source": "manual" },
    { "query": "第八條是什麼", "expected_doc_id": 501, "expected_chunk_key": "doc501/art8", "source": "manual" }
  ],
  "synth_queries_count": 50,
  "judge_llm_credential_id": 7
}

→ 202 Accepted
{ "eval_run_id": 33, "status": "queued", "estimated_seconds": 480 }
```

**Step 2 — 看結果**：
```http
GET /api/ingestion/evaluator/{run_id}/results
→ 200
{
  "status": "completed",
  "results": [
    {
      "strategy": "hierarchical",
      "metrics": {
        "hit_at_1": 0.72,
        "hit_at_5": 0.91,
        "mrr": 0.81,
        "ndcg_10": 0.85,
        "llm_judge_relevance_avg": 2.4,
        "avg_chunk_tokens": 487,
        "chunks_per_doc": 31,
        "ingest_seconds_per_doc": 1.2
      },
      "rank": 1
    },
    { "strategy": "fixed-1024", "metrics": {...}, "rank": 2 },
    ...
  ],
  "recommended_strategy": "hierarchical",
  "per_query_breakdown_url": "/api/ingestion/evaluator/33/queries"
}
```

**Step 3 — 套用**：
```http
POST /api/ingestion/evaluator/{run_id}/apply
{ "reindex_existing_documents": true }
→ 202 Accepted
{ "job_id": 1099, "message": "Reindexing 34 documents with strategy=hierarchical" }
```

### 4.5 LLM Credentials（dev 自帶的 judge LLM）

```http
POST /api/ingestion/agents/{agent_id}/llm-credentials
{
  "name": "openai-gpt4-judge",
  "endpoint_url": "https://api.openai.com/v1",
  "model_name": "gpt-4-turbo",
  "api_key": "sk-..."
}
→ 201 { "id": 7, "api_key": null }   # 只回 id，key 不 echo 回來
```

---

## 5. Chunking Plug-in Interface

### 5.1 核心抽象

```python
# anila_core.ingestion.chunking_plugins.base
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

@dataclass
class ChunkResult:
    content: str
    metadata: dict[str, Any]
    token_count: int
    chunk_key: str   # hierarchical id

class ChunkerStrategy(ABC):
    """Base class for all chunking strategies.

    Subclasses register themselves via @register_chunker decorator.
    """

    name: str                          # e.g. "hierarchical", "fixed"
    display_name: str                  # e.g. "Hierarchical (heading tree)"
    default_params: dict[str, Any]
    param_schema: dict[str, Any]       # JSON schema for UI form generation

    @abstractmethod
    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        """Chunk one document. Must be deterministic given same input."""
        ...

    def estimate_chunks(self, document_text: str, params: dict[str, Any]) -> int:
        """Fast estimate for UI preview (default: rough token count / avg_size)."""
        return len(document_text) // (params.get("size", 1024) * 4)
```

### 5.2 Registration

```python
# anila_core.ingestion.chunking_plugins
from functools import wraps

_REGISTRY: dict[str, type[ChunkerStrategy]] = {}

def register_chunker(cls: type[ChunkerStrategy]) -> type[ChunkerStrategy]:
    if cls.name in _REGISTRY:
        raise ValueError(f"Chunker '{cls.name}' already registered")
    _REGISTRY[cls.name] = cls
    return cls

def get_chunker(name: str, params: dict) -> ChunkerStrategy:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown chunker: {name}. Available: {list(_REGISTRY)}")
    return _REGISTRY[name](**params)

def list_chunkers() -> list[dict]:
    return [
        {"name": cls.name, "display_name": cls.display_name, "param_schema": cls.param_schema}
        for cls in _REGISTRY.values()
    ]
```

### 5.3 6 個 built-in strategies

```python
# anila_core.ingestion.chunking_plugins.builtins

@register_chunker
class HierarchicalChunker(ChunkerStrategy):
    name = "hierarchical"
    display_name = "Hierarchical (heading tree)"
    default_params = {"max_leaf_tokens": 1024, "overlap_tokens": 64}
    param_schema = {
        "type": "object",
        "properties": {
            "max_leaf_tokens": {"type": "integer", "minimum": 128, "maximum": 8192},
            "overlap_tokens": {"type": "integer", "minimum": 0, "maximum": 512},
        },
    }
    def chunk(self, text, metadata, params):
        # 既有實作 — 搬自 anila_core.ingestion.chunker.HierarchicalChunker
        ...

@register_chunker
class FixedChunker(ChunkerStrategy):
    name = "fixed"
    display_name = "Fixed-size (token-based)"
    default_params = {"size": 1024, "overlap": 128}
    ...

@register_chunker
class MarkdownAwareChunker(ChunkerStrategy):
    name = "markdown-aware"
    display_name = "Markdown-aware (headings + code blocks)"
    ...

@register_chunker
class PdfPageChunker(ChunkerStrategy):
    name = "pdf-page"
    display_name = "PDF page boundaries"
    ...

@register_chunker
class CjkSentenceChunker(ChunkerStrategy):
    name = "cjk-sentence"
    display_name = "CJK sentence-aware"
    default_params = {"target_tokens": 512, "sentence_merge": True}
    ...

@register_chunker
class SemanticChunker(ChunkerStrategy):
    name = "semantic"
    display_name = "Semantic boundaries (embedding distance)"
    default_params = {"breakpoint_percentile": 85, "min_chunk_tokens": 256}
    # 需要 embedding callback，見實作
    ...
```

### 5.4 Third-party plug-in（future）

```python
# 自己的 package agentic_rag_custom/chunkers.py
from anila_core.ingestion.chunking_plugins import register_chunker, ChunkerStrategy

@register_chunker
class LegalClauseChunker(ChunkerStrategy):
    """切法律條文，保留「第 X 條 X 項」結構。"""
    name = "legal-clause"
    display_name = "Legal clause boundaries"
    ...
```

`pip install agentic-rag-custom` 後自動 register。Dev UI 看到這個 strategy 就多一個選項。

---

## 6. Chunking Evaluator 工作流詳解

### 6.1 Pipeline

```mermaid
flowchart LR
    start["Evaluator Start"]
    load["載入 sample docs"]
    parallel["Parallel runs<br/>(n strategies)"]
    chunk["Chunk sample docs"]
    embed["Embed chunks<br/>(小 pool,臨時 index)"]
    ret["對每個 query<br/>跑 retrieval"]
    score["計算 metrics<br/>+ LLM judge"]
    cleanup["清掉臨時 embeddings"]
    rank["排名 + recommend"]

    start --> load --> parallel
    parallel --> chunk --> embed --> ret --> score --> cleanup
    score --> rank
```

### 6.2 Metrics 計算

| Metric | 定義 | 需要什麼 |
|---|---|---|
| **Hit@k** | Top-k 結果中有包含 expected 的比例 | manual 或 synth 的 golden set |
| **MRR** | Mean Reciprocal Rank | 同上 |
| **NDCG@10** | Normalized Discounted Cumulative Gain | 同上 + graded relevance（0-3 分） |
| **LLM-judge relevance** | LLM 對每個 retrieved chunk 給 0-3 分，取平均 | dev-provided LLM endpoint + key |
| **Avg chunk tokens** | 每個 chunk 的 token 數中位數 | - |
| **Chunks per doc** | 每份文件平均切幾個 chunk | - |
| **Ingest seconds per doc** | Ingestion 時間（含 chunk + embed） | - |

最終分數：`0.4 * hit_at_5 + 0.3 * mrr + 0.3 * llm_judge_avg_normalized`，權重可調。

### 6.3 Golden set 的三種來源（dev 可混用）

**A. Manual queries（最準，dev 輸入）**
```json
{ "query": "記過的條件", "expected_doc_id": 501, "expected_chunk_key": "doc501/art23/sec2", "source": "manual" }
```

**B. Synthetic queries（零成本，LLM 反生）**
對每份 sample doc，用 dev-provided LLM 生 3-5 題：
```
System: 根據以下文件片段，生成 3 題可以從此片段回答的繁中問題。
Document excerpt: ...
Output JSON: [{"query": "...", "answer_hint": "..."}]
```
→ 每題自動關聯其來源 doc_id + chunk_key

**C. LLM-as-judge only（無 golden，只靠 judge）**
Dev 只提供 query 不提供 expected：
```
System: 評估以下 chunk 對 query 的 relevance（0=無關, 3=完全回答）
Query: "記過的條件"
Chunk: "第二十三條 記過 3 次視同記大過..."
Output: {"score": 3, "reason": "..."}
```
只能算 LLM judge metric，無法算 Hit/MRR。

### 6.4 LLM-as-judge 的成本與穩健性

- 單次 eval run：~50 queries × 6 strategies × top-5 = 1500 judge calls
- 若用 GPT-4 級 LLM ~$0.02/call → ~$30/run（dev 付）
- 若用地端 llama 70b → ~免費但慢（~20 分鐘）
- 建議預設 temperature=0 + few-shot 固定評分量表避免 judge drift

### 6.5 Cost & Throttle 治理（v0.2 新增）

> v0.1 估了「~$30/run」但沒治理機制。Reviewer 指出組內若常跑 evaluator，成本與 queue 爆量會失控。本節補三層治理。

#### 6.5.1 三段式預設（Dev UI 第一層選擇）

| Mode | Queries × Strategies × top-k | Judge calls | 預估成本（GPT-4）| 預估時間 |
|---|---|---|---|---|
| **Fast** | 10 × 3 × 3 | 90 | ~$2 | 1-2 min |
| **Standard** | 30 × 6 × 5 | 900 | ~$18 | 5-10 min |
| **Deep** | 80 × 6 × 10 | 4,800 | ~$96 | 25-40 min |

UI 預設為 Fast，dev 主動升級才走 Standard/Deep。

#### 6.5.2 三道 Throttle

**a. Per-agent 互斥**：同一個 agent 同時最多 1 個 eval run，後續進 queue。避免單一 dev 一晚跑 50 次浪費資源。

**b. Per-judge-LLM rate limit**：依 dev 自己提供的 endpoint 限速設定推算（從 `agent_llm_credentials` 表的 metadata 讀），不打爆對方 API。

**c. Platform-wide budget gate**（circuit breaker，**不是 quota**）：
```python
# 每月 platform aggregate judge spend ceiling，admin 在 CSP 設
class JudgeBudgetGate:
    monthly_ceiling_usd: float = 500.0   # 預設值

    async def check_or_raise(self, estimated_cost: float):
        spent_this_month = await self._sum_actual_spend()
        if spent_this_month + estimated_cost > self.monthly_ceiling_usd:
            raise BudgetExceededError(
                f"Estimated ${estimated_cost} would exceed monthly "
                f"ceiling ${self.monthly_ceiling_usd} (already spent ${spent_this_month})"
            )
        # 接近 80% ceiling 時 UI 顯示 warning banner
```

#### 6.5.3 結果 Cache（最有 leverage 的優化）

```python
cache_key = sha256(
    sample_doc_ids_sorted ⊕
    query_set_normalized ⊕
    judge_model_id ⊕
    strategy_configs_canonical
)
```

相同輸入的 eval 結果 cache 30 天，dev 反覆 tune query set 時免費。實務上組內 dev **反覆跑 baseline 的次數**（換不同 strategy 比較）遠超你想像。

#### 6.5.4 強烈推薦 Local LLM Judge

若 dev 在 `agent_llm_credentials` 已註冊地端 LLM（llama 70b 等），UI **預設**選它而非 GPT-4：

- Cost = 0
- 速度慢 1.5-3x（組內可接受）
- 評分品質 vs GPT-4 相關性實測 ~0.85（足夠用於 strategy ranking，不需要絕對精準）

UI 行為：dev 沒有 local credential 時才預設 GPT-4，並顯示「💡 Tip: 註冊一個 local LLM 可以零成本跑 evaluator」。

---

## 7. Dev UI Wireframe

### 7.1 新分頁「Knowledge Collections」（在 Developer Console 內）

```
╔═══════════════════════════════════════════════════════════════════╗
║ [My Agents] [Knowledge Collections] [LLM Credentials] [Usage]    ║
╟───────────────────────────────────────────────────────────────────╢
║ Agent: [my-legal-rag ▾]                      [+ New Collection]  ║
╟───────────────────────────────────────────────────────────────────╢
║ ┌─────────────────────────────────────────────────────────────┐   ║
║ │ 📚 legal-regulations                                        │   ║
║ │    陸海空軍懲罰法、國軍要則                                  │   ║
║ │    34 docs · 2,981 chunks · 147 MB                          │   ║
║ │    Strategy: hierarchical (1024/64)                          │   ║
║ │    [Upload] [Evaluate] [Reindex] [Settings] [Delete]        │   ║
║ └─────────────────────────────────────────────────────────────┘   ║
║ ┌─────────────────────────────────────────────────────────────┐   ║
║ │ 📚 internal-sop        (indexing: 12/45 docs...)            │   ║
║ │    ...                                                      │   ║
║ └─────────────────────────────────────────────────────────────┘   ║
╚═══════════════════════════════════════════════════════════════════╝
```

### 7.2 Upload dialog

```
╔══════════════════════════════════════════════╗
║ Upload to: legal-regulations                 ║
╟──────────────────────────────────────────────╢
║  Drag files here, or [browse]                ║
║  [📄 contract-v3.pdf  2.4 MB    ✕]          ║
║  [📄 sop-manual.docx  540 KB    ✕]           ║
║  [📦 policy-folder.zip 18 MB    ✕]           ║
║                                              ║
║  ☐ Preserve folder structure (zip only)      ║
║  ☐ Deduplicate by SHA-256                    ║
║                                              ║
║                [Cancel]   [Upload 3 files]   ║
╚══════════════════════════════════════════════╝
```

### 7.3 Evaluator wizard

```
Step 1/4 — Sample documents
  ☑ contract-v3.pdf
  ☑ sop-manual.docx
  ☐ ... (23 more)         [Select all] [Select 10 random]

Step 2/4 — Eval queries
  ┌──────────────────────────────────────────────┐
  │ Query                    │ Expected source   │
  ├──────────────────────────────────────────────┤
  │ 記過的條件               │ contract-v3.pdf   │
  │ 第八條是什麼              │ [auto-detect ▾]   │
  │ [+ Add query]                                │
  └──────────────────────────────────────────────┘
  ☑ Also generate 50 synthetic queries (LLM reverse-gen)
  ☑ Use LLM-as-judge for graded relevance

Step 3/4 — Strategies to compare
  ☑ hierarchical (default params)
  ☑ fixed-size 512      [Edit params]
  ☑ fixed-size 1024     [Edit params]
  ☑ markdown-aware
  ☑ pdf-page
  ☑ cjk-sentence
  ☐ semantic            (requires embedding-based split)

Step 4/4 — Judge LLM
  ○ Use my openai-gpt4-judge         [$0.02/call × ~1500 = ~$30]
  ○ Use my local-llama-70b           [Free, ~20 min]
  ● [+ Add new LLM credential]

                              [Cancel]  [Start Eval (~8 min)]
```

### 7.4 Results page

```
Eval run #33 — "baseline vs semantic chunker"   Completed · 7m 42s
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommended: hierarchical  ✓ [Apply to collection]

┌──────────────────┬──────┬──────┬──────┬──────────┬──────────┬────────┐
│ Strategy         │Hit@1 │Hit@5 │ MRR  │Judge avg │Chunks/Doc│Ingest/s│
├──────────────────┼──────┼──────┼──────┼──────────┼──────────┼────────┤
│ hierarchical 🏆  │ 0.72 │ 0.91 │ 0.81 │   2.4    │    31    │  1.2   │
│ fixed-1024       │ 0.64 │ 0.88 │ 0.74 │   2.1    │    18    │  0.8   │
│ cjk-sentence     │ 0.61 │ 0.85 │ 0.71 │   2.3    │    52    │  1.5   │
│ markdown-aware   │ 0.58 │ 0.82 │ 0.68 │   2.0    │    24    │  1.0   │
│ pdf-page         │ 0.55 │ 0.79 │ 0.63 │   1.9    │    12    │  0.6   │
│ fixed-512        │ 0.53 │ 0.81 │ 0.65 │   1.8    │    38    │  1.1   │
└──────────────────┴──────┴──────┴──────┴──────────┴──────────┴────────┘

[View per-query breakdown]  [Export CSV]  [Re-run with new queries]
```

---

## 8. Job State Machine

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: worker picks up
    running --> parsing
    parsing --> chunking: text extracted
    chunking --> embedding: chunks produced
    embedding --> indexing: vectors ready
    indexing --> succeeded: written to pgvector
    succeeded --> [*]

    parsing --> failed: parse error
    chunking --> failed: invalid strategy
    embedding --> failed: embedding endpoint down
    indexing --> failed: pgvector error
    failed --> [*]

    queued --> cancelled: user cancel
    running --> cancelled: user cancel
    cancelled --> [*]

    failed --> queued: retry per error policy
```

### 8.1 Error Taxonomy（v0.2 新增）

> v0.1 寫「Unknown failure → 1 次 + dead letter」是模糊的。Reviewer 指出實作時若沒結構化 error code，root cause 會被吞掉。本節定義完整 taxonomy。

#### 8.1.1 Error 基底

```python
# anila_core.ingestion.errors
class IngestionError(Exception):
    """Structured error with retryability hint."""

    code: str             # 結構化 error code（穩定 API）
    retryable: bool       # 是否可自動重試
    user_message: str     # 給 dev UI 看的訊息（可 i18n、不含 stack trace）
    details: dict         # 給 audit log（exception type / context / 耗時）
    severity: str         # info / warning / error / critical
```

#### 8.1.2 完整 Error Code 表

| Code | Class | Retryable | Severity | 觸發條件 / 處置 |
|---|---|---|---|---|
| `E_PARSE_FORMAT_UNSUPPORTED` | ParseError | ❌ | warning | 副檔名 / MIME 不支援；UI 顯示「請改上傳 PDF/DOCX 等支援格式」 |
| `E_PARSE_CORRUPT` | ParseError | ❌ | warning | 檔案損毀；不重試 |
| `E_PARSE_TOO_LARGE` | ParseError | ❌ | warning | 超過上限（預設 50 MB / 1000 page）|
| `E_PARSE_PASSWORD_PROTECTED` | ParseError | ❌ | warning | PDF 受密碼保護 |
| `E_CHUNK_INVALID_PARAMS` | ChunkError | ❌ | error | strategy params 不合法（schema validation 失敗）|
| `E_CHUNK_OOM` | ChunkError | ✅ (smaller batch) | warning | 記憶體不足；自動降低 batch size 重試 |
| `E_EMBED_TIMEOUT` | EmbedError | ✅ (3x exp backoff) | warning | embedding endpoint 逾時 |
| `E_EMBED_RATE_LIMIT` | EmbedError | ✅ (longer backoff) | warning | rate limited（HTTP 429）|
| `E_EMBED_DIM_MISMATCH` | EmbedError | ❌ | error | embedding 維度跟 collection 設定不符；不重試（model 換了要重建 collection）|
| `E_EMBED_MODEL_DOWN` | EmbedError | ✅ (with health check between) | error | endpoint 5xx 連續多次 |
| `E_PG_CONNECT` | StoreError | ✅ (3x) | error | pgvector 連線失敗 |
| `E_PG_CONSTRAINT` | StoreError | ❌ | error | unique violation / FK violation；schema bug 要 dev 修 |
| `E_PG_DISK_FULL` | StoreError | ❌ | **critical** | 磁碟滿；page on-call、不重試 |
| `E_PG_RLS_VIOLATION` | StoreError | ❌ | **critical** | RLS policy 拒絕；**代表 §3.3 layer 1+2 失守**，立即觸發安全告警 |
| `E_INTERNAL` | IngestionError | 1 次 retry | error | unknown — 完整 trace 進 audit log，不暴露給 dev UI |
| `E_CANCELED` | IngestionError | ❌ | info | 使用者主動取消 |

#### 8.1.3 三個關鍵設計原則

1. **不存在 `E_UNKNOWN`** — 任何 exception 進 worker 前都被 wrap 成上面其中一個 code。fallback 是 `E_INTERNAL` + 完整 trace 進 audit log，但 UI 顯示泛用文字（「內部錯誤，請聯絡管理員」），不洩露 stack 與檔案路徑。
2. **`E_PG_RLS_VIOLATION` 觸發安全告警** — 走 CSP 的 alerts 中心，severity=critical，自動 page on-call。代表 §3.3 layer 1+2 失守，必須立刻人工 review。
3. **Dead Letter Queue** — 超過 retry limit 的 job 進 `ingestion_jobs_dlq` table（不刪原 row、加 `failed_permanent_at` 時戳），admin UI 可看可手動 retry / mark resolved。Critical severity 的錯誤同步進 alerts 表。

#### 8.1.4 UI 顯示範例

```
✗ contract-v3.pdf — 失敗
   錯誤代碼: E_PARSE_PASSWORD_PROTECTED
   訊息: 此 PDF 受密碼保護，無法讀取。請使用未加密版本後重新上傳。
   [Show details] (admin only — 顯示 stack trace 與 audit log link)
```

---

## 9. Sprint Plan（6 週交付）

### Sprint 1（2 週）— Foundation + 雙軌收斂 ✅ 已完成 (2026-04-25)

**Goal**: 有最小可用的 ingestion API（不含 evaluator、不含 UI）+ 完成 anila-core 瘦身

實際拆成 7 個 chunks 落地，commits 列在 anila-core CHANGELOG v0.6.0：

| Chunk | 範圍 | Commit |
|---|---|---|
| A | Migration 0014（4 表 + Layer 1 + Layer 2 RLS）+ Error Taxonomy 5 codes | `567bd9c` |
| B | `anila_core.ingestion.chunking_plugins` 基礎抽象 + registry + 3 strategies | `567bd9c` |
| C | `AgentScopedPgVectorStore` + `PgPool`（jsonb / vector / halfvec codecs）+ `IngestionChunk` model + 12 constructor-guard tests | `567bd9c` |
| (RLS fix) | csp_app role 拆分 + FORCE RLS + halfvec(4000) 升級（Layer 2-prime gap）| `7c5b627` |
| D | CSP `/api/ingestion/collections` 5-endpoint CRUD + auth + audit log | `1c30c85` |
| E | `ingestion-worker` 服務（Arq + Redis）+ docker-compose 整合 + parsers (txt/md/pdf) + embedder (NV-embed-V2 client truncation) + handler. Migration 0015 把 schema 改 halfvec(4000) | `a984fa5` |
| F | AgenticRAG `api.py` + `app_factory.py` 改接中央 SDK；刪 legacy `pg_pool.py` / `pgvector_store.py`；retire 舊 IngestionService 路徑；G3 gate ✅ | `9e6ba86` |
| G | G1 / G2 / G3 pytest gates 上 CI；docker-compose 暴露 csp-db port for host pytest | `e9e913a` |

#### Sprint 1 必過 Gate × 3 — 結果

| # | Gate | 落地路徑 | 結果 |
|---|---|---|---|
| **G1** | `test_g1_random_workload_no_cross_agent_leak` | `anila-core/tests/integration/test_g1_agent_isolation.py` | ✅ 5 agent × 50 chunks × 30 query × 5 agent = **750 queries 零 leakage** (~1.7s)。`_CHUNKS_PER_AGENT` / `_QUERIES_PER_AGENT` 可調，design doc 原寫 200/1000，CI 跑 50/30 + sanity test 確保不是「RLS 把所有東西擋掉」的 false-positive |
| **G2** | `test_g2_csp_app_role_lacks_bypass_attributes` + `test_g2_force_rls_enabled_on_document_chunks` + 兩個 raw-psql bypass 測試 | `anila-core/tests/integration/test_g2_rls_bypass.py` | ✅ 4 paths 全擋：role attrs / FORCE / no-GUC / wrong-agent |
| **G3** | `test_g3_single_sql_entry_point` + literal grep ceiling | `anila-core/tests/test_g3_retrieval_path_uniqueness.py` | ✅ Actual SQL on `document_chunks` = 1 file (`pgvector_store.py`)。Literal grep 7 files (mostly docstrings)；ceiling 12 |

**Deliverable 驗證**：

- E2E pipeline 跑通，markdown upload → indexed in **200ms**（design doc 目標 7s），real NV-embed-V2 4096-d truncated to 4000-d．chunk_count = 3 in `document_chunks`．
- Agent isolation：`agent_3` 看 `agent_2` 已 indexed 的 3 chunks → **0 rows**．
- anila-core 209 passed（`+8` from Sprint 1 work）+ AgenticRAG 248 passed (`+24`)．0 regression．

### Sprint 2（2 週）— Async & More Strategies

**Goal**: 完整的 async pipeline + 6 個 strategies + 基本 UI

- [ ] 剩下 3 個 chunkers（pdf-page / cjk-sentence / semantic）
- [ ] `POST /api/ingestion/.../documents` multipart + zip 展開
- [ ] Job 進度 SSE endpoint
- [ ] CSP Developer UI 新分頁「Knowledge Collections」
- [ ] Upload dialog + job progress list + collection CRUD UI
- [ ] `agent_llm_credentials` table + CRUD + AES-256-GCM 加密儲存
- [ ] E2E smoke test：UI 上傳 → 看到進度 → done → agent 能檢索

**Deliverable**：一般 dev 可以完全透過 UI 完成 collection 建立與文件上傳

### Sprint 3（2 週）— Evaluator

**Goal**: Chunking Evaluator + apply workflow

- [ ] `ingestion_eval_runs` table
- [ ] Evaluator job handler（parallel strategies）
- [ ] Synthetic query generation（LLM 反生）
- [ ] LLM-as-judge scoring
- [ ] Metrics aggregation（Hit@k / MRR / NDCG / judge avg）
- [ ] Apply workflow（reindex with chosen strategy）
- [ ] Evaluator UI wizard 4 steps + results page + per-query breakdown
- [ ] Load test：5 個 dev 同時跑 eval → 確認 queue throughput

**Deliverable**：Dev 按一個 button 就能選出最佳 chunking strategy，從頭到尾 UI 完成

---

## 10. 風險與 Mitigation

| 風險 | 嚴重度 | Mitigation |
|---|---|---|
| Dev 的 LLM API Key 洩漏 | 🔴 高 | AES-256-GCM 加密儲存；僅 ingestion-worker 能解密；audit log 所有使用 |
| 100 agent × 大量 doc = pgvector 容量爆炸 | 🟡 中 | 監控 `pg_database_size`；加 storage quota warning（不限制 only warn）；預留 partition by agent_id 路徑 |
| IVFFlat index 在 10M+ rows 變慢 | 🟡 中 | Sprint 4 evaluate 換 HNSW index（`pgvector` 0.5+）|
| Evaluator 跑太久 dev 等到睡著 | 🟡 中 | 預設只用 20 queries × 6 strategies，估 5-10 min；提供「fast mode」= 10 queries × 3 strategies |
| LLM-judge drift（同樣 chunk 不同分數） | 🟡 中 | temperature=0 + few-shot 量表 + 每題 judge 3 次取中位數 |
| 機敏資料跨 agent 洩漏 | 🔴 高 | `AgentScopedPgVectorStore` 強制 filter；pytest 專門寫 "agent A 無法查到 agent B 的 chunk" 測試 |
| Dev 誤刪別 agent 的 collection | 🔴 高 | API 層 verify `agent.owner_user_id == caller.user_id` 或 caller 是 admin |

---

## 11. Open Questions for Kickoff

這些不影響 sprint 1 起步，但 sprint 2 前需要回答：

1. **Redis 部署**：獨立 `redis` 容器，還是用 CSP 已有的（若有）？預估記憶體 512 MB 夠用
2. **ingestion-worker 副本數**：預設 1，流量起來要不要 auto-scale？100 agent 預估峰值 ~5 concurrent jobs
3. **Evaluator 的 LLM judge 成本**：組內 dev 自付還是 CSP 代付？若代付要加 usage tracking
4. **Reindex 時舊 chunks 處理**：原地 replace、還是保留舊的 + 新版本並存（A/B test 機會）？
5. **Delete collection 是 soft 還是 hard**？soft-delete 30 天後才清 pgvector？

---

## 12. anila-core 邊界與雙軌收斂（v0.2 新增）

> Reviewer 指出：「若 ingestion 平台走 CSP 中央化，需避免 anila-core 又長出另一套 ingestion API，造成雙軌。」
>
> 事實調查（§1.4）顯示**不只是要避免長出新軌，而是現有的 anila-core ingestion 早就是死 code**，正好趁這次中央化把 anila-core 收斂回 pure runtime。

### 13.1 現況雙軌的真實狀態

```
            ANILA platform 內目前有兩份 ingestion code：

  anila-core/src/anila_core/ingestion/    AgenticRAG/src/agentic_rag/ingestion/
  ─────────────────────────────────       ────────────────────────────────────
  parsers.py                              parsers.py
  chunker.py (RecursiveTextSplitter)      chunker.py (HierarchicalChunker + ...)
  service.py                              service.py
                                          normalize.py     ← 多
                                          tokenize_zh.py   ← 多
                                          docling_parser.py← 多
                                          ocr.py           ← 多
        ▲                                       ▲
        │                                       │
   只有 tests 引用                        AgenticRAG template 自用
   (production caller = 0)               + index_documents.py CLI
```

**結論**：anila-core 那份是歷史遺物，README 早就標 Task 3 pending（要搬走），但事實上 AgenticRAG 已經有更完整版本。**直接刪除 anila-core 那份零風險**（沒 caller 會 break）。

### 13.2 收斂方案 A++（直接刪除）

| 動作 | 模組 | 風險 |
|---|---|---|
| **刪除** | `anila-core/src/anila_core/ingestion/`（4 檔） | 零（無 production caller） |
| **刪除** | `anila-core/src/anila_core/api/{documents,search}.py` | 零 |
| **刪除** | `anila-core/src/anila_core/storage/adapters/{pg_pool,pgvector_store,postgres_store}.py` | 零 |
| **刪除** | `anila-core/src/anila_core/providers/embedding_nvidia.py` | 零 |
| **刪除** | `anila-core/src/anila_core/engine/rag_preprocessor.py` | 零 |
| **拆出** | `anila-core/src/anila_core/tools/__init__.py` 的 `create_vector_search_tool` / `create_keyword_search_tool` / `create_read_document_tool` | 零（搬到 AgenticRAG template；anila-core 保留 `dispatch_tool`） |
| **拔掉** | `app_factory.py` 內的 RAG-specific wiring（IngestionService / VisionProvider / HierarchicalChunker / RagPreprocessor） | 低（要驗證 router-only 部署仍可起） |
| **保留** | Protocol 定義（`storage/ports.py`）+ pure runtime 模組（engine / coordinator / compact / memory / context / registry / router / tools.dispatch_tool / providers.{base,openai_compat,csp_platform,mock}）| — |

詳細邊界與判定原則見 **[`anila-core-boundary.md`](./anila-core-boundary.md)**。

### 13.3 與 ingestion platform sprint 編排的耦合

**不能拆開做** — 否則 sprint 2 結束時會有三套 ingestion code（anila-core 既有 + AgenticRAG 既有 + CSP 新的），永遠收斂不回來。

Sprint 1 結束時的目標狀態：
- `anila-core` 已瘦身完成（只剩 pure runtime）
- 新 ingestion-worker 服務 alive
- AgenticRAG `api.py` 改為呼叫新 SDK
- RAG tools 從 anila-core 搬到 AgenticRAG template
- 唯一直接打 `document_chunks` 的檔案 = 新版 `pgvector_store_v2`

### 13.4 anila-core 升級時的兼容性處理

由於 anila-core 還沒有 1.0 release（內部使用），**直接 breaking change** 是可接受的。但要做兩件事：

1. **CHANGELOG** 明確標 BREAKING：
   ```
   v0.5.0 (2026-XX-XX) — anila-core boundary cleanup
   BREAKING:
     - Removed `anila_core.ingestion.*` (use ANILA Ingestion Platform via CSP)
     - Removed `anila_core.storage.adapters.{pg_pool,pgvector_store,postgres_store}`
     - Removed `anila_core.api.{documents,search}`
     - Moved `vector_search` / `keyword_search` / `read_document` tool factories
       from `anila_core.tools` to `agentic_rag.tools`
   ```
2. **Migration guide** 寫一段：dev fork 的舊 agent 升級 anila-core 時要做的事

---

## 13. 非目標（Out of Scope v0.1）

留給 v0.2+：

- 🔮 Real-time document sync（Confluence/SharePoint webhook 自動同步）
- 🔮 Multi-modal（圖片單獨 search、audio transcript）
- 🔮 Cross-agent federated search（需要機敏 label 系統先做）
- 🔮 Chunking strategy 熱插拔（目前要重啟 worker 才 load 新 plug-in）
- 🔮 Golden set 的版本控制（query set 變動時比較 eval 結果的方法）
- 🔮 Fine-tune embedding model（per-domain adaptation）

---

**Last updated**: 2026-04-25 (v0.2) · **Next review**: Sprint 1 kickoff · **Companion doc**: [`anila-core-boundary.md`](./anila-core-boundary.md)

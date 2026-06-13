# ingestion-worker

> ANILA 文件攝取（ingestion）的非同步 worker：parse → chunk → embed → index 一份文件 + 跨文件關係抽取，並提供 chunking 策略評估（evaluator）。

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 worker 存在於所有 ANILA 部署分支，內容跨分支一致。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

`ingestion-worker` 是 ANILA 攝取管線的背景處理服務，**不是 HTTP API**（無 health endpoint），是以 [Arq](https://arq-docs.helpmanual.io/)（Redis 為後端的 async job queue）驅動的 worker process。CSP enqueue job，worker 取出後執行。entrypoint：`arq ingestion_worker.main.WorkerSettings`。

`main.py` 註冊 **三個** job function：

1. **`ingest_document`** — 完整攝取一份文件：
   - **Parse**（pct 15）→ **Caption + 落地 images**（pct 22，選用 VLM，把 `[[IMAGE:<id>]]` 佔位符換成描述、寫 `ingestion_images` + caption embedding）→ **Chunk**（pct 30，依 `chunking_config`，預設 `hierarchical`）→ 分 parent / leaf → **Embed leaf**（pct 60）→ **Index**（pct 85，`CollectionScopedPgVectorStore`，先 `add_parent_chunks` 再 `index_chunks`）→ 更新計數。
   - **關係抽取（best-effort、皆可關）**：① regex 引用邊（`relations.py`）② LLM 邊（`llm_relations.py`）③ embedding 主題相似邊（`similarity_relations.py`）。
2. **`evaluate_strategies`** — 對樣本文件 + 查詢比較多個 chunking 策略的檢索品質（Hit@1 / Hit@5 / MRR，選用 LLM-as-judge `judge_avg` 1–3 分），寫回 `recommended_strategy` 與 `results`。
3. **`reresolve_collection_relations`** — collection 級「重抽關係」：重新 parse 每份 `indexed` 文件、重跑 rule / LLM / similarity 邊（per-doc parse 失敗則跳過）。

Arq `WorkerSettings`：`max_tries=3`、`job_timeout=300s`、`keep_result=3600s`。

---

## 架構與技術棧

- 語言 / runtime：Python `>=3.11`；`hatchling` 打包，原始碼在 `src/ingestion_worker`。
- Job queue：`arq>=0.26`（Redis 後端）。
- 共用 SDK：`anila-core[rag]>=0.14.0` — parser registry、chunking plugins、`PgPool`、`CollectionScopedPgVectorStore`、`VisionProvider`、`IngestionError` 體系、安全工具（`decrypt_credential` / `validate_outbound_url`）。
- DB / 向量：`asyncpg>=0.29` + `pgvector>=0.3`，寫 csp-db；chunk 向量欄位 `halfvec(4000)`（migration 0015）。
- HTTP client：`httpx>=0.27`（embedding / VLM / relation-LLM / judge）。
- 設定：`pydantic-settings>=2.0`（`WorkerSettings`，env 載入，`case_sensitive=False`）。

### 向量維度合約（重要）

embedding endpoint 回傳 NV-embed-V2 原生 4096 維、不支援 OpenAI `dimensions` 截斷，故 worker 在 **client 端截斷**到 `EMBEDDING_DIM`（預設 4000，對齊 `halfvec(4000)`），並在每次回應後 assert 維度（不符即 `E_EMBED_DIM_MISMATCH`，fail-fast，不讓錯誤維度拖到 asyncpg INSERT 才爆）。

---

## 目錄結構

```
ingestion-worker/
├── Dockerfile            # repo root 為 build context；先裝 anila-core[rag] 再裝本 worker
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq WorkerSettings + 三個 job function 註冊 + on_startup/shutdown + retry
│   ├── settings.py       # env 載入的設定（DB / Redis / embedding / vision / relation / similarity）
│   ├── handlers.py       # ingest_document + reresolve_collection_relations 主流程
│   ├── embedder.py       # OpenAI 相容 embedding client（維度截斷 + assert）
│   ├── evaluator.py      # evaluate_strategies handler
│   ├── judge.py          # LLM-as-judge 評分（憑證解密 + SSRF guard）
│   ├── relations.py      # regex 引用邊（rule-based）
│   ├── llm_relations.py  # LLM 關係抽取
│   ├── similarity_relations.py  # embedding 主題相似邊
│   └── parsers.py        # anila_core.ingestion.parsers.extract_text 相容 re-export
└── tests/                # 8 個 test 檔（parsers / uniform_color / llm_relations /
                          #   handlers_helpers / embedder / judge / settings / evaluator_metrics）
```

---

## 啟動與部署

```bash
# 在 stack 中（建議）— repo root compose
docker compose -f docker-compose-dev.yml up -d --build ingestion-worker

# 本機開發 / 測試
cd ingestion-worker && pip install -e '.[dev]'
pytest            # asyncio_mode=auto；testpaths=tests
ruff check src tests
```

compose 中：build context = repo root；`depends_on`（皆 `service_healthy`）`csp-db` / `redis` / `csp`；volume `./share-dev/uploads/ingestion` → 容器 `/var/anila/ingestion-uploads`；CMD `arq ingestion_worker.main.WorkerSettings`。

### 環境變數（取自 `settings.py`）

| 變數 | 預設 | 說明 |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN，**必須**用 `csp_app` 角色（RLS） |
| `REDIS_URL` | `redis://redis:6379` | Arq 佇列後端 |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1`（compose `http://csp:8000/v1`） | embedding endpoint |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `nvidia/NV-embed-V2` / `not-set` | 模型 / Bearer token |
| `EMBEDDING_DIM` / `EMBEDDING_TIMEOUT_SECONDS` | `4000` / `30.0` | 截斷維度（對齊 halfvec(4000)）/ 逾時 |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | 與 CSP 共用的上傳 blob 目錄 |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | 連線池（亦上限並行度） |
| `ENABLE_IMAGE_CAPTIONS` | `true` | VLM caption 總開關 |
| `VISION_URL` | `""`（compose `http://csp:8000/v1`） | VLM endpoint；空字串停用 caption |
| `VISION_MODEL` / `VISION_API_KEY` / `VISION_VERIFY_SSL` | `gemma4` / `not-set` / `false` | VLM 模型 / token / TLS 驗證 |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` / `VISION_MAX_IMAGE_BYTES` | `4` / `60.0` / `8 MiB` | 並行 / 逾時 / 超過跳過 caption |
| `ENABLE_RELATION_LLM` | `true` | LLM 關係抽取總開關 |
| `RELATION_LLM_URL` | `""` | 空字串停用 LLM 邊 |
| `RELATION_LLM_MODEL` / `RELATION_LLM_API_KEY` / `RELATION_LLM_VERIFY_SSL` | `gemma4` / `not-set` / `false` | 模型 / token / TLS |
| `RELATION_LLM_TIMEOUT_SECONDS` / `RELATION_LLM_MAX_CHARS` / `RELATION_LLM_MAX_CANDIDATES` | `120.0` / `12000` / `200` | 逾時 / 輸入上限 / 候選上限 |
| `ENABLE_SIMILARITY_EDGES` | `true` | embedding 相似邊總開關 |
| `SIMILARITY_TOP_K` / `SIMILARITY_MIN` / `SIMILARITY_MAX_DOCS` | `3` / `0.75` / `500` | 每文件連 K 個近鄰 / cosine 下限 / 超過略過重算 |

> `SECRET_KEY`、`ANILA_ALLOW_*` 由 `anila-core` 安全模組消費（憑證解密 / SSRF），compose 注入 dev 預設值。

---

## 與其他服務的關係

- **CSP**：上游。enqueue job + 輪詢進度。**Embedding / VLM / relation-LLM 呼叫都路由經 CSP `/v1` proxy**（指向 `http://csp:8000/v1`），由 CSP `proxy_service` 統一寫 `token_usage`，worker 不自行記帳（`embed()` 收到的 `user_id` 直接 `del`）。以 `ingestion-worker` 系統 API key（compose 由 `INTERNAL_PLATFORM_API_KEY_DEV` 注入）對 CSP 認證。
- **csp-db**：以 `csp_app`（受 RLS）連線；RLS-scoped 寫入用 `SET LOCAL anila.collection_id`。讀 documents / collections / eval_runs / user_llm_credentials，寫 chunks / images / `document_relations` / 狀態 / 計數。
- **Redis**：Arq 佇列後端。
- **共用上傳目錄**：CSP 寫、worker 讀；captioned 圖存 `<UPLOAD_DIR>/anila-images/<doc_id>/`。
- **Judge / relation LLM**：使用者自帶憑證（`user_llm_credentials`，AES）即時解密；外連前 `validate_outbound_url`（SSRF），憑證 `__repr__` 遮罩 key。

---

## 相關文件

- [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)（含 evaluator §6.5 LLM-as-judge）
- [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md)
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

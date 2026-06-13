# ingestion-worker

> ANILA 文件攝取（ingestion）的非同步 worker：parse → chunk → embed → index 一份文件，並提供 chunking 策略評估（evaluator）。

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 worker 存在於所有 ANILA 部署分支，內容跨分支一致。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

`ingestion-worker` 是 ANILA 攝取管線的背景處理服務。它本身**不是 HTTP API**，而是以 [Arq](https://arq-docs.helpmanual.io/)（Redis 為後端的 async job queue）驅動的 worker process。CSP backend 把上傳後的文件丟成 job 進佇列，worker 取出後執行完整攝取流程：

1. **Parse** — 從上傳 blob 解析純文字（PDF / DOCX / ODF / RTF 等，透過 `anila-core` parser stack）。
2. **Image caption（選用）** — 解析器在文字留下 `[[IMAGE:<id>]]` 佔位符；設定 VLM endpoint 時，worker 呼叫視覺模型把圖片描述寫回文字，讓圖表也能被檢索。
3. **Chunk** — 依 collection 的 `chunking_config` 用對應 chunker（`hierarchical` / semantic）切塊。
4. **Embed** — 對 leaf chunk 批次向量化（呼叫 OpenAI 相容 embedding endpoint）。
5. **Index** — 把 parent / leaf chunks 連同向量寫入 PostgreSQL + pgvector（collection-scoped store）。

過程持續更新 `ingestion_documents` 與 `ingestion_jobs` 狀態（queued → running → parsing → chunking → embedding → indexing → succeeded/failed），CSP 端據此向前端推 SSE 進度。

第二個 handler `evaluate_strategies`：對一組樣本文件 + 查詢，比較多個 chunking 策略的檢索品質（Hit@1 / Hit@5 / MRR，可選 LLM-as-judge `judge_avg`），最後寫回 `recommended_strategy`。

> 本服務無 HTTP health endpoint；存活與否由 Arq worker process 與 `ingestion_jobs` 進度體現。

---

## 架構與技術棧

- **語言 / runtime**：Python `>=3.11`；`hatchling` 打包，原始碼在 `src/ingestion_worker`。
- **Job queue**：`arq>=0.26`（Redis 後端）。entrypoint：`arq ingestion_worker.main.WorkerSettings`。
- **共用 SDK**：`anila-core[rag]>=0.14.0` — parser registry（`pymupdf4llm` / `python-docx` / `odfpy` / `striprtf` / `Pillow`）、chunking plugins、`PgPool`、`CollectionScopedPgVectorStore`、`VisionProvider`、`IngestionError` 體系、安全工具（`decrypt_credential` / `validate_outbound_url`）。
- **DB / 向量**：`asyncpg>=0.29` + `pgvector>=0.3`，寫入 csp-db；chunk 向量欄位 `halfvec(4000)`（migration 0015）。
- **HTTP client**：`httpx>=0.27`（呼叫 embedding / VLM / judge LLM endpoint）。
- **設定**：`pydantic-settings>=2.0`（`WorkerSettings`，由環境變數載入）。

### 向量維度合約（重要）

embedding endpoint 預期回傳 NV-embed-V2 原生 4096 維；endpoint 不支援 OpenAI `dimensions` 截斷參數，因此 worker 在 **client 端截斷**到 `EMBEDDING_DIM`（預設 4000，對齊 `halfvec(4000)` schema），並在每次回應後 assert 維度，避免錯誤維度在 asyncpg INSERT 時才以難讀錯誤爆出。

---

## 目錄結構

```
ingestion-worker/
├── Dockerfile            # 以 repo root 為 build context；先裝 anila-core[rag] 再裝本 worker
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq entrypoint：WorkerSettings、on_startup/on_shutdown、retry 政策
│   ├── settings.py       # 由環境變數載入的設定（DB / Redis / embedding / vision）
│   ├── handlers.py       # ingest_document handler：parse→caption→chunk→embed→index 主流程
│   ├── embedder.py       # OpenAI 相容 embedding client（含維度截斷與 assert）
│   ├── evaluator.py      # evaluate_strategies handler：比較 chunking 策略的檢索品質
│   ├── judge.py          # LLM-as-judge 評分（憑證解密 + SSRF guard + 1–3 分相關性）
│   └── parsers.py        # 對 anila_core.ingestion.parsers.extract_text 的相容性 re-export
└── tests/
    └── test_uniform_color.py   # 純色圖（PDF 背景填充）過濾的單元測試
```

---

## 啟動與部署

### 在 stack 中（建議）

服務名稱 `ingestion-worker`，定義於 repo root 的 `docker-compose-dev.yml`：build context 為 repo root，dockerfile 為 `ingestion-worker/Dockerfile`；`depends_on`（皆 `service_healthy`）：`csp-db` / `redis` / `csp`；volume `./share-dev/uploads/ingestion` → 容器 `/var/anila/ingestion-uploads`（與 CSP 共用上傳目錄）；CMD `arq ingestion_worker.main.WorkerSettings`（`--watch` 刻意關閉，dev 採明確重啟）。

```bash
docker compose -f docker-compose-dev.yml up -d --build ingestion-worker
```

### 本機開發 / 測試

```bash
cd ingestion-worker
pip install -e '.[dev]'          # worker + pytest/respx/ruff
pytest                            # asyncio_mode=auto；testpaths=tests
ruff check src tests              # line-length=100, target py311
```

> 完整跑通仍需可連線的 Redis / csp-db / embedding endpoint；單純 `pytest` 只覆蓋不依賴外部服務的單元測試。

### 主要環境變數

| 變數 | 預設（settings.py） | 說明 |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN，**必須**用 `csp_app` 角色以套用 RLS |
| `REDIS_URL` | `redis://redis:6379` | Arq 佇列後端 |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1`（compose 覆寫 `http://csp:8000/v1`） | OpenAI 相容 embedding endpoint |
| `EMBEDDING_MODEL` | `nvidia/NV-embed-V2` | embedding 模型識別字 |
| `EMBEDDING_DIM` | `4000` | client 端截斷後維度，須對齊 `halfvec(4000)` |
| `EMBEDDING_TIMEOUT_SECONDS` | `30.0` | 單次 embedding timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | 與 CSP 共用的上傳 blob 目錄 |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | 連線池大小（亦上限 worker 並行度） |
| `ENABLE_IMAGE_CAPTIONS` | `true` | VLM caption 注入總開關 |
| `VISION_URL` | `""`（compose `http://csp:8000/v1`） | OpenAI 相容 VLM endpoint；空字串即停用 caption |
| `VISION_MODEL` / `VISION_API_KEY` | `gemma4` / `not-set` | VLM 模型 / Bearer token |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` | `4` / `60.0` | 單 job 並行 VLM 數 / 單張逾時 |
| `VISION_MAX_IMAGE_BYTES` | `8 MiB` | 超過則跳過 caption（避免 VLM OOM） |

> `SECRET_KEY`、`ANILA_ALLOW_*` 由 `anila-core` 安全模組消費（憑證解密 / SSRF / HTTP endpoint 放行），compose 已注入 dev 預設值。

---

## 與其他服務的關係

- **CSP backend（`csp`）**：上游。把上傳文件 enqueue 成 Arq job，並輪詢 `ingestion_jobs` 進度。**Embedding 與 VLM 呼叫都路由經 CSP `/v1` proxy**（`EMBEDDING_BASE_URL` / `VISION_URL` 指向 `http://csp:8000/v1`），由 CSP `proxy_service` 統一寫 `token_usage`，worker 不自行記帳。worker 以 `ingestion-worker` 系統 API key（compose 由 `INTERNAL_PLATFORM_API_KEY_DEV` 注入並於 CSP 啟動時 seed）對 CSP 認證。
- **csp-db（PostgreSQL + pgvector）**：以 `csp_app`（非 superuser、受 RLS）連線。讀 `ingestion_documents` / `ingestion_collections` / `ingestion_eval_runs` / `user_llm_credentials`，寫 chunks / images / 狀態 / 計數。
- **Redis**：Arq 佇列後端，承載 `ingest_document` 與 `evaluate_strategies` 兩個 job 函式。
- **共用上傳目錄**：CSP 寫入、worker 讀取；同 host 路徑 bind-mount 進兩容器（dev 為 `./share-dev/uploads/ingestion`）。captioned 圖另存 `<UPLOAD_DIR>/anila-images/<doc_id>/`。
- **Judge LLM（evaluator 選用）**：使用者自帶 LLM 憑證存 `user_llm_credentials`（AES 加密），worker 於 eval run 即時解密、呼叫評分；外連前以 `validate_outbound_url` 做 SSRF 防護，憑證 `__repr__` 遮罩 API key。

---

## 相關文件

- [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md) — 攝取平台整體設計（含 evaluator §6.5 LLM-as-judge 規格）
- [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md) — parent / leaf 雙層 chunk 與 parent-child RAG
- [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md) — `anila-core` 共用 SDK 職責邊界
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

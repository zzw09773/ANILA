# Gate 3 Data／Artifact Durability Closeout

日期：2026-07-14

分支：`codex/gate3-data-artifact-durability`

Base：`main@f647219815f9e1514ade3c2d3645fea656f06d51`（Gate 2 merge）

本文件記錄 `docs/planning/anila-development-roadmap.md` Gate 3 的工程 closeout。
GitHub CI 與 exact `claude -p --model claude-fable-5` 必須審最終 remote head；其
verdict 留在 PR closeout comment，避免把 verdict 寫回本文件後改變被審 commit。

## 1. Gate 3 工作矩陣

| ID | 完成內容 | 主要證據 |
|---|---|---|
| I1 | upload／reprocess 改為 document、job、outbox 同交易；relay 可在 Redis unavailable 時保留 durable intent，並以 deterministic job id replay | `services/csp/app/services/ingestion_outbox.py`、`test_ingestion_enqueue_rollback.py` |
| I2 | PostgreSQL lease/fence、heartbeat、retryable/permanent taxonomy、reaper、DLQ；terminal write 失敗會 fail loud | `services/ingestion-worker/src/ingestion_worker/job_state.py`、`test_job_state_pg.py` |
| I2b | 正式與 dev Redis 開 AOF；outbox、worker 與 Studio 皆有 restart replay／dedupe | `infra/compose/{platform,dev}.yml`、`test_redis_durability.py`、Redis integration tests |
| I2c | worker internal health、readiness、bounded non-identifying metrics、queue age 與 supervisor liveness | `health_server.py`、`test_health_server.py` |
| I3 | immutable staging generation、lease-fenced publication、單一 active generation 與 retry idempotency | migrations `r1_0021`、`test_g10_document_generations.py` |
| I4 | document availability 與 processing/generation state 分離；re-index 失敗仍保留舊 active generation | ingestion models/migration、`test_g10_document_generations.py` |
| I5 | file read／parser 移出 event loop；parse/index/job timeout 與 cancellation 收斂到唯一 durable terminal/retry state | `document_io.py`、`test_document_io.py`、worker handler tests |
| I6 | LLM relation HTTP 與推論搬出 DB transaction，寫入前重新驗 lease／clearance | `llm_relations.py`、`test_llm_relations.py` |
| I7 | collection-scoped monotonic debounce/dedupe lease；超量與重算會清 stale edge，lost owner 不能 mutation | `similarity_relations.py`、durable/PG concurrency tests |
| I8 | 每個 generation 保存 embedding model、SHA-256 fingerprint 與 dimension；不相容 posture fail-stop | worker settings/handlers、generation migration/integration tests |
| I9 | production backup profile 作 SSOT，覆蓋 DB、blob/artifact、active generation、release/config/key references、retention、加密/off-host/alert；提供 prepared restore automation | `infra/deployment/backup/`、`production-backup-restore.md`、deployment tests |
| A1 | Studio 五條 pipeline 改為 Redis durable envelope、lease/fence、heartbeat、retry/DLQ 與 restart supervisor | `job_store.py`、`job_supervisor.py`、Redis integration tests |
| A2 | CSP content-addressed immutable blob store；所有正式 artifact bytes（含 slides）先落 blob 再發布 version | `blob_store.py`、`test_artifact_blob_store.py` |
| A3 | CSP Artifact／Version 為唯一 SSOT；download/export/reporting 重新驗 owner、Task、snapshot/collection、classification、compartment、version、revocation | `services/csp/app/api/artifacts.py`、artifact/studio runtime tests |
| A4 | ANILALM Outputs／Viewer 改讀 CSP；logout、換使用者與舊 storage migration 都不保留 artifact metadata | `apps/anilalm/src/store/cspArtifacts.ts`、`test_anilalm_browser_storage.py` |
| A5 | blob/image/counter/archive/retention/erase lifecycle，含 legal hold、active task/job、race、symlink/traversal 與 idempotent reconciliation | `retention_reaper.py`、retention unit/PG tests |
| A6 | upload extension、declared MIME、magic bytes、OOXML container、macro 與 archive traversal fail-closed | `content_sniffing.py`、sniffing/upload negative tests |

額外收斂：Redis job 與 ingestion queue envelope 加 HMAC integrity proof；worker 在讀 raw
document 與每個 inference boundary 前重新驗 authoritative actor clearance；Studio runtime
使用獨立、具名、Task-scoped service client，不能沿用 browser bearer 或 artifact writer
token。

## 2. Security closeout

對 Gate 3 working diff 做完整 discovery、validation 與 attack-path analysis。過程發現並在
同一分支修正 8 個 pre-final candidate：ArtifactJob lease/state hijack、image blob live
clearance、foreign artifact export、無換行 SSE memory DoS、nested Studio inference TaskRun
錯綁、偽造 ingestion Redis job、stale ingestion/reresolve clearance，以及偽造 Studio
Redis envelope。

修正後 canonical finding set 為 0；security work ledger 66/66 完成，未留下 suppressed
code finding。這個結果不取代最終 remote head 的 Fable review。

第一輪 Fable review 對 `1914b8c` 提出兩個 blocker，已在後續 head 修正並加上
mutation-sensitive regression：

- retention reaper 現在於 counter reconciliation 與 document erase transaction 設定
  transaction-local `anila.collection_id`，並檢查 scoped delete row count 與 residual；
  `csp_app` 真 PostgreSQL 測試實際建立 FORCE-RLS image/chunk/relation rows 及磁碟檔案，
  舊實作會留下 image bytes，新實作完整清除。
- published outbox 只有 DB job 仍為同一個 queued attempt 時才保留 Redis-loss replay；
  job 已前進後清除無 recovery 用途的 receipt，並在同次 claim 立即往後掃。150 筆終態
  歷史資料加 1 筆新 pending 的 regression 證明新任務不會 starvation。
- GitHub inline review 指出 backup external-command 啟動失敗會裸拋 `OSError`；`Runner`
  現已轉譯為保留 cause 的 `BackupAutomationError`，缺少 `docker`／`age` 等依賴時以
  受控 non-zero fail-closed，並有 `FileNotFoundError` mutation regression。

## 3. 測試與 runtime 證據

### 3.1 完整套件

- CSP：`1209 passed, 31 skipped`（31 項皆由下一節的真 PostgreSQL job 補跑）。
- ingestion-worker：`219 passed, 10 skipped`；Ruff 全綠。
- anila-studio：`585 passed, 5 skipped`。
- anila-core：`806 passed, 11 skipped`。
- anila-security：`37 passed`。
- deployment contracts：`135 passed, 2 skipped`。
- ANILALM：OpenAPI export、generated types、`npm run typecheck`、`npm run build` 全綠。
- PPTX renderer：4 支 live HTTP contract tests 全綠；production `npm audit` 為 0
  vulnerabilities。`qs` 由有 advisory 的 `6.15.1` 升到 `6.15.3`，CI 已加入
  Moderate 以上 audit 與 live renderer tests。
- 所有 Gate 3 變更 Python 檔 Ruff 全綠；workflow YAML parse 與 `git diff --check`
  通過。
- Gate test-governance：16 required suites、24 registered skip callsites、0 xfail、
  0 quarantine；Gate 1 capability freeze：25 surfaces／372 entries；Compose
  image-lock wiring 全綠。

`skip` 是既有的環境／真 PostgreSQL 分流，不是新增 quarantine；下節以 disposable
PostgreSQL／Redis 補跑 Gate 3 infra contracts。

### 3.2 真 PostgreSQL／Redis 與 restore

以專用 disposable `pgvector/pgvector:pg16` 與 `redis:7-alpine` volumes 執行：

- fresh DB 從零 migration 到 `r1_0024 (head)`。
- CSP retention/upload PostgreSQL race／FORCE-RLS erase：3 passed。
- ingestion generation migration/backfill：1 passed。
- anila-core pgvector/RLS：10 passed。
- worker lease/similarity PostgreSQL concurrency：10 passed。
- Studio Redis AOF/restart：5 passed。
- 新增的 GitHub PostgreSQL job 指令另以第二個 fresh container 重跑：generation
  migration 1 passed；worker lease/DLQ/similarity 10 passed。
- prepared restore 使用實際 `pg_dump`。第一次故意缺 artifact blob 時 fail-closed；補入
  referenced surface 後 readback 為：

```json
{"checks":{"active_generation_chunk_count_mismatches":0,"active_generation_contract_violations":0,"active_leaf_invalid_embeddings":0,"artifact_blob_references":1,"attachment_blob_references":0,"chunk_document_orphans":0,"indexed_documents":0,"ingestion_blob_references":0},"status":"pass"}
```

這是 Gate 3 I9 的 prepared restore smoke；正式獨立、破壞性 DR drill 仍由 Gate 6 P1
驗收，不在此偷換定義。

### 3.3 正式映像與 non-root runtime

本機 Docker build/import smoke 的 manifest digests：

- CSP：`sha256:00f3d7ca057e3ebfc8e61ae1e431b9fc84e878f2210d0bde8c917443ad62dea6`，user `csp`。
- ingestion-worker：`sha256:3644fd344cda19679acaa5b1a88bb397c00e9b81e4819b4641211595ad7f0d7f`，user `ingestion`。
- Studio：`sha256:b7b6905cb95b0206883ebede31c8be16593bdc77fe2466c4ed497abcd88dda5f`，user `anila`。
- FLUX agent：`sha256:32ec37debdb240bf1e2a09efb7d1787906f58bddff27123445150d976660cd1f`，user `anila`。
- PPTX renderer：`sha256:adca980d4b53670ba30b4a6baf2ff2c95d59e88352a44031983ae03305789ec0`，user `anila`。

五個應用／artifact data-plane images 都以 UID/GID 10001 執行。CSP 與 worker 在同一
fresh upload volume 實測雙向 create/read/delete；FLUX share 與 PPTX temp surface 也做
non-root write/delete smoke。底層 GPU model server image 不在本 Gate 3 application
runtime 變更內，仍須在 Gate 6 的實體硬體／egress acceptance 驗證。

測試後已刪除 `anila-platform`、`anila-platform-dev` 與 Gate 3 disposable
containers/volumes；沒有以舊資料狀態充當通過證據。

## 4. Gate 3 merge gate

只有以下全部成立才能 merge：

1. GitHub required CI 對最終 remote head 全綠。
2. 所有 review threads resolved，且沒有新 blocker。
3. exact `claude -p --model claude-fable-5` 審最終 remote head，session metadata 證明
   實際 model，verdict 明確 `Approve`。
4. 本機與 remote head 一致、worktree clean、PR base 仍為 `main`。

## 5. Gate 4 與 Production 邊界

Gate 4 是唯讀 Agentic timeline Demo Lane；Gate 3 未 merge 前不得開始。Gate 3 完成也
不代表機密 production Go。實體卡完整矩陣、異機／破壞性 DR restore drill、PKI/CRL
freshness、實體 egress/ingress、SLO、signed acceptance profile 與 release artifact 仍是
Gate 6 blocker。

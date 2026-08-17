# anila-studio extraction — Architecture Decision Record

**Date**: 2026-05-23
**Status**: Implemented in PR #12

## Context

ANILA 平台原本把「簡報生成」邏輯(FLUX 圖像 + RAG + LLM 大綱 + PPTX 渲染)放在 `myCSPPlatform/backend` 內(8 個 service 檔 + studio.py + schemas)。frontend 是 ANILALM(Vite + React SPA)。

User 痛點:**「ANILALM 簡報製作插圖時,重啟的卻是 csp」** ── 改插圖邏輯要 rebuild csp container,dev loop 慢。

## Decision

把 Studio 整套抽出成獨立 `anila-studio` FastAPI service:
- 透過 HTTP 跟 csp 溝通(不直連 csp-db,不共用 SECRET_KEY)
- ANILALM 走 `STUDIO_BASE_URL` 切到 anila-studio
- csp 端只保留 Studio 需要的 contract endpoint(ingestion search / image blob / JWKS / token revoke)

## Why these specific choices

### 為什麼用 RS256 + JWKS,不是共享 SECRET_KEY

對稱 HMAC(HS256)需要 csp 跟 anila-studio 都拿 private key。任一邊 leak 雙邊失守。改 RS256 非對稱:
- csp 持 private key 簽
- anila-studio 從 csp `GET /.well-known/jwks.json` 拿 public key 驗
- 任何擴增的 service(未來 anila-X)都用同 pattern

副作用:cutover 時所有 user 強制重登一次,因 HS256 token 跟 RS256 不相容。Plan 接受這個 trade-off。

### 為什麼用 Redis pub/sub 做 revocation,而非 polling

JWT verify 在 anila-studio 端本地做(快)。但 csp 端 user ban / token revoke 時需要立即跨服務生效:
- **Polling**:anila-studio 每秒拉 csp `/revocations`?成本高、延遲大
- **Redis pub/sub**:csp publish event,anila-studio subscribe + memory cache lookup。延遲 <100ms,Redis 本來就在 stack 內

cold-start 機制:啟動時拉近 30 天的撤銷紀錄補洞(防 Redis pub/sub fire-and-forget 漏)。

### 為什麼 fail-closed when Redis disconnects

原 plan v1 設計「Redis 失聯 → 降到 JWT TTL only」── codex 複核標 CRITICAL 漏洞:被 ban 的 user 在 Redis 失聯窗口內最多 60min 內仍可存取。

Plan v2 改 **fail-closed**:Redis 失聯 → revocation_cache.ready = False → auth dependency 回 503。Caller(`/health` + auth endpoints)都 503 直到 Redis 恢復。安全屬性 > 可用性。

### 為什麼 in-memory job state(不持久化)

原 csp 端 studio_job_service 就是 `_jobs: dict[str, JobRecord] = {}` ── 進程內 state。抽出後 anila-studio 維持這個設計:
- 簡單,無 DB 搬遷負擔
- Job 平均生命週期 30s,重啟丟失影響小
- 真要持久化未來用 Redis hash 或加 csp DB 都行

### 為什麼 csp 仍是「control plane」

Plan v2 沒選「全 standalone 每個子專案自己 DB」── 那是 100+ 檔的架構重寫,跟 user 痛點(Studio 改邏輯)scope 不對等。

維持「csp 是 control plane,anila-studio 是 worker / extension」:
- 用戶身份 / model registry / LLM 計費 都在 csp
- ingestion 資料層 都在 csp(anila-core + pgvector + csp-db)
- anila-studio 用 csp HTTP API,需要時抽出更多 worker(類似 ingestion-worker)

### 為什麼 OpenAPI codegen 而不是 npm package

ANILALM 需要 TypeScript types 對齊 anila-studio。三個選項:
- **OpenAPI + codegen**(選):FastAPI 自帶 OpenAPI spec,`openapi-typescript` 一鍵生
- npm package shared-types:手寫同步,長期 drift
- pydantic→ts 工具:多裝一套工具

OpenAPI 是 backend authoritative source,codegen 結果 check-in 讓 PR diff 看得到 type 變化。

## Alternatives considered

### A. 把 Studio 搬進 ANILALM 倉(ANILALM 加 Python backend)
- 好處:直覺(Studio 是 ANILALM 的 feature)
- 壞處:ANILALM 是 Vite + React SPA,加 Python = 重蓋部署架構

### B. csp 內抽 sub-package(不獨立 service)
- 好處:工程量小
- 壞處:**不解決 user 的根痛(改插圖要 rebuild csp)**

### C. 全 standalone(每個子專案自己 DB)
- 好處:極致 modular
- 壞處:100+ 檔重寫,csp-db schema 拆 N 個 DB,2-6 月工程量,跟 user 痛點不對等

## Consequences

### 正面

- **dev loop**:改 flux_image_provider.py → rebuild anila-studio(~30s)而非 csp(~2min)
- **clear ownership**:Studio 程式 vs csp control plane 邊界明確
- **future-proof**:同 pattern 可抽出更多 worker(ingestion-worker 已是這個 pattern)
- **security**:RS256 + JWKS 比 SECRET_KEY 共享更 prod-grade

### 負面

- **架構複雜度** +1:多一個 service / docker container / network hop
- **跨 service latency**:LLM 從 csp 內 in-process call 變 HTTP call(~5ms 增加,可接受)
- **deploy 操作** +1:多 docker-compose entry,多 health check 對象
- **JWT cutover 一次性重登**:所有現有 user 必須重登

## Implementation summary

**Phase 0-8 全部 commit 在 PR #12**(`feature/anila-studio-extract`):

| Phase | Commits | Notes |
|---|---|---|
| 0 | 2 | plan v2 + baseline + skeleton |
| 1 | 5 | csp endpoint 暴露 / JWKS / revocation |
| 2 | 6 | anila-studio skeleton + jwks_client / revocation_cache / csp_client / auth + Dockerfile + compose |
| 3 | 14 | 搬 10 service + studio.py 9 sub-commit A→I |
| 4 | 5 | 搬 20 test + numpy dep |
| 5 | 4 | OpenAPI export + ANILALM codegen + studio.ts refactor |
| 6 | — | E2E by user(`2026-05-23-e2e-runbook.md`) |
| 7 | 3 | csp 刪 27 個 dead 檔 |
| 8 | 本 PR | docs/README |

**測試結果**:
- anila-studio:269 passed / 3 baseline failed(`test_hydrate_images.py` 既有 async issue,plan 不修)
- csp 端 non-Studio:69 passed(Phase 1 endpoint)
- 0 regression

**期程**:plan v2 估 15-17 工作天,平行 subagent 加速跑完 ~4 hr。

## References

- Plan v2 設計文件:`docs/superpowers/anila-studio/plans/2026-05-23-extraction-plan.md`
- E2E runbook:`docs/superpowers/anila-studio/plans/2026-05-23-e2e-runbook.md`
- Baseline:`anila-studio/MIGRATION_BASELINE.md`
- anila-studio README:`anila-studio/README.md`

> ⚠ **2026-08-01 P2.1**：本計畫／規格所描述的 `csk-` 入向守衛與靜態服務憑證路徑，
> 已由派工 JWT＋JWKS 驗簽取代。下文保留原設計決策軌跡；現行接入見
> `docs/guides/developer-guide.md`。

# ANILA Studio Service Extraction Plan

**Date**: 2026-05-23
**Version**: v2(經 codex 複核後修訂)
**Author**: Claude(planner agent 細化)+ codex 複核
**Scope**: II — Studio extraction + shared types/contract(~62 檔 · 15-17 工日)
**Branch**: `feature/anila-studio-extract`(待開)

---

## v2 Changelog(2026-05-23 codex review 後修訂)

**Critical 阻斷修正**:
- **R14 fail-closed**: Redis 失聯時,需要 revocation check 的 endpoint 一律 503,不再降到 TTL-only mode
- **Phase 6 ↔ Phase 7 順序對調**: E2E 驗收(原 Phase 7)提前到 csp 刪檔(原 Phase 6)之前,避免「刪了再測」
- **Cold-start race**: anila-studio health check 加 readiness gate,csp `/revocations` 不通就 503

**High 補強**:
- **JWKS 非對稱 JWT 升級**(取代 SECRET_KEY 對稱共享)── 見「JWKS 升級設計」新章節
- **Phase 3 sub-commit A→I 邊界定義**: 每步 explicit content + `python -m compileall` import gate
- **Token version 語意定義**: revocation cache 比較 `>=` 含同版本 token 行為,Phase 1 contract test 涵蓋 bump 前/後/同
- **Cold-start 視窗對齊**: csp `/revocations?since=` retention 改 30 天,對齊 anila-studio cache TTL
- **PR #4 acceptance 強化**: 加 FLUX wiring / RAG wiring / PPTX wiring 三條 smoke
- **RAG endpoint 授權**: Phase 1 contract test 明確驗 caller user permission(`_require_collection_access` 跨 service 行為)

**Medium 改進**:
- **CI codegen drift detection**: ANILALM CI 重 gen 比對,不同則 fail
- **`cachetools.TTLCache`**: revocation cache 改用標準 TTL 容器,加過期測試
- **Phase 5 自動化驗收**: 加前端整合測試 assert API URL 正確,不只靠 user 看 DevTools

**Low**:
- Phase 1 endpoint 數量列清:**6 個**(原 5 個 + JWKS endpoint)

**期程影響**: 12-14 工日 → **15-17 工日**(主要為 JWKS 升級 +2-3 天)
**檔案動量**: 62 檔(+ JWKS 約 +5 檔)

---

## 動機(問題陳述)

User 觀察:**「ANILALM 簡報製作插圖時,重啟的卻是 csp」**

實況:Studio 的 10 個服務檔(`flux_*.py` × 4、`studio_*.py` × 3、`diagram_renderer.py`、`geometric_qa.py`、`schemas/studio.py`)+ `api/studio.py` 全部住在 `myCSPPlatform/backend`,但功能上是 ANILALM 的「簡報製作」── 改插圖邏輯要 rebuild 整個 csp control plane。dev loop 慢。

## 三個技術決策(user 已 confirm)

1. **Location**: 新建獨立 `anila-studio/` Python service(FastAPI)
2. **RAG strategy**: csp 暴露 `POST /api/ingestion/collections/{cid}/search` + 新增 `/images/search` + `/images/{id}/blob` HTTP endpoint,anila-studio HTTP 呼叫(不直連 csp-db)
3. **Shared types**: OpenAPI spec + `openapi-typescript` codegen → ANILALM `src/api/studio-types.gen.ts`

## 預設

- **Auth(v2 升級到非對稱 JWKS)**: csp 改用 **RS256 簽發 JWT**,公鑰透過 `GET /.well-known/jwks.json` 暴露;anila-studio 取 public key 本地驗;**JWT revocation 用 Redis pub/sub 跨服務通知**(見「JWT Revocation 機制」一節)
- **Model registry**: anila-studio HTTP call csp `/api/proxy/v1/chat/completions`(LLM 走 csp proxy 維持計費)
- **FLUX cache**: anila-studio 自己一個 docker volume

## JWKS 升級設計(v2 新增)

### 為何升級

原 plan 用對稱 HMAC(HS256)+ 共享 `SECRET_KEY`,任一 service compromise 雙邊失守(codex finding #8)。改用非對稱算法:csp 簽 / anila-studio 只驗。

### 設計

1. **算法**: RS256(RSA-2048;ES256 也可,但 RSA 工具鏈成熟度較高)
2. **Key pair 生成**:
   - csp 啟動時若 `JWT_PRIVATE_KEY_PATH` 環境變數指向的 PEM 不存在 → 用 `cryptography.hazmat` 生成 RSA-2048,存 PEM 到 `/var/anila/secrets/jwt-private.pem`(secret volume,non-world-readable)
   - 對應 public key 也存 `jwt-public.pem`,用於 JWKS endpoint serve
   - Production:由 ops 預先生成 + Vault 注入,不在 container 生(避免 fleet 內 key 不一致)
3. **JWKS endpoint(csp)**:
   - `GET /.well-known/jwks.json` → 回 standard JWKS format
     ```json
     { "keys": [{ "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "anila-v1", "n": "...", "e": "AQAB" }] }
     ```
   - 不需 auth(public key 本來就 public)
   - HTTP cache header `Cache-Control: max-age=3600`
4. **csp 簽發 JWT**:
   - `app/utils/security.py` 改用 `jose.jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "anila-v1"})`
   - 簽算法切換 cutover 一次性,所有舊 HS256 token 強制重登(無雙 algo 過渡)
5. **anila-studio 驗 JWT**:
   - `auth.py` 啟動時 fetch `${CSP_BASE_URL}/.well-known/jwks.json` cache 進 memory
   - JWT header 帶 `kid`,verify 時依 `kid` 選 public key
   - 每 1 小時 background task 重 fetch JWKS(cover key rotation)
6. **Key rotation 流程**:
   - csp 生新 key pair `anila-v2`,JWKS 同時 serve `v1` 與 `v2`
   - csp 簽新 token 改用 `v2`
   - 24 小時後(所有 `v1` token 已 refresh 過)從 JWKS 移除 `v1`
   - anila-studio 自動跟上(因每 1hr refetch JWKS)

### Cutover 風險

- 所有現有 access + refresh token 失效一次 → 全平台 user 必須重登
- 緩解:選低使用時段、發前置公告
- Phase 1 完成後立即 deploy 並等所有 user 重登再進 Phase 2

### 新增 task(歸 Phase 1)

- 7d: csp `app/utils/security.py` 從 HS256 改 RS256,加 key load logic
- 7e: csp 新增 `GET /.well-known/jwks.json`
- 7f: csp 生 key pair 與 Vault / volume mount 流程(部署手冊)
- 7g: contract test `test_jwks_endpoint.py` 確保 JWKS payload shape
- 11c: anila-studio `services/jwks_client.py`(fetch + cache + rotation poll)
- 11d: `tests/test_jwks_client.py`

工日: +2-3

## JWT Revocation 機制(選 c · prod 級)

### 既有 csp 流程
csp `_load_user_from_payload` 比對 `token_version` 防 token 撤銷:管理員 ban 一個 user → 該 user `token_version` bump → 該 user 持有的舊 JWT 立即失效。anila-studio 不查 DB,單純 verify JWT 簽章無法檢測撤銷。

### 新機制設計

1. **Channel**: Redis pub/sub channel `anila:auth:token-revoke`
2. **Publisher (csp)**: csp 每次 bump user `token_version` 或主動撤銷 token 時,publish
   ```json
   {"user_id": 42, "revoked_at_version": 3, "ts": "2026-05-23T05:40:00Z"}
   ```
3. **Subscriber (anila-studio)**:
   - Service startup 時 cold-start 從 csp `GET /api/auth/revocations?since=<ts>` 取得近 24hr 撤銷清單(防 service restart 期間漏接)
   - Background task `redis.pubsub.subscribe("anila:auth:token-revoke")` 持續接收
   - 內部維護 `dict[int, int]` = `{user_id: latest_revoked_version}`,memory cache
4. **Verify path**:
   - JWT verify 後拿 `user_id` 跟 `token_version`
   - 查 cache:若 `cache[user_id] >= token_version` → 拒收 401 + reason "token_revoked"
5. **Cache eviction**: TTL 30 天(對齊 csp `/revocations?since=` retention,使用 `cachetools.TTLCache` 標準容器,非 plain dict)

### Token version 語意(v2 補)

- `revoked_at_version=N` 表示 user 的「<= N 版本的 token 全部撤銷」
- verify path: 取 token 內的 `token_version` 與 cache 比較;**若 `cache[user_id] >= token_version` → reject**(含 `==` 即「同版本也算撤銷」── 用戶 ban 自己當前 active session 的標準語意)
- Phase 1 contract test 涵蓋:bump 前 token(舊 version)、bump 後 token(新 version)、同 version 三種 case

### Fail-closed policy(v2 critical 修正)

- anila-studio 啟動時必須 `revocation_cache` cold-start sync 成功才開放 traffic(`/health` 才回 200)
- 運行中 Redis 連線中斷:
  - 需要 revocation check 的 endpoint **fail-closed**,回 503 + `Retry-After`
  - 不是「降到 TTL-only mode」── 那會讓被 ban 的 user 在 Redis 失聯時繼續存取,違反安全屬性
  - exponential backoff 自動重連 Redis
- 啟動時 csp `/api/auth/revocations?since=` 拿不到 → service 不 ready(health 503)

### 影響範圍(加入 plan 後)

- **新 csp endpoint**: `POST /api/auth/revoke` (admin only) + `GET /api/auth/revocations?since=<ts>`(service-to-service)
- **新 csp service**: csp 在 user ban / token_version bump 時 publish event
- **新 anila-studio 模組**: `anila-studio/app/services/revocation_cache.py`(subscriber + cache)
- **依賴**: anila-studio 加 `redis>=5` dependency
- **docker-compose**: anila-studio depends_on `anila-platform-redis`(csp 已用的 Redis)
- **預估 額外 +3-4 task**(歸到 Phase 1 與 Phase 2)

---

# Architecture Changes

## 新建檔(anila-studio/)

| 路徑 | 內容 |
|---|---|
| `anila-studio/pyproject.toml` | Python 套件宣告(fastapi, httpx, pydantic, jose, opencc, **redis>=5**, graphviz dep) |
| `anila-studio/Dockerfile` | 基底 image + non-root + multi-stage + apt graphviz |
| `anila-studio/app/main.py` | FastAPI app + lifespan(初始化 FluxImageProvider + revocation_cache subscriber) |
| `anila-studio/app/config.py` | env settings(`CSP_BASE_URL` / `SECRET_KEY` / `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` / `REDIS_URL` / `INTERNAL_TIMEOUT_SECONDS` / `FLUX_CACHE_DIR` / `LOG_LEVEL`) |
| `anila-studio/app/auth.py` | 本地 JWT verify + `get_current_user_identity`(整合 revocation_cache) |
| `anila-studio/app/services/revocation_cache.py` | Redis pub/sub + in-memory cache + cold-start sync |
| `anila-studio/app/clients/csp_client.py` | csp HTTP wrapper(5 個 thin async function) |
| `anila-studio/app/api/studio.py` | 從 csp 搬過來、改 import |
| `anila-studio/app/services/studio_job_service.py` | 直接搬 |
| `anila-studio/app/services/studio_text_normalizer.py` | 直接搬 |
| `anila-studio/app/services/flux_image_provider.py` | 直接搬 |
| `anila-studio/app/services/flux_prompt_rewriter.py` | 直接搬 |
| `anila-studio/app/services/flux_quality_gate.py` | 直接搬 |
| `anila-studio/app/services/flux_style.py` | 直接搬 |
| `anila-studio/app/services/diagram_renderer.py` | 直接搬 |
| `anila-studio/app/services/geometric_qa.py` | 直接搬 |
| `anila-studio/app/schemas/studio.py` | 直接搬 |
| `anila-studio/openapi/studio.openapi.json` | OpenAPI export(由 main.py 產生) |
| `anila-studio/tests/` | 搬 17 個 test 檔 + 補新 contract / smoke test |

## 改檔(csp 端)

| 路徑 | 改什麼 |
|---|---|
| `myCSPPlatform/backend/app/api/ingestion/search.py` | Phase 1: 加 `POST /collections/{id}/images/search` |
| `myCSPPlatform/backend/app/api/ingestion/image_blob.py` 或併入 search.py | Phase 1: 加 `GET /images/{id}/blob`(streaming) |
| `myCSPPlatform/backend/app/api/auth.py` | Phase 1: 加 `POST /api/auth/revoke` + `GET /api/auth/revocations` |
| `myCSPPlatform/backend/app/services/auth_service.py` | Phase 1: token revoke 時 publish Redis event |
| `myCSPPlatform/backend/app/api/router.py` | Phase 6: 移除 studio_router 註冊 |
| `myCSPPlatform/backend/app/api/studio.py` | Phase 6: 刪檔 |
| `myCSPPlatform/backend/app/services/flux_*.py` (4 個) | Phase 6: 刪檔 |
| `myCSPPlatform/backend/app/services/studio_*.py` (3 個) | Phase 6: 刪檔 |
| `myCSPPlatform/backend/app/services/diagram_renderer.py` | Phase 6: 刪檔 |
| `myCSPPlatform/backend/app/services/geometric_qa.py` | Phase 6: 刪檔 |
| `myCSPPlatform/backend/app/schemas/studio.py` | Phase 6: 刪檔 |
| `myCSPPlatform/backend/tests/test_studio_*.py` / `test_flux_*.py` / `test_hydrate_images.py` / `test_layout_*.py` / `test_theme_override.py` / `test_slide_image_prompt.py` (17 個) | Phase 6: 刪檔 |

## 改檔(ANILALM 前端)

| 路徑 | 改什麼 |
|---|---|
| `ANILALM/src/api/studio.ts` | Phase 5: 改 base URL + codegen types |
| `ANILALM/src/api/client.ts` | Phase 5: 加 `STUDIO_BASE_URL` |
| `ANILALM/package.json` | + `openapi-typescript` devDep |
| `ANILALM/scripts/gen-studio-types.sh` | Phase 5: 新建 codegen script |
| `ANILALM/src/api/studio-types.gen.ts` | Phase 5: codegen 輸出(checked-in) |
| `ANILALM/vite.config.ts` | Phase 5: 加 `/api/studio` proxy |
| `ANILALM/.env.example` | Phase 5: `VITE_STUDIO_BASE_URL` |

## 改 docker-compose

| 路徑 | 改什麼 |
|---|---|
| `docker-compose.yml` | Phase 2: 加 `anila-studio` service · share `SECRET_KEY` · depends_on csp + pptx-renderer + flux2-dev + redis |
| `docker-compose-dev.yml` | Phase 2: 同步加 dev override |

---

# 8 Phase / 47 Tasks(加入 revocation 機制後)

## Phase 0 — 基準線

| Task | 動作 | Risk |
|---|---|---|
| 1 | `git checkout -b feature/anila-studio-extract` | Low |
| 2 | 跑 csp baseline test(`pytest tests/test_studio* tests/test_flux* tests/test_hydrate_images tests/test_layout_* tests/test_theme_* tests/test_slide_image_prompt -v`)記錄 pass/fail | Low |
| 3 | grep `app\.(api|services|schemas)\.(studio|flux_|studio_)` 確認 csp 外部無 import | Medium |
| 4 | 建 `anila-studio/{app,tests,openapi}/` 空目錄 | Low |

**PR #1**: baseline 報告 + 空目錄。Acceptance: 分支存在、baseline 記錄、grep 無外部 import。

## Phase 1 — csp endpoint 暴露

| Task | 動作 | Risk |
|---|---|---|
| 5 | 寫 `test_search_contract.py`(鎖既有 `POST /collections/{id}/search` 簽名) | Low |
| 6 | 新增 `POST /api/ingestion/collections/{id}/images/search`(包 `_retrieve_images()`)+ contract test | Medium |
| 7 | 新增 `GET /api/ingestion/images/{id}/blob`(streaming binary,collection_id 鑑權)+ contract test | Medium |
| **7a (新)** | 新增 `POST /api/auth/revoke`(admin)+ `GET /api/auth/revocations?since=<ts>`(service-to-service token) | Medium |
| **7b (新)** | csp `auth_service.py` 加 Redis publisher(token revoke 時 publish `anila:auth:token-revoke`) | Medium |
| **7c (新)** | 寫 `test_token_revoke_publish.py`(mock Redis,assert publish payload shape) | Low |
| 8 | router.py include 新 endpoint | Low |

**PR #2**: 5 個新 endpoint + Redis publish + 4 個 contract test。Acceptance: 全綠 + curl 5 條 endpoint 都正確回。

## Phase 2 — anila-studio skeleton

| Task | 動作 | Risk |
|---|---|---|
| 9 | `pyproject.toml` + `Dockerfile`(apt graphviz)+ `redis>=5` dep | Medium |
| 10 | `main.py`(/health)+ `config.py`(含 `REDIS_URL`) | Low |
| 11 | `auth.py`(本地 JWT verify 整合 revocation_cache 查詢)+ `tests/test_auth.py` | High |
| **11a (新)** | `services/revocation_cache.py`(Redis subscriber + memory cache + cold-start sync via csp `/api/auth/revocations`) | High |
| **11b (新)** | `tests/test_revocation_cache.py`(mock Redis pubsub + cold-start sync) | Medium |
| 12 | `clients/csp_client.py`(5 個 thin async function)+ `tests/test_csp_client.py` | Medium |
| 13 | docker-compose 加 `anila-studio` service entry · share SECRET_KEY · depends_on redis | Medium |

**PR #3**: anila-studio skeleton + compose 改動(不啟用)。Acceptance: 本機 uvicorn 起來、unit test 綠、docker-compose config 解析無錯。

## Phase 3 — 搬程式 + 改 import

| Task | 動作 | Risk |
|---|---|---|
| 14 | 一次 commit 搬 9 個檔(`studio_job_service` + `studio_text_normalizer` + `diagram_renderer` + `geometric_qa` + `flux_*` × 4 + `schemas/studio`) | Low |
| 15 | 搬 `api/studio.py` 並改造(分 sub-commit A→I):刪 DB / model_registry / proxy_request,改用 csp_client + 本地 auth | **High** |
| 16 | `main.py` include studio_router | Low |
| 17 | 手測 `uvicorn app.main:app` 起得來、`/docs` 看到 4 條 studio endpoint | Low |

**PR #4**(可拆 #4a/#4b): 含整個 Phase 3。Acceptance: anila-studio uvicorn 起、import 過、health endpoint OK。

## Phase 4 — 搬 + 補 test

| Task | 動作 | Risk |
|---|---|---|
| 18 | 一次 commit 搬 17 個 test 檔 | Low |
| 19 | 跑 `pytest anila-studio/tests/ -v` 修紅燈(mock 改 csp_client / identity 改 dataclass / image fetch 改 mock blob) | **High** |
| 20 | 補 `test_csp_client_contract.py`(respx mock csp,assert HTTP shape) | Low |
| 21 | 補 `test_pipeline_smoke.py`(ASGITransport + full mock) | Medium |

**PR #5**: 完整 anila-studio test suite。Acceptance: 紅燈集合 ⊆ Phase 0 baseline、覆蓋率 ≥ csp -5%。

## Phase 5 — ANILALM 前端切換

| Task | 動作 | Risk |
|---|---|---|
| 22 | `npm install --save-dev openapi-typescript@^7` | Low |
| 23 | `anila-studio/scripts/export-openapi.py` 產生 `openapi/studio.openapi.json` | Low |
| 24 | `ANILALM/scripts/gen-studio-types.sh` + npm script `gen:studio-types` | Low |
| 25 | 跑 codegen 並 check-in `src/api/studio-types.gen.ts` | Low |
| 26 | `src/api/client.ts` 加 `STUDIO_BASE_URL` | Low |
| 27 | 重寫 `src/api/studio.ts` 用 codegen types + 新 base URL | Medium |
| 28 | `vite.config.ts` 加 `/api/studio` proxy | Low |
| 29 | `.env.example` 加 `VITE_STUDIO_BASE_URL` | Low |
| 30 | `npm run build / lint / test` 全綠 | Low |

**PR #6**: ANILALM 前端切到 anila-studio。Acceptance: build/lint 綠 + user 從 browser 確認 Network 走到 anila-studio:8100。

## Phase 6 — E2E 驗收(v2 順序對調,原為 Phase 7)

### 為何順序對調(codex finding #7)

原 plan Phase 6 是 csp 刪檔、Phase 7 是 E2E。**「刪了再測」是錯的**:E2E 抓到 bug 時 csp 端 studio 已不可用,沒得 fallback。

v2 順序: **Phase 5 (前端切換) → Phase 6 (E2E 驗收) → Phase 7 (csp 清理斷尾)**。Phase 6 過了 csp 殘段才刪。

### Tasks

| Task | 動作 | Risk |
|---|---|---|
| 37 | user 從 browser 跑完整 deck 生成 + 觀察 DevTools Network | Medium |
| 38 | 我 read-only 撈 docker log 並客觀呈現 | Low |
| 39 | 條件式 sub-PR 修 bug(若有) | — |

若 Phase 6 E2E 失敗:revert Phase 5 PR (PR #6) → ANILALM 打回 csp(可運作,因 csp 殘段未刪)
若 Phase 6 E2E 通過:進 Phase 7

**PR #7(條件式 bug 修)**: 僅當 E2E 抓到 bug 才開

## Phase 7 — csp 清理(斷尾,v2 重編)

| Task | 動作 | Risk |
|---|---|---|
| 31 | **user 確認** Phase 6 E2E 通過 + Phase 5 deploy 後 24-48hr csp `/api/studio/*` 流量為 0 | **High if skipped** |
| 32 | csp `router.py` 移除 studio_router 註冊 | Low |
| 33 | `git rm` 10 個 csp studio service 檔 + 雙重 grep | **High** |
| 34 | `git rm` 17 個 csp test 檔 | Low |
| 35 | 跑 csp full pytest 確認 baseline 不變 | Medium |
| 36 | 清 csp `config.py` 的 FLUX env 變數 | Low |

**PR #8**: 斷尾清理。Acceptance: csp 全套(扣 studio)test 綠 + grep 程式碼為空。

## Phase 8 — Docs

| Task | 動作 | Risk |
|---|---|---|
| 40 | `anila-studio/README.md`(zh-TW + EN) | Low |
| 41 | 根 README 加 anila-studio 段 | Low |
| 42 | csp README 註明 studio 已移出 | Low |
| 43 | ANILALM README 註明 studio.ts 走獨立 service | Low |
| 44 | 加 `docs/architecture/anila-studio-extraction.md` | Low |

**PR #9**: 文件 PR。

---

## Phase 1 v2 補的 task(JWKS)

| Task | 動作 |
|---|---|
| 7d | csp `app/utils/security.py` 從 HS256 改 RS256;`create_access_token`/`create_refresh_token` 用 private key |
| 7e | csp 新增 `GET /.well-known/jwks.json`(public key in JWKS format) |
| 7f | csp key pair 生成 + 部署手冊(本機 dev / docker / Vault) |
| 7g | `test_jwks_endpoint.py` + JWT sign/verify round-trip test |

## Phase 2 v2 補的 task(JWKS client + revocation refinement)

| Task | 動作 |
|---|---|
| 11c | `anila-studio/app/services/jwks_client.py`(fetch + cache + 1hr refresh + key rotation) |
| 11d | `tests/test_jwks_client.py`(mock httpx + 過期 key 失效) |
| 11e | `revocation_cache.py` 改用 `cachetools.TTLCache`,加過期測試 |
| 11f | health endpoint 加 readiness gate(JWKS + revocation cold-start sync 都 ready 才回 200) |

## Phase 3 v2:studio.py 改造 A→I 邊界明定

| Sub-commit | 內容 | Verify |
|---|---|---|
| A | 刪 `from sqlalchemy.orm import Session` + `from app.database import get_db, SessionLocal` + 移除所有 `db: Session` 參數簽名 | `python -m compileall anila-studio/app/api/studio.py` |
| B | 刪 `from app.models.{user,ingestion,model_registry}` + 移除 `_load_user_from_payload`/`db.query` 使用 | compileall |
| C | 刪 `from app.services.{ingestion_pool, proxy_service}` + 標記後續要改的呼叫點為 `TODO` placeholder | compileall + grep "TODO" 數量已知 |
| D | 加 `from app.clients.csp_client import (get_collection, search_chunks, search_images, fetch_image_blob, proxy_chat_completions)` + `from app.auth import get_current_user_identity` | compileall |
| E | `_retrieve_chunks()`: 替換 SQL → `await search_chunks(...)`,簽名改 `(bearer, collection_id, query)` | unit test stub 過 |
| F | `_retrieve_images()`: 替換 SQL → `await search_images(...)` | unit test stub 過 |
| G | `_call_llm_chat()`: 替換 `proxy_request` → `await proxy_chat_completions(...)` | unit test stub 過 |
| H | `_hydrate_images()`: 替換 file open → `await fetch_image_blob(...)`,加 `bearer` 參數一路傳 | unit test stub 過 |
| I | `_run_pipeline()` + endpoint handler:全部刪 `db: Session = Depends(get_db)`,改 identity + bearer 傳遞 | uvicorn 起得來 + `/health` 200 |

每 sub-commit 必跑 `python -m compileall` + import smoke;沒過不能進下一步。

## Phase 4 v2:PR #4 acceptance 強化(原為 PR #5 acceptance 強化)

PR #4 必綠的 3 條 wiring smoke:
1. **FLUX wiring**: mock `httpx` 到 flux2-dev,POST `/studio/slides/jobs` 觸發後 → 確認 anila-studio 真的呼叫 `${FLUX_BACKEND_URL}/v1/chat/completions`(用 respx 攔截)
2. **RAG wiring**: 同上,確認 anila-studio 呼叫 `${CSP_BASE_URL}/api/ingestion/collections/{id}/search`
3. **PPTX wiring**: 確認 anila-studio 呼叫 `${RENDERER_BASE_URL}/render`

## Phase 5 v2:CI drift detection

新 task:
- `ANILALM/.github/workflows/codegen-drift.yml` 或併入既有 CI:
  - `npm run gen:studio-types`
  - `git diff --exit-code src/api/studio-types.gen.ts` 不同則 fail
- 確保 reviewer 看到 type drift,而非「checked-in 跟 backend OpenAPI 不一致悄悄上 prod」

## Phase 5 v2:自動化前端整合測試

新 task:
- `ANILALM/tests/integration/studio-api-base-url.test.ts`:
  - 用 vitest + msw 攔截 fetch
  - 觸發任一 studio API call(`createJob`)
  - assert intercepted URL pattern matches `/api/studio/` 且 host 是 `STUDIO_BASE_URL`
- 取代「user 手動看 DevTools Network」這個不可自動化的 acceptance criteria

---

# Risk Register

| ID | 風險 | 嚴重度 | 緩解 |
|---|---|---|---|
| R1 | `SECRET_KEY` 同步問題 | High | env 同 source,rotate runbook 寫雙邊一起 |
| R2 | JWT revocation 跨 service 不即時 | High | **Plan 改用 (c) Redis pub/sub** |
| R3 | image blob 大流量 → csp 拖慢 | Medium | streaming response + httpx pool;deck 限 30 張 |
| R4 | docker network 名稱對不齊 | High | 用 user 既有 `anila` network |
| R5 | FLUX cache 從 csp 搬到 anila-studio 失效 | Low | 接受,reset 一次 |
| R6 | OpenAPI codegen 比手寫嚴格 | Medium | Phase 5 必跑 build 抓 |
| R7 | 大 PR 看不過來 | Medium | Phase 3 拆 sub-commit、PR ≥ 8 個 |
| R8 | test 改紅燈時不小心降低 assertion | Medium | code-reviewer + baseline 對比 |
| R9 | csp 殘留 import 漏抓 | High | grep 雙保險 + full pytest |
| R10 | LLM proxy 多一跳延遲 | Medium | 同 docker network <5ms,可接受 |
| R11 | Phase 7 unhappy path bug | Medium | 留 PR #8 buffer |
| R12 | feature branch 走太久,main 沖突 | Medium | 每完 PR rebase main 進 feature |
| **R13(新)** | Redis pub/sub 漏訊息(network blip / restart 期間) | Medium | cold-start 從 csp `/revocations?since=<ts>` 取**近 30 天**撤銷(v2 對齊 cache TTL);event payload 含 `version`,subscriber 不會降冪覆蓋 |
| ~~R14~~ | ~~Redis 失聯降到 TTL-only~~ | ~~Medium~~ | **v2 fail-closed**:Redis 失聯時 revocation-required endpoint 一律 503,不允許 TTL fallback,違反安全屬性 |
| **R15(v2 新)** | JWKS cutover 一次性踢所有 user 重登 | Medium | 排程低使用時段;發 24hr 公告;Phase 1 deploy 後等所有 user 重登再 Phase 2 |
| **R16(v2 新)** | JWKS public key fetch 失敗 → anila-studio 無法驗 JWT | High | anila-studio 啟動時 fetch JWKS 失敗 → health 503;運行中 fetch fail 用 last-good key 撐 1hr;1hr 仍失敗則 503 traffic |
| **R17(v2 新)** | RSA private key leak(從 csp container exfiltrate) | High | private key mount 為 secret volume / non-world-readable;production 走 Vault;不寫 log;rotation 流程文件化 |
| **R18(v2 新)** | E2E 在斷尾前過了,但 30 天後出 long-tail bug | Low | Phase 8 完成後額外 30 天觀察期 + csp 端保留 git history(隨時 cherry-pick 還原可能) |

---

# Test Strategy

| Phase | 必綠 test |
|---|---|
| 0 | csp baseline |
| 1 | csp baseline + 4 個新 contract test + `test_token_revoke_publish` |
| 2 | + anila-studio `test_auth` + `test_revocation_cache` + `test_csp_client` |
| 3 | + anila-studio uvicorn boot OK |
| 4 | + 17 搬入 + 2 contract + 1 smoke,共 ~22 個 test 全綠 |
| 5 | + ANILALM build / lint / test |
| 6 | csp 扣 studio 後仍綠 + anila-studio full + ANILALM full |
| 7 | E2E by user |
| 8 | docs link valid |

---

# 回退計畫(per phase)

| Phase 失敗 | 回退 |
|---|---|
| 1 | revert PR #2;Phase 2 暫停 |
| 2 | revert PR #3;Phase 3 暫停 |
| 3 | 用 sub-commit 拆 revert,不用整 phase 退 |
| 4 | 不 merge PR #5,回去重看 csp test 邏輯 |
| 5 | revert PR #6;前端打回 csp(可運作) |
| 6 | revert PR #7;檔回來,雙 service running 也 OK |
| 7 | revert PR #6 把流量打回 csp |
| 8 | 直接 revert |

整體安全性質: **PR #6 (Phase 5) merge 之前**,csp 仍是 studio 唯一 owner,任何時刻 revert 都不破生產。**PR #7 是不可逆斷尾**,前提是 user 觀察流量為 0。

---

# 期程估算

- Phase 0:0.5 天
- Phase 1:**4 天**(2 base + revocation +1 + **JWKS +1**)
- Phase 2:**3 天**(1 base + revocation_cache +1 + **JWKS client +1**)
- Phase 3:2-3 天
- Phase 4:2-3 天(含 PR #4 acceptance 強化的 wiring smoke)
- Phase 5:1-2 天
- **Phase 6(E2E,順序對調)**:0.5-1 天(user 跑)
- **Phase 7(斷尾,順序對調)**:1 天
- Phase 8:0.5 天

**Total ~14.5-17.5 工作天 (3-3.5 週)** — v2 比 v1 多 2-3 天(JWKS 升級主因)

# 檔案動量

- 新建:~25 檔(anila-studio 全部)
- 移動:27 檔(10 service + 17 test)
- 改:~10 檔(csp endpoint / ANILALM / compose)

**Total ~62 檔動量**(原估 30 檔嚴重低估,但多數為機械式移動)

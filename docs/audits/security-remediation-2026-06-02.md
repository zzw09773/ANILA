# 資安修正進度 — 2026-06-02

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——稽核報告,保留當時的判定與依據,不代表現況。現行狀態與執行順序見 `PLAN.md`。

對應 `anila-full-audit-2026-06-02.md` 的資安發現。驗證層級:`py_compile` 語法 + 邏輯審查(依「不動 running container」原則,完整整合測試請在測試環境/下次後端啟動時驗)。

## ✅ 已修正(working tree,尚未 commit)

| 項目 | 嚴重度 | 改動 | 驗證 |
|---|---|---|---|
| **S1 secret guard 旁路 + admin 預設密碼** | HIGH | `.env`:`ANILA_ALLOW_DEV_SECRET=1→0`、新增 160-bit `ADMIN_PASSWORD` | 複現 startup_security 檢查:4 個守門 secret 全 PASS,flag=0 不會擋啟動 ✅ |
| **S2 ingestion audit log 不落地** | HIGH | 10 個 audit 站點(collections×3 / credentials×3 / eval_runs×1 / documents×3)加 `commit=True` | py_compile ✅;已逐站確認皆「主操作 commit 後→audit→return」模式,加 commit 安全 |
| **DB 驅動錯誤外洩** | LOW(conf high) | `collections.py` 不再回 `e.orig`;改 server-side log + 通用訊息,status 500→409 | py_compile ✅ |
| **CORS wildcard fallback** | LOW | `main.py`:移除 `_allowed_origins or ["*"]` footgun,空值改為拒絕跨域(同源 SPA 不受影響) | py_compile ✅ |
| **無入向 Host header 驗證** | MEDIUM | `config.py` 新增 `ALLOWED_HOSTS`(預設 `*` 不破壞)、`main.py` 條件加 `TrustedHostMiddleware`;operator 在 prod pin 真實 host 即生效 | py_compile ✅ |

### S1 重要後續(user 行動)
- `auto_seed.py:98` 是 `if not admin` → 改 .env **不會**更新既有 admin id=1。**既有 admin 密碼需你從 UI/CLI 自行重設**(新 .env 密碼僅用於全新部署 seed + 通過 guard)。
- 新 ADMIN_PASSWORD 值另外私下提供(見對話)。

## ⏸️ 延後(無法安全一次做完,附原因與計畫)

### ingestion_images 無 RLS(MEDIUM → 實為 defense-in-depth)— ✅ code 完成(2026-06-03,部署待 user)
- **修正 finding 框架**:image search endpoint **有** `_require_collection_access`(`search.py:363`),隔離是「endpoint 授權 + WHERE collection_id」雙層,非 finding 說的「僅 WHERE」。RLS 是 defense-in-depth,非補開放漏洞。
- **已做**(分支 `security/ingestion-images-rls`,跨三路徑):
  1. **migration 0037**:ENABLE+FORCE RLS + `FOR ALL USING(collection_id = anila.collection_id GUC)` policy(鏡像 0019;INSERT 沿用 USING 當 WITH CHECK)+ 窄 `SECURITY DEFINER` resolver `ingestion_image_collection_id(id)`(只回 collection_id)解 blob by-PK 雞生蛋。
  2. **search.py image search**:txn + `SET LOCAL anila.collection_id`(原 WHERE 保留為 belt-and-suspenders)。
  3. **ingestion-worker `_persist_images`**:txn + SET LOCAL + 逐列 savepoint(保留 best-effort continue-on-error)。
  4. **image_blob.py by-PK**:SECURITY DEFINER resolver 取 collection_id → `_require_collection_access` 把關 → set GUC 後在 RLS 下讀;dialect-guard(RLS 僅 Postgres,SQLite 測試走直讀)。
- **已驗證**(對 dev Postgres):`test_ingestion_images_rls_pg.py`(Postgres-gated,SQLite skip)—— GUC scoping、跨 collection INSERT 被 42501 擋(RLS WITH CHECK 非 23514)、resolver 繞 FORCE RLS。既有 SQLite 契約測試保持綠。
- **部署待 user**:RLS 是 defense-in-depth;部署前請對 staging Postgres 跑完整 image search/blob/ingest 整合套件(SQLite 無法驗 RLS)。

### SSRF guard 僅 create/update 時驗、非呼叫時驗(LOW/MED, TOCTOU)— ✅ 已修(2026-06-03)
- `url_guard` 在 endpoint_url create/update 時驗證,儲存後到呼叫間可被 DNS-rebinding。
- **已做**:`proxy_service._guard_outbound()` 在 proxy_request / proxy_stream 呼叫前重驗 resolved target_url;proxy.py agent 直轉/answer stream、health_checker、agents/models 健康探測同樣呼叫時重驗。trusted hosts 走 fast-path(無 DNS),hot path 成本近零。見 commit "re-validate outbound URLs at call time"。

### ANILA_TRUSTED_HOSTS 含 host.docker.internal(MEDIUM, 設定)
- `docker-compose.yml:56` 預設含 `host.docker.internal`,擴大 SSRF 出向允許面。
- **建議(非程式)**:prod 移除該預設;但需先確認沒有 agent/model endpoint 依賴 host 服務。屬部署設定,建議你評估後調整。

### Swagger /docs + /openapi.json 無 auth(INFO)— ✅ 已修(2026-06-03)
- `main.py` 自訂 `/docs` + 預設 `/openapi.json` 無認證,洩漏 API surface。
- **已做**:加 `ENABLE_API_DOCS` 設定(預設 False = secure-by-default);prod 關閉 `openapi_url` 與自訂 `/docs` 路由,docker-compose-dev.yml 設 `ENABLE_API_DOCS=true` 保留 dev docs。見 commit "gate Swagger /docs + /openapi.json"。

### audit log 系統性遺失(S2 延伸)— ✅ 已解決(2026-06-03 逐站複核)
- 原估「~55 站待補」為 doc 撰寫時快照。dev-public 的 #110 merge 後已大幅收斂。
- **複核現況**(security/p2-deferred-117，off dev-public):csp backend 共 **77 個 `log_audit_event` 呼叫,64 帶 commit=True**,僅 12 個 commit=False(audit_service.py:7 為定義不計)。
- 這 12 個 commit=False **全部已原子落地** —— 每個呼叫端在 log 後都有 `db.commit()`:
  - `alerts.py` 77/101(ack/resolve)→ commit 85/109
  - `service_clients.py` 176/217(create/rotate)→ commit 186/226；rotate/revoke 經 service 由 endpoint commit 251/273
  - `agent_credential_service.py` 8 站(issue_bootstrap/consume_bootstrap×2/issue_static/rotate_agent/rotate_service_client/revoke_agent/revoke_service_client)→ 服務層 `db.flush()`,呼叫端 `agents.py`/`service_clients.py` endpoint commit(821/858/893/949/976、251/273)
  - `auth.py` login/安全審計皆已 `commit=True`(137/160/183/204/287/329/379)——doc 原文「auth.py:130 是延後 commit 模式」已過時。
- **結論:不需補 commit**。這 12 站是「服務層 flush + 端點 commit」的**正確原子模式**;若硬加 commit=True 反會破壞 atomicity(在服務層提前 commit credential 寫入,與端點交易拆開)。S2 audit durability 視為已達成。

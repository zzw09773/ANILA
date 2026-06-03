# 資安修正進度 — 2026-06-02

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

### SSRF guard 僅 create/update 時驗、非呼叫時驗(LOW/MED, TOCTOU)
- `url_guard` 在 endpoint_url create/update 時驗證,儲存後到呼叫間可被 DNS-rebinding。
- **計畫**:在 proxy dispatch 呼叫前對 resolved IP 再驗一次;需注意別影響正常 agent dispatch 效能,需測。

### ANILA_TRUSTED_HOSTS 含 host.docker.internal(MEDIUM, 設定)
- `docker-compose.yml:56` 預設含 `host.docker.internal`,擴大 SSRF 出向允許面。
- **建議(非程式)**:prod 移除該預設;但需先確認沒有 agent/model endpoint 依賴 host 服務。屬部署設定,建議你評估後調整。

### Swagger /docs + /openapi.json 無 auth(INFO)
- `main.py:244` 自訂 `/docs` + 預設 `/openapi.json` 無認證,洩漏 API surface。
- **計畫**:加 `ENABLE_API_DOCS` 設定(prod 設 False → `openapi_url=None` 連帶關 docs),或將 `/docs` 掛 auth 依賴。INFO 級,低優先。

### audit log 系統性遺失(S2 延伸)
- 全 codebase 74 個 `log_audit_event` 僅 9 帶 commit=True;本次只修 ingestion 10 站。
- **為何不全改預設**:`auth.py:130` 等是「audit→後續才 commit」模式,翻全域預設會提前 commit 半成品交易。其餘 ~55 站需逐站分析其交易邊界。
- **計畫**:逐站歸類(post-commit→return = 加 commit=True;mid-txn = 不動),或重構 audit 走獨立 session 確保durability。

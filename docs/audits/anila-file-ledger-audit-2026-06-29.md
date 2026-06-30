# ANILA 全檔案 Ledger 審查與 Roadmap

日期：2026-06-29  
分支：`prod-public-passwd`  
範圍：目前 Git tracked files，共 1102 個檔案。未追蹤與 gitignored 檔案（例如 `.env`、實際 secret、runtime volume）未讀取也未輸出，避免把敏感資料寫進審查報告。

## 產出

- 逐檔 ledger：[`docs/audits/anila-file-ledger-2026-06-29.csv`](anila-file-ledger-2026-06-29.csv)
- 本報告：`docs/audits/anila-file-ledger-audit-2026-06-29.md`

Ledger 欄位：

- `path`：tracked file 路徑
- `service`：服務/子專案歸屬
- `class`：程式碼、部署設定、migration、文件、generated、binary asset、archive 等分類
- `review_level`：`manual-deep`、`manual-targeted`、`automated-ledger`、`context-review`、`deferred-generated-or-asset`
- `risk_tags`：auth/security、deploy、RAG/RLS、artifact、browser-token、giant-file 等標籤
- `notes`：本次審查處理方式或 deferred 理由

## Coverage 摘要

逐檔 ledger 已覆蓋 1102 個 tracked files：

| review_level | 檔案數 | 意義 |
|---|---:|---|
| `manual-deep` | 19 | 高風險入口人工讀過重點段落並納入 finding/roadmap。 |
| `manual-targeted` | 170 | 由部署、安全、schema、巨型檔或路徑規則標記；修改時必須再做 focused review。 |
| `automated-ledger` | 727 | 進入逐檔清冊並跑 heuristic scan，本次未發現檔案級人工 finding。 |
| `context-review` | 93 | 文件/架構脈絡，用於理解 repo，不是 runtime surface。 |
| `deferred-generated-or-asset` | 93 | lockfile、vendor XSD、binary artifact、backup/archive；已盤點，不做逐行人工審查。 |

服務分布：

| service | 檔案數 |
|---|---:|
| `anila-core` | 202 |
| `csp-backend` | 197 |
| `anila-agent` | 113 |
| `anila-studio` | 100 |
| `csp-frontend` | 87 |
| `pptx-renderer` | 70 |
| `docs` | 69 |
| `anilalm` | 68 |
| `anila-ui` | 52 |
| `anila-ui-archive-or-doc` | 50 |
| `ingestion-worker` | 24 |
| 其他 root/infra/models/router | 70 |

## 人工深讀與定點閱讀檔案

下表包含 CSV 中的 `manual-deep` 檔案，以及本次為了確認 finding 而額外定點閱讀的 `manual-targeted` 檔案。

| 檔案 | 重點 |
|---|---|
| `docker-compose.yml` | prod stack、secret fallback、trusted hosts、n8n/GitLab services |
| `docker-compose-dev.yml` | dev stack 與 dev-only 放寬設定 |
| `.env.example` | dev secret/SSRF 範本值 |
| `scripts/deploy-prod.sh` | preflight 對 required env / placeholder 的檢查 |
| `scripts/phase1-e2e.sh` | 過時 code-server 驗證與預設帳密 |
| `myCSPPlatform/docker/nginx.conf` | 外部 ingress、CSP header、n8n/GitLab routes |
| `myCSPPlatform/backend/app/main.py` | startup、trusted host backfill/provider |
| `myCSPPlatform/backend/app/config.py` | CSP 設定與 fallback |
| `myCSPPlatform/backend/app/services/startup_security.py` | dev-default secret gate |
| `myCSPPlatform/backend/app/api/auth.py` | cookie/JWT/refresh token flow |
| `myCSPPlatform/backend/app/services/auth_service.py` | service token verify 與 legacy fallback |
| `anila-core-router/main.py` | Router service token state file/bootstrap |
| `anila-core/src/anila_core/api/router_server.py` | Router streaming/dispatch/resume giant module |
| `anila-core/src/anila_core/security/url_guard.py` | SSRF guard/trusted host bypass |
| `ingestion-worker/src/ingestion_worker/main.py` | worker entrypoint |
| `ingestion-worker/src/ingestion_worker/settings.py` | DB/RLS/embedding 設定契約 |
| `anila-studio/app/main.py` | Studio app entrypoint |
| `ANILA_UI/anila-ui/src/runtime/api.js` / `auth.jsx` | cookie-only runtime auth 參考實作 |
| `ANILALM/src/store/auth.ts` / `api/client.ts` | ANILALM token persistence 與 Bearer flow |
| `ANILALM/pptx-skill/server.js` | pptx renderer、LibreOffice/pdftoppm、JSZip boundary |

## Findings

### F1. `prod-public-passwd` 仍保留 n8n / GitLab 對外路由與服務

嚴重度：High（對外攻擊面 / production hardening）  
證據：

- `docker-compose.yml:82-83` 的 `AUTO_REGISTER_LINKS` 仍註冊 `/n8n`、`/gitlab`，且 `is_public:true`、`required_roles:["developer"]`。
- `docker-compose.yml:355-382` 仍定義 `n8n` service。
- `docker-compose.yml:389-421` 仍定義 `gitlab` service。
- `myCSPPlatform/docker/nginx.conf:248-269` 443 server block 代理 `/n8n`。
- `myCSPPlatform/docker/nginx.conf:320-334` 443 server block 代理 `/gitlab/`。
- README 已明確標註此分支「移除 code-server，但 n8n / GitLab 仍保留」。

判斷：這不是新引入 bug，而是已知未完成 hardening。外網 prod 若追求最小對外面，n8n / GitLab 不應預設掛在同一個 public ingress 下；n8n code node、GitLab Omnibus 都是高價值、高複雜度服務。

建議：

- 對外部署若不需要：移除 compose service、nginx location、`AUTO_REGISTER_LINKS`。
- 若需要保留：加上明確 auth gate（例如 nginx `auth_request`/SSO）、rate limit、審計、備份/restore runbook、獨立 exposure 決策，並做版本與 CVE 維護節奏。

### F2. Root prod compose 與 `.env.example` 仍偏 dev-friendly，不夠 secure-by-default

嚴重度：High（誤部署風險；已有部分防線）  
證據：

- `docker-compose.yml:47` `SECRET_KEY` fallback 為 `dev-secret-key-change-in-prod`。
- `docker-compose.yml:49` `ANILA_ALLOW_DEV_SECRET` fallback 為 `1`。
- `docker-compose.yml:65` `CSP_SERVICE_TOKEN` fallback 為 `dev-service-token`。
- `docker-compose.yml:74`、`:151`、`:165` 使用 `sk-internal-worker-changeme` fallback。
- `.env.example:11` 設 `ANILA_ALLOW_DEV_SECRET=1`，`:16-17` 開 SSRF dev opt-in，`:21-26` 放 dev secret/API key。
- `scripts/deploy-prod.sh:108-130` 已有 preflight，會擋缺少或含 `changeme|placeholder|example` 的必要 secret。
- `startup_security.py` 也會在 production 未明確 opt-in 時拒絕 dev default。

判斷：程式層不是完全裸奔，已有 `deploy-prod.sh` 與 `startup_security` 兩道防線；真正問題是 root `docker-compose.yml` 本身仍很容易被當成 production command 直接使用，且預設值會把 local/dev 心智帶進 prod。

建議：

- 將 root prod compose 改成 secure-default：必要 secret 使用 `${VAR:?required}`，不要在 prod stack 給 dev fallback。
- 拆出 `.env.dev.example` 與 `.env.prod.example`；prod 範本只放空值/placeholder 說明，不放可啟動的 dev secret。
- `docker-compose-dev.yml` 保留 dev fallback；不要讓 root stack 同時承擔 dev 與 prod。

### F3. `host.docker.internal` 在 trusted hosts 預設清單中，會擴大 SSRF 放行面

嚴重度：High/Medium（依部署是否允許 user-supplied endpoint 而定）  
證據：

- `docker-compose.yml:56` 預設 `ANILA_TRUSTED_HOSTS` 包含 `host.docker.internal`。
- `docker-compose.yml:109` `extra_hosts` 把 `host.docker.internal` 指到 host gateway。
- `anila-core/src/anila_core/security/url_guard.py:248-281` 先檢查 scheme，再讓 trusted host 跳過後續 deny list、internal suffix、single-label、private/loopback IP、DNS resolution 檢查。

判斷：trusted host 是必要能力，否則模型 stack 的 single-label service name 無法註冊；但把 host gateway 放進 prod default，會讓任何可控 endpoint 設定的路徑更接近 host network。

建議：

- prod default 移除 `host.docker.internal`；只在 dev 或明確 on-prem 例外中 opt-in。
- trusted host provider 應維持 owner/admin-only，並保留 audit log。
- 評估 `_DENY_HOSTS` 與明確 unsafe IP 類檢查是否應在 trusted host 之前先做一次不可繞過檢查。

### F4. ANILALM 仍持久化 browser token，尚未收斂到 cookie-only

嚴重度：Medium/High（XSS 後果擴大）  
證據：

- `ANILALM/src/store/auth.ts:42`、`:61` 將 `access_token`、`refresh_token` 存進 Zustand state。
- `ANILALM/src/store/auth.ts:105` 使用 `localStorage` persist。
- `ANILALM/src/api/client.ts:68` 對 request 注入 `Authorization: Bearer`。
- `ANILALM/src/api/client.ts:107` refresh 後重送 Bearer。
- 對照組：`ANILA_UI/anila-ui/src/runtime/api.js:1-16`、`auth.jsx:11-15` 已明確採 cookie-only，SPA 不再持有 JWT/API key。

判斷：ANILALM 已有 cookie-first hydrate 設計，但仍維持 legacy localStorage/Bearer 路徑。這是已知安全債，應排入近期待辦。

建議：

- Wave 1：停止 persist `refreshToken`；只保留 user profile/UI state。
- Wave 2：改成 cookie-only `getMe` / `refresh`；Bearer 僅保留給 SDK/curl 或顯式 legacy mode。
- Wave 3：後端 refresh endpoint 對 browser path 僅接受 httpOnly cookie；JSON body refresh token 改為 SDK-only contract。

### F5. Service-token cutover 與 Router bootstrap 還有 legacy/pass-through 設計

嚴重度：Medium  
證據：

- `myCSPPlatform/backend/app/services/auth_service.py:168-226` 驗 service token 順序為 DB-backed `service_clients` / `agent_credentials`，最後仍接受 `settings.CSP_SERVICE_TOKEN` legacy env fallback；命中會寫 audit event。
- `anila-core-router/main.py:136-170` `_self_bootstrap` 註解明確說目前把 `CSP_BOOTSTRAP_TOKEN` 當作 long-lived token pass-through 寫入 state file，真正 one-shot bootstrap endpoint 尚未完成。

判斷：目前是可遷移設計，不是立即破口；但只要 legacy env token 還能驗，服務呼叫歸因與輪換都不完整。

建議：

- 定義 cutover SLO：連續一個 release window 沒有 `service_token_legacy_env_used` 後移除 fallback。
- 補 CSP service client one-shot bootstrap endpoint：TTL、consume-once、audit、least privilege。
- Router 啟動不再接受長期 bootstrap pass-through；state file token 必須可輪換、可撤銷。

### F6. Studio / artifact jobs 多數仍是 in-memory manager，restart / multi-worker / orphan artifact 邊界明確但尚未產品化

嚴重度：Medium  
證據：

- `anila-studio/app/services/studio_job_service.py:1-26` 設計明確採 in-memory job manager；restart 後 job 404，由前端標成 failed。
- `anila-studio/app/services/infographic_job_service.py:1-17` 說明 disk artifact 可能 orphan，disk GC out of scope for MVP。
- `datatable_job_service.py` 已有 artifact unlink，代表部分 artifact family 已往正確方向前進。

判斷：MVP 說明清楚，但 production 若要多 worker、restart resilience、長任務、artifact retention，需要統一 persistence/GC contract。

建議：

- 短期：在 deploy docs 明確標註 single-worker contract 與 restart 後 job 失效行為。
- 中期：以 Redis/Postgres 存 job state；artifact 進統一 artifact store，加入 TTL/GC。
- 加 metrics：job count、in-flight、artifact bytes、eviction、orphan cleanup。

### F7. 幾個巨型模組提高維護風險

嚴重度：Medium  
證據：

- `anila-core/src/anila_core/api/router_server.py` 2594 行，涵蓋 dispatch parsing、SSE streaming、session/resume、agent stream passthrough、memory/recompose 等多個責任。
- `ANILALM/pptx-skill/server.js` 1802 行，涵蓋 pptx build、LibreOffice/pdftoppm screenshots、JSZip QA、layout logic。
- `ANILA_UI/anila-ui/src/app.jsx` 2685 行、`chat.jsx` 1980 行。
- `ingestion-worker/src/ingestion_worker/handlers.py` 1045 行。

判斷：不建議一次重構；但這些檔案是未來修改風險集中點。尤其 Router streaming/dispatch/resume 是核心 user path，拆分前必須先有 characterization tests。

建議：

- Router 先拆非行為核心：dispatch parser、event normalization、session helpers、agent stream adapter。
- pptx renderer 拆 schema validation、render route、screenshot route、QA route、layout primitives。
- 前端大檔拆 feature slices，但先避免改 UI 行為。

### F8. nginx CSP 仍含 `unsafe-inline` / `unsafe-eval`

嚴重度：Medium  
證據：

- `myCSPPlatform/docker/nginx.conf:92` 與 `:433` 的 `Content-Security-Policy` 允許 `script-src 'unsafe-inline' 'unsafe-eval'`、`style-src 'unsafe-inline'`。

判斷：這會放大 XSS 後果；但 React/Vue/markdown/mermaid/Studio artifact 可能依賴這些行為，不能直接刪。

建議：

- 先加 `Content-Security-Policy-Report-Only` 收集違規。
- 分 app 收斂：CSP admin、ANILA_UI、ANILALM、Studio artifact 可用不同 policy。
- 逐步以 nonce/hash、移除 runtime eval、清理 inline style/script。

### F9. `scripts/phase1-e2e.sh` 已過時，不能當 prod 驗證依據

嚴重度：Medium/Low  
證據：

- `scripts/phase1-e2e.sh:26-28` 使用 `admin changeme`、`smoke-user changeme`、`1140921 changeme`。
- `scripts/phase1-e2e.sh:71-86` 仍測 `/codeserver/` WebSocket probe；此分支 README 已說 code-server 已移除。

建議：

- 移到 `scripts/legacy/` 或改名為 `phase1-e2e.legacy.sh`。
- 新增 `scripts/prod-public-smoke.sh`：驗 `/api/health`、login/cookie/CSRF、`/v1/chat/completions`、Router、ANILALM、ANILA_UI、n8n/GitLab 是否依部署策略存在或不存在。

### F10. Repo 內有 archive/generated/binary/reference 檔案，需明確 ownership

嚴重度：Low  
證據：

- Ledger 標出 93 個 `deferred-generated-or-asset`：lockfile、Office XSD schema、PDF/PPTX、backup archive。
- `ANILA_UI/scraps/anila-ui-backup-2026-04-21/**`、`ANILA_UI/ANILA_templete/**` 等路徑容易混淆 runtime source 與歷史素材。
- `myCSPPlatform/backend/Dockerfile` 依專案 AGENTS 屬 legacy/dead，正式 image 是 `myCSPPlatform/docker/Dockerfile`。

建議：

- 將 archive/reference 路徑納入 README/AGENTS 的「不可部署」清單。
- 若檔案只是歷史素材，考慮搬到 `docs/archive/` 或 release artifact，不放 runtime tree。
- vendor schema 保留但加來源/版本說明，避免被誤改。

## Positive Controls

這些區域本次審查判斷為方向正確，後續主要是驗證與收斂：

- CSP startup security：`startup_security` 已能在 production 模式擋 dev default secret。
- Prod deploy preflight：`scripts/deploy-prod.sh` 已檢查 required env 與 `changeme|placeholder|example`。
- RLS/pgvector：`CollectionScopedPgVectorStore` 驗 `collection_id`，在 DB transaction 內 `SET LOCAL anila.collection_id`；RLS integration tests 覆蓋 raw connection without GUC、`csp_app` 無 BYPASSRLS 等情境。
- SSRF：model/agent/proxy/memory 多處有 call-time guard，已比「只在註冊時驗一次」安全。
- ANILA_UI / CSP frontend：主要 auth flow 已往 cookie-only + CSRF echo 收斂，可作為 ANILALM 遷移參考。

## Roadmap

### 0-2 週：Production hardening 與驗證基線

1. 決定 `prod-public-passwd` 是否保留 n8n/GitLab。若不保留，移除 compose service、nginx route、platform link；若保留，補 auth gate 與維運 runbook。
2. 拆 `.env.dev.example` / `.env.prod.example`，root prod compose 改 secure-default。
3. prod default 移除 `host.docker.internal` trusted host；保留 dev stack opt-in。
4. 退役或改名 `scripts/phase1-e2e.sh`，新增符合本分支的 prod smoke。
5. 把 RLS smoke、Router chat/dispatch、cookie/CSRF login、Studio artifact 代表性 job 放進 staging/preflight checklist。

### 2-4 週：Auth/token 收斂

1. ANILALM 改 cookie-only，停止 localStorage refresh token。
2. CSP refresh endpoint 分 browser cookie path 與 SDK legacy path；browser route 不再需要 JSON body refresh token。
3. 完成 service client one-shot bootstrap endpoint，Router 移除 bootstrap pass-through。
4. 觀察 `service_token_legacy_env_used` audit，達 cutover SLO 後移除 `CSP_SERVICE_TOKEN` fallback。

### 1-2 個月：Artifact 與核心巨型模組降風險

1. Studio job state 持久化：Redis/Postgres job state + artifact TTL/GC。
2. pptx renderer 加 payload size limit、zip bomb guard、concurrency limit、artifact/tmp cleanup metrics。
3. Router 拆模組前補 characterization tests，再拆 dispatch parser / stream adapter / session helpers。
4. ANILA_UI/ANILALM 大檔按 feature 拆分，不改 UI 行為。

### 2-3 個月：長期平台化

1. n8n/GitLab 若繼續存在，導入 SSO/OIDC、獨立 maintenance window、版本升級節奏與備份演練。
2. 分支同步制度化：`main` SSOT，downstream 只挑選式 port；`prod-intranet-card` 保留 card/SSO fork 熱區。
3. 模型/agent endpoint 註冊增加 deployment profile：dev/on-prem/prod public 的 trusted-host policy 不同。
4. CSP policy 收斂：從 report-only 到 per-app strict CSP。

## 驗證狀態

本次是審查與 roadmap 輸出，未修改 runtime code，未啟動 Docker stack，也未跑完整 unit/integration/e2e 測試。

已執行：

- `git status --short --branch`
- `git ls-files` 產生 1102 檔 ledger
- repo-wide `rg` 掃描：dev secret fallback、n8n/GitLab/code-server、browser token/CSRF、SSRF/trusted hosts、RLS/pgvector、CSP header、service token/bootstrap、artifact subprocess/job manager
- 人工閱讀 high-risk entrypoint 段落並納入 findings

後續若要把 roadmap 轉成實作，第一個 PR 建議只做 `prod-public-passwd` hardening 決策與 prod smoke script，避免和 auth / Router / Studio 重構混在一起。

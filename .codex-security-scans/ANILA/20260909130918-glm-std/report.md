# Security Review: ANILA

## Scope

The scan was configured for the include paths and exclusions listed below.

- Scan mode: repository
- Target kind: git_worktree
- Target ID: anila-main-151fca19
- Revision: 151fca19a41ee35e9347050e6b4833a666397830
- Snapshot digest: codex-security-snapshot/v1:sha256:9d57f3dd9490a689f3ce6f2b8d9501f33f3894f308e8d0bb55982415dc55e055
- Inventory strategy: repository
- Included paths: .
- Excluded paths: node_modules, models/model, data, share, logs, .env, secrets, runtime_logic, myCSPPlatform, node_modules
- Runtime or test status: not recorded

Limitations and exclusions:
- Excluded node_modules: vendored dependencies, audit via lock/deps surface instead
- Excluded models/model: model weights, not code
- Excluded data: runtime data volume
- Excluded share: runtime upload volume
- Excluded logs: runtime logs
- Excluded .env: secret-bearing env, never read
- Excluded secrets: secret keys, never read
- Excluded runtime_logic: reference-only gitignored tree (README tracked only)
- Excluded myCSPPlatform: legacy reference tree, not shipped runtime

### Scan Summary

| Field | Value |
| --- | --- |
| Scan outcome | completed |
| Reportable findings | 3 |
| Severity mix | medium: 1, low: 2 |
| Confidence mix | high: 3 |
| Coverage | partial |
| Validation mode | not recorded |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

No explicit canonical threat-model summary was recorded.

## Findings

| Finding | Severity | Confidence | Detailed write-up |
| --- | --- | --- | --- |
| [非 agent 服務呼叫者可指定／覆寫 artifact job 的 owner（合法代辦契約未收斂）](#finding-1) | medium | high | inline below |
| [studio／asr-gateway 經內網明文 HTTP 抓 CSP JWKS（信任錨傳輸未加密）](#finding-2) | low | high | inline below |
| [studio／flux2-dev-agent 執行期消費 image-primary URL 未重驗 SSRF（註冊面有守衛，執行面 TOCTOU 未對齊）](#finding-3) | low | high | inline below |

### Confidence Scale

| Label | Meaning |
| --- | --- |
| high | Direct evidence supports the finding with no material unresolved blocker. |
| medium | Evidence supports a plausible issue, but material runtime or reachability proof remains. |
| low | Evidence is incomplete and the item is retained only for explicit follow-up. |

<a id="finding-1"></a>

### [1] 非 agent 服務呼叫者可指定／覆寫 artifact job 的 owner（合法代辦契約未收斂）

| Field | Value |
| --- | --- |
| Severity | medium |
| Confidence | high |
| Confidence rationale | source-validated control flow on revision 151fca19; no runtime reproduction |
| Category | broken_access_control |
| CWE | CWE-639 |
| Affected lines | services/csp/app/api/artifacts.py:434-441, services/csp/app/modules/artifacts/service.py:83-84, services/csp/app/modules/artifacts/service.py:129-141, services/csp/app/api/artifacts.py:446-448 |

#### Summary

非 agent 服務呼叫者可指定／覆寫 artifact job 的 owner_user_id（合法代辦契約未收斂）

#### Root Cause

register_artifact_job 僅對 agent 呼叫者釘死 owner；service_client／legacy fleet token 走 else 分支，把 payload 的 requester_user_id 原樣交給 resolve_owner，不做存在性或授權檢查；register_job upsert 對既有列無差別覆寫 owner_user_id（同 job_id 可奪取他人 job）。

#### Validation

services.py:434-441 只有 identity.kind==agent 走 _enforce_agent_requester_scope；modules/artifacts/service.py:83-84 requester_user_id 直接回傳不驗；service.py:119-141 register_job 覆寫所有欄位含 owner_user_id；既有測試只鎖 agent 不得指定他人（test_dispatch_callback_auth.py:472-496）。

#### Dataflow

POST /v1/artifact-jobs（X-CSP-Service-Token 或 legacy fleet token）→ payload.requester_user_id → resolve_owner 原樣回傳 → ArtifactJob.owner_user_id；同 job_id 重呼 → register_job 覆寫既有列 owner／status／trace。

#### Reachability

需要有效平台 s2s 憑證（Studio／legacy token）；使用者 JWT 已被 403（artifacts.py:206-209）；屬 P2.1 W2 刻意留下的代辦面，擁有者契約未裁定。

#### Severity

**Medium** — medium impact, high-likelihood for s2s holders; owner-adjudication pending

Additional runtime or deployment evidence could raise or lower this severity.

#### Remediation

契約候選（owner 裁定）：resolve_owner 對查無使用者回 400；upsert 時解析後 owner 與既有列不同視為衝突拒絕（不需 schema 變更）；不要把 agent 釘死邏輯套到 Studio 代辦路徑。

<a id="finding-2"></a>

### [2] studio／asr-gateway 經內網明文 HTTP 抓 CSP JWKS（信任錨傳輸未加密）

| Field | Value |
| --- | --- |
| Severity | low |
| Confidence | high |
| Confidence rationale | source-validated control flow on revision 151fca19; no runtime reproduction |
| Category | insecure_transport |
| CWE | CWE-319 |
| Affected lines | services/asr-gateway/app/services/jwks_client.py:279-283, infra/compose/platform.yml:521, packages/anila-core/src/anila_core/api/middleware/jwks_client.py:243-249 |

#### Summary

studio／asr-gateway 經內網明文 HTTP 抓 CSP JWKS（08-02 已裁定維持現狀的信任模型殘餘風險）

#### Root Cause

CSP_BASE_URL 預設並由 compose 寫死 http://csp:8000；兩份 _fetch_jwks（studio 原件與 asr vendored 逐字副本）裸 httpx GET 無 https 檢查、無 ca_file。對照 anila-core jwks_client.py:243-262 明確拒 http。

#### Validation

platform.yml:521/840 寫死；config.py 預設；studio test_jwks_client.py:123-126 基座鎖 http://csp-test:8000；2026-08-02 私有稽核已裁定維持現狀（無內部 TLS），revocation 未 ready 時 fail-closed 503/4503。

#### Dataflow

攻擊者須先能在 anila-net 干擾 compose DNS／ARP 才能餵假 JWKS；撤銷快取未 ready 時服務本身 fail-closed。

#### Reachability

前置條件是內網 MITM 能力，超出現行信任模型記錄；08-02 裁定維持現狀，重開需先建內部 TLS（CA＋SAN＋CA 發進容器）。

#### Severity

**Low** — mitigated by internal trust model ruling 2026-08-02

Additional runtime or deployment evidence could raise or lower this severity.

#### Remediation

維持現狀（08-02 裁定）。若重開：先內部 CA＋SAN 憑證＋CA 發進 studio/asr 容器＋兩極測試（http JWKS→啟動失敗；無 TLS 時現行 http 仍可啟動），再共用 anila-core client。

<a id="finding-3"></a>

### [3] studio／flux2-dev-agent 執行期消費 image-primary URL 未重驗 SSRF（註冊面有守衛，執行面 TOCTOU 未對齊）

| Field | Value |
| --- | --- |
| Severity | low |
| Confidence | high |
| Confidence rationale | source-validated control flow on revision 151fca19; no runtime reproduction |
| Category | ssrf |
| CWE | CWE-918 |
| Affected lines | services/csp/app/api/models.py:1462-1470, services/anila-studio/app/services/flux_image_provider.py:352-356, services/csp/app/api/proxy.py:1372-1375 |

#### Summary

studio／flux2-dev-agent 執行期消費 image-primary URL 未重驗 SSRF（TOCTOU 未對齊 proxy 對照組）

#### Root Cause

CSP 模型註冊時 _enforce_endpoint_url 有 validate_outbound_url（models.py:195），set-image-primary 不重驗；studio 與 flux2-dev-agent 從 GET /api/models/image-primary 取得 endpoint_url 後直接 POST，全樹零 validate_outbound_url 呼叫。對照 CSP agent proxy 兩處 call-time _guard_outbound（proxy.py:1372/1708）。

#### Validation

rg validate_outbound_url 在 services/anila-studio/app 與 services/flux2-dev-agent/app 零命中；flux_image_primary.py:82-104 入快取、flux_image_provider.py:352-356 POST、flux2-dev-agent image_primary_fetcher.py:92 起 GET 後 POST；出站 URL 是 admin 登記值、非使用者 URL；compose 三處 ANILA_ALLOW_HTTP_ENDPOINT 出廠 0、studio／flux-agent 容器無 ANILA_TRUSTED_HOSTS env。

#### Dataflow

admin 登記 model endpoint_url（註冊時過守衛）→ set-image-primary 不重驗 → studio／flux-agent 執行期 POST 該 URL。若 DNS 或登記值註冊後變動，執行面無 TOCTOU 防線。

#### Reachability

需要 admin-tier 登記權＋有效 image-primary；預設 compose 下 http 端點註冊即被擋（flag 出廠 0）；內網 http 是 SYSTEM-MAP 記錄的常態。

#### Severity

**Low** — admin-controlled target; exposure requires post-registration change

Additional runtime or deployment evidence could raise or lower this severity.

#### Remediation

若重開：studio／flux-agent POST 前呼叫 validate_outbound_url(endpoint_kind="model")，並補兩個容器各自的 ANILA_TRUSTED_HOSTS／ANILA_ALLOW_HTTP_ENDPOINT env 契約（預設 http://flux2-dev:8000 否則會斷），set-image-primary 重跑同一守衛。契約裁定前不動碼。

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| csp/api 全路由面（235 endpoints） | not recorded | No issue found | auth（password/card/oidc/revocations/registration）、api_keys、users、departments、alerts、banners、memory、attachments、handoffs、directory、conversations、proxy(/v1 chat)、usage、models、agents(credentials/registration/approval/runtime_config/functions/health)、trusted_hosts、service_clients/grants/services/launch、platform_links/settings/prompts、audit_logs(export)、ingestion(collections/documents/preview/jobs/relations/search/image_blob/eval_runs/credentials)、artifacts、institutional_kb、message_actions、tasks、jwks |
| csp middleware/services 核心 | not recorded | No issue found | csrf/caller/cookies/api_key_auth、startup_security、attachment_service、auth_service、card_auth(+service)、external_auth_service(OIDC)、audit_ledger、memory_service、storage_paths、proxy/task_link、service_token_envelope、credential_crypto |
| anila-core 安全基礎 | not recorded | No issue found | url_guard(validate_outbound_url)、credential_crypto、jwks_client(https guard)、router_server(auth relay/session owner)、memory.chunk 旗標、tools/shell(caps+allowlist)、dispatch_tool |
| router/studio/asr-gateway/flux-agent 入口 | not recorded | No issue found | router /v1 auth relay＋session owner binding；studio auth.py（JWKS＋revocation fail-closed）、job ownership、flux_image_primary/provider、image_primary_fetcher；asr WS 4401/4503 |
| 前端（anila-shell/anilalm/governance-ui） | not recorded | No issue found | cookie-only auth、CSRF header 注入（api.js/sse.js 兩處都有）、markdown/mermaid securityLevel strict、MarkdownPreview DOMPurify FORBID_TAGS、banners 純文字、echarts 無 HTML formatter、governance 無 v-html |
| 基礎設施（nginx/compose/Dockerfile/部署腳本） | not recorded | No issue found | 限速、CSP header、client_max_body_size、loopback DB、internal-only expose、profiles:ops gating（gitlab）、n8n 無 profiles gate（N8N 自帶首啟用戶管理）、codeserver 密碼 guard、pgvector/nginx/n8n/codeserver 自建映像、deploy-prod verify 修補後行為、patch-image-size-cve.js |
| cht mock 卡片元件 | not recorded | No issue found | dev-only、loopback 16888、測試 CA 30 天 fail-closed 三條件 |
| secrets/.env | not recorded | Not applicable | gitignore 忽略、未讀內容（secret 檔） |
| models/model 權重、data/、share/、logs/、node_modules、myCSPPlatform/.codex、runtime_logic | not recorded | Not applicable | 非程式碼或 gitignored |
| 逐行審計其餘 services/packages 檔案（合計 ~1100 py + 306 前端檔） | not recorded | Needs follow-up | 本 Standard 掃描為結構性審計＋熱區逐行（auth/proxy/artifacts/services/SSRF/jwks/attachments/memory/tasks/ingestion 上傳與 blob/前端 XSS 面/部署面）；餘量列出為部分覆蓋，非逐行 |

## Open Questions And Follow Up

- 治理 UI 無 allowed_origins 欄位字面（PlatformLinksView 用 entry_url）；治理中心是否還有別的建立面可產生空名單跨主機列未查 UI 全量
- 活庫存量資料（空名單跨主機服務列、幽靈 artifact owner）未盤點（禁 DB）
- n8n 於 default ship 啟用（無 profiles gate）；N8N 首啟用戶管理由誰把關未由本掃描驗證
- EXCEL 對 csv_formula_safe 前導字元的處理未實測（程式內註解自承）
- Q22 非 agent owner 歸因契約待擁有者裁定（DECISION-PACK 2026-09-08）；已以 finding glm-s2s-owner-attribution 記錄，待裁定後修補
  - Follow-up prompt: Review deferred unit q22-owner-contract and close its stated proof gap. Surfaces: csp-api-routes.
- A10 執行期 SSRF 契約待裁定（env 成本：studio/flux-agent 無 trusted-hosts env）；已以 finding glm-consumer-ssrf-toctou 記錄
  - Follow-up prompt: Review deferred unit a10-consumer-ssrf and close its stated proof gap. Surfaces: service-entrypoints.
- A07 內部 TLS 未建（2026-08-02 裁定維持現狀）；已以 finding glm-internal-jwks-plaintext 記錄
  - Follow-up prompt: Review deferred unit a07-internal-tls and close its stated proof gap. Surfaces: service-entrypoints, infrastructure.
- 約 1100 py + 306 前端檔之餘量為結構性走讀，未逐行；熱區已逐行
  - Follow-up prompt: Review deferred unit remaining-line-audit and close its stated proof gap. Surfaces: remaining-line-audit.

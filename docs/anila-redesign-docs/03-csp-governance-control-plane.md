> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的設計文件,保留當時的決策與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> ⚠ **2026-08-01 P2.1 契約更新**：下文若仍描述 `csk-`／`bsk-`／`X-CSP-Service-Token`／
> `CSP_SERVICE_TOKEN` 作為 agent 派工身分，該段已過時。現行＝5 分鐘派工 JWT＋JWKS 驗簽
> （開發者不領鑰匙；三級接入見 `docs/guides/developer-guide.md`）。
> 歷史段落未逐字改寫，以免破壞 redesign 文件結構。

# 03. CSP Governance Control Plane

> Status: draft v0.1  
> Purpose: 定義 CSP 作為 ANILA 控制面與資料面的職責、模組拆分、權限、審計與治理 API。  
> Principle: CSP 是 authoritative store，不是單純後台；所有正式 runtime traffic、registry、policy、audit 都要回到 CSP。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. CSP 職責

CSP 同時承擔兩個面：

```text
Control Plane /api/*
  - User / Department / Role
  - Card SSO / JWT / JWKS / Revocation
  - Model Registry
  - Agent Registry
  - Service Registry
  - Service Client / Integration Key
  - Knowledge Governance
  - Classification / Latch
  - Audit / Usage / Trace dashboard

Data Plane /v1/*
  - OpenAI-compatible model proxy
  - Agent proxy
  - Model discovery
  - Agent discovery
  - Embedding proxy
  - Usage accounting
  - Full trace ingestion
```

---

## 2. Bounded Contexts

CSP 不應成為單一上帝模組。新專案內部至少要分這些 bounded contexts：

```text
csp/
├── identity/
├── auth/
├── policy/
├── registry/
│   ├── models/
│   ├── agents/
│   ├── services/
│   └── tools/
├── data_plane/
│   ├── openai_proxy/
│   ├── model_gateway/
│   └── agent_gateway/
├── knowledge_governance/
├── classification/
├── trace/
├── audit/
├── usage/
└── admin_ui_contracts/
```

MVP 可同一個 FastAPI app 實作，但 module boundary 不可混用。

---

## 3. Credential Tiers

保留既有三層 credential 設計，並重新命名使開發者直觀。

| 層級 | 現有語意 | 新命名 | 用途 |
|---|---|---|---|
| User Session | JWT / cookie | Card SSO Session | 管理面、ANILA UI |
| User/API Data Plane | `sk-` API Key | Runtime API Key | SDK / Router / curl / runtime caller |
| Service-to-Service | `csk-` / service token | Integration Key / Service Client Token | Router / worker / agent / GUI Service callback |

> ✅ 已拍板（v0.2，命名統一規則）：`csk-` 作為**內部 token prefix 保留不變**
> （wire format、DB、程式碼不動）；但 UI 與文件對開發者一律顯示
> **Agent Integration Key**（agent 憑證）／ **Service Client Token**
> （router / worker 等 service client），**不再以「CSK」作主要概念名**。
> 與 doc 05 §8 的改名原則一致（改名須保留 csk- 的雙角色語意：
> 入向 dispatch 驗證＋出向 bound-collection 搜尋）。

### 原則

1. 前端不持有 upstream model API Key。
2. Router / Agent 不持有 upstream model API Key。
3. Agent 接收 CSP 注入的 service credential 與身份 header。
4. Model gateway 只接收模型 gateway API key，不接收 CSP service token。
5. GUI Service 使用 launch token / SSO，不拿使用者長期 API key。（**目標新增**：現況完全沒有 launch token 機制 —— `platform_links` 只是純連結，配 `service_access_grants` 決定可見/可開，開啟服務時不簽發任何 token。）

### 現況 credential 生命週期（origin）

`bsk-` bootstrap 與 `csk-` 服務憑證的生命週期已在現況實作（`myCSPPlatform/backend/app/services/agent_credential_service.py`），治理面應正面納管、而非重造：

- **`bsk-` bootstrap token**：admin 對單一 agent 簽發，一次性、預設 **15 分鐘 TTL**（`BOOTSTRAP_DEFAULT_TTL`）；重簽即讓前一把立即失效。
- **兌換綁定 endpoint URL**：兌換時呈交的 `endpoint_url` 必須與該 agent 註冊的 `endpoint_url` 完全一致，防外洩的 `bsk-` 被替別的 agent 兌換憑證。
- **原子 CAS 防重放**：條件式 UPDATE（`bootstrap_token_consumed_at IS NULL` 才命中）保證兩個併發兌換恰好一個成功。
- **靜態簽發**：無法跑 bootstrap 流程的 agent，admin 可直接 mint 憑證（跳過 bootstrap，無自動輪替、需人工定期輪替）。
- **輪替**：舊 token 進入 `service_token_previous_*` 欄位，預設 **24 小時 grace**（`ROTATION_GRACE_DEFAULT`）內仍可通過驗證。
- **軟撤銷 + 快取失效**：撤銷是 `is_active=False` + `revoked_at`（冪等）；出向 dispatch 用的 per-agent token 有 5 分鐘記憶體快取，admin 端點在簽發/輪替後呼叫 `invalidate_agent_token_cache()` 立即失效。
- **審計字彙**：`service_token_bootstrap_issued` / `service_token_bootstrap_consumed` / `service_token_issued` / `service_token_rotated` / `service_token_revoked` / `service_token_verified` / `service_token_legacy_env_used`。

**Legacy 艦隊共用 token**：`CSP_SERVICE_TOKEN` env var 仍是現況 fallback —— agent 尚無 DB 憑證時，出向 dispatch 退回這把艦隊共用 token；入向驗證命中 legacy token 時 caller 不可歸因，並寫 `service_token_legacy_env_used` 審計事件，憑證列以 `is_legacy` 追蹤 cutover 進度；`GET /api/usage/legacy-token-stats` 回報 24h/7d/30d 命中數與 `last_seen_at`。治理面必須把這條路徑列為「刻意退場」項目，而不是默默留著。

---

## 4. Identity Headers

保留既有 CSP → Agent header 模式，但 formalize（標註者為目標新增）：

```http
X-CSP-Service-Token: <integration-key>
X-ANILA-User-Id: <employee_id>
X-ANILA-User-Email: <email optional>
X-ANILA-User-Groups: <groups optional>
X-ANILA-Task-Id: <task_id>                     # 目標新增
X-ANILA-Trace-Id: <trace_id>                   # 目標新增（作為出向 header）
X-ANILA-Classification-Level: <level>          # 目標新增
```

模型呼叫只允許：

```http
Authorization: Bearer <MODEL_GATEWAY_API_KEY>
X-ANILA-User-Id: <employee_id optional>
X-ANILA-Trace-Id: <trace_id>                   # 目標新增（現況任何狀態都沒有 trace header 送往模型）
```

### 現況 header 快照（origin/prod-intranet-card；與 04 §3 逐字一致）

```http
# CSP → Agent dispatch（build_agent_headers）
X-CSP-Service-Token: <csk- per-agent 憑證；無 DB 憑證時退回 legacy CSP_SERVICE_TOKEN>
X-ANILA-User-Id: <員編；downstream_identity() 驗 6–9 碼數字，非卡片帳號 fail-safe 省略、不偽造，請求照常放行>
X-ANILA-User-Email: <email>

# CSP → Model gateway（build_model_gateway_headers + _apply_gateway_auth）
Authorization: Bearer <MODEL_GATEWAY_API_KEY>
X-ANILA-User-Id: <員編；同上省略規則>
```

- 模型呼叫**只**帶 gateway bearer key + `X-ANILA-User-Id`，永遠不帶 `X-CSP-Service-Token`、email/groups；任何狀態下都沒有 trace header 送往模型。
- `X-ANILA-User-Groups`：`build_agent_headers` 留有參數管線，但**沒有任何呼叫點傳入** —— dead plumbing，現況線上永遠不會送出。
- 入向（/v1 資料面）：CSP 只讀 `X-ANILA-Conversation-Id` 與 `X-ANILA-Trace-Id`（落入 usage 歸因欄位，不轉發為出向 header）。
- `X-ANILA-Task-Id`、`X-ANILA-Classification-Level`：目標新增，現況程式碼任何地方都不存在。

---

## 5. Policy Engine

### PolicyDecision

所有高價值動作必須產生 policy decision。

```ts
PolicyDecision {
  id: string
  actor_user_id: number
  action:
    | "task.run"
    | "model.invoke"
    | "agent.invoke"
    | "service.launch"
    | "artifact.export"
    | "collection.read"
    | "classification.downgrade_request"
    | "registry.create"
    | "registry.approve"

  resource_type: string
  resource_id: string
  decision: "allow" | "deny" | "require_approval"
  reason: string
  matched_policy_ids: string[]
  classification_level: ClassificationLevel
  created_at: string
}
```

### Policy Inputs

- User role。
- Department。
- Clearance level。
- Resource classification。
- Source scope。
- Model/Agent/Service classification ceiling。
- Service access grant。
- Agent permission。
- API key permission。
- Latch state。
- Supervisor approval state。

---

## 6. Registry Ownership

| Registry | Admin | Developer | Service Admin |
|---|---|---|---|
| Model Registry | CRUD / approve | read allowed | no |
| Agent Registry | approve / disable / assign permission | create own / update own draft | no |
| Service Registry | approve global policy | no | create/update own department service |
| Tool Registry | CRUD / approve | request | no |
| Classification Policy | CRUD | no | no |

> `service_admin` **不是全域 role enum 值**（現況 role 只有 `owner/admin/developer/user/system`，見 §14），而是 **per-service 指派**：由目標 `RegisteredService.service_admin_user_ids`（見 07）承載。表中「Service Admin」欄指的是被指派到該服務的使用者，不是一個新的全域角色。
>
> ✅ 已拍板（2026-07-02）：**保留 admin-tier（admin/owner）全域 bypass**。平台 admin/owner 可跨越 per-service admin 邊界（role gate、public flag、grant 全跳過）；per-service admin 是「授權下放」而非「排他邊界」。所有跨界管理操作仍必須寫入 audit log（actor、action、resource），供事後追溯。

---

## 7. Model Registry

保留既有 `model_registry`。新增欄位：

```text
classification_ceiling
network_zone = intranet
secret_ref
healthcheck_url
owner_department_id
allowed_task_types
```

---

## 8. Agent Registry

保留既有 `agents` 表。新增欄位：

```text
runtime_type
agent_version
audit_level = full_trace
classification_ceiling
default_classification_level
trace_callback_mode
manifest_url
healthcheck_url
owner_department_id
```

`requires_encryption` 暫時保留作 migration bridge：

```text
requires_encryption = true  => default_classification_level >= 機密
requires_encryption = false => default_classification_level = 無機密
```

---

## 9. Service Registry

基於 `platform_links` / `service_access_grants` 擴充：

```text
registered_services
service_versions
service_launches
service_audit_callbacks
service_project_bindings
service_access_grants
```

保留現有 default-deny + grant algorithm。

---

## 10. Audit Event Catalog

最小事件：

```text
auth.card_login_succeeded
auth.card_login_failed
auth.jwt_revoked

task.created
task.policy_checked
task.source_snapshot_created
task.completed
task.failed

model.registered
model.enabled
model.disabled
model.invoked

agent.registered
agent.approved
agent.rejected
agent.disabled
agent.invoked
agent.trace_received

service.registered
service.updated
service.launched
service.audit_callback_received
service.disabled

classification.latched
classification.downgrade_requested
classification.downgrade_approved
classification.downgrade_rejected

artifact.generation_requested
artifact.generated
artifact.exported

collection.created
collection.access_granted
collection.published
collection.revoked
```

---

## 11. CSP API Surface

### Identity

既有：

```text
GET  /api/auth/card/challenge     # 卡登實際路由形態
POST /api/auth/card/verify
GET  /api/auth/card/registration/departments   # pending 使用者完成註冊：部門下拉（public）
POST /api/auth/card/complete-registration      # pending 使用者完成註冊（registration_token JWT，非 cookie）
GET  /.well-known/jwks.json
GET  /api/auth/me
```

目標新增：

```text
POST /api/auth/revoke             # 現況對應：GET /api/auth/revocations（撤銷清單，供下游驗證器）+ POST /api/auth/logout
```

### Registry

既有：

```text
/api/models/*
/api/agents/*
/api/service-clients/*
/api/trusted-hosts/*              # 語意見 §14「Trusted hosts 現況語意」
```

目標新增：

```text
/api/services/*                   # 現況對應：/api/platform-links + /api/service-access-grants
                                  # 是 Service Registry 的遷移種子
```

### Data Plane

既有：

```text
GET  /v1/models
GET  /v1/agents
POST /v1/chat/completions
POST /v1/embeddings
```

目標新增：

```text
POST /v1/traces                   # 現況沒有任何 trace ingestion 端點
```

### Governance

既有：

```text
GET /api/audit-logs               # 實際路由；原稿的 GET /api/audit-events 為誤植
GET /api/usage/*                  # 實際只有子路由：/summary /chart /top-models /top-users
                                  # /top-departments /top-agents /by-base-model /by-client
                                  # /agents/{id} /legacy-token-stats /export；沒有裸 GET /api/usage
```

目標新增：

```text
GET /api/traces/{trace_id}        # 現況沒有 trace 查詢端點
GET /api/policy-decisions         # 現況沒有對應 router（見 §14）
```

---

## 12. UI Structure

```text
治理中心
├── Dashboard
├── 使用者 / 部門 / 角色
├── 模型
├── Agent
├── Service / 專案入口
├── Service Clients / Integration Keys
├── 知識治理
├── 機敏分類 / 降級申請
├── Trace Explorer
├── Audit Logs
└── Usage
```

---

## 13. Done Criteria

CSP v1 完成條件：

- 所有正式 model / agent / service 都有 registry row。
- 所有 runtime call 都寫 usage。
- 所有正式 task 都有 trace。
- 所有 policy deny 都有可解釋原因。
- 所有 classification latch / downgrade 都有 audit。
- 所有 GUI Service launch 都有 launch event。
- 所有 Admin 操作都有 audit event。

---

## 14. Repo evidence / 現況補齊

### `audit_logs` 與 `PolicyDecision`

目前 `myCSPPlatform/backend/app/models/audit_log.py` 的 `audit_logs` 欄位為：

```text
id
actor_user_id
actor_username
action
resource_type
resource_id
status
detail
ip_address
metadata_json
created_at
```

`myCSPPlatform/backend/app/services/audit_service.py` 的 `log_audit_event(...)`
可寫入任意 `metadata` JSON，因此短期可把 `PolicyDecision` 的
`reason`、`matched_policy_ids`、`classification_level`、`decision`
放在 `metadata_json`，作為過渡審計事件使用。

但它不足以成為正式 `PolicyDecision` store：

- 沒有 `policy_decisions` 獨立表。
- 沒有 `decision` enum 欄位與 policy FK。
- `matched_policy_ids`、`classification_level` 只會是 JSON，缺少可查詢索引。
- 沒有 append-only/hash-chain 或不可竄改機制。
- `GET /api/policy-decisions` 目前沒有對應 router。

結論：`audit_logs` 足以承載「policy checked」audit event；正式治理中心仍應新增
`policy_decisions` 或等價 projection table。

### `service_clients` / `agent_credentials` token envelope

`myCSPPlatform/backend/app/models/service_client.py` 的 `service_clients`
是非 Agent 的 S2S caller，註解與 API 目前定位為 Router、worker、admin tooling。
`myCSPPlatform/backend/app/api/service_clients.py` 允許的 `client_type` 目前是：

```text
router
worker
admin_tool
```

token 欄位包含：

```text
service_token_envelope
service_token_lookup_hash
service_token_previous_envelope
service_token_previous_lookup_hash
service_token_previous_expires_at
service_token_issued_at
service_token_rotated_at
revoked_at
is_active
```

`myCSPPlatform/backend/app/services/service_token_envelope.py` 使用
`enc::v1::<base64(nonce|tag|ciphertext)>` envelope，lookup hash 用 sha256；
`myCSPPlatform/backend/app/services/agent_credential_service.py` 先驗
`service_clients`，再驗 `agent_credentials`，並支援 previous-token grace window。

Agent 並不共用 `service_clients` 表，而是使用
`myCSPPlatform/backend/app/models/agent_credential.py` 的 per-agent credential。
因此：

- Router / worker / admin tooling：目前可由 `service_clients` 支援。
- Agent callback / RAG search：目前走 `agent_credentials`，不是 `service_clients`。
- GUI Service callback：token envelope 機制可重用，但需要新增 `client_type`
  或獨立 service registry 綁定；現有 allow-list 沒有 `gui_service` / `service_callback`。

### Trusted hosts 現況語意

`/api/trusted-hosts` 背後是「DB 允許清單 ∪ env `ANILA_TRUSTED_HOSTS`」的聯集：

- `myCSPPlatform/backend/app/services/trusted_host_service.py` 以 provider hook 掛進
  anila-core `url_guard`（`register_trusted_host_provider`），30 秒 TTL 記憶體快取、
  本程序內任何增刪即時失效。
- **Provider 失敗 fail-closed**：provider 拋錯或 DB 掛掉時，guard 退回 env-only
  清單繼續驗證 —— 絕不因 provider 壞掉而放行未知 host。
- 命中 trusted host 會跳過後續**所有** host 檢查（deny list、內部 zone、
  single-label、私網/loopback IP、DNS 解析），但 **scheme 檢查仍先行**
  （http 仍受 `ANILA_ALLOW_HTTP_ENDPOINT` 管制）。
- **授權不對稱**：`GET` 是 admin tier（admin + owner）可讀；`POST` / `DELETE`
  只有 owner 能動，且每次增刪都寫 audit log。

### `banners` / `alerts` 是否納入 governance UI

`banners` 已是控制面功能：

- `myCSPPlatform/backend/app/api/banners.py`
  - `GET /api/banners/active`：任何登入使用者可讀。
  - `GET/POST/PUT/DELETE /api/banners`：admin tier 管理。
- `myCSPPlatform/backend/app/models/banner.py`
  - plain text content，`level` 為 `info` / `warning` / `error` / `success`。
  - 註解明確定位為維運 / 安全 / policy notice。

`alerts` 也是 governance UI 應顯示的 operational signal：

- `myCSPPlatform/backend/app/api/alerts.py`
  - admin-only list / summary / ack / resolve。
- `myCSPPlatform/backend/app/models/alert.py`
  - `category`、`severity`、`source_type`、`source_id`、`status`、`metadata_json`。
- `myCSPPlatform/backend/app/services/health_checker.py`
  - model / agent health loop 會 upsert 或 resolve health alerts。

結論：`banners` 是治理訊息發布面，`alerts` 是治理告警面；兩者應納入 CSP
governance UI，但不應混同為 `PolicyDecision` 核心模型。

### Role enum 實際值

實際 role 值分散在 schema 與 service：

- `myCSPPlatform/backend/app/models/user.py`：DB 欄位是 string，default `"user"`，
  註解列出 owner/admin/user/developer。
- `myCSPPlatform/backend/app/schemas/user.py`：
  `UserRole = Literal["owner", "admin", "developer", "user", "system"]`。
- `myCSPPlatform/backend/app/services/auth_service.py`：
  admin tier 是 `("admin", "owner")`；另有 `require_owner`。
- `myCSPPlatform/backend/app/schemas/platform_link.py`：
  UI link role allow-list 為 `owner` / `admin` / `user` / `developer`。

結論：正式規格應採用：

```text
owner
admin
developer
user
system
```

其中 `system` 是 ingestion-worker 等內部帳號使用，不應出現在一般手動指派 UI。

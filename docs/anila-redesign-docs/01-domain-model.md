# 01. Domain Model

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的設計文件,保留當時的決策與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> Status: draft v0.1  
> Purpose: 定義新 ANILA 的核心實體、生命週期與資料擁有權。  
> Principle: 新專案可以重構 schema，但不應放棄既有成熟概念，如 `model_registry`、`agents`、`token_usage`、`audit_logs`、`platform_links`、`service_access_grants`、ingestion collections、conversation classified latch。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

## ADR note: OpenWebUI scope

OpenWebUI 不納入 ANILA 目標架構。OpenWebUI 只作為既有 Agent 註冊狀態的來源背景；本專案不做 OpenWebUI 匯出匯入、不做自動 bridge、不把 OpenWebUI 作 runtime 依賴。既有 Agent 一律透過 CSP Agent Registry 重新註冊（✅ 已拍板 2026-07-02，詳見 doc 00 ADR-0003 與 doc 06 文首）。

（緣由：經人工複核，OpenWebUI 為過去開發者開發完 Agent 後註冊之平台；本專案目的在於建立安全機制情況下將 Agent 收回，而非沿用開源套件，故 OpenWebUI 之匯出匯入與本專案開發無關。）

---

## 1. Core Domain Map

```text
Identity
├── User
├── Department
├── Role
├── AuthSession
└── ServiceClient

Task
├── Task
├── TaskRun
├── TaskMessage
├── SourceSnapshot
└── Citation

Knowledge
├── Collection
├── Document
├── Chunk
├── IngestionJob
└── CollectionReview

Runtime
├── ModelEndpoint
├── AgentDefinition
├── AgentVersion
├── AgentRuntimeCredential
├── ToolDefinition
└── TraceSpan

Artifact
├── Artifact
├── ArtifactVersion
├── ArtifactFile
└── ExportRecord

Project Entry
├── RegisteredService
├── ServiceLaunch
├── ServiceAccessGrant
├── ServiceAuditCallback
└── ServiceProjectBinding

Governance
├── PolicyDecision
├── AuditEvent
├── UsageRecord
├── ClassificationLabel
├── ClassifiedLatch
├── DeclassificationRequest
└── SupervisorApproval
```

---

## 2. Identity

### User

沿用既有 `users` 主體，保留：

- `id`
- `username`
- `email`
- `department_id`
- `role`
- `is_active`
- `token_version`
- card-login 分支中的員編語意

新增或標準化：

```ts
User {
  id: number
  employee_id: string
  display_name?: string
  email?: string
  department_id?: number
  role: "user" | "developer" | "admin" | "owner" | "system"
  clearance_level: ClassificationLevel
  is_active: boolean
}
```

> `system` 是內部服務帳號（如 ingestion-worker），只能由 migration / seed /
> owner-level tool 建立，不可在一般使用者管理 UI 中手動指派——與 doc 03 §14
> 的現況 role enum（`UserRole` Literal 五值）一致。

> `employee_id` 為目標新增欄位：現況（origin）任何分支的 `users` 表都沒有 `employee_id` 欄位，員編語意騎在 `username` 上——卡登分支中卡片憑證的員編就是 `username`。下傳身分現況（commit `80f60d4`）：`proxy_service.downstream_identity()` 在 `username` 符合員編形狀（6–9 碼數字）時，以 `X-ANILA-User-Id` 直接轉發員編；非卡片帳號（如 admin）回 `None`、fail-safe 省略 header，不偽造身分（請求仍放行，只是無下游使用者歸因）。

### Role

```text
user        一般使用者
developer   可註冊 Agent，可查看自己 Agent 狀態
admin       可管理模型、Agent、Service、權限、分類降級申請
owner       break-glass / 系統最高管理

service_admin（服務層級指派，非全域 role）
            可管理自己小組 GUI Service / Project Entry
```

> 定案：`service_admin` **不是**全域 role enum 值，而是服務層級指派（per-service assignment，例如 `RegisteredService.service_admin_user_ids`）。全域 role 維持 `user / developer / admin / owner` 四值（與現況 `users.role` 一致）；權限矩陣中的 service admin 語意由 Service Registry 承載，不需動 `users.role` schema。

---

## 3. Task

### Task

Task 是新系統主脊椎。Conversation 只是 Task 的互動容器。

```ts
Task {
  id: string
  task_type:
    | "query"
    | "summarize"
    | "analyze"
    | "compare"
    | "draft"
    | "generate_artifact"
    | "launch_service"
    | "governance"

  actor_user_id: number
  department_id?: number

  status:
    | "draft"
    | "submitted"
    | "policy_checking"
    | "source_resolving"
    | "running"
    | "waiting_for_user"
    | "completed"
    | "failed"
    | "cancelled"
    | "blocked_by_policy"

  source_scope:
    | "none"
    | "personal"
    | "project"
    | "organization"
    | "registered_service"

  source_snapshot_id?: string
  selected_collection_ids: number[]
  selected_service_id?: string

  requested_output_type?:
    | "answer"
    | "report"
    | "slides"
    | "mindmap"
    | "infographic"
    | "datatable"
    | "service_launch"

  classification_level: ClassificationLevel
  trace_id: string
  policy_decision_id?: string
  created_at: string
  updated_at: string
}
```

### TaskRun

每次執行、重跑、Agent handoff 都應有 run 記錄。

```ts
TaskRun {
  id: string
  task_id: string
  run_type: "model" | "agent" | "studio" | "service_launch" | "retrieval"
  status: "queued" | "running" | "completed" | "failed" | "cancelled"
  model_id?: string
  agent_id?: string
  service_id?: string
  started_at: string
  completed_at?: string
  error_code?: string
  error_message?: string
  trace_id: string
}
```

---

## 4. SourceSnapshot

SourceSnapshot 是避免 citation 漂移的核心。

```ts
SourceSnapshot {
  id: string
  task_id: string
  source_scope: Task["source_scope"]
  collection_ids: number[]
  document_ids: number[]
  chunk_ids: string[]
  document_versions: Record<string, string>
  retrieval_queries: string[]
  created_at: string
  classification_level: ClassificationLevel
}
```

規則：

1. 回答、artifact、GUI service launch 都要指向 snapshot 或明確宣告無來源。
2. Citation 指向 snapshot 中的 chunk，不直接指向 live document。
3. Snapshot classification = 所有來源最高分類。

---

## 5. Citation

```ts
Citation {
  id: string
  source_snapshot_id: string
  document_id: string
  chunk_id: string
  quote_preview: string
  page?: number
  score?: number
  used_by:
    | "answer"
    | "artifact"
    | "agent_tool"
  created_at: string
}
```

---

## 6. Knowledge

### Collection

保留既有 ingestion collection 的核心：

```ts
Collection {
  id: number
  name: string
  owner_user_id?: number
  owner_department_id?: number
  scope: "personal" | "project" | "organization"
  status: "draft" | "active" | "submitted_for_review" | "published" | "deprecated" | "revoked"
  classification_level: ClassificationLevel
  created_at: string
  updated_at: string
}
```

### CollectionMembership

```ts
CollectionMembership {
  collection_id: number
  subject_type: "user" | "department" | "role"
  subject_id: string
  permission: "read" | "write" | "review" | "publish" | "admin"
}
```

---

## 7. ModelEndpoint

既有 `model_registry` 應保留，但語意改名為 `ModelEndpoint`。

```ts
ModelEndpoint {
  id: string
  name: string
  display_name: string
  endpoint_url: string
  api_version: "v1" | "v2"
  model_type: "llm" | "embedding" | "vlm" | "image" | "reranker"
  provider_type: "intranet_remote"
  protocol: "openai_compatible" | "custom_adapter"
  api_key_secret_ref?: string
  classification_ceiling: ClassificationLevel
  is_active: boolean
  health_status: "unknown" | "healthy" | "degraded" | "unhealthy" | "disabled"
}
```

本專案目前只考慮「院內不同主機」模型，不建外部雲端模型語意。

> 健康狀態五值（`unknown/healthy/degraded/unhealthy/disabled`）為目標語彙。現況有兩套詞彙需遷移：`model_registry.health_status` 實際使用 `online / connecting / offline`（`model_registry.py:18`）；`agents.health_status` 欄位註解寫 `unknown / healthy / unhealthy`，但 health loop（`health_checker.py`）對 agent 實際寫入的也是模型那套 `online / connecting / offline`。遷移映射：`online → healthy`、`connecting → degraded`、`offline → unhealthy`、初始未檢查 → `unknown`；`disabled` 為新增值，承接停用（如 `is_active = false` / approval disabled）語意。

---

## 8. AgentDefinition

既有 `agents` 表應保留並升級。

```ts
AgentDefinition {
  id: number
  name: string
  owner_user_id: number
  owner_department_id?: number

  runtime_type:
    | "anila_agent"
    | "langchain"
    | "openwebui_pipe_compatible"
    | "openai_compatible_agent"
    | "custom_http"

  endpoint_url: string
  api_version: "v1"
  description_for_router: string
  input_schema?: object
  output_schema?: object
  capabilities?: object
  base_model_id?: string
  bound_collection_id?: number

  audit_level: "full_trace"
  classification_ceiling: ClassificationLevel
  default_classification_level: ClassificationLevel

  approval_status: "pending" | "approved" | "rejected" | "disabled"
  health_status: "unknown" | "healthy" | "degraded" | "unhealthy" | "disabled"
  runtime_config?: object

  created_at: string
  approved_by?: number
  approved_at?: string
}
```

> `requires_encryption: boolean` 應遷移為 `default_classification_level >= 機密` 或 `classification_policy`，避免 boolean 不能表示五級分類。

> `health_status` 五值為目標語彙，現況遷移映射見 §7 的註記。`runtime_type` 中的 `openwebui_pipe_compatible` 為目標保留值；其適用範圍受文件 00 ADR-0003 的「✅ 已拍板」裁決約束（2026-07-02：Agent 收回、改註冊 CSP 的目標不變，一律走既有精靈 / CLI 手動重新註冊，自動化匯出匯入橋接工具不列入範圍）。

---

## 9. TraceSpan

Full Trace 的最小單位。

```ts
TraceSpan {
  id: string
  trace_id: string
  parent_span_id?: string

  span_type:
    | "task"
    | "policy"
    | "retrieval"
    | "model_call"
    | "agent_run"
    | "tool_call"
    | "service_launch"
    | "artifact_generation"
    | "classification"
    | "audit"

  name: string
  status: "ok" | "error" | "cancelled"
  started_at: string
  ended_at?: string

  actor_user_id?: number
  model_id?: string
  agent_id?: string
  tool_id?: string
  service_id?: string

  input_ref?: string
  output_ref?: string
  error_code?: string
  attributes: object
}
```

---

## 10. Artifact

```ts
Artifact {
  id: string
  type: "report" | "slides" | "mindmap" | "infographic" | "datatable"
  title: string
  status: "queued" | "generating" | "completed" | "failed"
  owner_user_id: number
  source_task_id?: string
  source_snapshot_id?: string
  classification_level: ClassificationLevel
  trace_id: string
  current_version: number
  created_at: string
}
```

### ArtifactVersion

```ts
ArtifactVersion {
  artifact_id: string
  version: number
  file_refs: string[]
  citation_map: object
  generated_by_model_id?: string
  generated_by_agent_id?: string
  generated_by_studio_job_id?: string
  created_at: string
}
```

---

## 11. RegisteredService / Project Entry

將既有 `platform_links` 升級。

```ts
RegisteredService {
  id: string
  name: string
  description?: string
  owner_department_id: number
  owner_admin_user_id: number

  service_type: "gui_app" | "gui_tool" | "project_portal"
  entry_url: string
  launch_mode: "iframe" | "new_tab"
  sso_mode: "card_sso" | "oidc" | "launch_jwt"
  iframe_allowed: boolean

  project_entry: boolean
  project_id?: string

  data_ownership: "self_managed"
  audit_callback_url?: string
  healthcheck_url?: string

  classification_ceiling: ClassificationLevel
  required_roles: string[]
  is_active: boolean
}
```

---

## 12. Classification

```ts
type ClassificationLevel =
  | "無機密"
  | "營業秘密"
  | "機密"
  | "極機密"
  | "絕對機密"
```

### ClassifiedLatch

```ts
ClassifiedLatch {
  id: string
  resource_type: "task" | "conversation" | "collection" | "document" | "snapshot" | "artifact" | "service_launch"
  resource_id: string
  classification_level: ClassificationLevel
  latched_at: string
  latched_by_policy: string
  inherited_from?: {
    resource_type: string
    resource_id: string
  }
  active: boolean
}
```

### DeclassificationRequest

```ts
DeclassificationRequest {
  id: string
  resource_type: string
  resource_id: string
  from_level: ClassificationLevel
  to_level: ClassificationLevel
  requested_by_admin_id: number
  reason: string
  status: "pending_supervisor" | "approved" | "rejected" | "cancelled"
  supervisor_user_id?: number
  decided_at?: string
}
```

---

## 13. Audit / Usage

### UsageRecord

```ts
UsageRecord {
  id: string
  task_id?: string
  trace_id: string
  api_key_id?: number
  actor_user_id: number
  department_id?: number
  model_id?: string
  agent_id?: string
  service_id?: string
  request_type: "chat" | "embedding" | "agent" | "studio" | "service_launch"
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  duration_ms?: number
  created_at: string
}
```

### AuditEvent

```ts
AuditEvent {
  id: string
  timestamp: string
  actor_user_id: number
  actor_department_id?: number
  action_type: string
  resource_type: string
  resource_id: string
  policy_decision_id?: string
  trace_id?: string
  classification_level?: ClassificationLevel
  result_status: "success" | "deny" | "failure"
  metadata: object
}
```

---

## 14. Repo evidence / 現況補齊

### 目標 domain model 與現有 schema 映射

| Target domain | 現有 repo evidence | 可沿用 | 主要缺口 |
|---|---|---|---|
| `User` | `myCSPPlatform/backend/app/models/user.py` | `username`、`email`、`role`、`department_id`、`is_active`、`is_approved`、`token_version`、`ui_settings`、`last_login_at` | 所有分支皆無 `employee_id` 欄位——員編語意騎在 `username` 上（卡登分支 `username` 即憑證員編，origin 現況下傳 `X-ANILA-User-Id` 走 `downstream_identity()`）；亦無 `display_name`、`clearance_level`，皆為目標新增。 |
| `Department` | `department.py` | `name`、`description`、`is_active` | 目前是 flat department，沒有 parent hierarchy、department-scoped service admin policy。 |
| `ServiceClient` | `service_client.py`、migration `0027` | `client_name`、`client_type`、token envelope / lookup hash / previous hash、cert fingerprint、revocation 欄位 | 可作 s2s identity 基底，但還不是完整 integration key / launch client model。 |
| `Task` / `TaskRun` | `conversation.py`、`message.py` | `Conversation` 可作互動容器；`Message.trace_id`、`TokenUsage.trace_id` 可暫接 trace | 沒有 first-class `Task`、`TaskRun`、source snapshot、policy decision、span tree。 |
| `Collection` / `Document` / `Chunk` | `ingestion.py`、`anila_core/storage/adapters/pgvector_store.py` | collection / document / job / relation / chunk 基礎完整，chunk 走 collection-scoped RLS | 沒有檔案 version、source snapshot、collection membership / publish workflow。 |
| `ModelEndpoint` | `model_registry.py` | `name`、`display_name`、`model_type`、`endpoint_url`、`api_version`、`is_router_primary`、`health_status`、`is_internal`、`context_window`、`base_model_id` | 缺 classification ceiling、provider / protocol enum、secret ref。 |
| `AgentDefinition` | `agent.py`、migration `0002` / `0027` / `0029` / `0038` | endpoint agent、approval、health、`base_model_id`、`bound_collection_id`、`runtime_config`、bootstrap token、credential envelope | 缺 `runtime_type`、version、`output_schema`、Full Trace contract、classification policy；`requires_encryption` 仍是 boolean。 |
| `RegisteredService` | `platform_link.py`、`service_access_grant.py`、migration `0012` / `0013` | link catalog、role gate、public/private、user / department grants | 缺 launch mode、iframe policy、SSO mode、launch token、audit callback、healthcheck、owner department、classification ceiling。 |
| `UsageRecord` | `token_usage.py`、migration `0027` | API key / user / department / model / request_type / conversation / trace / caller agent / caller client usage | 尚未以 `task_id` 為中心，也沒有 service launch usage type。 |
| `AuditEvent` | `audit_log.py` | actor、action、resource、status、detail、ip、metadata | 沒有 first-class policy decision / classification event / trace span link。 |
| `ArtifactVersion` | `anila-studio/app/services/*_job_service.py` | 五類 Studio job 都有 job id、`state`（pending / running / done / failed / cancelled）、`step` 步驟字串與 result metadata（沒有數值 `progress` 欄位）；部分類型有 disk artifact path | 目前沒有共用 DB artifact table；多數 job store 是 process memory，restart 後 job state 會消失。 |

### Identity

`users` 現況是 CSP 權威 identity table：`id`、`username`、`email`、`hashed_password`、`role`、`department_id`、`is_active`、`is_approved`、`token_version`、`last_login_at`、`ui_settings`、`created_at`、`updated_at`。`role` 目前只有 `owner/admin/user/developer`，與目標全域 role 四值一致；`service_admin` 依 §2 定案不進全域 enum，改由 Service Registry 的 per-service 指派承載（目標新增），不需動 `users.role` schema。

`departments` 現況只有 `id/name/description/is_active/created_at/updated_at`，可先作 ownership 與 grant scope；若要支援組織樹、主管批核、department-scoped service admin，需要新增 hierarchy 與 approval relation。

`service_clients` 是非 agent 的 service-to-service caller：`client_type` 目前為 `router`、`worker`、`admin_tool`；已有 encrypted token envelope、lookup hash、previous token hash、cert fingerprint、`is_legacy`、`is_active`、revocation 欄位。migration `0027` 也新增 `idx_service_clients_active_prev_hash`，支援 token rotation cache lookup。

目標 `AuthSession` 在現況沒有對應 table：現行機制是 stateless JWT cookie + `users.token_version` + `token_revocations` table + JWKS（`app/api/jwks.py`）+ Redis revocation pub/sub（`services/token_revocation_publisher.py`）。Identity 重設計應以此為遷移起點，而非假設已有 session table。卡登分支（origin）另有 `auth_providers` / `external_identities` 兩表（OIDC provider 設定與外部身分綁定），列入 Identity 重設計範圍。

### 現況已存在、但未列入 Core Domain Map 的物件（補遺）

以下皆為 origin 現況已存在的 domain objects，目標 map 尚未涵蓋；重設計時應明確決定沿用、吸收或退役，各一句話定位：

| 現況物件 | Table | 一句話定位 |
|---|---|---|
| `UserFact` | `user_facts` | 使用者長期記憶事實（平台 LLM 逐輪抽取，`(user_id, key)` upsert）。 |
| `ConversationMemoryChunk` | `conversation_memory_chunks` | 跨對話記憶 RAG chunk；`is_encrypted` 旗標是機密 latch 繼承的觸發路徑（來源對話 agent `requires_encryption` 時標記，取用方負責把消費對話 latch 進機密狀態）。 |
| `Attachment` | `attachments` | 對話 / 訊息層附件（`filename`、`content_type`、`size_bytes`、`storage_path`）。 |
| `Handoff` | `handoffs` | 對話轉交（user → user / user → agent，`status` + `note`）。 |
| `AgentFunction`（相容別名 `AgentPrompt`） | `agent_functions` | 每 agent 的宣告式功能（`preset_prompt` / `prompt_action`），純資料設定、不含可執行程式碼。 |
| `ApiKey` | `api_keys` | 使用者 API key（§13 `UsageRecord.api_key_id` 已引用，但原 map 未列）。 |
| `TrustedHost` | `trusted_hosts` | SSRF guard 的 host 放行清單（admin 管理）。 |

### Task / conversation bridge

目前沒有 `Task` table。現有 bridge 是：

- `Conversation`：`id`、`user_id`、`agent_id`、`title`、`origin`、`collection_id`、`classified`、`classified_at`、`classified_by`、`classification_inherited`、timestamps。
- `Message`：`id`、`conversation_id`、`role`、`content`、`trace_id`、`latency_ms`、`model_name`、`agent_name`、`metadata`、`rating`、`created_at`。
- `ConversationShare`：分享 token、read-only / fork、expiry、share metadata。

`classified` 是 boolean latch，`classification_inherited` 是 inheritance marker；尚未表達五級分類、分類事件、降級申請或主管批核。`Message.metadata` 可暫存 citation / tool metadata，但不能取代 `SourceSnapshot` / `TraceSpan`。

### Knowledge / ingestion

`ingestion_collections` 現況欄位包含 `name`、`description`、`chunking_config`、`embedding_model`、`embedding_dim`、`status`、計數欄位、`created_by`、timestamps。它是 collection scope 的基礎，但還沒有 owner department、membership、publish / review lifecycle、classification level。

`ingestion_documents` 現況欄位包含 `collection_id`、`filename`、`title`、`normalized_title`、`sha256`、`mime_type`、`bytes`、`storage_path`、`status`、`chunk_count`、`error_message`、`uploaded_by`、`uploaded_at`、`indexed_at`。目前用 `(collection_id, sha256)` 避免重複檔案，但沒有 explicit `version`；若同名檔案重新上傳且內容不同，會是另一個 document row，不是同一 document 的新版。

chunk table 由 `anila-core` 的 `CollectionScopedPgVectorStore` 操作，不在 CSP ORM 建 relationship。實際欄位包含 `collection_id`、`document_id`、`chunk_key`、`content`、`metadata`、`embedding halfvec(4000)`、`token_count`、`parent_chunk_id`、`chunk_type`、`chunk_level`、`created_at`。pgvector store 會用 `SET LOCAL anila.collection_id` 維持 RLS collection scope。

`document_relations` 已支援 rule / LLM / similarity relation edges，欄位包含 `collection_id`、`src_document_id`、`dst_document_id`、`dst_chunk_id`、`target_ref`、`relation_type`、`confidence`、`source`。這可作 SourceGraph seed，但不等於 immutable source snapshot。

### Runtime registry

`model_registry` 已是模型 endpoint SSOT。它支援 `llm/vlm/embedding/agent` model type、router primary flag、health status、internal/external marker、context window 與 base model relation。新設計的 `ModelEndpoint` 可以從這裡擴充，不需要重做。

`agents` 已有 endpoint URL、router description、input schema、capabilities、approval、health、`base_model_id`、`bound_collection_id`、bootstrap token、credential envelope、`runtime_config`。migration evidence：

- `0002_add_agents.py` 建立 `agents` 與 `ix_agents_name`、`ix_agents_approval_status`。
- `0027_agent_credentials_and_service_clients.py` 補 agent credential / bootstrap token 相關欄位。
- `0029_agent_runtime_config.py` 補 `runtime_config`。
- `0038_agent_bound_collection.py` 補 `bound_collection_id`。

因此 Agent Registry 不應重寫，但要補 `runtime_type`、版本化、output schema、trace/audit level、classification ceiling。

### Project Entry / Service Registry

`platform_links` 現況已包含 `required_roles` 與 `is_public`。migration `0012_add_service_access_control.py` 新增 `service_access_grants`，並建立 `ix_service_access_grants_user_id`、`ix_service_access_grants_department_id`、`ix_service_access_grants_platform_link_id`、active user grant partial unique、active department grant partial unique。migration `0013_add_platform_link_is_public.py` 補 `is_public`，既有 rows backfill `true`，新 rows default `false`。

這表示現有 schema 可處理「誰可看見哪個外部入口」，但還不能處理「如何安全啟動該服務」。`RegisteredService` 應從 `platform_links` 漸進擴充，不應另外平行建立一套無法承接 grants 的 service catalog。

### Usage / audit

`token_usage` 現況欄位包含 `api_key_id`、`user_id`、`department_id`、`model_id`、prompt / completion / total tokens、`request_timestamp`、`request_duration_ms`、`conversation_id`、`trace_id`、`request_type`、`caller_agent_id`、`caller_client_id`。migration `0027` 補 caller 欄位與 partial indexes：`idx_token_usage_caller_agent`、`idx_token_usage_caller_client`。目前 usage 可支援模型、embedding、agent / service client 計量，但尚未 task-centric。

`audit_logs` 現況欄位包含 `actor_user_id`、`actor_username`、`action`、`resource_type`、`resource_id`、`status`、`detail`、`ip_address`、`metadata_json`、`created_at`。這是治理操作 audit 的基礎，但 policy decision、classification event、full trace span 仍需新增。

### Studio job 與 `ArtifactVersion`

`anila-studio` 五類 job service 目前不是共用持久化模型；每類 job record 以 `state`（pending / running / done / failed / cancelled）加 `step` 步驟字串表達進度，沒有數值 `progress` 欄位：

- slides：`studio_job_service.py` 用 in-memory `_jobs`，PPTX bytes 存在 process memory。
- reports：`report_job_service.py` 用 in-memory `_jobs`，產物檔案落在 artifacts dir。
- mindmaps：`mindmap_job_service.py` 用 in-memory `_jobs`，SVG / DOT bytes 存在 job record。
- infographics：`infographic_job_service.py` 用 in-memory `_jobs`，HTML / PDF path 落在 artifacts dir。
- datatables：`datatable_job_service.py` 用 in-memory `_jobs`，HTML / CSV / XLSX path 落在 artifacts dir，delete / eviction 會刪檔。

因此目前只能把 Studio job metadata 作為 `ArtifactVersion.generated_by_studio_job_id` 的來源，不能直接對應成可恢復的 `ArtifactVersion`。新設計需要建立 shared persisted `ArtifactJob` / `Artifact` / `ArtifactVersion` store，並讓五類 service 回傳一致的 `artifact_files`、`citation_map`、`source_snapshot_id`、`classification_level`、`trace_id`。

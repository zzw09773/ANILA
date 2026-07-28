# 08. Classified Latch and Policy Engine

> Status: draft v0.1  
> Purpose: 將既有 boolean classified latch 升級為中科院五級分類、單向閂鎖、Admin 降級申請與主管批核流程。  
> Confirmed decisions: 無機密 / 營業秘密 / 機密 / 極機密 / 絕對機密；上鎖後僅 Admin 可降級；需要主管批核。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. Classification Levels

```text
0 無機密
1 營業秘密
2 機密
3 極機密
4 絕對機密
```

排序不可變：

```ts
無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密
```

---

## 2. Core Invariant

任何資料或任務一旦觀測到更高分類，該資源與其衍生物只能維持或升級，不得自動降級。

```text
source classification
+ user prompt classification
+ agent default classification
+ model/tool/service classification
+ memory inherited classification
= effective classification
```

---

## 3. Current Implementation Bridge

既有實作有：

```text
conversation.classified: boolean
classified_at
classified_by
classification_inherited
agent.requires_encryption
anila.meta.classified
frontend classified latch helper
```

新專案 migration bridge：

| 現有欄位 | 新欄位 |
|---|---|
| `classified=false` | `classification_level=無機密` |
| `classified=true` | `classification_level=機密` |
| `requires_encryption=true` | `default_classification_level=機密` |
| `classification_inherited=true` | `inherited_from` event |

> ✅ 已拍板（v0.2）：**backfill 值只是 migration floor（最低安全起點），不是
> 最終分類**。既有加密 Agent 的真實等級可能高於「機密」（極機密／絕對機密），
> 自動 backfill 一律取保守下限，最終等級以人工分類盤點結果為準——見
> §15 的「Classification Inventory Before Cutover」。

```text
backfill level = 最低安全起點
final level    = 人工分類審核結果
```

### 現況 enforcement 面（重要）

五級政策引擎是蓋在一層**非常薄**的 enforcement 基礎上。現況 latch 真正
「擋」的後端行為只有三處：

1. 分享建立：classified conversation 的 `create_share` 回 403
   （`services/conversation_service.py`）。
2. 公開分享頁：已建立的 public share 若對話事後被 classified，檢視時遮蔽
   標題與訊息（`api/public_share.py`）。
3. 機密讀取 audit：GET conversation 時記 `access_classified_conversation`
   audit log —— 只是記錄，不是授權判斷。

其餘皆未 enforce：

- 匯出控制只在前端：ANILA UI 的匯出是純 client-side（走一般
  `GET /api/conversations/{id}` 再組 JSON/Markdown 下載），後端沒有任何
  export gate。
- 搜尋仍洩漏 classified 對話**標題**：搜尋（限本人對話）對 classified
  對話只抑制 snippet，標題照常回傳，且仍可由訊息內文命中
  （`api/conversations.py`）。
- Memory 檢索不過濾 encrypted chunks：`retrieve_relevant_chunks` 自由召回
  encrypted chunk，之後才 latch conversation —— 是「先讀再上鎖」，不是
  讀取前擋下。
- `requires_encryption` 從不阻擋 routing：classified 對話仍可呼叫任何
  model/agent/tool；proxy 只做 latch，沒有任何 ceiling 檢查。
- Ingestion 層（collections / documents / chunks）**完全沒有分類欄位**
  （`models/ingestion.py`）；RLS 只存在於 ingestion 表
  （`document_chunks` / `ingestion_images` / `document_relations`，以
  `anila.agent_id` GUC 的擁有者隔離為鍵，與分類無關）；`conversations`
  表沒有 RLS。
- 現況**沒有任何人（包含 admin）能降級**：`/declassify` 端點已被刻意
  移除（`api/conversations.py` 註解明講它是 pre-existing security
  backdoor），而 `classify_conversation` docstring 的
  「irreversible by non-admin」是誤導 —— 實際上 admin 也沒有降級路徑。

因此本檔 §7–§10 的降級流程與 policy enforcement 幾乎全部是「目標新增」，
不是對既有 enforcement 的延伸；migration 規劃要以此為起點估工。

---

## 4. Classification Propagation

### Task

```ts
task.level = max(
  user_requested_level,
  source_snapshot.level,
  selected_agent.default_level,
  selected_service.default_level,
  memory_inherited_level,
  policy_detected_level
)
```

### Artifact

```ts
artifact.level = max(
  task.level,
  source_snapshot.level,
  generated_content_detected_level
)
```

### Service Launch

```ts
service_launch.level = max(
  task.level,
  service.default_level,
  source_snapshot.level
)
```

---

## 5. Resource Fields

All core resources need:

```text
classification_level
classification_latched_at
classification_source
classification_event_id
```

Applied to:

- Task
- Conversation
- Message
- SourceSnapshot
- Collection
- Document
- Chunk
- AgentRun
- Artifact
- ServiceLaunch
- ExportRecord

---

## 6. ClassifiedLatch Event

```ts
ClassificationEvent {
  id: string
  resource_type: string
  resource_id: string
  previous_level: ClassificationLevel
  new_level: ClassificationLevel
  reason:
    | "source_selected"
    | "agent_policy"
    | "memory_inherited"
    | "manual_admin"
    | "service_policy"
    | "content_detection"
    | "declassification_copy"

  actor_user_id?: number
  inherited_from_resource_type?: string
  inherited_from_resource_id?: string
  trace_id?: string
  created_at: string
}
```

---

## 7. Downgrade Policy

使用者確認：

```text
上鎖後僅 Admin 可降級，且需要主管批核。
```

設計原則：

1. 一般使用者不可申請降級。
2. Developer 不可申請降級。
3. Service Admin 不可申請降級，除非同時具 Admin。
4. Admin 可建立降級申請。
5. 主管核准後才可生效。
6. 建議不要修改原物件，而是建立降密副本。
7. 若業務要求原物件降級，必須保留完整 event log。

> ✅ 已拍板（2026-07-02，防自我核准迴圈；同日補拍板採**變體 A**——系統內不做自我核准碼路徑）：
>
> 1. **降級申請人與核准人不得為同一人**（雙人原則），**系統內無例外**——
>    不存在任何自我核准碼路徑。組織最高權責者（如院長）的例外以
>    **紙本核定＋代錄**實作：院長書面核定降級後，由持「機密審批權責」者
>    將該核定登錄進系統（`approved_via="recorded_paper_decision"`，
>    必附核定文號與核定者官職姓名）。系統內仍是兩個帳號
>    （申請人 ≠ 代錄人），權威來源是紙本核定。
> 2. **核准權與平台角色脫鉤**：降級核准權**不**隨平台 `owner`/`admin` 技術角色
>    自動取得——平台 owner 可能只是一般職員，技術角色不等於保密權責。核准權
>    來自「機密審批權責」指派（組織資料：主管鏈或指定審批人，per-department
>    或全域）。平台 owner 負責管理「誰持有審批權責」的指派，該指派動作本身
>    必須入 audit；owner 本人未被指派權責時，不得核准降級。
> 3. **權責指派的信任錨**（回應「系統怎麼知道誰是院長／誰有權責」——系統
>    無從得知，職位是系統外事實，憑證卡只證明身分（員編）不證明職位，
>    故權威一律來自行政程序，系統只負責把指定記錄好、鎖好）：
>    - 指派「機密審批權責」必附核定依據（公文文號／簽呈），成為紀錄一部分。
>    - 指派動作本身雙人控制：owner 登錄＋另一名 admin 確認
>      （或於部署 bootstrap 時見證完成）。
>    - 指派異動＝高敏 audit event＋治理中心顯眼公告，不可靜默變更。
>    - 定期覆核：產出權責指派清單供保密單位核對（attestation）。
> 4. **fail-closed**：無主管資料且無可用權責者時，申請維持 pending，
>    不升級到技術 owner、不自動放行。
>
> 與 §12 的 escalation 規則一致（§12 已同步改）。

---

## 8. DeclassificationRequest

```ts
DeclassificationRequest {
  id: string
  resource_type: string
  resource_id: string

  from_level: ClassificationLevel
  to_level: ClassificationLevel

  requested_by_admin_id: number
  reason: string
  proposed_redaction_summary?: string

  status:
    | "pending_supervisor"
    | "approved"
    | "rejected"
    | "cancelled"
    | "applied"

  supervisor_user_id?: number
  supervisor_comment?: string
  decided_at?: string

  // ✅ 已拍板（2026-07-02，變體 A）：核准路徑二選一，皆為兩個相異帳號
  approved_via?: "in_system" | "recorded_paper_decision"
  // recorded_paper_decision（紙本核定＋代錄）時必填：
  authority_reference?: string      // 核定依據（公文文號／簽呈）
  authority_title_name?: string     // 核定者官職＋姓名（如：院長 ○○○）
  recorded_by_user_id?: number      // 代錄人（必須持「機密審批權責」，且 ≠ 申請人）

  resulting_resource_id?: string
  audit_event_ids: string[]
  created_at: string
}
```

> 不變量：`requested_by_admin_id ≠ supervisor_user_id`、
> `requested_by_admin_id ≠ recorded_by_user_id`。系統內**沒有**自我核准欄位
> 或碼路徑（變體 A）；最高權責者例外一律走 `recorded_paper_decision`。

---

## 9. Recommended Downgrade Mode

### Preferred: Declassified Copy

```text
artifact_A level=機密
→ admin request downgrade to 營業秘密
→ supervisor approve
→ create artifact_B level=營業秘密
→ artifact_A remains 機密
```

優點：

- 原始 audit 不被破壞。
- 不會造成歷史 trace 指向改變。
- 可在 artifact_B 記錄 redaction summary。

### Exceptional: In-place downgrade

只允許：

- DB resource 本身不可複製。
- 安全政策允許。
- Supervisor approval 完成。
- Audit event 明確記錄。

---

## 10. Policy Enforcement

### Model

```text
allow if task.level <= model.classification_ceiling
```

### Agent

```text
allow if effective_task_level <= agent.classification_ceiling
```

### Service

```text
allow if launch.level <= service.classification_ceiling
```

### Export

```text
allow if target_space.classification_floor >= artifact.level
```

---

## 11. UI Behavior

### ANILA UI

- 每個 task/conversation 顯示 classification badge。
- 一旦升級，badge 不可在一般 UI 中降回。
- 若回覆或 artifact 因 policy deny，顯示可理解原因。
- 不顯示敏感內部 policy 細節。

### ANILALM（現況盲點）

ANILALM 的 chat 已送 `X-ANILA-Conversation-Id`（`ANILALM/src/api/chat.ts`），
所以它的對話在伺服器端**會**正常 latch；型別也帶 `classified`
（`ANILALM/src/types.ts`）。但 ANILALM 前端**完全沒有** classified UI ——
沒有浮水印、沒有 banner、也沒有動作停用。多級分類的 UI 工程必須同時
涵蓋 ANILA_UI、ANILALM 與 CSP 三個前端，不是只改 ANILA_UI。

### CSP

新增「機敏分類」區：

```text
分類事件
降級申請
待主管核准
分類政策
資源查詢
```

---

## 12. Supervisor Approval

主管定義可由部門資料決定：

```ts
Department {
  id
  supervisor_user_id
}
```

若無 supervisor（✅ 已拍板 2026-07-02 —— 與 §7 防自我核准迴圈一致）：

- 申請**維持 pending（fail-closed）**，並 audit `supervisor_missing`。
- **不**升級到平台技術 owner——核准權來自「機密審批權責」指派（見 §7 第 2 點），
  平台 owner 未持有權責時不得核准；也不得由任一 admin group 成員核准
  （避免 Admin 申請人由同儕或自己核准）。
- 解套方式：由持有權責者在系統內核准（`approved_via="in_system"`），或
  取得最高權責者（如院長）的紙本核定後由持權責者代錄
  （`approved_via="recorded_paper_decision"`，附文號——見 §7 變體 A 與 §8 欄位）。
  兩條路徑都維持申請人 ≠ 核准人／代錄人。

現況資料模型明講：`departments` 與 `users` 目前**都沒有任何主管欄位**
（見 §16.5）——「主管批核」需要先新增組織資料（supervisor 欄位或部門
層級）、「機密審批權責」指派表與對應 migration 才能落地。

---

## 13. Audit Events

```text
classification.latched
classification.level_raised
classification.downgrade_requested
classification.downgrade_approved              # approved_via 記錄 in_system / recorded_paper_decision
classification.downgrade_recorded_paper_decision  # 代錄紙本核定（附文號、核定者、代錄人）
classification.downgrade_rejected
classification.declassified_copy_created
classification.in_place_downgrade_applied
classification.authority_assignment_changed    # 「機密審批權責」指派異動（高敏：雙人控制＋公告，見 §7）
```

---

## 14. Tests

必測：

- 無機密 → 機密後不可自動回無機密。
- 記憶繼承機密會讓新 conversation 升級。
- Agent default level 會升級 task。
- Artifact 繼承 source snapshot level。
- Admin 以外無法申請降級。
- Admin 申請後未經主管核准不可生效。
- 降密副本不修改原物件。
- 高分類 task 不可呼叫低 ceiling model/agent/service。

---

## 15. Migration

### Step 1

Add nullable `classification_level` to current classified resources。

### Step 2

Backfill:

```text
classified=false/null → 無機密
classified=true → 機密
```

### Step 3

Keep boolean fields as compatibility read model。

### Step 4

Move UI to `classification_level`。

### Step 5

Deprecate boolean-only writes。

### Classification Inventory Before Cutover（✅ 已拍板 v0.2）

切到五級分類前，必須完成**人工分類盤點**：

```text
所有既有 Agent / Collection / PlatformLink（RegisteredService）/ ModelEndpoint
在切到五級分類前，必須完成人工分類盤點並記錄盤點結果（盤點人、日期、依據）。

未盤點資源只能使用 migration floor（backfill 值），在人工盤點完成前：
- 不得處理高於「機密」的任務。
- 不得被標為「極機密」或「絕對機密」可用能力。
- 不得作為極機密／絕對機密 Task 的派發目標（policy engine 以
  classification_ceiling=機密 視之，fail-closed）。
```

盤點結果本身入 audit（`classification.level_raised` 或新盤點事件），
與 §3 的「backfill=floor、final=人工審核」原則一致。

---

## 16. Repo evidence / 現況補齊

### 16.1 `conversations` classified 欄位

現況定義在 `myCSPPlatform/backend/app/models/conversation.py`：

- `classified: bool`，not null，default false。
- `classified_at: DateTime | null`。
- `classified_by: FK users.id | null`，刪除 user 時 `SET NULL`。
- `classification_inherited: bool`，not null，default false。

Migration 來源：

- `0004_add_conversations_shares_attachments_handoffs.py` 建立
  `classified`、`classified_at`、`classified_by`。
- `0031_conversation_classification_inherited.py` 新增
  `classification_inherited`，用來區分「agent requires encryption」與
  「讀到 encrypted memory chunk 後升級」。

API read model 在 `myCSPPlatform/backend/app/api/conversations.py` 的
`ConversationOut` 會回傳 `classified`、`classified_at`、
`classification_inherited`。目前沒有 `classification_level`、
`classification_event`、declassification request 或 supervisor approval table。

### 16.2 後端 latch 行為

`myCSPPlatform/backend/app/services/conversation_service.py`：

- `classify_conversation` 只做升級；已 classified 會回 409。
- `declassify_conversation` 已移除，註解明確標示 classified 是 one-way latch。
- `create_share` 對 classified conversation 回 403，禁止分享。

`myCSPPlatform/backend/app/api/proxy.py`：

- `_latch_agent_classification` 在 resolved agent `requires_encryption=true`
  時，把 conversation 設為 `classified=true`。
- `_latch_inherited_classification` 在 memory read 命中 encrypted chunk 時，
  設 `classified=true` 並 `classification_inherited=true`。
- `/v1/chat/completions` 透過 `X-ANILA-Conversation-Id` 將 runtime 呼叫對回
  conversation row；沒有這個 header 時，後端無法持久化該對話的 latch。

`myCSPPlatform/backend/app/services/proxy_service.py`：

- `build_default_anila_meta` 產生 `anila_meta.classified`。
- `proxy_stream` 若 `requires_encryption=true`，會把 downstream
  `anila.meta.classified` OR 成 true；若 agent 沒送 meta，也會補預設
  `anila.meta`。

### 16.3 Memory encrypted chunk propagation

`myCSPPlatform/backend/app/services/memory_service.py` 與
`anila-core/src/anila_core/memory/long_term/models.py` 是目前 memory 傳遞來源：

- `ConversationMemoryChunk.is_encrypted` 在
  `myCSPPlatform/backend/app/models/user_memory.py` 定義；migration
  `0030_add_user_memory.py` 建立欄位。
- `persist_turn(..., is_encrypted=...)` 會把當次 user / assistant chunk 以相同
  `is_encrypted` 寫入 long-term memory。
- `retrieve_relevant_chunks` 回傳每個 chunk 的 `is_encrypted`。
- `MemoryReadResult.encryption_inherited` 是 `any(chunk.is_encrypted)`。
- `_inject_memory` 若讀到 encrypted chunk，`api/proxy.py` 會呼叫
  `_latch_inherited_classification`，並將本次寫入 memory 的
  `is_encrypted` 一併升級。

因此現況分類傳遞是 boolean 且 one-hop/one-way：只要引用過任一 encrypted
memory chunk，conversation 與後續記憶 chunk 都會升級為 classified。

### 16.4 ANILA UI classified helper / banner / tests

Runtime UI 目前相關檔案：

- `ANILA_UI/anila-ui/src/runtime/classified.js`：
  `computeConversationClassified`、`appendClassifiedTag`、
  `latchConversationWithMeta`，保證 agent / meta / prior classified 只能升級，
  不能被 `meta.classified=false` 降級。
- `ANILA_UI/anila-ui/src/runtime/messageMeta.js`：
  `buildPersistMeta` 會保留 final meta 或 message state 的 `classified=true`。
- `ANILA_UI/anila-ui/src/runtime/classifyRetryQueue.js`：
  frontend 收到 `anila.meta.classified=true` 但 conversation id 尚未穩定時，
  先排隊，稍後補打 `POST /api/conversations/{id}/classify`。
- `ANILA_UI/anila-ui/src/runtime/sse.js`：
  streaming request 會在 numeric conversation id 存在時送
  `X-ANILA-Conversation-Id`，並把 `anila.meta` 交給 UI callback。
- `ANILA_UI/anila-ui/src/app.jsx`：
  顯示 `ConfidentialWatermark`、`密等鎖定` badge（2026-07-26 / W1-3 前標示為
  「加密」，措辭已更正：`requires_encryption` 不做內容加密，它是單向密等
  latch）、inherited banner；
  classified conversation 禁用分享，並在 meta classified 時呼叫 classify API。
- `ANILA_UI/anila-ui/src/chat.jsx` / `trust.jsx`：
  顯示 classified 角標，並在 classified 時停用 copy/share/edit/feedback 等動作。
- Tests:
  `ANILA_UI/anila-ui/src/__tests__/classified.test.js`、
  `messageMeta.test.js`、`sse.test.js` 覆蓋 one-way latch、meta 不降級、
  persist meta 與 SSE named events。

現有 UI banner 仍是 boolean classified 模型；若升級五級分類，需同步改上述
helper、meta persistence、SSE meta handler、conversation mapper、badge/banner
文字、停用規則與測試。

### 16.5 Department supervisor 欄位

`myCSPPlatform/backend/app/models/department.py` 目前只有
`id`、`name`、`description`、`is_active`、`created_at`、`updated_at`。
`myCSPPlatform/backend/app/models/user.py` 只有 `department_id` 關聯，沒有
`supervisor_user_id`、department hierarchy、主管簽核角色或 delegation 欄位。

因此「主管批核」是 redesign 新需求；需要新增資料模型、授權規則與 migration，
不能假設現有 department schema 已支援。

### 16.6 Migration 注意事項

現有 latch 已在後端與 UI 兩邊落地為 boolean one-way 行為。升級到五級分類時：

- `classified=true` 應保守 backfill 為 `機密`，避免降級。
- `classification_inherited=true` 應轉成 event/source，而不是只當 UI 文案。
- `anila.meta.classified` 仍需保留 compatibility read model，直到 UI 全面改讀
  `classification_level`。
- 目前 UI inherited banner 文案提到可刪除加密記憶引用，但後端 row latch
  仍是 one-way；redesign 需明確定義刪除引用後是否只影響未來 memory search，
  或需要走正式降級申請。

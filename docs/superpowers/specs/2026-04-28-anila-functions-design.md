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
- **Live Test Console**：dev 在 UI 上直接跑、看 events stream，但 `execute` event 預設不真 eval
- **Audit log**：每次 Action 觸發保留 360 天（含完整 events_json，redact 後）
- **RBAC**：`developer` 寫，所有 role 用；`admin` 可 disable / 設 admin Valves
- **Worker isolation**：獨立 `anila-functions-worker` container，subprocess per run，rss 256MB / 30s timeout / non-root
- **Event types**：`status`、`execute`、`message`、`citation`、`error`（+ runtime sentinel `__done__`）

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
│  anila-functions-worker (NEW service)                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  POST /exec  body: {code, body, valves, user, metadata} │   │
│  │  → spawn subprocess (python -u runtime.py, non-root)    │   │
│  │  → SSE stream events as Action emits                    │   │
│  │  preinstalled: httpx requests pydantic python-dateutil  │   │
│  │  Resource limits: 256MB rss, 30s timeout, 32 nproc      │   │
│  │  Concurrency: pool of 8 simultaneous runs               │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**Component summary**：

| Component | New / Existing | Role |
|---|---|---|
| `anila-ui` (`ANILA_UI/anila-ui`) | Existing, extended | 加 `/admin/functions/*` 路由與 ChatRuntime button render |
| CSP (`myCSPPlatform`) | Existing, extended | 加 4 張表 / endpoint / SSE relay logic |
| `anila-functions-worker` | NEW | Stateless executor，subprocess sandbox |
| PostgreSQL | Existing | 多 4 張表（CSP DB 內） |

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
| `status` | `enum('draft','enabled','disabled')` | dev 寫到一半 = draft；admin 可 disable |
| `latest_version_id` | `bigint FK versions` | denormalized current version |
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

### 3.5 Marketplace 模型

不另起表。每筆 `action_functions` 對所有 logged-in user 可見。「fork」=「INSERT 新 row + `forked_from_id`」+「duplicate code 為新 fork 的 v1」。沒有 published/template 二元狀態。

---

## 4. API（CSP `/api/functions/*`）

所有 endpoint 走 CSP 既有 JWT cookie + role middleware。

### 4.1 Function CRUD

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions?author=me&status=enabled&tag=lint&q=...` | any | 列表 |
| `POST` | `/api/functions` | `developer`+ | 建新 |
| `GET` | `/api/functions/:slug` | any | Function + 最新 version code + valves schema |
| `PATCH` | `/api/functions/:slug` | author or `admin` | 改 metadata（不改 code） |
| `DELETE` | `/api/functions/:slug` | author or `admin` | v1 設 `status='disabled'` |

### 4.2 Versions

| Method | Path | Role | 說明 |
|---|---|---|---|
| `POST` | `/api/functions/:slug/versions` | author or `admin` | 存新版本 |
| `GET` | `/api/functions/:slug/versions` | any | 版本列表 |
| `GET` | `/api/functions/:slug/versions/:version_no` | any | 拿特定版本 code |

### 4.3 Valves（加密）

| Method | Path | Role | 說明 |
|---|---|---|---|
| `GET` | `/api/functions/:slug/valves` | author or `admin` | secret 欄位回 `{has_value: true}` 不回明文 |
| `PUT` | `/api/functions/:slug/valves` | `admin` | upsert |

### 4.4 Marketplace

| Method | Path | Role | 說明 |
|---|---|---|---|
| `POST` | `/api/functions/:slug/fork` | `developer`+ | 複製成自己的 |

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

SSE 回傳：
```
event: function_event
data: {"type":"status","description":"開始執行","done":false}

event: function_event
data: {"type":"execute","code":"document.querySelector(...)..."}

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

```
Browser                        CSP /run                        worker /exec
   │  POST {action_id, context}   │                                │
   ├──────────────────────────────▶│                                │
   │                              │  RBAC / status / test_mode 檢查 │
   │                              │  從 DB 拉 code + valves(decrypt)│
   │                              │  INSERT runs row(running)      │
   │                              │  POST /exec + X-Functions-Secret│
   │                              ├──────────────────────────────▶│
   │                              │                                │  spawn subprocess
   │  SSE: event=function_event   │  SSE chunks (redacted)         │  runtime.py
   │ ◀────────────────────────────┤ ◀──────────────────────────────┤
   │   ...                        │   ...                          │   ...
   │                              │  data={"type":"__done__"}     │
   │                              │ ◀──────────────────────────────┤
   │                              │  redact + UPDATE runs(success) │
   │  event=function_done         │                                │
   │ ◀────────────────────────────┤                                │
```

### 5.2 Worker `/exec` handler（要點）

- 收到 ExecRequest 驗 `X-Functions-Secret`
- spawn `python -u runtime.py`，stdin pipe 餵 JSON request
- preexec_fn 套 rlimits（rss 256MB、cpu 30s、nproc 32）
- StreamingResponse async 把 stdout 每行轉成 SSE `data:` line
- 結束 wait 2s 後強制 wait

### 5.3 `runtime.py`（worker image 內建 wrapper）

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

### 5.4 Reserved args

| 參數 | 形狀 | 來源 |
|---|---|---|
| `body` | `{action_id, conversation_id, message_id, message_content, selected_text}` | request |
| `__event_emitter__` | `async (event: dict) -> None` | runtime |
| `__user__` | `{id, username, email, role}` | CSP JWT 解出 |
| `__metadata__` | `{conversation_id, message_id, model_name, agent_name, started_at}` | CSP DB lookup |

### 5.5 SSE event types（瀏覽器端 dispatch）

| `data.type` | 瀏覽器行為 |
|---|---|
| `status` | transient toast：`{description, done}` |
| `execute` | `new Function(code)()` 在 tab 跑 JS（正式模式才 eval；Test Console 預設不 eval） |
| `message` | conversation 插入 system 訊息（render 走既有 markdown sanitize） |
| `citation` | push CitationsDrawer state |
| `error` | 紅 toast、不中斷 |
| `__done__` | runtime sentinel；不送 browser，CSP 收到才 emit `function_done` |

### 5.6 Resource limits

| 限制 | 值 | 觸發 |
|---|---|---|
| Wall clock | 30s | SIGKILL → SSE error + function_done(timeout) |
| Memory (RSS) | 256 MB | OOM kill → SSE error: out_of_memory |
| CPU | 30s ulimit | 同 wall clock |
| Network | 不限（v1） | 風險靠 audit + RBAC |
| Filesystem | 容器內 `/tmp/<run_id>/`，run 結束清掉 | — |
| Subprocess UID | 非 root（`nobody`） | docker isolation |
| Concurrent runs | 8（worker pool semaphore） | 第 9 個 → error: queue_full |

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
│  10:42:01.456  execute    [JS preview, 120 chars]    [⤒]   │
│  10:42:01.500  status     {"description":"完成","done":true} │
│  10:42:01.502  __done__   run_id=789  duration_ms=268        │
│ [Open audit detail →]                                      │
└────────────────────────────────────────────────────────────┘
```

`execute` event 預設只 preview，要 dev 明確點 `[⤒] Run anyway` 才 eval。

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
| 看 Functions 列表 / Library | ✅ | ✅ | ✅ |
| 看單一 Function code（read-only） | ✅ | ✅ | ✅ |
| 用 enabled Function | ✅ | ✅ | ✅ |
| 看 enabled-actions | ✅ | ✅ | ✅ |
| 建新 Function | ❌ | ✅ | ✅ |
| 修改 own Function | ❌ | ✅ (own) | ✅ (any) |
| Fork | ❌ | ✅ | ✅ |
| Disable | ❌ | own only | any |
| 設定 admin Valves | ❌ | ❌ | ✅ |
| 看 audit runs | ❌ | own only | any |
| Test mode run（disabled） | ❌ | own only | any |

### 7.2 Threat Model

| ID | 威脅 | 影響 | 緩解 |
|---|---|---|---|
| T1 | Developer 寫 RCE | worker container 受影響 | 獨立 container、subprocess 限 256MB/30s/non-root、audit 全文 |
| T2 | Developer 注入 XSS via `execute` | 偷 user cookie / token | RBAC 限 author=developer；audit 全文留存；Test Console 預設不 eval；`new Function(code)()` scope 隔離 |
| T3 | Test mode 逃逸 | 繞過 admin disable | server enforce：test_mode=true + status≠enabled → 403 unless author/admin |
| T4 | 無限迴圈 / fork bomb / OOM | DoS worker | rlimit + nproc + 8-concurrent semaphore → queue_full |
| T5 | 子程序 escape worker container | 影響 host | read-only rootfs，`nobody` user，no-new-privileges，不掛 docker.sock |
| T6 | Valves XSS | XSS 拿 admin token | values JSON-stringify 顯示；description 走既有 markdown sanitize |
| T7 | Token 出現在明文位置 | Credential 外洩 | admin Valves AES-256-GCM at rest；UI password input + `••••••••`；GET 不回明文；audit redaction match by plaintext substring |
| T8 | CSP↔Worker 沒驗證 | bypass CSP | shared secret `X-Functions-Secret`；worker 只 listen docker compose internal network |
| T9 | Marketplace 釣魚 | 誤觸發惡意 button | UI 強制顯示 author + forked_from；首次出現 "new" 標示 |
| T10 | SQL injection（slug / tag） | DB compromise | ORM + parameterized；slug regex；tag 限 20 字元 |

### 7.3 加密 / Key 管理

- `ANILA_FUNCTIONS_VALVES_KEY`：256-bit base64，CSP container env，不入 git
- AES-256-GCM with random nonce per encrypt
- Key rotation v1：手動 migration script（new key → re-encrypt → bump key_version；舊 key 仍解 `key_version=1` 的 row）
- 自動 rotation v2

### 7.4 Audit redaction（T7 兜底）

CSP 收到 worker SSE chunk 後 → push 到 SSE-to-browser 之前 → 寫 audit row 之前：

1. 從 DB 取出該 run 對應 valve 的 secret 欄位明文（解密後）
2. Pattern match 替換 chunk text：substring match → `<redacted:valves.field_name>`
3. Redaction 失敗 → events_json 寫 `[REDACTION_FAILED]` + 觸發告警 metric
4. Secret < 8 chars 不 match（避免誤殺通用字串）

### 7.5 Classified conversation 行為

- Function 跑在 classified conversation 裡：runs row inherit classified flag；audit 列表對 non-cleared user 隱藏
- v1 不額外標記「Function 是 classified」；v2 視需要加 `actions.requires_clearance`
- v1 不阻止 `execute` event 把 classified 訊息內容 leak 到外網（dev 自律 + audit）

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
- Versions append-only on save
- Valves encryption at rest
- GET valves 不回 secret 明文
- RBAC：user 角色 POST function → 403
- Marketplace fork
- Run row finalize：events_json redacted

### 8.3 Worker（pytest）— v1 必

- `runtime.py` 跑 valid Action class
- 擋無 `class Action`
- Reserved args 注入正確
- `__event_emitter__` async + 多次 emit 順序保留
- timeout / OOM / non-root / 8-concurrent / queue_full

### 8.4 端對端（Vitest + Playwright）— v1 必

- developer 寫 fill-text Function、save、enabled、chat toolbar 出現
- 點 button SSE events 串流到 DOM
- Test Console `execute` 預設不 eval、`Run anyway` 才 eval
- user 角色看到 list 但無 New Function CTA
- Fork：library → fork → my tab + forked_from
- Valves secret 欄位填入後再進來看到 `••••••••`
- Disabled function：button 不出現在 chat
- Audit detail 顯示 redacted token

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
| `function_save_total{author}` | governance |

---

## 9. Deployment / Rollout

### 9.1 New service in docker-compose

新增 `anila-functions-worker`：
- Base image：`python:3.12-slim`
- Preinstalled：`fastapi`, `uvicorn`, `httpx`, `requests`, `pydantic`, `python-dateutil`, `cryptography`
- Container：`--read-only` rootfs（除 `/tmp`）+ `--security-opt=no-new-privileges` + non-root user `nobody`
- Network：only on compose internal network、不 expose 對外
- ENV：`X_FUNCTIONS_SECRET`

### 9.2 CSP migrations

3 個 alembic migration（一支 PR 內）：

1. 建表：`action_functions`、`action_function_versions`、`action_function_valves`、`action_function_runs`
2. Trigger：versions UPDATE/DELETE 拒絕
3. Initial empty state（不 seed）

### 9.3 ENV 新增

| ENV | 哪個 service | 用途 |
|---|---|---|
| `ANILA_FUNCTIONS_VALVES_KEY` | CSP | AES-GCM 對 valves 加解密 |
| `ANILA_FUNCTIONS_WORKER_URL` | CSP | worker `/exec` URL（compose internal） |
| `X_FUNCTIONS_SECRET` | CSP + worker | shared secret |

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
- [ ] CSP `audit_logs` 高階紀錄的 actor / target 欄位要怎麼填？需確認既有 schema 對「FUNCTION_RUN」事件的容量
- [ ] Monaco bundle size：bundle 進去會讓 anila-ui build 變大幾 MB？需要 dynamic import 讓非 admin 路由不載
- [ ] `nobody` UID 在 worker container 裡是否需要 chown `/tmp/<run_id>`？需要時 entrypoint script 處理
- [ ] Network egress policy：v1 不限，但要不要先在 docker compose 設 outbound proxy whitelist 為日後鎖網準備

---

## 11. Implementation Sequencing（草案，待 writing-plans skill 細化）

預估 3 sprint（4-4.5 週）：

**Sprint 1** — Backend core
- Alembic migrations（4 表 + trigger）
- CSP endpoint：CRUD / versions / fork / enabled-actions
- AES-GCM helper + Valves endpoint（加密讀寫）
- Unit + integration tests

**Sprint 2** — Worker + SSE relay
- `anila-functions-worker` 服務（FastAPI + runtime.py）
- CSP `/run` SSE relay + redaction pass
- Worker tests + end-to-end SSE flow test
- Docker compose 更新

**Sprint 3** — Frontend
- `/admin/functions/*` 路由與頁面（list / editor / Test Console / audit）
- Monaco bundle（vite plugin）
- ChatRuntime 整合（toolbar button render）
- Vitest + Playwright E2E
- Dogfood 1-2 位 dev

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

# ANILA 平台整合計畫 (v7 — Runtime-first / Router-first)

## Context

三個子專案（onyx / myCSPPlatform / AgenticRAG）已合併進單一 repo 但尚未真正整合。目標是建立 **LLM-as-Router + Agent Registry + 開發者生態** 的多租戶 AI 平台：

- **使用者**初期透過 Router API / curl / OpenWebUI 驗證 query flow；`ANILA UI` 仍是必要模組，但會在 Router/CSP 穩定後再實作，且 UI/UX 設計由 Claude Design 負責
- **開發者**用 `anila-core` Python runtime foundation 打造各單位所需 agent（可含單位知識 / 流程 / tool），再註冊到平台
- **控制面**沿用 `myCSPPlatform/frontend`，負責登入、API Key、developer console、agent 管理
- **API Key / 權限 / 配額 / 計費**由 myCSPPlatform 統一治理
- **Onyx** 先不進近期實作；目前只保留未來模組規劃文件。待相關單位 API 完成後，再評估作為「跨系統 workflow / compliance agent backend」納入

**關鍵認知**：`AgenticRAG` 的 Python package `anila-core` 應去 `RAG-first` 化，升格成 **ANILA Core 的 Python runtime foundation**。ANILA Core Router 是一個**用這個 runtime 打造、配置成 dispatcher 角色的新 service**，不是 from scratch 重寫；RAG 只是其中一種可選 agent 能力，不是 runtime 本體。但必須注意：**`anila-core` 目前只有 `/chat` + `/agentic-chat` 自訂 schema，Router 需要新增 OpenAI-compatible 入口**（SDK 裡還沒有 `build_app(mode="router")` 這種東西，不能假設）。

`AgenticRAG/api.py` (port 24786) 是**已經 OpenAI-compatible** 的獨立 surface，可直接當第一個 registered agent 範例；不要和 SDK 的 `api/server.py` 混為一談。

## Design Decisions（已跟用戶對齊，全計畫基礎）

1. **CSP = Data plane**：所有 LLM / agent 流量（Router 呼叫主 LLM、Router dispatch agent、agent 內部 LLM 呼叫）**一律經 CSP proxy**。Usage 在 CSP 一次結算。Router / agent **不持有** upstream key，只持有 CSP 發的 Key。

2. **Registry 拆成兩張表**（用戶選 hybrid）：
   - `model_registry` 保留，只管原始 LLM / embedding / upstream model endpoint
   - 新增 `agents` 表管理 endpoint-based registered agent（owner / approval / manifest / health / visibility）
   - `agents.base_model_id → model_registry.id` FK 保留「此 agent 內部依賴哪個基礎模型」的關聯

3. **SDK 裡 registry 也要拆**：
   - `LocalAgentDefinition`：SDK 內部 coordinator sub-agent 定義（YAML / MD / tool allowlist / max_turns / system prompt）
   - `RemoteAgentManifest`：Router 從 CSP fetch 的遠端 agent 快取（endpoint URL / description_for_router / input_schema）
   - 獨立型別、獨立模組，底下若共用介面用 `AgentSpec` 抽象，**不要硬塞同一個 class**

4. **角色模型**：新增 `developer` role，由 admin 指派；developer 登入後可註冊 agent、查看審核狀態、下載官方 `anila-core` 模板，agent 仍需 admin 批准後才能被其他 user 發現 / 呼叫

5. **MVP 範圍**：先打通 `user → Router → CSP → registered agent` 一條線，初期可用 `api.py` 當第一個 sample agent；並保留 `myCSPPlatform/frontend` 作為正式控制面。`ANILA UI` 不是 Phase 1-3 的 blocker，但屬於後續必做模組

6. **認證契約（三層 credential，必須區分清楚）**：
   - **管理面 / control plane（CRUD / approve）**：`/api/auth/*`, `/api/users/*`, `/api/agents` CRUD, `/api/agents/{id}/approve|reject`, `/api/keys/*`, `/api/users/{id}/allowed-agents` 等 → 一律 **JWT access token**（user session）
   - **資料面 / data plane（runtime 讀 / 呼叫）**：`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, **`/v1/agents`（list available agents for the API-key's owner）** → 一律 **CSP API Key**（`sk-...`）。Router / agent 不持 upstream LLM key，只持 CSP API Key
   - **CSP → downstream agent**：用 **service-to-service credential**（CSP 有自己的 service token 或 mTLS；agent 只驗「來自 CSP」）；真實使用者身份透過 **trusted forwarded identity headers** 傳遞（`X-ANILA-User-Id`, `X-ANILA-User-Email`, `X-ANILA-User-Groups`）。downstream agent **不驗 user JWT 也不驗 user API key**，只信 CSP 注入的 header 做 ACL / quota

7. **責任邊界**：`AgenticRAG` 升格為 `ANILA Core` 的 Python runtime foundation，也是各單位開發 knowledge / workflow agent 的主線與預設 SDK；未來若納入 `Onyx`，其角色是 specialized workflow / compliance backend，用於跨系統任務協作，不是所有知識 agent 的預設底座

8. **規則與權限邊界**：`Onyx`（或任何 downstream agent）可以協助做規定檢查、表單補完、流程對話，但**不是最終權限來源**。硬性商業規則與最終授權判斷應由 deterministic checks、CSP service credential、以及目標業務系統共同承擔，不能只靠 LLM 判定

9. **UI 分工**：`myCSPPlatform/frontend` 是控制面；`ANILA UI` 是 end-user runtime client，兩者不合併。`ANILA UI` 的 UI/UX 規格、畫面風格、互動稿由 Claude Design 負責，工程實作再依設計稿接到 Router / CSP

10. **主循環原則**：`ANILA` 主循環是 router / orchestrator，不預設內建業務 RAG。主循環負責意圖判斷、agent 選擇、workflow 協調、狀態維持與串流回傳；真正的 RAG 能力下放到各 agent，只有需要知識檢索的 agent 才實作 RAG

11. **設計參考來源**：`/home/c1147259/下載/claude-code-src` 可作為高層設計參考，重點借鑑其 `tool/session/orchestration`、memory compact、任務分解與 sub-agent 協作思路；但**不直接拿它取代 runtime 底座**。`ANILA` 仍以 Python `anila-core` 為正式實作基底，避免把既有 CSP / Router / agent service 鏈拆掉重來

## Target Architecture

```
          ┌─ myCSPPlatform Frontend (Vue, 既有控制面) ─┐
          │ login / users / api keys / developer UI    │
          └──────────── control plane (/api/*) ────────┘
                             ↓
    ┌──────────────────── myCSPPlatform ────────────────────┐
    │  • auth / api key / role (admin/user/developer)      │
    │  • model_registry (LLM) + agents (endpoint agent)    │
    │  • approval / audit / usage / permissions            │
    │  • OpenAI-compatible data plane proxy                │
    └───────────────────────────────────────────────────────┘
                             ↑
                             │ 所有 runtime call 皆經此
┌─ Runtime Client（ANILA UI / OpenWebUI / curl） ───────────┐
└──────────── query → /v1/chat/completions ─────────────────┘
                             ↓
    ┌─ ANILA Core Router (anila-core SDK + router entrypoint) ─┐
    └───────────────────────────────────────────────────────────┘
         ↓                       ↓                         ↓
  ┌──────────┐           ┌──────────────┐        ┌────────────────────┐
  │ upstream │           │ knowledge /  │        │ future Onyx        │
  │ LLM /    │           │ RAG agents   │        │ workflow/compliance│
  │ embedding│           │ (optional)   │        │ agent              │
  └──────────┘           └──────────────┘        └────────────────────┘
                                 │                         ↓
                                 │ agent 內 LLM call 也回經 CSP
                                 └──────────────→ internal systems APIs / policy docs
```

**不變條件**：系統內所有「呼叫 LLM / 呼叫 agent」都必須打 CSP `/v1/chat/completions`（差別在 `model` 參數是真 LLM 還是 agent ID）。這是 usage 可信度的根據，也是 data plane 決策的實質含義。控制面則統一走既有 `myCSPPlatform/frontend` + `/api/*`。`ANILA` 主循環本身不預設做 RAG，知識檢索只在被分派到的 knowledge / RAG agent 內發生。

## Components

### 1. `anila-core` Python Runtime Foundation（AgenticRAG repo）

**重新定位**：`AgenticRAG` 去 `RAG-first` 化，升格為 `ANILA Core` 的 Python runtime foundation；`RAG`、workflow、tool-use 都只是建立在 runtime 之上的 agent 能力。`api.py` 則保留為可選的 sample knowledge / RAG agent。

**兩個 surface 要分清**：
- `api.py` (port 24786)：**現成** OpenAI-compatible，是第一個 sample registered agent 實體（目前範例偏 knowledge / RAG）
- `src/anila_core/api/server.py` (port 8000)：SDK 自帶 server，自訂 `/chat` + `/agentic-chat` schema —— **不是** OpenAI-compatible，Router 不能直接用

**改動清單:**
- **Registry 拆分**：
  - [src/anila_core/registry/agent_registry.py](AgenticRAG/src/anila_core/registry/agent_registry.py) 保留，作為 `LocalAgentDefinition` registry（給 coordinator 用）
  - 新增 `src/anila_core/registry/remote_agent_manifest.py`：`RemoteAgentManifest` dataclass + `RemoteAgentRegistry` 類別，啟動時從 CSP **`GET /v1/agents`**（data plane，帶 API Key）fetch，帶 TTL 快取
- 新增 `src/anila_core/tools/dispatch_tool.py`：`dispatch_to_agent(agent_id, query, stream=True)` —— **透過 CSP proxy** 呼叫（`POST <csp>/v1/chat/completions` with `model=agent-id`），不直接打 agent endpoint
- 新增 `src/anila_core/providers/cspplatform_provider.py`：OpenAI-compat provider，base_url 固定指向 CSP，bearer token 使用 CSP 發的 Key；取代直接打 upstream 的 `OpenAICompatProvider`
- 修改 [src/anila_core/config.py](AgenticRAG/src/anila_core/config.py)：新增 `CSP_BASE_URL` / `CSP_API_KEY`；`LLM_URL` / `EMBEDDING_URL` 內部 default 改指 CSP
- 修改 [src/anila_core/api/middleware/auth.py](AgenticRAG/src/anila_core/api/middleware/auth.py)：依 Decision #6，驗證函式**只**驗「請求是否來自 CSP」—— 檢查 `X-CSP-Service-Token` header（或 mTLS client cert），**不再比對 `settings.api_key`、也不驗 user JWT**。user 身份由 CSP 注入的 `X-ANILA-User-Id` / `X-ANILA-User-Email` / `X-ANILA-User-Groups` header 承載，agent 程式可信任直接讀
- 修改 [api.py](AgenticRAG/api.py)：加 auth middleware（目前**完全無 auth**）、LLM/Embedding 呼叫改走 `CSPPlatformProvider`
- 新增 `src/anila_core/api/router_server.py`（或 `router_app_factory`）：**OpenAI-compatible `/v1/chat/completions` + `/v1/models` router entrypoint**，載入 `CoordinatorAgent` + `RemoteAgentRegistry` + `dispatch_to_agent` tool；這是 ANILA Core Router 的底座
- 新增 `examples/simple-agent/`、`examples/router-mode/`
- 新增 CLI：`anila-core init` / `anila-core register`（Phase 6 再做）
- 補強 session/runtime 抽象：把 `conversation state`、`tool execution state`、`agent context` 視為 runtime 核心，而不是綁在單一 RAG pipeline 上
- 補強 compact / memory pipeline：整理現有 compact、session memory、memory extraction 能力，明確作為 runtime 的標準能力，可供 Router 與各 agent 重用
- 補強 task decomposition / orchestration：把 coordinator、sub-agent dispatch、budget control 明確寫成 runtime 的 orchestration 層，而不是只服務於 RAG 場景

**可重用（不要重寫）:**
- [QueryEngine 7-stage turn loop](AgenticRAG/src/anila_core/engine/query_engine.py) 直接作為 Router execution loop
- [Coordinator task decomposition / sub-agent dispatch](AgenticRAG/src/anila_core/coordinator/coordinator.py) 用於 multi-step routing
- 現有 [OpenAICompatProvider](AgenticRAG/src/anila_core/providers/openai_compat.py) 邏輯（只要換 base_url + auth header）
- 現有 compact / memory / session 相關模組，作為 runtime memory management 基底

**借鑑 `claude-code-src` 的方向（只借設計，不直接搬底座）:**
- `tool/session/orchestration`：把 turn loop、tool execution、session state 視為 runtime 第一級能力
- `memory compact`：優先建立可持續運作的 compact / summarization / context budget 機制，避免 Router 與 agent 各自長出一套
- `任務分配 / sub-agent`：借鑑 coordinator mode、task decomposition、agent handoff 思路，收斂成 ANILA 的 multi-agent orchestration 契約
- `runtime state discipline`：把 usage、budget、interrupt、retry、tool result storage 當成 platform runtime concerns，而不是每個 agent 自己處理

### 2. ANILA Core Router（新 deployment，位於 `anila-core-router/`）

**組成:**
- 載入 `anila-core` SDK 的 `router_app_factory`，啟動 FastAPI app
- 暴露 `/v1/chat/completions`（OpenAI 格式 + SSE）
- Root agent: `CoordinatorAgent`，system prompt 說明「你是 router，根據可用 agents manifest 決定直接回答或 dispatch」
- Tools: `list_available_agents()`, `dispatch_to_agent(agent_id, query, stream)`
- Registry: `RemoteAgentRegistry`，啟動時 fetch + TTL（如 60s）自動 refresh
- **Router 呼叫主 LLM 走 CSP proxy**（透過 `CSPPlatformProvider`）
- **Router dispatch agent 也走 CSP proxy**（`dispatch_to_agent` 內部是呼叫 CSP `/v1/chat/completions` with `model=<agent-id>`）

**明確不做:**
- Router 不預設先跑文件檢索
- Router 不內建單位知識庫
- Router 不把 RAG 當所有 query 的前置步驟

只有當路由結果命中 knowledge / RAG agent 時，才進入該 agent 內部的檢索流程。

### 3. myCSPPlatform 強化（data plane + control plane）

**Schema 變更（新建 agents 表 + developer role）:**

新增 `agents` 表：
```
id, name, owner_user_id (FK users), base_model_id (FK model_registry, nullable),
endpoint_url, api_version, description_for_router (text, for LLM routing),
input_schema (jsonb), capabilities (jsonb),
health_status, approval_status (enum: pending/approved/rejected),
approved_by (FK users, nullable), approved_at, created_at
```

新增 `user_agent_permissions`、`api_key_agent_permissions` 兩張關聯表（對齊既有 `user_model_permissions` / `api_key_model_permissions` 模式）。

修改 `users.role` enum：加入 `developer`（目前只有 admin / user），並由 admin 透過既有 user management 流程指派。

**Backend 關鍵檔案:**
- 新增 `myCSPPlatform/backend/app/models/agent.py`：`Agent`, `UserAgentPermission`, `ApiKeyAgentPermission` ORM
- 修改 [myCSPPlatform/backend/app/models/user.py](myCSPPlatform/backend/app/models/user.py)：role enum 加 `developer`
- 新增 `myCSPPlatform/backend/app/api/agents.py`（**JWT-protected 管理 API**）：
  - `POST /api/agents/register` — developer 可呼叫，狀態 `pending`
  - `GET /api/agents` / `GET /api/agents/{id}` — 列我的 / 讀詳情
  - `POST /api/agents/{id}/approve` / `reject` — admin only
  - `GET /api/agents/template/download` — developer / admin 下載官方 AgenticRAG template zip（先提供單一固定模板）
- 新增 `GET /v1/agents`（**data plane，API Key auth**，放在現有 proxy router 旁邊）：回傳該 API Key 擁有者可呼叫的 `approved` agents + manifest，語意對齊 OpenAI `/v1/models`；這是 Router 在每次 query 前會 fetch 的 endpoint，與管理面 `/api/agents` 完全分離 credential tier
- 修改 [myCSPPlatform/backend/app/api/users.py](myCSPPlatform/backend/app/api/users.py)：**正式新增 `allowed-agents` 指派 API**，對齊現有 `allowed-models`：
  - admin 用既有 `create/update user` 流程指派 `developer` role
  - `GET /api/users/{id}/allowed-agents`
  - `PUT /api/users/{id}/allowed-agents`（admin only）
  - 同步寫 audit log
- 修改 [myCSPPlatform/backend/app/services/proxy_service.py](myCSPPlatform/backend/app/services/proxy_service.py)：
  - **實作 SSE streaming**：用 `httpx.AsyncClient.stream()` + FastAPI `StreamingResponse`
  - **Usage accounting**：強制帶 `stream_options={include_usage: true}`，攔截最後 chunk 的 `usage` 寫入 `token_usage`；若下游未回 usage 則以輸入/輸出估算
  - **注入 service credential + identity headers 給 downstream agent**：附上 CSP 的 service token（`X-CSP-Service-Token` 或 mTLS）+ `X-ANILA-User-Id` / `X-ANILA-User-Email` / `X-ANILA-User-Groups`
- 修改 [myCSPPlatform/backend/app/api/proxy.py](myCSPPlatform/backend/app/api/proxy.py)：**agent / model resolve 邏輯** —— 收到請求的 `model` 欄位，先查 `agents` 表（name 或 id 命中就走 agent endpoint），落空再查 `model_registry`（走原有 upstream LLM 路徑）；解析後呼叫 `api_key_service` 做權限檢查
- 修改 [myCSPPlatform/backend/app/services/api_key_service.py](myCSPPlatform/backend/app/services/api_key_service.py)：**擴充 `check_model_permission` 同時支援 model / agent**（這是真正的權限判斷掛點，不要放在 `api_key_auth.py`）
- [myCSPPlatform/backend/app/middleware/api_key_auth.py](myCSPPlatform/backend/app/middleware/api_key_auth.py)：**保持現狀，只負責 API key validation**，不在這裡加 agent permission 邏輯

**Frontend 關鍵檔案（沿用既有 Vue 控制面，不另建第二個控制面）:**
- 修改 `myCSPPlatform/frontend/src/stores/auth.js`：新增 `isDeveloper` / role-aware UI 狀態
- 修改 `myCSPPlatform/frontend/src/router/index.js`：新增 developer-only routes / guards
- 修改 `myCSPPlatform/frontend/src/components/layout/AppSidebar.vue`：角色 badge、developer menu items
- 修改 `myCSPPlatform/frontend/src/views/UsersView.vue`：admin 可建立 / 編輯 `developer` role
- 新增 `myCSPPlatform/frontend/src/api/agents.js`：封裝 `/api/agents/*` 與 template download 呼叫
- 新增 `myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue`：developer console（register/list/status/template download）

**可重用（不要重寫）:**
- `api_key_service.generate_api_key()`
- `ApiKeyMiddleware` / `get_api_key()`
- `UserModelPermission` / `ApiKeyModelPermission` 設計模式（直接複製到 agent 對應表）
- Proxy 的重試 / timeout / token_usage 寫入
- **現有 app startup schema/backfill 機制** —— Codex 點出 CSP 已有此機制，MVP 階段沿用即可，**不排正式 Alembic migration 到關鍵路徑**
- **現有背景 health checker loop** —— 延後完善，不擋 MVP

### 4. Onyx Future Module Plan（純文件，無 code）

位置：`docs/onyx-application-plan.md`。目前 **只做文件，不排實作**；待相關單位 API 準備完成後，再回來啟動技術設計與整合。

文件需先收斂：

- 哪些 query / intent 會由 Router 分派到 Onyx，而不是一般 SDK-built agent
- 目標系統整合契約：form schema、lookup API、submit API、idempotency key、async job / polling、error shape
- 規定與硬規則切分：哪些由 deterministic checks 處理、哪些才交給 LLM 做規則輔助說明
- mutating actions 的互動模式：補欄、最終確認、附件需求、失敗重試 / rollback
- service credential、audit log、target system authorization 的責任邊界（Onyx 不是最終授權來源）
- 若有政策文件 / SOP / FAQ，要如何把這些知識餵給 Onyx 做 workflow 輔助，而不取代各單位用 `anila-core` 開發自己的 knowledge agent

Codex 明確指出：未來若有數以十計的系統要串接，Onyx 的價值會偏向 workflow / compliance orchestration，而不是另一套通用 knowledge-agent foundation。不先定清楚這個角色邊界，後面很容易和 AgenticRAG 的主線互相競爭。現階段因為相關單位 API 尚未就緒，Onyx 僅保留為 future module。

### 5. ANILA Runtime UI（需要，但後置）

位置：`anila-ui/`。

定位：
- end-user runtime chat client
- 與 `myCSPPlatform/frontend` 控制面分離
- 主要承接 chat、agent 切換、對話歷史、streaming response 等 runtime 體驗

分工：
- Claude Design：負責 wireframe、visual spec、interaction flow、狀態稿
- 工程：依 Claude Design 交付實作前端，並串接 Router / CSP

預期能力：
- Login / session handoff（沿用 CSP auth 契約）
- Chat UI 串 Router `/v1/chat/completions`
- 顯示 streaming 回覆、agent/route 狀態、錯誤狀態、重送
- 視需要讀取 `/v1/agents` 顯示使用者可用 agent

近期原則：
- Phase 1-3 仍以 curl / OpenWebUI 驗證 runtime flow
- ANILA UI 不阻塞 Router、CSP、sample agent 主線
- 但 ANILA UI 是正式 roadmap 項目，不再視為 optional

## Build Sequence

### Phase 1 — Python runtime foundation（關鍵路徑）
1. 拆 `LocalAgentDefinition` vs `RemoteAgentManifest` registry
2. 新增 `dispatch_tool`、`CSPPlatformProvider`
3. 新增 `router_server.py` / `router_app_factory`（OpenAI-compatible 入口）
4. `api.py` 作為第一個 sample agent，加 auth + provider 改走 CSP
5. `examples/simple-agent` + `examples/router-mode`
6. 整理 runtime 的 session / memory compact / orchestration 能力，明確從 `RAG pipeline` 抽離

### Phase 2 — myCSPPlatform data plane + control plane
7. 新增 `agents` 表 + 兩張 permission 關聯表（走現有 startup backfill）
8. `users.role` 加 `developer`
9. 新增 `/api/agents/*` endpoints + approval 流程
10. `proxy_service` 實作 SSE streaming + usage 結算
11. proxy 支援 `model=agent-id` 路由 agent
12. 沿用既有 `myCSPPlatform/frontend`，補齊 developer role、developer console、template download

### Phase 3 — Router MVP 端到端（無專屬 chat UI，curl / OpenWebUI 驗證）
13. 建 `anila-core-router/`
14. **新增 repo root `docker-compose.yml`**，統一管理 `csp` / `router` / `rag-agent`（目前作為 sample agent）/ `postgres` / `pgvector`，統一 network / port / env wiring / healthcheck / depends_on；用途明確標註為 **local integration & smoke test，非 production deployment**。之後 smoke test 的標準啟動方式就是 `docker compose up -d`
15. 管理員在 CSP 控制面把測試帳號升級成 developer，developer 下載 template 並註冊 `api.py` 為 agent，admin approve，給 test user 賦權（`allowed-agents`）
16. Smoke test：curl → Router → CSP → `api.py` → CSP → upstream LLM → SSE 回 curl，usage 三筆都落帳

### Phase 4 — Onyx Future Module Plan（純文件）
17. 寫 `docs/onyx-application-plan.md`
18. 明確標註 Onyx 目前不進入開發排程，待各單位 API 完成後再重新開案

### Phase 5 — ANILA Runtime UI
19. Claude Design 產出 `anila-ui/` 的 wireframe / visual / interaction spec
20. 建 `anila-ui/` 並實作 runtime chat shell
21. 串接 CSP auth / Router chat / user-visible streaming states

### Phase 6 — 開發者生態
22. `anila-core init` / `register` CLI
23. CSP 前端補齊 developer dashboard、template/version 說明、agent 狀態 UX
24. docs site + quickstart
25. 補 runtime design note：明確記錄 `claude-code-src` 借鑑到的 session / compact / orchestration 原則與 ANILA 的 Python 實作映射

### Phase 7 — 平台穩定化（非關鍵路徑，隨 MVP 穩定後推進）
- 正式 Alembic migration 取代 startup backfill
- Celery beat 週期 agent health-check
- Quota / rate-limit 表與 middleware
- 逐步把 memory compact、session recovery、task budgeting 產品化為 runtime 標準能力
- 若各單位 API 完成且需求成熟，再啟動 Onyx future module 的技術設計與 adapter 實作

## Verification

### End-to-end smoke test（Phase 3 結束即可執行）

```bash
# 1. 用 repo root 的整合 compose 一次啟動所有服務（標準 smoke test 啟動方式）
docker compose up -d
# 服務：csp(:8000) + router(:9000) + rag-agent(:24786, 目前 sample agent) + postgres + pgvector
docker compose ps   # 確認 healthcheck 全 healthy

# 2. admin 將測試帳號升級為 developer（控制面：JWT）
curl -X PUT http://localhost:8000/api/users/2 \
  -H "Authorization: Bearer <ADMIN_JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"developer"}'

# 3. developer 下載官方 anila-core template（控制面：JWT）
curl -L http://localhost:8000/api/agents/template/download \
  -H "Authorization: Bearer <DEVELOPER_JWT>" \
  -o anila-core-template.zip

# 4. Developer 註冊 sample agent（控制面：JWT；目前範例服務名沿用 rag-agent）
curl -X POST http://localhost:8000/api/agents/register \
  -H "Authorization: Bearer <DEVELOPER_JWT>" \
  -d '{"name":"rag-agent","endpoint_url":"http://rag-agent:24786","description_for_router":"General-purpose sample agent with optional knowledge lookup"}'
# admin approve（控制面：JWT）
curl -X POST http://localhost:8000/api/agents/1/approve -H "Authorization: Bearer <ADMIN_JWT>"

# 5. 給 test user 賦權（控制面：JWT，admin only）
curl -X PUT http://localhost:8000/api/users/2/allowed-agents \
  -H "Authorization: Bearer <ADMIN_JWT>" \
  -H "Content-Type: application/json" \
  -d '[1]'

# 6. Router 能看到該 user 可用 agents（資料面：API Key）
curl http://localhost:8000/v1/agents \
  -H "Authorization: Bearer sk-test-user-api-key"

# 7. 模擬 user query（資料面：API Key）
curl -N -X POST http://localhost:9000/v1/chat/completions \
  -H "Authorization: Bearer sk-test-user-api-key" \
  -d '{"model":"anila-router","messages":[{"role":"user","content":"查 Q3 財報重點"}],"stream":true}'
```

**驗證點:**
- Router 啟動成功從 CSP 拿到該 user 可用 agent 清單（`RemoteAgentRegistry` works）
- developer 可登入 CSP 前端、看到自己的 agent 列表並下載官方 template
- 主 LLM 呼叫**經 CSP proxy**（token_usage 有一筆 LLM-level 紀錄）
- 主 LLM 決策 → 呼叫 `dispatch_to_agent("rag-agent", ...)`
- dispatch **經 CSP proxy** 轉給 `api.py`（token_usage 有一筆 agent-level 紀錄）
- 若 `api.py` 內有 knowledge lookup / tool execution，其內部 LLM 呼叫**亦經 CSP proxy**（token_usage 再一筆）
- SSE 逐 chunk 串回 curl（不是一次性 dump）
- 三筆 `token_usage` 都正確 attribution 到 test user

### Future Module Note
- Onyx 相關驗證不列入近期 smoke test
- 目前交付物僅包含 `docs/onyx-application-plan.md`
- 待請假 / 簽核 / 其他內部系統 API ready 後，再定義實作驗證案例

### Phase 5 加測
- Claude Design 交付之 runtime UX 可落地成 `anila-ui/` 實作
- `anila-ui/` 可完成 login / chat / streaming 顯示 / error 狀態
- `anila-ui/` 與 `myCSPPlatform/frontend` 職責分離，沒有把控制面功能混進 runtime chat client

### Phase 6 加測
- 新開發者用 `anila-core init` + `register` 在 5 分鐘內上架新 agent
- Router **不重啟**，TTL 到期後動態發現新 agent

## Critical Files Summary

| 動作 | 路徑 | Phase |
|-----|------|-------|
| 修改 (拆 LocalAgentDefinition) | [AgenticRAG/src/anila_core/registry/agent_registry.py](AgenticRAG/src/anila_core/registry/agent_registry.py) | 1 |
| 新增 | `AgenticRAG/src/anila_core/registry/remote_agent_manifest.py` | 1 |
| 新增 | `AgenticRAG/src/anila_core/tools/dispatch_tool.py` | 1 |
| 新增 | `AgenticRAG/src/anila_core/providers/cspplatform_provider.py` | 1 |
| 新增 (OpenAI-compatible router 入口) | `AgenticRAG/src/anila_core/api/router_server.py` | 1 |
| 修改 | [AgenticRAG/src/anila_core/api/middleware/auth.py](AgenticRAG/src/anila_core/api/middleware/auth.py) | 1 |
| 修改 | [AgenticRAG/src/anila_core/config.py](AgenticRAG/src/anila_core/config.py) | 1 |
| 修改 (加 auth + 改走 CSP) | [AgenticRAG/api.py](AgenticRAG/api.py) | 1 |
| 新增 | `myCSPPlatform/backend/app/models/agent.py` | 2 |
| 修改 (role 加 developer) | [myCSPPlatform/backend/app/models/user.py](myCSPPlatform/backend/app/models/user.py) | 2 |
| 新增 | `myCSPPlatform/backend/app/api/agents.py` | 2 |
| 修改 (allowed-agents API) | [myCSPPlatform/backend/app/api/users.py](myCSPPlatform/backend/app/api/users.py) | 2 |
| 修改 (SSE + usage + service cred + identity headers) | [myCSPPlatform/backend/app/services/proxy_service.py](myCSPPlatform/backend/app/services/proxy_service.py) | 2 |
| 修改 (agent/model resolve) | [myCSPPlatform/backend/app/api/proxy.py](myCSPPlatform/backend/app/api/proxy.py) | 2 |
| 修改 (擴充 permission check 支援 agent) | [myCSPPlatform/backend/app/services/api_key_service.py](myCSPPlatform/backend/app/services/api_key_service.py) | 2 |
| 不改 (維持只做 key validation) | [myCSPPlatform/backend/app/middleware/api_key_auth.py](myCSPPlatform/backend/app/middleware/api_key_auth.py) | 2 |
| 修改 (role-aware auth store) | `myCSPPlatform/frontend/src/stores/auth.js` | 2 |
| 修改 (developer routes / guards) | `myCSPPlatform/frontend/src/router/index.js` | 2 |
| 修改 (developer menu / role badge) | `myCSPPlatform/frontend/src/components/layout/AppSidebar.vue` | 2 |
| 修改 (admin 指派 developer role) | `myCSPPlatform/frontend/src/views/UsersView.vue` | 2 |
| 新增 | `myCSPPlatform/frontend/src/api/agents.js` | 2 |
| 新增 | `myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue` | 2 |
| 新建 | `anila-core-router/` | 3 |
| 新建 (repo root integration compose) | `docker-compose.yml` | 3 |
| 新建 (純文件) | `docs/onyx-application-plan.md` | 4 |
| 新建 | `anila-ui/` | 5 |

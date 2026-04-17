# ANILA 平台整合計畫 (v3 — Codex-reviewed, decision-complete)

## Context

三個子專案（onyx / myCSPPlatform / AgenticRAG）已合併進單一 repo 但尚未真正整合。目標是建立 **LLM-as-Router + Agent Registry + 開發者生態** 的多租戶 AI 平台：

- **使用者**從 ANILA UI 發 query，主 LLM 自動分派給最適合的 agent
- **開發者**用 `anila-core` SDK 打造 agent，mlsteam 包裝後註冊到平台
- **API Key / 權限 / 配額 / 計費**由 myCSPPlatform 統一治理
- onyx 保留為「擅長企業文件 + 權限控管」的 registered agent（**延到 Phase 5**）

**關鍵認知**：`AgenticRAG` 的 Python package `anila-core` 是平台 SDK foundation —— ANILA Core Router 是一個**用 SDK 打造、配置成 dispatcher 角色的新 service**，不是 from scratch 寫的。但必須注意：**`anila-core` 目前只有 `/chat` + `/agentic-chat` 自訂 schema，Router 需要新增 OpenAI-compatible 入口**（SDK 裡還沒有 `build_app(mode="router")` 這種東西，不能假設）。

`AgenticRAG/api.py` (port 24786) 是**已經 OpenAI-compatible** 的獨立 surface，可直接當第一個 registered agent；不要和 SDK 的 `api/server.py` 混為一談。

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

4. **角色模型**：新增 `developer` role，自助註冊 agent 當 owner；admin 批准後才能被其他 user 發現 / 呼叫

5. **MVP 範圍**：先打通 `user → Router → CSP → RAG agent` 一條線；onyx 延到 Phase 5，並且 Phase 4 先寫 identity strategy 設計 note（純文件無 code）

6. **認證契約（三層 credential，必須區分清楚）**：
   - **管理面 / control plane（CRUD / approve）**：`/api/auth/*`, `/api/users/*`, `/api/agents` CRUD, `/api/agents/{id}/approve|reject`, `/api/keys/*`, `/api/users/{id}/allowed-agents` 等 → 一律 **JWT access token**（user session）
   - **資料面 / data plane（runtime 讀 / 呼叫）**：`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`, **`/v1/agents`（list available agents for the API-key's owner）** → 一律 **CSP API Key**（`sk-...`）。Router / agent 不持 upstream LLM key，只持 CSP API Key
   - **CSP → downstream agent**：用 **service-to-service credential**（CSP 有自己的 service token 或 mTLS；agent 只驗「來自 CSP」）；真實使用者身份透過 **trusted forwarded identity headers** 傳遞（`X-ANILA-User-Id`, `X-ANILA-User-Email`, `X-ANILA-User-Groups`）。downstream agent **不驗 user JWT 也不驗 user API key**，只信 CSP 注入的 header 做 ACL / quota

## Target Architecture

```
              ┌─ ANILA UI (Next.js, 新建) ─┐
              └──────────── query ──────────┘
                             ↓ /v1/chat/completions
    ┌─ ANILA Core Router (anila-core SDK + 新增 router entrypoint) ─┐
    └──────────────────────────────────────────────────────────────┘
                             ↓ 所有 LLM/agent 呼叫皆經此
    ┌────────────── myCSPPlatform (DATA PLANE) ──────────────┐
    │  • auth / api key / role (admin/user/developer)        │
    │  • model_registry (LLM) + agents (endpoint agent)      │
    │  • proxy w/ SSE streaming + usage accounting           │
    └────────────────────────────────────────────────────────┘
        ↓                    ↓                      ↓
  ┌──────────┐        ┌──────────────┐        ┌─────────────┐
  │ upstream │        │ RAG Agent    │        │ 未來 agents │
  │ LLM /    │        │ (api.py,     │        │ (用 SDK)    │
  │ embedding│        │  anila-core) │        │             │
  └──────────┘        └──────────────┘        └─────────────┘
                           │ agent 內的 LLM call 也回經 CSP
                           └─────→ (loop 回到上面 CSP)
```

**不變條件**：系統內所有「呼叫 LLM / 呼叫 agent」都必須打 CSP `/v1/chat/completions`（差別在 `model` 參數是真 LLM 還是 agent ID）。這是 usage 可信度的根據，也是 data plane 決策的實質含義。

## Components

### 1. `anila-core` SDK 強化（AgenticRAG repo）

**重新定位**：平台 SDK foundation + 附帶一個範例 RAG agent (`api.py`)。

**兩個 surface 要分清**：
- `api.py` (port 24786)：**現成** OpenAI-compatible，是第一個 registered agent 實體
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
- 新增 CLI：`anila-core init` / `anila-core register`（Phase 7 再做）

**可重用（不要重寫）:**
- [QueryEngine 7-stage turn loop](AgenticRAG/src/anila_core/engine/query_engine.py) 直接作為 Router execution loop
- [Coordinator task decomposition / sub-agent dispatch](AgenticRAG/src/anila_core/coordinator/coordinator.py) 用於 multi-step routing
- 現有 [OpenAICompatProvider](AgenticRAG/src/anila_core/providers/openai_compat.py) 邏輯（只要換 base_url + auth header）

### 2. ANILA Core Router（新 deployment，位於 `ANILA/anila-core-router/`）

**組成:**
- 載入 `anila-core` SDK 的 `router_app_factory`，啟動 FastAPI app
- 暴露 `/v1/chat/completions`（OpenAI 格式 + SSE）
- Root agent: `CoordinatorAgent`，system prompt 說明「你是 router，根據可用 agents manifest 決定直接回答或 dispatch」
- Tools: `list_available_agents()`, `dispatch_to_agent(agent_id, query, stream)`
- Registry: `RemoteAgentRegistry`，啟動時 fetch + TTL（如 60s）自動 refresh
- **Router 呼叫主 LLM 走 CSP proxy**（透過 `CSPPlatformProvider`）
- **Router dispatch agent 也走 CSP proxy**（`dispatch_to_agent` 內部是呼叫 CSP `/v1/chat/completions` with `model=<agent-id>`）

### 3. myCSPPlatform 強化（data plane 升級）

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

修改 `users.role` enum：加入 `developer`（目前只有 admin / user）。

**關鍵檔案:**
- 新增 `myCSPPlatform/backend/app/models/agent.py`：`Agent`, `UserAgentPermission`, `ApiKeyAgentPermission` ORM
- 修改 [myCSPPlatform/backend/app/models/user.py](myCSPPlatform/backend/app/models/user.py)：role enum 加 `developer`
- 新增 `myCSPPlatform/backend/app/api/agents.py`（**JWT-protected 管理 API**）：
  - `POST /api/agents/register` — developer 可呼叫，狀態 `pending`
  - `GET /api/agents` / `GET /api/agents/{id}` — 列我的 / 讀詳情
  - `POST /api/agents/{id}/approve` / `reject` — admin only
- 新增 `GET /v1/agents`（**data plane，API Key auth**，放在現有 proxy router 旁邊）：回傳該 API Key 擁有者可呼叫的 `approved` agents + manifest，語意對齊 OpenAI `/v1/models`；這是 Router 在每次 query 前會 fetch 的 endpoint，與管理面 `/api/agents` 完全分離 credential tier
- 修改 [myCSPPlatform/backend/app/api/users.py](myCSPPlatform/backend/app/api/users.py)：**正式新增 `allowed-agents` 指派 API**，對齊現有 `allowed-models`：
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

**可重用（不要重寫）:**
- `api_key_service.generate_api_key()`
- `ApiKeyMiddleware` / `get_api_key()`
- `UserModelPermission` / `ApiKeyModelPermission` 設計模式（直接複製到 agent 對應表）
- Proxy 的重試 / timeout / token_usage 寫入
- **現有 app startup schema/backfill 機制** —— Codex 點出 CSP 已有此機制，MVP 階段沿用即可，**不排正式 Alembic migration 到關鍵路徑**
- **現有背景 health checker loop** —— 延後完善，不擋 MVP

### 4. onyx Identity Strategy Note（Phase 4，純設計文件，無 code）

位置：`ANILA/docs/onyx-identity-strategy.md`。在寫 onyx adapter **之前**必須先決議：

- CSP user → onyx user 的映射模型（shadow user / PAT / service-account impersonation / on-the-fly provisioning？）
- onyx tenant / project 邊界對齊 CSP 的 `department` / project 概念
- onyx chat session lifecycle 歸屬（onyx 內部 vs Router 端）
- 文件 ACL 如何保真（onyx retrieval-time 用 `user_email` / `user_group` token 過濾，adapter 必須正確傳遞這些 identity token —— 見 [onyx/backend/onyx/access/models.py](onyx/backend/onyx/access/models.py)）

Codex 明確指出：adapter 的核心難點不是 schema / SSE 翻譯，而是 identity。不先決議這一步，adapter 寫完也不代表 ACL 問題解決。

### 5. onyx Adapter（Phase 5，拆兩子階段）

位置：`ANILA/onyx-adapter/`。

**5a — Identity integration**：實作 Phase 4 設計 note 選定的映射策略；建立 CSP user ↔ onyx user mapping 表；處理 PAT / session 生命週期。

**5b — Transport wrapper**：暴露 `/v1/chat/completions`；OpenAI messages → onyx payload 翻譯；onyx NDJSON SSE → OpenAI chunk 翻譯；註冊到 CSP，manifest 標「擅長企業文件 QA + 完整 document ACL」。

**參考 onyx 端點（不改 onyx 本身）:**
- [onyx/backend/onyx/server/query_and_chat/chat_backend.py:520](onyx/backend/onyx/server/query_and_chat/chat_backend.py#L520)
- [onyx/backend/onyx/chat/process_message.py](onyx/backend/onyx/chat/process_message.py)
- [onyx/backend/onyx/access/models.py](onyx/backend/onyx/access/models.py)

### 6. ANILA UI（新建，位於 `ANILA/anila-ui/`）

**技術**：Next.js 16 + React 19 + Tailwind（對齊 onyx，便於借鑒元件樣式與互動）。

**MVP 畫面（Phase 6）:**
- Login / Register → CSP `/api/auth/*`
- Chat UI → Router `/v1/chat/completions`（SSE）

**Phase 7 追加:**
- Agent Marketplace（end-user「我可用的 agents」，走 data plane `GET /v1/agents`，用 user API Key）
- Developer Dashboard（developer 管理自己的 agent；走控制面 `GET /api/agents` + `/api/keys`，用 JWT）

**UI 借鑒對象（不 fork，取設計模式）:**
- [onyx/web/src/sections/chat/ChatUI.tsx](onyx/web/src/sections/chat/ChatUI.tsx)
- [onyx/web/src/hooks/useChatController.ts](onyx/web/src/hooks/useChatController.ts)
- [onyx/web/lib/opal](onyx/web/lib/opal) 設計系統

## Build Sequence

### Phase 1 — SDK foundation（關鍵路徑）
1. 拆 `LocalAgentDefinition` vs `RemoteAgentManifest` registry
2. 新增 `dispatch_tool`、`CSPPlatformProvider`
3. 新增 `router_server.py` / `router_app_factory`（OpenAI-compatible 入口）
4. `api.py` 加 auth + provider 改走 CSP
5. `examples/simple-agent` + `examples/router-mode`

### Phase 2 — myCSPPlatform data plane
6. 新增 `agents` 表 + 兩張 permission 關聯表（走現有 startup backfill）
7. `users.role` 加 `developer`
8. 新增 `/api/agents/*` endpoints + approval 流程
9. `proxy_service` 實作 SSE streaming + usage 結算
10. proxy 支援 `model=agent-id` 路由 agent

### Phase 3 — Router MVP 端到端（無 UI，curl 驗證）
11. 建 `ANILA/anila-core-router/`
12. **新增 repo root `docker-compose.yml`**，統一管理 `csp` / `router` / `rag-agent` / `postgres` / `pgvector`，統一 network / port / env wiring / healthcheck / depends_on；用途明確標註為 **local integration & smoke test，非 production deployment**。之後 smoke test 的標準啟動方式就是 `docker-compose up -d`
13. 管理員手動註冊 `api.py` 為 agent、approve、給 test user 賦權（用 `PUT /api/users/{id}/allowed-agents`）
14. Smoke test：curl → Router → CSP → `api.py` → CSP → upstream LLM → SSE 回 curl，usage 三筆都落帳

### Phase 4 — onyx Identity Strategy Note（純文件）
14. 寫 `ANILA/docs/onyx-identity-strategy.md`

### Phase 5 — onyx 納入
15. Adapter identity layer (5a)
16. Adapter transport layer (5b)
17. 註冊到 CSP，驗證 Router 能根據 query 性質分派 RAG vs onyx

### Phase 6 — UI MVP
18. `ANILA/anila-ui/` Login + Chat 打通 UI → Router

### Phase 7 — 開發者生態
19. `anila-core init` / `register` CLI
20. UI Agent Marketplace + Developer Dashboard
21. docs site + quickstart

### Phase 8 — 平台穩定化（非關鍵路徑，隨 MVP 穩定後推進）
- 正式 Alembic migration 取代 startup backfill
- Celery beat 週期 agent health-check
- Quota / rate-limit 表與 middleware

## Verification

### End-to-end smoke test（Phase 3 結束即可執行）

```bash
# 1. 用 repo root 的整合 compose 一次啟動所有服務（標準 smoke test 啟動方式）
docker-compose up -d
# 服務：csp(:8000) + router(:9000) + rag-agent(:24786) + postgres + pgvector
docker-compose ps   # 確認 healthcheck 全 healthy

# 2. Developer 註冊 RAG agent（控制面：JWT）
curl -X POST http://localhost:8000/api/agents/register \
  -H "Authorization: Bearer <DEVELOPER_JWT>" \
  -d '{"name":"rag-agent","endpoint_url":"http://rag-agent:24786","description_for_router":"Complex multi-hop RAG over indexed documents"}'
# admin approve（控制面：JWT）
curl -X POST http://localhost:8000/api/agents/1/approve -H "Authorization: Bearer <ADMIN_JWT>"

# 3. 給 test user 賦權（控制面：JWT，admin only）
curl -X PUT http://localhost:8000/api/users/2/allowed-agents \
  -H "Authorization: Bearer <ADMIN_JWT>" -d '{"agent_ids":[1]}'

# 4. Router 能看到該 user 可用 agents（資料面：API Key）
curl http://localhost:8000/v1/agents \
  -H "Authorization: Bearer sk-test-user-api-key"

# 5. 模擬 user query（資料面：API Key）
curl -N -X POST http://localhost:9000/v1/chat/completions \
  -H "Authorization: Bearer sk-test-user-api-key" \
  -d '{"model":"anila-router","messages":[{"role":"user","content":"查 Q3 財報重點"}],"stream":true}'
```

**驗證點:**
- Router 啟動成功從 CSP 拿到該 user 可用 agent 清單（`RemoteAgentRegistry` works）
- 主 LLM 呼叫**經 CSP proxy**（token_usage 有一筆 LLM-level 紀錄）
- 主 LLM 決策 → 呼叫 `dispatch_to_agent("rag-agent", ...)`
- dispatch **經 CSP proxy** 轉給 `api.py`（token_usage 有一筆 agent-level 紀錄）
- `api.py` 內 `vector_search` 觸發，其內部 LLM 呼叫**亦經 CSP proxy**（token_usage 再一筆）
- SSE 逐 chunk 串回 curl（不是一次性 dump）
- 三筆 `token_usage` 都正確 attribution 到 test user

### Phase 5 加測
- 切換 query 為「解釋最新 HR 政策」→ Router 分派到 onyx agent
- 不同 user 重跑 → onyx 因權限回不同文件（identity mapping 生效）

### Phase 7 加測
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
| 新建 | `ANILA/anila-core-router/` | 3 |
| 新建 (repo root integration compose) | `docker-compose.yml` | 3 |
| 新建 (純文件) | `ANILA/docs/onyx-identity-strategy.md` | 4 |
| 新建 | `ANILA/onyx-adapter/` | 5 |
| 新建 | `ANILA/anila-ui/` | 6 |

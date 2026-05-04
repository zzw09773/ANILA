# 部署到 ANILA 平台

如果你想讓 ANILA Router 自動分派使用者請求給你的 AgenticRAG 實例，這份文件涵蓋從**註冊**到**啟動**的完整流程。

> **AgenticRAG 本身與 ANILA 平台解耦。** 不部署在 ANILA 也能跑（standalone deployment 走自己的 `/chat` / `/agentic-chat` 直連）。本文件是「**部署到 ANILA**」這個特定場景的指引。

## 架構速覽

```
使用者瀏覽器
   ↓ 對話請求
ANILA UI → ANILA Router (port 9000)
   ↓ 主 LLM 判斷要不要分派
   ↓ 如果要分派 → 找 CSP 註冊的 agent
ANILA Router → CSP /api/agents/forward → 你的 AgenticRAG
```

**Router 自動分派的前提**：你的 agent 已經在 CSP 的 `agents` 表裡 + 有 `approval_status='approved'` + 有有效的 service token。

## 前置條件

- ANILA 平台已部署且 healthy（CSP / Router / nginx 都通）
- 你能存取 CSP admin 帳號（或請 admin 幫你完成註冊）
- 你的 AgenticRAG 實例 `endpoint_url` 在 ANILA 的 docker network 裡可達（例：`http://my-agent:24786`）

## 步驟 1：註冊 agent

CSP 提供 admin endpoint 註冊 agent。可以用 admin UI（推薦）或 curl。

### 方式 A：admin UI

1. 登入 CSP admin（`https://your-anila/admin`）
2. **Developer → Agents → 新增 Agent**
3. 填入：
   - **Name**：你的 agent 識別名（snake_case 推薦），例 `my-rag-agent`
   - **Endpoint URL**：你的 AgenticRAG `/chat` 上層的 base URL（不含 `/chat`），例 `http://my-rag-agent:24786`
   - **Description for Router**：給 Router 主 LLM 看的描述，越具體 routing 越準。例：「處理 ACME 客服 FAQ 與訂單查詢，吃 zh-TW 問題，會 cite 文件來源」
   - **Base Model**：你的 agent 內部用的 LLM（從 model_registry 選）
   - **Capabilities**（optional）：JSON 物件，例 `{"streaming": true, "max_context": 32000}`
4. 送出後 admin 收到 approval request；按「核可」後 agent 進入 `approved` 狀態

### 方式 B：curl（自動化部署）

```bash
JWT="<你的 admin JWT>"

curl -X POST http://csp:8000/api/agents \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{
      "name": "my-rag-agent",
      "endpoint_url": "http://my-rag-agent:24786",
      "description_for_router": "處理 ACME 客服 FAQ 與訂單查詢...",
      "base_model_id": 1,
      "capabilities": {"streaming": true}
    }'
# 回傳含 agent id，記下來下一步要用
```

註冊後 agent 預設 `pending`，需要 admin 再呼叫 approve endpoint：

```bash
curl -X POST http://csp:8000/api/agents/{agent_id}/approve \
    -H "Authorization: Bearer $JWT"
```

## 步驟 2：簽發 service token

Service token (`csk-...`) 是 agent 跟 CSP 互信的憑證，CSP 會在每次轉發請求時帶 `X-CSP-Service-Token: csk-...` header 給你的 agent，agent 用這個 header 驗證請求是 ANILA 來的。

兩種發放方式：

### 方式 A：static credential（最簡單，dev / 小型部署）

```bash
curl -X POST http://csp:8000/api/agents/{agent_id}/credentials/issue-static \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{}'

# 回應：
# {
#   "service_token": "csk-xxxxxxx",
#   "credential_id": 5,
#   "issued_at": "2026-05-04T12:00:00",
#   "label": null
# }
```

把這個 `csk-...` 設成 AgenticRAG 容器的 env：

```bash
CSP_SERVICE_TOKEN=csk-xxxxxxx
```

### 方式 B：bootstrap-then-provision（推薦生產環境）

每個 agent 啟動時自動換一個新 token，避免共用、好 rotate。詳見 [`BOOTSTRAP_DEPLOYMENT.md`](../BOOTSTRAP_DEPLOYMENT.md)。

簡述：
1. Admin 簽 `bsk-` (bootstrap token，short-lived)
2. Agent 啟動腳本帶 `CSP_BOOTSTRAP_TOKEN=bsk-...` 起來
3. AgenticRAG entrypoint 偵測到 bsk- → 自動呼 `/api/agents/{id}/bootstrap` 換成 `csk-`
4. csk- 寫入持久化目錄（`/data/anila-state/`），下次啟動直接用

## 步驟 3：設定 AgenticRAG 容器 env

最少需要：

```bash
# CSP service-to-service auth
CSP_SERVICE_TOKEN=csk-xxxxxxx       # 步驟 2 拿到的

# 你的 LLM endpoint（CSP 統一代理）
LLM_BASE_URL=http://csp:8000/v1
LLM_API_KEY=sk-internal-...         # CSP 簽給你的 internal key

# Embedding 同樣走 CSP
EMBEDDING_BASE_URL=http://csp:8000/v1
EMBEDDING_API_KEY=sk-internal-...
```

完整 env 表見 AgenticRAG `.env.example`。

## 步驟 4：啟動 + 驗證

```bash
docker compose up -d my-rag-agent

# 確認 healthcheck
curl http://my-rag-agent:24786/health
```

到 ANILA UI 對話框輸入「**用 ACME 客服回答：訂單怎麼追蹤**」，正常情況下：

1. 主 LLM 看到 description 跟 query 匹配，決定分派給 `my-rag-agent`
2. CSP 轉發請求到 `http://my-rag-agent:24786/chat`，header 有 `X-CSP-Service-Token`
3. 你的 agent 處理 → SSE 串回
4. 使用者看到 agent 回覆

如果不分派：
- 檢查 description 是否夠具體（前面 OO 後面 XX、太抽象就不會分派）
- 檢查 `approval_status='approved'`
- 看 Router log 確認有看到你的 agent (`/health` 會回 `cached_agents`)

## 步驟 5（可選）：使用者個人化

ANILA 平台會把使用者長期事實存在 CSP 的 `user_facts` 表。你的 agent 可以呼叫 `/api/memory/users/{user_id}/facts` 拉這些事實，注入到 system prompt。

實作方式：寫一個 `UserContextProvider`（範本見 [`docs/examples/memory.md`](../examples/memory.md) 的「HTTP backend」範例），把 base URL 設成 `http://csp:8000`、token 用 `CSP_SERVICE_TOKEN`、user_id 從 `X-ANILA-User-Id` header 拿。

CSP 轉發請求時固定會帶以下 header：

| Header | 內容 |
|--------|------|
| `X-ANILA-User-Id` | 整數 user id |
| `X-ANILA-User-Email` | 使用者 email |
| `X-CSP-Service-Token` | 你的 service token（驗證用） |

把 provider 注入到 `create_app(user_context_provider=...)` 即可，agent 會自動把 facts 注入 system prompt。

## Token rotation

當 service token 洩漏或定期輪換：

```bash
# rotate（產新 token，舊 token 24h 後失效）
curl -X POST http://csp:8000/api/agents/{agent_id}/credentials/{cred_id}/rotate \
    -H "Authorization: Bearer $JWT"
```

新 token 寫入 agent env / state file，restart container。24h 過渡期讓你 deploy 沒空窗。

## 移除 agent

```bash
# 停用 agent
curl -X PATCH http://csp:8000/api/agents/{agent_id} \
    -H "Authorization: Bearer $JWT" \
    -H "Content-Type: application/json" \
    -d '{"approval_status": "rejected"}'
# 或直接刪
curl -X DELETE http://csp:8000/api/agents/{agent_id} \
    -H "Authorization: Bearer $JWT"
```

刪除會 cascade 把 agent_credentials 一起清乾淨。

## 故障排除

### Router 不分派給我的 agent

1. **description 太籠統** — 加上具體觸發詞 / 領域 / 範例 query
2. **approval_status != 'approved'** — admin 補 approve
3. **endpoint_url 不通** — 從 CSP 容器 `curl <endpoint>/health` 確認連線
4. **Router cache** — Router 60 秒重 fetch agent 清單，等一下或 restart

### Agent 收到請求但回 401

→ Agent 端 `CSP_SERVICE_TOKEN` 沒對上。檢查 env 是否真的注入到 container 裡。

### 拉不到 user facts

1. `ANILA_CSP_BASE_URL` 沒設（agent 不知道 CSP 在哪）
2. Service token 沒正確 forward 到 fact API（check provider 程式碼）
3. 該 user 在 ANILA 還沒有 fact（新使用者需要對話幾輪後系統才有萃取記錄）

## 進階主題

- **Multi-tenant**：一個 AgenticRAG 服務多個 ANILA tenant — 每個 tenant 一個 agent 註冊 + token，agent 內部用 `X-ANILA-User-Id` 切資料
- **Streaming**：AgenticRAG 預設 SSE streaming；ANILA Router 會 forward 給最終 client
- **Classified 對話**：CSP 會把 `requires_encryption=true` 的 agent 對話自動 latch 為機密；agent 不必處理，CSP / UI 那層自動加鎖頭、稽核浮水印

## 相關文件

- [`BOOTSTRAP_DEPLOYMENT.md`](../BOOTSTRAP_DEPLOYMENT.md) — bootstrap-then-provision 詳細流程（K8s + persistent volume 方案）
- [`CSP_INTEGRATION.md`](../CSP_INTEGRATION.md) — CSP API 細節 + middleware 載入路徑
- [`SERVER_DEPLOYMENT_PHASE4.md`](../SERVER_DEPLOYMENT_PHASE4.md) — 多 server 部署 + load balancer
- ANILA 平台側 README — 解釋 Router / CSP 全貌

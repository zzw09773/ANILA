# 開發者指南 — 在 myCSPPlatform 註冊一個 Agent

> 本文件說明開發者要如何從 fork `AgenticRAG` 樣板 → 部署自己的 agent →
> 在「Developer / Agent Console」頁面完成註冊 → 送審 → 被 Router 自動 discover。
>
> 頁面位置：`/developer/agents`（`myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue`）

---

## TL;DR

1. 在 Agent Console 按「下載官方模板」取得 `anila-core-template.zip`。
2. 解壓、修改 `api.py` 的檢索/推論邏輯，設定 `.env`，`docker compose up -d`。
3. 確認 `GET /health` 回 200、`POST /v1/chat/completions` 可接 OpenAI-compat 請求。
4. 回到 Agent Console 按「註冊 Agent」，填名稱、endpoint URL、router 描述。
5. 等 admin 核准 → Router 自動 discover → 在前端對話列表看得到。

---

## 1. 前置條件

- 一台可跑 Docker 的機器，對 CSP backend 可達（或反過來）。
- 至少一組 LLM / embedding 端點（直連或走 CSP proxy）。
- 你在 myCSPPlatform 已經登入且角色為 `developer` 或 `admin`。

---

## 2. Fork AgenticRAG 樣板

```bash
# 1. 取得樣板（或從 Agent Console 下載 zip 再解壓）
git clone <your-fork-url> my-new-agent
cd my-new-agent

# 2. 改 api.py 的 retrieve_context() 為你的檢索/推論邏輯
#    - 可以是 vector search、外部 API、knowledge graph、MCP tool 等
#    - SYSTEM_PROMPT 換成你的 agent 人格與格式要求

# 3. 設定 .env（見 AgenticRAG/.env.example）
cp .env.example .env
# 至少填：LLM_URL / LLM_API_KEY / MODEL 或 CSP_BASE_URL / CSP_API_KEY

# 4. 部署
docker compose build && docker compose up -d

# 5. 自測健康與對話
curl http://localhost:24786/health
# {"status":"ok","model":"google/gemma4","rag":true}
```

`anila-core/` 不用動，那是平台共用的 runtime foundation。

---

## 3. Endpoint 合約（你必須實作的）

Router 只會碰你的 agent 這幾條路徑，其他都可以自己加。

| 方法 | 路徑 | 目的 | 驗證 |
|---|---|---|---|
| `GET` | `/health` | CSP discovery / docker healthcheck | 公開 |
| `GET` | `/v1/models` | 回報可用模型 ID（OpenAI-compat） | s2s token |
| `POST` | `/v1/chat/completions` | 主要推論端點 | s2s token |

驗證用 `X-CSP-Service-Token` header，由 `CspServiceTokenMiddleware` 處理；
`CSP_SERVICE_TOKEN` 為空時等於 dev mode（不驗）。

### 3.1 `/health` 輸出格式

```json
{
  "status": "ok",
  "model":  "google/gemma4",
  "rag":    true
}
```

只要 HTTP 200 且 `status == "ok"`，Router 就認為健康。

### 3.2 `/v1/models` 輸出格式（OpenAI-compat）

```json
{
  "object": "list",
  "data": [
    {
      "id":       "rag/google/gemma4",
      "object":   "model",
      "created":  1735689600,
      "owned_by": "agentic-rag"
    }
  ]
}
```

### 3.3 `/v1/chat/completions` 輸入

```json
{
  "model":    "rag/google/gemma4",
  "messages": [
    {"role": "system", "content": "你是客服助理"},
    {"role": "user",   "content": "請假規定？"}
  ],
  "stream":      true,
  "temperature": 0.7
}
```

### 3.4 `/v1/chat/completions` 輸出 — **這就是「模型的輸出格式」**

**非串流**（`stream: false`）— 一次回一個 JSON：

```json
{
  "id":      "chatcmpl-abc123",
  "object":  "chat.completion",
  "created": 1735689600,
  "model":   "rag/google/gemma4",
  "choices": [
    {
      "index": 0,
      "message": {
        "role":    "assistant",
        "content": "根據《員工手冊 §3.2》……"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens":     128,
    "completion_tokens": 64,
    "total_tokens":      192
  }
}
```

**串流**（`stream: true`）— Server-Sent Events，一筆一個 `data:` 行，最後以 `data: [DONE]` 結束：

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"content":"根據"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"content":"《員工手冊》"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**可選欄位：**

- `choices[].delta.reasoning_content`：思考塊 / RAG 檢索軌跡，前端會顯示在折疊區。
- 回覆結尾自行 append「參考來源」清單，平台不強制格式。

回應 header 必須是 `Content-Type: text/event-stream`，且保持連線直到 `[DONE]`。

---

## 4. 在 Agent Console 註冊

到 `/developer/agents` 按「註冊 Agent」，填：

| 欄位 | 必填 | 範例 | 備註 |
|---|---|---|---|
| `name` | ✅ | `hr-policy-agent` | 全平台唯一，建議 `kebab-case` |
| `endpoint_url` | ✅ | `http://10.0.1.20:24786` | 必須 `http://` 或 `https://`，Router 可達 |
| `description_for_router` | ✅（≥24 字元） | `處理員工手冊、請假規定、薪酬結構等 HR 法規查詢。` | 自然語言；Router 用這段做 agent 選擇 |
| `api_version` | ❌（預設 `v1`） | `v1` | 目前 Router 只認得 `v1` |

後端 schema（`myCSPPlatform/backend/app/api/agents.py:43`）還收下列欄位：

- `base_model_id`: ✅ **必填**。整數，指向 CSP Model Registry 中已註冊的底層 LLM（`Field(..., description="必須指定底層模型 ID")`）。
- `capabilities`: ❌ 可選。JSON dict，自由 metadata（能力標籤、tag、可處理領域…）。
- `input_schema`: ❌ 可選。JSON Schema，描述你的 agent 期待的輸入結構。

> ⚠️ **per-agent service token / api_key（規劃中）**：未來 Create 流程會額外回傳 agent 專屬的 `service_token`（inbound 驗證用）與 `api_key`（agent 呼 LLM/embedding 走 CSP proxy 用）。目前這兩把仍是手動共享的全域 secret。

送出後 agent 進入 `approval_status = "pending"`，`health_status = "unknown"`。

---

## 5. 審核與健康檢查

- **Admin 核准**：admin 在同一頁按「核准」→ `approval_status = "approved"`。被拒絕時會附留言。
- **Health polling**：CSP 會定期呼叫你的 `/health`。連不到 → `unhealthy`；對話時 Router 會跳過。
- **加密模式**：admin 可切 `requires_encryption = true`。啟用後凡是經由此 agent 的對話會**單向**鎖為加密，使用者無法關閉 — 這不可逆，請評估再用。

核准後 Router 下一次 discovery tick 會把你加進候選池，前端就看得到。

---

## 6. 常見送審失敗原因

| 狀況 | 怎麼修 |
|---|---|
| `Endpoint 必須是 http 或 https URL` | 前端 `DeveloperAgentsView.vue` 內驗證；補上 scheme。 |
| `Router 描述至少需要 24 個字元` | 寫清楚這個 agent 處理什麼領域、什麼格式的問題。 |
| 註冊成功但 health 一直 `unhealthy` | CSP backend 連不到你的 host/port；檢查防火牆、Docker 網段、`endpoint_url` 是否是 CSP 能解析到的位址（不是 `localhost`）。 |
| 核准後 Router 叫不到 | `X-CSP-Service-Token` 驗證失敗；`.env` 的 `CSP_SERVICE_TOKEN` 要跟 CSP 發的一致；留空則只能跑 dev mode。 |
| 串流回覆卡住 | 檢查 `Content-Type: text/event-stream`、`[DONE]` 結尾、proxy/nginx buffering 要關掉（`X-Accel-Buffering: no`）。 |

---

## 7. 進階：tool-driven / multi-turn agent

如果你的 agent 需要 LLM 自己決定何時檢索、呼叫哪個 tool，不要停在 OpenAI-compat
pre-process 注入層，改用 `anila_core.engine.query_engine.QueryEngine`：

```python
from anila_core.engine.query_engine import QueryEngine, QueryConfig
from anila_core.tools import VectorSearchTool, KeywordSearchTool, ReadDocumentTool

engine = QueryEngine(QueryConfig(
    model=MODEL,
    allowed_tools=[
        VectorSearchTool(_pool),
        KeywordSearchTool(_pool),
        ReadDocumentTool(_pool),
    ],
))
async for event in engine.run_turn(messages):
    ...
```

Output 面向 Router 的格式一樣是 OpenAI-compat SSE，tool call 的中間狀態走
`delta.reasoning_content`。

---

## 8. 相關檔案索引

- 前端頁面：`myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue`
- 註冊 API：`myCSPPlatform/backend/app/api/agents.py`（`register` / `approve` / `reject`）
- Agent ORM：`myCSPPlatform/backend/app/models/agent.py`
- 樣板：`AgenticRAG/`（`README.md`、`api.py`、`index_documents.py`）
- Runtime foundation：`anila-core/`
- 平台藍圖：`anila_plan.md`

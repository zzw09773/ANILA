# ANILA / myCSPPlatform 整合指南

> **讀者**：把這份 AgenticRAG template fork 成自己 agent 的開發者。
> **前提**：你已經看過根 README 並成功把 agent 在本機跑起來（`docker compose up -d` → `curl /health` 回 200）。

這份文件解答「**本機跑得起來，下一步怎麼接進 ANILA 平台**」。

---

## 一眼看整個介面

```
  使用者瀏覽器
       │
       ▼
┌────────────────────────────────────────────┐
│   myCSPPlatform (backend + UI)             │
│                                            │
│   ─ Router 依 agent description 挑選對象   │
│   ─ 注入 X-CSP-Service-Token 後轉發         │
│   ─ 注入 X-ANILA-User-{Id,Email,Groups}    │
└────────────────────────────────────────────┘
       │  POST /v1/chat/completions
       │  header: X-CSP-Service-Token: <secret>
       ▼
┌────────────────────────────────────────────┐
│   本 Agent（api.py  port 24786）           │
│   CspServiceTokenMiddleware 驗 token       │
│    └─ 不通過 → 401 / 403                    │
│    └─ 通過   → 跑 RAG pipeline 然後 stream  │
└────────────────────────────────────────────┘
```

本 agent 暴露給 CSP 的介面面積**刻意保持最小**：

| 方法 | 路徑 | 用途 | 權限 |
|---|---|---|---|
| GET  | `/health`              | CSP health-checker 每分鐘 poll | 公開 |
| GET  | `/v1/models`           | CSP 問「你在跑什麼模型？」 | 需 X-CSP-Service-Token |
| POST | `/v1/chat/completions` | Router 轉發用戶對話 | 需 X-CSP-Service-Token |

## 註冊流程（三選一）

### 方式 A — 在 CSP 啟動時自動註冊（推薦給部署管理者）

把 [`anila-agent.yaml`](../anila-agent.yaml) 轉成 JSON 塞進 CSP backend 的 `.env`：

```bash
# 一行把 yaml 轉成平台需要的 JSON
python -c "import yaml,json;print(json.dumps([yaml.safe_load(open('anila-agent.yaml'))],ensure_ascii=False))"
```

把輸出貼到 myCSPPlatform 的 `.env`：

```bash
AUTO_REGISTER_AGENTS='[{"name":"agentic-rag",...}]'
```

重啟 CSP backend，啟動 log 會顯示：

```
INFO 自動註冊 agent: agentic-rag -> http://agentic-rag:24786
```

### 方式 B — CSP UI 手動登錄（推薦給試用 / PoC）

1. 登入 myCSPPlatform UI
2. 「Developer」分頁 → 「Register new agent」
3. 把 `anila-agent.yaml` 裡的欄位對應填入：
   - `name` → Name
   - `endpoint_url` → Endpoint URL
   - `description_for_router` → Description
   - `base_model` → Base Model（從下拉選既有模型）
4. 送出 → 管理員在「Approvals」分頁按核准
5. 核准後 Router 立即可轉發

### 方式 C — 從程式碼 POST `/agents`

在現場 provisioning 腳本中使用：

```bash
curl -X POST https://csp.internal/agents \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d @anila-agent.json
```

Schema 欄位與 `anila-agent.yaml` 相同；實際 API 位置請以 CSP backend 的 OpenAPI 為準
（`http://csp-backend:8000/docs`）。

---

## 本 agent 側的設定

### Middleware 載入順序（Phase 0 後）

本 template 啟動時會掛 **`CspServiceTokenMiddleware`**：

1. 讀取 `${ANILA_AGENT_STATE_DIR}/service_token.json`（預設 `/var/lib/anila-agent/`）
2. 找不到檔案 → 退回 `CSP_SERVICE_TOKEN` env var（legacy fallback）
3. 兩者皆無 → 進入「local dev mode」，所有 incoming 請求放行

收到 incoming `X-CSP-Service-Token` 與在記憶體的 token mismatch 時，會**重新讀
state file 一次後重試**，這樣 admin rotation 不需要重啟 container 就能生效。

Loader 邏輯在 [`src/agentic_rag/api/middleware/loader.py`](../src/agentic_rag/api/middleware/loader.py)：
若 host 環境碰巧有裝 anila-core 就優先用 `anila_core.api.middleware.auth.CspServiceTokenMiddleware`
（platform-side 部署常見情境）；其他情況一律 fallback 到內建版
[`src/agentic_rag/api/middleware/csp_auth.py`](../src/agentic_rag/api/middleware/csp_auth.py)。
**Devs fork 本 template 永遠不需要 anila-core**——內建 fallback 行為與
anila-core 版本完全一致。

### 相關環境變數（Sprint 8 X / Phase D 後）

```bash
# 一次性 bootstrap：admin 從 CSP UI 「Issue bootstrap token」複製過來。
# 容器 entrypoint 會用它呼叫 CSP /api/agents/{id}/bootstrap，把回傳的
# csk- 寫進 /var/lib/anila-agent/service_token.json (mode 0600)。
# 用過即失效；首次啟動完成後務必從 .env 移除。
CSP_BOOTSTRAP_TOKEN=bsk-XXXX

# Bootstrap 必填的 3 個參數：
CSP_URL=http://csp:8000
ANILA_AGENT_ID=2
ANILA_ENDPOINT_URL=http://agentic-rag:24786
ANILA_REPLICA_LABEL=pod-0   # K8s 多副本時建議加；single-replica 可略

# Legacy（cutover 過渡期還可用，建議遷出）：
# CSP_SERVICE_TOKEN=<fleet-shared-secret>

# 若 LLM / embedding 也要走 CSP proxy 而非直連內網 vLLM：
CSP_BASE_URL=https://csp-backend.internal
CSP_API_KEY=<agent-api-key-issued-by-csp>
```

完整部署步驟（含 K8s multi-replica + recovery）：見
[`BOOTSTRAP_DEPLOYMENT.md`](BOOTSTRAP_DEPLOYMENT.md)。

`CSP_BOOTSTRAP_TOKEN` / `CSP_SERVICE_TOKEN` 與 `CSP_BASE_URL` **可獨立設定**：
- 只設 token 群（任一）→ CSP 轉進來的 traffic 會被驗證，但 agent 自己直連 vLLM
- 只設 `CSP_BASE_URL` → agent 把 LLM 呼叫透過 CSP 走，但自己不驗 CSP token（適合極信任內網）
- 都設 → 完整 ANILA 整合

---

## 轉發過來的可信 headers

CSP 驗過使用者之後，會注入下列 headers 再轉發給 agent：

| Header | 內容 | 用途 |
|---|---|---|
| `X-CSP-Service-Token` | agent 註冊時拿到的 shared secret | agent 用 middleware 驗證來源 |
| `X-ANILA-User-Id`     | CSP 內部 user id | 給 agent 做 per-user scoping（本 template 預設不用）|
| `X-ANILA-User-Email`  | 使用者 email | audit log |
| `X-ANILA-User-Groups` | 逗號分隔的 group 名 | RBAC / 企業內部權限過濾 |

要在本 agent 用 user-scoped 檢索（例如不同部門看不同文件）：

```python
# src/agentic_rag/api/server.py 內
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    user_id = request.headers.get("X-ANILA-User-Id", "default")
    groups = request.headers.get("X-ANILA-User-Groups", "").split(",")
    # 帶進檢索參數
    results = await retrieve(query, user_id=user_id, allowed_groups=groups)
```

這些 headers **只有 CSP 能設定** — 直接對 agent 打的外部流量會先被
`CspServiceTokenMiddleware` 擋下，所以信任鏈成立。

---

## 本地開發（脫離 CSP）

把三個 CSP 變數留空即可。本 agent 會自動進入 dev mode：

```bash
# .env
CSP_SERVICE_TOKEN=
CSP_BASE_URL=
CSP_API_KEY=
LLM_URL=https://your-internal-llm/v1
LLM_API_KEY=...
```

啟動後 log 會有：

```
INFO CSP middleware installed from agentic_rag.api.middleware.csp_auth (dev_mode=True)
INFO CSP middleware: loaded_from=agentic_rag.api.middleware.csp_auth, enforced=False, csp_proxy_mode=False
```

`enforced=False` 就代表 `/v1/*` 開放存取，適合 `curl` / OpenWebUI 直連。

---

## 常見問題

### Q1. CSP 送過來的 `stream=true` 會不會斷？

Agent 端沒問題（FastAPI SSE streaming 走 `text/event-stream`）。要確認的是 **CSP proxy 本身會不會 buffer response** ——
如果 CSP 在你們部署裡開了 nginx buffer，請在 agent 這邊加上 header
`X-Accel-Buffering: no`（本 template 已預設），或讓 CSP proxy 透傳這個 header。

### Q2. `/health` 為什麼不驗 token？

這是**刻意的公開路徑**，因為 CSP health-checker 每分鐘 poll 一次決定 agent
是否在線。驗 token 反而會製造假 unhealthy 假象。敏感資訊不要放在 `/health`。

### Q3. 同一份 agent 要註冊多個 instance（不同 corpus）怎麼做？

在 CSP 用**不同 `name`** 註冊兩次，各自指向不同容器（或同一容器 + 不同 env）：

```yaml
# legal-rag.yaml
name: legal-rag
endpoint_url: http://agentic-rag-legal:24786
description_for_router: "法務文件 RAG — 合約、政策、法規"

# research-rag.yaml
name: research-rag
endpoint_url: http://agentic-rag-research:24786
description_for_router: "研究論文 RAG — 內部白皮書與外部期刊"
```

Router 依 `description_for_router` 的語意相似度挑選對象，所以**描述要準**。

### Q4. 部署到 staging 和 production，CSP token 要不同嗎？

**必須不同**。每個 CSP instance issues 自己的 token，把 staging token
裝進 production agent 會導致 401。CI/CD pipeline 應該在 deploy 時注入對應
環境的 `CSP_SERVICE_TOKEN`。

---

## 驗收 Checklist

在把 agent 丟上 staging 之前：

- [ ] `docker compose up -d` 後 `curl http://localhost:24786/health` 回 200
- [ ] 本機設 `CSP_SERVICE_TOKEN=test123`，不帶 header 打 `/v1/models` → 回 401
- [ ] 本機設 `CSP_SERVICE_TOKEN=test123`，帶 `-H "X-CSP-Service-Token: test123"` → 回 200
- [ ] 在 CSP UI 可以看到 agent、health 變 `healthy`
- [ ] 從 CSP UI 發一則測試訊息 → agent log 看到 `X-ANILA-User-Id` → 回覆正常串流
- [ ] `pytest tests/` 全綠（尤其是 `test_remediation.py`、`test_reranker.py`）

---

## 進階主題

- 想支援多租戶檢索 → 看上面 **「轉發過來的可信 headers」**
- 想改 `base_model` → 確認 CSP 的 ModelRegistry 有登錄對應模型
- 想讓 agent 不進 Router，只給指定用戶直呼 → `approval_status: pending` + 用 API key 直連
- 想在 agent 裡呼叫別的 agent → 用 `CSP_BASE_URL` 透過 Router 呼叫，不要 peer-to-peer

更深入的 CSP 機制見 myCSPPlatform 的文件：`myCSPPlatform/README.md`。

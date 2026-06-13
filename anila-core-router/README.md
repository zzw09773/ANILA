# anila-core-router

> **ANILA Router** — OpenAI 相容的請求自動分派服務。一層薄殼部署入口（`main.py`），實際分派邏輯在 [`anila-core`](../anila-core/README.md) SDK 的 `anila_core.api.router_server`。在 stack 裡以服務名 `router` 出現。

> 中文為主版；English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**：本服務存在於所有 ANILA 部署分支，內容跨分支一致。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

Router 對外暴露 OpenAI 相容的 `POST /v1/chat/completions`，並提供 pseudo-model `anila-router`。運作（依 `main.py` 與 `router_server` 確認）：

- Client 把 request body 的 `model` 設成 `anila-router` 即觸發自動分派；其他 `model` 值直接 forward 到 CSP，不經分派。
- Router 從 CSP `GET /v1/agents` 動態撈 agent manifest（含 `requires_encryption`）並 cache，再以 caller 的 API Key 呼叫主路由 LLM，由主 LLM 判斷是否分派給某個 agent（例如 `image-generator` 繪圖 agent）。
- 若決定分派，request 轉發到該 agent 的 `endpoint_url`，agent 的 SSE stream 逐 chunk 回傳給 caller。
- 主路由模型由 CSP 在 runtime 決定：`main.py` 每 60 秒從 CSP `GET /api/models/router-primary` 拉目前指定的主 LLM。CSP 未設主路由模型時，middleware 把 `/v1/chat/completions` 擋成 **503**，避免 silent fall-back 到錯誤 upstream。

> 定位：Router 是「用 anila-core runtime foundation 組一個分派服務」的部署範例，不是 core 本身。CSP（myCSPPlatform）才是平台權威的 control + data plane。

---

## 架構與技術棧

| 項目 | 內容（依 Dockerfile + 原始碼確認） |
|---|---|
| 語言 | Python 3.11（`python:3.11-slim`） |
| Web 框架 | FastAPI，由 `anila_core.api.router_server.create_router_app()` 建立 |
| ASGI server | `uvicorn`（`uvicorn main:app --host 0.0.0.0 --port 9000`） |
| HTTP client | `httpx`（非同步，呼叫 CSP / agent） |
| 核心相依 | `anila-core` SDK（純 runtime，**不含** `[rag]` extras）+ `pydantic-settings` |
| Port | `9000` |

`main.py` 不只是薄殼，它在 app factory 之外額外負責：

1. **主路由模型 TTL refresh**（`_refresh_primary` / `_ensure_primary`，60s TTL）+ `/v1/chat/completions` 的 503 gate middleware。
2. **Service token 三段式解析**（`_load_service_token` / `_self_bootstrap` / `_initialise_token_source`）：state file → `CSP_BOOTSTRAP_TOKEN` 自動 bootstrap → `CSP_SERVICE_TOKEN` legacy env，啟動 log 明示走哪條。
3. **CSP 回 401/403 時 hot-reload state file 一次** 後重試（admin 在 CSP 輪替 router-primary credential 後零停機）。

---

## 目錄結構

```
anila-core-router/
├── main.py        # 部署 entrypoint：create_router_app() + 主路由模型 TTL refresh
│                  #   + service-token state-file 三段式解析 + /router/primary-status debug endpoint
├── Dockerfile     # multi-stage；build context 須為 repo 根（會 COPY anila-core/）
└── README.md / README.en.md

# 實際分派邏輯在 anila-core SDK：
anila-core/src/anila_core/api/router_server.py   # create_router_app() + 分派 / SSE forward
```

---

## 啟動與部署

### 方式 1：repo 根 compose（推薦）

Router image 由本目錄 `Dockerfile` build，以服務名 `router` 跑：

```bash
# 於 repo 根
docker compose -f docker-compose-dev.yml up -d router   # dev
```

compose 中 `router` 只用 `expose: 9000`（**沒有** host port），UI 透過 `/router` 反向代理對外（見 UI 的 `VITE_ROUTER_BASE_URL` 預設 `/router`）。Router 等 `csp` healthy 後才啟動。

### 方式 2：自行 build image（build context 須為 repo 根）

```bash
docker build -f anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 -e CSP_BASE_URL=http://csp:8000 -e CSP_SERVICE_TOKEN=dev-service-token anila-core-router
```

### 方式 3：單機 uvicorn（開發）

```bash
pip install -e "../anila-core"        # 純 runtime，不需 RAG extras
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### 環境變數（依 `main.py` 確認）

| 變數 | 說明 | 預設 |
|---|---|---|
| `CSP_BASE_URL` | CSP 基底 URL；容器內為 `http://csp:8000` | `http://csp:8000` |
| `CSP_BOOTSTRAP_TOKEN` | 首次啟動 bootstrap token；entrypoint 寫進 state file | `""` |
| `CSP_SERVICE_TOKEN` | legacy fleet-shared shared-secret；state file 不存在時 fallback | `""` |
| `ANILA_ROUTER_STATE_DIR` | 持久化 service token 的目錄 | `/var/lib/anila-router` |
| `MODEL` | （已過時）改從 CSP `/api/models/router-primary` runtime 拉，啟動後被覆蓋 | — |

> Router **不**持有自己的 user API Key：它用 caller（UI / OpenAI SDK）的 Bearer API Key 回打 CSP data plane，因此 caller 看得到的 agent 與 Router 能分派的 agent 同步於該 API Key 的權限。Service token（Router→CSP 內部端點如 `/api/models/router-primary`）才走三段式解析。

---

## 與其他服務的關係

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents                  ──▶ CSP   取 agent manifest
   ├── GET /api/models/router-primary  ──▶ CSP   取主路由 LLM（X-CSP-Service-Token）
   ├── POST /v1/chat/completions       ──▶ CSP   呼叫主 LLM 判斷是否分派
   └── 分派 → agent endpoint_url        ──▶ 例：image-generator → http://flux2-dev-agent:8000
```

- **CSP（`CSP_BASE_URL`）**：Router 所有上游互動都經由 CSP — 撈 agent 清單、解析主路由模型、呼叫主 LLM。Router→CSP 內部端點以 `X-CSP-Service-Token` 認證。
- **Agents**：透過 CSP 註冊（如 `image-generator`，`endpoint_url: http://flux2-dev-agent:8000`）。主 LLM 判斷需要時 Router 分派並 forward SSE。
- **`/v1/agents` dispatch**：caller 能分派的 agent = 該 API Key 在 CSP 的 allowed agents — Router 不放大權限。

---

## 相關文件

- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- 多服務整合計畫（含 Router 角色）：[`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- Agent framework 架構：[`../docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation（SDK）：[`../anila-core/README.md`](../anila-core/README.md) · CSP：[`../myCSPPlatform/README.md`](../myCSPPlatform/README.md) · UI：[`../ANILA_UI/anila-ui/README.md`](../ANILA_UI/anila-ui/README.md)

---

## License

見 repo 根 [`LICENSE`](../LICENSE)。

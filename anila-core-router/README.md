# anila-core-router

**ANILA Router** — OpenAI 相容的請求自動分派服務。它是一層薄殼部署入口（`main.py`），實際分派邏輯實作在 [`anila-core`](../anila-core/README.md) SDK 的 `anila_core.api.router_server`。在 dev stack 裡它以服務名 `router` 出現。

> 中文為主要版本；英文鏡像見 [README.en.md](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

---

## 簡介 / Overview

Router 對外暴露一個 OpenAI 相容的 `POST /v1/chat/completions`，並提供一個 pseudo-model `anila-router`。其運作（依 `main.py` 與 `router_server` 程式確認）：

- Client 把 request body 的 `model` 設成 `anila-router` 即觸發自動分派；其他 `model` 值會直接 forward 到 CSP，不經分派邏輯。
- Router 從 CSP 的 `GET /v1/agents` 動態撈 agent manifest（含 `requires_encryption`）並 cache，再以 caller 的 API Key 呼叫主路由 LLM，由主 LLM 判斷是否要分派給某個 agent。
- 若決定分派，request 會被轉發到該 agent 的 `endpoint_url`，agent 的 SSE stream 逐 chunk 回傳給 caller。
- 主路由模型由 CSP 在 runtime 決定：`main.py` 每 60 秒從 CSP `GET /api/models/router-primary` 拉目前指定的主 LLM 名稱。當 CSP 沒有設定主路由模型時，middleware 會把 `/v1/chat/completions` 擋成 **503**，避免 silent fall-back 到錯誤 upstream。

> 定位：Router 是「用 ANILA Core runtime foundation 組一個分派服務」的部署範例，不是 core 本身。CSP（myCSPPlatform）才是平台權威的 control + data plane。

---

## 架構與技術棧 / Architecture & Stack

| 項目 | 內容（依 Dockerfile + 原始碼確認） |
|---|---|
| 語言 | Python 3.11（`python:3.11-slim`） |
| Web 框架 | FastAPI，由 `anila_core.api.router_server.create_router_app()` 建立 app |
| ASGI server | `uvicorn`（`uvicorn main:app --host 0.0.0.0 --port 9000`） |
| HTTP client | `httpx`（非同步，呼叫 CSP / agent） |
| 核心相依 | `anila-core` SDK（純 runtime 安裝，**不含** `[rag]` extras）+ `pydantic-settings` |
| Port | `9000` |

`main.py` 並非 1 行薄殼，它在 app factory 之外額外負責：

1. **主路由模型 TTL refresh**（`_refresh_primary` / `_ensure_primary`，60s TTL）+ `/v1/chat/completions` 的 503 gate middleware。
2. **Service token 三段式解析**（`_load_service_token` / `_self_bootstrap` / `_initialise_token_source`）：state file → `CSP_BOOTSTRAP_TOKEN` 自動 bootstrap → `CSP_SERVICE_TOKEN` legacy env，啟動 log 會明示走哪條。
3. **CSP 回 401/403 時 hot-reload state file 一次** 後重試（admin 在 CSP 輪替 router-primary credential 後零停機）。

---

## 目錄結構 / Layout

```
anila-core-router/
├── main.py        # 部署 entrypoint：create_router_app() + 主路由模型 TTL refresh
│                  # + service-token state-file 三段式解析 + /router/primary-status debug endpoint
├── Dockerfile     # Multi-stage build；build context 須為 repo 根（會 COPY anila-core/）
├── README.md      # 本檔（繁中主要版本）
└── README.en.md   # 英文鏡像

# 實際分派邏輯不在本目錄，而在 anila-core SDK：
anila-core/src/anila_core/api/router_server.py   # create_router_app() + 分派 / SSE forward
```

---

## 啟動與部署 / Setup & Run

### 方式 1：repo 根 dev compose（推薦）

Router 的 image 由本目錄的 `Dockerfile` build，並以服務名 `router` 跑在 `docker-compose-dev.yml`：

```yaml
# docker-compose-dev.yml（節錄，實際值請以檔案為準）
router:
  build:
    context: .
    dockerfile: anila-core-router/Dockerfile
  expose:
    - "9000"
  environment:
    CSP_BASE_URL: http://csp:8000
    CSP_SERVICE_TOKEN: ${CSP_SERVICE_TOKEN:-dev-service-token}
    MODEL: ${LLM_MODEL:-gemma4}
  depends_on:
    csp:
      condition: service_healthy
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://localhost:9000/health || exit 1"]
```

```bash
# 於 repo 根
docker compose -f docker-compose-dev.yml up -d router
```

注意：在 dev compose 中 `router` 只用 `expose: 9000`（**沒有** host port mapping）；UI 透過 `/router` 反向代理對外提供（見 UI 的 `VITE_ROUTER_BASE_URL` 預設 `/router`）。Router 會等 `csp` healthy 後才啟動。

### 方式 2：自行 build image

build context 須為 repo 根（Dockerfile 會 `COPY anila-core/` 進去）：

```bash
# 於 repo 根
docker build -f anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 \
  -e CSP_BASE_URL=http://csp:8000 \
  -e CSP_SERVICE_TOKEN=dev-service-token \
  anila-core-router
```

### 方式 3：單機 uvicorn（開發）

需先安裝 `anila-core` SDK（純 runtime，不需 RAG extras）：

```bash
pip install -e "../anila-core"
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### 環境變數（依 `main.py` 確認）

| 變數 | 說明 | 預設 |
|---|---|---|
| `CSP_BASE_URL` | CSP（myCSPPlatform）基底 URL；容器內為 `http://csp:8000` | `http://csp:8000` |
| `CSP_BOOTSTRAP_TOKEN` | 首次啟動的 bootstrap token；entrypoint 會寫進 state file | `""` |
| `CSP_SERVICE_TOKEN` | Legacy fleet-shared shared-secret；state file 不存在時 fallback | `""` |
| `ANILA_ROUTER_STATE_DIR` | 持久化 service token 的目錄 | `/var/lib/anila-router` |
| `MODEL` | （已過時）Router 改從 CSP `/api/models/router-primary` runtime 拉，啟動後會被覆蓋；保留作歷史相容 | — |

> Router **不**持有自己的 user API Key：它用 caller（UI / OpenAI SDK）的 Bearer API Key 回打 CSP data plane，因此 caller 看得到的 agent 與 Router 能分派的 agent 同步於該 API Key 的權限。
> Service token（Router→CSP 內部端點如 `/api/models/router-primary`）才走上述三段式解析。

---

## 與其他服務的關係 / Integration

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents              ──▶ CSP (CSP_BASE_URL)   取 agent manifest
   ├── GET /api/models/router-primary ─▶ CSP   取主路由 LLM（X-CSP-Service-Token）
   ├── POST /v1/chat/completions   ──▶ CSP   呼叫主 LLM 判斷是否分派
   └── 分派 → agent endpoint_url   ──▶ 例：已註冊 agent → http://<agent-service>:<port>
```

- **CSP（`CSP_BASE_URL`）**：Router 所有上游互動都經由 CSP — 撈 agent 清單、解析主路由模型、呼叫主 LLM。Router→CSP 的內部端點以 `X-CSP-Service-Token` header 認證（token 來源見三段式解析）。
- **Agents**：開發者把自訂 agent 透過 CSP 註冊（每個 agent 有自己的 `endpoint_url`）。主 LLM 判斷需要某 agent 時，Router 把 request 分派給該 agent 並 forward 其 SSE stream。
- **`/v1/agents` dispatch**：caller 能分派出去的 agent，等於該 caller API Key 在 CSP 的 allowed agents — Router 不放大權限。

---

## 相關文件 / Related docs

（以下路徑皆已確認存在）

- 平台整體：[repo 根 README](../README.md)
- Agent framework 架構：[`docs/agent-framework/anila-agent-framework-architecture.md`](../docs/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation（SDK）：[`anila-core/README.md`](../anila-core/README.md)
- CSP 平台：[`myCSPPlatform/README.md`](../myCSPPlatform/README.md)
- UI：[`ANILA_UI/anila-ui/README.md`](../ANILA_UI/anila-ui/README.md)

> 注意：舊版 README 連結的 `AgenticRAG/README.md` 已不在 repo 根（目前僅 `docs/agenticrag/` 留有文件），故此處不再列出。

---

## License

見 repo 根 [`LICENSE`](../LICENSE)。

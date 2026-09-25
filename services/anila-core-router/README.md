# anila-core-router

> **ANILA Router** — OpenAI 相容的請求自動分派服務。一層**薄殼部署單元**(`main.py`);實際分派邏輯在 [`anila-core`](../../packages/anila-core/README.md) SDK 的 `anila_core.api.router_server`。在 stack 裡以服務名 `router` 出現。

> 中文為主版;English mirror:[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**:本服務存在於所有 ANILA 部署分支,內容跨分支一致。分支策略見根目錄 [`README.md`](../../README.md) （現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 簡介

Router 對外暴露 OpenAI 相容的 `POST /v1/chat/completions`,並公告 pseudo-model `anila-router`。運作(依 `main.py` 與 SDK `router_server` 確認):

- Client 把 request body 的 `model` 設成 `anila-router` 以把流量導到本服務。實作上 `chat_completions`(`router_server.py`)**不檢查 `model` 欄位**:不論 `model` 值為何,每個 request 一律走完整分派流程。`anila-router` 只是 `GET /v1/models` 對外公告用的 pseudo-model。
- **Agent discovery 只有一個來源**:Router 的 `RemoteAgentRegistry` 從 CSP `GET /v1/agents` 動態撈 agent manifest 並 TTL cache — 沒有本地 registry、不直連上游。再以 caller 的 API Key 呼叫主路由 LLM,由主 LLM 判斷是否分派給某個 agent(例如 `image-generator` 繪圖 agent)。
- 若決定分派,request 轉發到該 agent 的 `endpoint_url`,agent 的 SSE stream 逐 chunk 回傳給 caller。
- 主路由模型由 CSP 在 runtime 決定:`main.py` 每 60 秒從 CSP `GET /api/models/router-primary` 拉目前指定的主 LLM。CSP 未設主路由模型時,middleware 把 `/v1/chat/completions` 擋成 **503**,避免 silent fall-back 到錯誤 upstream。

> 定位:Router 是「用 anila-core runtime foundation 組一個分派服務」的**部署範例**,不是 core 本身。CSP([`services/csp`](../csp/),前身 `myCSPPlatform`)才是平台權威的 control + data plane。

---

## 架構與技術棧

| 項目 | 內容(依 Dockerfile + 原始碼確認) |
|---|---|
| 語言 | Python 3.11(`python:3.11-slim`) |
| Web 框架 | FastAPI,由 `anila_core.api.router_server.create_router_app()` 建立 |
| ASGI server | `uvicorn`(`uvicorn main:app --host 0.0.0.0 --port 9000`) |
| HTTP client | `httpx`(非同步,呼叫 CSP / agent) |
| 核心相依 | `anila-core` SDK(純 runtime,**不含** `[rag]` extras)+ `pydantic-settings` |
| Port | `9000`(compose 內只 `expose`,不對 host 開 port) |

`main.py` 不只是薄殼,它在 app factory 之外額外負責:

1. **主路由模型 TTL refresh**(`_refresh_primary` / `_ensure_primary`,`PRIMARY_TTL_SECONDS=60`)+ `/v1/chat/completions` 的 503 gate middleware。主模型沒有背景 timer:startup 觸發一次,之後由 gate middleware 在過期時 lazy refresh。憑證檔另有一個週期重讀(預設 30 秒,見 `ANILA_SERVICE_TOKEN_RELOAD_SECONDS`)。
2. **Service token 解析**(`_load_service_token` / `_initialise_token_source`):有設 `ANILA_SERVICE_TOKEN_FILE` 時只讀那個檔。檔案不在是 `file_missing`,讀不到或是空的是 `file_error`;兩種都不改走別的憑證,並照週期重讀,CSP 寫上檔之後會自己恢復。後三個(state file、`CSP_BOOTSTRAP_TOKEN`、`CSP_SERVICE_TOKEN`)只在 `ANILA_SERVICE_TOKEN_FILE` **沒設**時才用。`CSP_BOOTSTRAP_TOKEN` 有值且 state file 還沒有時,會把該值抄進 state file(mode 0600),**不做 HTTP 交換**。啟動 log 只記來源名稱,不記明文。`/health` 的 `token_source` 是 `file`、`file_missing`、`file_error`、`state_file`、`bootstrap`、`legacy_env` 或 `none`。
3. **憑證檔變更會重讀**;CSP 回 401/403 時再強制讀一次後重試,然後才放棄。

> `main.py` 對 CSP 只主動發一個呼叫 `GET /api/models/router-primary`(帶 `X-CSP-Service-Token`);`GET /v1/agents`、`POST /v1/chat/completions`、agent dispatch + SSE forward 都在 SDK `router_server.py`。Router **不**持有自己的 user API Key:它用 caller(UI / OpenAI SDK)的 Bearer API Key 回打 CSP data plane,因此 caller 看得到的 agent = Router 能分派的 agent(不放大權限)。

---

## 目錄結構

```
services/anila-core-router/
├── main.py        # 部署 entrypoint:create_router_app() + 主路由模型 TTL refresh
│                  #   + 憑證檔/state-file token 解析 + /router/primary-status debug endpoint
├── Dockerfile     # multi-stage;build context 須為 repo 根(會 COPY packages/anila-core/)
└── README.md / README.en.md

# 實際分派邏輯在 anila-core SDK:
packages/anila-core/src/anila_core/api/router_server.py   # create_router_app() + 分派 / SSE forward + trace 產生
```

---

## 啟動與部署

### 方式 1:repo 根 compose(推薦)

Router image 由本目錄 `Dockerfile` build,以服務名 `router` 跑。根 compose 檔皆為 shim:

```bash
# 於 repo 根
docker compose -f compose.dev.yaml up -d router   # dev  → include infra/compose/dev.yml
docker compose -f compose.yaml     up -d router   # prod → include infra/compose/platform.yml
```

compose 中 `router` 只用 `expose: 9000`(**沒有** host port),外部走 nginx `/router/*` → `router:9000`(UI 的 `VITE_ROUTER_BASE_URL` 預設 `/router`)。Router 等 `csp` healthy 後才啟動。

### 方式 2:自行 build image(build context 須為 repo 根)

```bash
docker build -f services/anila-core-router/Dockerfile -t anila-core-router .
docker run -p 9000:9000 -e CSP_BASE_URL=http://csp:8000 -e CSP_SERVICE_TOKEN=dev-service-token anila-core-router
```

### 方式 3:單機 uvicorn(開發)

```bash
pip install -e "../../packages/anila-core"        # 純 runtime,不需 RAG extras
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000 --log-level info
```

### 環境變數

`main.py` 讀取(raw `os.environ`):

| 變數 | 說明 | 預設 |
|---|---|---|
| `CSP_BASE_URL` | CSP 基底 URL;容器內為 `http://csp:8000` | `http://csp:8000` |
| `ANILA_SERVICE_TOKEN_FILE` | CSP 寫好的憑證檔。compose 為 `/run/anila/service-clients/router-primary.token` | 未設 |
| `ANILA_SERVICE_TOKEN_RELOAD_SECONDS` | 週期重讀憑證檔的間隔,最短 5 秒 | `30` |
| `CSP_BOOTSTRAP_TOKEN` | 只在 `ANILA_SERVICE_TOKEN_FILE` 沒設、且 state file 也沒有時,抄進 state file | `""` |
| `CSP_SERVICE_TOKEN` | 只在憑證檔路徑沒設時的最後後援:舊式共用祕密。router-primary 有自己的憑證後,打不進 router-only 端點 | `""` |
| `ANILA_ROUTER_STATE_DIR` | state file `service_token.json`(mode 0600)的目錄。憑證檔路徑有設時不用它 | `/var/lib/anila-router` |

SDK(`router_server`)另讀 **Full Trace opt-in** env(見 doc `09` §10 凍結線):

| 變數 | 說明 | 預設 |
|---|---|---|
| `ANILA_TRACE_ENDPOINT` | 未設 → 整條 trace 路徑 no-op(行為與未接前一致)。bare flag(`1`/`true`/`on`/`yes`/`default`)→ 用 `CSP_BASE_URL`;其他值 → 顯式 trace base URL。span 以 `POST {base}/v1/traces/{trace_id}/spans` 上傳並 mirror 進 `anila.spans` SSE | `""`(關) |
| `ANILA_TRACE_TOKEN` | trace export 用的 service token;未設則 fallback `CSP_SERVICE_TOKEN` | `""` |

> `main.py` **不讀 `MODEL`**(主路由模型完全由 CSP `/api/models/router-primary` runtime 決定;compose 的 `router` 服務雖仍帶 `MODEL: ${LLM_MODEL:-gemma4}`,純屬殘留 env,不影響行為)。

---

## 與其他服務的關係

```
Client (UI / OpenAI SDK)
   │  POST /v1/chat/completions  (model=anila-router, Bearer sk-...)
   ▼
router (:9000)
   ├── GET /v1/agents                  ──▶ CSP   取 agent manifest(唯一 discovery 來源)
   ├── GET /api/models/router-primary  ──▶ CSP   取主路由 LLM(X-CSP-Service-Token)
   ├── POST /v1/chat/completions       ──▶ CSP   呼叫主 LLM 判斷是否分派
   ├── 分派 → agent endpoint_url        ──▶ 例:image-generator → http://flux2-dev-agent:8000
   └── (可選) POST /v1/traces/{id}/spans ──▶ CSP  Full Trace 匯出(ANILA_TRACE_ENDPOINT 開啟時)
```

- **CSP(`CSP_BASE_URL`)**:Router 所有上游互動都經由 CSP — 撈 agent 清單、解析主路由模型、呼叫主 LLM。Router→CSP 內部端點以 `X-CSP-Service-Token` 認證。
- **Agents**:透過 CSP 註冊(如 `image-generator`,`endpoint_url: http://flux2-dev-agent:8000`)。主 LLM 判斷需要時 Router 分派並 forward SSE。
- **`/router/primary-status`**(debug):回傳 cache 的主路由模型名、last error、`service_token_source`、state file 路徑。

---

## 相關文件

- 平台整體:[`../../README.md`](../../README.md) · 分支策略:[`../../docs/archive/branch-sync-backlog.md`](../../docs/archive/branch-sync-backlog.md)
- Redesign 設計沿革（收斂紀錄）:constitution [`../../docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md) · runtime/registry 協定 [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md) · API/事件凍結線(含 SSE + `/v1/traces`)[`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md)。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 多服務整合計畫(含 Router 角色):[`../../docs/platform/multi-service-integration-plan.md`](../../docs/platform/multi-service-integration-plan.md)
- Agent framework 架構:[`../../docs/archive/agent-framework/anila-agent-framework-architecture.md`](../../docs/archive/agent-framework/anila-agent-framework-architecture.md)
- Runtime foundation(SDK):[`../../packages/anila-core/README.md`](../../packages/anila-core/README.md) · CSP:[`../csp/README.md`](../csp/README.md) · Shell:[`../../apps/anila-shell/README.md`](../../apps/anila-shell/README.md)

---

## License

見 repo 根 [`LICENSE`](../../LICENSE)。

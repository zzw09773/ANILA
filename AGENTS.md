# ANILA 專案級 Codex 指示

> **現行入口：[`docs/CURRENT-STATUS.md`](docs/CURRENT-STATUS.md)。**
> 開發線是單一 `main`。`CLAUDE.md` 不存在。下文「架構／七分支」已過時，只保留工作慣例（怎麼讀碼、怎麼驗證、不要擅自 commit）。

> 🔴 **2026-08-17 盤點：本檔的「架構描述」已大幅過時，不可照著辦事。**
> 下文的**工作慣例**（怎麼讀碼、怎麼驗證、不要擅自 commit）仍然有用；
> 但凡是描述「這個 repo 長什麼樣」的段落，一律以 `docs/CURRENT-STATUS.md` 與 `PLAN.md` 為準（`CLAUDE.md` 不存在）。
>
> 七分支模型與分支同步已移到 [`docs/archive/agents-seven-branch-model.md`](docs/archive/agents-seven-branch-model.md)。
>
> **現行狀態看這兩份**：環境事實與陷阱＝`docs/CURRENT-STATUS.md`；現況與執行順序＝`PLAN.md`（專案權威）；
> 規格＝`SYSTEM-MAP.md`；重啟歷史＝`RESTART-FROM-REDESIGN.md`。
>
> ⚠ 另注意：檔頭自稱「最後更新 2026-06-22」，但內文已引用 07-31 之後的產物（如 `r1_0027`）——
> **檔頭日期與內文並不一致，兩者都不能當作新鮮度的證據。**
> ⚠ 本檔內文沿用「國軍／軍方」等舊用語；平台擁有者已兩度糾正——**這是中科院（NCSIST）院內平台**。
>
> ---
>
> 最後更新: 2026-06-22
>
> 本檔是 ANILA repo 的專案級工作規範。全域 `AGENTS.md` 仍適用；若與本檔衝突，以本檔為準。回覆一律使用繁體中文與台灣用語。

## 0. 任務定位

- 這是多服務 monorepo，不是單一前後端專案。動手前先確認你要改的是哪個服務、哪條分支、哪個部署目標。
- 不要只讀 README。至少要讀相關程式碼入口、compose、Dockerfile、測試、設定與 branch 檔案。
- 預設不要 commit/push；除非 user 明確要求。
- 目前 repo 原本沒有專案層 `AGENTS.md`。此檔建立後，後續 agent 必須先讀本檔再動手。

## 1. 必讀脈絡

動手前讀：

- [`docs/CURRENT-STATUS.md`](docs/CURRENT-STATUS.md)
- [`PLAN.md`](PLAN.md)
- [`SYSTEM-MAP.md`](SYSTEM-MAP.md)

## 2. 專案地圖

| 路徑 | 角色 | 技術 / 注意事項 |
|---|---|---|
| `services/csp` | CSP control plane + data plane | FastAPI + SQLAlchemy/Alembic + PostgreSQL/pgvector + Redis；權威管理 users/API keys/models/agents/conversations/ingestion/usage/JWKS/revocation；Vue 管理前端在 `apps/csp-governance-ui`。 |
| `packages/anila-core` | 共用 Python runtime library | agent runtime、Router app factory、tools、memory、SSRF guard、credential crypto、pgvector store、ingestion/parser primitives。不是常駐服務。 |
| `services/anila-core-router` | OpenAI-compatible Router 部署 wrapper | 實際 app 由 `anila_core.api.router_server.create_router_app()` 產生；負責 primary model refresh、agent dispatch、SSE passthrough、多輪 session/resume。 |
| `packages/anila-agent` | 官方 agent starter/template | OpenAI Agents SDK 型 runtime；可 CLI 或 FastAPI service wrapper；root compose 以唯讀模板掛進 CSP。 |
| `services/ingestion-worker` | Arq background worker | parse -> image caption -> chunk -> embed -> pgvector -> rule/LLM/similarity relations；無 HTTP port。 |
| `services/anila-studio` | artifact 生成服務 | FastAPI；不共用 CSP DB，透過 HTTP/JWKS/Redis 與 CSP 協作；處理 slides/reports/mindmaps/infographics/datatables。 |
| `apps/anilalm` | knowledge-base + Studio SPA | React/TS/Vite；另有 `services/pptx-renderer` Node renderer，前端不直接打 renderer，由 `anila-studio` server-to-server 呼叫。 |
| `apps/anila-shell` | runtime chat UI | React/Vite；cookie + CSRF；串 CSP `/api`, `/v1` 與 Router `/router`。 |
| `infra/models` | 獨立模型 stack | `anila-models` compose（`infra/models/docker-compose.yml` + `infra/models/src`；權重在 `models/model` 不搬）；LLM/embedding/FLUX/image-agent shim 走 external network `anila-models-net`，不開 host port。FLUX 服務源碼在 `services/flux2-dev{,-agent}`。 |
| `runtime_logic` | 參考資料 | reference-only；gitignored source tree 不能當 runtime import 或 deployment source。 |

## 3. 分支模型

七分支模型與同步步驟見 [`docs/archive/agents-seven-branch-model.md`](docs/archive/agents-seven-branch-model.md)。

## 4. Compose / 部署規則

- 主 stack 定義在 `infra/compose/platform.yml`，由 root shim `compose.yaml`（`include:`）帶入，project `anila`；在 repo 根目錄跑 `docker compose` 即可。產品入口是 nginx **443**；**4443 只做重新導向**。80 轉 https。CSP/Router/model 不應直接開 host port。DB 只綁 loopback `127.0.0.1:5433:5432`。
- `infra/compose/dev.yml`（root shim `compose.dev.yaml`）是隔離 dev stack，project `anila-platform-dev`：nginx `8080/8443/9443`、DB `127.0.0.1:5533`、`share-dev/`、`*-dev` volumes、`anila-dev-net`。不要混用 live volumes/ports。
- `infra/models/docker-compose.yml` 是獨立模型 stack，project `anila-models`。第一次先建立 external network：
  ```bash
  docker network create anila-models-net
  ```
- 模型 stack lifecycle 獨立，先起 models，再起平台：
  ```bash
  docker compose -f infra/models/docker-compose.yml up -d
  docker compose up -d --build
  ```
- root compose 沒有 `env_file:`；Compose 會自動讀根目錄 `.env` 做 `${...}` interpolation。不要把 `.env` 或任何 secret commit。
- `.env`、compose、build args 或 mounted config 變更後，用 `docker compose up -d` recreate；不要只 `docker restart`。
- CSP 正式 image 使用 `infra/docker/csp.Dockerfile`，而且現在**只剩這一份**——曾經另有一份 compose 從不建的 `services/csp/Dockerfile`，已於 2026-08-06 刪除（FAKE-CONTROLS #50：改對了檔案但那個檔案沒人建）。
- csp / ingestion-worker 以 uid **10001** 跑、pptx-renderer 以 `node` (1000) 跑。bind mount 的所有權由 host 決定，映像裡 chown 沒有用 → 部署前必須跑 `infra/deployment/scripts/fix-runtime-ownership.sh`（deploy-prod.sh 的 `deploy`/`up`/`rebuild` 三條路徑與 intranet-deploy.sh `[4c]` 都已接進去）。涵蓋四個掛載：`share/uploads/ingestion`、`share/attachments`、`share/pki`、`secrets/`。少了它的症狀是**容器全綠、上傳回 500、JWKS 回 500、出向 https 全掛**。
- ⚠ `secrets/` 的放寬是**白名單**：日後新增「csp 執行期要讀的 secrets 檔」必須在該腳本加一行 `widen_file`，否則讀不到（腳本每次會把刻意沒動的檔列出來，所以漏掉看得見）。`infra/compose/dev.yml` 的 `share-dev/` 刻意不接腳本，一行解法寫在該檔的掛載註解裡。
- prod 部署優先走：
  ```bash
  bash infra/deployment/scripts/deploy-prod.sh preflight
  bash infra/deployment/scripts/deploy-prod.sh deploy
  ```
  不要跳過 preflight、JWT keypair、health 與 endpoint smoke verify。
- nginx 路由重點（都在 **443**，產品入口）：
  - `/anila/`：任務中心；`/anilalm/`：知識庫；`/api`、`/v1`、`/v2`：CSP
  - `/api/(studio|reports|mindmaps|infographics|datatables)/`: `anila-studio`
  - `/router/`: Router，會 strip prefix
  - **4443**：舊書籤重新導向到 443，不是另一個產品入口

## 5. 資料流與契約

Chat / Router / Agent：

- 使用者或 SDK 打 `/v1/chat/completions`。
- CSP data plane 驗 `sk-*` API key 或 JWT/cookie。
- `model=anila-router` 時交給 Router。
- Router 從 CSP `/v1/agents` 取 agent manifest，主 LLM 回一般答案或 `DISPATCH:<agent_id>:<query>`。
- dispatch 透過 CSP proxy 呼叫 agent endpoint；streaming 要保留 OpenAI `data:` 與 named `event:`，並轉成 `anila.*` 事件給前端。
- 改 `packages/anila-core/src/anila_core/api/router_server.py` 時，必跑 Router streaming/resume/session owner 相關測試。

Ingestion / RAG：

- CSP 建 ingestion job 並 enqueue Redis/Arq。
- `ingestion-worker` 讀上傳檔，parser 抽文字與圖片。
- VLM caption 可把 `[[IMAGE:id]]` 替換為圖片描述。
- chunker 產 parent/leaf chunks。
- leaf chunk 呼叫 embedding，寫入 pgvector。
- rule/LLM/similarity relations 是 best-effort；檔案 `indexed` 不代表 relation graph 一定完成。
- runtime DB 必須用 `csp_app` role；migration 才能用 superuser。用 superuser 跑 runtime 查詢會繞過 RLS。
- pgvector/collection 存取需維持 collection scope，例如 `SET LOCAL anila.collection_id`；不得用方便查詢破壞隔離。
- Embedding 契約目前是 `halfvec(4000)`。NV-Embed 原生 4096 維由 worker 裁切到 4000；換模型要同步 migration、worker、retriever 與測試。

Studio / Artifact：

- `anila-studio` 不共用 CSP DB；透過 CSP HTTP contract、JWKS、Redis revocation cache 協作。
- revocation cache 未 ready 時應 fail-closed，不要改成放行。
- 改 `anila-studio` API schema 後，先匯出 OpenAPI，再更新 ANILALM types：
  ```bash
  cd services/anila-studio && python scripts/export-openapi.py
  cd ../../apps/anilalm && npm run gen:studio-types
  ```

Image generation：

- Router dispatch `image-generator` -> CSP proxy -> `flux2-dev-agent` -> `flux2-dev /generate`。
- `flux2-dev-agent` 本身無 auth，必須只放在內網/CSP 後面，不得直接對外。
- FLUX.2-dev README 標示 Non-Commercial license；production 前需法務確認。

## 6. Auth / Security

- CSP cookie flow：`anila_access_token` httpOnly path `/`、`anila_refresh_token` httpOnly path `/api/auth/refresh`、`anila_csrf` 非 httpOnly path `/`。Cookie-auth mutating request 必須帶 `X-CSRF-Token`；Bearer `Authorization` 路徑才可跳過 CSRF。
- CSP JWT 已切 RS256；JWKS 在 `/.well-known/jwks.json`。`anila-studio` 只驗 public key，不共享私鑰。
- Data plane `/v1/*` 接受 `sk-*` API key 或 JWT/cookie。**Agent 派工身分（P2.1）**：平台現簽約 5 分鐘 RS256 JWT（`Authorization: Bearer`；claims=`user_id`/`department`/`agent_id`），agent 以 `/.well-known/jwks.json` 驗簽；開發者不領 `csk-`／`CSP_SERVICE_TOKEN`。Router／worker 等**平台內部** s2s 仍可能使用 service clients（與 agent 派工 JWT 無關）。舊 `bsk-`→`csk-` bootstrap 僅歷史協議，見 `docs/archive/agent-framework/csp-agent-bootstrap-protocol.md` 頂部取代說明。
- `apps/anila-shell` 與 `apps/anilalm` 都以治理中心 `/login` 的 cookie 為準。anilalm 載入時清掉舊的 localStorage token。
- prod 不可設 `ANILA_ALLOW_DEV_SECRET=1`，不可使用 `dev-secret-key-change-in-prod`, `dev-service-token`, `changeme`, `sk-internal-worker-changeme` 等 fallback。
- 不得提交 `.env`, `secrets/`, JWT private key, API key, `.pem`, `.key`, 內部憑證私鑰。`services/csp/secrets/.gitignore` 可追蹤，但 key 檔不可追蹤。
- 所有 user-supplied endpoint 維持 SSRF guard。新增模型或 agent docker service name 時，同步 `ANILA_TRUSTED_HOSTS` 或 trusted-hosts UI；不要用全域放寬取代 allow-list。
- nginx CSP header 目前仍含 `'unsafe-inline'` / `'unsafe-eval'`。若要收斂，要先實測 React/Vue/markdown/mermaid 與 Studio artifact 不破。

## 7. 測試與驗證矩陣

不要只看 health 200。SPA catch-all 可能對不存在路由回 `200 text/html`，驗證時要看真實使用路徑、Content-Type、正式 HTTP API 與認證。

| 範圍 | 指令 / 方法 |
|---|---|
| `packages/anila-agent` | `cd packages/anila-agent && make install && make test && make lint`；live endpoint 才跑 `make test-live`。 |
| `packages/anila-core` | `cd packages/anila-core && pip install -e '.[dev,rag]' && pytest`；DB/RLS 類另跑 `pytest -m integration`；品質跑 `ruff check src tests`, `mypy src`。 |
| `services/ingestion-worker` | `cd services/ingestion-worker && pip install -e '../../packages/anila-core[rag]' -e '.[dev]' && pytest && ruff check src tests`。 |
| `services/csp` | `cd services/csp && python -m pytest`；schema/API 改動要驗 Alembic startup。 |
| `apps/csp-governance-ui` | `cd apps/csp-governance-ui && npm test && npm run build`。 |
| `apps/anila-shell` | `cd apps/anila-shell && npm test && npm run build`。沒有 Playwright e2e。 |
| `apps/anilalm` | `cd apps/anilalm && npm run typecheck && npm run build`；schema 變更後先 `npm run gen:studio-types`。 |
| `services/pptx-renderer` | `cd services/pptx-renderer && npm test`。`jszip` 是直接依賴（`3.10.1`）。 |
| `services/anila-studio` | `cd services/anila-studio && pip install -e '.[dev]' && pytest`。 |
| `services/flux2-dev` | `cd services/flux2-dev && pip install -e '.[test]' && pytest`；測試用 mock pipeline，不載大型權重。 |
| `services/flux2-dev-agent` | `cd services/flux2-dev-agent && pip install -e '.[test]' && pytest`。 |
| 整合 stack | `docker compose -f compose.dev.yaml up -d --build` 或 root `docker compose up -d --build`；再測 `/api/health`, `/router/health`, login, `/v1/chat/completions`, agent dispatch, ingestion upload -> search。 |
| prod | `bash infra/deployment/scripts/deploy-prod.sh preflight`, `... deploy`, `... verify`。 |

端到端驗證至少覆蓋：

- 前端能登入並取得 CSRF/cookie。
- `/v1/chat/completions` 可用正式 API key 或 cookie/JWT 路徑呼叫。
- `anila-router` 能正常回一般答案與 dispatch agent。
- ingestion 從上傳、job、worker、pgvector 到 search 都跑通。
- Studio artifact 至少跑一條代表性 job，確認 `anila-studio`、`pptx-renderer`、CSP auth/revocation 互通。
- Docker 服務與模型服務健康，且沒有新 import error / ModuleNotFoundError。

## 8. 已知雷區 / 待確認事項

- `prod-public-passwd` 已移除 code-server，但 `n8n` / `gitlab` 仍在 compose 與 nginx 對外。外網部署若不需要，必須移除 service、nginx location 與 `AUTO_REGISTER_LINKS`。
- `infra/deployment/scripts/phase1-e2e.sh` 仍測 `/codeserver/`，對目前 `prod-public-passwd` 是過時殘留，不可當 prod 驗證依據。
- `apps/anila-shell` 的 `BASE_PATH` / nginx `/anila/` routing 曾被 README 提到，但 root/dev compose 主要只傳 CSP/Router build args。重建 UI 前先確認資產路徑。
- `services/flux2-dev-agent` volume 目前偏向 `share-dev/uploads/flux`，但 prod deploy 腳本檢查 `share/uploads/flux`。prod 啟用 image-generator 前確認落地路徑與 nginx `/uploads/flux` 一致。
- Router state 預設 `/var/lib/anila-router`；若使用 state-file/bootstrap token，要確認容器 user、volume 與權限，避免寫檔失敗。
- `.doc` parser 可能依賴系統 `antiword`；若 worker image 沒裝會失敗。
- `DispatchIdentityMiddleware`（`packages/anila-core/src/anila_core/api/middleware/dispatch_auth.py`）在 production 不可放行空的派工設定；`dev_mode=False` 且沒指到平台時要 fail-closed。

## 9. 寫碼規則

- 小改動維持小範圍；不要順手重構整個 monorepo。
- schema 改動必須有 Alembic migration，並考慮既有資料與 branch fork。
- API 邊界用 Pydantic/schema 驗證；SQL 參數化；HTML/markdown 輸出要淨化；錯誤訊息不得洩漏 secret。
- 新增 endpoint 要檢查 authn/authz、CSRF/Bearer 例外、rate limit、audit log 是否需要。
- 改 Router streaming/dispatch/resume/session owner 必補 focused tests。
- 改 ingestion parser/chunker/embedding/relations 要補 unit tests；牽涉 pgvector/RLS 時加 integration tests。
- 改前端要跑 `npm run build`，不要只跑 `tsc` 或 dev server。
- 改 `anila-studio` contract 要同步 OpenAPI/types 與 ANILALM 呼叫端。
- 修改 compose/env/nginx 後，要用 recreate 驗證，不以 `docker restart` 作為套用設定的證據。

## 10. 交付前 checklist

- [ ] 已讀本檔與相關子專案程式碼，不只 README。
- [ ] 已確認目前 branch 與目標分支，沒有誤改 downstream fork 區。
- [ ] 無 `.env`、key、token、JWT private key、憑證私鑰被加入 git。
- [ ] prod 路徑沒有 dev fallback secret 或 `ANILA_ALLOW_DEV_SECRET=1`。
- [ ] DB runtime 使用 `csp_app`，migration 才用 superuser。
- [ ] 新增模型/agent endpoint 已處理 SSRF allow-list。
- [ ] 有對應測試；不能跑的測試要如實說明原因。
- [ ] 端到端驗證走正式 HTTP API + auth，不直連 DB 假裝完成。
- [ ] 回報包含：改了什麼、為什麼、怎麼驗、剩餘風險。

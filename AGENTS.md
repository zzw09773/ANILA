# ANILA 專案級 Codex 指示

> 最後更新: 2026-06-22
>
> 本檔是 ANILA repo 的專案級工作規範。全域 `AGENTS.md` 仍適用；若與本檔衝突，以本檔為準。回覆一律使用繁體中文與台灣用語。

## 0. 任務定位

- 這是多服務 monorepo，不是單一前後端專案。動手前先確認你要改的是哪個服務、哪條分支、哪個部署目標。
- 不要只讀 README。至少要讀相關程式碼入口、compose、Dockerfile、測試、設定與 branch 檔案。
- 預設不要 commit/push；除非 user 明確要求。
- 目前 repo 原本沒有專案層 `AGENTS.md`。此檔建立後，後續 agent 必須先讀本檔再動手。

## 1. 必讀脈絡

動手前依任務範圍讀下列檔案：

- 整體與分支: `README.md`, `docs/branch-sync-backlog.md`
- 主 stack: `compose.yaml`(root shim,include `infra/compose/platform.yml`)
- dev stack: `compose.dev.yaml`(root shim,include `infra/compose/dev.yml`)
- 模型 stack: `infra/models/docker-compose.yml`
- nginx 入口: `infra/nginx/anila.conf`
- prod 部署: `infra/deployment/scripts/deploy-prod.sh`
- CSP backend: `services/csp/app/main.py`, `services/csp/app/config.py`
- Router: `services/anila-core-router/main.py`, `packages/anila-core/src/anila_core/api/router_server.py`
- Ingestion: `services/ingestion-worker/src/ingestion_worker/main.py`, `services/ingestion-worker/src/ingestion_worker/settings.py`
- Agent template: `packages/anila-agent/app.py`, `packages/anila-agent/anila_agent/config.py`, `packages/anila-agent/Makefile`
- Studio: `services/anila-studio/app/main.py`, `services/anila-studio/pyproject.toml`
- UI: `apps/anila-shell/package.json`, `apps/anilalm/package.json`, `apps/csp-governance-ui/package.json`

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

`main` 是 SSOT。通用 feature、bugfix、docs、測試先進 `main`，再同步 downstream。downstream 之間不要互相 merge；需要跨分支修補時，先進 `main`，再分別 port。

### 3.1 現況實測（2026-07-24 分支精簡，改動前請重新量測）

> ⚠ 本節更早的內容已過時，且過時方向會誤導決策。以下為 2026-07-24 分支裁撤後的實測結果。
> 歷史教訓：07-10 至 07-22 間分支曾雙向漂移（downstream 落後 main 170–199 commit、
> 「領先 11 commit」經 `git patch-id --stable` 證實 10/11 是 main 上同卵雙胞的假警報）。
> 07-22 重建六條分支後，07-24 進一步裁撤零語意分支：`dev-public`／`dev-military`
> （收斂後與 main 只差 `.env.example` 識別橫幅、零旗標差異——開發直接用 `main`）、
> `prod-public-passwd`（無存活部署以其為重建源；姿態可由 main 範本＋部署 `.env` 重現）、
> `anila-redesign` 與歷史 docs 殘枝（全數已完整合併，零內容損失）。

**程式碼已收斂成單一版本。** 三條 downstream 分支中，**兩條與 `main` 的差異只有 `.env.example` 一個檔案**：

| Branch | 與 `main` 的非文件差異 |
|---|---|
| `prod-military-passwd` | 只有 `.env.example` |
| `prod-intranet-card` | 只有 `.env.example` |
| `trial-military` | `.env.example` ＋ 8 個開發者視圖刪減檔（唯一刪減型分支） |

推論三點：

1. **`prod-intranet-card` 已不是 card/SSO code fork。** `services/csp/app/services/card_auth.py`、`card_auth_service.py`、`auth_service.py`、`api/auth/*` 在它與 `main` 上位元組相同——card/SSO 程式碼已收進 `main`，由 `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` 兩個旗標決定行為。舊版所稱的「永久 fork 熱區」不再存在。
2. **不要用 commit count 判斷 porting 負擔。** `git rev-list --left-right --count` 會顯示 12/11 之類的 divergence，那是 **commit 圖差異**（同語意不同 SHA 的 cherry-pick），不是內容差異。一律用 `git diff --name-only` 看檔案，疑似「領先」的 commit 用 `git patch-id --stable` 與 main 對比（07-22 的「11 個領先功能 commit」即以此法證偽）。
3. **真正的風險已經轉移。** 正式部署身分現在由**可變的環境設定**決定，而不是由不可變的簽章 release artifact 決定。長期解法是單一 code line ＋ 簽章 deployment profile。見 `docs/planning/anila-development-roadmap.md` §2.4 與 Gate 6。

量測指令（不要 checkout）：

```bash
git fetch --all --prune
git diff --name-only origin/main origin/<branch> -- . ':(exclude)docs/**' ':(exclude)*.md'
```

### 3.2 分支表

| Branch | 定位 | 維護重點 |
|---|---|---|
| `main` | 開發 SSOT（**dev 直接在此**） | 所有通用變更來源。card/SSO 程式碼在此，由旗標切換。本機 dev stack 直接從 `main` 建。 |
| `prod-military-passwd` | 國軍 prod，純帳密 | 程式碼＝`main`。見 §3.3。 |
| `prod-intranet-card` | 中科院內網 prod，SSO + 自然人憑證卡 | 程式碼＝`main`；`.env.example` 姿態差異（`ANILA_ENV=production`、`ANILA_ALLOW_DEV_SECRET=0`、`ANILA_ALLOW_HTTP_ENDPOINT=0`、`ENABLE_CARD_LOGIN=true`、`REQUIRE_CARD_LOGIN_ONLY=true`、`CARD_CRL_REQUIRED=true`、`ENABLE_PUBLIC_SHARE=false`、`ENABLE_MEMORY=false`；`ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1` 供 MLSteam）。 |
| `trial-military` | 國軍 trial / 展示精簡版 | **唯一真正的刪減型分支。** 刪減範圍＝8 個開發者視圖檔（`DeveloperAgentsView.vue`、`DeveloperGuideView.vue` 及其 router/sidebar/header/dashboard 接線）。⚠ 舊敘述「另刪 mindmap／OutputsPage／anila-ops.sh」已作廢——07-23 收斂確認那是功能時間差，該三者已回歸本分支。前端改動會撞 modify/delete，只挑選式 port。 |
| `feature/backend-adapter` | 進行中 feature 分支（落後 main 242） | 繼續開發前先 merge `main`。 |

（2026-07-24 裁撤：`dev-public`、`dev-military`、`prod-public-passwd`、`anila-redesign`、`docs/gate1-handoff`——皆已完整合併或零語意。外網帳密部署如需重啟，以 `main`＋部署 `.env` 姿態即可，毋須分支。）

### 3.3 尚未落實的交付要求 ⚠

舊版本表格宣稱某些分支「已移除」code-server / n8n / GitLab。**實測：七條分支的 `infra/compose/platform.yml` 全部都含這三個服務，且 `codeserver` 沒有 `profiles:`（預設隨 stack 啟動）。**

| 分支 | 交付要求 | 實測（2026-07-10；07-24 註記） |
|---|---|---|
| `prod-military-passwd` | n8n / GitLab 應移除；code-server 由交付規格決定 | **皆仍在** |
| （已裁撤的 `prod-public-passwd`／`dev-military` 原有相同要求；若以 `main`＋`.env` 重啟該類部署，交付前同樣必須落實服務移除。） | | |

**安全影響（CRITICAL）**：`codeserver` 以 read-write 掛載 repo root，遮蔽清單只有 `.env` 與 `infra/nginx/certs/server.key` 兩條，而 `anila-ops.sh` 的備份預設落在同一目錄（含 `pg_dump -U csp` 的 superuser 全庫 dump、`.env` 副本、`secrets/*.pem`），nginx `/codeserver` 無 SSO。詳見 `docs/planning/anila-development-roadmap.md` §3.1 C1 與 Gate 0 S1。

### 3.4 分支操作規則

- 分析分支差異時不要 checkout 擾動工作樹；用 `git show <ref>:path`, `git diff`, `git log`, `git cherry` 直接比較 refs。
- **ahead/behind 與 commit count 會誤導**（見 §3.1 第 2 點）。多條 downstream 有語意相同但 SHA 不同的 commit；同步前同時看檔案差異與 cherry 狀態。
- 同步前至少跑：
  - `git status --short --branch`
  - `git fetch --all --prune`
  - `git diff --name-status origin/main origin/<branch>`
  - `git diff --stat origin/main origin/<branch>`
  - `git log --oneline --no-merges origin/main..origin/<branch>`
  - `git cherry -v origin/main origin/<branch>`
- commit 標籤維持既有規則：`[card-only]`, `[public-only]`, `[military-only]`, `[dev-only]`, `[security-all]`。
- **2026-07-24 精簡後，兩條非 trial downstream 的唯一非文件 tip delta 是 `.env.example`**；這是現況快照，不是永久不變式。port 時不要用 `main` 的版本覆蓋掉分支的旗標姿態；反向同樣成立——姿態更新必**語意重推導**（以 main 現行 `.env.example` 為基底、只覆寫分支蓄意值），禁直接 apply 舊 diff（07-22 廢棄的 sync 分支曾因此丟失 card 的 `ENABLE_PUBLIC_SHARE=false`/`ENABLE_MEMORY=false`）。§3.3 的服務移除落實後，compose/nginx 也會成為正式 delta。
- `trial-military` 是刪減型分支，前端改動會撞 modify/delete，需挑選式 port。

### 3.5 建議同步順序

1. `main` 先完成通用修補與驗證。
2. `prod-military-passwd`。
3. `prod-intranet-card`。
4. `trial-military` 挑選式 port，只帶安全與核心 bugfix。

（2–6 目前程式碼相同，實務上多為確認 `.env.example` 未被覆蓋。若 §3.3 的服務移除要求落實，這些分支才會重新出現真實的 compose delta。）

## 4. Compose / 部署規則

- 主 stack 定義在 `infra/compose/platform.yml`，由 root shim `compose.yaml`（`include:`）帶入，project `anila-platform`；在 repo 根目錄跑 `docker compose` 即可。對外入口只有 nginx：`80`, `443`, `4443`；CSP/Router/model 不應直接開 host port。DB 只綁 loopback `127.0.0.1:5433:5432`。
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
- CSP 正式 image 使用 `infra/docker/csp.Dockerfile`。`services/csp/Dockerfile` 是 dead/legacy，不要改成部署目標。
- prod 部署優先走：
  ```bash
  bash infra/deployment/scripts/deploy-prod.sh preflight
  bash infra/deployment/scripts/deploy-prod.sh deploy
  ```
  不要跳過 preflight、JWT keypair、health 與 endpoint smoke verify。
- nginx 路由重點：
  - `443`: CSP/Vue 管理平台、`/api`, `/v1`, `/v2`, `/anilalm`, `/n8n`, `/gitlab`
  - `4443`: ANILA runtime UI
  - `/api/(studio|reports|mindmaps|infographics|datatables)/`: `anila-studio`
  - `/router/`: Router，會 strip prefix

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
- Data plane `/v1/*` 接受 `sk-*` API key 或 JWT/cookie。Router/agent/service 另有 `csk-*`, `bsk-*`, service clients 與 legacy `CSP_SERVICE_TOKEN` fallback。
- `apps/anila-shell` 已偏 cookie-only；`apps/anilalm/src/store/auth.ts` 仍有 localStorage token + Bearer 注入，屬安全債，改 auth 時需一併收斂。
- prod 不可設 `ANILA_ALLOW_DEV_SECRET=1`，不可使用 `dev-secret-key-change-in-prod`, `dev-service-token`, `changeme`, `sk-internal-worker-changeme` 等 fallback。
- 不得提交 `.env`, `secrets/`, JWT private key, API key, `.pem`, `.key`, 內部憑證私鑰。`services/csp/secrets/.gitignore` 可追蹤，但 key 檔不可追蹤。
- 所有 user-supplied endpoint 維持 SSRF guard。新增模型或 agent docker service name 時，同步 `ANILA_TRUSTED_HOSTS` 或 trusted-hosts UI；不要用全域放寬取代 allow-list。
- nginx CSP header 目前仍含 `'unsafe-inline'` / `'unsafe-eval'`。若要收斂，要先實測 React/Vue/markdown/mermaid 與 Studio artifact 不破。

## 7. 測試與驗證矩陣

不要只看 health 200。SPA catch-all 可能對不存在路由回 `200 text/html`，驗證時要看真實使用路徑、Content-Type、正式 HTTP API 與認證。

| 範圍 | 指令 / 方法 |
|---|---|
| `packages/anila-agent` | `cd packages/anila-agent && make install && make test && make lint`；live endpoint 才跑 `make test-live`。 |
| `packages/anila-core` | repo root 先跑 `pip install -e ./packages/anila-contracts -e ./packages/anila-security -e './packages/anila-core[dev,rag]'`，再 `cd packages/anila-core && pytest`；DB/RLS 類另跑 `pytest -m integration`；品質跑 `ruff check src tests`, `mypy src`。 |
| `services/ingestion-worker` | repo root 跑 `pip install -e ./packages/anila-contracts -e ./packages/anila-security -e './packages/anila-core[rag]' -e './services/ingestion-worker[dev]'`，再 `cd services/ingestion-worker && pytest && ruff check src tests`。 |
| `services/csp` | repo root 跑 `pip install -e ./packages/anila-contracts -e ./packages/anila-security -e './packages/anila-core[rag]' -r services/csp/requirements-dev.txt`，再 `cd services/csp && python -m pytest`；schema/API 改動要驗 Alembic startup。不得用 bare internal distribution name 從 public index 安裝。 |
| `apps/csp-governance-ui` | `cd apps/csp-governance-ui && npm run build`。 |
| `apps/anila-shell` | `cd apps/anila-shell && npm test && npm run build`。目前 `e2e/README.md` 是過時殘留，沒有可靠 Playwright spec。 |
| `apps/anilalm` | `cd apps/anilalm && npm run typecheck && npm run build`；schema 變更後先 `npm run gen:studio-types`。 |
| `services/pptx-renderer` | 沒有 npm scripts；用手動 `node tests/test_*.js` 類測試。注意 `server.js` 直接 `require("jszip")`，但 package 未直接列 `jszip`，目前仰賴 transitive dependency。 |
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

- （`prod-public-passwd` 分支已於 2026-07-24 裁撤。）外網帳密部署若以 `main`＋`.env` 重啟：`n8n` / `gitlab` 仍在 compose 與 nginx 對外，不需要就必須移除 service、nginx location 與 `AUTO_REGISTER_LINKS`。
- `infra/deployment/scripts/phase1-e2e.sh` 仍測 `/codeserver/`，屬過時殘留，不可當 prod 驗證依據。
- `apps/anila-shell` 的 `BASE_PATH` / nginx `/anila/` routing 曾被 README 提到，但 root/dev compose 主要只傳 CSP/Router build args。重建 UI 前先確認資產路徑。
- `services/flux2-dev-agent` volume 目前偏向 `share-dev/uploads/flux`，但 prod deploy 腳本檢查 `share/uploads/flux`。prod 啟用 image-generator 前確認落地路徑與 nginx `/uploads/flux` 一致。
- Router state 預設 `/var/lib/anila-router`；若使用 state-file/bootstrap token，要確認容器 user、volume 與權限，避免寫檔失敗。
- `.doc` parser 可能依賴系統 `antiword`；若 worker image 沒裝會失敗。
- `RotatingServiceTokenMiddleware` / service-token fallback 不可在 production 放行空 token；改動時要明確 fail-closed。

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

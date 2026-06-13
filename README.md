# ANILA 平台 — `prod-public-passwd`(對外網正式部署版)

> **Runtime-first、On-prem 多 Agent 平台。** 三個核心服務、一個落地 LLM，docker compose 一鍵啟動。

> 🌐 **你正在看 `prod-public-passwd` 分支**(對外網 prod，純帳密)。= `main` + 外網 hardening，**移除全部 dev tooling**(code-server / n8n / GitLab)。
>
> | Branch | 部署對象 | 認證 | 定位 |
> |---|---|---|---|
> | `main` | 開發 SSOT（default） | 純帳密 | 所有 feature 的源頭 |
> | `prod-intranet-card` | 中科院內網 prod | SSO + 自然人憑證卡 | main + auth/SSO/card fork |
> | **`prod-public-passwd`** ← *你在這* | 對外網 prod | 純帳密 | main + 外網 hardening |
> | `prod-military-passwd` | 國軍交付 prod | 純帳密 | main + military spec |
> | `dev-public` | 對外網 dev | 純帳密 | main + dev tooling |
> | `dev-military` | 國軍 dev | 純帳密 | dev-public + military 客製 |
> | `trial-military` | 國軍 trial / 展示 | 純帳密 | main 精簡子集 |
>
> **`main` 是 SSOT** — 新 feature 先進 main，再 `git merge origin/main -X theirs` sync 進本分支。本分支的 fork 區僅止於「移除 dev tooling + 外網 hardening」，**不含 SSO/card auth**（那是 `prod-intranet-card` 專屬）。完整 SOP 見 [`docs/branch-sync-backlog.md`](./docs/branch-sync-backlog.md)。

ANILA 是一套企業內部的多 Agent 平台：統一管理模型與 API Key、對外以 OpenAI 相容介面提供推論、讓開發者基於樣板複製出自己的 Agent 並註冊進來、讓終端使用者透過統一 UI 與所有 Agent 對話，並以「主 LLM 未加密 → 遇到加密 agent 整段對話升級為加密」的**單向閂鎖（one-way latch）**處理敏感資料。

平台層內建**使用者記憶**：每輪對話自動萃取個人事實 + embed 訊息片段，下次對話自動帶入，跨 agent 共享。

---

## 本分支定位（`prod-public-passwd`）

| 面向 | 內容 |
|---|---|
| **部署對象** | 對外網（public-facing）正式環境 |
| **認證** | 純帳密（本地登入）；**無** SSO / card auth fork |
| **與 main 差異** | `docker-compose.yml` + `nginx.conf` **移除 code-server / n8n dev tooling 區段**（對外環境不暴露開發工具）；其餘對齊 main |
| **hardening 取向** | 外網威脅模型：CSP 嚴一級、rate-limit / WAF-ready、最小對外攻擊面 |
| **同步** | 從 main sync；本分支 fork 區只有「移除 dev tooling」 |

> 對外網正式環境的核心原則：**對外只開 nginx `:443`，強制 HTTPS，最小化可達服務**。CSP / Router / 模型 stack 全在 docker network 內部，外部打不到。

| 子專案 | 角色 | 預設 Port |
|---|---|---|
| [`myCSPPlatform`](./myCSPPlatform/) | **CSP**（Control & Data Plane）— 使用者 / API Key / 模型 / Agent / 對話 / 附件 / 分享 / 交接 / 審計 / Ingestion / OpenAI 相容代理 / JWKS / token revocation publisher | `:8000`（internal） |
| [`anila-studio`](./anila-studio/) | **Studio service** — Deck 生成（RAG + LLM + FLUX + PPTX）；本地驗 JWT（JWKS）+ Redis pub/sub revocation | `:8100`（internal） |
| [`anila-core`](./anila-core/) | **Runtime foundation（SDK）** — api / registry / engine / tools / providers / storage / **memory** / compact / cli / **security** | — |
| [`anila-core-router`](./anila-core-router/) | **Router** — OpenAI 相容分派器；per-credential service token 走 s2s | `:9000`（internal） |
| [`anila-agent`](./anila-agent/) | **官方 sub-agent 模板**（git subtree；上游 [`zzw09773/anila-agent`](https://github.com/zzw09773/anila-agent)） | `:24786`（獨立執行時） |
| [`ingestion-worker`](./ingestion-worker/) | **Async pipeline worker** — Arq + Redis；parse → chunk → embed → pgvector + Chunking Evaluator | （無 host port） |
| [`ANILA_UI/anila-ui`](./ANILA_UI/anila-ui/) | **Chat Runtime UI** — React，cookie + SSE | nginx 前 |
| [`ANILALM`](./ANILALM/) | **Knowledge-base + Studio SPA**；mount 在 nginx `/anilalm/` | nginx 前 |
| **`nginx`** | 對外閘道；同源 reverse-proxy `/api`、`/v1`、`/router`、`/anilalm/`；6 個安全 header | `:443` / `:4443` |
| **`redis`** | ingestion-worker queue + token-revoke pub/sub | （無 host port） |

> ⛔ 相對 `main`：**不含** `/codeserver`、`/n8n` 路由與對應 compose 服務。

> **唯一規劃文件**：[`anila_plan.md`](./anila_plan.md)。

---

## 整體架構

```mermaid
flowchart TB
    users["🧑‍💻 外網使用者 / OpenAI SDK"]
    nginx["nginx :443<br/>強制 HTTPS · 6 安全 header · 最小對外面"]

    subgraph spas["前端"]
        anila_ui["anila-ui<br/>對話 · 分享 · 交接"]
        anilalm["ANILALM<br/>知識庫 · Studio"]
    end

    subgraph csp["myCSPPlatform (internal only)"]
        csp_ctrl["Control Plane /api/*<br/>cookie / JWT · CSRF"]
        csp_data["Data Plane /v1/*<br/>sk- API Key · chat/completions"]
    end

    studio["anila-studio :8100"]
    router_core["Router :9000"]
    agents["已註冊 Agent"]
    redis[("Redis")]
    worker["ingestion-worker"]
    llm["models stack（anila-models-net）"]
    db[("PostgreSQL + pgvector")]

    users -->|cookie / sk-*| nginx --> anila_ui & anilalm & csp_ctrl & csp_data & router_core
    csp_data -.->|model=anila-router| router_core --> agents --> csp_data
    csp_ctrl --> db
    csp_data --> llm
    csp_ctrl -.->|enqueue| redis --> worker --> db
    studio -.->|FLUX 生圖| llm

    classDef plane fill:#fef3c7,stroke:#d97706
    class csp_ctrl,csp_data plane
```

**核心資料流**：UI POST `/v1/chat/completions`（`model=anila-router`）→ Router 取 agent manifest + 問主 LLM 是否分派 → 必要時轉發 agent → SSE 逐 chunk forward 回 UI；agent `requires_encryption` 時 meta 標 `classified=true`，對話永久閂鎖加密。

---

## 快速開始 / 部署

### 正式部署（建議走部署腳本）

對外網正式環境建議用 [`scripts/deploy-prod.sh`](./scripts/deploy-prod.sh)，它支援三條 prod 分支（含本分支），內含 pre-flight 檢查（branch / docker / 必要 env 非 dev 值 / `anila-models-net` / 模型服務 healthy）避免漏項：

```bash
git checkout prod-public-passwd && git pull origin prod-public-passwd
set -a; source /path/to/public-prod.env; set +a    # secret 不 commit 進 repo
bash scripts/deploy-prod.sh                          # preflight + build + up + 等 healthy + verify
```

子指令：`deploy`（預設）/ `preflight` / `up` / `down`（保留 named volumes）/ `restart` / `rebuild <svc>` / `status` / `logs <svc>` / `verify`。

### 直接 compose（dev / 簡易啟動）

```bash
cp .env.example .env       # 填好真值
docker compose up -d
```

`.env` 至少要填：`CSP_SECRET_KEY`、`CSP_SERVICE_TOKEN`、`INTERNAL_PLATFORM_API_KEY`、`LOCAL_LLM_BASE_URL` / `LOCAL_LLM_MODEL`。把 `LOCAL_LLM_BASE_URL` 指向任何 OpenAI 相容 endpoint（vLLM / Ollama / llama.cpp）；`LOCAL_EMBEDDING_BASE_URL` 未設則同 LLM。

> **正式環境**：`.env` **不要**設 `ANILA_ALLOW_DEV_SECRET=1`。所有 dev 預設值（`SECRET_KEY=dev-secret-key-change-in-prod` / `ADMIN_PASSWORD=changeme` 等）會被 `startup_security` 拒絕，container 直接開不起來。

### 3. 驗證

```bash
curl https://<your-domain>/api/health    # CSP（經 nginx）
curl https://<your-domain>/router/health # Router
```

啟動順序（healthcheck 串接）：`csp-db` → `csp` + `redis` → `ingestion-worker` → `router` → `anila-ui` + `anilalm` + `pptx-renderer` → `nginx`。

---

## 外網 hardening 檢查清單

部署到對外網前逐項確認：

- [ ] **HSTS 已啟用**且憑證已切到**非自簽**（HSTS 啟用前務必確認，否則自簽會把使用者鎖在錯誤憑證）。
- [ ] `ANILA_ALLOW_DEV_SECRET` **未設**；`SECRET_KEY` / `ADMIN_PASSWORD` / `CSP_SERVICE_TOKEN` / DB password / `INTERNAL_PLATFORM_API_KEY` 全為真值。
- [ ] **dev tooling 不存在**：確認 compose 無 `codeserver` / `n8n` 服務、nginx 無 `/codeserver` / `/n8n` 路由（本分支已移除）。
- [ ] CSP / Router **無 host port**，外部只能經 nginx `:443`。
- [ ] 6 個 nginx 安全 header 全到位（HSTS / CSP / Permissions-Policy / Referrer-Policy / X-Frame-Options / X-Content-Type-Options）。
- [ ] 模型 stack（`models/docker-compose.yml`）走 `expose:` 不對 host 開埠，只在 `anila-models-net` 內可達。
- [ ] SSRF guard allow-list（`/trusted-hosts`）只含必要的內部 docker service name。

---

## 維護 anila-agent / models stack

與 `main` 相同：

```bash
git subtree pull --prefix=anila-agent anila-agent main      # 同步 sub-agent 模板
docker network create anila-models-net                       # 模型 stack 第一次
docker compose -f models/docker-compose.yml up -d            # 起模型 stack（獨立 lifecycle）
```

新增模型走純 UI（`/models` → Add Model），遇 SSRF guard 跳對話框時點「add + retry」加進 `trusted_hosts`。

---

## 環境變數速查

| 變數 | 使用者 | 用途 |
|---|---|---|
| `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_MODEL` | CSP / Router | 落地 LLM endpoint 與模型名 |
| `LOCAL_EMBEDDING_BASE_URL` / `LOCAL_EMBEDDING_MODEL` | CSP | Embedding；未設則同 LLM |
| `CSP_SECRET_KEY` | CSP / Worker | JWT 簽署 + credential AES-GCM 主鑰 |
| `CSP_SERVICE_TOKEN` / `CSP_BOOTSTRAP_TOKEN` | CSP / Agent / Router | s2s token + per-credential bootstrap state file（0600） |
| `ANILA_TRUSTED_HOSTS` | CSP / Worker | SSRF guard allow-list bootstrap；之後由 `/trusted-hosts` UI 管 |
| `ANILA_ALLOW_PRIVATE_ENDPOINT` / `ANILA_ALLOW_HTTP_ENDPOINT` | CSP | 設 `1` 放行私網 IP / `http://` |
| `ADMIN_PASSWORD` / `INTERNAL_PLATFORM_API_KEY` | CSP | seed 用；prod 必覆寫 |
| `ANILA_ALLOW_DEV_SECRET` | CSP / Worker | **prod 必拿掉**（dev 才設 `1`） |
| `ANILA_PUBLIC_CSP_BASE_URL` / `ANILA_PUBLIC_ROUTER_BASE_URL` | UI build | 瀏覽器對外 URL |

> 完整範本見 [`.env.example`](./.env.example)。

---

## 安全設計要點

- **On-prem runtime-first**：LLM 流量全進落地 endpoint，無雲端 fallback、無 quota。
- **最小對外攻擊面**：CSP / Router / 模型 stack 無 host port；對外只開 nginx `:443`，強制 HTTPS。**無任何 dev tooling 對外**。
- **Classified 單向閂鎖**：CSP + Router + UI 三層鎖 classified，UI 無降級路徑，持久化到 DB。
- **SPA 認證**：httpOnly cookie + CSRF double-submit；SPA 不持有 API Key。SDK `Bearer` 路徑豁免 CSRF。
- **Credential 加密**：AES-256-GCM + PBKDF2 600k；SSRF guard 把關所有 user-supplied endpoint（loopback / metadata 永不可繞過）。
- **啟動安全檢查**：`startup_security` 在 prod 拒絕 dev 預設值。
- **nginx 6 安全 header** + 上傳 allow-list + zip 1 GB 解壓上限 + 路徑遍歷防護。
- **審計日誌**：所有 admin 操作自動寫 `audit_logs`。

---

## 分支與同步

- 本分支 = `main` + 外網 hardening（移除 dev tooling）。**不含 SSO/card auth**。
- 從 main sync：`git merge origin/main -X theirs`，再確認 dev tooling 區段沒被 merge 回來。
- `[public-only]` 標籤的 commit 只進 `prod-public-passwd` / `dev-public` 兩條。
- 完整 SOP：[`docs/branch-sync-backlog.md`](./docs/branch-sync-backlog.md)。

---

## 授權

見 [`LICENSE`](./LICENSE)。Onyx 原 upstream 程式碼已於 2026-04-27 搬離本 repo。

---

**分支**：`prod-public-passwd`（對外網 prod / 純帳密）· **部署**：`docker compose up -d`（先過外網 hardening 清單）· **分支同步**：[`docs/branch-sync-backlog.md`](./docs/branch-sync-backlog.md)

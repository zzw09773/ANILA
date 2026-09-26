# CONFIG-MAP — ANILA 設定地圖

> 給第一次接手的開發管理員：每一顆設定答得出五問——**怎麼設、設了幹麻、誰何時會改、改了怎麼生效、設錯會怎樣**。
>
> ⚠ 本稿由**唯讀盤點**產生、盤點日 **2026-08-22**（當下 HEAD `5c5ed730`）。**值一律不抄**——只記鍵名與 `file:line`；
> 未讀 `.env` 本體；`[未查]`＝那一格本輪查不到、不是「沒有」，讀者別把它當「已證無」。
>
> 🔴 **不要抄紙面顆數。** 母集合用下面的指令現算。指令輸出變了，以指令為準。
>
> 🔴 **三層模型（全稿反覆用它）**：
> 1. **烘進映像**（Dockerfile `ARG` × compose `build.args:`）——改它要 **rebuild**，`up -d` 不重跑 build。
> 2. **部署參數**（`.env` → compose `environment:`）——改它要 **`up -d`**（`restart` 不重載 `.env`，本平台踩過）。
> 3. **執行期即時**（治理頁 C 類，含三份 Router prompt）——改它下一個請求或 Router 的 prompt TTL 內生效，不需重載、不需 rebuild。

---

## 0. 開頭盤點（跑指令，不抄顆數）

| 母集合 | 產生它的指令 |
|---|---|
| `.env.example` 鍵（compose 部署參數之源） | `/usr/bin/grep -oE '^[A-Za-z_][A-Za-z0-9_]*' .env.example \| sort -u \| wc -l` |
| compose `${VAR}` 去重 | `/usr/bin/grep -rhoE '\$\{[A-Za-z_][A-Za-z0-9_]*' compose.yaml infra/compose/*.yml \| sed 's/\${//' \| sort -u \| wc -l` |
| 烘進映像 `ARG`（全樹 Dockerfile） | `grep -rhoE '^ARG ' apps/*/Dockerfile services/*/Dockerfile \| sort -u` |
| UI 即時 C 類（治理頁，含三份 Router prompt） | `grep -c '^    _spec(' services/csp/app/services/settings_registry.py` |
| 死鍵候選 | §4 的表；全樹命中用 `grep -n '<鍵名>'` 對程式正本 |

compose 引用的名字總數不是「管理員要設的鍵數」：compose 自己有 `:-預設`，也有跨檔、跨 profile 的變數。哪些要設、哪些吃預設，見 §2。

---

## 1. 你要管的三層，白話一次講清

1. **烘進映像的**（改一行 → **要 rebuild 映像**）：§0 的 `ARG` 指令列出的那幾顆，決定「前端 build 時把哪個後端 URL 寫死進 JS bundle、SPA 掛哪個子路徑」。
   改它而不 rebuild，**什麼都不會變**。檔案：`infra/compose/dev.yml` 與 `infra/compose/platform.yml` 的 `args:` 區塊；Dockerfile 端 `apps/anila-shell/Dockerfile`、`apps/anilalm/Dockerfile`。
2. **部署參數的**（改 `.env` → **`docker compose up -d`**）：`.env.example` 裡 §0 第一條指令數到的鍵，大部分屬此。
   `docker restart` 不重載 `.env`（本平台親踩）。改完 `up -d`。
3. **即時生效的**（治理中心「平台設定」頁，顆數用 §0 的 `_spec` 指令）：存在 DB。一般鍵每請求重讀；三份 Router prompt 由 Router 以 TTL 向 CSP 取（預設 30 秒，`ANILA_ROUTER_PROMPTS_TTL`）。§3 詳列。
   文件解析與語音辨識不在那張設定表：治理中心「外部服務」改位址與憑證，下一個擷取工作與下一次頁面聚焦會跟上（約 30 秒快取）。

---

## 2. `.env` 鍵分群（全文見附 1，值不抄；顆數用 §0 指令）

> 每鍵只記「白話用途」與「層級／生效」，`file:line` 要真。按**誰會改它**分群，不按字母序。

### 2a. 安全與信任（越想自己先弄對的）
| 鍵 | 白話用途 | 層級 | 生效 | 來源 |
|---|---|---|---|---|
| `ANILA_AUTH_MODE` | 登入方式開關（本機帳密／卡登） | 部署 | `up -d` | `.env.example` |
| `CARD_INITIAL_OWNERS` | 卡登初批「擁有者」員編；**必須真員編、不可空**（填錯＝沒人能核准，自鎖） | 部署 | `up -d` | `.env.example` |
| `ANILA_ALLOW_DEV_SECRET` | 開不開「開發祕密照收」 | 部署 | `up -d` | `.env.example` |

### 2b. 改錯「大聲死」（拒絕啟動）
`startup_security.py` **實際 grep 到的**（非猜）：`SECRET_KEY`、`CSP_SERVICE_TOKEN`（已從 `.env.example` 拿掉；環境裡還留著舊值才會被檢查）、`INTERNAL_PLATFORM_API_KEY`（worker 不再讀；flux agent 的 `CSP_API_KEY` 仍引用）、`ANILA_ALLOW_DEV_SECRET`。

⚠ 不在上面≠設錯無聲：`CSP_DB_PASSWORD`／`CSP_APP_DB_PASSWORD` 在連 DB 那刻爆；`MODEL_GATEWAY_API_KEY` 在出向呼叫時才用。

### 2c. 其餘部署參數（一句一句，白話）
- `ASR_PROBE_*`／`ASR_PARTIALS_ENABLED`／`ASR_MAX_SESSION_SECONDS`／`ASR_OPENCC_MODE`＝語音 gateway 的探針與切句行為。解碼位址不在 `.env`。
- `EMBEDDING_*`／`LOCAL_EMBEDDING_*`／`LOCAL_LLM_*`／`GEMMA4_BASE_URL`＝第一次啟動時登錄模型用的端點。平台自己用哪一顆（主路由、嵌入、簡報、視覺、摘要、知識庫對話）在治理中心「模型角色」指定，不寫在 `.env`。
- `FLUX_BACKEND_URL`／`FLUX_AGENT_BASE_URL`／`N8N_*`＝簡報／流程編排服務指向。
- `ANILA_ALLOW_{HTTP,GRPC,PRIVATE,HTTP_AGENT}_ENDPOINT`＝「放行哪些非 https 模型端點」的四面。
- `ALLOWED_HOSTS`／`ANILA_HOST`／`ANILA_ENV`／`ANILA_TRUSTED_HOSTS`＝主機／環境／信任主機名。
- 登入／code server（`CARD_*`／`CODESERVER_*`／`ENABLE_IMAGE_CAPTIONS`／`PDF_OCR_FALLBACK`）。文件解析與語音位址在治理中心「外部服務」。

全文逐鍵在附 1。

已從 `.env.example` 拿掉的鍵：`CSP_SERVICE_TOKEN`。Router、anila-studio、ingestion-worker 改讀 CSP 寫的憑證檔。部署後從執行中的 `.env` 刪掉 `CSP_SERVICE_TOKEN=...` 那一行。`INTERNAL_PLATFORM_API_KEY` 還在，只剩 `flux2-dev-agent` 的 `CSP_API_KEY` 在用。

### 附 1：`.env.example` 鍵清單（以 §0 指令為準；下面依檔案行序抄錄，值不抄）
```
ADMIN_PASSWORD
ALLOWED_HOSTS
ANILA_ALLOW_DEV_SECRET
ANILA_ALLOW_GRPC_ENDPOINT
ANILA_ALLOW_HTTP_AGENT_ENDPOINT
ANILA_ALLOW_HTTP_ENDPOINT
ANILA_ALLOW_PRIVATE_ENDPOINT
ANILA_AUTH_MODE
ANILA_ENV
ANILA_HOST
ANILA_QUERY_EXPANSION
ANILA_REMOTE_MODELS
ANILA_TRUSTED_HOSTS
ANILA_ZH_NORMALIZE
ASR_MAX_SESSION_SECONDS
ASR_OPENCC_MODE
ASR_PARTIALS_ENABLED
ASR_PROBE_CONNECT_TIMEOUT_SECONDS
ASR_PROBE_TIMEOUT_SECONDS
CARD_INITIAL_OWNERS
CODESERVER_PASSWORD
CODESERVER_WORKSPACE
CSP_APP_DB_PASSWORD
CSP_DB_PASSWORD
EMBEDDING_BATCH_SIZE
EMBEDDING_TIMEOUT
EMBEDDING_TIMEOUT_SECONDS
ENABLE_IMAGE_CAPTIONS
FLUX_AGENT_BASE_URL
FLUX_BACKEND_URL
GEMMA4_BASE_URL
INTERNAL_PLATFORM_API_KEY
LOCAL_EMBEDDING_BASE_URL
LOCAL_EMBEDDING_MODEL
LOCAL_LLM_BASE_URL
LOCAL_LLM_MODEL
MODEL_GATEWAY_API_KEY
N8N_NODE_FUNCTION_ALLOW_EXTERNAL
N8N_TLS_REJECT_UNAUTHORIZED
PDF_OCR_FALLBACK
SECRET_KEY
```

---

## 3. UI 即時設定（顆數＝§0 的 `_spec` 指令）

> 來源 `services/csp/app/services/settings_registry.py` 的 `SETTINGS`。
> 回退鏈：`platform_settings`（DB）→ `os.environ` → 程式預設；管理員第一次從畫面改＝在 DB 寫一列。
> 一般鍵下個請求生效。三份 Router prompt 由 Router TTL 拉取（見該三列）。

| key | 白話用途 | env 別名（若有） |
|---|---|---|
| `institutional_kb.score_threshold` | 院內規章檢索分數門檻 | —（只住 DB） |
| `memory.retrieve_min_cosine` | 對話摘要搜尋的相似度門檻 | `MEMORY_RETRIEVE_MIN_COSINE` |
| `memory.retrieve_top_k` | 對話摘要搜尋取回筆數 | `MEMORY_RETRIEVE_TOP_K` |
| `memory.enabled` | 長期記憶總開關。關閉後不注入、不萃取、不搜尋 | `MEMORY_ENABLED` |
| `memory.idle_minutes` | 對話閒置幾分鐘後才萃取摘要與事實 | `MEMORY_IDLE_MINUTES` |
| `proxy.llm_timeout` | LLM 逾時（秒） | `LLM_TIMEOUT` |
| `proxy.embedding_timeout` | 嵌入逾時（秒） | `EMBEDDING_TIMEOUT` |
| `auth.access_token_expire_minutes` | access 有效分鐘 | `ACCESS_TOKEN_EXPIRE_MINUTES` |
| `auth.refresh_token_expire_days` | refresh 有效天數 | `REFRESH_TOKEN_EXPIRE_DAYS` |
| `limits.department_max_depth` | 部門樹上限層數 | `ANILA_DEPARTMENT_MAX_DEPTH` |
| `limits.action_invoke_per_min` | 每分每使用者自訂動作上限 | `ANILA_ACTION_INVOKE_PER_MIN` |
| `limits.attachment_budget_ratio` | 附件可佔 context 比例 | `ANILA_ATTACHMENT_BUDGET_RATIO` |
| `intl.zh_normalize` | 簡體→繁體正規化 | `ANILA_ZH_NORMALIZE` |
| `intl.query_expansion` | 檢索前同義詞擴展 | `ANILA_QUERY_EXPANSION` |
| `router.prompt.system` | Router 派工模板（有已註冊 agent 時）。必須留 `{agent_list}` | —（只住 DB；出貨全文在 `packages/anila-core/src/anila_core/api/router_prompts.py`） |
| `router.prompt.plain` | Router 直答模板（沒有 agent 時） | —（同上） |
| `router.prompt.forced` | Router 強制自答模板（依院內規章自己答） | —（同上） |

改錯值：寫入正規化把關，收不進 → **400 明說**，不靜默反轉意圖。

---

## 4. 有旋鈕沒消費者（死鍵候選）

> 判法：`.env.example` 有指定行、但全樹沒有非 env/compose/doc 的檔讀它。**列進這節≠該刪**——
> n8n/flux 整包未上線前留著的佔位都在這，標而不砍（擁有者 2026-08-22 裁定「真的有必要再列，沒有就不要」）。

| 鍵 | 全樹命中（消費層） | 判 |
|---|---|---|
| `FLUX_AGENT_BASE_URL` | 0 程式正本 | 死鍵候選 |
| `N8N_NODE_FUNCTION_ALLOW_BUILTIN` | 0 | 死鍵候選 |
| `N8N_NODE_FUNCTION_ALLOW_EXTERNAL` | 0 | 死鍵候選 |
| `N8N_TLS_REJECT_UNAUTHORIZED` | 0 | 死鍵候選 |
| `N8N_WEBHOOK_URL` | 未進 `.env.example`、compose 有引用 | 半死（見 open 問題） |

---

## 5. 有消費者沒旋鈕

> **不補列進 `.env.example`**（擁有者 2026-08-22 裁定：「真的有必要再列，沒有就不要」）——查詢入口就是本表。

code 真的在讀（`os.getenv`）、`.env.example` 卻完全沒列。全文在附 2。改法各異、改錯多是本平台「不知道去哪改」的核心。

### 附 2：code 有讀、`.env.example` 沒列的鍵（以 `comm -23` 現算為準；下面是抄錄）
```
ANILA_AGENT_STATE_DIR
ANILA_CA_FILE
ANILA_HTTP_CONNECT_TIMEOUT
ANILA_HTTP_MAX_CONNECTIONS
ANILA_HTTP_MAX_KEEPALIVE
ANILA_HTTP_POOL_TIMEOUT
ANILA_HTTP_READ_TIMEOUT
ANILA_JWT_OWNER_CACHE_TTL
ANILA_ROUTER_MODEL_TTL
CARD_CA_BUNDLE_PATH
CARD_DEV_SKIP_NONCE_BINDING
CARD_DEV_TRUST_TEST_CA
CSP_API_KEY
CSP_BASE_URL
DATABASE_URL
DEFAULT_ASPECT_RATIO
DEFAULT_BASE_URL_ENV
DEFAULT_STATE_DIR
DOCLING_OCR_LANGS
DOCLING_SERVICE_TOKEN
DOCLING_URL
ENABLE_PROMPT_TRANSLATION
ENV_CA_FILE
FLUX_API_KEY
FLUX_MAX_CONCURRENT
FLUX_MODEL
FLUX_TIMEOUT_SECONDS
GEMMA_MODEL
HF_HUB_OFFLINE
MIGRATION_DATABASE_URL
PDF_OCR_CONCURRENCY
PUBLIC_URL_PREFIX
REDIS_URL
RENDERER_BASE_URL
SHARE_DIR
TRANSFORMERS_OFFLINE
UPLOAD_DIR
VISION_API_KEY
VISION_URL
```

平台自己用的模型名（主路由、嵌入、簡報、視覺、摘要、知識庫對話）不在這張表：治理中心「模型角色」指定，服務執行期問 CSP。`VISION_MODEL`、`MEMORY_LLM_MODEL`、`LLM_MODEL`、`ANILALM_DEFAULT_CHAT_MODEL`、`ANILA_STUDIO_SLIDES_MODEL`、`ANILA_STUDIO_VISION_MODEL`、`VITE_DEFAULT_CHAT_MODEL` 都不再讀。

---

## 6. Router 三份 system prompt（已是即時設定）

三份都在 §3：`router.prompt.system`、`router.prompt.plain`、`router.prompt.forced`。
出貨全文在 `packages/anila-core/src/anila_core/api/router_prompts.py`（`DEFAULT_ROUTER_SYSTEM`／`DEFAULT_PLAIN_ASSISTANT`／`DEFAULT_FORCED_ANSWER`）。
治理中心寫入 `platform_settings` 後，Router 用 `refresh_router_prompts` 在 TTL 內取回（`packages/anila-core/src/anila_core/api/router_server.py`，預設 30 秒）。CSP 讀不到時用出貨全文。改 prompt 不需要 rebuild。

---

## 7. 改錯會怎麼死 vs 無聲（只寫查證過）

| 格 | 判 |
|---|---|
| 烘進映像的 `ARG`（§0） | 不 rebuild＝**全部無效（無訊號）** |
| 部署參數（`.env.example`，§0） | 改 `.env` 不 `up -d`＝續用舊值（無訊號）；`up -d` 後多數「大聲」（連線／URL 錯 → 健康檢失敗、500） |
| UI 即時設定（§3，含三份 Router prompt） | 改錯值＝**400 明講**；壞值經 API 寫透 → 下請求回退 env→預設並留 warning log。Router prompt 在 TTL 內生效，不 rebuild |

---

## 8. Open questions（真查不到，不寫傾向）

1. `ASR_GPU`／`FLUX_AGENT_BASE_URL`／n8n 五鍵是否「整包未上線前留的佔位」——需看現行 compose profile 是否還掛那幾包。
   本輪停在「沒有程式正本」就標死鍵候選，未再追「是不是 profile 未啟用」。
2. `DOCKER_GID`／`GID`／`UID` 等 compose 引但不在 `.env.example` 的鍵——多數是 compose 自己 `:-預設` 吃掉，本輪未逐顆分「引了但 player 不看」vs「player 靠它活」。

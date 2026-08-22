# CONFIG-MAP — ANILA 設定地圖

> 給第一次接手的開發管理員：每一顆設定答得出五問——**怎麼設、設了幹麻、誰何時會改、改了怎麼生效、設錯會怎樣**。
>
> ⚠ 本稿由**唯讀盤點**產生、盤點日 **2026-08-22**（當下 HEAD `5c5ed730`）。**值一律不抄**——只記鍵名與 `file:line`；
> 未讀 `.env` 本體；`[未查]`＝那一格本輪查不到、不是「沒有」，讀者別把它當「已證無」。
>
> 🔴 **數字是快照，不是承諾。** 本檔每個數字都是以**盤點日**為準的一次量測，判準是「各節寫的產生指令」——
> 數字漂了就用該指令重跑，不要相信紙面數。任何一處「目前共 N 顆」都**不成立**，別照抄。
>
> 🔴 **三層模型（全稿反覆用它）**：
> 1. **烘進映像**（Dockerfile `ARG` × compose `build.args:`）——改它要 **rebuild**，`up -d` 不重跑 build。
> 2. **部署參數**（`.env` → compose `environment:`）——改它要 **`up -d`**（`restart` 不重載 `.env`，本平台踩過）。
> 3. **執行期即時**（治理頁 12 顆 C 類）——改它**下一個請求**就生效，不需重載。

---

## 0. 開頭總數（快照，判準＝右欄產生指令）

| 母集合 | 數 | 產生它的指令 |
|---|---|---|
| `.env.example` 鍵（compose 部署參數之源） | **55** | `/usr/bin/grep -oE '^[A-Za-z_][A-Za-z0-9_]*' .env.example \| sort -u \| wc -l` |
| compose `${VAR}` 去重（6 份 compose） | **131** | `/usr/bin/grep -rhoE '\$\{[A-Za-z_][A-Za-z0-9_]*' compose.yaml infra/compose/*.yml \| sed 's/\${//' \| sort -u \| wc -l` |
| 烘進映像 `ARG`（全樹 Dockerfile） | **4** | `grep -rhoE '^ARG ' apps/*/Dockerfile services/*/Dockerfile \| sort -u` |
| UI 即時 C 類（治理頁） | **12** | `services/csp/app/services/settings_registry.py:132` 的 `SETTINGS: tuple` |
| 有消費者、無旋鈕 | **44** | `comm -23 <(code 讀取集) <(.env 鍵集)` |
| 死鍵候選 | **5**（＋1 半死） | §4 逐個給全樹命中 |

⚠ **131 ≠ 55＋4**：差來自 (a) compose 自己 `:-預設` 的鍵（不寫吃預設、寫了能覆寫）；(b) Inheritance 跨檔、跨 profile 的變數。
**131 是「compose 裡被引用的名字總數」，不是「管理員要設的鍵數」**——哪些要設、哪些吃預設，見 §2。

---

## 1. 你要管的三層，白話一次講清

1. **烘進映像的**（改一行 → **要 rebuild 映像**）：4 顆決定「前端 build 時把哪個後端 URL 寫死進 JS bundle、SPA 掛哪個子路徑」。
   改它而不 rebuild，**什麼都不會變**。檔案：`infra/compose/dev.yml` 與 `infra/compose/platform.yml` 的 `args:` 區塊；Dockerfile 端 `apps/anila-shell/Dockerfile`、`apps/anilalm/Dockerfile`。
2. **部署參數的**（改 `.env` → **`docker compose up -d`**）：`.env.example` 那 55 顆大部分屬此。
   `docker restart` 不重載 `.env`（本平台親踩）。改完 `up -d`。
3. **即時生效的**（治理中心「平台設定」頁 12 顆）：存在 DB、每請求重讀、不快取。§3 詳列。

---

## 2. `.env` 55 顆分群（全文完整，值不抄）

> 每鍵只記「白話用途」與「層級／生效」，`file:line` 要真。按**誰會改它**分群，不按字母序。

### 2a. 安全與信任 3 顆（越想自己先弄對的）
| 鍵 | 白話用途 | 層級 | 生效 | 來源 |
|---|---|---|---|---|
| `ANILA_AUTH_MODE` | 登入方式開關（本機帳密／卡登） | 部署 | `up -d` | `.env.example` |
| `CARD_INITIAL_OWNERS` | 卡登初批「擁有者」員編；**必須真員編、不可空**（填錯＝沒人能核准，自鎖） | 部署 | `up -d` | `.env.example` |
| `ANILA_ALLOW_DEV_SECRET` | 開不開「開發祕密照收」 | 部署 | `up -d` | `.env.example` |

### 2b. 改錯「大聲死」（拒絕啟動）的 4 顆
`startup_security.py` **實際 grep 到的**（非猜）：`SECRET_KEY`、`CSP_SERVICE_TOKEN`、`INTERNAL_PLATFORM_API_KEY`、`ANILA_ALLOW_DEV_SECRET`。

⚠ 不在上面≠設錯無聲：`CSP_DB_PASSWORD`／`CSP_APP_DB_PASSWORD` 在連 DB 那刻爆；`MODEL_GATEWAY_API_KEY` 在出向呼叫時才用。

### 2c. 其餘部署參數（一句一句，白話）
- `ASR_*`＝語音辨識服務的指向 URL／token／模型大小／逾時／暫存。
- `EMBEDDING_*`／`LOCAL_EMBEDDING_*`／`LOCAL_LLM_*`／`MEMORY_LLM_MODEL`／`GEMMA4_BASE_URL`／`LLM_MODEL`／`ANILALM_DEFAULT_CHAT_MODEL`＝各模型端點與預設模型名。
- `FLUX_BACKEND_URL`／`FLUX_AGENT_BASE_URL`／`N8N_*`＝簡報／流程編排服務指向。
- `ANILA_ALLOW_{HTTP,GRPC,PRIVATE,HTTP_AGENT}_ENDPOINT`＝「放行哪些非 https 模型端點」的四面。
- `ALLOWED_HOSTS`／`ANILA_HOST`／`ANILA_ENV`／`ANILA_TRUSTED_HOSTS`＝主機／環境／信任主機名。
- 登入／code server／文件解析開關（`CARD_*`／`CODESERVER_*`／`DOC_PARSER`／`ENABLE_IMAGE_CAPTIONS`／`PDF_OCR_FALLBACK`）。

全文逐鍵在附 1。

### 附 1：`.env.example` 55 鍵清單（依 `.env.example` 行序；值不抄）
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
ANILALM_DEFAULT_CHAT_MODEL
ANILA_QUERY_EXPANSION
ANILA_REMOTE_MODELS
ANILA_TRUSTED_HOSTS
ANILA_ZH_NORMALIZE
ASR_COMPUTE_TYPE
ASR_DECODE_API_KEY
ASR_DECODE_PROTOCOL
ASR_DECODER_TOKEN
ASR_DECODE_URL
ASR_DECODE_URL_TTL
ASR_GPU
ASR_MAX_SESSION_SECONDS
ASR_MODEL_SIZE
ASR_OPENAI_MODEL
ASR_OPENCC_MODE
ASR_PARTIALS_ENABLED
ASR_PROBE_CONNECT_TIMEOUT_SECONDS
ASR_PROBE_TIMEOUT_SECONDS
CARD_INITIAL_OWNERS
CODESERVER_PASSWORD
CODESERVER_WORKSPACE
CSP_APP_DB_PASSWORD
CSP_DB_PASSWORD
CSP_SERVICE_TOKEN
DOC_PARSER
EMBEDDING_BATCH_SIZE
EMBEDDING_TIMEOUT
EMBEDDING_TIMEOUT_SECONDS
ENABLE_IMAGE_CAPTIONS
FLUX_AGENT_BASE_URL
FLUX_BACKEND_URL
GEMMA4_BASE_URL
INTERNAL_PLATFORM_API_KEY
LLM_MODEL
LOCAL_EMBEDDING_BASE_URL
LOCAL_EMBEDDING_MODEL
LOCAL_LLM_BASE_URL
LOCAL_LLM_MODEL
MEMORY_LLM_MODEL
MODEL_GATEWAY_API_KEY
N8N_NODE_FUNCTION_ALLOW_EXTERNAL
N8N_TLS_REJECT_UNAUTHORIZED
PDF_OCR_FALLBACK
SECRET_KEY
```

---

## 3. UI 即時 12 顆

> 來源 `services/csp/app/services/settings_registry.py:132` `SETTINGS: tuple[SettingSpec, ...]`。
> 回退鏈：`platform_settings`（DB）→ `os.environ` → 程式預設；管理員第一次從畫面改＝在 DB 寫一列，下個請求生效。

| key | 白話用途 | env 別名（若有） |
|---|---|---|
| `institutional_kb.score_threshold` | 院內規章檢索分數門檻 | —（只住 DB） |
| `memory.retrieve_min_cosine` | 記憶相似度門檻 | `MEMORY_RETRIEVE_MIN_COSINE` |
| `memory.retrieve_top_k` | 記憶取回筆數 | `MEMORY_RETRIEVE_TOP_K` |
| `proxy.llm_timeout` | LLM 逾時（秒） | `LLM_TIMEOUT` |
| `proxy.embedding_timeout` | 嵌入逾時（秒） | `EMBEDDING_TIMEOUT` |
| `auth.access_token_expire_minutes` | access 有效分鐘 | `ACCESS_TOKEN_EXPIRE_MINUTES` |
| `auth.refresh_token_expire_days` | refresh 有效天數 | `REFRESH_TOKEN_EXPIRE_DAYS` |
| `limits.department_max_depth` | 部門樹上限層數 | `ANILA_DEPARTMENT_MAX_DEPTH` |
| `limits.action_invoke_per_min` | 每分每使用者自訂動作上限 | `ANILA_ACTION_INVOKE_PER_MIN` |
| `limits.attachment_budget_ratio` | 附件可佔 context 比例 | `ANILA_ATTACHMENT_BUDGET_RATIO` |
| `intl.zh_normalize` | 簡體→繁體正規化 | `ANILA_ZH_NORMALIZE` |
| `intl.query_expansion` | 檢索前同義詞擴展 | `ANILA_QUERY_EXPANSION` |

改錯值：寫入正規化把關，收不進 → **400 明說**，不靜默反轉意圖。

---

## 4. 有旋鈕沒消費者（死鍵候選）

> 判法：`.env.example` 有指定行、但全樹沒有非 env/compose/doc 的檔讀它。**列進這節≠該刪**——
> n8n/flux 整包未上線前留著的佔位都在這，標而不砍（擁有者 2026-08-22 裁定「真的有必要再列，沒有就不要」）。

| 鍵 | 全樹命中（消費層） | 判 |
|---|---|---|
| `ASR_GPU` | 僅 standalone yml，無 Py 正本 | 死鍵候選 |
| `FLUX_AGENT_BASE_URL` | 0 程式正本 | 死鍵候選 |
| `N8N_NODE_FUNCTION_ALLOW_BUILTIN` | 0 | 死鍵候選 |
| `N8N_NODE_FUNCTION_ALLOW_EXTERNAL` | 0 | 死鍵候選 |
| `N8N_TLS_REJECT_UNAUTHORIZED` | 0 | 死鍵候選 |
| `N8N_WEBHOOK_URL` | 未進 .env 55、compose 有引用 | 半死（見 open 問題） |

---

## 5. 有消費者沒旋鈕（44 顆）

> **不補列進 `.env.example`**（擁有者 2026-08-22 裁定：「真的有必要再列，沒有就不要」）——查詢入口就是本表。

code 真的在讀（`os.getenv`）、`.env.example` 卻完全沒列。全文在附 2。改法各異、改錯多是本平台「不知道去哪改」的核心。

### 附 2：44 顆全文（`comm -23` 產生，非猜名）
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
ANILA_STUDIO_SLIDES_MODEL
ANILA_STUDIO_VISION_MODEL
ANILA_TRACE_ENDPOINT
ANILA_TRACE_TOKEN
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
VISION_MODEL
VISION_URL
```

---

## 6. 無旋鈕的「寫死的」系統行——Router 三份 system prompt

| 消費者 | 位置 |
|---|---|
| `_ROUTER_SYSTEM_TEMPLATE`（Router 派工主模板） | `packages/anila-core/src/anila_core/api/router_server.py:126-181` |
| `_PLAIN_ASSISTANT_TEMPLATE`（無 agent 直答） | 同檔 `:192-209` |
| `_FORCED_ANSWER_TEMPLATE`（「依院內規章重查」強制答） | 同檔 `:226-239` |

⚖ **現況（以現況為準，勿把將來式寫成現在式）**：這三份「怎麼答、何時派工、用什麼口氣」的模板**目前仍寫死在 Python 檔**，
改了要 **rebuild** 才生效。**擁有者 2026-08-22 已裁定「進 UI 設定頁」（治理中心可改），但尚未施工**
——排隊單 `queued-fix-router-prompt-ui-knob.md`。**施工完成前，本節描述的現況 = 有效描述。**

同族需改動：`COMMON_PREAMBLE`、`IDENTITY`（`prompts/__init__.py` ＋ `router_server.py:42`）。

---

## 7. 改錯會怎麼死 vs 無聲（只寫查證過）

| 格 | 判 |
|---|---|
| 烘進映像 4 顆 | 不 rebuild＝**全部無效（無訊號）** |
| 部署參數 55 顆 | 改 `.env` 不 `up -d`＝續用舊值（無訊號）；`up -d` 後多數「大聲」（連線／URL 錯 → 健康檢失敗、500） |
| UI 12 顆 | 改錯值＝**400 明講**；壞值經 API 寫透 → 下請求回退 env→預設並留 warning log |

---

## 8. Open questions（真查不到，不寫傾向）

1. `ASR_GPU`／`FLUX_AGENT_BASE_URL`／n8n 五鍵是否「整包未上線前留的佔位」——需看現行 compose profile 是否還掛那幾包。
   本輪停在「沒有程式正本」就標死鍵候選，未再追「是不是 profile 未啟用」。
2. `DOCKER_GID`／`GID`／`UID` 等 compose 引但不在 `.env.example` 的鍵——多數是 compose 自己 `:-預設` 吃掉，本輪未逐顆分「引了但 player 不看」vs「player 靠它活」。

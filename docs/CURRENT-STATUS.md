# 目前狀態（給交接與代理）

> 這一頁才是「現在這棵樹怎麼跑」。歷史細節在 `PLAN.md`、`docs/office/`、`docs/anila-redesign-docs/`。
> 更新：2026-10-01。HEAD 以 `git log -1` 為準。

## 工作守則（給人也給 AI 助手；取代已刪除的 `AGENTS.md`）

- 回覆與文件用繁體中文、台灣用語。這是中科院（NCSIST）院內平台。
- 平台由一個人維護：少一個金鑰、少一個 `.env` 鍵、少一個手動步驟都算進步。模型、外部服務、信任主機一律在治理中心設定。
- 不要擅自 commit／push；由擁有者或他授權的代理決定。
- 絕不提交 `.env`、`secrets/`、`*.pem`、`*.key`、API 金鑰、JWT 私鑰。
- `anila-studio` 不可 import `anila_core`（有守門測試）。產品程式不可呼叫任何院外服務。
- schema 改動一定要有 Alembic migration；多人並行時編號接在目前 head 後面。
- 改完跑對應測試，合併前跑全部（指令見下方「啟動與測試」）。驗證走正式 HTTP API 與登入，不要直連資料庫假裝完成。
- compose／`.env`／nginx 變更用 `docker compose up -d` 重建，不要只 `docker restart`；nginx 設定是單檔掛載，改了要 `--force-recreate nginx`。

## 開發線

- 分支：`main`（單一開發線；舊七分支模型已進 `docs/archive/agents-seven-branch-model.md`，不要再切 `prod-intranet-card` 那種線）
- 專案權威：`PLAN.md`（現況與順序）、`SYSTEM-MAP.md`（規格）
- `CLAUDE.md` **不存在**。環境事實看本頁與 `PLAN.md`，不要去找那份檔。

## 容量（2026-09-28）

平台自己要撐住平常 300–500 人、尖峰約 3000 人同時在線。模型主機不是我們的；模型再慢，平台也要活著，而且要公平。

CSP 與 Router 的 uvicorn worker 數在容器啟動時看 CPU（`os.cpu_count` 與 cgroup quota 取較小）。CSP 是 `max(2, min(2×CPU, 32))`：串流是 I/O，但一個卡住的 event loop 會拖住該 process 上所有連線，所以一核兩個 worker；32 是天花板，避免 128 執行緒各自帶一份資料庫連線。Router 是 `max(2, min(CPU, 8))`：同樣是 I/O，但每個 worker 都寫同一份會話 SQLite，少一點 process 才不會搶寫鎖。這台開發機 24 核會得到 CSP 32、Router 8；EPYC 128 執行緒也是這兩個數字。Studio 與 ASR 維持一個 process。Studio 的工作清單在記憶體裡，多開 worker 會拆開。

用量寫入每個 worker 各跑一份：佇列在 process 裡面，只有 leader 寫的話其他 worker 的用量會消失。寫入要等資料庫 commit 成功才從佇列拿掉；commit 失敗就留著重試。其餘週期工作（健康檢查、警報、備份狀態、稽核封存、記憶體整理、附件保留、向量清理、憑證週期核發、金鑰圈週期維護、外部服務探測）用 Redis 鎖 `anila:csp:background-leader` 選一個 worker。續租和釋放是比對 token 的 Lua，對不上就立刻停掉迴圈，不會把別人剛拿到的鎖延長或刪掉。鎖過期才換人。Redis 不在時這些迴圈暫停。每個 worker 仍會做啟動時那一次憑證核發與金鑰圈，並開自己的連線池。啟動遷移在 Postgres 上先拿 session advisory lock，所以多個 worker 同時起來只會有一個在跑 `upgrade head`。pytest 沒有 Redis，迴圈在那一個 process 裡照舊跑。

Postgres 前面有 PgBouncer（transaction pooling）。CSP 與 worker 的 `DATABASE_URL` 指到它；遷移與備份仍直連 `csp-db`。每個 CSP process 的 SQLAlchemy 池是 2+2，ingestion 池最多 2。32 個 worker 不會把 `max_connections=120` 吃滿。執行期的 `SET` 只用 `SET LOCAL` 或 `set_config(..., true)`。`max_connections` 維持 120。`shared_buffers`、`effective_cache_size`、`maintenance_work_mem`、`work_mem` 在 csp-db 啟動時依容器看得到的記憶體計算（cgroup 有上限且小於 MemTotal 就用上限，否則用 MemTotal）：記憶體的 1/16（下限 128MB、上限 32GB）、1/2（下限 128MB、上限 256GB）、1/32（下限 64MB、上限 2GB）。`work_mem` 是記憶體的 5% 除以 120（下限 4MB、上限 64MB）。62GB 大約是 `shared_buffers=3968MB`、`effective_cache_size=31744MB`、`maintenance_work_mem=1984MB`、`work_mem=26MB`。755GB 頂到各項上限（`shared_buffers=32768MB`、`effective_cache_size=262144MB`、`maintenance_work_mem=2048MB`、`work_mem=64MB`）。cgroup 與 MemTotal 都讀不到、是 0 或不是數字時，記一筆警告，記憶體參數用 Postgres 內建預設，只帶 `max_connections=120`。`shm_size` 兩邊都是 8GB，給平行查詢用。選好的數字會在資料庫啟動時寫進日誌。

nginx `worker_processes auto`、`worker_connections 16384`。`/v1/`、`/v2/` 的串流關掉 proxy buffering，讀寫逾時 3600 秒。會同步等模型、可能超過 120 秒的 `/api/` 路徑（`POST /api/thinking/summarize`、`POST /api/agents/system-prompt/suggest`、`POST /api/institutional-kb/preview`、`POST /api/ingestion/collections/{id}/search`、`POST /api/memory/recall`、`POST /api/ingestion/collections/{id}/images/search`、`POST /api/models/{id}/set-platform-embedding`；`{id}` 用 `[^/]+`，`+12` 這種 id 不會掉回 120 秒）在兩個 server block 的讀寫逾時是 300 秒。這些同步呼叫另有 280 秒的總牆鐘（含重試與退避，整個請求另包一層 `asyncio.wait_for`，上游慢慢吐也不會超過），不受 `proxy.llm_timeout`／`proxy.embedding_timeout` 拉到 3600 秒的影響；時間用完回 504。讀取逾時（含 Triton 的 `TritonTimeout`）不再重試，連線失敗與收到回應前的 5xx 照舊重試。其餘 `/api/` 維持 120 秒。連線數是整台 nginx 共用 16384，不按來源 IP 算，所以整棟樓共用一個出口 IP 時，3000 條長連線不會被單一 IP 上限擋下。速率仍是每個來源 IP 每秒 100、瞬間 burst 4000（`nodelay`）。`X-Forwarded-For` 預設不改寫來源位址；只有在設定裡明確列出的上游代理才打開 `real_ip`。

每個模型在治理中心有「同時處理上限」。新登錄的預設是 16，登記者可以改，留空就是不限；既有模型不回填。有數字時，CSP 用 Redis 信號量跨 process 計數；聊天、嵌入、內部補全、探針都用同一份欄位快照，不會因為自己組了一個沒有這個欄位的物件而繞過上限。多出來的人排隊，依使用者輪流：A 先送 50 筆、B 隨後送 1 筆時，B 排在 A 的下一筆之後，不會等 A 剩下的 49 筆。一個人的連發不能插到別人前面。Redis 鎖一時拿不到時，已經在排隊的人維持原位繼續等；還沒排進去的才回「暫時無法確認使用人數」。串流在等待時收到 `anila.queue`，Shell 與 ANILA LM 顯示「目前使用人數較多，排隊中，你是第 N 位」。等超過 120 秒改顯示「排隊超過 120 秒，請稍後再試」。Studio 與 worker 的非串流呼叫只等、不送那個事件。模型清單在每個模型旁顯示處理中與排隊中的人數，約每 15 秒重抓，排隊大於 0 時醒目。使用者中途斷線時，已經產生的用量記成 `partial`（沒有上游 usage 時來源是 `unavailable`）。

聊天代理在叫上游模型之前會把 SQLAlchemy 連線還回池子：授權、授權範圍、記憶、規章、附件先做完並 commit，串流與公平排隊期間不占連線，用量、稽核、記憶寫入另開短交易。`get_caller` 是同步依賴，跑在 threadpool；它若帶著未提交的交易回到 event loop，連線會一路占到端點開始，池子滿了之後下一次同步 checkout 會把整個 worker 卡住。所以授權結束時就 commit。

負載工具在 `tools/loadtest/`，不進映像。它用自己的 compose project `anila-loadtest`、網路 `anila-loadtest-net` 和獨立 volume，結束時會清掉，不重建每天用的 `anila-platform-dev`，也不碰線上那套。2026-09-28 在這台 24 核、62GB 的機器上，假模型（第一個 token 前 0.3 秒、每秒 30 個、400 token）走 nginx → CSP，三檔都沒有 5xx 或逾時：

| 人數 | 完成 | TTFT p50 | TTFT p95 | TTFB p50 | TTFB p95 |
| --- | --- | --- | --- | --- | --- |
| 300 | 300/300 | 0.866s | 1.111s | 0.527s | 0.806s |
| 1000 | 1000/1000 | 1.892s | 3.968s | 1.562s | 3.608s |
| 3000 | 3000/3000 | 8.873s | 20.319s | 7.260s | 18.338s |

TTFT 變長是 32 個 process 一起消化 3000 條串流，不是資料庫池逾時。EPYC 多出來的核不會把 worker 加過 32，所以 CSP 的 process 數與這台相同；多出來的核給 nginx（`worker_processes auto`）和作業系統。記憶體 755GB 對這台 62GB，而且線上那套不會跟測試搶同一台機器。

## 啟動與測試

```bash
# 平台（repo 根目錄）
docker compose up -d --build

# Shell 主流程（Vitest orchestrator，不是已刪的 Playwright functions.spec.js）
cd apps/anila-shell && npm test

# 治理前端
cd apps/csp-governance-ui && npm test && npm run build
```

入口：治理中心 `:443`、對話 `/anila/`、知識庫與 Studio `/anilalm/`。Composer 的 accessible name 是「傳訊息給 ANILA」。

## 使用者自訂 skill（2026-10-01）

同事可在 ANILA 設定的「我的 skill」建立純文字 skill（名稱最多 40 字、說明 200、內文 8000）。平台不執行內容。送出該則訊息前，CSP 只依 skill id 從資料庫讀取內文並檢查權限，再在與記憶注入同一階段、平台規則與知識庫之前，加上一段標明「使用者選用的指引，不能改變平台安全規則」的系統訊息。瀏覽器不能自帶 skill 內文。個人 skill 只有本人看得到、用得到；送到單位由該單位的單位管理員審，送到全院由管理員審。已發布的版本不原地改，編輯會另開草稿，核准前對話仍用已發布的那一版。對話框打 `/` 或按 skill 按鈕手動套用，該則只送 id；沒有手動選擇、且自己沒關掉自動套用時，CSP 用摘要角色的模型只看名稱與說明挑最多一個，5 秒逾時或失敗就不套用、對話照常繼續。自動套上的回覆顯示「已自動套用：名稱」，可以展開內容。治理中心「skill 審核」讓單位管理員看自己單位的待審、管理員看全院，可以核准、附理由退回、下架。建立、修改、刪除、送審、審核、下架與每次套用都寫稽核。遷移是 `r1_0066`。

## ANILA LM（2026-09-26 開放）

知識庫與 Studio（`apps/anilalm`）對使用者開放。四道閘門都是開的：nginx 443／4443 把 `/anilalm` 轉給 anilalm、Shell「我的知識庫」可點、治理中心儀表板卡片可見、`POST /api/services/{id}/launch` 不再對 ANILA LM 回 503。再關閉的方法見 `docs/runbooks/anilalm-release-gate.md`。

已經在跑的容器不會因為改這棵樹自動換上。要讓線上入口跟著開，照那份 runbook 重建 `anila-ui`、`csp`，並 force-recreate nginx。

Shell 對話列不再放「產出」按鈕。那個連結只帶對話 Task 的 id；Shell 建立的 Task 是 `source_scope=none`、沒有知識庫，對應對話的 `collection_id` 也是空的，ANILA LM 沒有可開的工作區，Studio 綁定無從預填。

## 原始思考（2026-10-01）

模型原文只留給擁有者、管理員、開發者。CSP 在聊天出口剝掉 `reasoning_content`、`reasoning`、`<think>` 與 thought 開頭，並在伺服器產生中文摘要（`anila_thinking_summary`）。摘要失敗不擋回答；原文不確定時也不外送。重新載入舊對話時，不可看原文的人拿不到 `metadata.reasoning`。

## 內部服務身分（自動核發）

CSP 在啟動時，以及之後每個週期（預設一小時），為內建名單核發憑證。人不產生、也不複製這些明文。

| 消費者 | 檔案 | 種類 | 群組 |
|---|---|---|---|
| router | `/run/anila/service-clients/router-primary/token` | 服務憑證 `csk-`（`client_type=router`） | 目錄 gid 10002 `anila-svc-tokens` |
| anila-studio | `/run/anila/service-clients/anila-studio/token` | 服務憑證 `csk-`（`client_type=studio`） | 目錄 gid 10003 `anila-studio-tokens` |
| ingestion-worker | `/run/anila/service-clients/ingestion-worker/token` | 系統使用者的 `sk-` API key（雜湊存在 `api_keys`，不是 `service_clients`） | 目錄 gid 10004 `anila-worker-tokens` |

子目錄的擁有者是 uid 10005（沒有服務用這個 uid），mode 2770，只有該群組進得去。CSP、studio、worker 都是 uid 10001；若憑證放在同一個他們擁有的目錄，0640 擋不住互讀。`csp-credential-dirs` 在 CSP 啟動前用 root 把這三個目錄建好（既有 volume 也不會漏），並刪掉根目錄的扁平 `<client>.token`。有刪到檔案時留下標記，CSP 把 `router-primary` 輪替一次且不留寬限，複製走的舊檔因此失效。沒有專屬目錄時 CSP 拒絕發布、不退回扁平檔，`/health` 降級。檔案本身是 0640。CSP 加入上述三個群組才能寫；每個消費者只加入自己的群組。上層目錄仍是 gid 10002、mode 2750。日誌不記明文。chmod／chown 失敗，或寫完之後的 mode／gid 不符，這次發布算失敗，readiness 降級。約 30 天輪替一次。服務憑證的上一把在寬限期（預設 24 小時）內仍可通過驗證；worker 的舊 key 同樣留到寬限期。環境變數 `INTERNAL_PLATFORM_API_KEY` 不再參與換發。`ANILA_SERVICE_CLIENT_AUTO_PROVISION=0` 時 `/health` 是 503。

三個服務都讀 `ANILA_SERVICE_TOKEN_FILE`。檔案變了會重讀；CSP 回 401／403 時再讀一次才放棄。路徑有設而檔案不在或讀不到時，不改用別的憑證。`/health`（worker 沒有 HTTP，啟動日誌與 `credential_health()`）的 `token_source` 是 `file`、`file_missing` 或 `file_error`。studio 在 `file_missing`／`file_error` 時 `/health` 是 503。

舊的共用 `CSP_SERVICE_TOKEN` 已從設定欄位刪除，服務不會再把它讀進設定。請求只帶那把祕密會得到 401。資料庫裡 `is_legacy` 的列（現用或寬限複本）都不是身分；自動核發換掉這種列時，不把舊祕密留成寬限憑證。`.env` 或 shell 裡這兩個鍵有非空值時，部署拒絕，訊息只印鍵名。部署檢查照 compose 的讀法解析 `.env`：引號值只取到結束引號，沒加引號的值「空格 + #」起是註解。程序環境裡還看得到非空值時，該服務拒絕啟動，並請操作者刪掉。Router、Studio、asr-gateway 只讀 `ANILA_SERVICE_TOKEN_FILE`。長效 `agent_credentials` 已退役（代理用 5 分鐘派工 JWT）；遷移 `r1_0048` 撤銷仍有效的列並清掉寬限複本，驗證路徑也不再接受那些列。

`asr-gateway` 只讀專屬憑證檔。本機生圖服務已刪除，不再讀 `INTERNAL_PLATFORM_API_KEY`。模型若有自己的金鑰而解密失敗，這次呼叫失敗（「模型暫時無法使用：憑證無法讀取，請通知管理員」），管理端測試、健康檢查、整批帶入、背景健康檢查與串流錯誤事件用同一句，不改用全域 `MODEL_GATEWAY_API_KEY`。警報與健康迴圈共用 `health:model:{id}`，同一模型只有一筆，金鑰修好、下一輪健康檢查恢復時自動結案；請求路徑上同一模型 60 秒內只送一次，寫入與寄信交給背景執行緒。沒有專屬金鑰的模型仍用全域金鑰。

緊急吊銷服務憑證：治理中心「服務客戶端」按吊銷。CSP 不會把已吊銷的列重新核發，並刪掉憑證檔。要恢復時，刪掉那筆已吊銷的 `service_clients` 列，然後重啟 CSP（或等下一個週期）。worker 的 key 不在那個畫面：把名為 `ingestion-worker-system-key` 的 API key 停用後，CSP 不會再核發，並刪掉憑證檔；要恢復就刪掉那些已停用的 key 列再重啟 CSP。

### 換上這版之後，擁有者要從 `.env` 刪掉的行

部署並確認 studio、worker、router 的 `token_source=file` 之後，刪掉這些行（整行，含值）：

- `CSP_SERVICE_TOKEN=...`

worker 已經不讀 `INTERNAL_PLATFORM_API_KEY`、`EMBEDDING_API_KEY`、`VISION_API_KEY`、`RELATION_LLM_API_KEY`。本機生圖服務也不再讀它。

同時要重建 csp、router、anila-studio、ingestion-worker 映像（群組 10002／10003／10004），再用更新後的 compose 啟動。憑證 volume 仍是 `anila-service-credentials`（dev 是 `anila-service-credentials-dev`）。不要再把 `CSP_BOOTSTRAP_TOKEN` 灌進 router。

`.env.example` 已拿掉的鍵：`CSP_SERVICE_TOKEN`。

## JWT 簽章金鑰

CSP 自己保管 RS256 簽章金鑰，放在資料表 `jwt_signing_keys`（遷移 `r1_0052`）。私鑰用既有的憑證加密（由 `SECRET_KEY` 衍生）存放。狀態是 `next` → `active` → `retiring` → `retired`。JWKS（`/.well-known/jwks.json`，快取 `max-age=3600`）公布 `next`、`active`、`retiring`，並帶每把鑰匙的 `anila_key_state`。只有 `active` 拿來簽名。`next` 升成 `active` 之前不能驗權杖。`retiring` 只接受 `iat` 早於退役時間的權杖。沒有 `iat` 的舊權杖只在從 PEM 匯入的那一把上接受。沒有 `kid` 的權杖拒絕。平常讀 active 金鑰不拿資料庫鎖；鎖只用於匯入與輪替，而且鎖內會再確認一次。

預設每 90 天把 `next` 升成 `active`。天數是平台設定 `auth.jwt_rotation_days`（1–365，預設 90），下一次排程檢查就生效。90 天週期會提前 7 天建立 `next` 並公布；而且一把鑰匙至少先公布一個 JWKS 快取週期，才會變成 `active`。同一輪排程裡剛建立的 `next` 不會立刻升上去。舊鑰接著進入 `retiring`，保留時間比 refresh token **允許的最長天數**再多一個快取週期（不是當下畫面上的那個天數），所以平常輪替不會把人登出，5 分鐘的派工權杖也還驗得過。多個 worker 用資料庫 advisory lock；啟動先跑一次，之後每小時再檢查。

第一次啟動、資料表還是空的，會把 `secrets/jwt-private.pem` 以當時的 `JWT_KID` 匯入成 `active`，既有登入與派工權杖繼續有效。匯入之後簽名不再讀這兩個 PEM。服務不會刪除它們。擁有者可以留著當**最初那一把**的備份。後來輪替出去的私鑰只在資料庫裡：要復原整圈鑰匙，還原資料庫備份，並且用同一把 `SECRET_KEY`。不要重產 PEM 來復原，那不會換回已輪替的鑰匙，也會讓人以為舊權杖還能用。

更換 `SECRET_KEY` 之前，先用舊、新兩把密鑰跑 `infra/deployment/scripts/reseal-credentials.py`（先不加 `--apply` 看筆數，確認後再 `--apply`），把 `jwt_signing_keys` 以及其他用同一套密封的憑證轉封。轉封完成、環境改成新密鑰之後才重建 CSP。若 active 私鑰解不開，CSP 啟動時直接拒絕，不會拖到第一次登入才失敗。`scripts/reencrypt-credentials.py` 只升級同一把密鑰的 PBKDF2 迭代次數，不能拿來換 `SECRET_KEY`。

緊急輪替在治理中心「平台設定」（擁有者或管理員、CSRF、寫入稽核）。確認文字是「所有人會被登出，進行中的派工權杖會失效。自行驗證派工權杖的 agent 最多還能接受舊鑰 5 分鐘」。新鑰立刻成為唯一公布的鑰匙，其餘鑰匙退役，並把退役的 `kid` 送到既有的 Redis 撤銷通道。Studio 與 ASR 的撤銷快取會立刻拒絕這些 `kid`，即使 JWKS 快取裡還留著舊公鑰。自行驗證派工權杖的 agent 不讀這份撤銷清單，殘餘窗口最多 5 分鐘。Studio、ASR、anila-core 與 quickstart 遇到不認識的 `kid` 會在同一把鎖裡預留下一次重抓，失敗也要退避，其餘請求共用那一次結果。Studio 不再讀 `JWT_KID`。

## 生圖（2026-09-26：不部署本機模型）

治理中心「模型角色」多了「生圖模型」（`image_generation`，類型用既有的 `image`）。有設且健康時，Studio 經 CSP `POST /v1/images/generations` 配圖，不直連模型主機。沒設、不健康或請求失敗時，簡報仍用版面、圖示、圖表、表格，以及知識庫文件裡已有的圖，不留空的配圖框。

本機 `flux2-dev`／`flux2-dev-agent`、compose 服務、`/uploads/flux` 與 `FLUX_*` 環境變數已從這棵樹拿掉。這次沒有刪除主機上的 Docker volume。若先前起過 Studio，具名 volume `anila_anila-studio-flux-cache` 與 `anila-platform-dev_anila-studio-flux-cache-dev` 可能還在，由擁有者自行移除。`share/uploads/flux` 若還在磁碟上，同樣先留著。

## GitLab（2026-09-26 先拿掉）

compose 不再宣告 `gitlab` 服務，也不再宣告 `gitlab_config`、`gitlab_logs`、`gitlab_data`。nginx 各 listener 不再代理 `/gitlab`。部署腳本不再寫 `GITLAB_*`。code-server 與 `codeserver-init` 掛 `profiles: ["maint"]`，n8n 仍是 `ops`。平常 `up` 不會把它們拉起來；最後彩排才加 `--profile maint`（n8n 另加 `COMPOSE_PROFILES=ops`）。安裝腳本在 `UID`、`GID`、`DOCKER_GID` 尚未寫入，或那一行是空的、只有空白、或空引號時，用 sudo 的身分或安裝根目錄的擁有者，以及 `getent group docker`，就地換掉那一行，寫進 `state/.env`。

這次沒有刪除主機上的 Docker volume。舊的 `anila-platform_gitlab_data` 還在主機上，之後由擁有者自行移除。

## 設定面（2026-09-27）

`.env` 只留站台名稱 `ANILA_HOST`。入向 Host 白名單是它，加上 localhost、127.0.0.1、`::1`、會呼叫 CSP 的 compose 服務名，以及任何裸 IPv4／IPv6。其他主機名拒絕。nginx 的 `map $is_anila_host` 在容器啟動時（官方映像對 `/etc/nginx/templates/*.template` 做 envsubst）代入同一個 `ANILA_HOST`，不用各站改 conf。信任主機在治理中心。模型與 agent 只在治理中心登錄；空的登錄表可以起來，角色顯示「需設定」。沒有平台嵌入角色時，新建知識庫不寫模型名；入庫先等，狀態是「平台嵌入模型尚未在治理中心設定」，角色設好後才繼續。LLM 文件關聯用摘要角色，沒設就略過，規則與相似度關聯照常。掃描 PDF 的文字辨識是 Docling，原生解析器碰到掃描件會說明需要 Docling，不再走視覺模型 OCR。正式部署（`deploy-prod.sh` 與 `intranet-deploy.sh`）拒絕 `ANILA_ALLOW_DEV_SECRET=1`、`CARD_DEV_TRUST_TEST_CA=1`、指到測試 CA 的 `CARD_CA_BUNDLE_PATH`，以及不是 `card-only` 的 `ANILA_AUTH_MODE`。`card-only` 仍保留擁有者的密碼登入。`SECRET_KEY` 必填，不再接受 `CSP_SECRET_KEY` 別名。

## 外部服務（2026-09-26）

文件解析（Docling）與語音辨識的位址在治理中心「外部服務」，不在 `.env`。

1. 以管理員打開治理中心的「外部服務」。
2. 文件解析：填遠端 Docling 的位址、需要的話填憑證、打開啟用。不要把帳密寫進網址。沒啟用時，畫面寫明擷取走內建原生解析器。啟用之後服務中斷，擷取工作會失敗，不會改回原生解析器。
3. 語音辨識：填遠端解碼器位址、選 native 或 openai、憑證可留空、打開啟用。健康由 CSP 背景探測，畫面只顯示上次結果，不會因為重新整理就把憑證送出去。Shell 與 ANILA LM 只在這一列是啟用且健康時顯示麥克風。頁面載入與回到視窗時各問一次，未啟用時不會去打解碼器。
4. asr-gateway 預設就啟動（2026-09-29 起不再用 `asr` profile）。它只負責切句與轉送，位址向 CSP 讀；解碼端沒設定或不健康時麥克風自己藏起來，容器健康檢查只看 gateway 本身（`/asr/health` 回 200 或 503 都算活著），所以解碼端壞了不會讓平台更新判定失敗。沒有本機 whisper。語音模型（權重與解碼器）屬於模型側，由別人維護；平台只留 asr-gateway。
5. 開機不再從 `.env` 匯入文件解析或語音位址。請在治理中心「外部服務」填。`.env` 裡若還留著 `DOC_PARSER`、`DOCLING_URL`、`DOCLING_SERVICE_TOKEN`、`ASR_DECODE_URL`、`ASR_DECODER_TOKEN`、`ASR_DECODE_PROTOCOL`、`ASR_DECODE_API_KEY`、`ASR_OPENAI_MODEL`，刪掉即可，服務不會讀。

憑證存在 CSP 自己的金鑰檔裡，不是模型 API key 那把 `SECRET_KEY`。畫面只看得到「有沒有憑證」。語音憑證只有 asr-gateway 讀得到，文件解析憑證只有 ingestion-worker 用它的憑證檔讀得到。

## 帳號閒置（2026-09-29）

超過設定天數（預設 180，在治理中心帳號區改，沒有新的環境變數）沒有成功登入的已核准帳號，每日 UTC 排程會先試算、下一個 UTC 日才停用。尚未核准的帳號不會被自動停用，因為他們本來就登不進去。擁有者與系統帳號也不停用。因閒置停用的人再次登入會回到待核准，不發權杖；管理員已經重新啟用的人照正常登入。排程若停在試算中或停用中，約五分鐘後重試同一天，不會等到午夜。藍色通知的人數只算這一輪實際停用的。

## 警報（2026-09-27）

未處理的警報會在治理中心每一頁上方出現紅橫幅（擁有者與管理員），連到「警報」。確認或解決後橫幅消失。寄信在同一頁的「警報寄信」：SMTP 主機、連接埠、不加密／STARTTLS／SSL、選填帳密（密碼只寫入）、寄件者、群組信箱、啟用，以及「寄測試信」。沒有 `ANILA_ALERT_SMTP_*` 環境變數。寄失敗只記在該區與日誌，偵測不會停。稽核帳保留期是 365 天。

磁碟使用率 80% 起為 high、95% 為 critical（2026-09-29 從 85% 下修警告線）。儀表板另有一格，只顯示掛載標籤、使用率與剩餘 GiB，不顯示宿主機路徑。

HTTPS 憑證由資訊單位用院內 CA 核發，擁有者更換 `infra/nginx/certs` 的檔案。CSP 對 compose 裡的 nginx 做 TLS 連線（SNI 用既有的 `ANILA_HOST`），讀伺服端憑證的到期日，不掛載、也不讀私鑰。未滿 30 天是 high「HTTPS 憑證將於 N 天後到期，請向資訊單位申請新憑證」；未滿 7 天或已過期是 critical；換新後結案。nginx 連不上不開這條（入口無回應由既有偵測器負責）。到期日也顯示在儀表板。

## 備份（2026-09-27）

compose 的 `backup` 服務跟資料庫共用 `anila-pgvector:local`。起來先備份一輪，成功後每 24 小時再跑；失敗約 15 分鐘後重試。產出在 repo 的 `share/backups/`（宿主機路徑可用 `ANILA_BACKUP_DIR` 改）。內容是 `pg_dump -Fc`，加上上傳、附件、靜態檔、公開 CA、快速起步 profile、Studio 成品、路由會話、n8n。保留 14 份每日、6 份每月。

`status.json` 只讀掛進 CSP。治理中心儀表板顯示「最後一次備份」。超過 36 小時沒有成功，或最近一輪失敗，走既有告警。

`.env`、`secrets/`、CSP 本地金鑰 volume、nginx 私鑰要另外留一次。模型權重與 Redis 不在每日包裡。另一台用 `docs/runbooks/csp-db-backup-restore.md` 的 rsync 來拉。平台這台不設定對方位址，也不要再跑 `backup-csp-db.sh` 或改 crontab。

## 出貨與內網更新（2026-09-29）

有網路的開發機跑 `scripts/release/build-release.sh`，產出 `anila-YYYY.MM.DD-N`（含 asr-gateway 映像，預設不起），結束時印出壓縮檔 SHA256。內網主機沒有網路，只跑出貨包裡的 `anila-update.sh`：先拿安裝鎖，核對清單，確認治理中心有生效公告，先停寫入再備份資料庫與 studio volume，載入映像後用映像 ID 核對並標進這個 compose 專案，`docker compose up` 先不起 nginx，內部健康檢查過了才開入口。已有同名 compose 專案或 volume 卻沒有安裝記錄時，要用 `adopt` 先備份再認領：收進舊的 `.env`、準備目錄後才啟動，成功記成 `adopt`。安裝記錄一旦存在就是唯一依據，不會改寫成別的根目錄。失敗自動回復；資料庫或檔案還原失敗仍拉起原先那一版、線上資料庫不換，並同時留下更新失敗與回復失敗。手動 `rollback` 會先停寫入，再用備份時間秀出會消失的對話／訊息／文件筆數，打對版本才把備份與檔案快照換上；線上庫先改名保留，健康檢查過了才丟掉。還原失敗或健康檢查沒過，都會把資料庫、檔案、標籤與指標切回正在跑的那一版。更新、認領與回復的結果追加在 `/opt/anila/state/operations.log`，資料庫可用之後也寫稽核。第一次安裝只問 `ANILA_HOST`。儀表板顯示映像裡的版本。步驟見 `docs/deploy/UPDATE.md`。這套還沒在這台對線上那套打包或更新。

舊的 `intranet-deploy.sh`、`build-and-export-for-intranet.sh`、`anila-serve.sh` 已退役，放在 `infra/deployment/archive/intranet-legacy/`，直接執行或 source 都會停。模型那邊的 `model-serve.sh`、權重下載與分塊仍在用，移到 `infra/deployment/archive/model-side/`。

## 尚未當成上線完成的項目

- P2.6 出貨腳本已改寫，尚未對線上那套打包或更新（見 `docs/deploy/UPDATE.md`）
- Q53 人資 Oracle（`csiih.vihbuy`）等資安放行 `oracledb` wheel
- G9 對話密等標記介面
- `app.jsx`／`router_server.py` 大檔拆分（等主流程測試穩定後再抽）
- 真模型主機的負載（`tools/loadtest/` 量的是平台自己，模型是可調速度的假上游）

## 本輪工程債（2026-09-18 抽查）

1. Shell／治理測試基線（composer accessible name、群組 `extractError`）
2. 共用工作站資料夾 localStorage 依帳號隔離
3. 檢索 `calibrated` 必須對應當下 embedding 模型
4. （2026-09-27 已處理）備份由 compose 的 `backup` 服務排程，還原含資料庫與檔案。見 `docs/runbooks/csp-db-backup-restore.md`。

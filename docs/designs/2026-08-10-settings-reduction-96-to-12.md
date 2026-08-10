# 設定參數削減裁決（fable 仲裁票，2026-08-10）

> ⚠ **這是仲裁票的建議清單,不是規格。** 擁有者 2026-08-10 深夜拍板採用它的**綱領**
> (96 → 12,判準見 `docs/OWNER-QUESTIONS.md` 的 Q46),但**逐顆的去處要執行時自己驗證讀取點**
> ——尤其「還有沒有別的服務在讀這個 env 名」。清單裡任何一顆與實際讀取點衝突時,**以讀取點為準**。
>
> 執行派工單在 scratchpad 的 `task-m-settings-reduction.md`;被取代的舊路線在分支
> `SUPERSEDED-wt-settings-open-Q45-route`(**只能參考,不可合併**——它上面有兩個 CRITICAL)。


## 結論數字

**96 → 12 顆設定**（設定頁只剩 12 列，全部 C 類、改完立即生效）。
其餘 84 顆的去向：**35 顆退回部署事實**（env/compose，裝機定一次，退出畫面）、
**12 顆移出設定語意**（祕密 → secrets 掛載）、**31 顆降級為程式常數**（連 env 讀取點一起刪）、
**3 顆併掉**、**3 顆刪掉**。12+35+12+31+3+3 = 96。

**連帶退役的機制**（這才是擁有者說的 loading）：
- 「56 顆需重啟才生效」→ **0 顆**。留下的 12 顆全是 C 類，`B_EDIT`／`B_LOCKED`／
  開機覆蓋（`apply_boot_overrides`）／`restart_required` 語意／「這次開機沒有載入設定覆蓋」
  紅字，整組跟著退場。
- 五種互不相容的 bool 判準 → **一種**（留下的 12 顆只用到 int／float／`!= "0"` 一種 bool）。
- Q45 追加裁決要求的「先補 12 顆 accept-any 值域再放寬載入器」**大半失效**：
  最危險的那幾顆（`ssl_cert_file`、`card.ca_bundle_path`）根本離開畫面，不再需要值域工程。

## 那句判準（寫進專案文件用）

> **一顆值要留在設定頁，必須答得出：上線之後，誰、在什麼情境、為什麼不能等下一次改版。**
> 答不出來的——是祕密就進 secrets 掛載；是裝機才定一次的就留在 compose；其餘寫死成程式常數。
> 設定頁只放「會被改第二次」的東西。

配套的一句（給登錄表檔頭換血用）：登錄表只宣告設定頁上的東西；
其他 env 變數的唯一目錄是 `.env.example`（名字＋一句用途＋消費者是誰）。
「隱形旋鈕」的解法不是把 95 顆全畫上畫面，而是**把不該存在的讀取點刪掉**——
降級為常數的 31 顆連 env 讀取點一起消失，沒有變隱形的問題。

## 為什麼這樣裁（against 登錄表自己的憲章）

`settings_registry.py` 檔頭主張「csp 讀得到的每一顆都要宣告，少一顆＝一個看不見的開關」。
這個主張對「可見性」是對的，但它把解藥開錯了：42 顆連 compose 都沒出現過的變數，
正確處置不是「畫上畫面讓人看見」，是**刪掉讀取點讓它不存在**。
今晚 `auth.allow_dev_secret` 變成 CRITICAL 的因果鏈就是證據：
**它先被當成「一顆設定」列進表，才有後來被開放編輯、被開機套用的路徑。**
參數少一顆，就少一個變成洞的機會——這句話是這份裁決的軸心。

⚠ 執行時注意：現有測試套件釘著「登錄表 ↔ config.py 逐顆對應」，
憲章改了測試要跟著改，否則第一次維護就會有人「好心」把 84 顆加回來。

---

## 一、保留為設定（12 顆，全轉 C 類）

| # | key | 現況 | 氣隙裡誰會改它 |
|---|-----|------|----------------|
| 1 | `institutional_kb.score_threshold` | C | **不可砍**。換嵌入模型就必須重新校準（HANDOFF-08-07 §八.1），預設值是拿替代模型量出來的猜測 |
| 2 | `memory.retrieve_min_cosine` | C | 同上一顆的孿生（描述自承「同一種東西」）：嵌入模型分數分佈不可預知，上線後要量測調整 |
| 3 | `memory.retrieve_top_k` | C | 與門檻成對的檢索品質鈕；校準時兩顆一起動 |
| 4 | `proxy.llm_timeout` | C | 內網模型慢，逾時是使用者「回答斷掉」抱怨的第一線自救 |
| 5 | `proxy.embedding_timeout` | C | 同上，嵌入批次大時要放寬 |
| 6 | `auth.access_token_expire_minutes` | SEC | 使用者抱怨重登太頻繁時的政策鈕；⚠ 需重接：發票時走 `get_setting`（現為 boot 讀定） |
| 7 | `auth.refresh_token_expire_days` | SEC | 同上成對 |
| 8 | `limits.department_max_depth` | C | 院內組織樹深度是業務事實，超過 3 層時管理員要能自救 |
| 9 | `limits.action_invoke_per_min` | C | 使用者撞到限流時，維運當場放寬，不必等改版 |
| 10 | `limits.attachment_budget_ratio` | C | 「附件被截斷」抱怨的第一線調節鈕 |
| 11 | `intl.zh_normalize` | C | 簡轉繁誤傷（程式碼區塊等）時要能整顆關掉 |
| 12 | `intl.query_expansion` | C | 檢索品質異常時的除錯開關（關掉看是不是擴展惹的禍） |

SEC 兩顆搬進可編輯前，記得 Q45 已點名 `platform_settings` 沒有 CHECK/RLS——
但留下的 12 顆全是有界數值與 bool、逐顆 `domain_fn`，風險面比 63 顆全開小兩個數量級。

## 二、退回部署事實（35 顆：env/compose 定一次，退出登錄表與畫面）

唯一目錄是 `.env.example` 分節註解。其中標 (dev) 的只出現在 dev overlay，永不進正式 `.env`。

| key | 一句裁決 |
|-----|----------|
| `app.site_url` | 每個部署不同（localhost vs FQDN），裝機定一次 |
| `app.host` | 只被開機 placeholder 檢查消費，compose 事實 |
| `auth.jwt_kid` | 三服務同值、輪鑰時才動——輪鑰不該要求重建映像，留 env |
| `auth.allow_dev_secret` (dev) | **env-only 開發逃生門，永不入表**。今晚 CRITICAL 的直接教訓 |
| `card.enabled` | 部署姿態（內網開、dev 關）；紅線鄰接，本來就不該從畫面關 |
| `card.initial_owners` | bootstrap 員編 CSV，裝機事實 |
| `card.ca_bundle_path` | **不可寫死也不可上畫面**：CA 換代（CSPKI G2）時要能不重建映像換 bundle |
| `card.dev_trust_test_ca` (dev) | mock 讀卡機用，env-only |
| `card.dev_skip_nonce_binding` (dev) | env-only；若 mock 流程其實不需要，升格為刪 |
| `network.allowed_origins` | CORS 白名單，隨部署網域定一次 |
| `network.allowed_hosts` | 有真中介層（`main.py:517` TrustedHostMiddleware），正式部署設 FQDN，裝機事實 |
| `network.allow_http_model_endpoint` | P0.2 擁有者拍板的分域例外，**保持 env-only**（PLAN 紀錄在案） |
| `network.allow_http_agent_endpoint` | 同上（aiops NodePort 純 http） |
| `network.allow_grpc_endpoint` | 活碼（Triton grpc 健檢，`url_guard.py:86`）；交付清單無 Triton 則升格為刪 |
| `network.allow_private_endpoint` (dev) | url_guard 自述「on-prem dev」用，env-only |
| `network.environment` | 部署姿態字串，compose 寫 production 一次定案 |
| `network.ssl_cert_file` | **不可寫死**：CSPKI 信任庫路徑，換憑證要能不重建映像。踩雷史（§2）＝更要離開畫面 |
| `alerts.smtp_enabled` ~ `smtp_use_tls`（7 顆） | relay 方案未定（設計 §3.2）；定案後裝機寫一次。**整組不上畫面** |
| `queue.redis_url` | 跨服務 DSN，執行期改＝事故製造機（登錄表自己的話）；併案收斂三讀取點的預設值分歧 |
| `seed.models`／`seed.agents`／`seed.links` | 開機 bootstrap JSON，裝機事實；之後歸治理中心。`seed.links` 的 upsert 語意複雜度偏高，可考慮刪（見擁有者問題 3） |
| `memory.llm_model` | gateway 模型陣容換代（gemma4 退役）時要能重指，env 事實 |
| `ingestion.pdf_ocr_fallback` | worker 側部署開關（csp 端 compose 刻意 false 是設計） |
| `ingestion.vision_url`／`vision_model` | OCR 視覺模型指向，部署事實 |
| `ingestion.pdf_ocr_concurrency` | 隨 .15 硬體定一次 |
| `ingestion.doc_parser`／`docling_ocr_langs` | 映像有裝 docling 才活；沒裝則兩顆升格為刪（見擁有者問題 3） |

## 三、移出設定語意（12 顆祕密 → secrets 掛載，不在畫面）

`db.url`、`db.migration_url`、`db.app_role_password`、`auth.secret_key`、`admin.password`、
`proxy.model_gateway_api_key`、`alerts.smtp_password`、`service.csp_service_token`、
`service.internal_platform_api_key`、`service.codeserver_password`、`seed.api_keys`、
`ingestion.vision_api_key`。

- 開機的「非 dev 預設值」檢查（`startup_security.py`）**保留**，那是守衛不是設定。
- `internal_platform_api_key`／`codeserver_password`：csp 只做開機檢查不消費——
  全棧盤點若零消費者，升格為刪（連檢查一起）。
- 畫面上要不要留一塊唯讀「已設定／未設定」面板：**綁在擁有者問題 1 一起裁**，本裁決不預決。

## 四、降級為程式常數（31 顆：刪 env 讀取點，寫死）

| key | 寫死成 | 備註 |
|-----|--------|------|
| `app.name` | "ANILA" | 一人維運不會改平台名 |
| `app.version` | 隨版本發佈 | 版本屬於 build，不屬於設定 |
| `app.debug` | False | 唯一讀取點是 import 期 engine echo，本來就套不上 |
| `app.static_dir` | 映像內路徑 | import 期絕對路徑，改了要重建映像＝它就是映像的一部分 |
| `app.python_unbuffered` | compose 固定 "1" | 直譯器旗標，非應用設定 |
| `auth.jwt_algorithm` | "HS256"（寫死在呼叫點） | ⚠ **不是死參數**：`card_auth_service.py:81` 等 6 處真的在用；正因如此更不可調——調了驗章就斷 |
| `auth.jwt_private_key_path`／`jwt_public_key_path` | "secrets/jwt-*.pem" | 掛載位置由 compose 保證，路徑無需可調 |
| `auth.cookie_secure` | True | 入口皆 https；dev 若真要 http 走 dev overlay，不是設定頁的事 |
| `admin.username` | "admin" | Q45 用這個帳號名當祕密閘門——**閘門更該釘死，不該是可改的設定** |
| `auth.allow_auto_keygen` → 見刪 | — | — |
| `proxy.max_retries` | 3 | 微調鈕，故障該在 gateway 端解 |
| `proxy.retry_base_delay` | 0.5 | 同上 |
| `health.check_interval` | 60 | 沒有人會調健檢週期 |
| `alerts.check_interval` | 60 | ⚠ 08-09 才重接成 C 類（沉沒成本，具名認列）——仍裁常數：沒有情境需要調告警輪詢 |
| `usage.batch_size`／`flush_interval` | 100／5 | 內部效能參數 |
| `storage.attachment_path` | "data/attachments" | 容器內路徑寫死；宿主側是 compose volume 的事 |
| `storage.ingestion_upload_dir` | "/var/anila/ingestion-uploads" | 同上 |
| `queue.token_revocation_redis_timeout` | 2.0 | ⚠ 同為 08-09 重接的沉沒成本——仍裁常數：0.1–60 秒的鈕沒有使用情境 |
| `limits.message_max_siblings` | 20 | 微調鈕 |
| `limits.action_max_body_chars` | 20000 | 微調鈕 |
| `limits.default_context_window` | 128000 | 模型該在治理中心登記視窗，後備值不是鈕 |
| `limits.attachment_token_safety` | 1.15 | 估算係數，無人會調 |
| `limits.attachment_max_stored_tokens` | 800000 | DB 膨脹護欄，常數即可 |
| `intl.zip_filename_encoding` | ""（cp950→gbk 內建序） | 極罕用；真遇到再說 |
| `memory.max_chunk_chars` | 1200 | 切塊參數 |
| `memory.http_timeout` | 30.0 | 與 proxy 逾時不同通道，但無獨立調整情境 |
| `agents.template_dir` | repo 內建路徑 | import 期模組常數，本來就套不上 |
| `ingestion.pdf_ocr_vision_prompt` | 程式內建 prompt | prompt 屬於程式行為，迭代走版本 |
| `ingestion.pdf_ocr_dpi`／`pdf_ocr_max_pages` | 200／100 | worker 護欄常數 |

## 五、併成一顆（3 顆）

| key | 併去哪 |
|-----|--------|
| `auth.secret_key_fallback`（CSP_SECRET_KEY） | 併入 `SECRET_KEY`：一個祕密一個名字。⚠ 先確認 fallback 讀取點遷移與既有憑證加密資料的相容（`models.py:1356` 有輪替警語） |
| `card.require_card_only` | 與 `card.enabled` 併成一顆 `ANILA_AUTH_MODE`（password／mixed／card-only），env-only 部署事實 |
| `network.trusted_hosts` | 併入 DB 表 `trusted_hosts`（治理中心管理），env 只留開機 backfill——**掛在 HANDOFF #5 既有的雙源收斂 follow-up，本裁決不代行** |

## 六、刪掉（3 顆）

| key | 理由 |
|-----|------|
| `db.legacy_sqlite_path` | 重啟樹、.15 資料可刪砍掉重來（RESTART §四）→ 舊 SQLite 匯入通道整段（`startup_migrations.py` opt-in 部分）可刪。**條件**：擁有者確認無舊資料要接 |
| `auth.allow_auto_keygen` | dev 產鑰走既有腳本（keypair 產到 `./secrets`），不需要執行期旗標；正式環境永遠 False 的旗標就是未爆彈 |
| `ingestion.vision_verify_ssl` | **「關閉 TLS 驗證」不該是旋鈕**。信任問題用 `SSL_CERT_FILE` 指 CSPKI bundle 解，這正是 allow_dev_secret 的同族洞 |

---

## 不可砍清單（分兩種語意，別搞混）

**A. 必須留在畫面可編輯**（拿掉＝氣隙內失去自救能力）：
- `institutional_kb.score_threshold`＋`memory.retrieve_min_cosine`＋`memory.retrieve_top_k`：
  換嵌入模型就要重新校準，這是 HANDOFF-08-07 明文的長期承諾。
- `proxy.llm_timeout`／`embedding_timeout`：內網模型效能不可預測，逾時是第一線自救。

**B. 必須保留 env 可改、但要離開畫面**（寫死＝違反紅線或自斷後路）：
- `SSL_CERT_FILE`、`CARD_CA_BUNDLE_PATH`：憑證換代不能要求重建映像。
- `ANILA_ALLOW_HTTP_ENDPOINT`／`ANILA_ALLOW_HTTP_AGENT_ENDPOINT`：P0.2 擁有者拍板的分域例外。
- `ANILA_TRUSTED_HOSTS`（至收斂 follow-up 完成前）：.12 放行靠它。

**對 §5 紅線的核對**：SSRF guard、卡登驗章、JWT 信任錨全部**沒有被弱化**——
把紅線旋鈕從畫面上移走是**強化**（allow_dev_secret 教訓的一般化：
能從網頁動到的安全開關，就是一個等著被動的安全開關）。

## 與 Q45 的關係（衝突，明著擺）

Q45（08-10）裁「全部可編輯＋祕密限 admin 帳號」；同日擁有者又說「為何要這麼多參數、loading 很重」。
兩句話的**綜合案**：先把 96 砍到 12，剩下的 12 顆全部可編輯、立即生效——
「全部可以設定」在 12 顆的世界裡自動成立，而且不需要 Q45 追加裁決那串值域補課。
今晚的 allow_dev_secret CRITICAL 就是「96 顆全開」路線的第一張帳單。
**此綜合案是否取代 Q45 原案，要擁有者點頭**（見下）。

## 需要擁有者決定的三件事

1. **確認綜合案取代 Q45 原案**：設定頁收斂到 12 顆全開放，祕密退回 secrets 掛載
   （畫面要不要留唯讀「已設定／未設定」面板一併裁）。不點頭則維持 Q45 的 63 顆開放＋值域補課路線。
2. **SMTP 告警整組（8 顆）**：relay 方案未定——是留 env 等定案，還是整個郵件告警功能
   （含程式碼）先刪、上線後有需求再加回？
3. **交付清單盤點**（決定 6 顆條件刪的生死）：docling 有沒有進映像（2 顆）、
   code-server 出不出貨（1 顆）、Triton grpc 端點會不會接（1 顆）、
   舊 SQLite 有沒有資料要匯（1 顆）、`seed.links` 要不要留 bootstrap 通道（1 顆）。

## 執行順序建議（不在本裁決範圍，供排程）

刪常數（31）→ 移祕密（12）→ 退部署事實（35，含 `.env.example` 目錄化）→
併三顆 → 12 顆轉 C 類重接（token expiry 兩顆要動讀取點）→ 退役 B_EDIT/B_LOCKED/開機覆蓋機制 →
改登錄表憲章與對應測試。每一步都比 Q45 的值域補課路線小。

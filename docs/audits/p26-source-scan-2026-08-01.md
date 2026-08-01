> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# P2.6 源碼安全掃描 — 2026-08-01

> **這份報告取代 `docs/audits/p26-source-scan-2026-07-31.md`。** 那份從未進 repo,
> 它的「3 高 2 中」無從逐條核對,兩個「待裁決」中風險的內容在全樹查無紀錄。
> 重跑一次比考古便宜(PLAN.md 2.6 的收尾判準)。這一份**已經在 repo 裡**。
>
> - **掃描對象**:`restart/from-redesign` 主樹,commit `efc03811`(2026-08-01)。
> - **方法**:唯讀靜態分析。每一條發現都追到可達的程式路徑並寫出具體情境;
>   grep 命中但沒有路由到得了的,不算發現,不寫進來充數。
> - **威脅模型**:氣隙內網 + PKI 卡登。**需要公開網際網路才成立的威脅一律不列**;
>   真正成立的是**內部**威脅——特權管理者造假、跨使用者存取、密等繞過。
> - **不含**:sibling worktree 正在施工的 P2.1 身分、附件解析器(掃不到,見文末)。

---

## 一、執行摘要

| 嚴重度 | 數量 |
|---|---|
| CRITICAL | **0** |
| HIGH | **0** |
| MEDIUM | **3** |
| LOW | **6** |

**沒有高風險項。** PLAN.md 2.6 的驗收條件(「無 OWASP Top 10 高風險項」)在
**M-1 修掉後**成立;M-1 是本次唯一一條「真的有人能拿它做壞事」的授權缺陷,
其餘兩條中風險是部署姿態與結構性陷阱,不是現正在漏的洞。

掃描過程中最值得記下的一件事:**這棵樹的安全基線比預期好很多**。
卡登驗章(簽章＋鏈＋nonce＋dev CA 的 production 圍欄)、RS256 信任錨、
SSRF 雙層守衛(註冊時＋呼叫時)、CSRF double-submit、撤銷清單 fail-closed、
前端兩個 XSS sink 都有正確消毒——這些都不是「看起來有做」,是逐行追過確認有效。
下面 9 條是在這個基線之上找到的縫。

---

## 二、發現

### M-1 · MEDIUM · A01 破損的存取控制
**`/v1` artifact 寫入面接受任何 service token,且不做物件層擁有權檢查**

- **位置**
  - `services/csp/app/api/artifacts.py:101-123` — `require_service_caller`
  - `services/csp/app/api/artifacts.py:269-280` — `PATCH /v1/artifact-jobs/{job_id}`
  - `services/csp/app/api/artifacts.py:342-375` — `POST /v1/artifacts/{artifact_id}/versions`
  - `services/csp/app/api/artifacts.py:249-266` — `POST /v1/artifact-jobs`
  - `services/csp/app/modules/artifacts/service.py:74-88` — `resolve_owner`

- **可達路徑**
  `require_service_caller` → `_resolve_service_token`(`artifacts.py:83`)→
  `agent_credential_service.verify_service_token`,該函式**同時比對 `service_clients`
  與 `agent_credentials`**(`agent_credential_service.py:134-145`),所以任何一把
  agent `csk-` 都能通過這個 gate。通過之後,三支寫入端點的 handler **完全沒有
  再引用 `caller`**——沒有任何一行檢查這個呼叫者跟目標物件有關係。

- **具體情境**(三個,由輕到重)
  1. 持有任一 agent `csk-` 者 `PATCH /v1/artifact-jobs/{任意 job_id}`,
     翻掉別人 Studio 工作的狀態與進度。
  2. `POST /v1/artifacts/{任意 artifact_id}/versions` 對**別人的成品**追加一個版本,
     內容由攻擊者提供。讀取面回的是最新版 → 別人的報告內容被無聲替換。
     (密等有單向 latch,只能升不能降,所以這是**竄改**不是**降密**。)
  3. `POST /v1/artifact-jobs` 的 `requester_user_id` 是**呼叫者自填、零驗證**的
     (`resolve_owner:83-84` 直接 `return requester_user_id`),再用該 job 建 artifact
     → **在任意使用者名下植入一筆成品**。這正好命中規格 §8 的威脅模型:偽造紀錄。

- **為什麼這是縫而不是設計**
  同一個檔案的匯出面 `require_export_caller`(`artifacts.py:139-154`,拒收在 `:150`)**明確拒收**
  `kind == "agent"`,註解寫得一清二楚:「that would let any developer-issued agent
  token bypass `ensure_artifact_access`」。同一份威脅判斷做在匯出面,沒有做在
  相鄰的寫入面——這就是 commit `1a8633c` 說的那個形狀:「讀比對了作者,寫沒有」,
  只是這次發生在 service 面而不是 user 面。

- **為什麼是 MEDIUM 不是 HIGH**
  攻擊者必須先持有一把 service 憑證(`csk-`),那是開發者層級的信任,不是
  「任何登入者」。影響限於完整性(竄改/植入),不含讀取別人 artifact 內容,
  也不含降密。07-31 抓到的匯出缺口是「任何登入使用者」,那才是 HIGH 的形狀。

- **修法方向**(是方向不是解答)
  三件各自獨立、可以分開做:
  (a) `require_service_caller` 比照 `require_export_caller` 拒收 `kind == "agent"`;
  (b) 兩支 by-id 寫入面把目標物件的 owner 拿出來跟 caller 比對;
  (c) `POST /v1/artifact-jobs` 的 `requester_user_id` 必須能由 caller 身分推導,
      或至少限定只有 `service_client` 可以代填。
  ⚠ 做 (a) 之前先確認**現在有沒有 agent 走這條路**——全樹沒看到,但
  agent 是外部部署的,repo 看不到全部。

- **修法的持續維護成本**
  近乎零。三處都是「加一個既有謂詞的呼叫」,沒有新的設定、沒有新的資料表、
  沒有需要有人每月去看的東西。測試各加一條「錯的呼叫者被拒 + 對的呼叫者仍成功」。

---

### M-2 · MEDIUM · A05 設定錯誤 ＋ 靜默成功
**`.env.example` 出貨時就把開機安全檢查關著,部署腳本不檢查這一項**

- **位置**
  - `.env.example:27` — `ANILA_ALLOW_DEV_SECRET=1`
  - `infra/deployment/scripts/deploy-prod.sh:110-139` — `check_env`(沒有這一項)
  - `services/csp/app/services/startup_security.py:76-77, 112, 138-150, 217-241`
  - `infra/compose/platform.yml:81` — compose 端預設是 `0`(對的),被 `.env` 蓋掉

- **可達路徑**
  `platform.yml` 寫的是 `${ANILA_ALLOW_DEV_SECRET:-0}`,預設正確。但操作者拿到的
  範本 `.env.example` 第 27 行是 `1`,而部署流程就是「複製 `.env.example` → 填值」。
  `.env` 一旦帶著 `=1` 進 `.15`,`_is_dev_mode()` 回 True,三個開機守衛全部從
  「拒絕啟動」降級成「log 一行 warning」:
  - `assert_no_dev_defaults` — `admin/changeme`、dev `SECRET_KEY`、dev DB 密碼全放行;
  - `assert_audit_ledger_locked_down` — **P2.7 稽核帳防竄改姿態不完整也照樣開機**,
    連「查不出來」都放行(`startup_security.py:217-223`)。

- **具體情境**
  `.15` 首次部署。腳本 preflight 全綠(它只檢查 `CSP_SERVICE_TOKEN` /
  `INTERNAL_PLATFORM_API_KEY` / `CSP_SECRET_KEY` 有沒有值、像不像 `changeme`),
  容器全 healthy,五個入口都通,`/health` 200。**沒有任何一個綠燈是假的**——
  只是那三道守衛從此不再守。半年後稽核現場才發現稽核表的擁有權從來沒被拿走,
  而那正是規格 §8 要防的「admin 權限人員偷偷做假」。
  `deploy-prod.sh` 對 `ANILA_ENV` 沒設成 production 會 `warn`(:135-137),
  對這一項連 warn 都沒有——**兩個同類旗標,一個提醒一個不提醒**。

- **為什麼算「靜默成功」**
  這是本專案第一號禁忌的標準形狀:操作者做了部署、看不到任何錯誤、以為
  安全檢查有跑,實際上檢查全程只是在寫 log。`.15` **至今一次都沒部署過**,
  所以現在修的成本是零;等它上線再修,要動的是生產機的 `.env`。

- **修法方向**
  (a) `.env.example:27` 改成 `0`,把 `=1` 移到註解裡當本機開發的說明
     (第 14 行本來就已經有那行註解了);
  (b) `deploy-prod.sh` 的 `check_env` 加一條:`ANILA_ALLOW_DEV_SECRET=1` 時
     `fatal`(不是 warn——它關掉的是稽核防竄改)。

- **修法的持續維護成本**
  零。兩行改動,不新增任何需要長期照顧的東西。反向成本才是真的:
  不改的話,每次重灌 `.env` 都要有人記得這一行。

---

### M-3 · MEDIUM · A05 設定錯誤(結構性)
**`/uploads/` 是無認證靜態服務,用「黑名單」擋敏感目錄——下一個放錯的人不會收到任何錯誤**

- **位置**
  - `infra/nginx/anila.conf:544-546` — `location ^~ /uploads/ingestion/ { return 404; }`
  - `infra/nginx/anila.conf:548-553` — `location /uploads/ { alias …/uploads/; }`
  - (443 區塊 `:931-940` 有一模一樣的一份)
  - `infra/compose/platform.yml:328` — `../../share/uploads:/usr/share/nginx/share-files/uploads:rw`

- **可達路徑**
  `share/uploads/` 底下的任何檔案,只要不在 `ingestion/` 之下,
  **內網任何人**(不需登入、不需卡、不需 cookie)`GET /uploads/<路徑>` 就拿得到。
  目前樹裡只有 `flux/`(FLUX 生成圖,UUID 檔名)與被 404 擋掉的 `ingestion/`。

- **具體情境**
  今天沒有洩漏——`flux/` 是 UUID 檔名的能力式 URL,`try_files $uri =404` 也沒開目錄列表。
  問題在**形狀**:這是黑名單。哪天有人加一個 `share/uploads/reports/` 或
  `share/uploads/exports/`,那個目錄立刻全院可讀,**沒有任何錯誤訊息、沒有告警、
  沒有測試會紅**。這個陷阱已經害得團隊在兩個地方各寫過一次警告註解
  (`platform.yml:168-170`、`HANDOFF-2026-07-29.md:77`)——需要靠註解記住的規則,
  遲早會有人沒讀到。

- **為什麼列 MEDIUM**
  是照**失效模式**評的,不是照現況曝險評的:現況曝險接近 LOW。
  一個「做錯了完全無聲」的結構,在四級密等場域值得先翻過來。

- **修法方向**
  翻成白名單:`location ^~ /uploads/flux/ { alias …; }` 明確服務,
  其餘 `location /uploads/ { return 404; }`。要多開一個目錄時,人必須動 nginx 設定——
  那正是我們要的摩擦點。

- **修法的持續維護成本**
  一次性四行。之後**每新增一個公開目錄要多改一行 nginx**——這就是成本,
  而它同時就是防護本身。⚠ 改完必須 `up -d --force-recreate nginx`
  (bind-mount 單檔改動不會進容器,見 CLAUDE.md §4)。

---

### L-1 · LOW · A05 錯誤訊息洩漏內部資訊
**聊天路徑的 SSRF 502 會把上游模型端點主機名回給一般使用者,而相鄰的面已經有遮蔽 helper**

- **位置**
  - `services/csp/app/services/proxy/guard.py:30-34` — `detail=f"上游端點未通過出向安全驗證: {exc}"`
  - `services/csp/app/api/models.py:1373-1379` — 同形狀
  - 對照組:`services/csp/app/api/agents/health.py:130-139` — `_safe_ssrf_detail` **有**遮蔽

- **可達路徑**
  任何使用者送一則訊息 → `api/proxy.py:983` / `services/proxy/service.py:228,564`
  呼叫 `_guard_outbound` → `UnsafeEndpointError` 的訊息字串本身嵌著
  host 與解析出的 IP(`url_guard.py:390-394, 435-440`)→ 原樣包成 502 detail 回瀏覽器。

- **具體情境**
  平台刻意設計了「一般使用者看不到模型端點位址」這條政策
  (`endpoint_author_service.py:56-91`,`ENDPOINT_REDACTED = "<owner-only>"`),
  而且 agent 健康檢查面已經照做。聊天面沒照做:上游一出狀況,
  一般使用者就會在錯誤訊息裡看到 `aiagent2.ai.ncsist.org.tw` 或內網 IP。

- **為什麼只是 LOW**
  氣隙內網,那個 FQDN 對院內使用者本來就不是祕密;而且只在失敗態出現。
  列出來是因為它是**同一條政策在兩個面上不一致**,而這個專案已經被
  「一個面有守、兄弟面沒守」咬過五次。

- **修法方向 / 成本**
  把 `_safe_ssrf_detail` 從 `api/agents/health.py` 搬到共用位置,兩處改呼叫它。
  成本零,而且少一份重複邏輯。

---

### L-2 · LOW · A09 紀錄與監控失效
**服務啟動權杖走 query string,會被 nginx access log 明文記下**

- **位置**
  - `services/csp/app/modules/launch/service.py:74-79` — `build_launch_url` 把 token 接在 query
  - `services/csp/app/modules/launch/token.py:26` — TTL 10 分鐘
  - `infra/nginx/anila.conf` — `access_log off` 只出現在 6 個 location,`/gitlab/`、`/n8n`、`/codeserver` 都不在其中

- **可達路徑**
  使用者點開一個註冊服務 → CSP 簽一張 RS256 launch token(帶 `user_id`、
  `employee_id`、`roles`、`department_id`、`classification_level`)→ 附在
  `launch_url` 的 query → iframe `src`。若該服務是同源路徑(`/gitlab/`、`/n8n`),
  這個 URL 連同 token 會進入 nginx 的 combined access log。

- **具體情境**
  拿得到 nginx 日誌的人(維運者、或任何讀得到那個 volume 的容器),
  可以在 10 分鐘內把該 token 重放到**同一個服務**上,以那位使用者的身分進去。

- **既有的防護(所以不是 MEDIUM)**
  token 綁 `aud`(只能對那個服務用)、10 分鐘 TTL、**沒有 `type: "access"` claim**
  所以 `_load_user_from_payload`(`auth_service.py:70`)會拒絕它當 CSP session 用;
  iframe 設了 `referrerPolicy="no-referrer"`(`services.jsx:158`)所以不外洩到第三方。
  一人維運、氣隙、日誌讀者本來就是最高權限者。

- **修法方向 / 成本**
  兩個選項,成本差很多:(a) 什麼都不做,只在 runbook 記一句「nginx log 含
  launch token,輪替與存取比照憑證」——成本零;(b) 改成 POST + 一次性
  consume(`service_launches.consumed_at` 欄位已經留好了)——要動 CSP、
  nginx、每個註冊服務的接收端,**成本遠高於風險**,不建議。

---

### L-3 · LOW · A01 ＋ 靜默成功
**服務↔專案綁定 API 收得到、寫得進、稽核得了,但存取判定從來沒有讀它**

- **位置**
  - `services/csp/app/api/services.py:629-667` — `POST /{service_id}/project-bindings` 回 201 並寫稽核
  - `services/csp/app/services/access_control.py:23-27, 107` — 「Step 7 project membership check — MVP no-op pass」

- **可達路徑**
  admin 呼叫 `POST /api/services/{id}/project-bindings` → 201 + `service.project_bind`
  稽核事件。`can_access_service` 走完 step 1–6 後,**step 7 直接 `return True`**,
  完全沒有查 `ServiceProjectBinding`。

- **具體情境**
  管理者以為「把服務綁到專案 = 只有該專案成員看得到」,實際上綁定對
  誰能開這個服務**零影響**。這是本專案第一號禁忌的形狀,只是目前
  **治理 UI 沒有這個面**(`apps/csp-governance-ui/src` 全樹無 `project_binding`),
  所以要被騙到必須先走 `/docs`(admin-gated)自己打 API——所以是 LOW 不是 MEDIUM。

- **注意**:`access_control.py` 的 docstring 誠實地寫了「MVP no-op pass」,
  沒有假裝。問題在 **API 那一端沒有任何一句話告訴呼叫者這件事**。

- **修法方向 / 成本**
  三選一,由擁有者定(見文末 Q-4):真的接進 step 7 / 刪掉這組端點 /
  留著但在 response 與 docstring 明說「僅為 metadata,不影響存取」。
  第三個成本最低(一行字),而且立刻止血。

---

### L-4 · LOW · A01(資訊性,刻意設計)
**`/uploads/flux/<uuid>.png` 是無認證的能力式 URL**

- **位置**:`services/flux2-dev-agent/app/image_store.py`、`app/main.py:136`、
  `infra/nginx/anila.conf:548`
- **情境**:使用者用提示詞產的圖(內容可能帶密等)存在無認證路徑,
  只靠 UUID 不可猜。拿到連結的人(轉貼、瀏覽器歷史)不需登入就看得到。
- **這是已知且刻意的**:`HANDOFF-2026-07-29.md:77` 記載對話附件**刻意不放這裡**,
  正是因為知道這條路無認證。列在這裡只為了讓 P2.6 的清單完整,
  以及提醒 M-3 的白名單改動要記得把 `flux/` 留著。
- **不建議修**:要加認證就得讓 CSP 代理圖片位元組,那是為了低風險付高持續成本。

---

### L-5 · LOW · A05 ＋ 文案與實作不符
**服務 iframe 對同源服務而言不是沙箱,但畫面上寫著「於受限沙箱中執行」**

- **位置**
  - `apps/anila-shell/src/services.jsx:157` — `sandbox="allow-scripts allow-same-origin allow-forms"`
  - `apps/anila-shell/src/services.jsx:138` — 「內容於**受限沙箱**中執行」
- **技術事實**:`allow-scripts` + `allow-same-origin` 同時給,對**同源**內容
  等於沒有沙箱——被嵌的頁面拿得到真實 origin,可以直接讀寫 `parent.document`。
  平台自己就有三個同源服務路徑:`/gitlab/`、`/n8n`、`/codeserver`。
- **為什麼只是 LOW**:能在同源放任意 JS 的人(例如有 code-server 權限者)
  本來就已經能在平台主機上執行程式,沙箱不是那條鏈上最弱的一環。
  真正的問題是**那句安撫文案在同源情形下是假的**。
- **對照組做對了**:LLM 產出的 artifact 預覽用 `ARTIFACT_IFRAME_SANDBOX = ""`
  (`runtime/artifactDetect.js:84`),那是最嚴格的沙箱,而且有測試釘住
  (`__tests__/artifactPreview.test.jsx:69`)。
- **修法方向 / 成本**:跨源服務拿掉 `allow-same-origin`(它們不需要);
  同源服務把文案改成誠實的(「此服務與平台同源,請以對待平台本身的標準看待」)。
  成本:一行判斷 + 一行文案。

---

### L-6 · LOW · A01 列舉洩漏
**artifact 讀取面對外人回 403、對不存在回 404 —— id 存在與否可被列舉**

- **位置**:`services/csp/app/api/artifacts.py:505-522`
  (`_load_artifact_or_404` → 404;`ensure_artifact_access` 失敗 → 403)
- **情境**:artifact id 是連續整數。任何登入者從 1 掃到 N,就知道系統裡
  有幾筆成品、哪些 id 被用掉了。內容拿不到。
- **為什麼列出來**:commit `1a8633c` 把 conversations / agents / services /
  handoff cancel 的 403 全部收斂成 404 就是為了關掉這個 oracle,
  **artifact 是同一批裡沒被收斂到的那一個**。
- **修法方向 / 成本**:`except PermissionError` 改回 404,措辭與「找不到此 artifact」一致。
  一行。⚠ 前端如果靠 403/404 分辨過,要一起看。

---

## 三、查過、確認乾淨的部分

以下每一項都是逐行追過、不是只 grep 過。列出來是為了讓下一次掃描知道
哪些地方不必重來。

**A02 加密與金鑰**
- JWT 一律 RS256,`ALGORITHM` 硬編碼(`utils/security.py:45`),`jwt.decode` 一律
  帶 `algorithms=["RS256"]` 白名單,`kid` 不符直接拒。`alg=none` / HS256 混淆
  在三個服務(csp / anila-studio / asr-gateway)都關死,而且
  `JWT_ALGORITHMS = ("RS256",)` 兩處預設值都對——**這點特別重要**,因為公鑰
  就掛在 `/.well-known/jwks.json`,只要有一處接受 HS256 就是完整的認證繞過。
- 卡登驗章是**真的驗**:CMS 簽章對 signedAttrs、`messageDigest == hash(eContent)`、
  憑證鏈逐層驗簽到釘死的 CSPKI root、效期檢查、nonce 反 replay
  (`services/card_auth.py:213-262`)。dev 測試 CA 有三重 production 圍欄
  (`:107-116, 446-476`),而且是整包拒收不是濾掉——這個選擇是對的。
- 密碼 bcrypt;service token 走加密信封 + `hmac.compare_digest`;
  解不開的信封 `continue`(fail-closed,`agent_credential_service.py:197, 253`)。

**A03 注入**
- 全樹沒有 f-string / 字串拼接的 SQL。唯二兩處插值都是安全的:
  migration `0026:135` 的索引名來自寫死的 tuple;
  `pgvector_store.py:96` 的 `SET LOCAL` 走 `int()`,而且建構子拒收非 int
  (連 `bool` 都擋,`:64-71`——這個細節做得很好)。
- 沒有 `os.system` / `shell=True` 在任何可達的 API 路徑上。
  `anila_core/tools/shell.py` 確實有 `create_subprocess_shell`,但
  `exec_bash` / `exec_python` 兩個 cap **預設都是 False**(`workspace/caps.py:44-45`),
  而且全樹沒有任何服務把這兩個工具接進 agent。**不是發現。**
- 路徑穿越:附件與 ingestion 的落地路徑都由 sha256 / DB 欄位推導,不吃使用者字串
  (`documents.py:211-218, 1043-1044`);SPA catch-all 有 `resolve()` + `relative_to()`
  雙重驗證(`main.py:342-359`)。

**A05 設定**
- `/docs` 與 `/openapi.json` 都掛 `Depends(require_admin)`(`main.py:286-310`),
  FastAPI 內建的 public 版本已用 `docs_url=None, openapi_url=None` 關掉。
  `config.py:10-12` 還特地刪掉了一個從來沒被讀取的 `ENABLE_API_DOCS` 旗標——
  正是為了不讓人以為自己關掉了 docs。這個判斷很對。
- CORS 是明確白名單、無 `*` fallback,空值時 `allow_credentials` 也跟著關
  (`main.py:238-257`)。
- `DEBUG` 預設 False 且只餵給 SQLAlchemy `echo`,沒有進 FastAPI。
- 只有 nginx(80/443/4443)與 DB(**綁 127.0.0.1**)對外開埠;
  csp / router / studio / asr 全部只在 docker 網路內。因此
  `X-Real-IP` 偽造需要先進到 docker 網路內 → `utils/client_ip.py` 的
  「優先 X-Real-IP、否則取 XFF **最後**一跳」在這個拓撲下是正確的。
- 安全標頭齊全:HSTS / CSP / X-Frame-Options / nosniff / Referrer-Policy /
  Permissions-Policy,兩個 server 區塊都有。

**A06 相依**
- Python 相依**釘死到 patch level 並附 CVE 理由註解**
  (`services/csp/requirements.txt`:starlette 顯式釘 0.49.3、python-jose 3.5.0、
  python-multipart 0.0.32、pytest-asyncio 刻意不升並寫明原因)。這比多數有 CI
  的專案做得還細。
- 三個前端 app 的 `package-lock.json` 都在版控裡。

**A07 身分與認證**
- 撤銷有效:access token 帶 `tv`,每次請求比對 `user.token_version`
  (`auth_service.py:87-91`);anila-studio 與 asr-gateway 的撤銷快取
  **不可用時回 503 而不是放行**(`anila-studio/app/auth.py:118-126`、
  `asr-gateway/app/auth.py`)——這是正確的 fail-closed。
- **沒有找到任何 fail-OPEN 分支在認證路徑上。** 唯一的 opt-out
  (`ANILA_ALLOW_NO_SERVICE_TOKEN`)預設關閉、名字寫著它在做什麼、
  而且在 agent 側不在平台側(見 Q-5)。
- CSRF double-submit 正確:`hmac.compare_digest` 比對、豁免清單只有
  「本來就沒有 cookie 可劫持」的路徑、Bearer 請求豁免的推理成立
  (瀏覽器不會自動帶 Authorization,而 CORS 是白名單)。
  card-only 模式還會把 SameSite 升到 `Strict`(`middleware/cookies.py:46-51`)。
- Token **不進 localStorage**:三個 app 都是 cookie-only,`anilalm` 甚至在
  開機時主動清掉舊的 localStorage 殘留(`store/auth.ts:29-30`)。
- WebSocket 認證只吃 header / cookie,**不吃 query string**
  (`asr-gateway/app/auth.py:76-83`)——避免 token 進日誌。

**A09 日誌**
- 全樹沒有把 token / 密碼 / API key 的值寫進 log 的地方
  (`auto_seed.py:472` 記的是 key 的**名稱**)。
- 稽核帳有 P2.7 的日級雜湊鏈,而且開機會驗擁有權與權限
  (`startup_security.py:153-248`)——`assert_audit_ledger_locked_down` 連
  「查不出來」都在 production 拒絕啟動,這個設計很對(受 M-2 影響)。

**A10 SSRF**
- 守衛是**兩層**:註冊時(models / agents / ingestion credentials)＋
  呼叫時(`proxy/guard.py`、`health_checker.py`、`memory_service.py`),
  後者明確為了 DNS rebinding / TOCTOU。
- 檢查了幾個常見繞法,都堵住:scheme 檢查在 trusted-host 繞道**之前**
  (`url_guard.py:336-348` 註解明寫);loopback / link-local / metadata
  **不受任何旗標影響**;唯二兩處 `follow_redirects` 都是 `False`
  (`manifest.py:43`、`health_checker.py:525`)——重導向繞過不成立。
- `_trusted_hosts` 的 provider 例外處理是**降級到 env-only 繼續驗**,
  不是跳過驗證(`url_guard.py:178-187`)。

**祕密外洩(PUBLIC repo)**
- 全樹掃過 `sk-` / `csk-` / `bsk-` / PEM 私鑰 / JWT 字面值:
  **零個真實祕密**。兩個命中都是測試 placeholder。
- `.gitignore` 涵蓋 `.env`(含 `.bak` / `.save` 變體)、`*.pem`、`*.key`、`secrets/`,
  並用 `!` 明確放行那份公開的 `cspki_ca_bundle.pem`。
- `.env.example` 的值全是 `<openssl rand …>` 型的 placeholder,
  而且 `startup_security.py:39-46` 會把這些 placeholder 字面值本身當成
  offender 擋掉——「忘了 replace」不會靜默上線。
- 註解裡的員工編號都是假的,並且有一行註解明寫「這是 PUBLIC repo,
  真人的員工編號不進註解」(`config.py:194`)。

**前端 XSS**
- 只有兩個 `dangerouslySetInnerHTML`,兩個都有正確防護:
  `anilalm/MarkdownPreview.tsx:144` 走 marked → **DOMPurify**(還額外 hook
  硬化 `<a>` 的 `rel`);`anila-shell/markdown.jsx:217` 是 mermaid 在
  `securityLevel:"strict"` 下的輸出。
- `apps/csp-governance-ui/src` 全樹**零個 `v-html`**。
- `anila-shell` 用 react-markdown 且**沒有裝 `rehype-raw`**,所以 LLM 回答裡的
  原始 HTML 一律被跳脫。

**A01 抽查(未重做 166 端點普查)**
- 全樹 295 條路由自動列舉,逐一比對授權相依;所有「看起來沒 gate」的
  都追進去確認過用的是自訂謂詞(`_require_developer_or_admin`、
  `resolve_search_principal`、`require_service_caller`…)。
  真正無認證的只有:`/health`、`/api/banners/public`(刻意,給待核准者看)、
  `/.well-known/jwks.json`(刻意,公鑰)、SPA catch-all——**全部正確**。
- 用 AST 找出「綁了身分卻在函式主體從未引用」的 by-id 端點,共 12 支;
  逐一確認 11 支是 `require_admin` 已在相依層完成判定(正確),
  剩下 1 支是 M-1。
- `agents/runtime_config.py:67-88` 的 PATCH 已經退場成 410,
  而且註解明寫「Accepting admin edits while nothing applies them was a
  silent no-op」——這是修對的。
- `docs/FAKE-CONTROLS.md` 的第 1、2 號(PII 遮罩文案、agent
  `classification_ceiling`)**在主樹都已經修掉**:文案改成誠實的
  (`trust.jsx:178, 204`),`classification_ceiling` 改成明確拒收
  (`registration.py:114-128`)。那份文件在這兩條上已經過時(往好的方向)。

---

## 四、沒有涵蓋到的部分,以及原因

1. **執行期驗證**。本次是唯讀靜態掃描,依交辦不動容器 / DB / alembic。
   **沒有任何一條發現是用真實請求打出來確認的**——M-1 的三個情境是從程式
   路徑推導的,修之前建議先用一把 `csk-` 實打一次。
2. **相依套件的 CVE 現況**。這棵樹沒有 CI,掃描環境也不該對外連網。
   釘版策略看得出來有人在追,但**版本新不代表今天沒有新 CVE**。
   建議由人在有網路的機器上跑:
   - `pip-audit -r services/csp/requirements.txt`(以及其餘 5 份 pyproject/requirements)
   - `npm audit --omit=dev`(三個 `apps/*`)
   跑完把結果貼回這份報告的附錄,才算 A06 真的收掉。
3. **sibling worktree 正在施工的部分**(P2.1 agent 身分、附件解析器)。
   只掃主樹;那邊如果已經修掉某條,這裡看不到。
4. **166 端點普查沒有重做**(commit `1a8633c`,2026-07-31)。
   本次是抽查該次之後的變更 ＋ 用 AST 補該次結構上覆蓋不到的角度
   (「綁了身分卻沒用」)。M-1 就是這個角度撈出來的。
5. **範圍外的服務**:`services/pptx-renderer`、`services/flux2-dev`、
   `services/asr-decoder`、`cht/`(mock 讀卡機,dev-only)、
   `myCSPPlatform/`、`scraps/`、n8n workflow 檔。
6. **migration 內的 RLS policy 是否真的擋得住**。看了宣告
   (`0037`、`0039` 有 `ENABLE` + `FORCE ROW LEVEL SECURITY`),
   但**沒有實際對 DB 跑跨 collection 的讀取測試**——那需要動 DB。
   `services/csp/tests/test_memory_scope_pg.py` 等 PG 測試可能已經蓋住,
   建議確認那幾支有在跑。
7. **agent 端的實際部署姿態**。agent 跑在別人的機器上,
   repo 看不到它們的 `.env`(見 Q-5)。

---

## 五、需要擁有者裁決的事項

> 這一節每一題都是**權衡**不是**缺陷**——缺陷已經寫在上面,照著修就好。
> 每題都可以用一句話回答。

**Q-1(對應 M-1)** — `/v1` artifact 寫入面要不要比照匯出面拒收 agent `csk-`?
> 匯出面已經拒了,理由是「不能讓開發者發的 agent token 繞過擁有權檢查」。
> 同樣理由套在寫入面成立,代價是**如果將來有 agent 要自己寫 artifact,得改回來**。
> 一句話回答:「拒收 / 不拒收,改用物件層擁有權檢查就好」。

**Q-2(對應 M-1)** — `POST /v1/artifact-jobs` 的 `requester_user_id` 該由誰決定?
> 現在是呼叫者自填、零驗證,等於「服務可以代任何人建工作」。
> 這是 Studio 代客下單的必要能力,還是應該限定只有 `service_client`(非 agent)可以代填?
> 一句話回答:「只有 service_client 可代填 / 維持現狀 / 完全禁止代填」。

**Q-3(對應 M-2)** — `.env.example` 要不要改成 `ANILA_ALLOW_DEV_SECRET=0`,
並讓 `deploy-prod.sh` 在看到 `=1` 時**拒絕部署**?
> 代價:本機開發者複製 `.env.example` 之後要多改一行才跑得起來。
> 收益:`.15` 不可能在稽核防竄改關著的狀態下悄悄上線。
> 一句話回答:「改 / 不改」。

**Q-4(對應 L-3)** — `service_project_bindings` 要**接進存取判定**、**刪掉**,
還是**留著但明說它只是 metadata**?
> 「收了就丟比沒有更糟」是 FAKE-CONTROLS 對同類問題的原話。
> 第三個選項成本最低(改一行 docstring + response 加一句說明)。
> 一句話回答:「接進去 / 刪掉 / 標成 metadata」。

**Q-5(新提,對應第三方 agent)** — 平台要不要在註冊 agent 時**不帶 token 探測一次**,
只要對方回 200 就拒絕註冊?
> 背景:`ANILA_ALLOW_NO_SERVICE_TOKEN=1` 是 agent 側的旗標,平台管不到。
> 開發者若在內網 agent 上設了它,那個 agent 就變成無認證端點——
> 任何人知道 URL 就能派工,並藉該 agent 的 `csk-` 讀到它綁定的知識庫。
> 現在的 `test-connection` 只判斷「路徑對不對、憑證有沒有被接受」
> (`api/agents/health.py:44-52`),**判斷不出對方根本不檢查憑證**。
> 代價:多一次探測(一個 HTTP 請求),以及「開發者要先設好 token 才註冊得成功」的摩擦。
> 一句話回答:「加這個檢查 / 不加,由開發者自負」。

---

## 六、給下一次掃描的備忘

- 這份報告的自動化部分(295 條路由列舉、「綁了身分卻沒用」的 AST 檢查)
  是一次性腳本,**沒有留在樹裡**。如果覺得有用,值得做成
  `infra/checks/` 底下的一支——那裡已經有 `check_orm_pg_drift.py` 之類的同伴。
  成本:一支約 60 行的腳本,加進 `run-all.sh`。
- 反覆出現的形狀是「**一個面守了、兄弟面沒守**」:
  1a8633c 抓到 5 次(user 面),本次 M-1 是第 6 次(service 面),
  L-1 是第 7 次(錯誤訊息遮蔽)。下次掃描可以直接從這個形狀開始找:
  **凡是同一個資源有兩個以上入口的,把它們的授權判定並排比對**。

# CLAUDE.md — ANILA 重啟樹(給 Claude Code)

> **這是重啟樹**:2026-07-28 平台擁有者決定回到 redesign 收斂點 `a4118a3`(2026-07-03)重新出發。
> 權威文件就在本樹根目錄:**規格＝`SYSTEM-MAP.md`**(14 章,`## 0`～`## 13`;
> 檔頭的 Q1–Q28 是「依平台擁有者逐題確認重建」的**來歷**,不是章節結構)、
> **順序＝`PLAN.md`**(到 8 月底上線)、
> **歷史＝`RESTART-FROM-REDESIGN.md`**(373 commit 履歷＋attic 取回方式)。
> 🟣 **2026-08-17 擁有者裁定:`PLAN.md` 是專案權威**——其餘文件指向它,不與它競爭。
> **分工要記牢:`SYSTEM-MAP.md` 是規格(系統「應該」長什麼樣);
> `PLAN.md` 是現況與工作順序(現在「是」什麼、下一步做什麼)。兩者不互相取代。**
> 📌 **接手先讀 `docs/HANDOFF-2026-08-15.md`（最新）＋ `docs/HANDOFF-2026-08-11.md`**
> （後者仍有效，兩道交付閘門、九段順序、驗收心法在那份；`-08-10` 與 `-08-07` 談的是
> 兩道交付閘門的來龍去脈與院內規章檢索的長期照顧事項）；
> ⚠ **這一行標「最新」的檔名每加一份 HANDOFF 就會過期一次**——2026-08-17 實際發現它指著
> `-08-11`，而 `-08-15` 早就存在。**寫「最新」就是寫一個保證會過期的宣稱。**
> 以 `ls docs/HANDOFF-*.md | sort | tail -1` 為準，別信這一行的檔名。
> 擁有者要看的是 `docs/TOMORROW.md`。
> 🟢 **設定頁只剩 12 顆**（2026-08-11，Q46）。新增設定前先過這一關：
> **一顆值要留在設定頁，必須答得出：上線之後，誰、在什麼情境、為什麼不能等下一次改版。**
> 那 84 顆被砍掉的，正是兩個 CRITICAL 的根源——**能從網頁動到的安全開關，就是一個等著被動的安全開關。**
> 🔴 **凍結前的硬閘，而且是兩道獨立的關**：① **雜物掃描**（`scan-image-artifacts.sh`）；
> ② **映像要真的載得回來**才算數。**兩道關互相看不見對方漏掉的東西**——
> **每次「重建映像」的收貨都要把這兩步都跑一次**。
> **`docker build` 成功不等於映像出得了門。**
> ⚠ **2026-08-15 修訂（機制已變，事故仍為真）**：舊版寫的是「`docker save` 2/7 張存不出去」。
> `6e68f931`（2026-08-14，已在 HEAD 祖先）之後，**本專案自己 build 的映像不再走 `docker save`**：
> `docker buildx bake --set <target>.output=type=docker,dest=<tar>` **由 builder 直出 tar**，
> 掃 tar，再**真的 `docker load` 驗回來**（`build-and-export-for-intranet.sh`，五段式 `[1/5]`～`[5/5]`）。
> **只有上游映像（pg／redis／nginx／gitlab）與選配 model 映像仍走 `docker save`。**
> 📌 **兩個歷史數字留著，因為它們是真的**：2026-08-10 實測**雜物掃描 65 筆違規／4 張映像**、
> **`docker save` 2/7 張存不出去**（本機 DCS 代理注入，掃描器結構上看不見）。
> **過時的是機制描述，不是那次事故**——那次事故正是現在這道「載得回來才算數」的由來。
> 🔎 **驗收單一定要有這一句**：「去找這一包自己有沒有長出它要消滅的那個形狀」。
> 2026-08-06 六包，**六包全中**，而且全部是驗收抓的——命中率比逐條檢查驗收條件還高。
> 🔬 **宣稱「測試過了」之前先跑突變檢查**：`cd apps/anila-shell && node scripts/mutation-check.mjs`。
> 這個專案六輪獨立驗收，**每一輪都找到「改一行讓功能整個死掉、而套件全綠」的位置**；
> 舊套件只抓得到 11/22。⚠ csp 那邊還沒有同樣的工具。
> ⚠ **執行順序以 `PLAN.md` 開頭那段〈九段〉為準**（擁有者 08-03 裁定，不趕上線、兩週內做完）。
> ⚠ **`AGENTS.md` 是 2026-06-22 版,大幅過時**——讀碼以 SYSTEM-MAP 與現行程式碼為準,別照它辦事。
> 舊資料夾 `~/桌面/ANILA/anila-migration-20260706/ANILA`(main＋attic/2026-07-28/*)只當**參照**,不在上面開發。
> 回覆一律**繁體中文＋台灣用語**(禁簡體、禁大陸用語)。

---

## -1. 跨專案制度(playbooks)

派工／驗收／完成判準在 `~/.claude/playbooks/`(symlink,真身＝`~/claude-harness-kit/`;10 調度、20 判斷、30 派工模板、40 維護、90 信)。路由表在全域 `~/.claude/CLAUDE.md`:**派 subagent 前讀 10、宣稱完成前讀 20**。與本檔衝突時專案檔優先。工作流編組(grok 主力、Claude 審查、sol 二票、kimi-k3 三票)照《10 §3b–3c》,不在此重複。

---

## 0. 一句話定位

ANILA = 中科院(NCSIST)**院內內網(air-gapped)** 的 NotebookLM 式平台,PKI 自然人憑證卡登入。
(⚠ 用語:擁有者已兩度糾正「不要再寫軍方」——這是中科院內部用的平台。)目前**單一開發線 `restart/from-redesign`**(工作 worktree 分支除外);舊 4 分支模型已進 attic,**不要**在 PLAN 排到之前重建部署分支。Repo 是 **PUBLIC** → 祕密零外洩。目標:**8 月底全院上線**(PLAN.md),一人維運。

---

## 1. 內網機器拓撲(環境事實,與程式基底無關)

| 主機 | IP / 名稱 | 角色 |
|---|---|---|
| 平台主機 | `.15` = 10.53.100.15 / `anila.ai.ncsist.org.tw` | docker compose 全棧;**現為 redesign 前舊版,資料可刪、砍掉重來**(RESTART 文件 §四) |
| 模型 gateway | `.12` = 10.53.100.12 / `aiagent2.ai.ncsist.org.tw` | My-OpenAI-Frontend;`/v1` 出 gpt-oss-20b / gemma4 / nv-embed,需 Bearer key |
| MLSteam | `aiops.ai.ncsist.org.tw` | anila-agent 跑在這的 Lab(**純 http NodePort**) |
| 本開發機 | `~/桌面/ANILA/anila-restart-20260729/ANILA` | 重啟樹寫碼處;舊 dev stack 已於 07-29 授權下線,埠 80/443/4443/5433 已釋出 |

- **csp 容器沒裝 `curl`** → 測內部端點用 `docker exec <csp> python3 -c "import httpx; ..."`(curl 回空＝假陰性)。

## 2. 內網 TLS / CSPKI(踩過大坑)

- 內網 https 走 **CSPKI**:`CSPKI Root CA G1`(自簽 root)→ 中繼 → `*.ai.ncsist.org.tw` leaf;`.12` 只送 leaf。
- **`services/csp/app/services/cspki_ca_bundle.pem`**(卡登驗章那份)= csp 信任 `.12` 所需完整鏈,可直接當 `ANILA_MODEL_CA_FILE`。
- ⚠ **`SSL_CERT_FILE`(=`ANILA_MODEL_CA_FILE`)是「取代」整個信任庫,非疊加**——指到空/壞檔,csp 所有出向 https 全掛。
- 先 `openssl s_client -connect host:443 -CAfile X` 驗到 `verify return code: 0` 再接進服務。
- **`.12` 一律用 FQDN**(SSRF 私網 guard＋憑證主機名);compose `extra_hosts` 解析;`ANILA_TRUSTED_HOSTS` 放行。**`.12` 本身走 https(CSPKI),不需 http 旗標**;P0.2 的 `ANILA_ALLOW_HTTP_ENDPOINT` 是為其他純 http 模型端點(PLAN 0.2)。
- **aiops agent 是純 http** → endpoint 填 `http://...` 並設 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1`;填 https → `WRONG_VERSION_NUMBER`。
- 平台門面:`server.pfx`(空密碼)抽 `server.crt`＋`server.key`;用 IP 連跳憑證警告是正常。

## 3. 運維 footgun

- **`docker restart` ≠ recreate**(不重載 `.env`/compose);套設定一律 `up -d`。
- csp 啟動要求 secrets 非 dev 預設值(`startup_security.py`),缺 JWT keypair → JWKS 500、登入炸;keypair 產到 `./secrets`(compose 掛 `:ro`)。
- 本機起正式 compose 用**獨立 project 名**(如 `-p anila-restart`),避免撞舊 stack 的網路/volume 名。

## 4. 當前快照(2026-08-01 凌晨,會變,動前核對)

**細節看 `PLAN.md` 的〈現在在哪裡〉、教訓看 `docs/HANDOFF-2026-08-01.md`**——這裡只留每次 session 開頭必須知道的。

- alembic head = **`r1_0031`**。本機 `-p anila-restart` **15 容器**。入口現況(2026-08-17 實測):
  `/`、`/anila/`、`/asr/health`、`/router/health` **四個通**;
  🔴 **`/anilalm/` 回 503 是刻意的發行閘,不是故障**——本版關閉 ANILA LM(`6554fdd9`),
  nginx `anila.conf:450-467` 註解掉那段 location、回「尚未開放」頁,重開程序見
  `docs/runbooks/anilalm-release-gate.md`。**容器仍在跑,供內部測試。**
  ⚠ **舊版這一行寫「五個入口都通」(08-01 寫的,關閉之前)**——2026-08-17 一次重建驗收
  就因為照抄它而把刻意的閘判成故障。**驗收條件抄自快照時,先確認那一行的日期。**
- 測試——🔴 **判準是規矩,不是數字**:**三個套件都必須被跑**(csp、anila-shell、
  治理中心 `apps/csp-governance-ui`),而且**每一條紅都必須有歸屬**(真缺陷／環境／既有,
  各自指名)。**不要求全綠,要求沒有一條紅是無主的。**
  ⚠ **數字會漂,規矩不會**——下面的數字是用來**偵測漂移**的,不是用來當通過條件的。
  **合併會新增測試的包之後,要回來更新它們。**
  當期數字:csp **2538 passed / 73 skipped**(2026-08-11 連跑三次一致)、
  anila-shell **366 passed**(08-11)、治理中心 **144 tests / 143 passed / 1 failed**
  (**2026-08-17 合併兩包後實測**,`npm ci` 之後跑 `npm test`)。
  📌 **治理中心那個數字三天內就漂了 5 條**(08-15 寫 139/138/1 → 08-17 實測 144/143/1),
  原因不是筆誤,是**任何新增測試的包都會讓它過期**——這正是判準要放在規矩上的理由。
  🔴 **那 1 條紅是真缺陷,不是雜訊**:`tests/healthOverview.test.mjs:190` 的裸 `data.detail` ratchet,
  指著 `views/DashboardView.vue:254`(`25cdcb4f`,2026-07-31 進來,**紅了 15 天沒人看見**)。**歸 F-9。**
  ⚠ **沒裝 `node_modules` 時會多一條假紅**(`tests/testConnectionFacts.test.mjs` 載不到 `vue`)——那是環境不是缺陷。
  📌 **這一行 2026-08-15 之前只列兩個套件,治理中心從未進基線**——**沒被宣告的套件不會被跑,
  沒被跑的守衛等於不存在**。F-1 能活兩週,這是第二個原因。
  ⚠ 那個「26 個紅燈」的舊基準是**錯的數字**,2026-07-31 已修好並釘住(見 `services/csp/tests/README.md`)。
- **7/30–8/01 共合併部署 73 包**。P4 全關、P2 只剩 2.1,P3 除 SMTP 寄送外全關。
- ⚠ **`.15` 尚未部署過任何一項**。本機是唯一驗證環境,**P5.5 整段未開始**。
- **待裁決集中在 `docs/OWNER-QUESTIONS.md`**(17 題,**未答的排在最前面**),**不要重問**。
  規則:遇到需要裁決的事**不停下來等**,記進去、用最保守假設繼續、註明假設。
- **氣隙防護盤點報告在 `~/anila-private-audits/`,刻意不在 repo**(寫了尚未修補的弱點位置,repo 是 PUBLIC)。
- 其他文件:`docs/FAKE-CONTROLS.md`(30 項)、`docs/UX-IDEAS.md`、`docs/designs/`、
  `docs/TOMORROW.md`(給擁有者的清單)。

### 本機新增的環境事實(2026-07-31)

- **語音輸入活體可用**:`cht/` mock 讀卡機 + `asr` profile。
  `up -d` **必須帶 CPU overlay**,否則 nvidia driver 錯誤會中斷整批啟動:
  `docker compose -p anila-restart -f compose.yaml -f infra/compose/asr-cpu.yml --profile asr up -d`
- **卡登本機是開的**(mock 讀卡機 + 執行時生成的測試 CA,`secrets/dev-card-ca/`,gitignored)。
  `.env` 的 CARD_* 鍵照**初裝章 §3 替換清單**辦理(`docs/runbooks/first-install-rehearsal.md`):
  信任測試 CA 類**絕不可帶**;`CARD_INITIAL_OWNERS` **必須帶、換真員編**。
- **GPU 沒接進 Docker**(`nvidia-container-toolkit` 未裝),語音跑 CPU `small`,中文有同音錯字。
- **`.well-known` 經 nginx 是 403**——`location ~ /\.` 的隱藏檔規則誤傷,不是政策。P2.1 的前置。

### 這台機器的操作陷阱(每一條都今晚踩過)

- **`docker compose up -d` 之後要 reload nginx**(已寫進 `deploy-prod.sh`)。upstream 區塊 DNS
  只在載入設定時解析一次,recreate 任何服務都會讓 nginx 打舊 IP → **全站 502 但容器全綠**。
- **治理中心在 csp 映像裡**(`infra/docker/csp.Dockerfile` 多階段 build)。改治理中心要 **rebuild csp**,
  不是 anila-ui。⚠ 查部署路徑要**從跑著的容器往回追**,不要從 repo 裡看起來對的檔案往前推。
- **`npm run build` 過 ≠ 映像建得起來**。本機借用的 node_modules 有 devDeps,映像只裝 production。
  前端要合併前用 `docker compose build <service>` 驗;平行建置的錯誤訊息不會說是哪個服務。
- **新增 Dockerfile 的每個 `RUN` 都要以同一個 shell layer 的尾端清理 DCS 注入**：直接複製
  `rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid`，規則與原因見 `services/asr-decoder/Dockerfile:8-26`；
  guard 會掃 repo 內所有 Dockerfile，只有明列非交付用途的檔案例外。
- **平行派多包前先分配 migration 編號**,並把檔案集真的列出來對。一晚撞三次(兩次編號、一次同檔)。
- **驗 API 要看 Content-Type**。SPA catch-all 會回 `200 text/html`,看起來像端點沒有保護。
  🔴 **而且它會往哪個方向騙你,取決於你的腳本怎麼消費那個 body**(2026-08-15 稽核長實際踩到):
  拿去 `JSON.parse` 會**當場炸**,你會發現;拿去 `includes('某字串')` 只會**回 false**,
  於是「這條路由回了 200、但裡面沒有我要的資料」——**一個完全反過來的結論,而且沒有任何錯誤訊息**。
  **寫驗證腳本時要先斷言 Content-Type,不要靠「剛好用了會爆的解析方式」。**
- **改 bind-mount 的單一檔案,內容不會進到容器裡**。Docker 用 inode 綁定,而 git 改檔是「建新檔取代」
  (新 inode),容器還抓著舊的那個——**沒有任何錯誤訊息**,只是你的修改沒生效。
  nginx 設定就是這樣掛的(`infra/nginx/anila.conf` → 容器的 `default.conf`),
  改完要 `up -d --force-recreate nginx`,不是 reload。⚠ 也不要把檔案 `docker cp` 進 `conf.d/`,
  那會變成第二份設定檔然後 `resolver` 重複宣告 → 語法檢查失敗(2026-07-31 踩過)。
- ⚠ **nginx 容器的健康檢查原本是 `nginx -t`**——那只驗設定檔語法,所以 nginx 在狂噴 502 時
  照樣顯示 healthy。已於 2026-07-31 改成真的發 HTTP 請求。**這是今晚三次「容器全綠但使用者
  進不來」裡最根本的那一個。**

### 這個專案最貴的四條教訓

1. **不要把系統越搞越嚴。** 上一輪 25 天工作被整包放棄,直接原因就是安全限制越加越複雜。
   擁有者原話:「過於嚴格我也不會想維護,開發者不會想掛 agent,使用者也不想用。」
   加限制前先問:擋掉的是誰／被擋的人怎麼自救／**它會不會在未來擋住我們自己**。
2. **驗證要驗行為。** 只有眼睛能判斷的就渲染出來看(浮水印深色主題的結論與審查猜測相反);
   「有呼叫 close()」不算證明,要量到連線真的回到池子;
   **把 production 改動還原回去還會過的測試,等於不存在**(今晚抓到三條)。
3. **審查者給的修法方向是假設不是解答。** 派修訂輪傳**不變式與驗收情境**;
   同一缺陷兩輪關不掉就問「這兩個目標是不是互斥」。
4. **靜默成功比報錯危險。** 26 個「按了、沒報錯、什麼也沒發生」的控制項,
   其中兩個是使用者以為鎖住了存取但沒有。**任何讓使用者以為發生了什麼的控制項,
   都要確認後端真的收得到那個意圖。**

## 5. 鐵則

- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/` 不進追蹤;容器掛載不看 gitignore,新增 RW 掛載要想清楚。
- **端到端驗證用對方法**:別只看 status code(SPA catch-all 回 200 text/html → 驗 Content-Type);取資料走正式 API＋auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗 `npm run build`(非只 tsc);後端 pytest(本樹尚無 venv,暫借舊樹 `services/csp/.venv` 的直譯器＋`PYTHONPATH=packages/*/src`,建好自己的 venv 後改用)。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化(P0.2 的 http 旗標分域是擁有者拍板的例外,紀錄在 PLAN.md);改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時。

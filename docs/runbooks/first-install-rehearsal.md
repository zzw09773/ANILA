# 第 8 段演練 — 初裝章

> **這一章不是新的部署程序。** 權威程序是 `docs/runbooks/intranet-deployment-runbook.md`
> （以下簡稱 **runbook**）。這一章只做三件事：
> ① 規定**清除範圍與作答方式**（不做，初裝步驟根本沒被行使）；
> ② 點名**五件初裝必要步驟**（單位／密碼／keypair／CSPKI／指定主路由模型），
> 前四件的共同點是**漏掉不會報錯**，第五件會報錯但**沒有它，第一個問題就失敗**；
> ③ 列出**本機與 `.15` 的差異**，避免把演練環境的值帶進內網。
>
> 🔴 **演練的執行方式：逐字複製 runbook 的指令去跑。**
> 撞到失效 → **修 runbook 原文**，不要在這一章補註解。
> 兩份文件講同一件事，就會漂成兩份都不可信（`AGENTS.md` 是前例）。
> 這條規矩的由來：上一次演練「通了」，是因為做了一個**文件沒寫的動作**。

---

## 0. 這一章要證明什麼

**不是「平台起得來」，而是「照著文件、從零、由一個沒有本機記憶的人，能把平台裝起來」。**

差別在於：本機已經跑著一套。很多初裝步驟**因為產物早就存在而被跳過**，
跳過時腳本印的是綠色的「已有 ⋯⋯」，看起來跟成功一模一樣。

### 🔴 本機演練有一個天花板，而且那是**設計決定的**

從零的 `.env` ＝ **正式姿態**。正式姿態下有些東西**在本機必然驗不到**，
因為擋住它們的正是我們要的那些防護：

| 驗不到的 | 擋住它的東西 | 這是好消息因為 |
|---|---|---|
| 卡片登入 | 卡登驗章**釘死 CSPKI**，mock 假卡的測試 CA 不在信任 bundle 內 | 證明 `.15` 上不會有人拿假憑證進來 |
| 完整問答／檢索徽章 | 模型 gateway 在內網（`.12`），本機連不到；SSRF guard 也擋私網位址 | 證明平台不會被誘導去打任意內部位址 |

> ### 規矩：**凡是撞到這個天花板的驗收項，一律標「只能在 `.15` 驗」——不留白。**
>
> 留白會被下一手讀成**漏做**，然後不是白花時間重試，就是被當成缺口寫進報告。
> **標註才是事實。**

⚠ 這條跟本專案的總綱同構（見 `docs/HANDOFF-2026-08-11.md` 開頭）：
**把「沒發生的事」變成「明說的事」。**
沉默的成功危險，沉默的「沒驗」一樣危險——**兩者在紙面上長得都像已完成。**

📌 反過來也要成立：**如果某一項在本機竟然「過了」，要先懷疑組態被鬆綁了**，
而不是高興。本機能刷卡登入，就表示 `.env` 不是正式姿態。

---

## 1. 「從零」不是自動的 — 五個會被繞過的地方

`docker compose down -v` **只清 Docker volume**。初裝步驟的產物大多是
**host 檔案系統上的 bind mount**，`down -v` 之後原封不動。
而 `intranet-deploy.sh` 偵測到產物存在就會繞過那一步——**印綠字，看起來像做了**。

以下五類全部經 2026-08-14 現場定案。**做完這五件，「從零」才是真的。**

### 🔴 A 類：靜默跳過 — **演練前刪除**

| 產物 | 守衛 | 存在時的行為 |
|---|---|---|
| `secrets/jwt-private.pem`、`secrets/jwt-public.pem` | `intranet-deploy.sh:315` | 印 `ok "已有 JWT keypair (重跑沿用,token 不失效)"`，整步跳過 |
| `share/pki/model-ca.pem` | `:181` | 印 `ok "已有 … (沿用,不覆蓋)"` |

**處置：兩者演練前刪掉**（先備份）。

### 🟡 B 類：會問你 — 但**預設答案就是跳過**

> ## 🔴 從零安裝時，這兩題的正確答案與預設相反。

| 步驟 | 位置 | 問句 | 預設 | **從零安裝要答** |
|---|---|---|---|---|
| `[1/7]` TLS | `:145-147` | 「重新從 pfx 抽取?(會覆蓋)」 | `[y/**N**]` | **`y`** |
| `[3/7]` .env | `:196-199` | 「保留現有 secret?」 | `[**Y**/n]` | **`n`** |

⚠ **這一類比 A 類陰險**：它「有問過你」，所以事後看起來像操作者自己決定的。
**一路按 Enter，七把 secret 一把都不會重生，TLS 憑證也不會重抽。**

### ⚠ C 類：不是跳過，是**無聲降級** — 驗收要斷言那行環境變數

`intranet-deploy.sh:258`：CA 檔若是空的或不含憑證，它**不會停**，
而是把 `ANILA_MODEL_CA_FILE` 設成**空字串**、退回系統信任庫、印一行 warn 然後往下走。

🔴 **演練會「成功」，但走的是系統 CA——內網那條 CSPKI 鏈根本沒被驗到。**
而 `.15` 上系統 CA 不認得 CSPKI，**所以這個成功在內網不成立**。

> ### 驗收判準：**「部署跑完」不算成功。**
> 要斷言 `ANILA_MODEL_CA_FILE` 的值是 **`/etc/anila/pki/model-ca.pem`**，**不是空字串**。

（⚠ 查證時**不要**在容器內跑 `env` 或 `docker compose config | grep` —— 那會把真的 key 印出來。
用只取單一鍵的方式查。）

### 🔴 D 類：祕密**可能不是產生的，而是從包裡讀的**

`intranet-deploy.sh:207-212` 會從**映像包裡**讀 `intranet-defaults.env`
（**刻意不進 git，實體隨包帶入**），直接覆寫九個鍵：
`ADMIN_PASSWORD`、`CODESERVER_PASSWORD`、`CARD_INITIAL_OWNERS`、`GITLAB_ROOT_PASSWORD`、
`SECRET_KEY`、`CSP_SERVICE_TOKEN`、`CSP_DB_PASSWORD`、`CSP_APP_DB_PASSWORD`、
`INTERNAL_PLATFORM_API_KEY`。

**兩條路都可能發生，裝機的人必須知道自己走的是哪一條**：

| 情況 | secret 從哪來 | 怎麼分辨 | 要做什麼 |
|---|---|---|---|
| 包內**有** `intranet-defaults.env` | **檔案** | `[3/7]` 之前先看包根目錄有沒有這個檔 | **記錄該檔的 sha256**，並確認裡面的值是這次部署要用的（不是別批的殘留） |
| 包內**沒有** | **現場生成**（runbook §1.3） | 同上 | 七把 secret 產生後**立刻進密碼管理器** |

> 🟢 **2026-08-14 演練現場實測**：`~/anila-deliverables/export-buildx-20260813` 內**沒有**這個檔
> → **本次演練走的是「現場生成」那條**，七把 secret 的生成路徑會被真的行使到。

### 📌 E 類：`secrets/dev-card-ca/` — **本機保留，內網絕不可帶**

它不是 `intranet-deploy.sh` 產生的，是 `cht/` 那套 mock 讀卡機生成的
（`cht/docker-compose.yaml`，`CHT_DEV_CA_HOST_DIR=../secrets/dev-card-ca`）。

**留著不會讓任何一步跳過。** 本機保留是刻意的——**它是本機唯一能測卡登的路**，
而「mock 代真」在本專案是**明示的誠實邊界**，不是偷懶。

🔴 **但 `.15` 絕不可帶**：帶過去等於平台信任一個測試用的假 CA。
內網的卡登必須驗**真 CSPKI 鏈**。已列入 §3 內網替換清單。

### 🟢 已排除：DB 那一側不必處理

`auto_seed.py` 的冪等守衛都是 `if owner is None` / `if user is None` / `if model_id is None`
這種**查 DB 再決定**的形狀。`down -v` 清掉 volume → **這些全部會重新執行**，不會假綠。
alembic 同理。

### 演練前的清除（🟢 2026-08-14 已授權執行）

```bash
# 1. 平台停止並清 volume（資料歸零）
docker compose -p anila-restart down -v

# 2. A 類產物（先備份，見下）
rm -f secrets/jwt-private.pem secrets/jwt-public.pem
rm -f share/pki/model-ca.pem

# 3. .env：刪掉，照 runbook §2.2 步驟 3 重寫
rm -f .env
```

**備份位置（演練後要復原）**：
`~/anila-deliverables/rehearsal-backup-20260814`（DB dump ＋ env ＋ secrets）、
`~/anila-private-audits/rehearsal-env-20260814`（本機 `.env`）。

🟢 **不刪**：源碼、`.git/`、`docs/`、`infra/`、`secrets/dev-card-ca/`、`node_modules`。

---

## 2. 五件初裝必要步驟

前四件的共同點：**漏掉不會有錯誤訊息**，症狀出現在很後面、而且看起來像別的問題。
第五件（**指定主路由模型**）是 2026-08-14 演練當場撞出來的——它**會**報錯，
而且訊息還告訴你怎麼修，但**沒有它，從零安裝後的第一個問題必定失敗**。

### 2.1 單位（departments）

**做什麼**：owner 登入後到 `/departments` 建立單位清單。

**漏掉的症狀**：🔴 **三千人插卡全部卡在註冊**。
卡登驗章會過，然後畫面要求選單位，而下拉是空的——
提示寫「目前沒有可選的單位，需要管理員先建立。」（**提示是誠實的，不是假控制項**）。

**演練必撞**：`down -v` 之後 `departments` 是 0 筆（UI 掃描 F-3 實測）。

**順序**（這個順序才通，因為 owner 進場不依賴單位存在）：
1. owner 靠 `CARD_INITIAL_OWNERS` 的員編刷卡進入 → 不需要單位
2. owner 建立單位清單
3. 同事才開始首刷

⚠ **平台不會以零單位阻擋啟動**。部署健康檢查完成後會依本節顯示非阻斷提醒；
`startup_security.py` 擋了 `CARD_INITIAL_OWNERS` 還是預設值（那會造成「沒有人能核准 → 平台自鎖」），
但零單位仍須由 owner 依本節手動建立，提醒不會取代這個步驟。

> 🟢 **擁有者裁決（2026-08-14）：演練先建三個示範單位**即可，
> 全院正式清單**上線前另行提供**。（跟首頁連結清單同一批待補輸入。）

🔮 **Q53 會取代這一步，但還沒到**：擁有者已裁決部門改接 HR Oracle view（`csiih.vihbuy`），
註冊時即時查詢、每次卡登重查。⚠ **卡在行政**：唯讀帳號申請、防火牆開通、view 授權
（`docs/OWNER-QUESTIONS.md` Q53）。**上線走的仍是手動建立。**

### 2.2 密碼

**七把 secret** 照 runbook §1.3 產生（或來自包內檔案 — 見 §1 D 類，**先確認自己走哪條**）。

🔴 **最後一哩：`.env` 的 `ADMIN_PASSWORD` 只在「owner 帳號被建立的那一次」被消費。**
`auto_seed.py:114` 是在「帳號不存在才建立」的分支裡讀它的
（`hashed_password=hash_password(settings.ADMIN_PASSWORD)`）。

**意思是**：帳號建好之後再改 `.env` 的 `ADMIN_PASSWORD`，**DB 裡的密碼不會跟著變**，
而且**不會有任何錯誤訊息**。
👉 演練要驗的是「用文件寫的密碼真的登得進去」，不是「`.env` 裡有那個值」。

🔴 **code-server 的密碼＝平台最高權限，部署文件必須寫明。**
它掛的是**可寫的整包專案**、`secrets/` **沒有遮蔽**、`docker.sock` 也在裡面。
擁有者明白接受過這個設計（理由：只有平台管理員會有這個密碼），
**但接受不等於文件可以不寫**。詳見 runbook §5.1。

### 2.3 JWT keypair

**做什麼**：產出 `secrets/jwt-{private,public}.pem`，**必須在 compose `up` 之前**。

**漏掉的症狀**：`/.well-known/jwks.json` 回 **500** → **登入發不出 token**、
anila-studio 進 crash-loop。⚠ **不是「起不來」，是「起來了但登入炸」**。

🔴 **這一步在演練裡最容易假成功**（§1 A 類）：檔案存在就跳過並印綠色訊息。

⚠ 產生命令帶 `--user 0:0` **是必要的不是保險**——csp image 自 2026-08-06 起預設 uid 10001，
而 `secrets/` 這個 bind mount 屬於 host 帳號，不搶回 root 會在寫檔時 PermissionError。

### 2.4 CSPKI 清單

內網的 https 與卡片登入**是同一套 CSPKI CA**。盤點如下：

| 檔案 | 來源 | 去處 | 驗證 |
|---|---|---|---|
| `server.pfx` | My-OpenAI-Frontend repo 的 `nginx/cert/`（`.12` 上也有同一份） | 抽出 `infra/nginx/certs/server.{crt,key}` | subject 應為 `CN=*.ai.ncsist.org.tw` |
| `cspki_ca_bundle.pem` | **repo 內**：`services/csp/app/services/cspki_ca_bundle.pem` | `cp` 成 `share/pki/model-ca.pem` | `openssl s_client … -CAfile` → **`verify return code: 0`** |
| `secrets/dev-card-ca/` | 本機 `cht/` mock 讀卡機生成 | 🔴 **僅限本機，不進內網**（§1 E 類） | — |

⚠ **`ANILA_MODEL_CA_FILE` 是「取代」整個信任庫，不是疊加。**
指到空檔或壞檔 → **csp 所有出向 https 全掛**。先在 host 端驗到 `verify return code: 0` 再接進服務。

🔴 **驗收要斷言那行環境變數真的設到 `/etc/anila/pki/model-ca.pem`**（§1 C 類）——
它會無聲退回空字串，而部署照樣跑完。

### 2.5 指定「主路由模型」🔴 F-3 的雙胞胎

**做什麼**：到治理中心 `/models`，把一顆 LLM 指定為**主路由**。

**漏掉的症狀**：**平台的第一個問題就失敗**，畫面回：

> ANILA Router 無可用主路由模型。請管理員前往 CSP Models 頁面指定一個 LLM 為「主路由」。

> 🟢 **2026-08-14 演練實測**：`auto_seed` 確實把兩顆模型註冊進去了
> （`openai/gpt-oss-20b`＝LLM、`nvidia/nv-embed-v2`＝embedding），
> **但兩顆的 `is_primary_router` 都是 `false`**。
> **「模型有註冊」不等於「問得出東西」——這兩件事之間還有一個必須人工做的動作。**

⚠ **跟 F-3（零單位）是同一個家族**：從零安裝完成、所有容器健康、
所有指標都綠，然後**使用者做的第一件事就撞牆**。
差別是這一個**有誠實的錯誤訊息**（F-3 也有）——但**兩個都沒有守衛，也都不在原本的部署文件裡**。

> 🟢 **演練已實跑並確認可行**（2026-08-14）：走 owner 路徑
> `login → 取 CSRF → set-router-primary` → **200**。
> 這一步**做得到、而且只要做一次**——它缺的是**有人記得要做**，所以它在這裡。

⚠ **它的錯誤訊息本身是這份文件的一個正例**：訊息直接寫出「請管理員前往 CSP Models 頁面
指定一個 LLM 為主路由」——**照著做就能解決**。
（🔴 但它的**呈現**方式是缺陷，見 `docs/ui-sweep/2026-08-12-findings.md` 的 **F-7**：
那句話是包在原始 JSON 裡吐給使用者的。**修 F-7 時要保住這句話的內容，只改呈現。**）

📌 順帶（同一次實測，不是缺陷）：兩顆模型的 `health` 都是 `unhealthy`，
因為**本機連不到 `.12` 的模型 gateway**。`.15` 上這一格應該要是綠的，
**如果不是，先查的是 `extra_hosts` 與 `MODEL_GATEWAY_API_KEY`，不是平台**。

---

## 2.6 ⚠ 從零組態下，本機**沒有任何一條卡登路徑**（演練者必讀）

2026-08-14 實測：從零的 `.env`（＝正式組態）起來之後，
用 mock 讀卡機刷卡，卡片**偵測得到**（測試人員／員編 9999999／card #MOCKCARD00000001），
PIN 也收，但簽章送出後回：

> 憑證卡驗證失敗: 憑證鏈無法連到釘死的 CSPKI CA（issuer 不在信任 bundle 內）

🟢 **這是正確行為，而且是好消息**——它證明**卡登驗章真的釘死在 CSPKI**，
假 CA 過不了。這正是 `.15` 上必須成立的性質。

🔴 **但它的代價要寫清楚**：
**「從零的正式組態」與「本機唯一能測卡登的路（mock 假卡）」是互斥的。**
演練若要做卡登以外的任何 UI 驗證，**只能走破窗**（下節）。

### 破窗（owner 帳密）：實測有效，但**畫面上的入口沒出現**

| 層 | 實測結果 |
|---|---|
| **後端** | ✅ `POST /api/auth/login`（owner 帳號）→ **200**，三個 httpOnly cookie 正常發出，進得去、權限是 owner |
| **前端** | 🔴 `/login?show_alternatives=1` **沒有讓任何帳號密碼欄位出現** |

> ### 🔴 這一條上線前必須在**出貨映像**上重驗
> `docs/user-manual/admin.html` 的「破窗」那節教管理員加 `?show_alternatives=1`
> 然後用帳密登入。**在演練用的這個舊映像上，照著做會失敗。**
> 這個映像早於 UI-1 修復，所以**很可能新映像就是好的**——
> ⚠ **但「很可能」不能當驗證**。這是卡登壞掉時**唯一的回頭路**，
> 而且錯的時候，發現的人正好是進不去平台的那個人。

📌 釐清（我一度判錯，記在這裡免得下一手重犯）：
`/api/auth/providers` 回 `[]` **不代表沒有破窗**——那支是 **OIDC** 端點
（`services/csp/app/api/auth/oidc.py:109`），card-only 模式下本來就不列 OIDC。
密碼破窗活在 `POST /api/auth/login`，**owner 放行、其他人一律 404**（刻意不可區分）。

---

## 3. 本機 → `.15` 的替換清單（帶錯值會出事的）

| `.env` 鍵 / 檔案 | 本機 | `.15` |
|---|---|---|
| `secrets/dev-card-ca/` | 保留（唯一能測卡登的路） | 🔴 **絕不可帶** — 卡登必須驗真 CSPKI |
| `CARD_DEV_TRUST_TEST_CA` | 開著（信任測試 CA） | 🔴 **這個鍵不該存在** |
| `CARD_CA_BUNDLE_PATH` | 指向 `secrets/dev-card-ca/` | 指向真 CSPKI bundle |
| `CARD_INITIAL_OWNERS` | 測試員編 | **擁有者的真實員編**（填錯＝沒有人能核准，平台自鎖） |
| `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` | 本機為了測試而開 | 內網值照 runbook §2.3 |
| `MODEL_GATEWAY_API_KEY` | 本機值 | `.12` 上重新簽發（runbook §2.2b C） |
| 七把 secret | 本機值 | **全部重新產生**，不沿用 |

📌 文件漂移（follow-up）：`CLAUDE.md` 寫「卡登那**四個**變數一個都不能帶過去」，
本機 `.env` 實際有**五個** `CARD_*` 鍵，而且其中 `CARD_INITIAL_OWNERS`
**是必須帶過去的（換成真員編）**。這句話該按上表改寫。

---

## 4. 待補輸入

| # | 事項 | 狀態 |
|---|---|---|
| 1 | 全院**正式單位清單** | ⏳ 擁有者提供（演練先用三例） |
| 2 | 首頁連結清單（`seed.links` 已依裁決刪除，首次上線首頁是空的） | ⏳ 擁有者提供 |
| 3 | 帶進內網的 ref | 🟢 **已裁決：打 tag**（runbook §2.1 那句 `prod-intranet-card` checkout 是舊四分支模型，要一併改成 tag 制） |

### 演練順手驗的（不擴大範圍，撞到才記）

- runbook §4.3 的模型註冊期望值（`gemma4` / `image-generator` 不該出現）——
  auto_seed 的跳過行為 2026-08-14 動過，演練若會走到 §4.3 就一併對一下。
- runbook §1.2 那段模型搬運（2469 GiB / Google Drive）是舊拓撲，**本次演練不走**。

# 假控制項清單:按了、沒報錯、什麼也沒發生

> **這份清單只收「靜默成功」**——使用者做了某個動作、看不到任何錯誤、以為成功了,實際上沒有。
> 會報錯的 bug 不在這裡(報錯是誠實的)。
>
> **為什麼單獨立一份**:2026-07-30 一天內偶然撞到兩個(分享的「指定人」、「交給同事」),
> 兩個都已經上線一段時間、都沒有人發現。擁有者要求把其餘的**主動找出來**,
> 而不是等下一次偶然。三個角度平行掃:酬載對照 / 後端空轉 / 死控制項。
>
> **排序**:碰到分類、分享、權限、稽核的一律最前面,不管使用頻率——
> 「以為鎖了其實沒鎖」在四級密等的軍網場域是最貴的錯覺。
>
> 標記 ✅**已驗證** = 指揮官親自追過兩端程式碼;其餘為掃描回報,修之前請再確認。
>
> ⚠ **2026-08-01 P2.1**：agent 派工身分已改短效 JWT；下文若提到從 `.env` 拿掉
> `CSP_SERVICE_TOKEN`／舊版 token 汰換卡，屬當時假控制項紀錄，**勿當成現行 agent 上手步驟**
> （現行見 `docs/guides/developer-guide.md`）。

> 🔴 **2026-08-05 讀這份文件之前先讀這段。**
> 這份清單**曾經沒有把修復狀態回寫**，代價是 2026-08-05 開第 3 段時整整多跑一輪分診：
> 六條候選逐一追到兩端，**六條全部早就修好了**。
> 現在規則是：**條目一旦修好，就在那一條上標明，並附「現在讓它正確的那一行」的 file:line。**
> 不要只寫「已修（wt/某分支）」——分支會被刪掉，讀的人無從確認它有沒有進主線。
> 本輪的逐條回寫在文末〈第六輪〉。

---

## 最高:讓人以為資料被保護了

### 1. ✅ PII「遮罩」不會遮罩——原文照樣送進模型

- **畫面說**:`trust.jsx:178` 「送出時將自動遮罩」
- **實際**:`chat.jsx:1779` 送出的 `body` 是**原文**;`piiHits`(偵測到的位置)被
  `runtime/messageTree.js:53` 的註解明白標為 **client-only fields**——它從來沒離開瀏覽器。
  遮罩只發生在**你自己畫面上的顯示**。
- **三個模式的真相**:`block` 真的擋(`chat.jsx:1383`)、`warn` 真的警告、**`mask` 是假的**。
- **後果**:使用者打了身分證號、看到畫面上變成 `A12****789`、以為送出的是遮罩版,
  **完整號碼進了模型、也進了對話紀錄**。
- **修法方向**(不是解答):要嘛在送出前真的替換 `body`,要嘛把文案改成誠實的
  (「僅在本畫面遮蔽顯示,送出內容不變」)。**前者是功能、後者是一行字**,先做後者止血。

### 2. Agent `classification_ceiling`:收得到、回得出、**從來沒存進去**

- `api/agents/registration.py:153` 宣告在 schema、`:207` 在回應裡回傳,
  但**沒有任何一行把它寫進 Agent 物件**。
- ⚠ **報告把這條的嚴重度講高了,實測要修正**:密等防線**沒有**全面空轉——
  **模型**的 ceiling 寫得進去(`models.py:301` / `:1535`)也真的在擋(`proxy/ceiling.py:91`),
  而 `enforce_model_ceiling` 本來就只判模型。agent 真正在用的密等是 G9 的
  `default_classification_level`(有寫入、有 latch、今晚剛修過競態)。
  所以這是一個**收了就丟的殘留欄位**,不是破掉的防線。
- ⚠ **這條有指揮官的責任**:它是 OE-2 稽核早就記錄的 B2(「現況全樹無寫入,防線恆 no-op」),
  而 2026-07-30 派 OE-1 時明寫「保留 `classification_ceiling`,不要動它」——
  保住了欄位,卻沒要求補上寫入路徑。
- **修法方向**:要嘛補寫入並接進 enforce,要嘛**刪掉這個欄位**。
  收了就丟比沒有更糟,因為它讓人以為設定了。

### 3. 分享的 `mode` / `allow_fork`:寫進資料庫、零授權效果

隨 P4.3 已大幅改善(具名分享已上線),但這兩個欄位仍存而不用。**確認 P4.3 之後是否還有殘留**。

---

## 高:讓維運者以為系統是好的(假綠燈)

### 4. Agent「測試連線」:**不是 401 就算接對了**

- **這正是 2026-07-30「hi 沒有回覆」拖了那麼久才查出來的原因**:
  那台 gateway 先驗金鑰再看路由,所以連 `/v1/v1/models` 這種錯路徑也回 401。
  測試連線只證明「主機活著」,**證明不了路徑對**。
- 使用者拿到綠燈、以為設定好了,實際每一則訊息都 404。

### 5. Agent/模型「健康檢查」:命中 `/` 且 `<500` 就標 healthy

同一種假綠燈。探測清單裡有 `/`,而 `/` 幾乎永遠回東西。

### 6. `ENABLE_API_DOCS`:設定宣告可以關,**程式從來沒讀過它**

維運者以為自己關掉了 API 文件。(P2.3 已從另一條路真的關掉 router 的,但這個旗標本身是死的。)

### 7. 服務 `healthcheck_url`:UI 宣稱會探測,**零讀取**

### 8. Admin「執行設定」`runtime_config`:寫入回 200、宣稱 30 秒生效,**生產 agent 無人輪詢**
→ **已修**(wt/csk-cleanup):PATCH 維持 410;治理 UI 改唯讀並移除儲存/清除按鈕;
agent 端 `GET /me/runtime-config`（HISTORICAL: removed）與 `RuntimeConfigPoller` 一併移除(零生產呼叫點)。

### 8b. Agent 長效 `csk-`／`bsk-` 後端發行面(治理 UI 已拆、API 仍核發)

治理中心不再提供「核發 agent 憑證」按鈕,但後端仍接受
`issue-bootstrap`／`bootstrap`／`credentials/issue-static`／`rotate` 與
`GET .../credentials/me`(Tier-1 輪替偵測)。Q19 留下一顆低權限 agent 憑證的
兩個前提(runtime-config 自拉、撤銷清單)分別是「已拆」與「agent 端從未建、
且撤銷端點拒絕 `kind=agent`」——發行面成為無人消費的死路,卻仍能造出長效祕密。
→ **已修**(wt/csk-teardown Task 2+4):上列 agent 端點一律 **410**;
`anila-core agent bootstrap` exit 1;admin `GET/DELETE .../credentials` 保留清 orphan;
`POST /api/service-clients/{id}/issue-static` 不動(平台內部 s2s)。

---

## 中:日常會遇到,但不涉及安全

### 9. 對話**星號 / 資料夾 / 標籤**:只改前端 state,重載就沒了

程式碼註解自稱會寫進 `ui_settings`,實際沒接。這條**每天都會咬人**,而且修法便宜。

### 10. Handoff `accept`:改狀態＋發通知,**但不移交對話**(API 空殼)

### 11. Agent 註冊「版本」:UI 送 `version`,後端只認 `agent_version`,**靜默丟棄**

### 12. 服務註冊「網址」:前端送 `url`,schema 要 `entry_url` → **更新靜默丟棄**
(同一次請求裡的密等/角色**會**寫入,所以更像「存好了」)

### 13. 模型 `protocol`(`custom_adapter`):可存,proxy 不讀

### 14. Agent `capabilities` JSON:可編輯回存,執行路徑不閘

---

## 已知且刻意(不是缺陷)

- `shadow` 註冊旗標 / CLI draft:OE-1 收斂七態→三態時刻意忽略。

---

## 掃描範圍與信心

- **酬載對照**:粗掃約 55 個寫入點,確認 6、排除 49。
- **後端空轉**:確認 13。
- **死控制項**:第三輪掃描中,回來後追加。
- 三個角度都被要求**兩端都追到 file:line 才准報**,並先查 PLAN 不重報已排程項目。

## 建議的處理順序

1. **第 1 條先改文案**(一行字,今天就能止血),功能再排。
2. **第 2 條決定「補寫入」還是「刪欄位」**——不要留著。
3. **第 4、5 條一起修**:收緊成功條件。這兩條是「為什麼缺陷那麼晚才被發現」的答案,
   修它們等於提高之後所有問題的偵測率。
4. 第 9 條(星號/資料夾/標籤)雖然不涉安全,但**每天咬人且便宜**,值得早做。

---

## 第三輪(死控制項角度)追加 —— 2026-07-31 凌晨

確認 7 項、追進去後排除約 17 項(其中一項原本排第一名,讀了接收端才發現是誤判)。

### ✅ 已修:治理中心編權限——讀取失敗被吞掉,存檔變成靜默全撤 **(今晚最嚴重的一條)**

**沒有人做錯任何事就會發生。** 管理員點開「可用模型」,前端先清空、然後 `try { 讀取 } catch {}`
(空的 catch)。讀取失敗一次(逾時/500/網路抖動),清單停在空的,視窗照常打開、**每個框都沒勾**
——跟「這個人本來就沒有權限」在畫面上無法分辨。管理員勾了想加的那一個、按儲存,
後端**全量取代**(`users.py:295` 先 delete 再重建)、**再連帶把被撤掉的模型從該使用者所有 API key
移除**(`:304`)。畫面回報綠色成功,稽核看起來就是一次正常管理操作。
→ **已修**(`93465c5`):讀不到現況就不開視窗並說明原因。Agent 權限同一形狀,一併修。
→ 已確認其餘 `catch {}` 都在**輪詢迴圈**裡(下一輪會重試、不餵給寫入),那是正確的。

### 其餘六項(未修,依嚴重度)

| # | 事情 | 為什麼算靜默 |
|---|---|---|
| 1 | **服務登記編「網址」永遠不生效** | 前端送 `url`,schema 只有 `entry_url`,Pydantic 靜默丟棄;名稱/圖示/排序都存了,只有網址沒存。**同一個後端的舊相容門面 `platform_links.py:103` 有做這個轉換**,證明是漏做不是設計 |
| 2 | **對話的標籤/資料夾/星號從未送到後端** | 三個動作只改 React state(`app.jsx:898`),`Conversation` 模型根本沒有這些欄位。**資料夾清單本身有存,對話歸進哪個沒存**——重新登入後資料夾都在但全是空的 |
| 3 | **比較模式「採用此回答」只存在瀏覽器記憶體** | 建了一個純前端的假對話(`app.jsx:2112`),重整就沒。⚠ 加重:這段會把該對話標成 `classified`,等於**列管內容被採用進一個伺服器不知道存在的對話**,沒有 latch、沒有留存、沒有稽核 |
| 4 | **比較模式「重新產生」綁空函式** | `multiagent.jsx:146` `onRegenerate={() => {}}`,選單四個選項點了都沒事。一般聊天模式下這個 prop 是真的 |
| 5 | **儀表板抓取失敗時 KPI 顯示 0** | `|| 0` 落點,長得像「真的沒有流量」。註解說「錯誤會由 alert center 呈現」——**那句話是錯的**,攔截器只處理 401 |
| 6 | **舊版 token 汰換卡片永遠「載入中…」** | `v-else` 綁的是資料不是 loading flag,抓取失敗就永遠停住。⚠ 它是唯一告訴管理員「可不可以從 `.env` 拿掉 `CSP_SERVICE_TOKEN`」的畫面 |

### 另列(會報錯,不屬本類但等於功能不能用)

- **「新增服務」直接 422**:必填 `entry_url` 前端送成 `url`,**沒有人能從 UI 新增註冊服務**。與上表第 1 項同源。
- **API 金鑰頁「建立」用假理由鎖住自己**:讀取失敗 → 顯示「允許清單中沒有模型 · 請聯絡管理員」。
  **有理由的 disabled,但理由是錯的**——使用者去找管理員,管理員看到權限明明就在。
- 兩處空狀態指向不存在的路徑 `/admin/platform-links`(實際是 `/platform-links`)。


---

## 2026-07-31 凌晨 · 清理結果

**26 項裡有 13 項已處理。** 判準始終是同一句:**收了設定卻丟掉,比不提供這個設定更糟**——
設不了的人知道自己設不了,設了卻被忽略的人**以為自己受到保護**。

七項的清尾全部選擇「拿掉」而不是「接上去」,而且不是靜默忽略,是**明確拒絕**(422／410／501),
呼叫端會被告知:

| 項目 | 處置 |
|---|---|
| `ENABLE_API_DOCS` | 移除(宣告了但沒人讀,維運者會以為自己關掉了文件) |
| 服務 `healthcheck_url` | 移除;帶入即 422 |
| Admin `runtime_config` | 寫入路徑 410;治理 UI 改唯讀並拿掉儲存鈕;`GET /me` + poller 移除(沒有 agent 在輪詢) |
| Agent `classification_ceiling` | **只拿掉收了就丟的表面**(回應/序列化/UI,帶入 422);**DB 欄位、`enforce_agent_ceiling`、G9 全部不動** |
| Agent `capabilities` | 移除可編輯面,不加執行閘 |
| 模型 `custom_adapter` | 移除,下拉只留 `openai_compatible` |
| Handoff `accept` | ~~501 誠實拒絕~~ → **2026-07-31 真的做出來了**(見下) |

**另外三項今晚稍早已修**:PII 遮罩文案、治理中心權限靜默全撤、「交給同事」按鈕。

### 「交給同事」整條補回來(2026-07-31,擁有者答了 Q9)

原本是**兩段都假**:送出端沒有 `to_user_id`(交給「沒有人」),接受端只翻狀態不移交對話。
兩段一起補才有意義——只補送出端等於把同一個缺陷換個形狀。

- 新增 `GET /api/directory/users`(`services/csp/app/api/directory.py`):全院可查,
  回應模型只宣告 `id / username / department` 三個欄位。**不是**放寬 `GET /api/users`。
- `accept` 真的把 `conversations.user_id` 換成接收者,並自動幫原擁有者補一筆
  不過期的具名分享(`services/csp/app/services/handoff_transfer.py`)。
- 密／機密(> 營業秘密)的對話**建立與接受兩端都擋**,判準沿用具名分享的
  `outbound_action_allowed`。
- 收件人端有 UI 可以按(`collab.jsx` 的 `HandoffInbox`,掛在 `banners.jsx`)——
  沒有這塊,送出的請求一樣沒有人按得到「接受」。
- 行為測試在 `services/csp/tests/test_handoff_transfer.py` 與
  `apps/anila-shell/src/__tests__/handoffColleague.test.jsx`,每個 class 標了
  「改哪一行會轉紅」。

### 新增一項(壓力測試踩到,已修)

**`auto_seed` 覆寫管理員編輯**(OE-2 的 B3,早就排了):
`AUTO_REGISTER_MODELS` 清單裡的模型,端點在**每次開機時被無條件蓋回環境變數的值**——
三個地方都有同樣的覆寫(一般模型、agent 模型、agent registry)。
管理員在治理中心改了端點、看到成功、**下次部署悄悄變回去**。
壓力測試就是這樣被咬的:改過的 embedding 端點被重啟蓋回去,整輪 sweep 全 502。
→ 已修:env 只負責建立不存在的列,既有列歸管理員。

### 還沒處理

- 服務登記編網址／新增服務 → **已於 wt/govfix 修好**
- 對話星號／資料夾／標籤只在本機 → **已修**(`fb8d7a1`/`0c262d2`,每人一份 meta)
- 比較模式「採用此回答」→ **已修**(現在走伺服器,密等由伺服器決定)
- 比較模式「重新產生」綁空函式 → **已修**(`bdf739c`,沒有 handler 就不畫按鈕)
- 儀表板失敗顯示 0／永遠載入中 → **已修**

---

## 第二輪(2026-07-31 早)——又找到四個,其中一個是全新的形狀

前一輪自稱處理 13 項,實際是 **15 項以上**(上面兩條被記成「進行中／未處理」但其實做完了)。
反過來,以下四項是**漏掉的**:

### #27 ~~治理中心的「Router 主模型」根本沒接線~~ ✅ **已修，本節保留為紀錄**

> ⚠ **2026-08-05 更正:下面這段描述已經不成立,而且它在 08-05 害我對擁有者講錯話。**
> 擁有者說「我指定 Gemma,平台就會回答」——**擁有者是對的**。實測證據見下。

**當初的描述(已過時)**:模型頁可以把某個模型標成 `is_router_primary`,
router 完全不讀這個欄位,只讀環境變數 `MODEL`。管理員換了模型、看到成功、什麼也沒發生。

**現況(2026-08-05 實測)**:**接線了,而且會生效。** 兩套獨立機制指向同一支 CSP 端點:
`router_server.py:735-778` 的 `refresh_router_model`,以及 `main.py:227-234` 的 `_apply_primary`
(它直接改寫行程內的 `settings.model`)。解析點在 `router_server.py:723-726`——
**CSP 指定的名字優先於環境變數**,任一機制生效即可。

實測:用正式 admin API 把主路由從 `gemma26` 換成 `gemma26-nothink`,
trace 從 `Proxy -> gemma26` 變成 `Proxy -> gemma26-nothink`,`token_usage` 的 `model_id` 跟著換;
換回來,又變回去。環境變數 `MODEL` 只是**冷啟動的退路**,CSP 回答過一次之後就再也不用。

📌 **這一條最值得記的不是結論,是為什麼會誤判**:
`router_server.py:701-710` 有一段註解在**描述修好之前的狀態**,而它就寫在修正程式的正上方
(那是「為什麼要修」的理由)。**只讀那段註解、再看一眼容器的環境變數,就會得到完全相反的結論。**
→ **修好一個缺陷之後,講述舊狀態的註解要標明那是歷史**,否則它會被當成現況引用。

⚠ 順帶一個**沒過時**的事實:`is_router_primary` 同時是授權決定——
`api_key_service.py:125` 讓主路由模型對**每一個啟用中的使用者**開放,不需要逐列授權。
指定主路由不只是選路由,也是開權限。

### #28 API 金鑰頁的空 `catch{}` —— 跟「今晚最嚴重那個」是同一個形狀

`ApiKeysView.vue:196-199` 讀取失敗被空 catch 吞掉 → 畫面顯示
「允許清單中沒有模型 · 請聯絡管理員」(`:185`),**跟「這個人本來就沒有權限」長得一模一樣**。
治理權限那個修了,**這個沒有人記得**。同一個 bug 在第二個畫面上活著。

### #29 agent「版本」欄位兩端都壞

UI 送 `version`,後端 schema 只收 `agent_version` 且沒有 `extra="forbid"` → **靜默丟棄**;
回應回 `agent_version`,UI 讀 `agent.version` → 詳情頁永遠顯示「—」。
開發者填了版本、沒有任何錯誤、東西不見了。

### #30 兩個連到不存在路由的連結

`ServiceAccessView.vue:68`、`DashboardView.vue:147` 指向 `/admin/platform-links`,真實路由不是這個。

**#28–#30 已開工(wt/govclean),#27 等 core-opt 合併後隨路由那批一起修。**

---

## 第三輪(2026-08-03)——模型登錄的假控制與假紅燈

### #31 ✅ `api_version`（v1／v2）看起來像協定，其實只換 URL 路徑前綴

- **畫面說**：治理中心模型頁「API 版本」下拉 v1／v2，像在選通訊協定。
- **實際**：proxy 只把路徑改成 `/v1/...` 或 `/v2/...`（`proxy/service.py`、
  `proxy.py` 串流）；**送出的 body 與回應解析完全相同**。更糟的是維度探測
  曾硬編碼 `/v1/embeddings`，與 `api_version=v2` 的呼叫路徑對同一模型各說各話。
- **schema**：曾是任意 `str`，打錯字可靜默寫入，下游只有 v1／v2 有意義。
- **現況**：
  - 探測改走 `proxy_request`，與 live 路徑共用 `api_version`／`protocol`。
  - schema 收成 `v1`｜`v2`；治理 UI 標明「URL 路徑前綴，不是通訊協定」。
  - **保留 v2 選項**（庫內已有 `api_version='v2'` 列）；不把它重載成
    Triton——Triton 走獨立的 `protocol=triton_grpc`。

### #32 ✅ Triton gRPC 模型健康檢查永遠紅（假紅燈）

- **實際**：`probe_model_health_detailed` 對 gRPC port 發 httpx GET
  `/health`、`/v1/models`、`/` → 全失敗 → `unhealthy`＋告警「模型離線」，
  即使 Triton `ServerLive`／`ModelReady` 都正常。
- **後果**：工作中的模型長期顯示異常，訓練維運者忽略紅燈——比壞掉的模型更糟。
- **現況**：sweep／手動重測帶入 `protocol`＋`model_name`；`triton_grpc` 改探
  Triton gRPC（及 `grpc.health.v1`），不再對 gRPC port 做 HTTP 探測。

### #33 ✅ Triton gRPC 模型的「模型金鑰 · api key」欄位送不出去

- **畫面說**：`protocol=triton_grpc` 的模型登錄表單照樣顯示「模型金鑰 · api key」，
  輸入後存檔跳成功;列表列還標示「使用全域金鑰」。
- **實際**：Triton 路徑從不呼叫 `resolve_model_gateway_key` / `_apply_gateway_auth`
  （只有 HTTP 分支 `proxy/service.py:545` 會），`triton_grpc/client.py` 也沒有
  掛 call credentials 或 metadata。**一個 byte 都沒有送出去。**
- **後果**：管理員以為這個 Triton 端點有金鑰保護,實際上是裸的。
  這正是「使用者以為鎖住了存取但沒有」那一類。
- **現況**：`ModelsView.vue` 在該協定下**不顯示**金鑰欄位;先在別的協定下打過字
  再切協定的殘值會在 `buildModelPayload()` 丟掉;列表列改顯示「不使用金鑰」。
  端點提示同時說明本協定不送金鑰。

### #34 ✅ 公開 `/v1`、`/v2` embeddings 對外沒有 query／document 開關

- **畫面說**（對開發者）：CSP 的 embeddings 是 OpenAI 相容端點,送什麼進去就編碼什麼。
- **實際**：兩個路由都硬寫 `embedding_input_role="document"`,行程外呼叫端
  （含平台自己的 anila-agent SDK）**無法**表示自己是查詢側。對 Triton 類 embedder,
  查詢與文件走不同輸入張量 → 每一次 agent RAG 查詢都被當文件編碼,
  **不會報錯**,只是排序悄悄變差（實測 cosine 0.828 → 1.0）。
- **現況**：兩個路由接受 `input_type`（`query`／`document`,沿用 Cohere/Voyage/Jina
  慣例）,不帶維持 `document`,打錯字回 400 不靜默退回;該欄位不會轉送到上游。
  `anila_pgvector.search` 改帶 `query`;`memory/recall.py` 原本把
  `[query, *documents]` 併成一批送 —— 拆成兩次呼叫,一次 query 一次 document。

### #35 ⚠ `input_type` 送到 `nv-embed-proxy` 會被照單全收然後丟掉

- **文件說**:`packages/anila-agent/README.md:72`（`.en.md:76` 同）教人設
  `ANILA_EMBED_BASE_URL=http://nv-embed-proxy:8000/v1`。#34 之後,
  `memory/recall.py` 與 `retrieval/anila_pgvector.py` 都會在 body 裡帶
  `input_type`,看起來查詢側就有了。
- **實際**:那個 URL 指的是 **model 容器**,不是 CSP。
  `infra/models/src/embedding_proxy/app.py` 的 `EmbeddingRequest` 沒有宣告
  這個欄位,pydantic 預設 `extra="ignore"` → **不會 400,也不會被讀**;
  該 shim 對 Triton 一律送 `{"name": "documents", "shape": [1, N]}`。
  也就是說:欄位送得出去、不會壞,但查詢仍舊被當文件編碼。
- **後果**:照 README 設定的 agent,RAG 查詢的排序照舊悄悄變差,
  而呼叫端沒有任何訊號說它的 `input_type` 沒人理。
- **現況**:**尚未修**,先記在這裡。要真的拿到 query/document 分流,
  `ANILA_EMBED_BASE_URL` 要指向 **CSP** 的 `/v1`,且該 embedding model 在模型頁
  以 `protocol=triton_grpc` 註冊(runbook §3.1c)——那條路徑有測試把關
  (`services/csp/tests/test_triton_grpc_wire.py`)。
  沒有改 shim 的理由:`infra/models/` 是另一套獨立 build 的 model stack,
  本樹沒有任何測試會跑到它,改了也沒有人驗得到——那正是這份清單在收的東西。
  兩個 README 與 `recall.py` 的 docstring 已就地註明這個差異。

### #36 ✅ runbook 叫操作者調的 `EMBEDDING_TIMEOUT` 到不了容器

- **文件說**:runbook §3.1c 排錯表(以及 `triton_grpc/client.py` 兩則逾時錯誤
  訊息)叫操作者「調高 `EMBEDDING_TIMEOUT`」;`services/csp/.env.example` 也列著
  `EMBEDDING_TIMEOUT=30`。
- **實際**:`infra/compose/platform.yml` 的 csp 區塊沒有這一行,而整棵樹
  **沒有任何 `env_file:`** —— compose 只把「列舉出來的」環境變數放進容器,
  `.env` 本身不會整包灌進去。實測:`.env` 設了值 `docker compose config` 零命中,
  `docker exec anila-restart-csp-1 printenv EMBEDDING_TIMEOUT` 空、rc=1。
- **後果**:操作者照著 runbook 改 `.env`、`up -d csp`,**什麼都沒有改變**,
  而且沒有任何錯誤訊息 —— 502 照舊,他會以為是別的原因。
  這與 #34 之前 `ANILA_ALLOW_GRPC_ENDPOINT` 的形狀是同一個(旗標到不了容器),
  也是這份清單裡「按了沒反應」那一類最貴的變體:**指示本身是假的**。
- **現況**:`platform.yml` csp 區塊補上
  `EMBEDDING_TIMEOUT: "${EMBEDDING_TIMEOUT:-30}"`(預設值與 `Settings` 同),
  根目錄 `.env.example` 補上該鍵與說明,runbook 加了「調高 EMBEDDING_TIMEOUT」
  小節(含 `printenv` 驗證那一步)。
  `services/csp/tests/test_compose_csp_env_passthrough.py` 把「文件叫人去設的
  旋鈕 → csp 環境區塊有直通」整組釘住,免得第三次再犯。

---

## 第四輪(2026-08-05)——ASR 遠端化順帶清掉的一條

### #37 `ASR_ALLOW_HTTP_DECODER` —— 宣告了一個安全旗標,**沒有任何程式在讀**(已退役)

- **看起來是什麼**:`.env.example:218`、`infra/compose/platform.yml`、`infra/compose/dev.yml`
  都宣告 `ASR_ALLOW_HTTP_DECODER=0`,名字讀起來像「預設不准解碼端走純 http」。
  `services/asr-gateway/app/config.py` 也有對應欄位。
- **實際**:**零讀取**。`ASR_ALLOW_HTTP_DECODER` 在整個 repo 只出現在宣告處,
  沒有任何一行程式碼查詢它。`services/asr-gateway/tests/test_ws.py` 甚至有一條
  測試明白斷言「這個旗標不再擋任何東西」,而 `config.py:24-27` 的註解自承
  它是為了讓舊 .env 還能解析才留著。設成 0 的維運者會以為自己關掉了 http。
- **這一條的形狀**:不是「按了沒反應」,是**「以為鎖住了其實沒鎖」**——
  跟本清單最前面那一類同源,所以即使它只是一個環境變數也要記。
- **處置(2026-08-05)**:**拿掉**,不是接上去。http 的放行決定本來就已經由
  `ANILA_ALLOW_HTTP_ENDPOINT` 擁有(PLAN.md P0.2 拍板),ASR 沒有理由有第二個
  旗標——兩個旗標對同一件事表態,遲早會不一致,而不一致的那一天沒有人會發現。
  同一批把 asr-gateway 的解碼位址接進 `anila_core` 的 `validate_outbound_url`,
  所以「http 准不准」現在跟其他 model endpoint 走同一道門、同一個旗標。
- **舊 `.env` 留著那一行不會壞**(pydantic-settings 只對顯式 kwargs forbid extra)。
- **補登(同日,審查抓到)**:退役當下**文件沒清乾淨**。
  `services/asr-decoder/docker-compose.standalone.yml:14` 與
  `services/asr-decoder/README.md:61` 仍在教維運者「gateway 端設
  `ASR_ALLOW_HTTP_DECODER=1`」—— 而那兩個檔正是**遠端解碼端部署時唯一會被讀到
  的**。照著做的人得到的是「設了、沒報錯、gateway 照樣在啟動時拒收 http 位址」,
  也就是這一條原本要消滅的那個形狀。
  **教訓:退役一個假控制項,要把指向它的文件一起改掉才算退役完** —— 否則假控制項
  只是從程式碼搬進了文件,而文件比程式碼更難被測試抓到。兩處已改成
  `ANILA_ALLOW_HTTP_ENDPOINT=1`。

---

## 第五輪(2026-08-05)——專案入口磁貼的驗收帶出來的

### #38 ✅ 空的 `allowed_origins` 不是「不准」,是「全部都准」——launch token 直接送出去 ✅ 已修(2026-09-08)

- **畫面說**:治理中心的服務登記有一格 `allowed_origins`(iframe 來源允許清單)。
  管理員留白 → 直覺是「我沒有授權任何外部來源」。
- **實際**:`services/csp/app/api/services.py:185` 是

  ```python
  if allowed and origin not in allowed:   # ← allowed 是空的就整條跳過
  ```

  空清單是 falsy,**整個比對被略過**。於是一筆 `entry_url` 指向任意外部主機、
  `allowed_origins` 留白的服務,啟動時會把 **launch token 併進那個外部網址發出去**,
  一格檢查都不做。
- **實測(2026-08-05,本機 pytest)**:`entry_url="https://attacker.example/steal"`
  + `allowed_origins=[]` + `is_public=True` → `POST .../launch` 回 **200**,
  `launch_url = "https://attacker.example/steal?launch_token=eyJhbGciOiJSUzI1NiIs…"`。
- **誰會踩到**:只有**從資料庫/建立 API 生出來**的服務。env 種子那條路安全——
  `auto_seed.py:74,84` 對絕對 URL 一律回填 `allowed_origins=[origin]`。
  但建立端點 `services.py:280` 是 `data.get("allowed_origins") or []`,**預設就是空的**。
  → 管理員用治理中心新增一個外部服務,不填那格,就是這個狀態。
- **不是新的**:這一行與 `be969f99`(base)逐位元組相同,`wt/fix-launch-tiles` 沒有動它。
  記在這裡是因為它在該包的驗收中被翻出來,**免得下次有人當成新的迴歸再查一遍**。
- **修法方向**(不是解答,動之前要先問):把空清單當成「不准跨主機」是最直覺的,
  但那會**擋掉同源相對路徑**——同源那條路本來就靠「`allowed_origins` 空的時候放行」
  在走(見 `_validate_launch_entry_url` 的註解)。所以不能只把 `allowed and` 拿掉,
  要分域:同源(`_SAME_ORIGIN`)空清單放行,跨主機空清單則 fail closed。
  ⚠ 這會讓**現存**的、留白的跨主機登記全部停掉,是個 breaking change,
  要先盤點資料庫裡有幾筆、並且給管理員一條看得懂的錯誤訊息。

- **現在讓它正確的那一行**(2026-09-08):`services/csp/app/api/services.py` `_validate_entry_url_origins` — 跨主機空／過濾後空的 `allowed_origins` 在簽發 launch token 與寫 `service_launches` 之前回 400（`跨主機服務必須設定 allowed_origins`）。同源相對路徑空名單仍放行。建立／更新在 `entry_url` 或 `allowed_origins` 變更時用合併後狀態做同一道驗證；存量列不自動回填，launch 時大聲失敗。

### #39 設定壞掉的服務,對沒有授權的人回 400 而不是 404

- `services.py` 的 launch 端點裡,`_validate_launch_entry_url` 排在 `can_access_service`
  **之前**。所以一個 `entry_url` 壞掉的服務,對一個**根本沒有授權**的人會回
  `400 服務 entry_url 必須是 http(s) URL` ——同時洩漏「這個 id 存在」與「它設定壞了」,
  而正常的拒絕應該是與「id 不存在」同形的 404。
- **不是靜默成功**,嚴重度也低(要先有一筆壞掉的登記);記在這裡是因為它與
  #31 同屬 launch 端點的順序問題,而且 `services.py` 的 release gate 註解引用了這一條。
- **不是新的**:base 就是這個順序。`wt/fix-launch-tiles` 只把 release gate 與
  `is_active` 兩道移到驗證之前(修掉「停用+相對路徑回 400」那個),沒有動驗證本身的位置。
- **修法方向**:把 `_validate_launch_entry_url` 移到 access gate 之後、
  `create_service_launch` 之前。改動很小,但會動到既有測試對 400 的期待,要一起看。

---

## 第三輪(2026-08-05,知識庫升密那包順手記下)

### #40 知識庫升密**不會**清掉先前從它萃取的記憶 🆕

- **畫面/API 說**:知識庫詳情頁按「確認升密」→ 成功,庫與庫內文件全部升到新密等
  (`POST /api/ingestion/collections/{id}/classification`)。
- **實際上沒發生的事**:`apply_classification` 的記憶清除
  (`modules/policy/service.py`,P4.4)**只對 `resource_type == "conversation"` 生效**;
  `memory_service` 也**沒有知識庫的概念**——它掛在 conversation 上。
  所以在知識庫還是「無機密」的時候,從它的內容萃取進使用者長期記憶的片段,
  在知識庫升到「機密」之後**原封不動留著**,而且不帶新密等。
- **實測**:升一次知識庫,`memory_service` 的清除函式呼叫次數 = **0**(2026-08-05)。
  這一點就升密路由本身而言是**正確的**——那條路徑不該亂刪對話記憶;
  問題在於**沒有任何一條路徑**負責處理「知識庫升密 → 其衍生記憶」。
- **為什麼算假控制項**:升密是使用者用來「把這個知識庫關起來」的動作。
  他會合理地以為從它流出去的東西也一起被關起來了。沒有。
- **不在那包的範圍內**,刻意不順手改:`memory_service.py` 屬凍結檔,
  而且正確做法(清除?繼承密等?只標記?)是政策問題不是實作問題。
- **要決定的事**:知識庫升密時,對「該知識庫來源的記憶片段」應該
  (a) 刪除、(b) 跟著升密、還是 (c) 什麼都不做但在 UI 明講。
  在能回答之前,**不要**在 UI 上暗示升密會回收已外流的內容。
  → **2026-08-05 已進裁決冊 Q33**,現行假設 = (c)。

---

## 第六輪(2026-08-05 下午)——第 3 段開工前的分診回寫

### 一、六條舊候選:**全部已修**,不要再開包

逐條追到 UI 與後端**兩端**確認,並刻意不採信「說已修好」的註解。

| 舊條目 | 現在讓它正確的那一行 |
|---|---|
| **#3** 分享 `mode`／`allow_fork` 存而不用 | `services/csp/app/services/conversation_service.py:1638-1643` 建構子根本沒有這兩欄;`ShareCreate`／`ShareOut`(`api/conversations.py:229-253`)已移除;全樹無授權讀取點 |
| **#4** Agent 測試連線「不是 401 就算成功」 | `api/agents/health.py:60-110` 改回三項事實,401／403 明確**不算**成功;`DeveloperAgentsView.vue:404-415,513` 逐條渲染 |
| **#5** 健康檢查命中 `/` 就 healthy | `services/health_checker.py:73-80` 把 `/` 隔離成 WEAK path,`:191-205` 最多只能落到 `degraded`,**永不 healthy**;`utils/healthStatus.js:40,49` 顯示琥珀色「降級」,不塌縮回綠燈 |
| **#11／#29** Agent 版本欄位兩端不符 | `registration.py:229-233` 用 `AliasChoices("agent_version","version")` 兩拼法都收,且 `extra="forbid"`;UI 四處同名 |
| **#28** `ApiKeysView.vue` 空 `catch{}` | 換成 `utils/allowList.js` 三態模型,`:220` 守衛擋住「讀不到就送出」;後端 `api_keys.py:61` 更進一步——非 admin 的 `model_ids` 由**後端自算**,不採信前端 |
| **#30** `/admin/platform-links` 死連結 | 兩處都已改成 `/platform-links`,與 `router/index.js:80` 一致 |

同一輪對治理中心做了兩種形狀的主動掃描:**「空 catch 的結果被拿去組寫入酬載」比對了全部 view,
「欄位名不符被靜默丟棄」比對了 13 條寫入路徑 → 零發現**。
三處追到兩端後判定**不構成**該形狀,列出來免得日後重查:
`UsersView.vue:353-360`(寫入走的是另一條已加守衛的讀取)、
`PlatformLinksView.vue:337`(管理員 id 來自伺服器回傳物件,非 users 清單)、以及數個輪詢迴圈。

### #41 對話路由是**假綠燈**:探測打在一個不存在的路徑上 🔴

- **畫面說**:健康總覽的「對話路由」卡片是綠的。
- **實際**:`health_checker.py:500` 探的是 `http://router:9000/ready` → **404**。
  而 `_probe_http_service` 的規則是 `<500 → healthy`,所以 **404 被讀成「服務在答話」**。
  router 真正的健康端點是 `/health`,回 200 且帶著 `last_refresh_error` 這種有內容的欄位。
- **後果**:總覽**從來沒有問過 router 好不好**,只問了「你會不會回話」。
  router 內部壞掉(例如模型清單刷新一直失敗)而 FastAPI 還活著時,卡片照樣是綠的。
- **實測(2026-08-05,本機 `-p anila-restart`)**:`/ready` → 404 `{"detail":"Not Found"}`;
  `/health` → 200。

### #42 文件匯入工作者是**永久假紅燈**,而且會把整個總覽拖紅 🔴

- **實際**:`health_checker.py:501` 探 `http://ingestion-worker:8081/ready`,
  但它是 **Arq 佇列工作者,根本沒有 HTTP 伺服器**——`infra/compose/platform.yml:261-300`
  沒有宣告任何埠。實測 8081／8080／8000 全部 connection refused。
- **後果**:這張卡片**永遠 unhealthy**,而 `aggregate_health` 取「最差的那一個」,
  於是**整個健康總覽永遠是紅的**。
- 📌 **這一條最值得記的是**:`health_checker.py:518-523` 自己寫著
  「把『沒部署』跟『部署了但掛了』混成同一個紅點,卡片會天天喊狼,管理者三天後就不看了
  —— 那等於這個功能沒做。」**它的註解說中了它自己在做的事。**

### #43 反向代理探測被**今天自己的修正**打壞(迴歸)

- **實際**:port 80 的 Host 允許清單(今日合併 `be969f99`)結尾是
  `if ($is_anila_host = 0) { return 444; }`,而探測用的 `Host: nginx` 不在清單裡
  → nginx **直接關連線、不回任何回應** → httpx 丟 `RemoteProtocolError`。
  那個例外是 `ProtocolError` **不是** `NetworkError`,所以躲過
  `except (httpx.ConnectError, httpx.NetworkError)`,掉進通用 `except Exception`
  → 卡片標成 **unhealthy / probe_failed**。
- **確認是迴歸**:該合併的第一父 `de368194` 的 port-80 區塊是裸的 `return 301`,
  探測拿到 301 → healthy。**沒有任何測試抓到這個。**
- ⚠ **那個 nginx 修正本身是對的**(它封掉 open redirect:攻擊者控制的 `Host`
  會被原樣寫進 `Location`)。**要改的是探測,不是允許清單。**
- 📌 **順帶第三次踩到同一個形狀**:`health_checker.py:497-499` 的註解寫著
  「nginx :80 對所有路徑 `return 301`」——那是合併**之前**的狀態。
  Router 那次(#27)、ASR 旗標那次(#37)、這次,**都是被一句描述舊狀態的註解帶偏**。

### #44 anilalm:檢索失敗會**對模型說謊**,不只是對使用者靜默 🔴

- **實際**:`apps/anilalm/.../WSChat.tsx:334` 呼叫檢索,`:340-343` 把任何失敗吞成瀏覽器
  `console.warn` 然後繼續;`:199-210` 接著組出一段 prompt,**向模型斷言**
  「本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落」。
  檢索是**錯誤**而不是**空結果**的時候,**那句話是假的,而且是餵給模型當前提的**。
- **同形狀的還有三條**:anila-studio 五條產線裡的
  slides(`studio.py:580-585`)、datatable(`datatables.py:483-486`)、
  infographic(`infographics.py:537-542`)。
- **後果**:使用者拿到一個流暢、自信、**完全沒有依據**的答案,而且看起來跟有依據的一模一樣。
  在一個「就是要用院內文件回答」的平台上,這是最貴的一種——**因為沒有人會回報它**。
- **現成的正解就在同一棵樹裡**:報告產線兩條路徑都**大聲失敗**
  (`report_runner.py:487`、`:495`)且兩條都有測試(`test_report_runner.py:142`、`:162`);
  軟警告通道 `FALLBACK_DECK_WARNING` → `mark_done(warning=…)`(`studio.py:140`、`:770`)
  也已經接好而且有測試守著,只是沒接到檢索失敗上。
- ⚠ **`apps/anilalm` 完全沒有測試執行器**(`package.json` 沒有 `test` script、沒有 vitest)。

### #45 索引錯配 → **永遠回空,零筆日誌** 🔴

- **實際**:`pgvector_store.py:246` 以 `embedding_source_model = $4` 過濾。
  當平台指定的 embedding 模型與既有 chunk 當初索引用的不一致時,
  每一次搜尋都回 `200 {"results": []}`——**永遠,而且任何層級都沒有一行日誌**。
- **後果**:一個裝滿文件的知識庫變成一個什麼都答不出來的知識庫,而**沒有任何地方說得出為什麼**。
  觸發它不需要任何人犯錯——在治理中心改指定 embedding 模型是支援的操作。

### #46 `test_platform_embedding.py:211-256` 是**恆真測試**

- 它**從來沒有呼叫那個端點**:`:246` 自己手寫警告字串,然後斷言自己寫的字面值。
- **實測突變**:把真正的 `set_platform_embedding` 換成一個直接拋例外的函式,**6 條照樣通過**。
  刪掉 `models.py:1708` 不會讓任何東西轉紅。
- **這正是「測試全綠不是證據」在後端的樣本**,而 csp 目前**還沒有**前端那種突變檢查工具。

### #47 文件裡的 0.7 門檻:程式沒事,**教材有事**

- `api/search.py:177`(**會渲染進 OpenAPI schema,因此進到產生出來的 client**)
  與 `pgvector_store.py:219` 都把 0.7 講成一個合理的相似度門檻。
- **為什麼危險**:這個平台的 embedder 是**非對稱**的——同一句話的查詢側與文件側編碼
  實測 cosine 約 **0.60–0.83**,不是 1.0。所以 0.7 這個值**壓在「同一句話」的分數上**,
  設下去會**永遠濾掉每一段落**,而且看起來完全合理。
- 現行程式沒有任何門檻高於 0.3,**風險全在它教會下一個維護者什麼**。

### #48 評測頁:憑證讀取失敗會偽裝成「尚未註冊」(低) ✅ 已修

`EvaluatorView.vue:350` 的 `.catch(() => ({ data: [] }))` 讓讀取失敗長得像「尚未註冊 LLM 憑證」,
操作者於是略過評審直接送出評測。**減輕因素**:覆核面板 `:158` 仍誠實顯示「已停用」,
所以被誤導的是**原因**不是**結果**——與治理權限那次靜默全撤不同量級。**已修**。
失敗時現在顯示固定的繁中句子，後端 `detail` 只進 `title`，不取代使用者可見文字。

### #49 零 chunk 的文件仍標成 `indexed`(低,未修)

`handlers.py:797-803`(無測試)與影像說明嵌入失敗(`handlers.py:292-309`)兩條路徑
都在 WARNING 記了一筆,但最後**仍以 `status='indexed'` 收尾**。
一份實際上什麼都沒索引到的文件,在畫面上與正常文件無法分辨。

### #50 ~~csp 有**兩個 Dockerfile**,而「已改非 root」的那個是死的~~ ✅ **已修,本節保留為紀錄**

- **看起來是什麼**:`services/csp/Dockerfile` 有 `USER csp`。任何人翻 repo 都會得出
  「csp 已經降權跑」的結論。
- **實際**:compose 建的是 `infra/docker/csp.Dockerfile`,**沒有 `USER`**;
  實測跑著的容器 `uid=0`。三個部署中的映像(csp、ingestion-worker、pptx-renderer)都是 root。
- **這一條的形狀**:不是「按了沒反應」,是**「修正不在部署路徑上」**——
  比沒修更糟,因為它讓稽核和交接都以為這件事做完了。
- 📌 **CLAUDE.md 早就寫過這條**:「查部署路徑要**從跑著的容器往回追**,
  不要從 repo 裡看起來對的檔案往前推。」這是同一句話的第二次應驗
  (第一次是治理中心在 csp 映像裡,改了 anila-ui 沒有用)。
- **修的時候會踩到的兩件**(偵察已量):`main.py:60-67` 對 root 所有的 `/app/logs/csp.log`
  開 `RotatingFileHandler`,降權即 `PermissionError` 開不起來;
  `share/uploads/ingestion` 掛載內容已經是 `root:root`,只加一行 `USER` 會得到
  **容器全綠、上傳靜默 500**。要有一次性 `chown` 的部署步驟。

→ **已修(2026-08-06)**。現在讓它正確的那幾行:

| 讓它正確的那一行 | 做什麼 |
|---|---|
| `infra/docker/csp.Dockerfile:92-96` | `groupadd --gid 10001` + `useradd --uid 10001 --gid 10001` + `chown` `/app/logs` + `USER anila` |
| `services/ingestion-worker/Dockerfile:55-58` | 同一組 **10001:10001**(跟 csp 共用 `share/uploads/ingestion`,號碼不同就單向壞掉) |
| `services/pptx-renderer/Dockerfile:69-70` | `chown node:node $PPTX_TMP_DIR` + `USER node`(node 官方映像釘死的 1000:1000) |
| `infra/deployment/scripts/fix-runtime-ownership.sh` | 一次性、**冪等**的 host 端所有權對齊。**四個**掛載各自處理:兩個 RW 目錄 `chown -R` 給 10001;`secrets/` 走**白名單**(只放寬 JWT keypair 與 dev-card-ca bundle,其餘原封不動**並列印出來**);`share/pki` 遞迴放寬(公開 CA 憑證,讀不到 = 出向 https 全掛)。擁有者一律不動。映像不在時 warn 但 **exit 0**,不擋 up。 |
| `infra/deployment/scripts/deploy-prod.sh:278,293,328,363` | `fix_runtime_ownership()` 接進**所有會 (re)start 服務的路徑**:`deploy` / `up` / `rebuild`(`restart` 走 `up`)。漏掉 `rebuild` 曾是驗收抓到的 FAIL —— 那正是「把這包套到已在跑的部署上」最自然的命令。 |
| `infra/deployment/intranet/intranet-deploy.sh:323,334-337` | `[4b]` 產金鑰的 one-shot 補 `--user 0:0`(映像預設已非 root,不搶回 root 會 PermissionError);新增 `[4c]` 跑上面那支腳本 |
| `services/csp/app/utils/security.py:128-149` | 金鑰缺席的錯誤訊息不再叫人 `set ALLOW_AUTO_KEYGEN=true` —— 那件事在容器裡做不到(runtime user 非 root、`/app/secrets` 是 `:ro`),改指向真的能用的 host 端步驟。**指名一個做不到的補救,比不指名更糟**:操作者會拿整段停機時間去試它。 |
| `infra/compose/dev.yml:129-134,212-213` | dev 的 `share-dev/` 缺口:註解寫明第一次 `up` 會被 Docker 建成 root:root,以及那一行 root one-shot 的解法。**刻意不接腳本**(dev 沒有部署腳本可掛)。 |

- **死檔已刪**:`services/csp/Dockerfile` 不存在了。留著它就是留著下一次「改對了檔案但那個檔案沒人建」。
  遷移對照表 `docs/anila-redesign-docs/10-migration-and-development-guardrails.md:412` 原本還把它列成
  合法的 image 位置(那是**指示性**的,不是歷史敘述),已一併更正。
- ⚠ **這條留下的是一條口頭契約**:`10001` 同時寫在兩個 Dockerfile 與對齊腳本裡,三者必須相等,
  而**沒有任何機器檢查會發現它們漂開** —— 漂開的症狀是上傳單向壞掉,而兩個容器都 healthy。
- ⚠ **白名單要人維護**:以後任何新的「csp 要讀的 secrets 檔」,不加進 `fix-runtime-ownership.sh`
  就讀不到。這是刻意的取捨:代價是要記得加一行,換到的是不會把未來每一把私鑰都對 gid 10001 開讀。
  腳本每次都會把「刻意沒動的檔」印出來,所以漏掉時看得見。

### #57 掃描 PDF 的 OCR **永遠不會觸發**——佔位符自己撞破門檻(潛伏,打開就會以為生效) 🔴

- **看起來是什麼**:有一個 `PDF_OCR_FALLBACK` 旗標,打開之後純掃描的 PDF 應該會走 OCR。
- **實際**:`PdfParser` 每遇到一張圖就插一個 `[[IMAGE:<id>]]` 佔位符,**每個 24 字元**。
  而 `needs_ocr_fallback` 的門檻是「抽出來的文字少於 40 字」。
  **兩頁的純掃描 PDF 就有 48 字的佔位符 → 超過門檻 → 判定「有文字,不需要 OCR」。**
  同時它也繞過「抽不到文字就報錯」那條(#55 講的那個硬閘),於是文件被標成
  **已索引、而且零可檢索文字**。
- **現在是潛伏的**:`PDF_OCR_FALLBACK` 預設關閉,所以還沒有人踩到。
  ⚠ **但誰在 `.15` 把它打開,都會以為它生效了**——而兩頁以上的掃描件一份都不會進 OCR。
- **修法**:四行以內(門檻改成算「扣掉佔位符之後」的實際文字量)。
- 📌 這一條的形狀:**一個機制自己產生的副產物,把自己的觸發條件撐過門檻。**

### #58 DOCX 的**所有表格都被搬到文件最後** 🔴

- **實際**:`parser_registry.py:1037` 附近,**表格的迴圈跑在段落迴圈之後**,
  所以不管表格原本在文件的哪裡,解析出來一律接在全文尾端。
- **實測**:一份規範文件裡「各假別之上限如下表:」**底下沒有表**;
  那張假別時數表跑去跟文件最後的附件表單**黏在同一顆 leaf 裡**。
- **後果**:每一份**有表格的規範文件**都受影響,而且是現在就在發生。
  引導句與表格被拆開 → 問「加班上限幾小時」撈到的段落讀起來沒有上下文;
  而不相干的兩張表被黏在一起 → 撈到也分不清哪張是哪張。
- **這不是「按了沒反應」,是「解析出來的文件結構跟原件不一樣,而沒有任何地方說」。**
- **未來若要一併處理可讀性**,最小改動是一行(`builtins.py:389` 讓 leaf 內容前綴自己的
  `heading_path`),同時關掉這條與「附件標題不跟著表格走」兩者;
  代價是 leaf 變長且**要重跑 ingestion**,所以是「量到不夠再做」。

### #56 embedding 模型名稱的**大小寫碰撞**——資料缺陷,不是程式缺陷 🟡 寫入端已關,讀取端還有三條

> ✅ **2026-08-07 處置(第 1、2、3、6 條已關)**:寫入端一律存 `model_registry` 的拼法,
> 既有列由 migration `r1_0032` 就地改寫。**現在讓它正確的那幾行**:
> `services/csp/app/services/platform_embedding.py:130`(`canonical_embedding_model_name`,
> 恰好一種註冊拼法對得上才改寫,對不上或有兩種拼法就原樣存、不猜)、
> `services/csp/app/api/ingestion/collections.py:136`(建立集合時套用)、
> 同檔 `:145` 與 `platform_embedding.py:35`(最後手段預設值改成模型自己註冊的拼法)、
> `services/csp/migrations/versions/r1_0032_canonical_embedding_model_name.py`
> (拿掉 0014 的欄位預設值 ＋ 改寫四個欄位的既有列)。
> **候選只取 `model_type='embedding'` 的註冊列**;`is_active` **故意不濾**——已停用的 embedder
> 仍然是它產出的那些列的正確名字(見第 9 條)。這條不是潔癖:驗收實測過,只要多一列
> **已停用的聊天模型**叫 `NVIDIA/NV-Embed-V2`,不濾就會讓真正的 embedder 變成「有歧義」,
> **整批安靜跳過**——修正被一筆不相干的清冊資料繳械。
> **沒改的也會說**:每張表跑完會記一行 WARNING,講「拼法有歧義而不敢猜」與
> 「根本沒有對應 embedding 註冊列」各幾列。只記改了幾列,等於教操作者把沉默當乾淨。
> ⚠ **大小寫不敏感的比較要留著,不是死碼**:第 8 條沒關(`model_registry.name` 仍可
> 建出只差大小寫的兩列),而 r1_0032 只在**升級當下**對得上註冊表的列才改寫;
> 被它刻意留下的那些列(歧義／未註冊)也還靠那個比較。
> `packages/anila-core/src/anila_core/storage/adapters/pgvector_store.py:279` 的理由段
> 已同步改寫——它原本用「欄位預設值是 `nvidia/NV-embed-V2`」當理由,而那個預設值被 r1_0032 拿掉了。
> ⚠ **`r1_0032` 的資料改寫不可逆**——原本的大小寫沒有留在任何地方;`downgrade()` 只還原欄位預設值。
> ⚠ chunk 來源欄位在 `document_chunks` / `ingestion_images` 的 **FORCE RLS** 後面,
> migration 逐一集合設 `anila.collection_id` GUC 再改寫,**不假設 migration 角色是 superuser**
> (本專案目前是,但這正是 #52 的形狀,不值得賭)。
> 驗收測試 `services/csp/tests/test_embedding_model_canonical_pg.py` **刻意用
> NOSUPERUSER／NOBYPASSRLS 的擁有者角色跑這支 migration**——csp 現有 PG 測試都用 superuser DSN,
> 而 superuser 直接繞過 RLS,**所有跟 RLS 有關的突變在那種夾具下天生看不見**。

- **事實**:`ingestion_collections.embedding_model` 的 DB DEFAULT 是 `nvidia/NV-embed-V2`,
  而模型在 `model_registry` 註冊的名字是 `nvidia/nv-embed-v2`。
  migration `r1_0018` 又從那個欄位回填了 chunk 的來源模型欄位。
  → **一個索引完全正常的語料庫,會被大小寫敏感的比較判成「索引在別的模型下」。**
- **2026-08-05 的處置**:把三個比較改成大小寫不敏感
  (`similarity_search` 的查詢過濾、409 背後的 `EXISTS`、以及 `_collections_not_indexed_under`)。
  ⚠ **只修其中一個會更糟**——實測證明:只修偵測、不修查詢過濾,
  會讓正常語料庫變成 **200 筆空結果、零日誌**,也就是原本那個缺陷。
- ⚠ **這不是權宜之計,但它會讓資料缺陷變安靜。** 大小寫不敏感在兩個各自獨立寫入的
  自由文字欄位之間本來就是正確語意;但修完之後,**那個資料缺陷不再有任何徵兆**,
  而它每建一個用預設值的新集合就再犯一次。
- **所以下面這份清單必須被寫下來。** 沒有它,合併換到的是一個安靜的平台,
  而失去的是本來會發現這件事的那個人:

| # | 位置 | 事情 | 2026-08-07 |
|---|---|---|---|
| 1 | `services/csp/app/api/ingestion/collections.py:135` | 硬寫 `"nvidia/NV-embed-V2"` | ✅ 改成 `LAST_RESORT_EMBEDDING_MODEL`(`platform_embedding.py:35`) |
| 2 | `ingestion_collections.embedding_model` 的 DB DEFAULT | 同一個拼法,需要 migration | ✅ `r1_0032` **直接拿掉這個預設值**(ORM 本來就沒宣告,每條寫入路徑都給值;沒給值的寫入應該當場失敗而不是靜靜寫錯) |
| 3 | 建立集合的 payload(`collections.py:127`→`:158`) | `payload.embedding_model` **存進去前沒有任何正規化** | ✅ `collections.py:136` 過 `canonical_embedding_model_name` |
| 4 | `memory_service.py:323` | 記憶檢索,**仍大小寫敏感** | ⬜ 未動。r1_0032 把 `conversation_memory_chunks.embedding_source_model` 也一起正規化了,**但比較本身仍敏感** |
| 5 | `platform_embedding.py:201` 的 `count_pending_recompute`(⚠ 2026-08-15 更正行號,原記 `:149`) | **兩個缺陷在同一支**:大小寫敏感 ＋ 因 RLS 恆回 0(見 #52) | ⬜ 未動,兩個都還在 |
| 6 | `r1_0018` 的回填是**歷史資料** | 之後把欄位正規化**不會重寫已經寫進去的列**,要一支資料 migration | ✅ `r1_0032` 改寫四個欄位:collections、document_chunks、ingestion_images、conversation_memory_chunks |
| 7 | worker 寫 `embedding_source_model` 用的是**註冊表**的名字 | 舊 chunk 帶的是**集合欄位**的名字 → **同一個集合裡兩種拼法天生共存** | 🟡 舊 chunk 已由 r1_0032 對齊;但 worker 的最後手段預設值 `services/ingestion-worker/src/ingestion_worker/settings.py:45` **還是 `nvidia/NV-embed-V2`**,與 csp 這邊不一致(不在該包範圍,見下) |
| 8 | `model_registry.name` | **沒有唯一性也沒有正規化**——只差大小寫的兩列仍然建得出來,**正是原本那個缺陷的形狀** | ⬜ 未動——**這一條就是「大小寫不敏感比較不能拆」的理由** |
| 9 | **停用**指定的 embedding 模型 | 軟回退會移動 → 健康語料庫從 200 變 409(**刻意不關,見下**) | ⬜ 刻意不關 |

- 🔁 **復發途徑,仍然開著(2026-08-07 記)**:
  `services/ingestion-worker/src/ingestion_worker/settings.py:45` 的最後手段預設值
  **仍是 `nvidia/NV-embed-V2`**,而 csp 這邊已改成 `nvidia/nv-embed-v2`
  (`services/csp/app/services/platform_embedding.py:35`)。
  worker 只有在 `model_registry` **一列 embedding 都沒有**時才會落到這個值
  (`services/ingestion-worker/src/ingestion_worker/platform_embedding.py:82`),
  但**只要落到一次,那批新 chunk 就帶著錯的拼法**,r1_0032 已經跑完不會再回頭修。
  **兩邊的最後手段預設值要對齊**,這是一行的事;`services/ingestion-worker/**`
  不在本包範圍,所以留成工作項而不是留在會被歸檔的報告裡。
- 📌 **誰在寫 `embedding_source_model`(修這條之前要先知道的完整清單)**:
  `services/ingestion-worker/src/ingestion_worker/handlers.py:351`、`:358`、`:915`
  (用 `embedder.model_name`)、`services/csp/app/services/memory_service.py:544`
  (用 `resolve_platform_embedding` 的名字)、
  `packages/anila-core/src/anila_core/storage/adapters/pgvector_store.py:218`、`:228`。
  **不是只有 worker**;先前的紀錄把這件事講得太窄。
- **4、5 是既有的靜默空結果路徑**(不是這次改動造成的),但它們**帶著同一個碰撞**——
  chunk 檢索修好之後,**記憶檢索仍然會因為大小寫而悄悄回空**。
  影像檢索(`search.py:966`)已於同一輪順手修掉,不在清單上。
- **第 9 條刻意不關,理由值得記**:要讓它不 409,就得在指定落在軟回退時放棄來源模型過濾。
  但那樣**查詢向量來自 F 模型、段落來自 M 模型**,跨兩個嵌入空間的 cosine 沒有意義,
  端點會回一批任意段落**並且宣稱它們相關**。
  **有自信的錯答案比誠實的錯誤更糟**——那個語料庫真的取不到,壞的只是措辭。
  訊息已改成平台**接受**的動作(重新指定回原模型;若已停用要先重新啟用,因為停用的模型會被拒絕)。
- ✅ **那條「待確認」查清楚了(2026-08-07)**:`DEFAULT_EMBED_MODEL` **存在**,在
  `packages/anila-core/src/anila_core/memory/long_term/embedding.py:40`,值是 `nvidia/NV-embed-V2`
  (**又一個同族拼法**)。目前只被 `memory/__init__.py`、`memory/user.py` 再匯出,
  以及 `packages/anila-core/tests/test_memory_user_layer.py:306` 釘住那個字串;
  **全樹沒有任何寫入路徑用它**。所以它是潛伏的第 10 條,不是現行缺陷。
  `packages/anila-core/**` 不在本包範圍(同期被另外三包動過),留給後續。

### #54 `ANILA_MODEL_FAST` —— 設了不會有任何效果(接線未完成)

- **看起來是什麼**:設這個環境變數,期待自動標題改用比較快的模型。
- **實際**:**接線還沒做完**。自動標題送的是 `model: effectiveTarget`——
  **跟回答那輪對話的同一顆模型**(`apps/anila-shell/src/app.jsx:1111-1136`),
  跟這個環境變數無關。`packages/anila-core/src/anila_core/prompts/model_routing.py:20-21`
  自己註明「待接」。
- **處置**:**接線包落地之前不要在任何文件或部署說明裡提這個變數。**
  現行行為(跟著對話那顆走)反而是零設定、自我維護的——模型陣容換了會自動跟著
  治理中心指定的主路由走,一人維運要的就是這個形狀。
- 📌 順帶澄清一個誤解:自動標題**沒有**綁定 `nothink` 變體,
  所以內網沒有那個變體也不會壞;失敗時保留「使用者第一句截 28 字」,靜默但無害
  (使用者沒有表達過「請下標題」的意圖,不構成假控制項)。
  anilalm 工作區**完全沒有** LLM 標題,只截前 60 字。

### #55 匯入失敗的原因**存了但前端一次都沒畫** ✅ 已修

- **畫面說**:側欄只顯示紅色的「失敗」兩個字。
- **實際**:後端**有存** `error_message`,而且訊息是準確的
  (例如「檔案抽取後沒有可用文字(純圖片 / 加密 / 空檔)」);
  `apps/anilalm/src/workspace/WSSidebar.tsx:428` 附近零渲染,全 `src` grep 無命中。
- **後果**:擁有者 2026-08-05 回報「大型空白表格建不了索引,好像被判沒語意」——
  **那是誤診,而誤診的原因就是這一條**。系統其實明確知道也存下了原因,只是沒說。
- **真正的機制**(fable5 在活體容器實測):沒有 chunk 層級的語意判斷;
  唯一的判定是整份文件層級的
  `packages/anila-core/src/anila_core/ingestion/parsers.py:132-138`
  「抽取後零文字即毀損」。**DOCX 的空白表格可以正常索引**,
  症狀只發生在**沒有文字圖層的 PDF／掃描檔**。
- **修法方向**:把已存在的 `error_message` 畫進失敗列。不動匯入邏輯、不加啟發式。
  ⚠ **不要**為此引入表格結構解析那類重依賴——那正對著「越複雜就被放棄」那條教訓。
  目前保留錯誤原因作為 `title`，內聯文字以 ellipsis 限寬，避免 unbounded `Text` 撐破側欄。

### #53 「哪一則回答變成沒有依據」沒有人記(座標已定,待排小包)

- **背景**:#44 的 RAG 靜默退化修好之後,失敗會被寫進訊息的
  `metadata.retrieval_failed`,而且撐得過重整。**紀錄是有的,問題是沒有人會發現它。**
- **已查證的兩件(2026-08-05,活體容器)**:
  ① csp **有**每請求存取日誌(`services/csp/app/main.py:76-88` 的 `setup_logging()`
  明確掛 `StreamHandler(sys.stdout)`、root level INFO,呼叫點在 `lifespan()` 是部署路徑),
  活體容器內數到 **6344 行**,含 502×1、401×211、404×3334 ——
  **打到 csp 的檢索失敗今天就已經有一行日誌**。
  ② 它看不到的只有兩類:**根本沒到 csp 的失敗**(nginx 502／TLS／DNS／離線／中止),
  以及**「哪一則回答因此變成沒有依據」的關聯**。
- **座標(RAG 那包追出來後停手,因為 csp 不在它範圍內)**:
  `services/csp/app/api/conversations.py:698`(`POST /{conv_id}/messages`,metadata 在 `:716`),
  以及同一份 metadata 會經 `:831` 的 turn/finalize 路徑進來——**兩條要一起處理**。
  寫入帶該旗標的訊息時吐一行 WARNING 即可。
- 📌 **這一條記在這裡本身就是教訓**:驗收指出這組座標原本只活在交接報告裡,
  全樹 grep 零命中。**查得到 ≠ 有人會發現**,對日誌是這樣,對座標也是這樣。

### #52 「待重算筆數」**結構性地永遠是 0** —— 一個有自信的零 🔴 ✅ 已修

- **畫面說**:`GET /api/models/platform-embedding` 回報 `pending_recompute`,
  也就是「換了平台 embedding 模型之後,還有多少東西沒重算」。
- **實際**:`app/services/platform_embedding.py:201-231`(⚠ **2026-08-15 更正行號**:原記 `:121-155`,
  程式碼位移後失準;`count_pending_recompute` 現在在 `:201`,缺陷本身已修)在一個**沒有設定 RLS 情境**的
  session 上計數,而 `document_chunks` 與 `ingestion_images` 都是 **FORCE-RLS**,
  runtime 角色 `csp_app` 又**不繞過 RLS**(這是刻意的,見鐵則)。
  於是這兩個數字**恆為 0,與真實積欠量無關**;只有沒開 RLS 的
  `conversation_memory_chunks` 講的是實話。
- **實測(2026-08-05,活體 DB 唯讀)**:兩張表 `relrowsecurity=t, relforcerowsecurity=t`;
  `csp_app` 的 `rolbypassrls=f, rolsuper=f`;GUC 未設時該述詞為真 → 全部濾掉。
- **為什麼算最糟的形狀**:它不是「按了沒反應」,是**主動報一個令人安心的數字**。
  維運者換完模型看到「待重算 0」,會直接認為換模型沒有代價。
- 📌 **這一條是索引錯配那包挖出來的,而且它拒絕把這個數字接進自己的新畫面**——
  理由是「那會在一個專門為了誠實而做的功能上,送出一個謊」。**這個判斷是對的。**
- **已修**:`platform_embedding.py` 已逐一設定 collection RLS scope，並在函式結束時清空
  `anila.collection_id`；`health_overview.py` 未消費這個數字，要一起看。
- **成本決策（量測）**:保留逐 collection 的精確計數，不加 cap/cache 或權限限制。
  以 100 collections 的測試 double 實測 **303 次 `Session.execute`**（固定 2 次 + 每庫
  scope 1 次與兩張 FORCE-RLS 表各 1 次 + 清理 1 次）；若是 300 collections，按同一公式約 **903 次**。
  這個數字與量測條件也寫在 `count_pending_recompute` docstring，後續若要改成本邊界可從此基準重驗。

### #51 CSRF 豁免清單看的是**攻擊者可控的路徑**(第 4 段要修) 🔴

- **實際**:`middleware/csrf.py:88-90` 用 `request.url.path` 判斷豁免;
  而 starlette 0.49.3 的 `request.url` 是**把原始 Host 標頭字串串接**出來的
  (`url = f"{scheme}://{host_header}{path}"`)。所以 `Host: x/api/auth/login`
  可以把路徑邊界整個挪走,讓 `startswith()` 命中豁免——**同一個請求**
  FastAPI 卻是用乾淨的 `scope["path"]` 繞送,照樣進到目標端點。
- **已重現(2026-08-05,活體容器內跑本專案自己的 `_should_skip`)**:
  四種污染 Host 全部拿到 `SKIP_CSRF=True`。
- **兩個成因,要一起修**:starlette 的缺陷(CVE-2026-48710,1.0.1 修掉)
  **加上**本專案選了 `url.path` 而不是 `scope["path"]`。
  **只升版不改判斷來源,等於把安全邊界外包給函式庫。**
- **nginx 擋得住,但不夠**:Host 允許清單在 80／443／4443 三個埠都擋掉了(已實測),
  然而 csp 與 codeserver／n8n 同在 `anila-net`,那兩個都跑使用者提供的程式碼,
  可以直接打 `csp:8000` 繞過 nginx。
  ⚠ `TrustedHostMiddleware` **沒有註冊**,而且就算註冊,它掛在 `CsrfMiddleware` **內側**,
  補不到這個洞。

---

## 第七輪(2026-08-15)——凍結前巡 compose,asr-gateway 有三顆沒人讀的旋鈕

### #59 asr-gateway 的三顆 JWT／cookie 旋鈕:compose 給了,**沒有任何程式在讀**(裁定拿掉,而且要趕在重建映像之前)

- **維運者看到的**:`infra/compose/platform.yml:784-785`、`:788` 在 **asr-gateway**
  (區塊起於 `:728`)的 `environment:` 底下列著 `JWT_ISSUER`、`JWT_AUDIENCE`、`COOKIE_SECURE`,
  前兩顆還吃 `${...}` 覆寫。任何人翻 compose 都會得出「改這裡就會改到 gateway 的
  驗章與 cookie 行為」的結論——而且這三個名字讀起來全都像安全開關。
- **實際**:`services/asr-gateway/app/config.py` 的 `Settings` **刻意沒有宣告**這三個欄位。
  `:91-97` 講 `JWT_ISSUER`／`JWT_AUDIENCE`:csp 從不簽 `iss`／`aud`,把欄位加回去
  (哪怕只是「有設才驗」)會直接復活 2026-07-31 那次故障——因為 compose 有給值,
  「有設才驗」在部署環境等於「一律驗」→ 一律 401。
  `:104-111` 講 `COOKIE_SECURE`:它以前用來在兩個 cookie 名之間二選一,而 cookie 名
  現在是常數,這顆旋鈕沒有東西可以選。
  三個名字在 `services/asr-gateway/**` 只出現在上面那兩段註解裡:程式碼零取用、
  `services/asr-gateway/Dockerfile` 零命中、全樹沒有一處用 `os.environ`／`os.getenv`
  直接去讀它們。而 **pydantic-settings 不會為多出來的 env var 報錯**
  (`config.py:96-97` 自己寫了),所以症狀是**完全沉默**:設了、沒報錯、什麼也沒發生。
- **為什麼算這份清單的東西**:「刻意不收」的理由寫在**消費端**(`config.py` 的註解),
  而會被誤導的人正在編輯**生產端**(compose)。**改 compose 的維運者不會去讀 gateway 的
  設定模組**——一個只有消費端知道的約定,對生產端而言就等於不存在。
  這與 #37 是同一個形狀,差別只在這次是三顆一起。

**裁決(2026-08-15,幕僚長裁定,稽核線同意):把這三個鍵從 asr-gateway 的 env 區塊拿掉。**
不是「補文件說明它們沒作用」,也不是「兩端各寫一次名字」。

- **判準沿用本檔 `:178-181`**:清尾一律選「拿掉」而不是「接上去」,而且靜默忽略要換成明確拒絕。
  **同形狀的先例就在 `:186`**——`ENABLE_API_DOCS` 宣告了但沒人讀、維運者會以為自己
  關掉了 API 文件,當時的處置就是**移除**。
- ⏰ **時效:必須趕在出貨映像重建之前。** compose 是交付包的一部分——根目錄
  `compose.yaml` 以 `include:` 引入 `infra/compose/platform.yml`,而
  `infra/deployment/intranet/intranet-deploy.sh:165-166` 與
  `infra/deployment/intranet/build-and-export-for-intranet.sh:79` 都是以 `compose.yaml` 起棧。
  **釋出標籤之後才動,出去的那一包就還是帶著三顆死旋鈕。**
- **範圍只有 asr-gateway 的 env 區塊**,不要順手動別的服務。
- **動手的人自己要重新舉證**。要證明的命題是「`services/asr-gateway/` 裡**沒有任何機制**
  讀得到這三個名字」,不是「`Settings` 沒有宣告它們」——應用程式碼、進入點腳本、
  `Dockerfile` 都要 grep,並且另外 grep 直接用 `os.environ`／`os.getenv` 的地方。
  稽核已經跑過一輪、命中零,**但那不免除實作者自己的舉證責任**:
  本專案的教訓是「搜不到 ≠ 不存在」,舉證責任在提報的那個人身上。
- 📌 **已經有一道守衛在守這件事的一半,而它漏掉的正好是另一半**:
  `services/csp/tests/test_settings_registry.py` 的 `REMOVED_ENV_NAMES`(`:42` 起)
  把 `COOKIE_SECURE` 列為已退役(`:78`),並且**掃全 repo 的 Python 檔**
  (`_python_sources()` 的 docstring 寫明刻意用 repo 範圍,免得別的服務偷偷復活舊名)
  斷言沒有任何行程還讀得到它——所以「沒人讀」這半是有機器在看的
  (三顆裡只有 `COOKIE_SECURE` 在那份清單上)。
  但那支測試的檔頭 `:1-6` 自己講明:**部署檔與腳本**的同一份孤兒清單
  「在交付報告中用逐名全樹掃描留證」——**也就是 compose 這一側沒有機器在看**。
  #59 能活到今天,原因就寫在那句話裡:守衛守的是消費端,而這個缺陷長在生產端。
- ⚠ **拿掉是為了誠實,不是為了修正**。`config.py:96-97` 明講多帶這幾個 env var
  不會讓 `Settings` 爆掉,所以這個改動在功能上是零。下一個讀到這裡的人**不要**
  因此把它當成「沒必要的改動」再加回去:留著的代價是一顆看起來能動、實際動不了的安全開關,
  而這正是設定頁從 96 顆砍到 12 顆的同一條理由。

**動手前要一併看的一件(觀察,不在上述裁決範圍內)**:`infra/compose/dev.yml:357-358`、
`:361` 的 asr-gateway 區塊有**一模一樣的三個鍵**。#37 留下的教訓是「退役一個假控制項,
要把指向它的宣告一起清掉才算退役完,否則它只是換個地方活著」;
dev.yml 要不要一起清,裁決當下沒有講,由實作包提出來問。

### 🔧 綁在同一輪做的 follow-up:讓守衛覆蓋它自己宣稱要守的範圍(2026-08-15 裁定,稽核線提出)

**改動點只有一行**:`services/csp/tests/test_settings_registry.py:97` 的
`for path in repo.rglob("*.py")` —— **那一行就是守衛的整個視野**。
它的檔頭 `:1-6` 自己寫著,**部署檔與腳本**那一側只靠「交付報告的人工全樹掃描」留證。
🔴 **如果那一行早就含 `*.yml` / `*.sh`,#59 這三顆根本活不到今天**;
我們是靠一次文件逐句稽核**偶然**撞到的,而 #37 證明同一個形狀已經復發過一次。

⚠ **這不是「把系統變嚴」**。那條教訓管的是**對使用者的限制**;
這裡是讓一個**已經存在**的守衛,覆蓋它宣稱要守的範圍。不同類。

🔴 **實作必須解掉的陷阱(否則它會變成噪音)**:Python 那側用 AST,分得開「提到名字」與
「真的讀取」;**yml／sh 只能做文字比對,而最常提到已退役名字的地方,正是解釋它為什麼
被退役的註解**——`services/asr-gateway/app/config.py:91-106` 現在就是這樣(三顆名字各出現
兩次,全是註解)。
👉 **判準要寫成「看起來像賦值」而不是「出現過」**:`^\s*KEY:`(yaml)／
`^\s*(export )?KEY=`(sh)／`^KEY=`(.env.example)。**註解行天然被排除,不需要 allowlist。**
⚠ **做成「出現就紅」的話,第一次誤報之後就不會有人再看它**——那是另一種失效:
**守衛還在,但沒有人相信它。**

📌 **排程:跟上面「拿掉三顆」同一輪做,不要分兩次。**
**第二件做完,第一件才不會再發生。**

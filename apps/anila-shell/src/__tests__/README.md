# anila-shell 測試:強度分級與突變檢查

## 為什麼有這份文件

2026-08-03 的稽核測到一件事:把 `app.jsx` 的對話歷史組裝弄壞(讓每一回合
都送出空歷史),**全套 413 條測試一條都沒紅**。產品當時是壞的——使用者每
一句話都在沒有前文的情況下送給模型——而測試是綠的。

原因不是測試寫得少(33 個檔、16 個做元件渲染),而是**沒有任何一條把
orchestrator 掛起來**。`app.jsx` 是對話狀態機、歷史組裝、存檔與錯誤呈現
的所在地,而它唯一被 import 的測試只取了一個純函式;其餘關於它的斷言全
部是「讀原始碼字串比對」。

## 三級強度

| 級別 | 長相 | 擋得住什麼 | 擋不住什麼 |
|---|---|---|---|
| **行為測試** | 掛起真的元件/orchestrator,操作它,斷言使用者看得到的結果 | 邏輯錯、時序錯、狀態沒接上、靜默失敗 | — |
| **純函式單元測試** | 直接呼叫 `runtime/*.js` 的匯出函式 | 該函式自己的邏輯 | 呼叫端根本沒接、參數傳錯 |
| **原始碼字串比對** | 讀檔 + `toContain` | 整段被刪掉 | 幾乎所有其他情況 |

第三級**不是行為覆蓋率**。它們有價值(擋整段刪除、擋文案倒退),但不能
拿來回答「這個功能會動嗎」。所有這類檔案都在檔頭標了 `@source-text-guard`,
`sourceTextGuardRegistry.test.js` 會確保新增的也一定要標。

## 行為測試

| 檔案 | 釘住的不變式 |
|---|---|
| `orchestratorSend.test.jsx` | 送出時帶的對話歷史正確(含第三輪之後);回答真的存進後端 |
| `orchestratorFailures.test.jsx` | 存檔失敗/2xx-without-id/後端非 2xx **一定被使用者看見** |
| `orchestratorBranching.test.jsx` | 編輯重問與重新產生的歷史切點、`/branch` 兄弟節點 |
| `orchestratorConversations.test.jsx` | 切換對話不互相污染;接續既有對話帶得到伺服器上的前文 |
| `orchestratorThrottle.test.jsx` | 回到視窗時的 agent 清單重抓有節流 |
| `transportHeaders.test.jsx` | **請求離開瀏覽器之前**:CSRF、對話歸屬、Task 歸屬三個標頭真的掛上去了 |
| `transportSessionAnswer.test.js` | `streamSessionAnswer` 自己抄的那份 CSRF 組裝(第二級:app.jsx 還沒有呼叫端) |

### 掛載方式

`helpers/orchestrator.jsx` 的 `mountOrchestrator()` render 的是 **`src/app.jsx`
的 default export**,包在 `main.jsx` 用的同一組 Provider(`AuthProvider` +
`ConfirmProvider`)裡。連登入態都是真的:`AuthProvider` 自己去打
`/api/auth/me`,由假後端回答,而登入後的兩個 cookie
(`anila_access_token` / `anila_csrf`)由 `seedSessionCookies()` 種下——
CSRF 中介層是看這兩個 cookie 決定要不要檢查的。

`helpers/fakeBackend.js` 攔的是全域 `fetch`。app.jsx 所有對外路徑
(`authRequest` / `streamChatCompletion` / `createTaskForConversation` /
`refreshAgents`)最後都收斂到那裡,所以攔在這一層,受測的是**真的
orchestrator**,沒有任何一個它自己的函式被替身取代。

> ⚠ 不要在 harness 裡「重新實作一遍」app.jsx 的任何邏輯。一旦這麼做,
> 測試就變成在測 harness 自己——那正是上一輪全綠但產品壞掉的成因。

假後端是**有狀態**的:訊息樹(`parent_id` / `sibling_*`)、active leaf、
對話列表都真的維護,所以「多輪之後歷史還是對的」驗得出來。

## 假後端對真度稽核(2026-08-05)

假後端最危險的失敗模式不是「寫得少」,而是**和真後端往同一個方向錯**——
那種假後端證明不了任何事,而且會在真後端改變之後繼續全綠。所以凡是這裡
模擬的行為都要指得出真後端的對應位置,而且要真的量過。

**證據等級**要標清楚,不然下一個人會以為整張表都量過:

- **活體** = 對本機執行中的 CSP(`https://localhost/`,15 容器)真的發過請求。
- **原始碼** = 讀 `services/csp/` 的路由/中介層確認,沒有實際打過那一條。

| 項目 | 真後端 | 證據 | 假後端(修正前) | 處置 |
|---|---|---|---|---|
| unsafe 請求的 CSRF | 帶 session cookie、不帶 `X-CSRF-Token` → **403** `{"detail":"CSRF 驗證失敗，請重新登入"}`;帶了相符的就穿過中介層;值不符也是 403;完全沒有 session cookie 則放行給下游回 401 | **活體**(四種組合都打過) | **完全不檢查** | 已補:`csrfRejection()` 逐條照抄 `_should_skip` + `dispatch` |
| `anila.meta` frame | **一定會出現**;下游沒送就由 `proxy_stream` 自己補(`if not meta_seen`),而且排在 `[DONE]` **之前**(`pending_done_block` 延後 yield) | **原始碼**(`services/proxy/service.py:754-770`)。要打活體需要有效卡登 session,沒做 | 只在測試明講時才送 → `app.jsx` 的 `applyMeta` 整條路徑在所有 orchestrator 測試裡從沒被執行過 | 已補:`scriptAnswer()` 一律送一個 `defaultMeta()`,欄位與 `build_default_anila_meta()` 逐項對齊 |
| `POST /api/conversations` | **201** | **原始碼**(`api/conversations.py:410`) | 200 | 已改 |
| `POST …/messages`、`…/branch` | **201** | **原始碼**(`:668`、`:699`) | 200 | 已改 |
| `POST /api/tasks` | **201** | **原始碼**(`modules/tasks/router.py:41`) | 200 | 已改 |
| `X-ANILA-Conversation-Id` 指到別人的對話 | **404** `Conversation not found`;不可轉成整數則視同沒帶(降級,不報錯) | **原始碼**(`proxy.py` `_coerce_conversation_id` / `_require_conversation_access`) | 完全不看這個標頭 | 已補,含降級那一支 |
| 錯誤 body 形狀 | `{"detail": "…"}`,`content-type: application/json` | **活體**(401/403 都量過) | 同形 | 一致,不必改 |
| `DELETE` 回 204 | 204 無 body | **原始碼**(`:657`) | 204,`api.js` 有 `status === 204 → null` 的分支 | 一致 |

⚠ 標「原始碼」的那幾列**還沒有被活體打過**。要升級成活體驗證需要一個有效的
卡登 session;做得到的時候應該補,尤其是 SSE frame 順序那一列 —— 它是這張表
裡最容易在真後端改動後悄悄漂掉的一條。

**已知仍有落差、刻意不模擬的**:

- **速率限制 / 配額**:真後端有,假後端沒有。orchestrator 測試不驗這一層,
  補上去只會讓測試變慢變脆。
- **`anila.meta.trace` 的內容**:形狀對齊了(`kind`/`label`/`detail`/`status`),
  但真後端的 trace 會依 router 決策、記憶注入、附件注入而增減條目。
  假後端固定只放一條 `call`。驗 trace 細節的測試要自己 `enqueueFrames`。
- **`enqueueFrames()` 送的是原始 frames,不會自動補 meta**——那是給
  「我要驗一個不尋常的串流」用的逃生口。用它就要自己把 frame 序列排對。

### 合併漂移事故(2026-08-05):假後端落後真後端一個架構

`wt/shell-reserve` 與 `wt/test-foundation` 是從同一個基底平行長出來的,
git 合併(`263d4f46`)**零衝突** —— 兩包動的是不同的 hunk。但 reserve 把送出
路徑整個換掉了:使用者訊息與助理列改由 `POST /api/conversations/{id}/turn` 在
**同一個交易**裡建好,串流結束再 `PUT …/messages/{id}` 把內容寫回那一列。

`helpers/fakeBackend.js` 當時只認得舊的 `POST …/messages`,於是對 `/turn` 回
404 → `persistTurnHead` 判定失敗 → 整輪在串流開始前就中止 → **28 條測試同時
紅**(orchestratorSend 5/5、orchestratorBranching 6/6、orchestratorFailures
7/8、orchestratorConversations 4/5、transportHeaders 6/7)。

值得記下來的是**它壞得很大聲**。這份文件開頭警告的那個失敗模式是反過來的:
假後端和真後端往同一個方向錯,測試繼續全綠,而產品已經壞了。這次是假後端
**落後**真後端,所以 28 條測試立刻倒下。落後比同向錯好太多。

處置:`helpers/fakeBackend.js` 補上 `POST /turn`、`POST …/branch-turn`、
`PUT …/messages/{id}` 三條,語意逐段照抄 `fakeConversationBackend.js`
(寫入者權杖 409、非終局狀態不得由 PUT 建立、終局後狀態封存 409、
不帶 envelope 的 patch 不抹掉既有標記、`_close_unanswered_leaf`)。
另外四處注入點跟著搬,斷言的**意圖**一個都沒有放寬:

| 測試 | 原本注入在 | 現在注入在 | 為什麼不算放寬 |
|---|---|---|---|
| 助理回答存檔失敗 | `POST …/messages` (role=assistant) | `PUT …/messages/{id}` | 助理內容現在就是走 PUT 落庫的 |
| 2xx-without-id(送出 / 編輯重問) | 同上 | 同上 | 同上 |
| 使用者訊息存檔失敗 | `POST …/messages` (role=user) | `POST …/turn` | 使用者訊息現在由 turn 落庫 |
| 編輯重問不得就地覆寫 | 斷言「PUT 一次都沒有」 | 斷言「沒有一個 PUT 打在使用者訊息上」 | 預留列的寫回本來就是 PUT;要擋的是**舊問句被覆寫**,那一條原封保留 |

還有一條斷言是被**產品改對**而失效的:`/儲存|失敗/` 釘的是措辭,而 reserve
把文案換成更精確的「這則回答沒有存回對話紀錄，重新整理後就會消失（你的問題
已經存好了）。」。改成斷言 `ANSWER_PERSIST_FAILURE_NOTICE` 這個常數 ——
它仍然分得出「回答沒存回去」與「head 失敗」兩種說明,而後者刻意**不**斷言
沒存進去(那時使用者的問題其實已經在資料庫裡)。

### 為什麼有兩個假後端,還沒有合併

| | `helpers/fakeBackend.js` | `fakeConversationBackend.js` |
|---|---|---|
| 攔在哪 | 全域 `fetch` | 注入的 `authRequest` |
| 涵蓋面 | 整個 shell:auth、agents、tasks、banners、ui-settings、SSE(可逐格 push)、CSRF 中介層 | 只有對話/訊息樹 |
| 強項 | 端到端,測得到 transport 層(標頭有沒有掛上去) | 協定嚴格,還有請求/回應閘門可以編排兩個分頁互相插隊 |
| 誰在用 | orchestrator*、transportHeaders | reservedTurn |

**目前的決定:保持兩份,但訊息樹那一半以 `fakeConversationBackend.js` 為準。**
理由是合併的兩個方向都要付一次大改寫:把 SSE/auth/agents/CSRF 那一層搬進
class 版,或把權杖狀態機＋閘門搬進 fetch 版;而兩邊各自的強項(端到端 vs
可編排)其實是兩種不同的測試需求,不是重複。

**但這個決定有明確的代價,而這次事故就是帳單。** 所以配套是:訊息樹相關的
端點,`helpers/fakeBackend.js` **不自己詮釋**,一律照抄 class 版 —— 檔案裡
那三段新程式碼都標了對應的方法名。下一次真後端再改協定,要改的仍然是兩個
地方,這一點沒有解決,只是把「兩份會分歧」降級成「兩份會一起過時」。
真正的收斂應該排進 PLAN,不該夾在這一輪裡順手做。

## 突變檢查(證明測試真的會紅)

一條「把它宣稱保護的行為還原回去、卻仍然通過」的測試等於不存在。
`scripts/mutation-check.mjs` 就是用來證明不是這樣的。

```bash
cd apps/anila-shell
node scripts/mutation-check.mjs                 # 全部突變
node scripts/mutation-check.mjs history-*       # 只跑符合的
node scripts/mutation-check.mjs --list          # 看清單
node scripts/mutation-check.mjs --check-anchors # 只驗錨點,不跑測試(幾毫秒)
```

每個突變會:確認乾淨時**兩組都是綠的** → 套用 → 跑新測試與既有測試兩組 →
還原 → 再確認**兩組**都回到綠。任何突變**存活**(該紅卻沒紅)離開碼為 1。

**為什麼還原後要驗兩組。** 整份報告最有價值的數字是「既有測試只抓到 N 個」。
如果某條既有測試在突變**之前**就已經是紅的,「被突變殺掉」和「本來就壞著」
在輸出上長得一模一樣,那個數字就沒有意義。所以每一個突變都必須從一個
**已知全綠**的狀態出發——基準線那一次不夠,還原之後也要再確認一次,
因為那正是下一個突變的起點。基準線不綠就直接離開(離開碼 2),不會硬跑。

**中途中止是安全的 —— 但不是靠訊號處理器。** 2026-08-05 量到(node v22.23.1):
**在 `execFileSync` 阻塞期間送到本行程的 SIGINT 會被整個吃掉**,
`process.on("SIGINT")` 完全不觸發;而且因為註冊了處理器,連 node 預設的
「收到就死」也一併失效。這個腳本 99% 的時間卡在 `execFileSync` 裡,所以
**光寫訊號處理器等於沒做**(最小重現寫在腳本檔頭)。真正扛住的是兩層:

1. **還原日誌**(`node_modules/.cache/anila-mutation-check.json`)。改壞之前
   先寫,還原成功才刪。下一次啟動看到它就先把樹修回去並大聲說出來 ——
   連 `kill -9` 都救得回來,因為它不依賴本行程還活著。
   單獨修:`node scripts/mutation-check.mjs --restore`。
2. **看子行程怎麼死的**。互動式 Ctrl-C 送給整個 process group,`npx vitest`
   也會收到,`execFileSync` 因此丟出 `signal === "SIGINT"`。那是唯一能即時
   可靠地知道「使用者要停」的訊號,而且它**不能**被記成「測試紅了」——
   那會變成一個假的「抓到」。

量測結果:互動式 Ctrl-C → 離開碼 130、工作目錄乾淨;`kill -9` → 樹是髒的
但日誌還在,下一次啟動自動修回去並印出修了哪些檔。

設計約束:每個突變都**保留所有識別字**,改的是運算子、索引、屬性名這種
東西——所以 grep 原始碼的測試救不了你。清單裡至少有兩個是「讓存取器回
空值」與「刪掉一個賦值」那一型,也就是當初讓 413 條全綠的那一型。

錨點必須在目標檔案裡剛好命中一次,`replace` 也必須真的改到東西(兩者
都在 `applyMutation()` 裡硬檢查,不符就直接報錯離開)。原始碼改動後突變
清單需要跟著更新,這是刻意讓它吵的;`--check-anchors` 讓這件事幾毫秒就
問得出答案。

### 象限:清單會繼承作者對「什麼會壞」的想像

原本的 16 個突變**全部落在「請求送出之後」**——歷史組裝、存檔、分支、
錯誤呈現。整個 **「請求離開瀏覽器之前」** 的象限是空的:transport 標頭
與對話/Task 歸屬,一條測試都沒有看著。這不是疏忽,是清單的必然:
它只涵蓋得到作者想像得到的壞法。

實測後果:把 `X-CSRF-Token` 從 `runtime/api.js` 或 `runtime/sse.js` 刪掉,
兩套測試全綠;而帶 cookie 不帶該標頭打 `POST /api/conversations` 回 403、
帶了回 201。也就是說瀏覽器裡每一次送出、編輯、重新產生都會失敗,
而測試不會告訴你。

double-submit CSRF 的組裝在 shell 裡被抄了**五份**(`api.js` 的
`buildHeaders` 與 `authMultipart`、`sse.js` 的 `streamChatCompletion` 與
`streamSessionAnswer`、`tasks.js` 的 `createTaskForConversation`),
修好一份不會連帶修好其他四份,所以每一份都要有自己的突變。

**目前無法從這個 harness 觸及、刻意留白的形狀**(同樣寫在腳本檔頭):

- `runtime/api.js` 的 `authMultipart`:要從掛起來的 shell 觸發需要走完
  檔案挑選 → 上傳的 UI 流程,jsdom 的 `FormData`/`File` 與瀏覽器差太多,
  做出來的會是在測 harness。已由 `wt/shell-reserve` 的
  `csrfHeaders.test.js` 在函式層面收掉,**兩個分支合併後**這一格才有守衛。
  在本分支單獨列突變會存活 → 那會是一個假的紅,所以不列。
- `runtime/sse.js` 的 `streamSessionAnswer`:`app.jsx` 目前沒有任何呼叫端
  (全樹 grep 只有定義與兩處註解),掛起來的 shell 走不到。改用直接呼叫的
  `transportSessionAnswer.test.js` 收,突變照列。等 `app.jsx` 真的接上
  中斷/續答流程,這條要升級成 orchestrator 級。

### 分工(與 `wt/shell-reserve` 的重疊面)

`wt/shell-reserve` 的 `csrfHeaders.test.js` 在**函式層面**釘住 `api.js`
(`buildHeaders`、`authMultipart`)與 `sse.js`(`streamChatCompletion`)
三條路徑。本分支不重複那三條,改釘:

- **orchestrator 端到端的接線**——直接呼叫時 `conversationId` 是測試自己
  傳的,永遠是對的;產品真正會壞的那一種是 `app.jsx` 把**暫態 client id**
  (字串)傳下去,`typeof conversationId === "number"` 不成立、標頭靜悄悄
  地整個不送。只有掛起來測才看得到。
- 它明說沒收的兩處:`runtime/tasks.js` 與 `streamSessionAnswer`。

**合併時要處理的三件事**(已看過 `wt/shell-reserve` 的 diff):

1. 該分支改了 `app.jsx` 839 行、也動到 `runtime/messageTree.js`。本清單有
   **13 個突變的錨點在這兩個檔**,合併後幾乎一定會漂掉。合併後第一件事就是
   `node scripts/mutation-check.mjs --check-anchors`(幾毫秒),再依實際內容
   更新錨點——**不要**因為錨點對不上就把突變刪掉。
2. 該分支新增的 `csrfHeaders.test.js` 不在本腳本的 `PRE_EXISTING_EXCLUDES`
   裡,合併後會被算進「既有測試」,讓那個數字虛胖。它其實是同一批新工作的
   一部分,合併時應該加進排除清單(或把兩邊的分桶重新定義一次)。
3. 該分支的 `reservedTurn.test.jsx` 在**註解**裡提到同步讀檔函式的名字。
   登記簿的偵測會先剝註解,所以不會誤判——但這正是「檔案級標記」那個界線
   的活例子:如果哪天有人在那個 1770 行的行為測試檔裡塞一條真的字串比對,
   整檔會被標成第三級。字串比對請放 `src/__tests__/guards/`。

## 登記簿的兩個逃逸口(2026-08-05 補)

`sourceTextGuardRegistry.test.js` 之前有兩個洞,兩個都不是惡意,是習慣不同:

1. **偵測只認 `readFileSync` 這一個字串。** 改用 `fs/promises` 的非同步
   讀檔、`createReadStream`、或動態 `import("node:fs")` 都看不到。
2. **目錄掃描沒有遞迴。** `src/__tests__/` 任何子目錄裡的字串比對測試
   完全在視野之外。

`src/__tests__/guards/headerLinesPresent.test.js` 就是**同時踩中兩個洞**
的那個形狀(子目錄 + `fs/promises`),留在樹上當回歸樣本。兩個洞都補了:
偵測改成訊號清單、掃描改成遞迴,而且多了一條測試直接釘住「掃描是遞迴的」。

**兩個要寫下來的界線:**

- **標記是檔案級的,不是測試級的。** 一個檔案只要有一條讀原始碼,整檔
  就被標成第三級——也就是說它標的是**該檔最弱的那條測試**。目前被標的
  五個檔都是純字串比對檔,沒有東西被誤標;但如果有人在行為測試檔裡塞
  一條字串比對,整檔會被降級標示,而那個標示會低估同檔其他測試的強度。
  **慣例:字串比對放自己的檔案(`src/__tests__/guards/`),不要和行為
  測試混在同一檔。**
- **偵測是黑名單,黑名單永遠補不完。** `eval` 一段組出來的字串、把讀檔
  包進 helper 再 import,都繞得過去。真正的防線不是那份清單,而是
  `KNOWN_SOURCE_TEXT_GUARDS` 要求**完全相符**:任何新增/移除都會讓登記簿
  變紅,逼人動手改清單,而那個改動一定出現在 diff 裡讓審查者看到。
  清單只擋「無意間繞過」;「刻意繞過」擋不住,也不打算擋。

# 報告二:ANILA 聊天 UI 實機驗證

> 驗證日期 **2026-07-26**,分支 `feat/ux-parity`。方法:Playwright + Chromium 無頭瀏覽器,實機點擊、實機對話。
> 環境:`anila-platform-dev` 11 服務全 healthy;模型後端 = **本機 llama.cpp gemma-4-12b**(Router primary);
> 入口 `https://localhost:8443/anila/app`,admin 帳密登入。
> 所有結論都附實測輸出或截圖;我自己犯的量測錯誤一併列出。

---

## 0. 結論先行

1. **昨晚做的五個 UX 功能,在真實瀏覽器裡逐一驗證通過**——不只是「選單打得開」,封存是**真的封存進去**(側欄出現「已封存 2」計數)。斜線指令 6 個內建全部命中。
2. **ANILA 的代理層效率比 OpenWebUI 好**:直接指定模型時平台只加 **+0.6s**(OpenWebUI +1.4s)。這是 CSP 代理設計的實質優勢,值得記錄。
3. **但預設走 Router 的延遲是 64 秒**,比直接指定模型慢 **~10 倍**。原因是 Router 要跑兩段 LLM(路由決策 + 直答),而 reasoning 模型每段都要思考。這是**產品層面最該決定的一件事**(見 §5)。
4. **抓到兩個真實 bug**,都跟「非預設埠」有關,對院內部署是硬傷(見 §4)。
5. **一個必須修的模型相容性問題已定位並實證**:Router 的 inference 呼叫完全不宣告 `max_tokens`,而 `model_registry.context_window` 欄位存在卻沒有任何地方在用——正是你說的「token 上限要跟部署模型相同」缺的那條線。

---

## 1. 五個 UX 功能實機驗證

| 功能 | 結果 | 實測證據 |
|---|---|---|
| **`/` 斜線指令** | ✅ | 打 `/` 後**6/6 全命中**:翻譯、摘要、公文、清空、快捷鍵、搜尋 |
| **`@` mention 零回歸** | ✅ | 選單出現且含 agent 候選 |
| **`Ctrl+K` 命令面板** | ✅ | 面板開啟;`archived:` 前綴可輸入;搜尋「ANILA」有結果 |
| **`Ctrl+/` 快捷鍵面板** | ✅ | 面板列出快捷鍵 |
| **對話封存** | ✅ | 「更多」選單含**封存/刪除/重新命名**;執行後清單變化,側欄顯示「**已封存 2**」 |
| **標籤** | ✅ | 「+新增」入口可開 |
| 側欄篩選 | ✅ | 全部 / 已加星 / 已封存 三個分頁皆可點 |

聊天頁共 **74 個可見按鈕**(OpenWebUI 聊天頁 25 個)——ANILA 單頁功能密度更高。

### 截圖佐證的細節

昨晚做的**發現性設計**都在畫面上:
- 側欄:`新對話 Ctrl+Shift+O`、`搜尋 / 跳轉 Ctrl+K`(鍵位直接寫在按鈕上)
- 輸入框 placeholder:「問 ANILA 任何事情 — **/ 開指令、@agent 指定 agent** · Shift+Enter 換行」
- 對話搜尋框 placeholder:「搜尋... (**tag:hr** 特休 / 支援同義詞)」
- 導覽:任務中心、我的知識庫、產出中心、專案入口、治理中心

---

## 2. 真實對話(UI 層實測)

**✅ 64 秒完成**,經 Router 自動路由:

```
用一句繁體中文說明你是什麼模型
  ▸ 11 步分析                      ← Router 的決策鏈,可展開
  我是一個由 Google 訓練的大型語言模型。
  [翻譯成英文] [摘要重點] [改寫成公文]     ← 快捷動作
  trace: trace-1784995855738 · conv: 51 · 60742ms · copy
```

三個 ANILA 獨有的優勢在這一張畫面上同時出現:
1. **11 步分析可展開**——把 Router 的路由決策鏈攤開給使用者看,三家競品都是黑盒
2. **trace id 與 conv id 直接顯示**——稽核可回溯
3. **快捷動作**(翻譯/摘要/公文)貼合公務場景

---

## 3. 效能對比(同一顆 gemma、同一個問題、同一台機器)

| 路徑 | 延遲 | 平台開銷 |
|---|---|---|
| llama.cpp 直連(基準線) | **5.4s** | — |
| **ANILA 直接指定模型** | **6.0s** | **+0.6s** ✅ 最小 |
| OpenWebUI(API 層) | 6.8s | +1.4s |
| **ANILA 經 Router(auto)** | **64s** | **+58s** ⚠️ |

**兩個結論方向相反,都要說**:
- ANILA 的 **CSP 代理層很有效率**,開銷只有 OpenWebUI 的 4 成
- 但**預設的 Router 模式讓使用者等 64 秒**,而競品是 6 秒

58 秒去哪了?Router 跑兩段 LLM:①路由決策(要模型輸出結構化 `RouteDecision` JSON)②直答。而 gemma 是 reasoning 模型,實測光是路由決策那段就要 **359 tokens 的思考**。兩段相加就是 ~58 秒。

---

## 4. 抓到的真實 bug(對院內部署是硬傷)

### 4.1 登入導向丟掉 port(HIGH)

未登入進 `/anila/app` → 導向 `https://localhost/login?next=...`(**沒有 `:8443`**)→ `ERR_CONNECTION_REFUSED`,使用者卡在瀏覽器錯誤頁。

程式碼在 [`apps/anila-shell/src/main.jsx:58`](apps/anila-shell/src/main.jsx#L58),而且**註解自己承認**:

```js
const loginOrigin = `${window.location.protocol}//${window.location.hostname}`; // 443/80 default
```

同一模式也在 [`runtime/auth.jsx:106`](apps/anila-shell/src/runtime/auth.jsx#L106)。設計假設是「CSP LoginView 永遠在預設埠」。生產若 nginx 直接吃 443 則成立,但:
- **dev 環境(8443/9443)必壞**——我實測撞到
- **院內若前置非標準埠的 reverse proxy / LB,也會壞**

修法很小:`hostname` → `host`(含 port)。**建議在正式上線前修掉**,因為它的失敗模式是「白畫面 + 連線錯誤」,使用者完全無法自救。

### 4.2 nginx 4443 的 `/` 導向同樣丟 port(MEDIUM)

`https://localhost:9443/` → `https://localhost/anila/`。來源是 [`infra/nginx/anila.conf:890`](infra/nginx/anila.conf#L890) 用 `$host` 而非 `$http_host`(同檔 `:389`、`:412`、`:810` 就是用 `$http_host`,所以是不一致而非慣例)。

---

## 5. 需要你決策的兩件事

### 5.1 預設要不要走 Router?

| | 直接指定模型 | 經 Router(現行預設) |
|---|---|---|
| 延遲 | **6s** | **64s** |
| 透明度 | 無決策鏈 | 11 步分析可展開 |
| 自動分派 agent | ✗ | ✓ |

我的看法:**保留 Router 但不要當唯一預設**。可行方向:
- 讓使用者記住上次選擇(現在每次新對話都回到 `auto`)
- 或先用小模型/非 reasoning 模型做路由決策,直答才用 gemma —— 路由決策只需要輸出一個 JSON,不需要 12B reasoning 模型
- 或在 UI 上明示「auto 模式會多花時間換取自動分派」,讓使用者自己權衡

第二點我認為是最好的解:**把 Router 的路由決策模型與直答模型分開設定**。這樣既保留透明度,又把 58 秒砍掉大半。

### 5.2 Router 的 token 預算(你昨天提的)

實證確認你的判斷完全正確,而且問題比表面更深:

1. **Router 的 inference payload 只有 `model`/`messages`/`stream`,沒有 `max_tokens`**([`csp_registry_client.py:816-822`](packages/anila-core/src/anila_core/router/csp_registry_client.py#L816))。Router 從不宣告自己要多少輸出空間。
2. **`model_registry.context_window` 欄位早就存在**([`model_registry.py:153`](services/csp/app/models/model_registry.py#L153)),API 也回傳它,但**除了顯示之外沒有任何地方在用**。
3. `anila-core` 甚至有完整的 `auto_compact`(算 effective context、compact 門檻),但**沒有呼叫端把 registry 的 `context_window` 餵進去**——工具打好了,線沒接。

**實測證明這條線斷掉的後果**:我先前用 `-c 8192 --parallel 8` 起 llama-server,每個 slot 只有 **1024 tokens**,Router 的路由決策就完全失敗(「目前無法安全判斷是否需要 Agent」)。改成 `-c 32768 --parallel 4`(每 slot 8192)後,gemma 立刻正確輸出:

```
content : '{"decision":"DIRECT_ANSWER"}'
finish  : stop | tokens: 359
```

**所以「reasoning 模型不能當 router primary」是假的**——它可以,只要 token 預算足夠。真正的問題是平台沒有把「模型的真實容量」納入預算計算,於是使用者丟一份論文就會撞牆而且**錯誤訊息完全看不出原因**(只說「無法安全判斷」)。

建議的修法(需動 `anila-core`,屬跨切面核心改動,我沒有擅自動):
1. 註冊模型時要求填 `context_window`(或從上游 `/v1/models` 自動探測)
2. Router/proxy 依它計算輸入預算與 `max_tokens`,接上既有的 `auto_compact`
3. reasoning 模型額外保留思考空間(實測 gemma 光路由決策就要 359 tokens)
4. 錯誤訊息要能區分「模型拒答」與「token 不夠」

---

## 6. 我自己的量測錯誤(誠實交代)

這份報告的第一版數據是錯的,修了三輪:

| 輪次 | 我的錯 | 修正後 |
|---|---|---|
| 1 | 判定「對話 5s PASS」——但條件匹配到**使用者自己輸入的「模型」二字**,截圖顯示當時還在「思考中」 | 改成「文字差異比對 + 停止鈕消失」 |
| 2 | 判定「對話 FAIL(318s 無內容)」——但截圖顯示回應其實成功了,是我的 assistant 選擇器抓錯容器 | 改成比對送出前後的 body 文字差異 |
| 3 | 「已封存 FAIL」——實際文字是「已封存 **2**」(含計數),我用 exact match 匹配失敗 | 功能正常,是我的判定太嚴 |

另外深度互動測試第一次卡死在 overlay 遮蔽點擊,改用 `force: true` 才通。

**這三次錯誤的共同點**:我先寫判定條件,再拿它當結論,而沒有先用截圖交叉確認。後來每個結論都改成「截圖或原始輸出佐證後才寫入」。

---

## 7. 本次未涵蓋

- Citations 相關度與同檔分片分組(需要先建知識庫並跑 RAG)
- Artifact 版本歷史 UI(在 anilalm,`https://localhost:8443/lm/`,本次未測)
- 標籤的實際新增/移除/篩選(只驗證入口可開)
- 命令面板 Enter 跳轉的實際目標對話(有跳轉但未驗證跳對)
- 74 個按鈕的逐一點擊(OpenWebUI 那份做了 21 個;ANILA 這邊只做了功能導向的關鍵路徑)
- 響應式/行動版、i18n(盤點報告已標為未實作)

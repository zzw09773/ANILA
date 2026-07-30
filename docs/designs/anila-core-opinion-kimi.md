# anila-core 路由設計與 wt/core-opt 獨立評估 — kimi

> 2026-07-30。依據:重啟樹本體(`restart/from-redesign`)、`wt-coreopt` 工作樹未提交 diff、
> 優化報告 `w-coreopt-report.md`、CSP `proxy.py` / `auth_service.py`、shell `app.jsx`/`chat.jsx` 實碼。
> 三模型獨立作答,未揣摩其他模型立場。

---

## 0. 先修正題目的測量框架(這影響後面所有答案)

「每則訊息都付兩輪 LLM」**只對 dispatch 路徑成立**。看實碼:

- 非串流 direct answer:sentinel 那一輪的輸出**就是**最終回答(`router_server.py` 非 dispatch 分支直接
  回 `llm_text`)。單輪,不是兩輪。
- 串流 direct answer:Plan C tail-buffer 在偵測窗後即時逐 token 轉發,同樣單輪,只有 TTFT 多一個偵測窗。
- 只有 dispatch 是真的兩輪(路由輪 + agent 輪),multi-turn 才是 N+1 輪。

所以真正的問題不是「每則兩倍成本」,而是「**被分派的訊息**多付一整輪 primary LLM,且所有訊息的
TTFT 多一個偵測窗」。決策前應該先看現場 dispatch 率——若大部分訊息是 direct(開放式聊天通常如此),
平均額外成本遠低於題目暗示的兩倍。題目把「路由 overhead」和「dispatch overhead」混為一談。

---

## 1. 每則 LLM 路由是對的設計嗎?——**作為 fallback 對,作為唯一路徑錯**

**為什麼 LLM sentinel 本身該留:**

- agent 數量是個位數到十幾個,`_ROUTER_SYSTEM_TEMPLATE` 的 agent list 很短;讓 primary model
  「順便」路由,邊際 prompt 成本接近零,且路由品質直接跟著主模型升級走,不用另外調。
- 規則/embedding 預分類器省下的是延遲不是錢,代價是多一個會漂移的判準:agent description 一改、
  threshold 就要重調,而且「調錯」沒有警訊。對一人維運,這是每週都在的東西。小型分類模型
  (.12 上有 gemma4)同理——多一個要養的元件,換來的只是把已經很便宜的路由輪變便宜一點。
- 題目說「不要 consider caching 式的答案」——我的具體替代方案如下,但結論是:**它們都已經存在,
  問題是預設值,不是缺技術**。

**具體替代方案(按成本排序):**

1. **明式路由(已上線,零新成本)**。shell 已有 @mention 自動完成(`chat.jsx` 的 `@tok` 解析、
   `explicitAgents`、compare 模式)和 agent 下拉選擇;選定或 @ 之後,訊息走
   `cspBaseUrl/v1/chat/completions`,`model=<agent_id>`,**完全不進 Router、不付 sentinel**
   (`app.jsx:1311` 的 baseUrl 分支;CSP `proxy.py:729` 直接 `_resolve_agent` 分派)。
   換句話說「繞過 sentinel」的基礎設施已存在,Router 只服務「使用者沒指定」的開放式提問。
   Accuracy:明式指定是 100% 正確(使用者負責歧義,不是模型)。這應該是主推路徑。
2. **Sticky conversation routing(小改,值得排進 P 系)**。對話綁定上一輪 dispatch 的 agent
   (shell 已有 `updateConversationAgent` / `routedAgentId` 的概念),無 @ 訊號的 follow-up 直接
   送回同一 agent,省掉 sentinel 整輪。Accuracy 成本=話題漂移時誤留,但使用者一句 @ 即修正,
   且 OPT-5 的 route meta 讓誤留可觀測。這是把「established conversation」變成確定性規則,
   而不是讓 LLM 每輪重新猜。
3. **Embedding/小模型預分類:不推薦**(理由如上,維運稅 > 收益)。

**給擁有者的每週成本**:方案 1 = 0(已存在,頂多是 UI 預設值與引導文案);方案 2 = 一次性 1–2 天
+ 之後每週 0;方案 3 = 每週都要看一眼的調參債,與「不要越用越嚴格/越養越重」直接衝突。

---

## 2. 何時可以安全略過 sentinel

| 候選 | 判定 | 理由 |
|---|---|---|
| @ 明式指定 | **已安全略過(現況)** | 前端確定性解析,直達 CSP,不進 Router。不用再「允許」,已在發生。 |
| established conversation 的 follow-up | **條件式安全** | 只有「對話已綁 agent ∧ 本則無 @ 切換訊號」時安全。風險=話題漂移誤留。必須有 escape hatch(@ 或 UI 切換),否則就是把一種錯誤換成另一種更難發現的錯誤。 |
| 琐碎訊息「好」「繼續」 | **看起來最安全、其實最不安全** | 短訊息是最依賴上下文的。「繼續」出現在 agent 任務中段時,正確路由是**回到同一 agent**;若為了省事把短訊息導去 direct answer,會得到答非所問。若要規則化,方向應相反:短訊息優先走 sticky agent,而不是走 direct。 |

**怎麼知道在生產上搞錯了**:OPT-5 的 `route.decision` 就是觀測面——`route_miss`/`dispatch_error`/
`salvaged` 已經可計數。補兩個代理指標:(a) 同一 conversation 30–60 秒內重送語意相同訊息
(=使用者覺得答錯了);(b) 「換 agent 重答」按鈕點擊(`correctable:true` 已鋪路,UI 未做)。
取樣人工稽核每週 10 筆 route meta,一人維運負擔 < 15 分鐘。沒有這層觀測就上 sticky routing,
等於盲飛。

---

## 3. wt/core-opt 逐項審查

**OPT-1(共用 httpx pool)— 收。** 標準做法,成功路徑不變。三個注意:
- `max_connections=100`:每條 in-flight SSE stream 佔一條連線。3000 使用者的串流尖峰若同時數百條,
  會在 `pool=5s` 上排隊然後**把「慢」變成「錯」**。上線前用預估併發對一下這個數字,必要時調高——
  這是容量問題不是正確性問題。
- `get_http_client()` 的註解說 create-race 時 "the loser closes its extra client",實碼是 `pass`
  直接丟棄。comment 與 code 不符,小事,但反映這段沒被仔細讀過。
- `reset_http_client()` 不 await close(測試用),註解有坦白,可接受。

**OPT-3(JWT fingerprint cache)— 收,但撤銷故事要改寫,因為實際比報告說的好。**
關鍵事實鏈:Router 把同一支 JWT 原樣轉發給 CSP `/v1/chat/completions`(`router_server.py` 的
`_call_llm_non_stream`/`_stream_llm_sse` 都帶 `Bearer <caller_api_key>`),而 CSP 端
`get_caller` → `_load_user_from_payload` **每個請求**查 DB 驗 `tv == user.token_version` 與
`is_active`(`auth_service.py:87`)。所以:

- 被撤銷(tv bump)或停用的使用者,即使 owner cache 在 30s TTL 內放行,**sentinel 那一跳照樣 401**,
  走 llm_error 的 graceful fallback——**拿不到服務**。題目擔心的「revoked user might still be
  served」在 chat 主路徑不成立。
- 真正的殘餘窗口只有一個:**`/v1/sessions/{id}/state` 是純本地 SQLite 讀取,不打 CSP**。被停用者
  在 ≤30s 內還能讀**自己**的 session 快照。危害≈零(自己的資料、唯讀、30 秒)。
- TTL 30s ≪ access token 60 分鐘,cache 命中的 token 幾乎必然密碼學有效;cache 只存成功的 /me 解析,
  不會把 401 快取成通行。
- 若日後想拉長 TTL:CSP 已有 Redis revocation pub/sub(`token_revocation_publisher.py`,
  anila-studio 已訂閱),Router 接同一個 channel 做主動失效即可。**現在不需要**,30s 已經被 CSP
  端每請求 tv 檢查兜底。報告的風險段應降級為「30 秒內可讀自己的本地 session state」。

**OPT-4(preflight 平行化)— 收。** 安全順序不變(ownership 檢查仍在 identity resolve 之後)。
`asyncio.gather` 預設第一個例外即傳播、另一支繼續跑;`ensure_fresh` 內部已 catch 所有例外,
無 orphan task 問題。

**OPT-5(route meta)— 收,長期價值最高的一項。** additive、舊前端忽略;`agent_id` 是名稱不是
端點位址,不踩 P4.6 的位址遮蔽雷區。它是上面所有「怎麼知道搞錯」的基礎設施。

**OPT-2(url_guard DNS TTL cache)— 建議砍掉。** 這是唯一一項把安全相關模組的語意放寬的改動:
原本「每次即時解析」變成 30 秒陳舊視窗(TOCTOU 窗口延長),還新增 negative caching(gaierror 也
快取 30s)。收益?報告自己量的:warm 0.13→0.11 ms,而且 **guard 不在 Router 熱路徑**(註冊時才跑)。
在 SSRF guard 裡加狀態換量測雜訊級的收益,違反「不值得其複雜度」。它是「可獨立還原」沒錯,
但更好的處置是根本不進。砍掉它也順便砍掉一個未來審查者每次都要重新論證的窗口。

**OPT-6(拆 connect timeout)— 收。** 內網 3s connect 綽綽有餘,死掉 upstream 從卡 120s 變 3s
是純改善。

**整體**:五收一砍。全部可獨立還原的紀律很好,`OPT-N` 標記讓還原成本接近零——這正是
「一人維運」該有的改動形狀。

---

## 4. 大家都沒想到的(我沒想到的內容)

1. **RemoteAgentRegistry 無界增長——整個區域唯一會隨 uptime 惡化的東西,而且不在 branch 上。**
   `_agents_by_key` 以 `sha256(api_key)` 為鍵(`remote_agent_manifest.py:62`),JWT 每 60 分鐘
   rotate,**每個新 token 產生一個新 entry,永不淘汰**。3000 使用者 × 每天多次 refresh,
   Router 長跑就是緩慢記憶體漏;同時每個新 token 都視為 stale → 每次 token refresh 多打一次
   `/v1/agents`。OPT-3 的 JWT cache 有 4096 上限,registry 沒有。修法一舉兩得:registry 改以
   OPT-3 產出的 owner hash 為鍵(同一人換 token 不再新 entry)+ LRU 上限。這比 OPT-2 值得做,
   也應該擋在 merge 前或緊跟在後。
2. **`has_system` 分支是未文件化的 load-bearing 行為。** 呼叫方帶任何 system message,router
   prompt(agent list + DISPATCH 規則)整個被丟棄(`router_server.py:735-739`)。shell 的對話標題
   產生器正好**依賴**這個行為:它把 title-generator system prompt 送進 Router 以避免 dispatch
   (`app.jsx:973`)。後果雙向:未來任何在前端加 system message 的功能會靜默關掉路由;反過來,
   任何「修好」這個分支的人會弄壞標題產生器。至少寫進註解;更乾淨是標題生成改走直達 CSP。
3. **「路由設計對不對」的答案有一半在前端預設值。** Router 是可繞過的,CSP `model=<agent>` 本就
   直達。討論 sentinel 存廢而不討論「為什麼預設路徑是不指定 agent」,是在錯誤的層次爭論。
4. **CSP 端每請求一次 DB user 查詢是正確但浪費的結構。** `get_caller` 每跳都查;Router→CSP 的
   me/agents/chat 三跳各付一次。OPT-3 只在 client 端省了一跳;若要再省,在 CSP 端做 per-request
   identity 記憶化比讓 N 個 client 各做 N 個小快取划算。但這是「正確而浪費」候選——不做也活得
   很好,列在這裡是因為它是下一次優化衝動來時該先看的地方。
5. **SQLite session 單一共用連線**(報告已列,不重複)加上第 1 點,是 Router 唯二「隨時間惡化」
   的東西;其餘都是常數成本。一人維運的優先序應該永遠先排「會惡化的」,再排「常數的」。
6. **OPT-1 的 pool=100 是第一個容量天花板**(見 §3),它不是 bug,但它是 8 月底上線前唯一需要
   用預估併發對過一次的數字。

---

## 5. 總結建議(給擁有者的一頁)

1. **merge OPT-1/3/4/5/6,砍 OPT-2。**
2. **同批或緊接著修 RemoteAgentRegistry 的無界增長**(鍵改 owner hash + 上限)——這是唯一
   隨 uptime 惡化且目前無人認領的。
3. **路由策略本身不動**,把「明式路由已存在」變成產品事實:UI 鼓勵選/@ agent,sentinel 繼續當
   開放式提問的 fallback。sticky routing 等 route meta 有兩三週觀測資料再說。
4. OPT-3 的撤銷恐懼降級記錄:CSP 每請求 tv 檢查是真正的閘,Router cache 的 30 秒窗口只剩
   「讀自己的本地 session state」。
5. 每週維運成本:≈0 新增;route meta 取樣 15 分鐘。

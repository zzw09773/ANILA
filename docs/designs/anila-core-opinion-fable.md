# anila-core 路由設計與 wt/core-opt 分支獨立意見（fable）

依據：實讀 `/home/c1147259/桌面/ANILA/anila-restart-20260729/wt-coreopt`（分支零 commit，全部是工作樹未提交變更，與報告自述一致）＋主樹 CSP 認證碼。

## Q1 每則訊息過一輪 LLM 路由，對嗎？

**對，而且題目的前提有錯。**「每則訊息都付兩輪 LLM」不成立：

- **直接回答路徑只有一輪**。非 dispatch 時，sentinel 那輪的輸出本身就是最終答案
  （`router_server.py` 非 dispatch 分支直接回 `llm_response["content"]`；streaming 路徑
  註解明寫 "Direct answers are forwarded chunk-by-chunk in real time"，邊生成邊送）。
  這與任何普通聊天機器人的成本相同。
- **只有 dispatch 訊息才是兩輪**，且第一輪是「prefill 重、輸出一行」的短輪
  （輸出僅 `DISPATCH:agent:query`）。真正讓第一輪變貴的是 reasoning 模型的
  chain-of-thought（程式碼裡有 sanitize reasoning、`_DISPATCH_EMPTY_RE` 搶救邏輯，
  證明 CoT 確實在發生）——「每則都先想很久」的元凶是 CoT 長度，不是「兩輪」結構。

替代方案評估（給結論不給選項堆）：
1. **關鍵字／embedding 前置分類器**：在 CJK 軍規領域準確率會掉，且多一個要調校的
   元件——違反單人維運鐵律。不採。
2. **獨立小分類模型**：air-gapped GPU 上多養一個模型、多一條部署線。不採。
3. **UI 明選 agent（@ 或下拉）＋ LLM 路由當預設 fallback**：零準確率代價、零維運
   成本、使用者可自救。**採，這是唯一值得做的結構性補充。**

維運成本：方案 3 是 shell 前端一次性工作，之後每週 0 小時。

## Q2 何時可以安全跳過 sentinel

- **`@` 明選 agent：安全**。使用者意圖顯式，只需保留 registry 可見性檢查
  （該 agent 必須在 `registry.get(caller_api_key, agent_id)` 裡）。可做。
- **後續輪沿用上一個 agent：看似安全，其實不是**。話題轉彎（「謝謝，另外問…」）
  會被錯釘。可接受的形式是 **sticky-with-escape**：預設跟隨 pinned agent，但
  dispatch_error 或使用者按「換一個」即重跑 sentinel——OPT-5 的
  `route.correctable` 正好是這個機制的地基。
- **「好」「繼續」瑣碎訊息：不安全，且是陷阱**。「繼續」在 dispatch 中的 session
  必須進 agent（續跑），在 direct session 該進 router；決定去向的正是上下文——
  跳過 sentinel 等於偷渡了 sticky 規則。不要做關鍵字清單。
- **怎麼知道錯了**：沒有 OPT-5 之前錯誤分派對使用者是隱形的（`_dispatch_safe`
  失敗回一句像正常助手的話）。上線後看 `route.decision` 分佈：
  `dispatch_error` 率、`route_miss` 率、以及（做了換agent按鈕後）使用者更正率，
  按「是否跳過 sentinel」分桶比較。更正率上升＝跳過規則錯了。

## Q3 分支審查

**總評：值得留，六項全部方向正確、可獨立還原、零新增必要設定（符合「不要越用越嚴」）。
merge 前修兩件事，另有一個被高估的風險要正名。**

1. **OPT-3 JWT 快取的撤銷風險被題目高估了——快取不是授權閘。**
   Router 每次真正呼叫 CSP（sentinel LLM、dispatch、resume）都轉發使用者原始 JWT
   （`router_server.py:2106/2247/1520`），而 CSP 每次驗 JWT 都比對 DB 的
   `token_version`（`services/csp/app/services/auth_service.py:87`）。被撤銷的使用者
   在 TTL 窗口內**拿不到任何模型輸出**——請求會在 CSP 401 掉。
   真正的殘餘窗口只有一個：`GET /v1/sessions/{id}/state`（router_server.py:1346 起）
   只靠快取的 fingerprint 認證，被撤銷 token 可在 ≤30s 內**讀 session 歷史**。
   內網環境可接受；便宜的收口法＝任何下游 CSP 呼叫對該 token 回 401 時順手清掉
   該筆快取。不必為此擋 merge。
2. **要修（一）：共用連線池的上限是本部署唯一「錯的預設」。**
   `http_pool.py:53-56` 定死 `max_connections=100, pool=5.0`，而 stream 會抱著連線
   最長 120s。改動前每請求新 client＝沒有全域上限；改動後 3000 人尖峰若同時
   >100 條生成，第 101 條 5 秒後吃 PoolTimeout——**優化引入了原本不存在的失敗
   模式**。修法：上限拉高並改成 env 可調（與 OPT-6 同風格），一行事。
3. **要修（二）：OPT-4 讓未認證請求汙染全域 registry 錯誤狀態。**
   改成 gather 後，壞 JWT 的請求也會觸發 `ensure_fresh`（改前會先死在 `/me`）；
   失敗寫進**全域** `_last_refresh_error`（`remote_agent_manifest.py:92`），接著別的
   健康使用者的 trace 會顯示「registry refresh 失敗」（`router_server.py:831-838`）。
   修法：401 類失敗不寫全域錯誤，或錯誤改 per-key。
4. 小疵：`http_pool.py:59-63` 的 create-race 分支是死碼——註解說「輸家丟棄自己的」，
   實際永遠覆寫 `_client`，被棄的是先建的那個且未 close。單 event loop 下實際
   打不到，修註解或修邏輯皆可。
5. OPT-2 附帶了負向快取（gaierror 記 30s）：guard 對解析失敗本來就是放行，
   語意不變，可留。OPT-5 是全包最便宜的贏面，必留。OPT-6 connect=3s 對內網正確。

## Q4 大家沒想到的

- **「兩輪」前提本身**（見 Q1）——優化報告與題目共享了這個錯誤框架；
  真正該量的是「dispatch 佔比 × 第一輪 CoT token 數」。
- **sentinel 輪的 token 帳**：routing_messages 帶完整對話史，每輪成本隨對話長度
  線性長；接上計量後 sentinel 輪要不要計入使用者用量是舊帳
  （router sentinel 用量抑制＝計費地雷），OPT 分支沒碰、PLAN 也還沒排。
- **quick win 沒人提**：路由輪與直答輪共享同一呼叫，所以不能單獨 cap 路由輪的
  max_tokens——但可以對 reasoning 模型設 reasoning effort 下限檔。若擁有者覺得
  「開場想太久」，先試這個再談改架構。
- **`clear_jwt_owner_cache` 沒有 production 觸發面**：要立即撤 fingerprint 只能
  重啟 router。因為 CSP 才是授權閘，這只影響 session-state 讀取窗口，記錄即可。
- **維運週成本**：本分支全部 env 可調、零必要新設定、零新啟動閘——完全符合
  「不要越用越嚴」。merge 後每週成本 ≈ 0。

## 給擁有者的第一步

先照報告的 throwaway 容器親試（尤其連發兩則感受 OPT-3），**merge 前只要求修
連線池上限與 OPT-4 錯誤汙染兩件事**；路由架構不要動，只補 UI 明選 agent 與
sticky-with-escape，兩者都以 OPT-5 的 route 欄位為地基。

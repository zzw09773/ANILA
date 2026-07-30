以下是完整意見(我已讀過 `router_server.py`、`remote_agent_manifest.py`、`http_pool.py`、`url_guard.py`/`upstream_urls.py` 的 diff,以及 `w-coreopt-report.md`)。

---

# GLM 對 anila-core 路由設計與優化分支的獨立意見

## 問題 1:每則訊息都跑 LLM 路由,在這個部署情境下是對的設計嗎?

**不對。** 對「3000 名機構使用者、air-gapped、少數幾個 agent、單一維運者、8 月底上線」這個 profile,把 LLM 當作每則訊息的第一級路由器是結構上錯誤的——不是因為它不優雅,而是因為它把成本放錯了地方。

關鍵觀察來自程式碼本身,不是來自量測:`router_server.py:120-176` 的 `_ROUTER_SYSTEM_TEMPLATE` 把**完整 agent 清單**塞進 system prompt,然後每一則訊息都跑一整輪 primary LLM(`_call_llm_non_stream` 在 `router_server.py:826`、串流版 `_stream_llm_sse` 在 `2119`)只為了產生 `DISPATCH:<agent>:<query>` 或直接回答。然後真正的答案才開始。所以分派情況下每則訊息 = 2 輪 LLM;`_multi_turn_dispatch`(`router_server.py:1738`)更糟,每個迭代再叫一次 sentinel,3 輪 multi-turn = 6 輪 LLM。owner 的 framing「兩輪 LLM」對 multi-turn 是樂觀的。

結構性問題:router 用的 sentinel **就是同一個 primary 模型**。你付錢讓模型讀自己的 agent 清單、決定、然後再付一次讓它回答。agent 清單小時這還好,但成本隨 agent 數線性成長,而且每一則訊息都付。在共享內網 GPU(gpt-oss-20b/gemma4)上,這同時是 latency 問題 **和** throughput 問題——每則訊息吃掉 2x 模型容量,3000 人共用時這會變成排隊。

**我會換成的設計(分層路由,LLM 只當殘差路徑):**

1. **顯式定址直接跳過 sentinel**:`@agent-id` 語法 → 直接 dispatch,零 LLM。這是所有 chat-bot 平台的做法,零路由準確度損失(使用者已經告訴你了),而且是最常見的進階使用者行為。失敗模式只有拼錯 → 退回 sentinel 帶「你是不是指…」提示。
2. **Session 黏著(latch)**:一旦某對話分派給 agent X,下一則預設走 X,除非使用者明確切換或訊號明顯換主題。**這個概念 owner 已經有了**——G9 的 agent latch(`1b05ea9`)和 OW-1 的 active-leaf 都在做類似的事。把它拉到 router 層當預設,common follow-up 就免 sentinel。
3. **關鍵字/觸發詞第一層**:每個 agent manifest 已經有 description;在註冊時加 3-5 個 trigger 關鍵字/regex。「顯示參數表」→ asrd,直接 dispatch。處理掉 deterministic 的 80%。
4. **LLM sentinel 只跑殘差**:沒有顯式定址、沒有 active latch、沒有關鍵字命中 → 才跑現在這個 LLM sentinel。這時它才真的在處理它擅長的(真正模糊的查詢),而且變成少數情況。

**路由準確度的成本**:顯式和 latch 路徑是 100%(使用者/上一輪告訴你的)。關鍵字路徑準確度取決於 trigger 寫得好不好——但 maintainer 能在 trace 看到 miss(OPT-5 的 `route.route_miss` 已經暴露了),加 trigger 即可。殘差 LLM 路徑準確度跟今天完全一樣。所以這個提案在 LLM sentinel 目前處理得好的 case **不損準確度**,在它處理得差的 case(「繼續」「好」這種 trivial 訊息目前要付一整輪 LLM)**同時提升準確度與 latency**。

**對 owner constraint 的權衡**:這個提案把一點點摩擦從「每次使用」移到「掛 agent 那一刻」——dev 註冊 agent 時要寫 3-5 個 trigger 詞。這正好是 owner 說的「開發者掛 agent」的摩擦,但它是**一次性的、在 attach 時**,不是每則訊息。而它移除的是每個使用者每則訊息的雙倍 LLM 稅。owner 的原話是「不要把系統越用越嚴格…開發者不會想掛 agent」——分層路由不增加治理嚴格度,只增加 dev 在註冊表單上寫幾個詞。**對 maintainer 的每週成本:~0 分鐘**,除非有新 agent 掛上來,那時是 ~5 分鐘審 trigger 詞。LLM-sentinel fallback 仍在,所以沒有新的維護負擔。

我對 framing 的一個直接反對:報告把 sentinel 描述成「a complete round of the primary model」,但沒有點名結構性問題——**你付同一個模型讀自己的 agent 清單再決定,再付它回答**。把這講清楚,「要不要動路由策略」就不是品味問題,而是「你願不願意讓使用者每則訊息為 router 的優雅付雙倍 GPU 時間」。

---

## 問題 2:如果保留 sentinel,哪些情況可以安全跳過?

- **`@agent-name` 顯式定址:安全。** 使用者已經消歧。唯一失敗是拼錯/不存在的 name → 退回 sentinel 帶提示,或直接 404 dispatch。零 LLM 成本,這是最大且最安全的勝利。**這個我會第一個做,跟優化分支無關。**

- **已建立對話的 follow-up turn:大部分安全,但有一條銳利的邊。** latch 預設 hold,除非訊號換主題。「繼續」「好」「嗯」「再說一次」→ 安全 latch。「那另外一個問題…」→ 不安全,使用者換主題了。陷阱在這:換主題偵測本身是 LLM call,會抵銷跳過 sentinel 的省下的時間。所以實用規則是:**短訊息(≤ N 字元,或匹配 continuation regex 如 `繼續|好|嗯|再|還有`)預設 latch;長訊息或以疑問詞開頭的訊息重跑 sentinel。** 邊界模糊沒關係——錯 latch 的成本是「一則 agent 回覆不對,使用者打 `@別的agent` 修正」,可恢復。

- **Trivial 訊息(「好」「繼續」):安全跳過 sentinel,而且可以連 dispatch 都跳過。** 對 Router-direct 答案的「繼續」= Router 直接接著答(同一個 answerer,沒有 dispatch 決策);對 agent 的「繼續」= dispatch 給同一個 agent,query 帶「繼續」。**看起來安全但其實不是的情況:** 「繼續」對一個 multi-turn dispatch 中間的 turn——它可能意味著「上一個 agent 繼續」,也可能意味著「router 繼續合成」。`_multi_turn_dispatch` 的 sentinel 是在判斷要不要再 dispatch 下一個 agent,跳過它會讓 router 失去 multi-agent orchestration 的能力。所以 trivial-skip 只在 **single-shot(非 multi-turn)** 路徑安全。

**怎麼在 production 知道自己判錯了:** OPT-5 的 `anila_meta.route.decision` 是正確的 hook。加 `decision=latched`、`decision=explicit`、`decision=trivial_skip`。然後 maintainer 可以 grep trace 找「latched 之後下一則訊息以 `@` 開頭」的 case——那就是 miss 訊號。沒有 trace visibility 就盲飛;**OPT-5 是任何 skip 規則的前置條件**,應該最先合。一個更便宜的 proxy:latched dispatch 後使用者的下一則如果以 `@` 開頭,大概率 latch 錯了,計數即可。

---

## 問題 3:優化分支的審查

整體紀律好:每個改動獨立可還原、有量測、有標籤。逐項:

**OPT-1(共用 httpx pool):正確、低風險、值得。** `http_pool.py:48-62` 的 create-once race 註解誠實說明不是 thread-safe,對單 event-loop FastAPI 沒問題。一個 nit:`reset_http_client` 不 await aclose,prod 中若被呼叫會洩連線——但它只在 `create_router_app`(prod 一次)被叫,OK。

**OPT-3(JWT fingerprint cache):這是唯一有真正 invalidation 故事的一項,報告低估了它。** 報告說「revoked token 最多在 Router 內多活 30s」。正確,但對**軍方平台**,30s 的 post-revoke 存取窗口是實實在在的。Cache key 是 `sha256(token)`(`router_server.py` diff 中的 `_jwt_cache_get`),所以**新** token(post-bump)拿新 entry,沒問題;問題在**舊** token:Router 會在 user 被停用後繼續用 cached 的 `/me` identity 服務最多 30s。

可選的緩解:
- 縮短 TTL 到 5s——成本是更多 `/me` 呼叫,但對 40 則對話仍是 ~8x 降幅(40→8)。
- 訂閱 CSP 既有的 revocation cache(Redis)——但那是 operational complexity,正是 owner 不要的。
- 折中:cache 同時存 `token_version`,hit 時做一次便宜的 version-only 查詢——但那還是 CSP RTT,抵銷 cache,不值得。

**我的建議:30s 對軍方平台太長。設 `ANILA_JWT_OWNER_CACHE_TTL=5`。** 5s 的窗口跟 CSP 自己驗 JWT 的 latency budget 同數量級,而 8x 降幅幾乎全保留。在 env var 描述裡寫明這個 tradeoff。`clear_jwt_owner_cache` 是手動逃生口——maintainer 停用 user 後若要立即生效,重啟 router 或等 TTL。這對單一操作者可接受,但**要寫進 runbook**,不能埋在 code comment 裡。

**OPT-4(preflight 平行化):正確、安全。** 安全順序保留(`ensure_session_owner` 仍在 `gather` 之後,`router_server.py:771-786`)。唯一微妙處:`registry.ensure_fresh` 現在在 `session_factory` 設定時也跑(`else` 分支)——是測試行為變更,報告說測試通過,假定 OK。

**OPT-5(route visibility):純 additive、極低風險,而且是 Q2 任何 skip 規則的前置條件。保留。**

**OPT-2(DNS TTL cache):這是我會 push back 的一項。** 風險是 SSRF 的 TOCTOU 窗口:url_guard 拒 private IP,但 30s DNS cache 期間,一個原本解到 public IP 的 host 可能被 rebind 到 private IP 而 guard 不會重查。報告自己承認「TOCTOU 窗口與文件原本承認的一致,只是略延長」——但 guard 的整個存在意義就是當 SSRF backstop。**而且 win 是「~0.13 → 0.11 ms」——這是 noise。** 對單一維運平台,加一個需要被論證的 TOCTOU 表面,換一個使用者永遠感覺不到的節省,不划算。**我會丟掉 OPT-2,或至少縮到只 cache `ANILA_TRUSTED_HOSTS` 裡的 host(CSP、model gateway),非信任 host 不 cache。** 這是「abstraction costing more than it earns」的教科書案例。

**OPT-6(拆 connect timeout):3s connect 預設對 air-gapped 網路偏激進,模型 gateway 在負載下握手可能 >3s。** env override 存在所以沒死,但**預設應該是 5s**,註明 3s 是給 well-provisioned LAN。3s 的風險是負載下的 false-failure,maintainer 要診斷——值得保留但 bump 預設。

**整體:** 我會 as-is 合 OPT-1、OPT-4、OPT-5。OPT-3 只在 TTL 降到 5s 且 revocation-window 寫進 runbook 的前提下合。OPT-2 縮到 trusted-hosts-only 或丟掉。OPT-6 bump 預設到 5s。

---

## 問題 4:大家都漏掉的事

1. **Sentinel 跟 answerer 是同一個模型——這是結構性浪費,不是快取問題。** 報告把 sentinel 描述成「a complete round of the primary model」,但沒點名:你付模型讀自己的 agent 清單、決定,再付它回答。agent 清單小時還好,但這是 O(agents) per message 的 router 工作,且隨 agent 數線性成長。Q1 的分層方案是**結構性**修這個,不是靠快取。

2. **System prompt 每則訊息重新 render 且重新 tokenize。** `_ROUTER_SYSTEM_TEMPLATE.format(agent_list=...)` 每則都跑(`router_server.py:728` 附近),完整 agent 清單每則都送。agent registry 有 60s TTL 快取(`remote_agent_manifest.py:40`),但 **system prompt 沒有**——每則重新 render、重新 tokenize。本地模型對同一個 1-2KB prefix tokenize 40 次/session 是實實在在的時間。一個以 agent-list hash 為 key 的 tokenize cache 會省真實毫秒。沒人量到它,因為它藏在 LLM call 內。這是「正確但浪費」。

3. **`_multi_turn_dispatch` 每個迭代再跑一次 sentinel LLM。** `router_server.py:1812` 的 `next_llm = await _call_llm_non_stream(caller_api_key, convo)`。3 輪 multi-turn = 6 輪 LLM。owner 的「兩輪 LLM per message」對 multi-turn 是樂觀的,而 multi-turn 是 router 的賣點之一。

4. **SQLite session store + 模組級 `_conn_cache` 是 single-writer 瓶頸。** 報告標為「架構票」。但對 3000 人單盒,這不是理論——session 表的並發寫會 serialize。**Postgres 已經在 stack 裡(CSP 用)。把 session store 走既有 Postgres + `csp_app` role,移掉一整個 maintainer 要 reason about 的子系統(SQLite 檔案位置、權限、備份)。** 這是「為了 router 自主性而獨立 SQLite」的抽象,對單一操作者成本 > 收益。

5. **Salvage paths(`_DISPATCH_EMPTY_RE`、`_sanitize_leaked_thought` 在 `router_server.py:207, 230`)是 model-specific 補丁。** 正確,但會 accrete:每個新模型可能需要新的 salvage path。owner 的「別讓我越維護越嚴格」反過來適用這裡——router 已經滿是 model-quirk workaround,maintainer 換模型時要 debug。一個 per-model 「quirks」config(或接受 misbehaving 模型路由變差)更簡單。這是「對單一操作者部署的預設錯誤」——預設假設 maintainer 會持續 patch router。

6. **`_respond` 的串流假打字機(`asyncio.sleep(0.012)` per chunk,`router_server.py:1218`)對 non-streaming direct answer。** 正確(給 caller streaming feel),但它**對 direct-answer 路徑加 latency**——使用者等模擬打字。在一個抱怨「秒級等待」的平台上,加 0.012s × N chunks 的人工延遲正好走反方向。non-streaming 路徑應該一次送完整內容,讓前端 render。這是「正確但浪費」且直接抵觸優化目標。

7. **`docs_url=None`/`openapi_url=None` 在 diff 裡但沒標 OPT。** 它是 security fix(P2.3 per comment),接續上一段(被截斷處):

7. **`docs_url=None`/`openapi_url=None` 在 diff 裡但沒標 OPT。** 它是 security fix(P2.3 per comment),跟優化無關卻搭在同一條 branch 上。這沒有錯,但意味著 branch 不是純優化——它 bundle 了一個 security 變更。owner 應該被告知「revert OPT-N 不會 revert 這個」,以免他以為這條 branch 是乾淨的效能包。commit log 顯示 `4b0d291`/`3c7890d`「close the router's anonymous schema dump」可能已經在 main 上 separate merge——若是,worktree diff 看到它只是因為 worktree 落後。需要 owner 核對,別讓我過度宣稱。

---

## 一句話回答 + 最強反對 + 第一件事

**Q1 一句話:** 對這個部署 profile,每則訊息跑 LLM 路由是結構上錯的——它付同一個模型讀自己的 agent 清單再決定,再付它回答,把成本放錯地方;分層路由(顯式 `@`→ latch → 關鍵字 → LLM 殘差)在 LLM 擅長的 case 不損準確度,在它處理差的 case 同時提升準確度與 latency,且對 maintainer 每週成本 ~0。

**最強反對:** 報告把 sentinel 描述成「a complete round of the primary model」而沒點名結構性問題——**sentinel 跟 answerer 是同一個模型,你付它讀自己的 agent 清單、決定,再付它回答**,這不是快取能修的,是設計層級的浪費,而且 `_multi_turn_dispatch` 每迭代再跑一次 sentinel 讓「兩輪 LLM」對 multi-turn 變成 6 輪,owner 的 framing 樂觀了 3 倍。

**第一件告訴 owner 做的事:** 先合 OPT-5(route visibility),因為它是任何路由 skip 規則(`@` 顯式、latch、trivial-skip)的前置條件——沒有 `decision=latched|explicit|trivial_skip` 寫進 trace,你在 production 是盲飛,無法知道跳過 sentinel 是否判錯;有了它,才有可能安全地動路由策略,而動路由策略才是真正會讓使用者感覺到「秒級→毫秒」的那一刀,不是這條優化 branch 的任何一項 OPT。

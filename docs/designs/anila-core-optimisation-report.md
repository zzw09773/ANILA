# anila-core 深層優化報告（wt/core-opt）

> 分支：`wt/core-opt`　工作樹：`…/wt-coreopt`  
> 狀態：**刻意今晚不 merge**——給擁有者明天親跑、親感覺。  
> 量測日：2026-07-30　測試：router 相關 106 passed（本機）。

---

## Baseline

### 怎麼量

1. **Router 純開銷**（不含真實模型）：`httpx.ASGITransport` + `respx` mock CSP，LLM 瞬間回覆「直接回答」。每組 40 次取 median / p95。  
2. **真實 TCP**：對本機 `anila-restart` 的 `http://127.0.0.1/api/health` 讀取（只 GET，不 recreate、不改 compose）。  
3. **DNS / url_guard**：對 `example.com` / 內網 FQDN 重複呼叫 `validate_outbound_url`。  
4. **沒有**對 `.12` 模型 gateway 做 live 延遲基準（本機解析／連線條件與內網現場不同）；sentinel 成本改以程式結構推論＋mock 隔離。

### 數字（優化前）

| 路徑 | median | 備註 |
|---|---|---|
| Router sk-key（mock CSP） | **3.70 ms** | 每則：session + registry + LLM mock |
| Router JWT（mock CSP） | **6.90 ms** | 同上，但每則多一次 `GET /api/auth/me` |
| 40 則 JWT 對話的 `/me` 次數 | **40** | 每次訊息都打 CSP |
| 新 `AsyncClient` vs 共用（真實 TCP `/api/health`） | 1.17 → 0.46 ms | **每 hop 約 0.7 ms** |
| `validate_outbound_url(https://example.com)` | ~0.13 ms | OS 已 warm-cache DNS 時本身就很小 |
| **真正使用者感覺到的 wall-clock** | **秒級** | = primary 模型的 sentinel／路由 LLM 整輪呼叫（`_call_llm_non_stream` / `_stream_llm_sse`），不是上面這些 ms |

關鍵發現：所謂「sentinel」不是另一次小分類呼叫，而是**每一則使用者訊息都先付一整輪 primary LLM**（system prompt + agent 清單 + 使用者內容），再決定直接答或 `DISPATCH:`。Router 自身開銷在 mock 下只有數 ms；現場延遲幾乎都在模型。

---

## Per change

每個改動都標了 `OPT-N`，可單獨還原。

### OPT-1 — 共用 `httpx.AsyncClient`（連線池）

- **做什麼**：新增 `packages/anila-core/src/anila_core/http_pool.py`；Router → CSP 的 `/me`、`/v1/agents`、chat、agent stream、resume、`dispatch_tool`、`RemoteAgentRegistry` 改走 `get_http_client()`，不再每次 `async with AsyncClient`。  
- **Before / after**：真實 TCP 每 hop 1.14 → 0.46 ms（Δ **−0.68 ms/hop**）。一則 JWT 訊息典型 3 hop（me+agents+llm）可省 ~2 ms 量級；TLS 到遠端 gateway 時握手節省會更大（本機未 live 量 TLS）。Router mock 路徑 sk 3.70 → **1.21 ms**（與 OPT-3/4 合計）。  
- **風險**：低。成功路徑回應不變。測試重建 app 時會 `reset_http_client()`，避免 respx MockTransport 殘留。  
- **單獨還原**：刪 `http_pool.py`；各呼叫點搜 `OPT-1`，改回 `async with httpx.AsyncClient(...)`。

**2026-07-31 修正（合併前必要條件）**：第一版寫死 `max_connections=100` ＋ `pool=5.0`。
分支前每個 hop 各開一個 client，**沒有任何全域上限、也永遠不會排隊**；
共用池加上這組數字，等於在尖峰時親手製造一個舊碼做不出來的失敗
（實測：101 個並行請求 → 100 個成功、第 101 個等滿 5 秒後 `httpx.PoolTimeout`）。
三千人、一個維運，這是淨損失。

- 現在的預設是**不設上限**（`ANILA_HTTP_MAX_CONNECTIONS=0`）。理由不是「數字調大一點」：
  httpx **只有在 `max_connections` 是有限值時才會讓呼叫端排隊**，設 `None`
  等於從結構上讓 `PoolTimeout` 不可能發生。上限處的行為＝每個在途請求最多一個 socket，
  和分支前一模一樣；而且因為有 keep-alive 重用，**同樣負載下開的 socket 只會比舊碼少**。
  真正的天花板還是舊碼本來就有的那個：OS fd 上限與對端的 accept queue，
  兩者都以 `ANILA_HTTP_CONNECT_TIMEOUT`（3s）收尾，是既有的失敗模式。
- `ANILA_HTTP_POOL_TIMEOUT` 只有在維運自己設了有限上限時才有意義（預設不可達）。
- `max_keepalive_connections` 維持 20（可用 `ANILA_HTTP_MAX_KEEPALIVE` 調）：它是**保留**上限
  不是併發上限，超過只會讓那一 hop 退回分支前的成本，不會擋住任何人。
- 守衛測試：`packages/anila-core/tests/test_http_pool_limits.py`。

### OPT-3 — JWT → user fingerprint 短 TTL 快取（**最值得擁有者感覺的一項**）

- **做什麼**：`_resolve_session_owner_hash` 對同一 access JWT 在 30s 內不重打 `/api/auth/me`（key = sha256 of token；raw JWT 不進 map）。`create_router_app()` 時清快取，避免測試交叉污染。  
- **Before / after**：JWT mock 路徑 6.90 → **1.23 ms**；40 則 JWT 的 `/me` 從 40 → **1**。現場每則省 **一整次 CSP RTT**（本機 nginx health ~0.5 ms；容器內 CSP 通常 1–20 ms）。  
- **風險**：中低。撤銷後的 JWT 最多在 Router 內多活 `ANILA_JWT_OWNER_CACHE_TTL`（預設 30s）。**不比較嚴**：失敗仍走原本 401/502。  
- **單獨還原**：刪 `OPT-3` 區塊與 `_jwt_cache_*`；`_resolve_session_owner_hash` 恢復每次都 GET `/me`。

### OPT-4 — preflight 平行化

- **做什麼**：`asyncio.gather(_resolve_session_owner_hash, registry.ensure_fresh)`；session 擁有權檢查仍在 identity 之後（安全順序不變）。  
- **Before / after**：冷啟動（registry stale + JWT miss）少一個序列 RTT。mock 下併入 sk 3.70→1.21。  
- **風險**：低。registry 失敗語意不變（仍用舊 cache）。  
- **單獨還原**：搜 `OPT-4`，改回先 `await _resolve…` 再 `await registry.ensure_fresh`。

**2026-07-31 修正（合併前必要條件）**：平行化把一個原本不會發生的事變成會發生——
壞掉／已撤銷的 JWT 在 identity 被拒之前，**已經**打過 `GET /v1/agents` 並失敗了
（序列版會先 401 短路，registry 根本不會被呼叫）。那個失敗寫進 registry 的
**單一全域** `last_refresh_error`，於是**下一位使用者**的 trace 會顯示
「registry refresh 失敗：…」，連 CSP 拒絕別人 token 的原文一起帶出去。
trace 是給維運看的，在列管平台上這是隱私缺陷，不只是難看。

- 修法：錯誤狀態改成**每個 caller 一份**（`RemoteAgentRegistry.refresh_error_for(api_key)`，
  與 agent 快取共用同一組 LRU 上限，失敗的 refresh 不會進 `_store`，所以錯誤表自己也要 LRU，
  否則就把剛修好的記憶體洩漏原樣開回來）。`last_refresh_error` 保留給 `/health`
  ——那本來就是全站維運視角，不是任何一個人的回應。
- 一併修：`gather(..., return_exceptions=True)` 並在之後才 raise。裸 gather 在
  identity 先失敗時會**留下還在跑的 registry refresh**，它的副作用會落在
  之後某個不確定的時間點（可能是下一位使用者的那一輪）。
- 守衛測試：`packages/anila-core/tests/test_router_registry_error_isolation.py`。

### OPT-5 — `anila_meta.route`（路由可見性，非加速）

- **做什麼**：在 meta 加 additive 欄位，例如  
  `{"decision":"direct"}` / `{"decision":"dispatch","agent_id":"…","correctable":true}` / `route_miss` / `dispatch_error` / `llm_error`，salvage 時帶 `salvaged:true`。舊前端忽略未知欄位。  
- **Before / after**：無延遲變化。讓錯誤分派／略過不再只能從 trace 字串猜。  
- **風險**：極低（只加欄位）。  
- **單獨還原**：刪 `_merge_anila_meta(..., route=)` 與各呼叫點的 `route={...}`。

### OPT-2 — `url_guard` DNS TTL 快取 —— **已於 2026-07-31 移除**

- **原本做什麼**：`socket.getaddrinfo` 結果快取 30s（`ANILA_URL_GUARD_DNS_TTL`）。  
- **為什麼拿掉**：買到 0.02 ms（0.13 → 0.11），代價是在 SSRF 防線裡多一份可變狀態：
  一個**沒有上限**、以 hostname 為 key 的 process 級 dict（正好是本分支剛修掉的那類洩漏），
  外加把 DNS rebinding 的 TOCTOU 窗口拉長到 30 秒。這條不划算——
  「不要把系統越搞越複雜」是擁有者的長期指令，而這是全包裡投報率最差的一項。
- **現狀**：`url_guard.py` 與 `security/__init__.py` 已與分支前逐字元相同
  （`git diff 47ddedb -- …` 為空）；`clear_dns_cache` 匯出一併撤掉，全樹無殘留引用。

### OPT-6 — 拆開 connect timeout（掛在 http_pool）

- **做什麼**：`Timeout(connect=3, read=120, write=60, pool=5)`，可用 `ANILA_HTTP_CONNECT_TIMEOUT` / `ANILA_HTTP_READ_TIMEOUT` 調。  
- **Before / after**：成功路徑不變；死掉的 upstream 從「卡到 120s」變成約 3s 連線失敗。  
- **風險**：中。極慢的握手（>3s）會提早失敗——內網通常遠低於此。若要更寬鬆只調 env，不必改碼。  
- **單獨還原**：與 OPT-1 同檔；或把 `_timeout()` 改回 `timeout=120.0`。

---

## Found but not changed

1. **Sentinel = 整輪 primary LLM**  
   每一則都付模型延遲。要再砍 wall-clock，得改路由策略（規則捷徑、快取「同 session 連續直接答」、較小分類模型）——會改可觀察行為，故只報告不下手。擁有者若覺得「每則都先想很久」，這才是主因。

2. **錯誤分派對使用者幾乎隱形**  
   `_dispatch_safe` 失敗時回「（agent「X」暫時不可用…已自動略過）」——看起來像正常助手句。OPT-5 的 `route.decision=dispatch_error` + `correctable` 是最小可見化；UI 尚未畫「換一個 agent」按鈕（需 shell，超出本包）。

3. **Blocking `getaddrinfo` 在 async 路徑**  
   `url_guard` 仍同步解析。Router 熱路徑幾乎不叫它（guard 在 CSP 註冊時）；若要 `run_in_executor` 屬行為/複雜度擴張，且本機 DNS 已 <0.1 ms，投資報酬低。

4. **SQLite session 單一共用連線**  
   模組級 `_conn_cache` 在高併發下可能變成串行點；改 pool/Postgres 是架構票，不是今晚可獨立還原的小 OPT。

5. **Holding resource across await（CSP pool 同類）**  
   Router 的 HTTP stream 跨 await 持有連線是正確的。SQLite 連線不跨 outbound HTTP 長持有。未發現與 CSP pool 同形的缺陷。

6. **`ensure_session_owner` 雙重讀取**  
   曾試作寫後不重讀，但並發 claim 會誤放行——已撤回。保留 fail-closed。

7. **不讓它更嚴**  
   未新增加密閘、未要求新設定才能啟動、未新增使用者可撞到的失敗模式（OPT-6 只讓「本來就會失敗」更快）。

---

## How the owner tries this

**不要**動正在跑的 `anila-restart` 專案（不要 restart / recreate / 改它的 compose）。用獨立 throwaway 容器掛同一網路試 Router。

```bash
# 在 wt-coreopt 工作樹
cd /home/c1147259/桌面/ANILA/anila-restart-20260729/wt-coreopt

# 1) 建專用 image（名稱刻意與 anila-restart-router 區隔）
docker build -f services/anila-core-router/Dockerfile \
  -t anila-coreopt-router:try .

# 2) 丟棄式容器：連既有 anila-net，host 埠 19000（不撞正式 router）
#    環境對齊正式 router（CSP 位址／token／model）——只讀用，不改正式 stack
docker run --rm --name coreopt-router-try \
  --network anila-net \
  -p 19000:9000 \
  -e CSP_BASE_URL=http://csp:8000 \
  -e CSP_SERVICE_TOKEN="$(docker inspect anila-restart-router-1 \
      --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^CSP_SERVICE_TOKEN=//p')" \
  -e MODEL="$(docker inspect anila-restart-router-1 \
      --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MODEL=//p')" \
  anila-coreopt-router:try

# 3) 健康檢查
curl -sS http://127.0.0.1:19000/health

# 4) 對照：正式 router 仍在 anila-restart 內；試打時把 shell／curl 指到 :19000
#    連續兩則用同一 JWT，第二則應幾乎不再等 /me（看 csp 日誌或 latency_ms）
#    回應 JSON／SSE 的 anila_meta.route 應出現 decision=direct|dispatch|…

# 5) 收工
docker stop coreopt-router-try   # --rm 會自動刪
# image 可留可刪： docker rmi anila-coreopt-router:try
```

單獨丟掉某 OPT：見上節「單獨還原」；檔案內搜 `OPT-N`。

本分支**未 push、未 merge**；工作樹變更尚未要求 commit。

---

## 建議擁有者優先感覺的一件事

連發幾則聊天（瀏覽器 cookie／JWT），感受第二則起是否少了一截「開場空白」——那是 OPT-3 省掉的 `/api/auth/me`。若整體仍慢，慢的是 **sentinel 整輪模型**，不是 Router 這幾毫秒；那時再決定要不要動路由策略（屬行為變更，需你拍板）。

# 原始思考不出伺服器

2026-10-01 擁有者決定：模型的原始思考只送給擁有者、管理員、開發者。其他人的瀏覽器與 API 用戶端一個字都收不到；他們看到的中文思考摘要由伺服器產生。

## 現況（9436dfce 之後）

1. 路徑是 Shell → Router（`/router/`）→ CSP `/v1/chat/completions`（`services/csp/app/api/proxy.py`）→ 模型。
2. 模型的 `reasoning_content`（或內容裡的 `<think>`）經 CSP 原樣送給 Router。Router 再對每一個呼叫者送出 `anila.reasoning` 事件，並把原文放進 `anila_meta.reasoning`。
3. Shell 收到原文後，分批送到 `POST /api/thinking/summarize`，由 CSP 用摘要模型產生中文摘要，再顯示出來。
4. Shell 存對話時，把原文寫進訊息的 `metadata.reasoning`。重新載入時，`conversations.py` 的 `_message_out` 會整包送回。
5. 畫面只對擁有者、管理員、開發者顯示原文（9436dfce）。但原文的位元組已經送到每一個瀏覽器。

## 目標

### 判斷誰可以看原文

- 「可看原文」的身分是 `owner`、`admin`、`developer`，以登入的使用者為準。
- API 金鑰呼叫以金鑰擁有者的身分判斷。
- 判斷在伺服器端做，不信任用戶端送來的任何旗標。

### CSP 是唯一的出口閘

CSP 的聊天代理知道呼叫者是誰。對不可看原文的呼叫者，CSP 在送回上游回應之前：
- 移除每一個串流片段與非串流回應裡的 `reasoning_content`、`reasoning`；
- 把內容裡被當成思考的部分也移除（`<think>…</think>`，以及 Router `_sanitize_leaked_thought` 認得的那種開頭）；
- 一邊收原文、一邊用 `summarize_reasoning_batch` 分批產生中文摘要，以自訂欄位送出（例如 delta 的 `anila_thinking_summary`）。摘要的節奏比照 Shell 現在的分批方式；
- 摘要失敗就不送摘要，回答照常進行（fail-open）。原文在任何情況下都不外送。

對可看原文的呼叫者，原文照舊轉送，也同樣送伺服器產生的摘要，讓 Shell 只需要一套顯示邏輯。

### Router

- 把 CSP 送來的摘要欄位轉成串流事件（例如 `anila.thinking_summary`）。
- 只有在原文真的存在時，才送 `anila.reasoning` 與 `anila_meta.reasoning`；不可看原文的呼叫者，自然收不到。
- Router 自己的思考或規劃步驟，若會把模型的原文放進事件或 trace，也要遵守同一個閘。

### Shell

- 顯示伺服器送來的摘要，不再自己呼叫 `/api/thinking/summarize`。
- 沒有原文時，不顯示「原始思考」，也不存 `metadata.reasoning`。

### 儲存與讀回

- `conversations.py` 讀訊息時，對不可看原文的使用者移除 `metadata.reasoning`（包含截斷後剩下的部分）。這也涵蓋已經存在資料庫裡的舊資料。
- 寫入訊息時，不可看原文的使用者送來的 `reasoning` 一律丟棄。
- 摘要（中文）可以存，讓重新載入時仍看得到。

### `/api/thinking/summarize`

Shell 不再使用這個端點。改成只給可看原文的身分用；若確認沒有其他用途，直接移除。

## 驗收

- 以一般使用者身分經 Router 串流一則會思考的回答，完整的串流位元組裡找不到原文中的任何一段（測試用一個固定的假模型，原文帶一個唯一標記字串）。
- 同樣的情況，管理員收得到原文。
- 一般使用者看得到中文摘要。
- 摘要模型掛掉時，回答照常送完，也不送原文。
- 一般使用者重新載入舊對話（資料庫裡已經存了原文）時，回應裡找不到原文。
- 非串流呼叫與 API 金鑰呼叫也照同樣規則。

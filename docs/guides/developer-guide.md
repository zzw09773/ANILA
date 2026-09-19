# 開發者指南 — 在 CSP 平台註冊一個 Agent

> 本文件說明開發者要如何取得樣板 → 部署自己的 agent → 在「Developer / Agent Console」
> 完成註冊 → 送審 → 被 Router 自動 discover。
>
> 頁面位置：`/developer/agents`（`apps/csp-governance-ui/src/views/DeveloperAgentsView.vue`）
>
> **P2.1（2026-08-01）**：agent 派工身分改為平台現簽的 **5 分鐘 JWT**（claims =
> `{user_id, department, agent_id}`），agent 用公開 JWKS 驗簽。**註冊不核發、
> 也不保管任何長效 agent 祕密**（不再有 `csk-`／`CSP_SERVICE_TOKEN` 上手路徑）。
> 權威設計見 `docs/designs/p21-drop-csk-scope-2026-08-01.md`；規格見 `SYSTEM-MAP.md` §身分。

---

## TL;DR

1. 在 Agent Console 按「下載樣板」取得 zip（解壓後核對是否已含驗簽與 CA——見下方三級制）。
2. 接上派工 JWT 驗簽（樣板內建、或單檔 `anila_verify.py`），設定非祕密 `.env`，部署服務。
3. 確認三條 OpenAI 相容端點：`GET /health`、`GET /v1/models`、`POST /v1/chat/completions`（欄位見 §3）。架構不限。
4. 回到 Agent Console 按「註冊 Agent」，填名稱、endpoint URL、router 描述（**不領鑰匙**）。
5. 等 admin 核准 → Router 自動 discover → 在前端對話列表看得到。

---

## 1. 前置條件

- 一台可跑 Docker（或等效）的機器，對 CSP 後端可達（或反過來）。
- 至少一組 LLM／embedding 端點（直連或走 CSP proxy）。
- 你在 CSP 平台已經登入且角色為 `developer` 或 `admin`。
- agent 主機要能以 HTTPS 打到平台的 `/.well-known/jwks.json`（內網需 CSPKI 信任錨；
  用 `ANILA_CA_FILE` 指 PEM，**不要**設 `SSL_CERT_FILE`）。

---

## 2. 接入驗簽 · 三級制

契約在 **HTTP 層**，不綁特定框架：平台每次派工帶
`Authorization: Bearer <JWT>`（RS256、約 5 分鐘；`iss=anila-csp`、`aud=anila-agent`），
agent 用 JWKS 驗簽。開發者**不領任何鑰匙**。氣隙內**治理中心是唯一發行點**。

語氣與「今日誠實可用」邊界以治理中心 `AgentGuardPanel` 為準：

### ① 新 agent — 下載樣板（零驗證碼）

按頁面「下載樣板」。設計目標：樣板內建驗簽與 CA，並附離線 wheel
（有網機先建 wheelhouse，氣隙再 `pip install --no-index --find-links=…`）。

**今日請先打開 zip 核對內容**——若尚無驗簽程式、`*.pem` 或 `*.whl`，代表樣板套件
尚未落地；請改走②下載 `anila_verify.py`，並用「下載平台 CA」取 PEM
（端點未上線時按鈕會提示，不會假裝成功）。註冊只需名稱與 endpoint，不核發長效祕密。

### ② 既有 Python 服務 — 單檔 `anila_verify.py`

按「下載 `anila_verify.py`」取得單檔（stdlib＋cryptography），放到你的服務旁，再接入：

```python
import os
from anila_verify import verify_authorization

def require_dispatch(request):
    """驗平台派工 JWT；失敗丟例外（fail-closed）。"""
    claims = verify_authorization(
        request.headers.get("Authorization"),
        jwks_url=f"{os.environ['CSP_BASE_URL'].rstrip('/')}/.well-known/jwks.json",
        ca_file=os.environ.get("ANILA_CA_FILE") or None,
    )
    # claims: user_id / department / agent_id
    return claims
```

無需向平台申請任何憑證。此檔由**治理中心發行**，不是樣板 zip 的內容。
**若下載端點尚未上線會顯示提示**——在那之前此級暫時無法在氣隙內取得該檔。

### ③ 無法改碼 — 驗證 sidecar

規劃中的選項：在 agent 前方放驗證 sidecar（驗完再轉發，本體不動），映像走內網既有搬運通道。
**今日尚無公開映像與部署說明**——若你無法改碼，請先走①樣板，或等候 sidecar 套件釋出；
此處不捏造操作步驟。

### 非祕密設定（`.env`）

```ini
CSP_BASE_URL=https://<csp-host-reachable-from-agent>
ANILA_CA_FILE=/path/to/cspki_ca_bundle.pem
# ⚠ 勿設 SSL_CERT_FILE——會整份取代系統信任庫
# 選填：ANILA_COLLECTION_ID=…（RAG；任務內回呼複用同一張派工 JWT）
```

**不必、也不應**再貼任何 `csk-`／`CSP_SERVICE_TOKEN`／`bsk-` 作為正常上手路徑。

---

## 3. Endpoint 合約（你必須實作的）

**架構不限。** 樣板、LangChain、LangGraph、自寫 FastAPI 都可以；平台不問 runtime 型別。
Router 與 CSP **只認** OpenAI 相容這三條路徑，其他你可以自己加。

| 方法 | 路徑 | 驗證 | 平台必讀的欄位 |
|---|---|---|---|
| `GET` | `/health` | 公開 | HTTP **200**。建議 JSON `{"status":"ok"}`（探測只看 2xx，也接受 `GET /v1/models` 當備援） |
| `GET` | `/v1/models` | 派工 JWT | `object`=`"list"`；`data[]` 至少一筆，每筆有 `id`、`object`=`"model"` |
| `POST` | `/v1/chat/completions` | 派工 JWT | **入向**必讀 `messages`（`[{role, content}]`）與 `stream`。**出向**見 §3.3／§3.4 |

驗證讀 `Authorization: Bearer <JWT>`，以平台 JWKS 驗簽（fail-closed）。身分在 JWT claims 內，
**不要**再依賴明文 `X-ANILA-User-*` 當信任根。空的 `messages` 回 400／422 可以——連線探測就是這樣打。

任務內回呼平台（RAG 搜尋、trace、artifacts）**複用同一張派工 JWT**
（`Authorization: Bearer <同一 JWT>`）；平台驗簽後仍做 `bound_collection_id` 範圍檢查。

### 3.1 `/health` 輸出格式

```json
{
  "status": "ok",
  "model":  "google/gemma4",
  "rag":    true
}
```

探測認 HTTP 2xx（`/health` 或備援 `/v1/models`）。`status == "ok"` 建議有，方便人讀。

### 3.2 `/v1/models` 輸出格式（OpenAI-compat）

```json
{
  "object": "list",
  "data": [
    {
      "id":       "rag/google/gemma4",
      "object":   "model",
      "created":  1735689600,
      "owned_by": "anila-core"
    }
  ]
}
```

### 3.3 `/v1/chat/completions` 輸入

平台一定會送、你一定要讀的只有兩欄：`messages`、`stream`。
`model`、`temperature` 以及其他 OpenAI 欄位會原樣轉發，可忽略。
空 `messages` 回 400／422 沒關係（連線探測用）。

```json
{
  "model":    "rag/google/gemma4",
  "messages": [
    {"role": "system", "content": "你是客服助理"},
    {"role": "user",   "content": "請假規定？"}
  ],
  "stream":      true,
  "temperature": 0.7
}
```

### 3.4 `/v1/chat/completions` 輸出 — **這就是「模型的輸出格式」**

平台要顯示回覆，**最少**要有：
- 非串流：`choices[0].message.content`
- 串流：`choices[0].delta.content` 若干筆，最後一行 `data: [DONE]`，`Content-Type: text/event-stream`

`usage` 選填；沒回的話 CSP 會估算 token。`reasoning_content` 選填（思考／檢索軌跡）。

**非串流**（`stream: false`）— 一次回一個 JSON：

```json
{
  "id":      "chatcmpl-abc123",
  "object":  "chat.completion",
  "created":  1735689600,
  "model":   "rag/google/gemma4",
  "choices": [
    {
      "index": 0,
      "message": {
        "role":    "assistant",
        "content": "根據《員工手冊 §3.2》……"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens":     128,
    "completion_tokens": 64,
    "total_tokens":      192
  }
}
```

**串流**（`stream: true`）— Server-Sent Events，一筆一個 `data:` 行，最後以 `data: [DONE]` 結束：

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"content":"根據"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{"content":"《員工手冊》"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1735689600,"model":"rag/google/gemma4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

**可選欄位：**

- `choices[].delta.reasoning_content`：思考塊 / RAG 檢索軌跡，前端會顯示在折疊區。
- 回覆結尾自行 append「參考來源」清單，平台不強制格式。

回應 header 必須是 `Content-Type: text/event-stream`，且保持連線直到 `[DONE]`。

---

## 4. 在 Agent Console 註冊

到 `/developer/agents` 按 **register** 進精靈。

**填 agent 細節：**

| 欄位 | 必填 | 範例 | 備註 |
|---|---|---|---|
| `name` | ✅ | `hr-policy-agent` | 全平台唯一，建議 `kebab-case` |
| `endpoint_url` | ✅ | `http://10.0.1.20:24786` | 必須 `http://` 或 `https://`，Router 可達 |
| `description_for_router` | ✅（≥24 字元） | `處理員工手冊、請假規定、薪酬結構等 HR 法規查詢。` | 自然語言；Router 用這段做 agent 選擇 |
| `base_model_id` | ✅ | `4` | CSP Model Registry 中**已啟用**的底層 LLM/VLM；用量歸戶用 |
| `collection_id`（RAG collection） | ❌ | `5` | 可綁定；任務內搜尋範圍由平台依綁定做最小權限檢查；非 RAG agent 留空 |
| `api_version` | ❌（預設 `v1`） | `v1` | 目前 Router 只認得 `v1` |

API 另接受 `capabilities`（JSON dict 自由 metadata）與 `input_schema`（JSON Schema），UI 沒露出。

送出後 agent 進入核准流程（以治理中心實際狀態機為準）。

**沒有「領 service token」步驟。** 平台派工時會現簽 5 分鐘憑條；你只須接好驗簽
（見 §2）。畫面上不該再出現任何要你保管的長效祕密字串。

非祕密設定（`CSP_BASE_URL`／`ANILA_CA_FILE`／選填 `ANILA_COLLECTION_ID`）可從接入面板複製。
⚠ `CSP_BASE_URL` 要填 **agent 那端可達的 CSP host，不是 localhost**。

---

## 5. 審核與健康檢查

- **Admin 核准**：admin 在同一頁按「核准」。被拒絕時會附留言。
- **Health polling**：CSP 會定期呼叫你的 `/health`。連不到 → `unhealthy`；對話時 Router 會跳過。
- **連線探測**：派工 JWT 驗簽須在 agent 側自行接好；核准不依賴舊的靜態 token 探打結果。
- **加密模式／列管**：admin 可切相關旗標。啟用後凡是經由此 agent 的對話可能**單向**鎖為列管，
  使用者無法關閉 — 請評估再用。

核准後 Router 下一次 discovery tick 會把你加進候選池，前端就看得到。

---

## 6. 常見送審失敗原因

| 狀況 | 怎麼修 |
|---|---|
| `Endpoint 必須是 http 或 https URL` | 補上 scheme。 |
| `Router 描述至少需要 24 個字元` | 寫清楚這個 agent 處理什麼領域、什麼格式的問題。 |
| 註冊成功但 health 一直 `unhealthy` | CSP backend 連不到你的 host/port；檢查防火牆、Docker 網段、`endpoint_url` 是否是 CSP 能解析到的位址（不是 `localhost`）。 |
| 派工回 `401` | agent 未接 JWKS 驗簽、CA 未佈、JWT 過期、或 `aud`/`iss` 不符；對照 §2 與治理中心片段重接。 |
| JWKS／HTTPS 失敗 | 確認 `ANILA_CA_FILE` 指向完整 CSPKI 鏈；**勿**用 `SSL_CERT_FILE`。 |
| 串流回覆卡住 | 檢查 `Content-Type: text/event-stream`、`[DONE]` 結尾、proxy/nginx buffering 要關掉（`X-Accel-Buffering: no`）。 |

---

## 7. 進階：tool-driven / multi-turn agent

如果你的 agent 需要 LLM 自己決定何時檢索、呼叫哪個 tool，不要停在 OpenAI-compat
pre-process 注入層——在 anila-agent 樣板裡用它自己的 `agentic_rag.tools`
（`VectorSearchTool` / `KeywordSearchTool` / `ReadDocumentTool` 的正式實作在那裡，
**不在** `anila_core.tools`；anila-core 自 Sprint 1 起就是 RAG-agnostic）。

`anila_core.engine.query_engine.QueryEngine` 仍可 import，但平台上沒有任何服務用它
（Router 是手寫派工器，2026-09-02 量測）；要用它自建 turn loop 的話，入口是
`await engine.run(messages, on_stream_delta=...)`（沒有 `run_stream()`），
範例見 `packages/anila-core/e2e_smoke.py`。

Output 面向 Router 的格式一樣是 OpenAI-compat SSE，tool call 的中間狀態走
`delta.reasoning_content`。

---

## 8. 相關檔案索引

- 前端頁面：`apps/csp-governance-ui/src/views/DeveloperAgentsView.vue`
- 接入面板：`apps/csp-governance-ui/src/components/agents/AgentGuardPanel.vue`
- 註冊 API：`services/csp/app/api/agents.py`（`register` / `approve` / `reject`）
- Agent ORM：`services/csp/app/models/agent.py`
- 官方樣板：`packages/anila-agent/`（README；樣板 zip 內容以實際下載為準）
- Runtime foundation：`packages/anila-core/`
- P2.1 設計：`docs/designs/p21-drop-csk-scope-2026-08-01.md`
- 平台規格：`SYSTEM-MAP.md`

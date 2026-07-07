# csp 後端相容轉換層（Backend Adapter Framework）— Phase 1+2

日期：2026-07-06 ｜ 狀態：設計中（Fable5 審查 + llama.cpp 實測後修訂 v2）｜ 範圍：Phase 1（骨架 + vLLM passthrough）+ Phase 2（llama.cpp adapter）

## 目標

讓 csp model gateway 能接內網各家推理後端（vLLM / llama.cpp / Ollama / TensorRT-LLM / Triton），對外統一成 OpenAI 相容介面。本 spec 只涵蓋 **Phase 1（可擴充骨架 + vLLM passthrough）與 Phase 2（llama.cpp adapter）**；Ollama / Triton / tensorrtllm / images 各家轉換是後續 phase 的獨立 spec。

落地 redesign doc 04 §2 已預留、標記「custom adapter 之後才落地」的 `protocol` 機制。

## 非目標

- 不做 Triton KServe v2 轉換（Phase 3）、Ollama / tensorrtllm adapter（Phase 4）、images 各家轉換（Phase 5）。
- 不改 csp 對外的 OpenAI 契約（`/v1/chat/completions`、`/v1/embeddings`、`/v1/models` 對外形狀不變）。
- 不自動偵測 backend——一律靠註冊時手動標（見 §4）。
- 不碰 health_checker（見 §7 說明：現況不解析 body，改了反而 regress）。

## 背景與現況（附查證 file:line）

- csp proxy（`services/csp/app/services/proxy/service.py`）目前是**透明轉發**：假設所有上游 OpenAI 相容，只做 `/v1` 拼接、認證、SSE 轉發、retry/timeout。
- **非串流** `_proxy_request_impl` timeout 走 `_get_timeout(model.model_type)`（只分 embedding/LLM，service.py:44-47, 154）。
- **串流** `_proxy_stream_impl` timeout **寫死** `settings.LLM_TIMEOUT`（service.py:547），**不呼叫** `_get_timeout`；且其簽名**不帶** `ModelRegistry` / `protocol` / `model_type`（service.py:473-493，只有 `usage_model_id:int`、`model_name:str`、已拼好的 `target_url`）。
- 串流會**無條件強制注入** `body={**request_body,"stream":True,"stream_options":{"include_usage":True}}`（service.py:535-536），並有 `[DONE]` holdback（:544, 579-580, 655）、`anila.meta` 預設事件注入（:639-654）、usage 攔截（:584-596）等下游邏輯。
- `_proxy_stream_impl` **同時服務 agent 串流**（`api/proxy.py:620-649` 傳 `target_agent_id`）；Agent 無 `protocol` 欄位。
- `model_registry.protocol` 已存在（`model_registry.py:18`），值域 `openai_compatible | custom_adapter`（doc 04 §2），但 **proxy 全檔不讀它**（唯一存在處：model 註解 model_registry.py:16、schema 註解 schemas/model_registry.py:20、UI 下拉 ModelsView.vue:321）→ `custom_adapter` 是**死值**，改值域**零 regress**。
- 非 2xx 錯誤解析只認 OpenAI `{"error":{"message"}}`（service.py:248-258）。

### 實測事實基礎（釘死，Phase 2 依據）

內網 `172.16.120.35:18018`（gemma-4-31b）/ `:18019`（mistral-small-4）**由 llama.cpp 起**（`/props` 回 200 為 llama.cpp 特有端點；無 `/version`）。實測 wire 行為：

- `GET /v1/models` **同時**含 Ollama 風格 `{"models":[{"name","model","modified_at","digest","capabilities",...}]}` **與 OpenAI 風格 `{"object":"list","data":[{"id",...}]}`** → **OpenAI client 讀 `data` 即通，無需格式轉換**。
- `POST /v1/chat/completions` 回**完整 OpenAI 格式含 `usage`**（額外良性欄位 `timings`、`system_fingerprint`、`usage.prompt_tokens_details.cached_tokens`）。
- 錯誤格式為 **OpenAI 形狀** `{"error":{"message","type","code"}}`（非 Ollama 字串）。
- `POST /v1/embeddings` 回 **501** `not_supported`（此實例未帶 `--embeddings` 啟動；屬部署配置，非相容缺陷）。
- **唯一真痛點：首 token 需 load model → 串流 read 冷啟慢，固定 `LLM_TIMEOUT` 會逾時。**

→ **結論**：這台 llama.cpp **高度 OpenAI 相容**，adapter ≈ passthrough + **timeout 放寬**；格式幾乎不轉。（若日後遇到未帶 OpenAI `data` 欄位的 llama.cpp 版本，才需 `adapt_models_list`。）

## 架構：per-endpoint adapter 介面 + protocol registry

```
csp proxy 收到 OpenAI 格式請求
  → 依 model.protocol 從 registry 取 adapter（openai_compatible→passthrough / llamacpp→LlamaCppAdapter / …）
  → path = adapter.backend_path(endpoint_kind, model, api_version)   # URL 段（Triton /v2/.. 用；OpenAI 系 /v1/..）
  → body = adapter.to_backend_request(endpoint_kind, body)           # OpenAI → 後端原生
  → 打後端（沿用既有 httpx + 認證 + SSRF guard；timeout = adapter.request_timeout(...)）
  → 若 2xx: adapter.from_backend_response(endpoint_kind, resp)       # 後端 → OpenAI
    每塊: adapter.from_backend_stream_chunk(endpoint_kind, raw_line) # 串流逐行轉
    若非 2xx: adapter.from_backend_error(status, raw_body)           # 錯誤 → OpenAI error
  → 回 OpenAI 格式
```

**設計原則**：`openai_compatible` → passthrough adapter（全 no-op，保證現有 vLLM/相容後端行為與**現況**逐位元組相同）；需轉換者各自 adapter，獨立可測。加後端 = 加一個 adapter class + 註冊，**非串流路徑不動 proxy 主流程**（介面含 `backend_path`/`from_backend_error`/`to_backend_request`/`from_backend_response`，涵蓋 Triton 非串流 POST `/v2/models/{model}/infer`）。**串流是例外**：目前串流一律依 request `stream` 走 OpenAI SSE parser（`api/proxy.py:797-837`、`sse.py:9-18` 只認 `event:`/`data:`），非 SSE 後端（Triton KServe v2）的串流**無法只靠 adapter**——需在其 Phase 3 spec 處理（含 `supports_streaming` 治理：非串流後端在 registry 標 `supports_streaming=false`，proxy 對它強制走非串流路徑）。故本 framework 的「不動主流程」宣稱**限定非串流**。

### BackendAdapter 介面（`services/csp/app/services/proxy/adapters/base.py`）

```python
class BackendAdapter(Protocol):
    name: str  # == protocol 值："openai_compatible" / "llamacpp" / ...

    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout: ...
    def backend_path(self, endpoint_kind: str, model_name: str, api_version: str) -> str: ...
    def to_backend_request(self, endpoint_kind: str, body: dict) -> dict: ...
    def from_backend_response(self, endpoint_kind: str, resp: dict) -> dict: ...
    def from_backend_stream_chunk(self, endpoint_kind: str, raw_line: str) -> str | None: ...
    def from_backend_error(self, endpoint_kind: str, status: int, raw_body: str) -> dict: ...
    def adapt_models_list(self, raw: dict) -> dict: ...  # 未來 auto-discovery 用；Phase 1+2 不接
```

- `endpoint_kind` ∈ `{"chat","embeddings","models","images"}`（Phase 1+2 實作 chat/embeddings；images 留 Phase 5，介面預留）。
- **timeout 回 `httpx.Timeout`**（connect 維持短、read 放寬）——只放寬 read，避免掛掉的主機吊滿放寬值。
- **`from_backend_stream_chunk` 語意鎖死**：輸入是 `resp.aiter_lines()` 的**單行**（raw line，pre-parse，未進 block 組裝）；回傳 `None` = **丟棄該行**（不轉發）、回傳 str = 轉發該行。passthrough 回傳原行不動。
- passthrough adapter：`backend_path` 回既有拼法、`request_timeout` 回 default、其餘回原值；`from_backend_error` 回既有 `{"error":{"message"}}` 解析。
- registry（`adapters/__init__.py`）：`{protocol_name: adapter_instance}`，未知 protocol → fallback passthrough + log warning（**僅為 DB 殘值 fail-safe**；顯式選值由 §4 server-side whitelist 擋在寫入前）。

### proxy 接入點（精確，實作者照這動手）

1. **`proxy_stream` public wrapper（service.py:729-749）與 `_proxy_stream_impl`（service.py:473）都加 `protocol: str = "openai_compatible"`**，wrapper 原樣轉交 `_impl`。`api/proxy.py` 兩處呼叫的是 **`proxy_stream(...)`**（不是 `_impl`），兩個呼叫端：
   - `api/proxy.py:620-649` **agent 分支** → 傳 `protocol="openai_compatible"`（agent 無 protocol，強制 passthrough）。
   - `api/proxy.py:803-822` **model 分支** → 傳 `protocol=model.protocol`。
2. 串流內：`adapter = get_adapter(protocol)`；
   - timeout：service.py:547 的 `httpx.AsyncClient(timeout=...)` 改成 `timeout=adapter.request_timeout("chat", httpx.Timeout(settings.LLM_TIMEOUT))`。
   - **hook 相對 stream_options 注入的順序**：先 `body=to_backend_request(...)` **再**強制注入 `stream`/`stream_options`（service.py:535 之前套 adapter），讓 passthrough 不動、未來 adapter 可在其 `to_backend_request` 先剝掉不吃的欄位——但注入本身保留（passthrough 語意 = 現況）。
   - **stream chunk hook 位置**：`resp.aiter_lines()` 取得每行後、進既有 block 組裝（`_parse_sse_block`）**之前**，套 `from_backend_stream_chunk`；passthrough 回原行 → `[DONE]` holdback / anila.meta 注入 / usage 攔截等下游邏輯吃的是**未改動**的行，逐位元組宣稱成立。
3. **`_proxy_request_impl`**（非串流，service.py:154）：body 套 `to_backend_request`、resp 套 `from_backend_response`、非 2xx 套 `from_backend_error`、timeout 用 `adapter.request_timeout`。注意與既有 `_aggregate_sse_to_chat_completion`（service.py:267-268，上游硬回 SSE 時聚合）的順序：先聚合成 dict 再套 `from_backend_response`。
4. **串流非 2xx**：Phase 1/2 **不改**現有 streaming error 行為（上游串流開頭若非 2xx，沿用既有處理；llama.cpp 錯誤已 OpenAI 形狀，passthrough 即正確）。`from_backend_error` 在串流路徑的套用留待有「串流錯誤形狀不相容」的後端（Phase 3+）再接，本 phase 明確不碰。
5. `api/proxy.py::list_models_openai`（`/v1/models` **對外**）維持自組 OpenAI 形狀，不受影響。

## §4 protocol 欄位治理（schema 決策）

doc 04 的 `protocol` 是二元 `openai_compatible | custom_adapter`，無法區分「用哪個 adapter」。**決策：單欄位細化，語意 = adapter id**（Fable5 推薦，優於雙欄位 `backend_flavor`——雙欄位製造非法組合矩陣卻無多餘表達力）：

```
protocol ∈ { "openai_compatible", "llamacpp", "ollama", "tensorrtllm", "triton" }
```

- `openai_compatible` = vLLM / trtllm-serve / 任何原生相容者 → passthrough（**預設值不變，向後相容**）。
- **本 phase 只實作 `openai_compatible` 與 `llamacpp`**。
- **Server-side whitelist**：**只有 `ModelCreate`/`ModelUpdate`** 的 `protocol` 用 Pydantic `Literal[...]`（`schemas/model_registry.py:32-43` 的 create/update），**下拉與 schema 只放已有 adapter 的值**（Phase 1+2 = `openai_compatible`、`llamacpp`）；選未實作的 backend 直接 422，不會靜默 passthrough 對著它裸打。**`ModelResponse` 的 `protocol` 維持裸 `str`**（不套 Literal）——否則 DB 殘值（舊 `custom_adapter`）反序列化 response 會炸。其餘值隨對應 adapter 落地才加進 whitelist。
- runtime `get_adapter` 的 fallback passthrough **只服務 DB 殘值**（例如舊 `custom_adapter`）。
- **同步 redesign SSOT**：改 doc 04 §2 的 `protocol` 值域定義為上述細化值（doc 是 SSOT，不能只在 spec 偏離）。
- **Migration/資料**：不改欄位型別（仍 `String(30)`），只擴充應用層 whitelist；既有 `custom_adapter` 列 = runtime fallback passthrough（與今天行為相同，無 regress），UI 對這類列標「需改標」。**不自動改資料**。
- **前端**（`apps/csp-governance-ui` ModelsView:215-218,318-322）：`protocol` 下拉改放 whitelist 值、**移除 `custom_adapter` 選項**；比照今天加 image model_type 的做法。
- **auto-seed 寫入路徑（codex 抓到的實作 blocker）**：`AUTO_REGISTER_MODELS` 目前建 row 只帶 name/type/url/api_version/description/context，**不寫 `protocol`**（`auto_seed.py:253-261`），既有 row 更新只改 endpoint（`auto_seed.py:265-267`）。Phase 1 必須讓 auto-seed **建立與更新都帶 `protocol`**（config JSON 加 `protocol` 欄、預設 `openai_compatible`），否則用 seed 註冊 llama.cpp 時標不了 `llamacpp`。`config.py:103-105` 的 AUTO_REGISTER_MODELS 範例同步加 protocol 欄示範。

## Phase 1 — 骨架 + vLLM passthrough

1. `adapters/base.py`：`BackendAdapter` Protocol + `PassthroughAdapter`（openai_compatible），含 `backend_path`/`from_backend_error` 的既有行為實作。
2. `adapters/__init__.py`：registry + `get_adapter(protocol)->BackendAdapter`（未知 fallback passthrough + warn）。
3. `service.py` 接入 adapter（§架構接入點 1–3），`openai_compatible` 路徑行為與**現況逐位元組相同**。
4. schema `Literal` whitelist + 前端 protocol 下拉（移除 custom_adapter）+ 同步 doc 04 §2。
5. 測試：
   - passthrough identity：chat/embeddings/models request/response 等同輸入；**串流 identity 涵蓋 CRLF、多行 `data:`、空 comment 行、`[DONE]`**（證明 pre-parse hook + 回原行不破壞 SSE 位元組）。
   - registry 未知值 fallback passthrough + warn。
   - schema 選未實作 backend → 422。

## Phase 2 — llama.cpp adapter（`adapters/llamacpp.py`）

依實測（§實測事實基礎），llama.cpp 高度相容，adapter 極簡：

1. `request_timeout("chat", default)`：回**放寬 read 的 `httpx.Timeout`**（connect 短、read = `LLAMACPP_READ_TIMEOUT`，值見 open questions）——解冷啟首 token 逾時。**這是 Phase 2 的主要價值**。
2. `to_backend_request` / `from_backend_response`（chat）：**passthrough**（實測 chat 已完整 OpenAI + usage）。
3. `backend_path`：`/v1/...`（同 passthrough）。
4. `from_backend_error`：passthrough（實測錯誤已是 OpenAI 形狀）。
5. `adapt_models_list`：**不接**（實測 `/v1/models` 已含 OpenAI `data`；且 health_checker 不解析 body）。
6. embeddings：此實例 501（未啟用）。adapter 不特殊處理；若內網有帶 `--embeddings` 的 llama.cpp 再於本 adapter 補（現階段 passthrough）。
7. 測試：
   - 對「實測釘死的 raw fixture」（本 spec §實測事實基礎的實際回應）跑：chat passthrough 等同、timeout 為放寬 read 的 httpx.Timeout。
   - **Acceptance criterion（硬性）**：**內網對 18018/18019 真實錄製的 chat（串流+非串流）fixture 重放全綠** —— 外網階段只能標「Phase 2 待內網驗收」，不得宣告完成。（呼應本次專案「只驗 health 是假陽性」教訓。）

## 錯誤處理

- 顯式選未實作 backend → **422**（server-side whitelist，寫入前擋）。
- 未知 protocol（DB 殘值）→ fallback passthrough + warn（退回今天行為，不 fail）。
- adapter 轉換拋例外 → 502 帶清楚訊息（哪個 adapter/endpoint_kind），不靜默吞。
- 上游非 2xx → `from_backend_error`（passthrough 用既有 OpenAI 解析；未來 adapter 處理各家錯誤形狀，如真 Ollama 的字串 error）。

## 測試策略

- **規格 mock 先行（外網）**：adapter × endpoint 對 fixture 跑轉換單元測試；passthrough 有 identity 測試。
- **內網錄製為 Acceptance（非補驗）**：Phase 2 完成的硬門檻 = 內網真實 fixture 重放全綠。
- 既有 csp 測試全綠不 regress（passthrough 保證 openai_compatible 路徑不變）。
- fail-then-pass：每個 adapter 行為先寫紅測試再實作。

## Open questions（實作時定）

1. `LLAMACPP_READ_TIMEOUT` 具體值 / 用獨立 env vs per-model timeout 欄位；並在 open questions 點名 **retry 複利**（非串流 `_proxy_request_impl` timeout 重試 3 次含退避，service.py:348-357 → 最壞 ≈ 3×read_timeout，每次重試可能重觸 model load）。
2. `custom_adapter` 舊值：要不要出一個 data-fix 腳本把現存列提示人工改標（DB 現況多為預設 `openai_compatible`，可能根本無此列）。
3. `supports_tools` / `supports_json_schema`（doc 04 schema 有）是否本 phase 接——暫不接，列後續。

## 資料流（llama.cpp chat 串流為例）

```
SPA/Router → csp /v1/chat/completions (OpenAI body, model=gemma-4-31b, protocol=llamacpp)
  → _proxy_stream_impl(protocol="llamacpp")
  → adapter=get_adapter("llamacpp")
  → body=to_backend_request("chat", body)（passthrough）→ 強制注入 stream/stream_options
  → httpx.stream POST target_url，timeout=request_timeout("chat", LLM_TIMEOUT) # read 放寬解冷啟
  → 每行 from_backend_stream_chunk("chat", line)（passthrough 回原行）
  → 既有 [DONE] holdback / anila.meta / usage 攔截照舊
  → 回標準 OpenAI chat.completion.chunk 串流
```

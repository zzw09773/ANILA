# ANILA Trace Adapters（非 anila-agent runtime 的 Full Trace 範例）

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】,保留供追溯,不代表現況。現行狀態與執行順序見 `PLAN.md`。

> **對應設計文件**：`docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md`
> （§6 Full Trace 協定、§9 Runtime Types）、`06-openwebui-agent-migration.md`
> （§6 Full Trace Acceptance、§8 Trace Test）、`10-migration-and-development-guardrails.md`
> §7 Slice 5 Done：**至少 1 個非 anila-agent runtime（LangChain／custom HTTP）以 trace
> adapter 完成 Full Trace**。

> ⚠ **2026-08-01 P2.1**：任務內 trace／RAG 回呼改帶平台派工 JWT
> （`Authorization: Bearer <JWT>`），不再使用長效 `csk-`／`X-CSP-Service-Token`。
> 入向驗簽見治理中心三級制與 `docs/guides/developer-guide.md`。

`anila-agent` runtime 有原生內建的 Full Trace（見
[`packages/anila-agent/anila_agent/tracing.py`](../../packages/anila-agent/anila_agent/tracing.py)）。
本目錄示範**第三方 runtime**（LangChain、任何 OpenAI-compatible／custom HTTP agent）如何用
**最小相依**接上同一條 Full Trace 管線，達到 L3 approval 的門檻。span 最終由治理中心
（`services/csp`）的 `POST /v1/traces/{trace_id}/spans` 收攏。

```
examples/trace-adapters/
├── common/anila_trace_adapter.py        # ★ 只需 httpx，可整支複製到你的 agent
├── langchain/adapter_example.py         # LangChain callback handler（import-guarded）
├── custom-http/fastapi_agent_example.py # 最小 OpenAI-compatible FastAPI agent
└── tests/test_adapters.py               # pytest 驗收（10 項，全綠）
```

---

## 1. Frozen wire contract（不可更動）

所有 adapter 都送同一份契約回 CSP：

```
POST {csp_base}/v1/traces/{trace_id}/spans
Authorization: Bearer <派工 JWT（與當次 dispatch 同一張）>
Content-Type: application/json

{"spans":[{ "span_id", "parent_span_id"?, "span_type", "name",
            "started_at", "ended_at"?, "status", "attributes"?,
            "producer":"agent" } ...]}          # 每批 ≤256，CSP 回 202
```

`span_type` 逐字採 doc-05 §6 的 13 種（6 對 started/finished + 單發 error）：

```
agent.run.started / agent.run.finished
agent.step.started / agent.step.finished
agent.model_call.started / agent.model_call.finished
agent.tool_call.started / agent.tool_call.finished
agent.retrieval.started / agent.retrieval.finished
agent.output.started / agent.output.finished
agent.error
```

CSP dispatch 會帶下列 header，adapter 由此啟動（**缺 `X-ANILA-Trace-Id`
→ adapter 停用、零外送、零行為變化**）：

| header | 用途 |
|---|---|
| `Authorization: Bearer <JWT>` | 入向派工身分（JWKS 驗簽）＋出向 trace ship 複用同一張憑條 |
| `X-ANILA-Trace-Id` | 有它才發 trace |
| `X-ANILA-Task-Id` | 歸因到任務中心的 Task |
| `X-ANILA-Classification-Level` | 四級分類等級（無機密／營業秘密／密／機密；隨 run／output span 帶出） |

---

## 2. 把 adapter 帶進真實 agent

### 2.1 通用發送器（`common/anila_trace_adapter.py`）

整支複製到你的 agent 專案（只需 `pip install httpx`，不必安裝 `anila-core` 或
`openai-agents`），核心用法：

```python
from anila_trace_adapter import AnilaTraceAdapter, RUN, STEP, MODEL_CALL, TOOL_CALL, RETRIEVAL, OUTPUT

# 從當次派工請求取出 Bearer JWT（勿另存長效 csk-）
auth = request.headers.get("Authorization", "")
dispatch_jwt = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else auth

adapter = AnilaTraceAdapter(
    csp_base=os.environ["ANILA_CSP_BASE"],                 # https://anila.ai.ncsist.org.tw
    dispatch_token=dispatch_jwt,                           # 當次派工 JWT
    trace_id=request.headers["X-ANILA-Trace-Id"],
    task_id=request.headers.get("X-ANILA-Task-Id"),
    classification_level=request.headers.get("X-ANILA-Classification-Level"),
)

with adapter.span(RUN, "chat.completions"):                # root
    with adapter.span(STEP, "answer"):
        with adapter.span(MODEL_CALL, "gpt-oss-20b", model="gpt-oss-20b") as m:
            ...                                            # 呼叫模型
            m.attributes.update(input_tokens=42, output_tokens=18, total_tokens=60)
        with adapter.span(TOOL_CALL, "search_documents", tool_name="search_documents"):
            with adapter.span(RETRIEVAL, "collection-search", top_k=4, collection_ids=[12]) as r:
                ...                                        # 檢索
                r.attributes.update(chunk_ids=[...], document_ids=[...])
        with adapter.span(OUTPUT, "final", citations=["doc_1"]):
            ...
adapter.flush()                                            # 批次 POST 回 CSP
```

- 巢狀 `with` 自動維護 `parent_span_id`（單一請求 = 單一 adapter 實例，各請求互不污染）；
  區塊內拋例外會自動補 `agent.error` 並以 `status="error"` 收尾後 re-raise。
- 非巢狀成對事件（如 LangChain 回呼）改用 `adapter.begin(base, name, parent=...)` 取得
  `Span`，於結束回呼 `span.finish(status=..., **attrs)`。
- `active` 為 False（缺 `trace_id`／`csp_base`／`dispatch_token`）時所有方法皆 no-op；
  ship 失敗一律 drop-and-log，`flush()` 絕不拋例外——絕不讓 agent 掛掉。

### 2.2 LangChain（`langchain/adapter_example.py`）

把 `AnilaLangChainTracer` 加進執行時的 callbacks：

```python
from adapter_example import AnilaLangChainTracer, adapter_from_env

tracer = AnilaLangChainTracer(adapter_from_env())   # 由 ANILA_* 環境變數建 adapter
chain.invoke(inputs, config={"callbacks": [tracer]})
```

映射：`on_chain_*`→run/step、`on_llm_*`→model_call、`on_tool_*`→tool_call、
`on_retriever_*`→retrieval、`on_*_error`→error。最外層 chain 結束時自動 `flush()`。本檔以
`try/except ImportError` 保護 LangChain 匯入 —— **未安裝 LangChain 也能 import／lint／測試**
（handler 類別僅在可用時定義，否則 `AnilaLangChainTracer = None`）。

### 2.3 Custom HTTP（`custom-http/fastapi_agent_example.py`）

最小可獨立執行的 OpenAI-compatible agent，實作 doc-05 §9 Custom HTTP 要求的四端點
（`/.well-known/anila-agent.json`、`/health`、`/v1/chat/completions`、`/anila/trace-test`）：

```bash
pip install fastapi uvicorn httpx
ANILA_CSP_BASE=https://anila.ai.ncsist.org.tw python fastapi_agent_example.py   # → :9100
```

`POST /v1/chat/completions` 帶上派工 JWT 與 §1 的 trace header 時，會在回傳合法 completion
的同時，發出成功路徑的完整 span 集（run/step/model_call/tool_call/retrieval/output 六對）。

---

## 3. 如何滿足 trace-test 8 項（doc-06 §8）

| trace-test 項目 | 由誰滿足 |
|---|---|
| endpoint health | custom-http `GET /health` |
| manifest valid | custom-http `GET /.well-known/anila-agent.json` |
| dispatch JWT valid | 入向驗派工 JWT／JWKS（agent 端自行 fail-closed；見 `docs/guides/developer-guide.md`） |
| `/v1/chat/completions` reachable | custom-http `POST /v1/chat/completions` |
| SSE valid | 本範例走 POST callback 模式（`callback_mode:"post"`）；SSE 為選項 |
| `anila.spans` received | `adapter.flush()` 送 `POST /v1/traces/{trace_id}/spans` |
| required span types complete | `run/step/model_call/tool_call/retrieval/output` 成對 + 失敗時 `agent.error` |
| classification header respected | `X-ANILA-Classification-Level` → run／output span 屬性 |

Full Trace Acceptance（doc-06 §6）對照：run start/end、model call start/end、tool call
start/end、retrieval chunks、error、final output、classification level、citations —— 皆由
上述 span 集涵蓋（citations 取自最終答案的行內引用【來源：id】）。

> **注意**：本範例的 agent 端**未內建**完整 JWKS 驗簽中介層（請接治理中心
> `AgentGuardPanel`／`anila_verify.py` 三級制）；範例聚焦在**出向 Full Trace**，
> 並示範以當次派工 JWT 作為 ship 憑證。正式部署務必補入向 JWT 驗簽。

---

## 4. 註冊（wizard／CLI）

adapter 只解決 trace；agent 仍須先在 CSP 完成註冊、接好派工 JWT 驗簽、通過健康／trace 準入
才能進正式任務。**註冊不核發長效 `csk-`。** Agent Registry 審核態以治理中心實際狀態機為準。
兩條路徑：

- **Wizard**：治理中心 `apps/csp-governance-ui` 的 `/developer/agents` —— 填 endpoint／
  runtime type／分類上限（不領鑰匙）→ 接 JWKS 驗簽 → 健康／trace 準入。
- **CLI**：`anila-core register`（讀 `anila.yaml` → `POST /api/agents/register`），支援
  `--base-model`／`--base-model-id`／`--runtime-type`／`--classification-level`／`--version` 旗標：

  ```bash
  anila-core register \
      --endpoint http://your-host:9100 \
      --base-model gemma4 \
      --runtime-type custom_http \
      --classification-level 機密 \
      --version 1.0.0
  ```

  `--base-model` 是底層模型「名稱」（CSP 端解析成 id）；顯示名稱撞名時才改用 `--base-model-id`。
  `--runtime-type` 合法值：`anila_agent`／`langchain`／`openwebui_pipe_compatible`／
  `openai_compatible_agent`／`custom_http`。
  `--classification-level` 合法值：`無機密`／`營業秘密`／`密`／`機密`。
  `--draft`／`--classification-ceiling` 已移除：送出去只會被伺服器丟掉。

---

## 5. 跑測試

```bash
# 用 CSP 的 venv（內含 httpx／respx／fastapi）
services/csp/.venv/bin/python -m pytest -q examples/trace-adapters/tests/
```

涵蓋（10 項）：批次切分（>256 → 多批）、span-type 常數逐字對齊 doc-05 §6「Required Span
Types」、拋例外時補 `agent.error` 且以 `status="error"` 收尾、custom-http 帶 header → 正確 span
多重集合（run 帶 `task_id`、output 帶 `citations`）、不帶 header → 零外送、manifest／health
端點、LangChain 檔在無 langchain 下仍可乾淨 import。

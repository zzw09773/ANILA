# 07 — `packages/anila-agent` 官方樣板更新建議

> 日期：2026-07-06  
> 範圍：`packages/anila-agent`  
> 定位：`anila-agent` 是官方 starter/template 與 reference implementation，**不是 ANILA 本體對外部 Agent 的強制 runtime**。  
> 目的：讓樣板更好地展示如何符合 `anila-core` 定義的 Agent Integration Contract，並提供 LangChain / 自製框架接入範例。

---

## 1. 正確定位

`packages/anila-agent` 應被定義為：

```text
官方 Agent starter
reference implementation
best-practice template
conformance fixture
開發者下載後可改的範例
```

它不應被定義為：

```text
所有 Agent 必須使用的 runtime
ANILA 本體的唯一 Agent SDK
外部開發者不可替代的框架
LangChain / LlamaIndex 的競爭框架
```

文件應清楚說明：

```text
你可以不用 anila-agent；
你也可以用 LangChain、LlamaIndex、FastAPI、Node.js 或既有服務；
只要你的服務符合 ANILA Agent Contract 即可註冊到 CSP 並被 Router dispatch。
```

---

## 2. 樣板應支援的目標

`anila-agent` 的價值在於幫 dev 快速做到：

```text
/manifest
/healthz
/readyz
/invoke
/v1/chat/completions optional compatibility
anila-agent-events-v1 SSE
AgentError 標準化
trace headers
classification metadata
artifact event
tool / memory gateway 範例
```

它應該是 `Silver` 或 `Gold` compatibility profile 的參考實作。

---

## 3. 建議文件架構

```text
packages/anila-agent/
├── README.md
├── README.en.md
├── docs/
│   ├── 01-quickstart.md
│   ├── 02-anila-agent-contract.md
│   ├── 03-bring-your-own-framework.md
│   ├── 04-langchain-adapter.md
│   ├── 05-streaming-events.md
│   ├── 06-manifest-and-registration.md
│   ├── 07-trace-and-audit.md
│   ├── 08-tool-memory-artifact-gateways.md
│   └── 09-conformance.md
```

最重要的是新增：

```text
Bring Your Own Framework
LangChain Adapter
Contract First
```

避免 dev 誤會 ANILA 強迫他使用官方 Agent framework。

---

## 4. 建議程式結構

```text
packages/anila-agent/anila_agent/
├── app.py                         # FastAPI app factory
├── config.py
├── manifest.py
├── serving/
│   ├── service_wrapper.py
│   ├── routes.py
│   └── openai_compat.py
├── harness/
│   ├── runtime.py
│   ├── context.py
│   ├── event_emitter.py
│   ├── errors.py
│   ├── trace.py
│   ├── budget.py
│   └── cancellation.py
├── adapters/
│   ├── langchain.py
│   ├── llamaindex.py
│   └── plain_function.py
├── gateways/
│   ├── tools.py
│   ├── memory.py
│   └── artifacts.py
└── examples/
    ├── minimal_agent.py
    ├── langchain_agent.py
    ├── streaming_agent.py
    ├── artifact_agent.py
    └── error_handling_agent.py
```

---

## 5. Manifest auto-generation

樣板應讓 dev 用程式宣告 Agent：

```python
from anila_agent import AnilaAgent

agent = AnilaAgent(
    id="image-generator",
    version="1.2.0",
    display_name="Image Generator",
    capabilities=["image_generation", "artifact_generation"],
    input_modes=["chat"],
    output_modes=["sse", "artifact", "image"],
    classification_ceiling="confidential",
    required_scopes=["agent.invoke.image"],
    supports_streaming=True,
    supports_resume=False,
)
```

然後自動提供：

```text
GET /manifest
GET /healthz
GET /readyz
POST /invoke
POST /v1/chat/completions
```

---

## 6. LangChain adapter 範例

樣板應示範如何把 LangChain 接到 ANILA contract。

概念：

```python
from anila_agent.adapters.langchain import LangChainAgentAdapter

adapter = LangChainAgentAdapter(
    runnable=my_langchain_runnable,
    stream_tokens=True,
)

agent = AnilaAgent.from_adapter(
    id="langchain-demo",
    adapter=adapter,
    capabilities=["knowledge_search", "chat"],
)
```

Adapter 做的事：

```text
ANILA InvocationEnvelope -> LangChain input
LangChain callbacks -> anila.agent.delta / tool events
LangChain exceptions -> AgentError
LangChain final output -> agent.completed
```

這能明確傳達：

```text
ANILA 不取代 LangChain；
ANILA 提供治理與互通外殼。
```

---

## 7. Plain function adapter

對簡單 Agent，應支援：

```python
async def run(ctx):
    await ctx.stream.status("開始處理")
    await ctx.stream.delta("處理中...")
    return "完成"

agent = AnilaAgent.from_function(
    id="simple-agent",
    run=run,
    capabilities=["chat"]
)
```

這讓 dev 不需要理解完整框架也能起步。

---

## 8. Event emitter

樣板應避免讓 dev 手刻 SSE。

應提供：

```python
await ctx.stream.started()
await ctx.stream.status("正在查詢資料")
await ctx.stream.delta("找到相關內容")
await ctx.stream.artifact_created(artifact)
await ctx.stream.completed(usage={"tool_calls": 1})
```

底層輸出：

```text
event: anila.agent.status
data: {"message":"正在查詢資料"}
```

---

## 9. Error helper

樣板應提供標準錯誤：

```python
from anila_agent.errors import AgentToolFailed

raise AgentToolFailed(
    safe_message="影像生成服務暫時無法使用。",
    retryable=True,
)
```

不要讓 dev 回傳 raw exception。

---

## 10. Trace helper

樣板應自動處理：

```text
trace_id
span_id
parent_span_id
agent.invocation.started
agent.tool.call
agent.artifact.created
agent.completed
agent.error
```

開發者只需要：

```python
async with ctx.trace.span("custom.step", attributes={"foo": "bar"}):
    ...
```

---

## 11. Tool / Memory / Artifact gateway 範例

這些不應是強制，但樣板應提供正確示範。

```python
docs = await ctx.memory.search("...", top_k=5)
result = await ctx.tools.call("image.generate", payload)
artifact = await ctx.artifacts.create(
    type="image",
    title="示意圖",
    content=result.bytes,
)
```

重點是傳達：

```text
不要直接連 CSP DB
不要任意打未治理 endpoint
不要自行繞過 classification
```

---

## 12. CLI 建議

```bash
anila-agent init my-agent
anila-agent serve
anila-agent validate-manifest
anila-agent conformance --target http://localhost:8080
anila-agent export-manifest
```

這些 CLI 可以降低開發者 onboarding 成本。

---

## 13. 範例專案建議

```text
examples/
├── minimal/
├── langchain/
├── llamaindex/
├── artifact-generator/
├── streaming/
├── error-handling/
└── legacy-service-adapter/
```

每個範例都應說明：

```text
如何啟動
如何看 /manifest
如何測 /invoke
如何註冊到 CSP
如何跑 conformance
```

---

## 14. README 應新增的關鍵段落

建議在 README 開頭放：

```text
anila-agent 是 ANILA 官方 Agent starter/template。
你不一定要使用本套件才能接入 ANILA。
任何 LangChain、LlamaIndex、自製 HTTP service 或既有內網服務，
只要符合 ANILA Agent Contract，都可以透過 CSP 註冊並由 Router dispatch。

本套件提供的是 reference implementation：
- manifest generation
- /invoke endpoint
- streaming events
- trace helpers
- error normalization
- tool / memory / artifact gateway examples
```

---

## 15. 與 `anila-core` 的依賴方向

建議維持：

```text
anila-agent 可以依賴 anila-core 的 contract definitions
anila-core 不應依賴 anila-agent
```

也就是：

```text
anila-core
  └── defines contracts

anila-agent
  └── implements contracts as official template
```

避免把 template 變成平台核心 dependency。

---

## 16. 測試建議

樣板本身應測：

```text
manifest generation
/invoke envelope validation
SSE event emitter
LangChain adapter callback mapping
error normalization
trace header propagation
artifact event
OpenAI-compatible endpoint
```

但這是樣板品質，不是 ANILA 本體 correctness 的唯一依據。

ANILA 本體仍應用 `anila-core` 的 conformance / AgentClient / StreamBridge 測外部任意 Agent。

---

## 17. P0 / P1 / P2 更新路線

### P0

```text
1. README 定位修正：optional template，不是強制 runtime
2. manifest auto-generation
3. /invoke 標準 endpoint
4. event emitter
5. standard error helper
6. minimal + streaming examples
```

### P1

```text
1. LangChain adapter
2. OpenAI-compatible endpoint
3. trace helper
4. artifact gateway example
5. conformance CLI integration
```

### P2

```text
1. LlamaIndex adapter
2. legacy service adapter
3. durable invocation / resume sample
4. approval_required event sample
5. sidecar deployment guide
```

---

## 18. 最終結論

`anila-agent` 仍然重要，但它的重要性不是「強制平台所有 Agent 用它」，而是：

```text
降低開發者接入成本
展示 ANILA Agent Contract 最佳實務
作為 conformance 參考實作
提供 LangChain / 自製服務的 adapter 範例
```

一句話總結：

> `anila-core` 定義 ANILA 的 Agent 互通規則；`anila-agent` 示範如何漂亮地遵守這些規則。

# `make_agent_tool` vs `as_tool` — 兩條 sub-routine 入口的決策對照

> **適用範圍**:anila-agent 內部 doc。對應 ANILA enhancement roadmap §4.3 P2-1
> (上游參考:`openai-agents` SDK `Agent.as_tool`)。

## TL;DR

| 想要... | 用 |
|---|---|
| 最少參數,API shape 跟 openai-agents 上游一樣 | `as_tool(sub_agent, name, desc)` |
| 客製 `prefix_strategy="fork"` / `fork_point` / `timeout` / `metadata` / tracer / hooks | `make_agent_tool(sub_agent, ...)` |
| 走 SDK 原生 nested `Runner.run`(無 prompt cache prefix)| `sub_agent.as_tool(...)`(SDK 自帶) |

兩者底層是**同一個** `make_agent_tool` factory,行為 100% 等價 — `as_tool` 只是 surface 較小的 shorthand。

---

## 1 · 背景:sub-routine pattern 跟 handoff 的差別

| 維度 | handoff | sub-routine (AgentTool / as_tool) |
|---|---|---|
| 控制權 | **轉移** — parent agent 結束 | **不轉移** — parent 等 sub-agent return |
| 下個 agent 看到的 input | 整段 conversation history | 只看到 tool args (`prompt` + `context_summary`) |
| 完成後 | conversation 由 sub-agent 接 | 結果以 tool result 回給 parent,parent 繼續推進 |
| 適合場景 | 「轉接給專門人員」(e.g. 客服轉技術) | 「呼叫專家做一件事再回來」(e.g. RAG 先 search 再 summarize) |

本 doc 只討論 sub-routine 那條(handoff 走另外的 module)。

---

## 2 · 兩條入口的 API surface 對照

### 2.1 `make_agent_tool` (P0-8,full control)

```python
from anila_agent.core import make_agent_tool

spec = make_agent_tool(
    retriever,
    name="search",                # 預設 f"call_{sub_agent.name}"
    description="搜尋 top-5 chunks",  # 預設從 sub_agent.instructions 取前 ~100 字
    metadata=ToolMetadata(cost_estimate="medium", category="rag"),
    prefix_strategy="fork",        # "share" / "fork" (P1-1)
    fork_point=8,                  # fork 策略下截斷的 message index
    timeout_seconds=120.0,
    runner=my_custom_runner,       # 預設 agents.Runner.run
    tracer=my_tracer,
    hook_registry=my_hooks,        # P1-17 SUBAGENT_DISPATCH_* event
    event_bus=my_bus,
)
main.tools.append(spec.tool)
```

8 個進階參數全部可調,適合**真的需要客製**的場景(RAG / 多階段 agent / 需要 prompt cache 命中)。

### 2.2 `as_tool` (P2-1, shorthand)

```python
from anila_agent.core import as_tool

spec = as_tool(retriever, "search", "搜尋 top-5 chunks")
main.tools.append(spec.tool)
```

只接 `tool_name` / `tool_description`,其他全預設(`prefix_strategy="share"` / 預設 timeout / 預設 metadata)。
**API shape 對齊 openai-agents 上游 `Agent.as_tool` 前兩個必傳參數**,讓習慣 SDK API 的開發者不必查 ANILA 客製介面。

### 2.3 `Agent.anila_as_tool` (opt-in method)

```python
from anila_agent.core import enable_anila_as_tool_method
enable_anila_as_tool_method()  # 應用啟動時呼叫一次

spec = retriever.anila_as_tool("search", "搜尋 top-5 chunks")
```

把 `anila_as_tool` method patch 到 `agents.Agent` class 上,讓寫法跟上游 `Agent.as_tool` 對齊(但 return type 不同 — ANILA 版回 `AgentTool` spec,SDK 版回 `FunctionTool`)。

> ⚠️ 不取名 `as_tool` 是為了**不覆蓋上游 SDK 同名 method**。兩者語意不同(SDK 走 nested `Runner.run`,ANILA 版走 prompt-cache prefix dispatch),共存才安全。

---

## 3 · 跟 openai-agents 上游 `Agent.as_tool` 對照

上游位置:`openai-agents/src/agents/agent.py:508`,簽名:

```python
def as_tool(
    self,
    tool_name: str | None,
    tool_description: str | None,
    custom_output_extractor=None,
    is_enabled=True,
    on_stream=None,
    run_config=None,
    max_turns=None,
    hooks=None,
    previous_response_id=None,
    conversation_id=None,
    session=None,
    failure_error_function=default_tool_error_function,
    needs_approval=False,
    parameters=None,
    input_builder=None,
    include_input_schema=False,
) -> FunctionTool: ...
```

### 3.1 對應關係

| 上游參數 | ANILA 對應 | 備註 |
|---|---|---|
| `tool_name` | `tool_name` (`as_tool` 第 2 參數)| 對齊 |
| `tool_description` | `tool_description` (`as_tool` 第 3 參數)| 對齊 |
| `custom_output_extractor` | (未支援)| sub-agent final output 直接 str 化回給 parent;若需 extractor 請走 `make_agent_tool(runner=...)` 自寫 |
| `is_enabled` | (未支援)| 透過 `ToolMetadata` 或 ToolRegistry 過濾 |
| `on_stream` | (未支援)| ANILA streaming 走 `AnilaStreamRunner`(P1-7) |
| `run_config` | (未支援)| 走 ANILA `AppConfig.agent` |
| `max_turns` | (未支援)| 由 `runner` callable 控制(預設不限) |
| `hooks` | `hook_registry` (`as_tool_full` / `make_agent_tool`)| ANILA HookRegistry,非 SDK `RunHooks` |
| `previous_response_id` / `conversation_id` / `session` | (未支援)| ANILA session 由 `AssembledAgent.short_term` 持有 |
| `failure_error_function` | (固定 error JSON)| sub-agent 失敗一律回 `{"error": "..."}` JSON,不 raise |
| `needs_approval` | (未支援)| 走 ANILA HITL `RunState` + `approval` |
| `parameters` | (固定 schema)| schema 永遠是 `{prompt, context_summary}`(`additionalProperties: false`)|
| `input_builder` | (固定 builder)| `_compose_sub_agent_input` 統一組 |
| `include_input_schema` | (固定 false)| |

### 3.2 ANILA 獨有擴展點

ANILA 版多了以下上游沒有的能力:

| ANILA 參數 | 來源 / 目的 |
|---|---|
| `prefix_strategy="share"\|"fork"` | **P1-1 prompt-cache prefix** — byte-identical prefix 命中 vLLM `--enable-prefix-caching` |
| `fork_point` | fork 策略下截斷 parent message 的 index(預設 `DEFAULT_PREFIX_MESSAGE_COUNT`)|
| `timeout_seconds` | 預設 300s;timeout 回 error JSON 不 raise |
| `metadata: ToolMetadata` | ToolRegistry 過濾用(`category="agent"` / `cost_estimate="high"` 等)|
| `tracer` | ANILA Tracer span `agent_tool.dispatch.<sub_agent.name>` |
| `event_bus` | P1-17 `SUBAGENT_DISPATCH_START` / `SUBAGENT_DISPATCH_END` event |

---

## 4 · 多 agent 協作典型場景

### 4.1 RAG: retriever + writer 都 as_tool 給 main

```python
from agents import Agent
from anila_agent.core import as_tool

retriever = Agent(
    name="retriever",
    instructions="只跑 vector_search 並回 top-5 chunks(以 JSON list 字串輸出)",
    tools=[vector_search],
)

writer = Agent(
    name="writer",
    instructions="把 chunks 變成中文摘要,標註來源編號",
)

main = Agent(
    name="main",
    instructions=(
        "使用者問問題時,先呼叫 search 拿到 chunks,"
        "再呼叫 summarize 把 chunks 變成 ≤300 字摘要回給使用者。"
    ),
    tools=[
        as_tool(retriever, "search", "搜尋相關 chunks").tool,
        as_tool(writer, "summarize", "把 chunks 變摘要").tool,
    ],
)
```

兩個 sub-agent 都用 shorthand 包,**main 並未轉移控制權**(這跟 handoff 是關鍵差別)。
sub-agent 跑完一段 → 結果 str 化 → 回 main 當 tool result → main 繼續推進。

### 4.2 何時改用 `make_agent_tool`?

當以上 4.1 案例**需要追加任一進階參數**時:

```python
from anila_agent.core import make_agent_tool

retriever_tool = make_agent_tool(
    retriever,
    name="search",
    description="搜尋相關 chunks",
    prefix_strategy="fork",          # parent context 太長,fork 出去獨立跑
    fork_point=12,
    timeout_seconds=60.0,            # retriever 不該跑超過 60s
    metadata=ToolMetadata(category="rag", cost_estimate="medium"),
)
main.tools.append(retriever_tool.tool)
```

shorthand 跟 full-control 兩條路徑可在同一個 main agent 內混用(每個 sub-agent 各自挑)。

---

## 5 · 向後相容保證

* P2-1 不動 `make_agent_tool` / `register_agent_as_tool` / `AgentTool` 任何欄位 — P0-8 既有測試與用法 100% 保留。
* `as_tool` 是新 API,加在 `anila_agent.core.__init__` export 上,不破壞既有 import path。
* `enable_anila_as_tool_method()` 走 opt-in,**不安裝**就完全不影響 `Agent` class(import 本 module 本身只 import function,不執行 patch)。
* 與上游 SDK `Agent.as_tool` **不衝突**:method 名為 `anila_as_tool`,不會 shadow SDK 同名 method。

---

## 6 · 參考

* 上游 SDK:`openai-agents/src/agents/agent.py:508` (`Agent.as_tool`)
* ANILA P0-8:`anila_agent/core/agent_tool.py` (`make_agent_tool` 主 factory)
* ANILA P1-1:`anila_agent/core/prompt_cache.py` (byte-identical prefix)
* ANILA P1-17:`anila_agent/core/hooks.py` (`SUBAGENT_DISPATCH_*` event)
* Roadmap §4.9:`docs/agent-framework/openai-agents-python-deep-dive.md` (`Agent.as_tool` 深入分析)

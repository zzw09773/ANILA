# openai-agents-python — anila-agent template deep dive

> **目的**：以 `anila-agent` 這個 sub-agent template 的視角,逐 module 對照最新解壓的 openai-agents SDK source,找出 **anila-agent 還沒用、應該借鑑、且工程可行** 的設計 pattern。
>
> **Source 位置**：`/home/aia/c1147259/ANILA/anila-agent/templete/openai-agents-python/` (本機,gitignored)
> **SDK 版本**:`0.17.3` (`pyproject.toml:3`),`RunState` schema `1.10` (`src/agents/run_state.py:131`)
> **anila-agent runtime 位置**:`/home/aia/c1147259/ANILA/anila-agent/anila_agent/`
> **既有 reference**:[`runtime-logic-openai-agents-deep-dive.md`](runtime-logic-openai-agents-deep-dive.md) — 該文以 **anila-core 與 AgenticRAG** 兩個 consumer 視角拆 SDK;本文不 contradict、focus 在 **「anila-agent template 跟最新 SDK 的 gap」** 這個獨立角度

---

## 0 · TL;DR(給趕時間的 reviewer)

| 落點 | 借鑑 pattern | 優先級 | 預估 |
|---|---|---|---|
| `anila_agent/core/hooks.py` | RunHooks 補滿 `on_agent_start` + `on_handoff` + 補 `AgentHooks` per-agent 層 | **P0** | 1d |
| `anila_agent/core/agent.py` | `Agent.handoffs` + `handoff()` + extensions/handoff_filters | **P0** | 2-3d |
| `anila_agent/core/guardrails.py` (new) | `InputGuardrail` / `OutputGuardrail` decorator + tripwire | **P0** | 1d |
| `anila_agent/tools/guardrails.py` (new) | `ToolInputGuardrail` / `ToolOutputGuardrail` + 三 behavior | **P0** | 1d |
| `anila_agent/tracing/` (new) | `Trace` + `Span` + `TracingProcessor` + 在 hooks bridge fire span | **P0** | 2-3d |
| `anila_agent/mcp/` (new) | `MCPServer` ABC + `MCPServerManager` + `Agent.mcp_servers` 動態 tool 合併 | **P1** | 2-3d |
| `anila_agent/memory/compaction.py` (new) | `CompactingSession` decorator pattern(不抄 OpenAI Responses API,自寫 LLM-summarize 版) | **P1** | 1-2d |
| `anila_agent/core/state.py` (new) | `RunState.to_json` + `from_json` HITL pause/resume + tool approval | **P1** | 1w+ |
| `anila_agent/core/runner.py` | `Runner.run_streamed` + `StreamEvent` 三層;`AnilaRunner.send_streamed` | **P1** | 2-3d |
| `anila_agent/core/agent.py` | `Agent.as_tool()` — sub-agent 當 tool 用(vs handoff 轉移控制) | **P2** | 0.5d |
| `anila_agent/providers/retry.py` (new) | `RetryPolicy` + `RetryDecision` + `retry_policies.network_error() / retry_after()` | **P2** | 1d |
| `anila_agent/extensions/handoff_prompt.py` (new) | `prompt_with_handoff_instructions` 把 handoff 規則塞進 instructions | **P2** | 0.5d |
| `anila_agent/tools/base.py` | `FunctionTool.tool_input_guardrails / tool_output_guardrails` 直接掛在 tool 上(取代 hook 全局 regex matcher) | **P2** | 1d |
| `anila_agent/extensions/visualization.py` (new) | `draw_graph` graphviz 視覺化 agent graph | **P3** | 0.5d |
| `anila_agent/cli/repl.py` | `agents.repl.run_demo_loop` — 對齊 streaming demo loop 寫法 | **P3** | 0.5d |

**不該抄**:`realtime/`、`voice/`、`sandbox/`(全套 manifest 太重,等真要做 untrusted exec 再評估)、`OpenAIResponsesCompactionSession`(綁 OpenAI 雲端 API,改用自寫 LLM-summarize 版即可)。

---

## 1 · SDK 簡介

### 1.1 版本與定位

`openai-agents-python 0.17.3` (`pyproject.toml:3`) 是 OpenAI 官方的 agent runtime,把「agent run = LLM + tools + handoffs + guardrails + memory + tracing」拆成 ~30 個檔的 declarative framework。SDK 自我定位:

- **無 vendor lock-in 的 model layer**:`agents.extensions.models.litellm_model.LitellmModel` 撐起任何 OpenAI-compat HTTP endpoint
- **declarative Agent**:`Agent` 是 `@dataclass`,state 在 `RunState` / `Session` / `RunContextWrapper`,instance 可多執行緒共享
- **server-managed conversation 與 local session 並存**:`Session` Protocol 4-method 抽象覆蓋 sqlite / OpenAI Conversations / 自寫 backend
- **runner 為 hidden API**:`Runner.run()` 是 façade,`run_internal/` 22 個檔是 advanced subclass hook point

### 1.2 跟 anila-agent 的關係

`anila-agent` 是 **ANILA 平台 sub-agent template** — 開發者 clone 一份就能跑的 Agentic RAG starter。它把 openai-agents SDK 當 **runtime base**:

```
anila-agent (template/starter)
  ├─ core/runner.py      : 包 agents.Runner + 加 hook fire + AnilaRunHooks bridge
  ├─ core/agent.py       : 用 agents.Agent dataclass + 自家 tool registry + memdir 注入 instructions
  ├─ core/hooks.py       : 6 個 Claude Code-style hook event 接到 agents.RunHooks 上
  ├─ memory/store.py     : 自寫的 memdir(claude-code-src port)
  ├─ memory/long_term.py : LLM-driven memory recall(用 agents.Runner one-shot)
  ├─ memory/short_term.py: 直接用 agents.memory.SQLiteSession
  ├─ tools/base.py       : 包 agents.function_tool + 加 ToolMetadata(read_only/destructive)
  ├─ tools/registry.py   : 自家 dedup loader
  ├─ models/openai_compatible.py : 包 LitellmModel + ModelSettings filter
  └─ cli/                : 自寫 REPL + slash command
```

`anila_agent/__init__.py:1-29` 暴露的 public surface 已對齊 SDK 慣例 — `AnilaRunner`, `HookEvent`, `HookSpec`, `RunSummary` 等;但 **大量 SDK feature 還沒接進來**。

### 1.3 跟既有 `runtime-logic-openai-agents-deep-dive.md` 的分工

| 文件 | 觀察視角 | 落點 | 寫作時間 |
|---|---|---|---|
| `runtime-logic-openai-agents-deep-dive.md` | anila-core(平台 router/engine)+ AgenticRAG(RAG agent) | Pillar 1+2 framework / RAG flavor | 2026-05-02,RunState 1.9 |
| **本文(`openai-agents-python-deep-dive.md`)** | **anila-agent template(sub-agent starter)** | template 本身的 module gap | 2026-05-26,RunState 1.10 |

兩文 module 列表會重疊,但這份的**結論不一樣**:既有文件講「該不該在平台層做」;本文講「sub-agent template 自身缺什麼 feature、要不要補」。

---

## 2 · 架構俯瞰

### 2.1 SDK top-level package layout(`src/agents/`)

```
src/agents/
├── __init__.py                公共 API 出口(~150 個 symbol)
├── agent.py                   Agent / AgentBase dataclass(615 行)
├── agent_output.py            output_type / AgentOutputSchemaBase
├── agent_tool_input.py        agent-as-tool 的 parameter schema
├── agent_tool_state.py        nested run result 透過 contextvars 帶回
├── exceptions.py              AgentsException 家族(11 個子類)
├── function_schema.py         Python signature → JSON Schema 自動生成
├── guardrail.py               InputGuardrail / OutputGuardrail framework(343 行)
├── tool_guardrails.py         ToolInputGuardrail / ToolOutputGuardrail + 三 behavior(279 行)
├── handoffs/                  multi-agent delegation(__init__ 347 行 + history 368 行)
├── items.py                   RunItem 12 種子型 + ItemHelpers
├── lifecycle.py               RunHooks / AgentHooks 兩 base class(199 行)
├── memory/                    Session Protocol + 4 個 backend
│   ├── session.py             Protocol 抽象(150 行)
│   ├── sqlite_session.py      sqlite backend
│   ├── openai_conversations_session.py
│   └── openai_responses_compaction_session.py  decorator + threshold trigger(521 行)
├── extensions/memory/         社群 backend
│   ├── advanced_sqlite_session.py  conversation branching + usage analytics
│   ├── async_sqlite_session.py     aiosqlite 版
│   ├── encrypt_session.py          Fernet 加密
│   ├── redis_session.py
│   ├── dapr_session.py
│   ├── mongodb_session.py
│   └── sqlalchemy_session.py
├── mcp/                       Model Context Protocol(server 1669 行 + manager 411 行 + util 706 行)
│   ├── server.py              MCPServer ABC + Stdio / SSE / StreamableHttp 三 transport
│   ├── manager.py             MCPServerManager async context manager(drop_failed_servers / reconnect)
│   └── util.py                MCP tool → FunctionTool 轉換
├── models/                    Provider 抽象 + OpenAI / Multi 實作
├── prompts.py                 OpenAI Prompts API 整合
├── repl.py                    run_demo_loop — 簡單 streaming REPL(76 行,可借鑑)
├── result.py                  RunResult / RunResultStreaming + to_state / approve / reject
├── retry.py                   ModelRetrySettings + RetryPolicy + retry_policies factory(361 行)
├── run.py                     Runner / AgentRunner — 1863 行公共 entry
├── run_config.py              RunConfig(per-run 設定 ~38 個欄位)
├── run_context.py             RunContextWrapper / AgentHookContext
├── run_error_handlers.py      error → retry / abort decision
├── run_internal/              22 個檔的 hidden runtime(turn_loop, turn_resolution, tool_execution...)
├── run_state.py               RunState durable snapshot — schema 1.10,3305 行
├── sandbox/                   tool 沙箱(manifest + capabilities + apply_patch + Docker/UnixLocal/...)
├── stream_events.py           三層 StreamEvent(Raw / RunItem / AgentUpdated)
├── tool.py                    8 種 Tool 子型 + FunctionTool field 群
├── tool_context.py            ToolContext typing
├── tracing/                   span tree + processor pipeline + OpenAI exporter
├── voice/                     語音輸入(本案外)
├── realtime/                  WebSocket realtime API(本案外)
└── extensions/visualization.py  draw_graph(agent 圖以 graphviz 視覺化)
```

### 2.2 anila-agent 現況 layout

```
anila_agent/
├── __init__.py            export AnilaRunner / Event / HookSpec / build_agent
├── main.py                CLI entry(python -m anila_agent.main)
├── core/
│   ├── agent.py           build_agent() — 組 agents.Agent + 注入 memdir index 到 instructions
│   ├── runner.py          AnilaRunner — wrap agents.Runner + RunSummary + abort tracking
│   ├── hooks.py           6 hook events 接到 agents.RunHooks(只實作 on_llm_*, on_tool_*, on_agent_end)
│   └── events.py          EventBus(observer pattern)
├── tools/
│   ├── base.py            @anila_tool decorator + ToolMetadata
│   ├── registry.py        dedup loader
│   ├── filesystem_tools.py
│   └── rag_tools.py
├── memory/
│   ├── store.py           MemdirStore(claude-code-src memdir port)
│   ├── short_term.py      包 agents.memory.SQLiteSession
│   ├── long_term.py       LLM-driven recall(用 agents.Runner one-shot)
│   └── summarizer.py      auto-memory trigger
├── models/
│   ├── openai_compatible.py 包 LitellmModel + filter ModelSettings keys
│   └── schemas.py         HookOutput / MemoryFrontmatter pydantic models
├── retrieval/             pgvector adapter
├── prompts/               yaml prompt loader
├── cli/                   REPL + slash command
└── utils/                 config / logging
```

### 2.3 模組關係圖

```
┌─────────────────────────────────────────────────────────────────┐
│                       anila-agent template                      │
│                                                                 │
│  AnilaRunner ──fires──▶ HookEvent (6)                          │
│       │                      │                                 │
│       │ wraps                ▼                                 │
│       ▼               HookRegistry ──matches──▶ HookSpec        │
│  agents.Runner ◀───────┘                                       │
│       │                                                         │
│       ├─ uses ──▶ agents.Agent (dataclass)                      │
│       │                ├─ tools (FunctionTool only)             │
│       │                ├─ model (LitellmModel)                  │
│       │                └─ instructions (memdir index 注入)       │
│       │                                                         │
│       ├─ uses ──▶ agents.memory.SQLiteSession (short-term)      │
│       │                                                         │
│       └─ AnilaRunHooks (bridge agents lifecycle → anila hooks)  │
│                  │                                              │
│         on_llm_start  → emit event "llm_started"               │
│         on_llm_end    → emit event "llm_ended"                 │
│         on_tool_start → fire PreToolUse                        │
│         on_tool_end   → fire PostToolUse                       │
│         on_agent_end  → fire Stop                              │
│                                                                 │
│   [尚未接的 lifecycle 點:on_agent_start, on_handoff]          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3 · anila-agent 已用 vs 未用 — 對照表

### 3.1 已用的 SDK symbols

| `from agents` import | 用在哪 | 用法 |
|---|---|---|
| `Agent` | `core/agent.py:8`, `memory/long_term.py:20`, `memory/summarizer.py:18` | 主 agent + memory-selector one-shot agent + summarizer |
| `Runner` | `core/runner.py:17`, `memory/long_term.py:20`, `memory/summarizer.py:18` | run loop entry + one-shot LLM call |
| `ModelSettings` | `core/agent.py:8`, `memory/long_term.py:20`, `models/openai_compatible.py:11` | 模型 tuning |
| `RunHooks` | `core/hooks.py:24` | `AnilaRunHooks(RunHooks[Any])` 繼承 |
| `RunContextWrapper` | `core/hooks.py:24` | hook 簽名 type |
| `Tool` | `core/hooks.py:24` | hook 簽名 type |
| `FunctionTool` | `tools/base.py:17`, `tools/registry.py:8` | wrapper / registry typing |
| `function_tool` | `tools/base.py:17` | decorator base |
| `agents.exceptions.UserError` | `core/hooks.py:244,248` | hook block / abort 時 raise |
| `agents.items.ModelResponse / TResponseInputItem` | `core/hooks.py:25` | hook callback 簽名 |
| `agents.memory.session.Session` | `core/agent.py:9` | typing only |
| `agents.memory.sqlite_session.SQLiteSession` | `memory/short_term.py:11` | 短期記憶 backend |
| `agents.models.interface.Model` | `core/agent.py:10`, `memory/long_term.py:21`, `models/openai_compatible.py:13` | typing |
| `agents.extensions.models.litellm_model.LitellmModel` | `models/openai_compatible.py:12` | model adapter |

### 3.2 未用(本文重點)

| SDK module / symbol | anila-agent 狀態 | gap 嚴重度 |
|---|---|---|
| `agents.handoff` / `Handoff` / `handoffs/__init__.py` | **完全未用** | **嚴重**:sub-agent template 沒有 multi-agent 能力 |
| `agents.guardrail` / `InputGuardrail` / `OutputGuardrail` | **完全未用** | **嚴重**:input safety 完全靠 hook regex,沒結構化 |
| `agents.tool_guardrails` / `ToolInputGuardrail` / `ToolOutputGuardrail` | **完全未用** | **嚴重**:per-tool guardrail 跟 hook 全局 regex 混在一起 |
| `agents.tracing/*` (全套) | **完全未用** | **嚴重**:沒結構化 trace,debug 痛苦 |
| `agents.mcp/*` (全套) | **完全未用** | **重要**:sub-agent 接不上 enterprise MCP server(Slack/GitHub/Confluence) |
| `agents.RunState` / `RunResult.to_state / approve / reject` / interruptions | **完全未用** | **重要**:HITL pause/resume 不可能;tool approval 無路徑 |
| `agents.AgentHooks` (per-agent) | **完全未用**(只用了 `RunHooks` 全 run 級) | **中**:無法針對單一 sub-agent 掛 hook |
| `Runner.run_streamed` / `StreamEvent` 三層 | **完全未用** | **中**:CLI 只能 await 完整 final_output,不能逐字串流 |
| `Agent.as_tool()` agent-as-sub-routine | **完全未用** | **中**:RAG 場景的「retriever agent 當 sub-tool」少這條路 |
| `agents.memory.OpenAIResponsesCompactionSession` decorator | **完全未用** | **小**(綁 OpenAI 雲,但 decorator pattern 值得抄) |
| `agents.retry / ModelRetrySettings / retry_policies` | **完全未用** | **中**:LiteLLM 內建 retry 沒接到 anila 自家 advice |
| `agents.extensions.handoff_filters` | **完全未用**(handoff 還沒接) | 中 |
| `agents.extensions.handoff_prompt.prompt_with_handoff_instructions` | **完全未用** | 小 |
| `agents.extensions.visualization.draw_graph` | **完全未用** | 小(debug 友善加分) |
| `agents.repl.run_demo_loop` | **完全未用**(anila 自寫 REPL) | 小(anila CLI 比較完整,但 stream 寫法可對齊) |
| `agents.sandbox/*` | **完全未用** | **不抄**(本案 scope 外) |
| `agents.realtime/*` / `voice/*` | **完全未用** | **不抄**(本案 scope 外) |
| `RunConfig` ~38 個欄位 | **沒用 — `Runner.run` 沒傳 `run_config`** | **中**:tracing / handoff filter / call_model_input_filter / tool_error_formatter 全部走預設 |
| `Agent.input_guardrails / output_guardrails` | dataclass field 沒填 | 跟 guardrail 一起補 |
| `Agent.handoffs` | dataclass field 沒填 | 跟 handoff 一起補 |
| `Agent.tool_use_behavior` 的 `StopAtTools` / `ToolsToFinalOutputFunction` 變形 | 只用 string literal | **小**(yaml 已暴露 `tool_use_behavior: stop_on_first_tool` 兩值 string) |

---

## 4 · 可借鑑 pattern 詳列

### 4.1 [P0] Handoffs — multi-agent delegation

#### 來源

- `src/agents/handoffs/__init__.py:42-181`(`HandoffInputData` + `Handoff` dataclass)
- `src/agents/handoffs/__init__.py:183-333`(`handoff()` builder function + 三個 overload)
- `src/agents/handoffs/history.py:1-368`(`nest_handoff_history` + `default_handoff_history_mapper`)
- `src/agents/extensions/handoff_filters.py`(`remove_all_tools` 等常用 filter)
- `src/agents/extensions/handoff_prompt.py:3-19`(`RECOMMENDED_PROMPT_PREFIX` + `prompt_with_handoff_instructions`)
- 範例:`examples/handoffs/message_filter.py`

#### Public API 樣貌

```python
from agents import Agent, handoff, HandoffInputData
from agents.extensions import handoff_filters

billing_agent = Agent(name="billing", instructions="...")
support_agent = Agent(
    name="support",
    instructions="如果用戶問計費問題,handoff 給 billing_agent",
    handoffs=[
        handoff(
            billing_agent,
            input_filter=handoff_filters.remove_all_tools,
            is_enabled=lambda ctx, agent: ctx.context.user.kyc_verified,
        ),
    ],
)
result = await Runner.run(support_agent, "我的帳單錯了")
print(result.last_agent.name)  # "billing"
```

#### 為什麼對 anila-agent 有價值

1. **sub-agent template 之名,需有 multi-sub-agent 之實**:目前 `anila-agent` 名字是「sub-agent」但本身**沒有 sub-agent 概念** — 只有 main agent + tools。對使用者來說 starter 缺一個顯眼能力。
2. **RAG 場景需求清楚**:retrieval-then-answer 是典型 multi-agent。`RetrievalAgent` 跑 vector_search、`AnswerAgent` 接 chunks + query 生答案、`VerifierAgent` 檢查 citation 完整性。
3. **`is_enabled` 動態開關**:e.g. user 沒驗 KYC 不能 handoff 到 billing — 對企業用戶很實用。
4. **handoff filter 是純函數 reducer**:測試容易。

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/core/handoffs.py`

```python
# 簡化重寫,不抄 SDK source — 只 re-export + 加 anila-flavor 包裝
from agents import Handoff, HandoffInputData, HandoffInputFilter, handoff
from agents.extensions import handoff_filters

# anila 自家 filter
def keep_last_n_items(n: int) -> HandoffInputFilter:
    def _filter(data: HandoffInputData) -> HandoffInputData:
        return data.clone(input_items=data.new_items[-n:])
    return _filter

__all__ = ["Handoff", "HandoffInputData", "handoff", "handoff_filters", "keep_last_n_items"]
```

**改 `anila_agent/core/agent.py:133-140`**:在 `Agent(...)` 建構時把 `handoffs=` 帶上(從 config 讀):

```python
agent = Agent[Any](
    name=config.agent.name,
    instructions=instructions,
    tools=all_tools,
    handoffs=_build_handoffs_from_config(config.agent.handoffs),  # ← 新加
    model=model,
    model_settings=model_settings,
    tool_use_behavior=config.agent.tool_use_behavior,
)
```

**改 `anila_agent/core/hooks.py`**:`AnilaRunHooks` 補 `on_handoff` → fire 一個新 hook event `HookEvent.AGENT_HANDOFF`(可選,P1)。

**Config schema**:`configs/agents.yaml` 增 `handoffs: list[HandoffSpec]`,每個 spec 寫 `name` / `instructions` / `tools` / `input_filter` / `is_enabled`。

#### 工作量 / 優先級

- **工作量**:2-3d(core 包裝 0.5d、agent.py 改動 0.5d、handoff config schema + factory 0.5d、整合測試 + example 1-2d)
- **優先級**:**P0** — sub-agent template 缺這個本質能力。

---

### 4.2 [P0] Guardrails — 兩層輸入 / 輸出檢查

#### 來源

- `src/agents/guardrail.py:1-343`(`InputGuardrail` / `OutputGuardrail` + `@input_guardrail` decorator + `GuardrailFunctionOutput` + tripwire)
- `src/agents/tool_guardrails.py:1-279`(`ToolInputGuardrail` / `ToolOutputGuardrail` + `AllowBehavior` / `RejectContentBehavior` / `RaiseExceptionBehavior` 三 behavior)
- `src/agents/exceptions.py:121-180`(`InputGuardrailTripwireTriggered` / `OutputGuardrailTripwireTriggered` / `ToolInputGuardrailTripwireTriggered` / `ToolOutputGuardrailTripwireTriggered`)
- `src/agents/tool.py:322,325,505-535`(`FunctionTool` 直接帶 `tool_input_guardrails` / `tool_output_guardrails` field)
- 範例:`examples/agent_patterns/input_guardrails.py`、`output_guardrails.py`、`streaming_guardrails.py`

#### Public API 樣貌

```python
from agents import GuardrailFunctionOutput, input_guardrail, output_guardrail
from agents.tool_guardrails import ToolGuardrailFunctionOutput, tool_input_guardrail

@input_guardrail
async def block_off_topic(ctx, agent, input_text) -> GuardrailFunctionOutput:
    is_off = await some_classifier(input_text)
    return GuardrailFunctionOutput(output_info={...}, tripwire_triggered=is_off)

@tool_input_guardrail
def block_pii_in_search(data) -> ToolGuardrailFunctionOutput:
    if contains_pii(data.context.tool_arguments):
        return ToolGuardrailFunctionOutput.reject_content("請去除個資後重試")
    return ToolGuardrailFunctionOutput.allow()

@function_tool(tool_input_guardrails=[block_pii_in_search])
def vector_search(query: str) -> list[dict]: ...

agent = Agent(
    name="rag",
    instructions="...",
    tools=[vector_search],
    input_guardrails=[block_off_topic],
    output_guardrails=[ensure_citations],
)
```

#### 為什麼對 anila-agent 有價值

1. **目前 anila-agent 的 hook regex matcher 是「重新發明的 guardrail」** — 用 regex match tool name 來阻擋(`anila_agent/core/hooks.py:122-128`),但無法精確區分:
   - 「整個 agent input 太敏感」(屬 input_guardrail)
   - 「這個 tool 的 args 含 PII」(屬 tool_input_guardrail)
   - 「這個 tool 結果該被 redact 後給 LLM 看」(屬 tool_output_guardrail.reject_content)
2. **三 behavior 是表達力差異關鍵**:`reject_content` 讓 tool 結果被替換成提示訊息塞回 LLM,讓 LLM「知道為什麼失敗」,**比直接 raise 好很多**。anila-agent 現在只有 abort/block 兩種,沒有 reject_content。
3. **decorator + dataclass field 設計很 Pythonic**,測試友善 — guardrail 是純函數可 unit test。
4. **`run_in_parallel=True` 預設並行**(`guardrail.py:100-103`)— guardrail 跟主 LLM call 同時跑,越早 trip 越早砍。anila-agent 現在只有 sequential。

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/core/guardrails.py`

```python
# Re-export SDK 介面,加 anila 自家 docstring + 範例
from agents import (
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    input_guardrail,
    output_guardrail,
)

__all__ = [
    "GuardrailFunctionOutput",
    "InputGuardrail",
    "OutputGuardrail",
    "InputGuardrailTripwireTriggered",
    "OutputGuardrailTripwireTriggered",
    "input_guardrail",
    "output_guardrail",
]
```

**新 module**:`anila_agent/tools/guardrails.py`

```python
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)

# 預設 anila guardrails
from anila_agent.tools.guardrails_builtin import (
    block_pii_input,
    redact_secrets_output,
    enforce_min_score,
)
```

**改 `anila_agent/core/agent.py:133-140`**:

```python
agent = Agent[Any](
    name=config.agent.name,
    instructions=instructions,
    tools=all_tools,
    input_guardrails=_load_guardrails(config.agent.input_guardrails),    # ← 新
    output_guardrails=_load_guardrails(config.agent.output_guardrails),  # ← 新
    handoffs=...,
    model=model,
    model_settings=model_settings,
    tool_use_behavior=config.agent.tool_use_behavior,
)
```

**改 `tools/base.py`**:`@anila_tool` 加 `tool_input_guardrails` / `tool_output_guardrails` 兩個可選 kwarg,內部傳給 `function_tool(...)`。

**改 `hooks.py`**:hook regex matcher **不廢**,定位改為「事件 audit / metrics 用」,**安全攔截改用 guardrail**。文件補一段「hook vs guardrail 怎麼選」決策表。

#### 工作量 / 優先級

- **工作量**:2d 整體(core/guardrails.py 0.5d、tools/guardrails.py 0.5d、agent.py + tool base 整合 0.5d、3 個內建 guardrail + tests 0.5d)
- **優先級**:**P0** — 跟 hook 的職責切分這件事一直缺,缺一個正式的 input safety 抽象。

---

### 4.3 [P0] Lifecycle hooks — `on_agent_start` + `on_handoff` + per-agent

#### 來源

- `src/agents/lifecycle.py:13-99`(`RunHooksBase` 7 個 callback)
- `src/agents/lifecycle.py:102-192`(`AgentHooksBase` per-agent 7 個 callback)
- `src/agents/lifecycle.py:195-198`(`RunHooks` / `AgentHooks` 是 `RunHooksBase[TContext, Agent]` 別名)

#### Public API 樣貌

```python
class MetricsHooks(RunHooks):
    async def on_agent_start(self, ctx, agent):
        ctx.context["agent_start_ts"] = time.time()

    async def on_agent_end(self, ctx, agent, output):
        ms = (time.time() - ctx.context["agent_start_ts"]) * 1000
        metrics.histogram("agent_latency_ms", ms, tags={"agent": agent.name})

    async def on_handoff(self, ctx, from_agent, to_agent):
        metrics.count("handoff", tags={"from": from_agent.name, "to": to_agent.name})

# Per-agent hooks
class BillingAuditHooks(AgentHooks):
    async def on_tool_start(self, ctx, agent, tool):
        await audit.log(f"billing 用 tool {tool.name}")

billing_agent.hooks = BillingAuditHooks()
```

#### 為什麼對 anila-agent 有價值

1. **anila-agent 的 `AnilaRunHooks` 漏實作 `on_agent_start` 跟 `on_handoff`**(`anila_agent/core/hooks.py:208-288` 只有 `on_llm_*` + `on_tool_*` + `on_agent_end`)
   - 後果:無法在 agent 啟動時插入「pre-flight check」 / 「context warm-up」邏輯
   - 加上 handoff 還沒接,`on_handoff` 也漏
2. **缺 per-agent `AgentHooks` 抽象**:目前 `HookSpec` 只有全 run 級 + tool_name regex,不能說「只給 billing_agent 加這個 audit hook」。
3. **6 anila hook events vs 7+7 SDK callbacks**:重疊但不完全 1:1。建議在 anila template 維持 6 個 declarative event(便於 yaml 配置)+ 底層更完整地接 SDK lifecycle,讓進階用戶可以直接寫 `RunHooks` subclass 注入。

#### 整合進 anila-agent 的概念草案

**改 `anila_agent/core/hooks.py`**:

1. `AnilaRunHooks` 加實作:
   - `on_agent_start` → emit `agent_started` event + fire 新 `HookEvent.AGENT_START`(可選)
   - `on_handoff(from_agent, to_agent)` → emit `handoff` event + fire `HookEvent.AGENT_HANDOFF`
2. 新增 `HookEvent.AGENT_START` 與 `HookEvent.AGENT_HANDOFF` 兩個 enum value
3. `AssembledAgent` 加 `agent_hooks: dict[str, AgentHooks]` field,`build_agent` 從 config 解析,在 `Agent(hooks=...)` 處填上(目前 `core/agent.py:133-140` 完全沒設 `hooks`)
4. 文件補「`HookEvent` (anila declarative) vs `RunHooks` (SDK low-level)」對照表 — 高階用戶寫 yaml,專家寫 subclass

#### 工作量 / 優先級

- **工作量**:1d(2 個 callback 補實作 0.3d、AgentHooks per-agent 路徑 0.4d、tests 0.3d)
- **優先級**:**P0** — 跟 handoff 一起做,沒有 `on_handoff` 連 audit log 都不完整

---

### 4.4 [P0] Tracing — span tree + processor pipeline

#### 來源

- `src/agents/tracing/__init__.py:1-43`(出口:`trace` / `agent_span` / `function_span` / `handoff_span` / `guardrail_span` / `mcp_tools_span` 等)
- `src/agents/tracing/processor_interface.py:1-143`(`TracingProcessor` ABC + `TracingExporter` ABC,6 個 abstract method)
- `src/agents/tracing/spans.py:31-263`(`Span` ABC + `NoOpSpan` + `SpanImpl`)
- `src/agents/tracing/span_data.py:28-427`(14 種 typed `SpanData`:`AgentSpanData` / `TurnSpanData` / `GenerationSpanData` / `FunctionSpanData` / `HandoffSpanData` / `GuardrailSpanData` / `MCPListToolsSpanData` 等)
- `src/agents/tracing/processors.py:522-705`(`BatchTraceProcessor` + `BackendSpanExporter`)
- `src/agents/tracing/provider.py`(`TraceProvider` — 多 processor 註冊)

#### Public API 樣貌

```python
from agents import trace, custom_span
from agents.tracing import add_trace_processor, TracingProcessor

# 自家 processor 送 OTel
class OTelProcessor(TracingProcessor):
    def on_span_end(self, span):
        otel_span = otel_tracer.start_span(span.span_data.type, ...)
        otel_span.set_attributes(span.span_data.export())
        otel_span.end(end_time=span.ended_at)
    def on_span_start(self, span): ...
    def on_trace_start(self, trace): ...
    def on_trace_end(self, trace): ...
    def shutdown(self): ...
    def force_flush(self): ...

add_trace_processor(OTelProcessor())

with trace("rag-query-flow"):
    result = await Runner.run(agent, input)
    # 自動產生 trace → AgentSpan → TurnSpan → GenerationSpan → FunctionSpan tree

with custom_span("vector-search-rrf-fusion") as span:
    span.span_data.set("top_k", 5)
    hits = await vector_search(...)
```

#### 為什麼對 anila-agent 有價值

1. **anila-agent 目前完全沒有 trace**:`EventBus` 只能事件流式聽,沒有 parent-child span 關係,無法回答「這個 query 為什麼慢?是 LLM call 還是 vector search?」這類 timing 問題
2. **typed `SpanData` 14 種**:已涵蓋 agent / turn / generation / function / handoff / guardrail / MCP / transcription 等 — 對 RAG agent 而言 90% 場景都被 SDK 預先定義好
3. **`TracingProcessor` 6-method 介面跟 OpenTelemetry 結構接得上**:寫一個 OTel adapter processor 就能串企業 APM(Datadog / Honeycomb / Jaeger)
4. **sub-agent template 帶 trace 出廠,用戶 fork 不用補**:這是 starter project 的賣點之一

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/tracing/__init__.py`

```python
# 直接 re-export — SDK 介面夠 clean
from agents.tracing import (
    Trace, Span, SpanData, TracingProcessor,
    add_trace_processor, set_trace_processors, flush_traces,
    trace, agent_span, custom_span, function_span, guardrail_span,
    handoff_span, generation_span, mcp_tools_span, turn_span,
)
```

**新 module**:`anila_agent/tracing/processors.py`

```python
# 兩個 anila-flavor processor:
# 1. JSONLProcessor — 寫 trace 到 .anila/traces/<trace_id>.jsonl(本地 debug)
# 2. OTLPProcessor — 送 OTel collector(production)
# 都繼承 TracingProcessor
```

**改 `core/runner.py:85-92`**:`AnilaRunner.send` 包進 `with trace(workflow_name=f"anila-{config.agent.name}")`:

```python
from agents import trace

async def send(self, prompt: str) -> RunSummary:
    ...
    with trace(workflow_name=f"anila/{self.assembled.agent.name}", group_id=self.session_id):
        try:
            result = await Runner.run(
                starting_agent=self.assembled.agent,
                input=prompt,
                hooks=hooks,
                session=self.assembled.short_term,
                max_turns=self.assembled.max_turns,
            )
        ...
```

**Config 增 `tracing:` section**:

```yaml
tracing:
  enabled: true
  exporters:
    - type: jsonl
      path: .anila/traces
    - type: otlp
      endpoint: http://otel-collector:4318
```

#### 工作量 / 優先級

- **工作量**:2-3d(re-export 0.2d、JSONL processor 0.5d、OTLP processor 1d、AnilaRunner trace wrap 0.3d、config + tests 1d)
- **優先級**:**P0** — observability minimum baseline,後續任何 RAG quality 改進都需要 trace 做 ground truth

---

### 4.5 [P1] MCP — Model Context Protocol server 整合

#### 來源

- `src/agents/mcp/server.py:223-526`(`MCPServer` ABC 抽象)
- `src/agents/mcp/server.py:528-1060`(`_MCPServerWithClientSession` 共用實作)
- `src/agents/mcp/server.py:1062-1184`(`MCPServerStdio` + `MCPServerStdioParams`)
- `src/agents/mcp/server.py:1185-1311`(`MCPServerSse` SSE transport)
- `src/agents/mcp/server.py:1311-1668`(`MCPServerStreamableHttp` HTTP streaming)
- `src/agents/mcp/manager.py:108-411`(`MCPServerManager` async context manager)
- `src/agents/mcp/util.py:1-705`(`MCPUtil.get_all_function_tools` MCP tool → FunctionTool 轉換)
- `src/agents/agent.py:188-244`(`Agent.mcp_servers` field + `get_all_tools` 動態 fetch)
- 範例:`examples/mcp/{filesystem,git,sse,streamablehttp,manager,tool_filter}_example/`

#### Public API 樣貌

```python
from agents.mcp import MCPServerStdio, MCPServerManager

slack = MCPServerStdio(
    name="slack",
    params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-slack"],
            "env": {"SLACK_TOKEN": "..."}},
)
github = MCPServerStdio(
    name="github",
    params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
)

async with MCPServerManager(
    [slack, github],
    drop_failed_servers=True,
    connect_in_parallel=True,
    connect_timeout_seconds=10.0,
) as manager:
    agent = Agent(name="ops-bot", mcp_servers=manager.active_servers)
    result = await Runner.run(agent, "找 Slack 上 #release 頻道最近 10 則,跟 GitHub PR 列表 cross-ref")
```

#### 為什麼對 anila-agent 有價值

1. **MCP 是 sub-agent 接 enterprise tool 的標準介面**:Slack / GitHub / Jira / Confluence / 各種 SaaS 都有 MCP server,sub-agent template 帶這條路出廠,用戶 fork 後改 yaml 就能接上
2. **`MCPServerManager.drop_failed_servers=True` 對 production 友善**:某個 MCP server 連不上(網路抖動 / 服務掛掉)不會炸整個 agent run
3. **`get_all_tools` 動態 fetch**(`src/agents/agent.py:246-266`):每 turn 重 list MCP tools,server 端加 tool / 撤 tool 下一輪自動反映,不用 restart agent
4. **Tool filter**(`src/agents/mcp/util.py` 的 `ToolFilter`):可以白名單 / 黑名單 MCP server 暴露的 tool,避免噪音 / 風險

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/mcp/__init__.py`

```python
# 直接 re-export
from agents.mcp import (
    MCPServer, MCPServerStdio, MCPServerSse, MCPServerStreamableHttp,
    MCPServerManager, MCPServerStdioParams, MCPServerSseParams, MCPServerStreamableHttpParams,
)
```

**新 module**:`anila_agent/mcp/loader.py`

```python
# yaml → MCPServer instance
def load_mcp_servers_from_config(cfg: list[dict]) -> list[MCPServer]:
    out = []
    for entry in cfg:
        transport = entry["transport"]  # "stdio" / "sse" / "streamable_http"
        if transport == "stdio":
            out.append(MCPServerStdio(name=entry["name"], params=entry["params"]))
        elif transport == "sse":
            out.append(MCPServerSse(name=entry["name"], params=entry["params"]))
        ...
    return out
```

**改 `core/agent.py:46-150`**:`build_agent` 接受 `mcp_manager: MCPServerManager | None` 參數;`Agent(..., mcp_servers=mcp_manager.active_servers if mcp_manager else [])`

**改 `core/runner.py`**:`AnilaRunner` 持有 `mcp_manager`;`start()` 進 `__aenter__` (`await manager.connect_all()`),session close 時 `cleanup_all()`

**Config 增 `mcp:` section**:

```yaml
mcp:
  servers:
    - name: slack
      transport: stdio
      params:
        command: npx
        args: ["-y", "@modelcontextprotocol/server-slack"]
        env: { SLACK_TOKEN: "${SLACK_TOKEN}" }
    - name: confluence
      transport: streamable_http
      params:
        url: http://confluence-mcp.internal:8080/mcp
  connect_in_parallel: true
  drop_failed_servers: true
  connect_timeout_seconds: 10
```

#### 工作量 / 優先級

- **工作量**:2-3d(re-export + loader 0.5d、AnilaRunner lifecycle 整合 1d、tests + 1-2 個 example yaml 1d)
- **優先級**:**P1** — 不是 starter 必備,但對企業用戶 fork 後立刻有用

---

### 4.6 [P1] Session compaction — decorator pattern

#### 來源

- `src/agents/memory/session.py:107-150`(`OpenAIResponsesCompactionAwareSession` Protocol + `OpenAIResponsesCompactionArgs`)
- `src/agents/memory/openai_responses_compaction_session.py:78-374`(`OpenAIResponsesCompactionSession` 包裝任一 Session)
- `src/agents/memory/openai_responses_compaction_session.py:30-56`(`select_compaction_candidate_items` + `default_should_trigger_compaction` — 預設 threshold 10)

#### Public API 樣貌

```python
from agents.memory import SQLiteSession, OpenAIResponsesCompactionSession

underlying = SQLiteSession(session_id="alice", db_path=".anila/sessions/alice.db")
session = OpenAIResponsesCompactionSession(
    session_id="alice",
    underlying_session=underlying,
    model="gpt-4.1",
    compaction_mode="auto",
    should_trigger_compaction=lambda ctx: len(ctx["compaction_candidate_items"]) >= 15,
)

result = await Runner.run(agent, "...", session=session)
# 每 turn 結束 session.add_items 後,decorator 檢查 threshold,超過自動 call OpenAI responses.compact
```

#### 為什麼對 anila-agent 有價值

1. **decorator pattern**(同 Session interface,包別人)是個極漂亮的擴充模式 — 不用改 Runner.run 一行就能加 compaction
2. **threshold-driven** 比 anila-agent 既有的 `auto_memory_min_messages` 細緻 — 不是 message 數而是 candidate item 數(skip user message + 已 compact 過的)
3. **anila-agent 應該寫自家 `LLMSummarizeCompactingSession`** — 不綁 OpenAI Responses API,改成「呼叫自家 model + ModelSettings 做 summarize」

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/memory/compaction.py`

```python
from agents.memory import SessionABC

class LLMSummarizeCompactingSession(SessionABC):
    """Decorator session — 當 underlying session item 數超過 threshold,
    呼叫自家 LLM 把舊 items summarize 成單一 system message 再寫回。"""

    def __init__(self, *, session_id, underlying_session, model, model_settings,
                 threshold: int = 20, summarize_keep_recent: int = 5):
        self.session_id = session_id
        self.underlying = underlying_session
        self.model = model
        self.model_settings = model_settings
        self.threshold = threshold
        self.summarize_keep_recent = summarize_keep_recent

    async def get_items(self, limit=None):
        return await self.underlying.get_items(limit)

    async def add_items(self, items):
        await self.underlying.add_items(items)
        await self._maybe_compact()

    async def _maybe_compact(self):
        items = await self.underlying.get_items()
        if len(items) < self.threshold:
            return
        old, recent = items[:-self.summarize_keep_recent], items[-self.summarize_keep_recent:]
        summary = await self._llm_summarize(old)
        await self.underlying.clear_session()
        await self.underlying.add_items([{"role": "system", "content": f"# 對話歷史摘要\n{summary}"}] + recent)
    ...
```

**配在 `core/agent.py:93-95`**:

```python
short_term: Session | None = None
if config.memory.short_term_enabled:
    base = open_session(session_id, config.memory.short_term_path)
    if config.memory.compaction_enabled:
        short_term = LLMSummarizeCompactingSession(
            session_id=session_id, underlying_session=base,
            model=model, model_settings=ModelSettings(temperature=0.0, max_tokens=2048),
            threshold=config.memory.compaction_threshold,
        )
    else:
        short_term = base
```

#### 工作量 / 優先級

- **工作量**:1-2d(decorator class 0.5d、summarize prompt + tests 0.5d、config 0.5d)
- **優先級**:**P1** — 中 context 應對的關鍵能力;sub-agent template 帶這個出廠,長對話 OOM 風險降很多

---

### 4.7 [P1] RunState — HITL pause / resume + tool approval

#### 來源

- `src/agents/run_state.py:131-148`(`CURRENT_SCHEMA_VERSION = "1.10"` + `SCHEMA_VERSION_SUMMARIES`)
- `src/agents/run_state.py:184-1101`(`RunState` dataclass + `to_json` / `from_json` / `approve` / `reject`)
- `src/agents/result.py:367-368`(`RunResult.interruptions: list[ToolApprovalItem]`)
- `src/agents/result.py:393-438`(`RunResult.to_state()`)
- 範例:`examples/agent_patterns/human_in_the_loop.py:67-135`

#### Public API 樣貌

```python
from agents import RunState

# 第一輪 run
result = await Runner.run(agent, "刪除使用者 alice 的全部資料")

while result.interruptions:
    state = result.to_state()
    state_json = state.to_json()
    Path(".cache/pending.json").write_text(json.dumps(state_json))

    # 過幾分鐘 / 換 thread / 換 process...
    saved = json.loads(Path(".cache/pending.json").read_text())
    state = await RunState.from_json(agent, saved)

    for interrupt in result.interruptions:
        if confirm(f"批准 {interrupt.name}({interrupt.arguments})?"):
            state.approve(interrupt)
        else:
            state.reject(interrupt)

    result = await Runner.run(agent, state)  # resume

print(result.final_output)
```

#### 為什麼對 anila-agent 有價值

1. **anila-agent 6 個 hook event 已有 `PermissionRequest`,但沒有對應的 pause/resume 機制** — 目前 hook 拋 abort 就死了,resume 走不下去
2. **`needs_approval` per-tool 是 declarative**(`tool.py` `@function_tool(needs_approval=...)`) — 比 anila-agent 自寫 hook regex 簡潔
3. **serializable state** 讓 sub-agent 可以「等使用者 Slack 按按鈕後再繼續」這類 long-running workflow
4. **schema versioned**(目前 1.10)— 對 production 升級友善

#### 整合進 anila-agent 的概念草案

**改 `core/runner.py`**:`AnilaRunner.send` 回傳的 `RunSummary` 加 `interruptions: list[ToolApprovalItem] | None` 跟 `state_json: dict | None` 兩個欄位

**新 method**:`AnilaRunner.resume(state_json, approvals: dict[str, bool])`,內部:
1. `state = await RunState.from_json(self.assembled.agent, state_json)`
2. 對每個 `interruption` 套 `approve / reject`
3. `await Runner.run(self.assembled.agent, state)`

**Config 增 per-tool approval matrix**:

```yaml
tools:
  builtin:
    - anila_agent.tools.filesystem_tools.write_file
  approval:
    write_file: always           # always | never | callable_path
    delete_file: always
    vector_search: never
```

**CLI 補 `/approve` / `/reject` slash command**:讀 `.anila/pending/<run_id>.json`,問使用者批准哪些,resume run

#### 工作量 / 優先級

- **工作量**:1w+(state JSON wrapper 0.5d、tool needs_approval 整合 1d、CLI flow 1-2d、persistence + storage 1d、example + docs 1d)
- **優先級**:**P1** — sub-agent template 缺這個會嚴重 cap production 場景;但工作量大,獨立排期

---

### 4.8 [P1] Streaming — `run_streamed` + StreamEvent

#### 來源

- `src/agents/stream_events.py:1-61`(`RawResponsesStreamEvent` / `RunItemStreamEvent` / `AgentUpdatedStreamEvent` 三層)
- `src/agents/run.py`(`Runner.run_streamed` 回 `RunResultStreaming`,有 `async for event in result.stream_events()`)
- `src/agents/result.py:445-...`(`RunResultStreaming`)
- 範例:`src/agents/repl.py:36-76`(76 行極簡 demo loop,可直接學)

#### Public API 樣貌

```python
result = Runner.run_streamed(agent, input=user_input)
async for event in result.stream_events():
    if isinstance(event, RawResponsesStreamEvent):
        if isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)
    elif isinstance(event, RunItemStreamEvent):
        if event.item.type == "tool_call_item":
            print("\n[tool called]", flush=True)
        elif event.item.type == "tool_call_output_item":
            print(f"\n[tool output: {event.item.output}]", flush=True)
    elif isinstance(event, AgentUpdatedStreamEvent):
        print(f"\n[Agent updated: {event.new_agent.name}]", flush=True)
```

#### 為什麼對 anila-agent 有價值

1. **anila-agent CLI 現在的 `AnilaRunner.send` 必須 await 整個 final_output**(`runner.py:86-92`)— 對話體驗很差,使用者等很久才看到結果
2. **三層粒度** 對 CLI / TUI / future Web UI 都有用:
   - `RawResponsesStreamEvent` — 顯示「正在打字」的 delta
   - `RunItemStreamEvent` — 顯示「正在用 vector_search 工具」/「retrieval 完成,3 個 chunks」
   - `AgentUpdatedStreamEvent` — 顯示「現在切換到 verifier 子 agent」(handoff 觸發)
3. **既有 EventBus 是事件流但沒有 LLM delta**,不能完全替代

#### 整合進 anila-agent 的概念草案

**改 `core/runner.py`**:加 `AnilaRunner.send_streamed(prompt) -> AsyncIterator[StreamEvent]`

```python
async def send_streamed(self, prompt: str):
    await fire_user_prompt_submit(...)
    hooks = AnilaRunHooks(...)
    result = Runner.run_streamed(
        starting_agent=self.assembled.agent,
        input=prompt,
        hooks=hooks,
        session=self.assembled.short_term,
        max_turns=self.assembled.max_turns,
    )
    async for event in result.stream_events():
        yield event
    # final summary handled by post-iter
```

**改 `cli/app.py:30-...`**:REPL loop 用 `send_streamed`,renderer 對三層 event 各有顯示樣式

#### 工作量 / 優先級

- **工作量**:2-3d(send_streamed wrapper 0.5d、CLI renderer 對三層分流 1d、tests + 對齊 hook fire 1d)
- **優先級**:**P1** — UX 顯著提升,但不影響功能正確性

---

### 4.9 [P2] Agent.as_tool — agent 當 sub-routine 用

#### 來源

- `src/agents/agent.py:508-708`(`Agent.as_tool()` method,含 `custom_output_extractor` / `is_enabled` / `on_stream` / `needs_approval` / `parameters` 等)

#### Public API 樣貌

```python
retriever = Agent(
    name="retriever",
    instructions="只跑 vector_search 並回 top-5 chunks",
    tools=[vector_search],
    tool_use_behavior="stop_on_first_tool",
)

main_agent = Agent(
    name="main",
    instructions="收到 query 後用 search_via_retriever 取 chunks,再生答案",
    tools=[retriever.as_tool(
        tool_name="search_via_retriever",
        tool_description="呼叫 retriever sub-agent 去檢索 top-5 chunks",
        custom_output_extractor=lambda result: json.dumps(result.last_agent.context["chunks"]),
    )],
)
```

#### 為什麼對 anila-agent 有價值

`Agent.as_tool` 跟 `handoff` 的根本差異:

| | handoff | as_tool |
|---|---|---|
| 控制權 | 轉移 | 不轉移(call/return) |
| 下個 agent 看到的 input | 整段 conversation history | 只看到 tool args |
| 適合場景 | 「轉接給專門人員」 | 「呼叫專家做一件事再回來」 |

對 RAG 場景,「retriever 跑完回 chunks 給 main answer agent」更接近 sub-routine,**不該轉移控制權**。`as_tool` 是該場景的正解。

#### 整合進 anila-agent 的概念草案

不需新 module — 文件補一段「handoff vs as_tool 怎麼選」決策表;config schema 允許 yaml 寫:

```yaml
sub_agents:
  retriever:
    instructions: ...
    tools: [...]
    tool_use_behavior: stop_on_first_tool

agent:
  tools_from_sub_agents:
    - sub_agent: retriever
      tool_name: search_via_retriever
      tool_description: 用 retriever 跑檢索,回 top-5 chunks
```

`build_agent` factory 解析後呼叫 `retriever_agent.as_tool(...)` 加進 main agent.tools

#### 工作量 / 優先級

- **工作量**:0.5d(config schema + factory)
- **優先級**:**P2** — 跟 handoff 是互補關係,handoff 上線後再做

---

### 4.10 [P2] Retry — model call robustness

#### 來源

- `src/agents/retry.py:1-361`(`ModelRetrySettings` + `RetryDecision` + `RetryPolicy` + `retry_policies` factory)
- `src/agents/retry.py:231-358`(`_RetryPolicies` — `never` / `provider_suggested` / `network_error` / `retry_after` / `http_status` / `all` / `any` composable)
- `src/agents/run_internal/model_retry.py`(實際 retry loop)

#### Public API 樣貌

```python
from agents import ModelRetrySettings, retry_policies, RunConfig

policy = retry_policies.any(
    retry_policies.network_error(),
    retry_policies.retry_after(),
    retry_policies.http_status({429, 500, 502, 503, 504}),
)

retry_settings = ModelRetrySettings(
    max_retries=3,
    backoff={"initial_delay": 0.25, "max_delay": 2.0, "multiplier": 2.0, "jitter": True},
    policy=policy,
)

result = await Runner.run(agent, "...", run_config=RunConfig(...))
```

#### 為什麼對 anila-agent 有價值

1. **anila-agent 現在的 retry 完全靠 LiteLLM 內建**:沒有 anila-specific advice,沒有區分 anila 用的 vLLM endpoint 跟 OpenAI 的不同錯誤模式
2. **`retry_policies.any(...)` 組合** 比寫 retry decorator 漂亮
3. **`replay_safety="safe"` 標記** 對 idempotent vs non-idempotent 呼叫有差別處理

#### 整合進 anila-agent 的概念草案

**新 module**:`anila_agent/providers/retry.py`

```python
from agents import retry_policies, ModelRetrySettings

DEFAULT_RETRY = ModelRetrySettings(
    max_retries=3,
    backoff={"initial_delay": 0.25, "max_delay": 2.0, "multiplier": 2.0, "jitter": True},
    policy=retry_policies.any(
        retry_policies.network_error(),
        retry_policies.retry_after(),
        retry_policies.http_status({429, 500, 502, 503, 504}),
    ),
)
```

**改 `core/runner.py`**:`Runner.run` 帶 `run_config=RunConfig(...)` 把 retry_settings 丟下去(不過 SDK 是綁在 ModelSettings/ Model 上,要看 LitellmModel 怎麼接 — 可能要看 run_internal/model_retry 細節)

#### 工作量 / 優先級

- **工作量**:1d(寫 default policy + RunConfig 整合 + tests)
- **優先級**:**P2** — LiteLLM 本身已能 retry,差別是 anila 想要 fine-grained policy

---

### 4.11 [P2] Tool-level guardrails 直接掛在 FunctionTool

#### 來源

- `src/agents/tool.py:322,325`(`FunctionTool.tool_input_guardrails` / `tool_output_guardrails` field)
- `src/agents/tool.py:505-535`(`function_tool(... tool_input_guardrails=..., tool_output_guardrails=...)`)

#### Public API 樣貌

```python
@function_tool(
    tool_input_guardrails=[block_pii_in_args],
    tool_output_guardrails=[redact_secrets_in_output],
)
def vector_search(query: str) -> list[dict]:
    ...
```

#### 為什麼對 anila-agent 有價值

- 比 anila-agent 既有 `hook regex matcher`(全局 + tool_name regex)精準 — 一個 tool 的 guardrail 就在那個 tool 旁邊宣告,**不會 register 錯地方**
- 跟 4.2 是同套機制,但是更靠近 tool 的綁定方式

#### 整合進 anila-agent 的概念草案

`anila_agent/tools/base.py:46-76` 的 `@anila_tool` decorator 加 `tool_input_guardrails / tool_output_guardrails` 兩個 kwarg pass-through 到 `function_tool(...)`

#### 工作量 / 優先級

- **工作量**:1d(改 decorator + tests + 1-2 個內建 guardrail example)
- **優先級**:**P2** — 跟 4.2 一起完成

---

### 4.12 [P2] `prompt_with_handoff_instructions` — 自動把 handoff 規則塞 instructions

#### 來源

- `src/agents/extensions/handoff_prompt.py:3-19`

```python
RECOMMENDED_PROMPT_PREFIX = (
    "# System context\n"
    "You are part of a multi-agent system called the Agents SDK, designed to make agent\n"
    "coordination and execution easy. Agents uses two primary abstraction: **Agents** and\n"
    "**Handoffs**. ...handoff between agents..."
)

def prompt_with_handoff_instructions(prompt: str) -> str:
    return f"{RECOMMENDED_PROMPT_PREFIX}\n\n{prompt}"
```

#### 為什麼對 anila-agent 有價值

`Agent.instructions` 寫的 prompt 沒提到「你可以用 handoff 工具切換到另一個 agent」,LLM 不知道該怎麼用。SDK 提供現成 prefix。

#### 整合進 anila-agent 的概念草案

`build_agent`:當 `config.agent.handoffs` 非空時,自動把 `RECOMMENDED_PROMPT_PREFIX` 接在 `instructions` 前面(可關)。

#### 工作量 / 優先級

- **工作量**:0.5d(整合 + config flag + 對應 anila 自家中文版 prefix 一個)
- **優先級**:**P2**

---

### 4.13 [P3] Graphviz 視覺化 agent graph

#### 來源

- `src/agents/extensions/visualization.py:147-...`(`draw_graph(agent, filename)`)
- 需 `graphviz>=0.17` 額外 dep

#### Public API 樣貌

```python
from agents.extensions.visualization import draw_graph
draw_graph(main_agent, filename=".anila/graph.png")
# 自動生 main → handoff → billing / support / verifier / ... 的 DAG 圖
```

#### 為什麼對 anila-agent 有價值

`/graph` slash command 印出當前 agent 的 sub-agent / tool 圖,debug 友善。多個 sub-agent 後人腦不好追拓樸。

#### 工作量 / 優先級

- **工作量**:0.5d(`/graph` slash command + 文件)
- **優先級**:**P3** — nice-to-have,不影響功能

---

### 4.14 [P3] `repl.run_demo_loop` — 對齊 demo loop 寫法

#### 來源

- `src/agents/repl.py:1-76`(76 行極簡,自包含)

#### 為什麼對 anila-agent 有價值

`anila-agent` 的 `cli/app.py` 自寫 REPL(177 行),功能更完整(有 slash command / metrics / FileHistory / rich renderer),不直接 replace。但 `repl.run_demo_loop` 的 stream event 處理寫法可以對齊,**保證行為一致**(SDK 升級時跟著對)。

#### 工作量 / 優先級

- **工作量**:0.5d(對齊 streaming branch)
- **優先級**:**P3**

---

## 5 · 不該抄的部分

| SDK module | 為什麼不抄 |
|---|---|
| `src/agents/realtime/*` | 走 OpenAI Realtime API 跟 WebSocket;ANILA 後端是 HTTP + LiteLLM,模型介面不匹配。如果 ANILA 未來做 voice/realtime,再從這抄 |
| `src/agents/voice/*` | 同上 |
| `src/agents/sandbox/*`(整套 Manifest + Capabilities + Docker/UnixLocal/Memory backend) | anila-agent 目前的 tool 都是 trusted Python function;無「跑使用者提交的 SQL / Python script」場景。`sandbox/sandboxes/`, `sandbox/session/` 加起來 30+ 個檔,複雜度太高。**結論**:預留 `tools/base.py` 加個 `sandbox: SandboxManifest | None = None` 欄位佔位,實作延後 |
| `src/agents/memory/openai_responses_compaction_session.py` 直接搬 | 綁 OpenAI Responses API + `responses.compact` 雲端 endpoint。**借鑑 pattern**(decorator + threshold),但 anila 自寫 LLM-summarize 版,**不引用此檔** |
| `src/agents/memory/openai_conversations_session.py` | 同上,綁 OpenAI Conversations API |
| `src/agents/models/openai_*` 全部 | OpenAI Responses 專屬。anila 走 LitellmModel 已涵蓋(`openai_compatible.py`) |
| `src/agents/prompts.py` 的 `Prompt` + OpenAI Prompts API | OpenAI 雲端產品。anila 自家 yaml prompt 已涵蓋 |
| `src/agents/run_state.py` 3305 行直接搬 | RunState 自身有 schema 1.10 / 14+ 種 RunItem 子型序列化邏輯。**只用 SDK API**(`to_json / from_json / approve / reject`),不重寫 — 但 anila 自家的 storage layer 可以另寫成簡單 JSON file (`.anila/pending/<run_id>.json`) |
| `src/agents/extensions/memory/dapr_session.py` / `mongodb_session.py` / `redis_session.py` 等 production-grade backend | sub-agent template **單機 starter** 立場,sqlite + 自寫 compacting decorator 已夠。production 用戶 fork 後自己接 |
| `src/agents/run_internal/*` 22 個檔 | hidden API,SDK 自己會升級;anila-agent 只用 `Runner.run` 公共 API,不該插這層。**例外**:如果 anila 自家 runner 長到 > 500 行,可以參考它的拆分風格 |
| `src/agents/models/multi_provider.py` | anila 一台 base_url 走天下(LitellmModel + vLLM/Ollama/OpenAI 全包),不需要 MultiProvider 動態 routing |

---

## 6 · 跟 antigravity-sdk-python / claude-code-src 的潛在重疊

> 本節輕量,只列 cross-SDK 重疊點,供未來整合報告參考。

| 能力 | openai-agents-python | antigravity-sdk-python(推測) | claude-code-src(推測) | anila-agent 該選誰 |
|---|---|---|---|---|
| **Hook 系統** | `RunHooks` + `AgentHooks` (12 callback 點) | unknown,需另查 | `types/hooks.ts` 5-event(PreToolUse, PostToolUse, Stop, SessionStart, UserPromptSubmit;anila-agent 已 port + 加 PermissionRequest) | **混合**:hook event 沿用 claude-code 6-event(declarative),SDK lifecycle bridge 用 openai-agents `RunHooks` |
| **Memory(memdir)** | `Session` Protocol — 結構化 conversation items | unknown | `memdir/` 寫實檔案 + frontmatter(anila-agent 已 port `MemdirStore`) | **memdir 走自家 port**(已完成);**session(短期 conversation) 走 SDK `SQLiteSession`**(已完成)。長期記憶選擇 memdir 對 LLM-recall 友善 |
| **Multi-agent / sub-agent** | `Handoff` + `Agent.as_tool` 完整 | unknown | claude-code 走 `Task` tool + sub-agent(本質類似 as_tool) | **`handoff` + `as_tool` 兩條** 都從 openai-agents 抄 |
| **Tool framework** | `FunctionTool` + 8 種子型 + sandbox | unknown | claude-code tools 自有 `is_read_only / is_destructive` metadata | anila 已混合(自家 `ToolMetadata` + SDK FunctionTool);保持現狀 |
| **Tracing** | `tracing/` typed span + processor | unknown | claude-code 自有 telemetry | 從 **openai-agents** 抄(spec 標準 + OTel-friendly) |
| **MCP** | `mcp/` 完整 stdio/sse/streamable | unknown | claude-code 也接 MCP,實作不同 | 從 **openai-agents** 抄(類別清楚) |
| **Sandbox** | manifest + capabilities + Docker/UnixLocal | unknown(若有再評) | claude-code 有 sandbox 概念但實作不同 | **延後**,不選任何一邊 |
| **Realtime/Voice** | `realtime/` + `voice/` | unknown | claude-code 無 voice | **延後**,等需求 |
| **Slash command** | 無 | unknown | claude-code 自家(anila-agent 已 port 到 cli) | anila CLI 走 claude-code 風格(已完成) |

**結論**:anila-agent 目前選擇是 **memdir 走 claude-code**、**runtime + hook lifecycle + multi-agent + tracing + MCP 走 openai-agents**。本文所有 P0/P1 建議都不破壞這個 split。

---

## 7 · 整合 roadmap(把 P0/P1/P2 排成 sprint)

```
sprint 1 (P0 — 2 週)
├── [1d] lifecycle hooks 補滿(on_agent_start, on_handoff, AgentHooks)
├── [2-3d] guardrails framework(input/output + tool 三 behavior)
├── [2-3d] tracing framework(re-export + JSONL processor + AnilaRunner wrap)
└── [2-3d] handoffs framework(handoff + handoff_filters + prompt_prefix)

sprint 2 (P1 — 2 週)
├── [2-3d] MCP 整合(loader + AnilaRunner lifecycle + 2 個 yaml example)
├── [1-2d] CompactingSession decorator
├── [2-3d] Streaming(run_streamed + 三層 event + CLI renderer)
└── [1w+] RunState HITL pause/resume(獨立 spike,可拆兩個 sprint)

sprint 3 (P2/P3 — 1 週)
├── [1d] retry framework
├── [1d] FunctionTool 直接掛 tool_guardrails(跟 sprint 1 #2 一起做)
├── [0.5d] Agent.as_tool sub-routine pattern
├── [0.5d] prompt_with_handoff_instructions
├── [0.5d] draw_graph slash command
└── [0.5d] repl.run_demo_loop streaming 對齊

總計:5-6 週可上完 P0+P1,sub-agent template 進階到「完整 multi-agent + guardrail + trace + MCP + HITL」
```

### 7.1 依賴關係圖

```
P0 lifecycle hooks ───┐
                      ├─▶ P0 handoffs (on_handoff 需 lifecycle)
P0 guardrails ────────┤
                      ├─▶ P2 FunctionTool tool_guardrails (跟 guardrails 一起)
P0 tracing ───────────┤
                      ├─▶ 所有後續 feature(便於 debug)
                      │
P1 MCP ───────────────┘
P1 CompactingSession (獨立)
P1 Streaming (獨立,只跟 hook 對齊)
P1 RunState/HITL ──── 依賴 P0 lifecycle + tracing

P2 retry (獨立)
P2 prompt_with_handoff (P0 handoffs 後)
P2 as_tool (P0 handoffs 後做 contrast 文件)
P3 draw_graph (P0 handoffs 後才有意義)
P3 repl.run_demo_loop (P1 streaming 後對齊)
```

### 7.2 獨立可 ship 的最小單位

| 單位 | 工作量 | 影響 |
|---|---|---|
| P0 lifecycle 補滿 + tracing 基礎 | 3-4d | 第一個閉環:debug 友善 + audit log 完整 |
| P0 guardrails(input/output + tool) | 3-4d | 第二個閉環:input safety + tool-level reject_content |
| P0 handoffs | 2-3d | 第三個閉環:multi-agent 上線 |

**3 個 P0 任一單獨 ship 都對 sub-agent template 有顯著加分**。

---

## 8 · 文件 / 工程化注意事項

### 8.1 對齊 SDK 升級節奏

`pyproject.toml:3` 指 `openai-agents 0.17.3`。SDK 在頻繁迭代(`run_state.py:131` 已 schema 1.10,半個月內從 1.9 升到 1.10);本文所有「直接 re-export」的 module(handoffs / guardrails / tracing / mcp)會**自動隨 SDK 升級拿到新 feature**,但要注意:

- **每次 SDK bump 需 read `SCHEMA_VERSION_SUMMARIES`**(`run_state.py:133-148`),看是否 RunState 序列化格式變 — anila 自家 storage layer 不影響,但要確認 `to_json / from_json` 跨版本相容
- **AGENTS.md / CLAUDE.md(本 source 下的)** 寫了「Public API Positional Compatibility」規則 — re-export 我們不會誤踩,但 anila 自家 wrapper 要 follow 同樣慣例(field 加最後不插中間)

### 8.2 自家 wrapper vs 直接 re-export 的取捨

| 模式 | 適用場景 | 範例 |
|---|---|---|
| **直接 re-export**(本文 P0 大多採用) | SDK API 已經夠 clean,加 anila wrapper 反而增加維護成本 | `core/handoffs.py` re-export `Handoff` / `handoff` / `HandoffInputData` |
| **wrapper class**(adapter pattern) | 需加 anila-specific 行為(e.g. fire event / log / metric) | `core/runner.py` 的 `AnilaRunner` |
| **bridge class**(translate domain) | 兩邊 domain 不一樣 | `core/hooks.py` 的 `AnilaRunHooks` 把 SDK lifecycle → anila 6-event |

**判斷規則**:如果 wrapper 沒加新行為,**直接 re-export**;否則 wrapper 但**只在邊界寫薄**(< 100 行)。

### 8.3 測試覆蓋

對應每個 P0 整合,測試 plan:

| 整合點 | 測試型態 | 要 cover 的 case |
|---|---|---|
| Lifecycle hooks 補滿 | unit | `on_agent_start` / `on_handoff` 被 fire 的順序、payload 正確 |
| Guardrails | unit + e2e | trip 的 case raise `*TripwireTriggered`、`reject_content` 替換 tool output、`run_in_parallel` 並行 |
| Tracing | e2e | 一次 run 生 trace tree 結構正確、processor 收到完整 span、`group_id=session_id` 對齊 |
| Handoffs | unit + e2e | handoff trigger 後 `last_agent` 變、`input_filter` 套用、`is_enabled=False` 該 handoff 隱藏 |
| MCP | e2e(用本地 stdio echo server)| connect 成功、`get_all_tools` 動態 fetch、`drop_failed_servers` 行為 |
| CompactingSession | unit | threshold 觸發、summarize 後 item 數變正確 |
| Streaming | unit | `RawResponsesStreamEvent` / `RunItemStreamEvent` / `AgentUpdatedStreamEvent` 三層都收得到 |

---

## 9 · 結語

`openai-agents-python` 在 0.17.3 已演化為一個**功能完整、模組邊界清楚、社群活躍**的 agent runtime。`anila-agent` 作為 sub-agent template 目前**只用了 SDK 的 ~30% 表面積**(`Agent / Runner / RunHooks / FunctionTool / SQLiteSession / LitellmModel`),其餘 70%(`handoffs / guardrails / tracing / mcp / RunState / streaming / retry / extensions`)幾乎全未觸碰。

最高 ROI 的三個 P0 任務:

1. **Tracing**:single-step debug 體驗從「rich event log + 自己拼時序」變成「打開 trace tree 看 parent-child timing」,所有後續改進都受益
2. **Guardrails(2 層)+ lifecycle hooks 補滿**:input safety + per-tool reject_content + audit completeness 三件事一個 sprint 拿下
3. **Handoffs**:sub-agent template 真正有「sub-agent」能力

完成 P0 後 sub-agent template 整體完整度顯著提升,可以作為 ANILA 平台對外推廣的 reference implementation。

---

**Last updated**: 2026-05-26 · **By**: ANILA 平台分析 (openai-agents-python deep dive subagent)

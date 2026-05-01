# openai-agents-python — deep dive for AgenticRAG enhancement

> **Source**: `runtime_logic/openai-agents-python/` (gitignored；本機快照)
> **Version studied**: src/agents/ 截至 2026-05-02 (RunState schema 1.9)
> **Status**: 為 AgenticRAG / anila-core 強化計畫提供「該讀什麼、該翻什麼介面、該怎麼接」的對照地圖
> **Reading rule**: 只讀 pattern + interface，自己重寫實作；逐字搬運禁止

整份 SDK 把「agent run = LLM + tools + handoffs + guardrails + memory + tracing」拆成 12 個明確邊界的模組。本文按那 12 條邊界一一拆解，每段最後附 AgenticRAG / anila-core 的 actionable 強化點。

---

## 1 · 全景

### 1.1 兩層 entry point

```
Runner (top-level façade)        public API；Runner.run / Runner.run_streamed / Runner.run_sync
   └─> AgentRunner (work doer)   做實際的 turn loop；有 hidden API extension hooks
         └─> run_internal/*       turn-by-turn 邏輯，依職責拆 22 個檔
```

`Runner` 跟 `AgentRunner` 的存在原因：library 用戶用 `Runner.run(...)` 簡單；想 inject hooks / 自訂 turn 邏輯的 advanced 用戶可以 subclass `AgentRunner` 改 internals。對應到 ANILA：`anila-core/api/router_server.py` 是 façade、`engine/query_engine.py` 是 doer，已是同一 pattern。

### 1.2 19-component map（src/agents/ top level）

```
agent.py                Agent dataclass + AgentBase（含 AgentToolStreamEvent / StopAtTools / MCPConfig）
agent_output.py         AgentOutputSchemaBase；structured output 的 schema 解析
agent_tool_input.py     Agent-as-tool 時的 parameter schema 構造
agent_tool_state.py     Nested run 結果的 contextvars-based 帶回
agent_runner_helpers.py Run 期間共用 helpers
exceptions.py           UserError / ModelBehaviorError / *TripwireTriggered 群
function_schema.py      Function 簽名 → JSON schema 自動轉換
guardrail.py            Input/Output guardrail framework
handoffs/               Multi-agent handoff
items.py                RunItem 類別系統 + ItemHelpers
lifecycle.py            RunHooks / AgentHooks (12 個 lifecycle 點)
memory/                 Session protocol + 4 個 backend
mcp/                    Model Context Protocol server 整合
models/                 Provider abstraction（OpenAI Responses + ChatCompletions + 多 provider）
prompts.py              Dynamic prompt（含 OpenAI Prompts API 整合）
realtime/               Voice / WebSocket 路徑（本案外）
result.py               RunResult / RunResultStreaming
retry.py                ModelRetrySettings + decision policy
run.py                  Runner / AgentRunner（1863 行；公共流控）
run_config.py           RunConfig（per-run 配置）
run_context.py          RunContextWrapper / AgentHookContext / TContext typing
run_error_handlers.py   Custom error / retry handlers
run_internal/           內部 turn loop 細節（22 個檔）
run_state.py            RunState durable snapshot（schema 1.9, 3304 行）
sandbox/                Tool sandboxed execution（apply_patch + capability model）
stream_events.py        StreamEvent union types
tool.py                 Tool / FunctionTool 框架
tool_context.py         ToolContext typing
tool_guardrails.py      Per-tool input/output guardrail（vs whole-agent guardrail.py）
tracing/                Span tree + processor pipeline + OpenAI exporter
extensions/             handoff_filters + experimental
```

### 1.3 Agent 是 dataclass，不是 stateful 物件

```python
@dataclass
class Agent(AgentBase, Generic[TContext]):
    name: str
    instructions: str | Callable[..., str] | None
    handoffs: list[Agent[Any] | Handoff[TContext, Any]]
    tools: list[Tool]
    mcp_servers: list[MCPServer]
    input_guardrails: list[InputGuardrail[TContext]]
    output_guardrails: list[OutputGuardrail[TContext]]
    output_type: type[Any] | AgentOutputSchemaBase | None
    hooks: AgentHooks[TContext] | None
    tool_use_behavior: Literal["run_llm_again", "stop_on_first_tool"] | StopAtTools | ToolsToFinalOutputFunction
    model: str | Model | None
    model_settings: ModelSettings
    reset_tool_choice: bool = True
```

關鍵 insight：**Agent 不持有 conversation state**。state 全部在 `RunState` / `Session` / `RunContextWrapper`。所以 Agent instance 可以被多 run / 多執行緒共享，run_state 才是 mutable single-tenant。

對應 AgenticRAG：目前 chunker config 跟 prompt 散在 `agentic_rag/registry/agent_registry.py`，沒明確切分 declarative agent definition vs run-time state。長 multi-agent 時需要對齊到這個 model。

---

## 2 · Run loop — 核心 turn-by-turn 邏輯

### 2.1 入口（run.py）

`AgentRunner` 暴露三條 public API，全部走 `run_internal/run_loop.py`：

| 公共 API | 內部實作 |
|---|---|
| `run(starting_agent, input, ...)` | `run_internal.run_loop.run_single_turn(...)` 在迴圈裡轉到收斂 |
| `run_streamed(starting_agent, input, ...)` | `run_internal.run_loop.run_single_turn_streamed(...)`，回傳 `RunResultStreaming`（async iterator over `StreamEvent`） |
| `run_sync(...)` | thin wrapper 把 async 包成 sync |

非串流跟串流路徑**必須行為一致**（AGENTS.md 寫死的 invariant）。差別只在事件何時 emit、不在 turn-decision 邏輯。

### 2.2 一輪 turn 內部（`run_single_turn` at run_loop.py:1698）

```
1. 第一輪? → 跑 input_guardrails（並行模式 default）
2. on_agent_start hook（global RunHooks + agent.hooks）
3. 並行取 system_prompt + prompt config (gather)
4. resolve output_schema, handoffs, tools
5. (optional) OpenAIServerConversationTracker.prepare_input — 處理 server-managed conversation
6. get_new_response — 呼叫 model；含 retry policy
7. get_single_step_result_from_response — 轉化成 RunItem + 決定 NextStep
8. Persist generated items to session（需要的話）
9. 如果 NextStep.tool_calls → tool_execution.execute_function_tool_calls
10. 如果 NextStep.handoff → 切換 current agent + 處理 input_filter
11. 如果 NextStep.final_output → 跑 output_guardrails → done
12. NextStep.run_again → 回到 1（增 turn counter）
```

**關鍵 invariants**：
- input guardrails 只跑第一個 turn 的 starting agent
- resume 從 `RunState` 不增加 turn counter；只有實際 model call 加
- 每個 step 都可以 interrupt（HITL approval / pause / resume），所以 `NextStep` 是 union type

### 2.3 NextStep 决策（turn_resolution.py）

模型輸出 → `ProcessedResponse` → `NextStep`：

```python
NextStep = NextStepRunAgain | NextStepFinalOutput | NextStepHandoff | NextStepInterruption
```

`NextStepInterruption` 是 RunState pause 的根本機制：當 tool 需要 approval（user-in-the-loop）就把整個 run 包進 RunState 序列化，等 user 解鎖再 resume。

對應 AgenticRAG：目前 query_engine 的 turn loop 沒有 interrupt-resume 機制。如果未來 AgenticRAG agent 要做「query 太敏感先 ack admin 才 search」這類流程，要從這個 pattern 抄。

### 2.4 Hidden API：22 個 run_internal 檔

| 檔 | 職責 |
|---|---|
| `run_loop.py` | 主迴圈 + streaming 平行版本 |
| `turn_resolution.py` | model output → NextStep |
| `tool_planning.py` | 哪些 tool 該 enabled（reset_tool_choice 等） |
| `tool_execution.py` | function tool / shell / apply_patch 執行 + 平行排程 |
| `tool_actions.py` | tool 結果 → RunItem 轉換 |
| `turn_preparation.py` | turn-start 時的 input filter / item 過濾 |
| `run_steps.py` | ProcessedResponse + tool run structs |
| `items.py` | RunItem normalize / dedupe / approval filter |
| `streaming.py` | RawResponsesStreamEvent 處理 |
| `session_persistence.py` | Session save / rewind 邏輯 |
| `oai_conversation.py` | `OpenAIServerConversationTracker`（server-managed conversation）|
| `guardrails.py` | input/output/tool guardrail 排程 |
| `model_retry.py` | retry decision pipeline |
| `error_handlers.py` | custom error handler |
| `approvals.py` | tool approval state |
| `agent_bindings.py` | public_agent vs execution_agent（agent-as-tool 分身） |
| `agent_runner_helpers.py` | 各種小幫手 |
| `prompt_cache_key.py` | prompt cache key 生成 + 持久化 |
| `tool_use_tracker.py` | 追蹤 tool 使用避免無限呼叫 |
| `run_grouping.py` | trace group |
| `_asyncio_progress.py` | async progress utils |

對 AgenticRAG / anila-core 的 takeaway：**single big run.py 是 anti-pattern**。當功能變多，就拆 internal/* 並在主檔只留 public flow control + composition。AGENTS.md 明確規定「`run.py` 不應再長」。

---

## 3 · Handoffs — multi-agent delegation

### 3.1 `Handoff` dataclass

```python
@dataclass
class Handoff(Generic[TContext, TAgent]):
    tool_name: str                  # LLM 看到的 tool name
    tool_description: str
    input_json_schema: dict         # handoff 帶給下一 agent 的 structured payload schema
    on_invoke_handoff: Callable[[RunContextWrapper, str], Awaitable[TAgent]]
    agent_name: str                 # 下一 agent 的 name
    input_filter: HandoffInputFilter | None = None
    nest_handoff_history: bool | None = None
    strict_json_schema: bool = True
    is_enabled: bool | Callable[..., bool] = True
```

key insights：

- handoff **是包裝過的 tool**：模型看到的是「`transfer_to_billing_agent` tool」，呼叫它 = 觸發切換
- `input_filter: HandoffInputFilter` 是 multi-agent context flow 的核心開關。default = 整段 conversation history 給下一 agent；可以改成「只給最後 N 條」「過濾掉 tool calls」「nest 整段 history 變成單一 system message」等等
- `nest_handoff_history` boolean — 是否把前 agent 的整段對話 nest 成下一 agent 的「先前對話摘要」
- `is_enabled` 可以動態 — 例如「user 沒驗 KYC 之前不能 handoff 到 billing agent」

### 3.2 `HandoffInputData`（前後 agent 的 context bridge）

```python
@dataclass(frozen=True)
class HandoffInputData:
    input_history: str | tuple[TResponseInputItem, ...]  # 原始 input
    pre_handoff_items: tuple[RunItem, ...]               # 切換前已生 items
    new_items: tuple[RunItem, ...]                       # 本 turn 新生 items（含 handoff trigger）
    run_context: RunContextWrapper[Any] | None
    input_items: tuple[RunItem, ...] | None              # 改用此覆蓋給下 agent 的 input
```

`input_filter(data) → data` 是純函數，filter 寫起來像 reducer。`extensions/handoff_filters.py` 提供 `remove_all_tools` / `summarize_history` 等現成 filter。

### 3.3 對 AgenticRAG 的價值

當前 AgenticRAG = single agent + tool-loop。要做「retrieve agent → answer agent → cite-verify agent」這種 RAG pipeline，handoff 是現成 abstraction。落地路徑：

1. AgenticRAG `Agent` definition 增加 `handoffs: list[Handoff]` 欄位
2. 內建 `RetrievalAgent` / `AnswerAgent` / `VerifierAgent` 三個 sub-agent
3. `handoff_filter` 寫一個「只給 retrieval 結果不給原 query」這樣保 verify agent 客觀
4. Top-level orchestrator agent 用 `handoffs=[retrieve, answer, verify]`

LOC 估：~150 (handoff core) + 100 (3 個 sub-agent definition)。讀 `handoffs/__init__.py` 一份檔即可開工。

---

## 4 · Guardrails — 兩層

### 4.1 Whole-run guardrails（guardrail.py）

```python
@dataclass
class InputGuardrail(Generic[TContext]):
    guardrail_function: Callable[..., GuardrailFunctionOutput]
    name: str | None = None
    run_in_parallel: bool = True   # ← 預設並行 run；可改成 sequential block

@dataclass
class GuardrailFunctionOutput:
    output_info: Any
    tripwire_triggered: bool       # ← True 即立刻 raise InputGuardrailTripwireTriggered
```

Output guardrail 同樣 shape，但只在 final output 跑。Trip 之後 raise 中斷整個 run。

key design：`run_in_parallel=True`（預設）讓 guardrail 跟主 turn 並行；越早 trip 越早砍。**不是序列化 hard block**。

### 4.2 Per-tool guardrails（tool_guardrails.py）

```python
@dataclass
class ToolGuardrailFunctionOutput:
    output_info: Any
    behavior: AllowBehavior | RejectContentBehavior | RaiseExceptionBehavior

# 三種行為：
AllowBehavior(type="allow")                              # 過
RejectContentBehavior(type="reject_content", message=…)  # 替換 tool output 為 message，繼續
RaiseExceptionBehavior(type="raise_exception")           # 直接中斷
```

`@tool_input_guardrail` / `@tool_output_guardrail` 是 decorator，把純函式變成 guardrail。極簡用法：

```python
@tool_input_guardrail
def block_pii(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    if contains_pii(data.context.tool_arguments):
        return ToolGuardrailFunctionOutput.reject_content(
            "我沒辦法用包含 PII 的 query 搜尋。請改寫後再試。"
        )
    return ToolGuardrailFunctionOutput.allow()
```

### 4.3 對 AgenticRAG 的價值

幾個 immediate use case：

| Use case | guardrail 類型 | 行為 |
|---|---|---|
| user query 太短 / 太空泛 | InputGuardrail | tripwire — 提早回「可否補充？」不浪費 retrieval budget |
| query 含 PII / SQL injection 字樣 | ToolInputGuardrail on `vector_search` | reject_content with "請去除個資後重試" |
| retrieval 結果都低於 threshold | ToolOutputGuardrail on `vector_search` | reject_content with "找不到相關文件，請改 query" 給 LLM 看 |
| answer 沒附 citation | OutputGuardrail | tripwire — 重試 |
| answer 跟 retrieval chunks 距離過遠（hallucination 偵測） | OutputGuardrail | tripwire 或重試 |

落地：~80 LOC 起步，建一個 `agentic_rag/guardrails/` 子目錄，每條 use case 一個 file。

---

## 5 · Memory / Session — 對話歷史抽象

### 5.1 Session protocol（memory/session.py）

```python
@runtime_checkable
class Session(Protocol):
    session_id: str
    session_settings: SessionSettings | None = None

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]: ...
    async def add_items(self, items: list[TResponseInputItem]) -> None: ...
    async def pop_item(self) -> TResponseInputItem | None: ...
    async def clear_session(self) -> None: ...
```

四個方法，兜出整個 conversation persistence 抽象。`Runner.run(... session=session)` 傳進去後，turn loop 自動：
- run 開始時 `await session.get_items()` 載入歷史
- 每 turn 結束 `await session.add_items(new_items)` 增量寫入
- HITL pop / rewind 時 `await session.pop_item()`

### 5.2 內建 4 個 backend

| Backend | 用途 |
|---|---|
| `SQLiteSession` | 本地 file / in-memory（dev / single-host）|
| `OpenAIConversationsSession` | 用 OpenAI server-managed Conversations API（雲端持久化） |
| `OpenAIResponsesCompactionSession` | 包裝任一 Session，達到 threshold（default 10 items）自動呼叫 `responses.compact` API |
| 自己實作 Protocol | Postgres / Redis / S3 等 |

### 5.3 `OpenAIResponsesCompactionSession` ── decorator pattern

```python
class OpenAIResponsesCompactionSession(SessionABC, OpenAIResponsesCompactionAwareSession):
    def __init__(
        self,
        session_id: str,
        underlying_session: Session,                          # ← 包別人
        *,
        client: AsyncOpenAI | None = None,
        model: str = "gpt-4.1",
        compaction_mode: Literal["previous_response_id", "input", "auto"] = "auto",
        should_trigger_compaction: Callable[[dict], bool] | None = None,
    ): ...
```

精彩之處：用 **decorator pattern** 包現有 session，不需要改 `Runner.run` 一行。decorator 在 `add_items` 後檢查 threshold，超過就 fire-and-forget 呼叫 OpenAI 的 `responses.compact`。AgenticRAG 的 sliding-window compact 已經是 in-process 版本，這個 pattern 給的是「server-side compaction 為什麼不用」的 reference。

### 5.4 對 AgenticRAG / anila-core 的價值

anila-core 沒有 Session abstraction —— conversation history 散在 `anila_core/storage/`、`compact/`、`memory/`。重點 takeaway：

1. **抽 Session protocol 出來放 storage/ports.py**，4 method
2. 在 `engine/query_engine.py` 入口接受 optional `session: Session`
3. 提供 `MemoryFileStoreSession` / `PostgresSession` / `CompactingSession` 三個 impl
4. RAG 用 `CompactingSession.wrap(PostgresSession(...))` 自動把長對話 compact

LOC 估：~200。特別有用的副作用：把目前 anila-core 已實作的 SessionMemory + Compact 整合在 single boundary 後，CSP 那邊可以直接 `session.add_items(history)` 帶進來，不用每個 caller 自己去處理 history persistence。

---

## 6 · Tool framework

### 6.1 Tool 三大族（tool.py）

```python
Tool = Union[
    FunctionTool,               # 一般 Python function 包裝
    HostedTool,                 # OpenAI server-side tool（file_search / web_search / code_interpreter）
    ComputerTool,               # Computer use（vision + click 控制）
    LocalShellTool,             # 本機 shell 執行
    ApplyPatchTool,             # apply_patch sandbox tool
    AgentSearchTool,            # 內部「找其他 agent」tool
    AgentTool,                  # 把另一個 Agent 當 tool 用
]
```

`AgentTool` 跟 handoff 的差別：
- **Handoff** = 控制權**轉移**給下個 agent
- **AgentTool** = 把另一個 agent **當 sub-routine** 用，結果回給呼叫者，控制權不轉

### 6.2 FunctionTool 的 schema 自動產生

`function_schema.py` 會檢查 Python function 的 type hints + docstring，自動生成 OpenAI function calling 需要的 JSON Schema。

```python
@function_tool
def search_docs(
    query: str,
    top_k: int = 5,
    language: Literal["zh", "en"] = "zh",
) -> str:
    """Search the corpus.

    Args:
        query: The search query.
        top_k: How many results to return.
        language: Filter by language.
    """
    ...
```

→ 自動變成正確 schema 的 FunctionTool；docstring 的 Args 段被解析出來填 parameter description。

### 6.3 `tool_use_behavior` — agent 的 tool 使用策略

```python
tool_use_behavior: (
    Literal["run_llm_again", "stop_on_first_tool"]
    | StopAtTools                                 # {stop_at_tool_names: [...]}
    | ToolsToFinalOutputFunction                  # 自己寫 callback 決定
) = "run_llm_again"
```

很實用：對「single-shot tool agent」可以設 `stop_on_first_tool` 直接把第一個 tool output 當 final answer，不浪費一次 LLM 呼叫。AgenticRAG 的 RAG agent 大可在「reranker 跑完直接出答案」這類 case 用 `StopAtTools(stop_at_tool_names=["rerank_and_answer"])`。

---

## 7 · MCP（Model Context Protocol）

### 7.1 MCP server 三種傳輸

```
MCPServerStdio              ← 本地 process via stdio（最常見）
MCPServerSse                ← 遠端 SSE
MCPServerStreamableHttp     ← 遠端 HTTP streaming
```

每種都實作 `MCPServer` ABC，提供 `connect()` / `list_tools()` / `call_tool()` / `cleanup()`。

### 7.2 `MCPServerManager`（mcp/manager.py）

production 用法。把多個 MCPServer instance 餵進去，async context manager 處理 connect / cleanup / failed-server retry：

```python
async with MCPServerManager(
    [slack_server, github_server, jira_server],
    drop_failed_servers=True,    # 連不上的就跳過，不影響 agent run
    strict=False,
    connect_in_parallel=True,
    connect_timeout_seconds=10.0,
) as manager:
    agent = Agent(name="X", mcp_servers=manager.active_servers)
```

### 7.3 與 Agent 的整合

`Agent.mcp_servers: list[MCPServer]` 是 field。`AgentBase.get_all_tools()` 在每 turn 開始**動態 fetch** MCP tools 跟 function tools 合在一起餵給 LLM：

```python
async def get_all_tools(self, run_context):
    mcp_tools = await self.get_mcp_tools(run_context)
    enabled = [t for t in self.tools if is_enabled(t)]
    return prune_orphaned_tool_search_tools([*mcp_tools, *enabled])
```

→ 這代表 MCP server 中途加 tool / 撤 tool，下一輪 turn 自動反映，不需 restart。

### 7.4 對 AgenticRAG 的價值

最直接的應用：**讓 enterprise 用戶把自家內部系統包成 MCP server，AgenticRAG 自動消費**。譬如：

- `slack-mcp-server` → `search_messages` / `get_thread` tool
- `confluence-mcp-server` → `search_pages` tool
- `github-mcp-server` → `search_code` / `read_file` tool

AgenticRAG 端 0 改動，只要 user 在 `anila-agent.yaml` 配上 `mcp_servers: [...]`，新 tool 自動進到 RAG agent 的 tool list。

落地：先把 `MCPServer` Protocol + `MCPServerManager` pattern 翻成 Python；連接細節（stdio / SSE / HTTP）可以三選一先做 stdio，其他 incremental。LOC ~250。

---

## 8 · Tracing（observability）

### 8.1 Span 樹

```
Trace (root)
 └─ TaskSpan         一次 logical operation
     ├─ AgentSpan    一個 agent 的 lifetime
     │   ├─ TurnSpan         一次 turn loop
     │   │   ├─ GenerationSpan    LLM call
     │   │   │   └─ ResponseSpan  raw response
     │   │   ├─ FunctionSpan      function tool call
     │   │   ├─ MCPListToolsSpan  動態列 MCP tools
     │   │   └─ GuardrailSpan     guardrail 跑了
     │   └─ HandoffSpan
     └─ CustomSpan   user-defined
```

每個 span 帶 typed `SpanData`（`AgentSpanData` / `GenerationSpanData` / etc.），結構化、可序列化。

### 8.2 Processor pipeline（tracing/processors.py）

```python
class TracingProcessor(Protocol):
    def on_trace_start(self, trace: Trace): ...
    def on_trace_end(self, trace: Trace): ...
    def on_span_start(self, span: Span[Any]): ...
    def on_span_end(self, span: Span[Any]): ...
    def shutdown(self): ...
    def force_flush(self): ...
```

預設 batch processor 把 spans buffer 後送 OpenAI tracing backend；可以自己實作 processor 送到 Datadog / Honeycomb / OTel collector。

### 8.3 開發者用法

```python
from agents import trace, agent_span, function_span

with trace("legal-question-answer-flow"):
    result = await Runner.run(legal_agent, "...")
    # 自動產生：trace → AgentSpan → TurnSpan → GenerationSpan...

# 手動加 custom span：
with custom_span("vector-search-rrf-fusion") as span:
    span.span_data.set("top_k", 5)
    span.span_data.set("min_score", 0.7)
    hits = await vector_search(...)
```

### 8.4 對 AgenticRAG 的價值

目前 `anila-core/engine/query_engine.py` 只有 `_post_turn_hooks`，**沒有結構化 trace**。痛點：debug RAG quality 時看不到「哪個 chunk 命中、reranker 怎麼排、為什麼選這個 strategy」的時序紀錄。

落地路徑：

1. 從 `tracing/spans.py` 抄 `Span` / `Trace` data class shape（不抄實作）
2. 寫 `anila_core/tracing/` 子套件：core types + 一個 default in-memory processor
3. `query_engine.run_stream` 包進 `with trace(...)`；每個 chunker / embedder / retriever 呼叫包成 span
4. CSP audit_logs 加一個 `trace_id` 欄位串起來
5. 後端可以 dump trace 到 Studio / EvaluatorView 給 dev review

LOC ~300，但是**最高 ROI 的 single 加法之一** —— 一旦 tracing 上線，所有後續 RAG quality 改進都有 ground truth 可比。

---

## 9 · Sandbox — tool 隔離執行

### 9.1 結構

```
sandbox/
├── apply_patch.py         apply_patch tool 整套（diff 解析 + apply）
├── files.py               檔案系統 abstractions
├── manifest.py            Mount / capability declaration
├── manifest_render.py     human-readable manifest description
├── capabilities/          per-capability validation
├── entries/               file system entry types
├── instructions/          給 LLM 看的 sandbox instructions
└── workspace_paths.py     SandboxPathGrant + path coercion
```

`Manifest` 是 declarative 的 — declare 哪些 path / 哪些 command 可以動，runtime 強制 enforce。default allowlist 在 manifest.py 開頭：`ls / find / cat / grep / rg / head / tail / ...`。

### 9.2 對 AgenticRAG 的價值

如果 AgenticRAG 的 RAG agent 未來要做：
- 「執行 user 提供的 SQL」
- 「跑 user 寫的 transformer pipeline」
- 「在 user workspace 寫檔案」

這類「user 給的 untrusted code」就要走 sandbox。OpenAI 的 sandbox 子系統是 production-tested 的範本，但複雜度高（manifest、capabilities、entries）。

**短期不需要**，但 design 設計可以先 align —— 譬如 `agentic_rag.tools` 裡未來加 `execute_user_query_tool` 時，先預留 `manifest: SandboxManifest | None` 欄位，內部用 None 表示 trusted。日後接 sandbox 不破 API。

---

## 10 · Lifecycle hooks — observability + plugin point

### 10.1 兩層 hook

`RunHooksBase` = run 級（一次 run 跑一輪）：
- `on_llm_start` / `on_llm_end`
- `on_agent_start` / `on_agent_end`
- `on_handoff`
- `on_tool_start` / `on_tool_end`

`AgentHooksBase` = agent 級（同 agent 變 active 時觸發）：
- 同上 7 個

→ 12 個明確 lifecycle 點。比 `_post_turn_hooks` (anila-core 目前只有 1 個) 細粒度多。

### 10.2 用法

```python
class MyAuditHooks(AgentHooks):
    async def on_tool_start(self, context, agent, tool):
        await audit_log.write({
            "agent": agent.name, "tool": tool.name,
            "args": context.tool_arguments, "user": context.context.user_id,
        })

agent.hooks = MyAuditHooks()
```

### 10.3 對 AgenticRAG 的價值

立刻可用：
- 實作 `RateLimitHooks` 在 `on_tool_start` 卡 quota
- 實作 `AuditHooks` 取代目前散在各處的 ad-hoc audit_log 寫法
- 實作 `MetricsHooks` 在 `on_llm_end` 寫 token_usage
- Plugin 開發者用 hooks 包自訂行為，不用 fork core

落地：~80 LOC。先把現有 anila-core engine 的 `_post_turn_hooks` 升級成 12-點 `RunHooks` interface，再把 CSP `proxy_service` 對 audit / metrics 的 ad-hoc 處理搬到 hook 實作裡。**比 tracing 更輕量但同樣高 ROI**。

---

## 11 · Stream events — 統一前端串流模型

### 11.1 三種 stream event

```python
@dataclass
class RawResponsesStreamEvent:
    """OpenAI Responses API 原生 raw stream chunk（最低層級）"""
    data: ResponseStreamEvent
    type: Literal["raw_response_event"] = "raw_response_event"

@dataclass
class RunItemStreamEvent:
    """SDK 把 raw events 重組後的『semantic event』(一個 message / tool call / tool output / handoff trigger 等)"""
    name: Literal["message_output_created", "tool_called", "tool_output", "handoff_requested", ...]
    item: RunItem
    type: Literal["run_item_stream_event"] = "run_item_stream_event"

@dataclass
class AgentUpdatedStreamEvent:
    """current agent 切換（handoff 觸發）"""
    new_agent: Agent[Any]
    type: Literal["agent_updated_stream_event"] = "agent_updated_stream_event"
```

意思：用戶 iterate 整個 stream 時看到三層粒度的事件。要顯示 typing dots 用 `RawResponsesStreamEvent`；要顯示「agent 在用 X 工具」用 `RunItemStreamEvent.name == "tool_called"`；要顯示「現在切換到 billing agent」用 `AgentUpdatedStreamEvent`。

### 11.2 對 AgenticRAG 的價值

目前 AgenticRAG 的 SSE `/agentic-chat` 自定 12 種 event type（`message_delta` / `tool_call_started` / `usage_update` 等），不是 OpenAI 原生格式。如果做 multi-agent handoff，現有 stream event 模型不夠。

落地：把 anila-core / AgenticRAG 的 SSE event taxonomy 照這份架構 align 成三層：
- **Raw**: text delta / token level
- **RunItem**: tool_called / handoff_requested / message_complete
- **AgentUpdated**: handoff 切換

frontend 跟 client SDK 都受益（統一語意、容易測試）。LOC ~150。

---

## 12 · Retry — model call robustness

### 12.1 結構

```python
@dataclass
class ModelRetrySettings:
    max_attempts: int = 3
    backoff: ModelRetryBackoffSettings    # initial / max delay / multiplier / jitter
    advice_provider: Callable[[ModelRetryAdviceRequest], ModelRetryAdvice] | None = None
```

`ModelRetryAdvice` 容許 caller 動態決定 retry 策略：

```python
@dataclass
class ModelRetryAdvice:
    decision: RetryDecision           # retry / abort / final
    delay_seconds: float | None
    reason: str
```

adviser 接 `ModelRetryNormalizedError`（429 / 500 / timeout / context_length_exceeded 等被分類過）+ retry attempt count，回 advice。

### 12.2 預設值

```python
DEFAULT_INITIAL_DELAY_SECONDS = 0.25
DEFAULT_MAX_DELAY_SECONDS = 2.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_JITTER = True
```

### 12.3 對 AgenticRAG 的價值

目前 AgenticRAG 對 model call 失敗沒有結構化重試。CSP `proxy_service` 有 `PROXY_MAX_RETRIES` 但是 ad-hoc。值得抄的 design：

- `RetryDecision` enum + `decision` 欄位讓重試策略可被 callable 換掉
- `ModelRetryNormalizedError` 把 raw exception 分類 — 不同 error code 不同 retry policy（429 應該長 backoff、context_length_exceeded 不該 retry）
- 把 retry 從 proxy_service 抽出來放 `anila-core/engine/retry.py`，agent 可選 inject

LOC ~180。

---

## 13 · 不該移植的東西

| 模組 | 為什麼不移 |
|---|---|
| `realtime/` | 走 voice / WebSocket / OpenAI Realtime API；ANILA 的後端服務模型不對齊；如果未來做 voice-RAG 再來 |
| `models/openai_*` 全部 | 直接 lock 到 OpenAI Responses + Conversations API；ANILA 走 OpenAI-compat 抽象（`anila-core/providers/openai_compat.py`）已經涵蓋同個 surface area |
| `extensions/handoff_filters.py` 內具體 filter | 看設計學概念；filter 本身的 prompt 跟邏輯要 ANILA 自己寫（避免 derivative work）|
| `prompts.py` 跟 OpenAI Prompts API 整合 | OpenAI 雲端產品，on-prem 不需要 |
| `sandbox/` 全套 manifest 系統 | 短期 RAG 用不到；複雜度太高，等真要做 untrusted code execution 再評估 |
| `models/openai_agent_registration.py` | OpenAI server-side agent registration（雲端產品功能）|

---

## 14 · 總體 takeaway

### 14.1 design 上能直接抄的 invariants

1. **Agent = dataclass，state 不在 agent 上**。state 全在 `RunState` / `Session` / `RunContextWrapper`。Agent instance 可被多 run 共享。
2. **public façade vs internal split**。當 file > 500 行就拆 `internal/` 子目錄，public file 只放 wiring + composition。
3. **`NextStep` union 表達 turn outcome**。turn loop 不是 if/else 連環，是「跑完拿到 NextStep → 對哪個 variant 反應」。
4. **`Session` Protocol 4 method 抽掉 conversation persistence**。把實作拆 decorator 套疊（`CompactingSession.wrap(SqliteSession(...))`）。
5. **Streaming 跟 non-streaming 行為對齊是硬性 invariant**。只 emit 時機差異，不該有 turn-decision 邏輯差異。
6. **`RunState` schema versioned**。每次 schema 動就 bump `CURRENT_SCHEMA_VERSION` 跟 `SCHEMA_VERSION_SUMMARIES`。released schema 不可變。
7. **Hooks 12 點 + Tracing span tree** 是 observability minimum baseline，不是 nice-to-have。

### 14.2 對 AgenticRAG 強化的 P0 順序（runtime_logic README 已記但這裡更具體）

| # | 強化點 | 起點檔 | 為什麼這個順序 |
|---|---|---|---|
| 1 | **Lifecycle hooks 12 點** | `lifecycle.py` 全 200 行 | 最便宜、立刻能拆掉散落 audit / metrics 程式碼，產生信號為其他工作的 baseline |
| 2 | **Tool guardrails 三 behavior** | `tool_guardrails.py` 全 280 行 | 安全性最大 ROI，落到 RAG search tool 直接擋 PII / SQL inject |
| 3 | **Tracing span tree** | `tracing/__init__.py` + `spans.py` + `processors.py` | 之後做 multi-agent handoff 一定需要 trace 才 debug 得動 |
| 4 | **Multi-agent handoff** | `handoffs/__init__.py` + `extensions/handoff_filters.py` | RAG quality 下一躍升點；前 3 步驟做完才有足夠工具撐住 |
| 5 | **Session Protocol + CompactingSession decorator** | `memory/session.py` + `memory/openai_responses_compaction_session.py` | 把 anila-core 散落的 history persistence 收掉 |
| 6 | **MCP server 整合** | `mcp/server.py` + `mcp/manager.py` + `mcp/util.py` | enterprise feature，做 multi-agent 後再開即可 |
| 7 | **`run_internal/` 拆分風格** | `run_internal/__init__.py` + AGENTS.md 規範段 | 不是新 feature，是當 anila-core/engine 又長了再 refactor 的 reference |

整段 1–7 加總 LOC 估 ~1200，4–5 週工作量。但每一項都可獨立 ship；P0-1 兩天就可以見效。

---

## 15 · 怎麼用本文

把這份文件當成 **「目錄索引」**：

- 想查某個能力 → 第 X 章對應的 `對 AgenticRAG 的價值` 段
- 想動手前讀什麼 → 第 X 章開頭的「結構」段，列出該模組的核心檔
- 想知道為什麼這樣設計 → 第 14 章 takeaway

**讀完不需要把整份 SDK 看完**。讀對自己要做的能力對應的那 1–2 個檔，看清介面、寫自己的版本。Source tree 在 `runtime_logic/openai-agents-python/src/agents/`（gitignored，本機）。

---

**Last updated**: 2026-05-02 · **Source studied**: openai-agents-python @ RunState schema 1.9 · **Next consumer**: AgenticRAG retrieval pipeline 重構 + anila-core engine refactor

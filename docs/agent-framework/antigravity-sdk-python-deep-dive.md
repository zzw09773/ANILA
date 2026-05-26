# Google Antigravity SDK (Python) Deep Dive — for `anila-agent` template integration

> **Status**: 完成深度分析，提供 borrow / skip 決策清單
> **Date**: 2026-05-26
> **Analyser**: ANILA 平台 sub-agent runtime 分析師
> **Source under review**: `anila-agent/templete/antigravity-sdk-python/` (`google-antigravity` v0.1.1)
> **Target consumer**: `anila-agent/anila_agent/` (現用 `openai-agents` SDK + Anila 自訂 hook/memory)

---

## 0. TL;DR — 一頁總結

Google Antigravity SDK 是 Google 在 2026 年釋出的 agent harness Python wrapper，**外殼是 Python SDK，核心是預先編譯好的 Go 二進位 (`localharness`)**。它鎖死 Gemini 系列模型，但 SDK 自身的 module 邊界、hook 分類、policy DSL、trigger 抽象、connection 多後端設計，都是**比 anila-agent 目前用的 openai-agents 更成熟、更專門為 agent 場景設計**的工程模式。

借鑑優先級（P0 = 立刻納入下一輪 sprint，P1 = 中期重構排程，P2 = 評估後可考慮）：

| Pattern | 來源 module | 優先級 | 工作量 |
|---|---|---|---|
| **三類 hook 強型別分類** (Inspect / Decide / Transform) | `hooks/hooks.py` | **P0** | 1-2d |
| **Hook decorator factory** (`@pre_turn`、`@post_tool_call`) | `hooks/hooks.py:244-288` | **P0** | 0.5d |
| **Policy DSL + 優先級 bucket 排序** | `hooks/policy.py` | **P0** | 2-3d |
| **`workspace_only` 路徑安全 policy** | `hooks/policy.py:282-376` | **P0** | 1d |
| **Triggers 子系統** (背景任務 → 注入 agent) | `triggers/` | **P1** | 3-5d |
| **`ConnectionStrategy` 三層架構** (Agent/Conversation/Connection) | `connections/` + `conversation/` | **P1** | 1w+ |
| **`ToolContext` + auto-injection** | `tools/tool_context.py` + `tools/tool_runner.py:41-99` | **P1** | 1-2d |
| **`HookContext` 三層 scope** (Session/Turn/Operation) | `hooks/hooks.py:34-86` | **P1** | 1d |
| **`McpBridge` (stdio/SSE/HTTP 三種 transport)** | `mcp/bridge.py` | **P1** | 1-2d |
| **`disable` vs `deny` 二維工具控管** | `types.py:311-365` + README | **P2** | 0.5d (純文件 + flag) |
| **`Conversation` 層的 turn / compaction index 追蹤** | `conversation/conversation.py` | **P2** | 2-3d |

**不該抄的**：`localharness` 編譯 Go binary + WebSocket + protobuf 全套底層（鎖 Gemini、不利 on-prem）、`GeminiConfig` 與 `gemini-3.1-flash-image-preview` image model 預設、`StreamableHttp` MCP 的特定 timeout 細節（看需求）。

---

## 1. SDK 簡介

### 1.1 是什麼

- **PyPI 套件名**：`google-antigravity`
- **版本**：0.1.1（Development Status: 3 - Alpha）
- **License**：Apache 2.0
- **Python 需求**：3.10+
- **核心依賴**：`google-genai>=1.0`、`mcp>=1.0`、`pydantic>=2.0`、`uvicorn>=0.46`、`websockets>=12.0`、`protobuf>=4.25`
- **官方定位**（節錄自 `README.md`）：

  > The Google Antigravity SDK is a Python SDK for building AI agents powered by Antigravity and Gemini. It provides a secure, scalable, and stateful infrastructure layer that abstracts the agentic loop, letting you focus on what your agent does rather than how it runs.

### 1.2 在 Google 生態的角色

Antigravity 是 Google 2025/2026 年推出的 agent IDE 平台（與 Gemini Code Assist 同源）。這個 SDK 是該平台的「外接 Python」介面，主要供：
1. 在 Gemini Code Assist / Antigravity IDE 外部編寫 agent
2. 將 Gemini 3.x 系列當作 agentic backbone 接到自家 pipeline

關鍵架構特性（從 `pyproject.toml` 與 `connections/local/local_connection.py` 可確認）：

```
+--------------------------+
|  Python SDK (本 repo)    |  ← Layer 1/2/3 都在 Python
+--------------------------+
            ↓ WebSocket + protobuf JSON
+--------------------------+
|  localharness (Go binary)|  ← 預編譯，wheels 才有，clone repo 不夠用
+--------------------------+
            ↓ HTTPS
+--------------------------+
|  Gemini API              |
+--------------------------+
```

這就是為什麼 README 明寫 _"Cloning this repository alone is not sufficient to run the SDK. Always install from PyPI"_：核心 agentic loop 是 Go binary，Python 只是 RPC client + 開發者 API。

**對 anila-agent 的關鍵啟示**：SDK 的「分層」純粹是 Python 層的設計取捨，我們可以借鑑分層思路，但**不要被 Go binary 綁架**——`localharness` 對應的「runtime」在 anila-agent 是 `openai-agents` 的 `Runner.run()`，那一塊不換。

### 1.3 三層架構（README §Architecture）

| Layer | 角色 | 關鍵類別 |
|---|---|---|
| Layer 1 — Simplified | 開箱即用 entry point | `Agent` |
| Layer 2 — Session | 帶歷史 / 收斂 / 統計的 session | `Conversation`、`ChatResponse`、`Step`、`ToolCall`、`AgentConfig`、`HookRunner`、`ToolRunner`、`TriggerRunner` |
| Layer 3 — Adapter | Transport / backend 抽象 | `Connection`、`ConnectionStrategy`、`LocalConnection` |

---

## 2. 架構俯瞰

### 2.1 Top-level package layout

```
google/antigravity/
├── agent.py            # Layer 1: Agent 類別 (high-level entry)
├── __init__.py         # 對外 re-export (只暴露 Agent / *Config / ToolContext)
├── types.py            # 33k LOC — 所有 pydantic 模型 (ToolCall / Step / HookResult / ChatResponse / Image / Document …)
├── types_test.py
│
├── connections/        # Layer 3 — transport 抽象
│   ├── connection.py   # ABC: Connection / ConnectionStrategy / AgentConfig
│   └── local/          # 唯一具體實作：透過 Go binary + WebSocket
│       ├── local_connection.py        # 61KB — LocalConnection / LocalConnectionStrategy
│       ├── local_connection_config.py # LocalAgentConfig (pydantic)
│       └── localharness_pb2.py        # 25KB — generated protobuf bindings
│
├── conversation/       # Layer 2 — 帶歷史的 session wrapper
│   └── conversation.py # Conversation 類別 (history / turn / compaction / usage)
│
├── hooks/              # Hook + Policy 系統 (本報告重點)
│   ├── hooks.py        # InspectHook / DecideHook / TransformHook + 9 個 concrete hook + decorators
│   ├── hook_runner.py  # HookRunner — 註冊 / dispatch
│   ├── policy.py       # 19KB — Policy DSL + 優先級 bucket + workspace_only
│   └── README.md       # 13KB — 完整 hook taxonomy 設計說明
│
├── triggers/           # 背景任務 → 注入 agent (anila-agent 沒有)
│   ├── triggers.py     # TriggerContext + Trigger type + @trigger decorator
│   ├── trigger_runner.py
│   ├── helpers.py      # every() + on_file_change()
│   └── README.md
│
├── tools/              # 工具註冊 / 執行
│   ├── tool_runner.py  # ToolRunner — 含 ToolContext auto-injection
│   └── tool_context.py # ToolContext — Layer 2 對 tool 暴露的 capability
│
├── mcp/
│   └── bridge.py       # McpBridge — stdio/SSE/HTTP 三種 MCP server 接入
│
└── utils/
    └── interactive.py  # CLI REPL + ToolConfirmationHook + AskQuestionHook
```

### 2.2 一段話總覽

```
                    +----------+
   user prompt --> | Agent    | (Layer 1, async ctx mgr, 預設讀-only)
                    +----------+
                         |
                         v
                    +-------------+      +-------------+
                    | Conversation| <--> | HookRunner  | (Inspect/Decide/Transform)
                    | (Layer 2)   |      +-------------+
                    +-------------+      +-------------+
                         |          <--> | ToolRunner  | (auto-inject ToolContext)
                         v                +-------------+
                    +-------------+      +-------------+
                    | Connection  | <--> | TriggerRunner| (背景 asyncio.Task)
                    | (Layer 3)   |      +-------------+
                    +-------------+      +-------------+
                         |          <--> | McpBridge   | (stdio/SSE/HTTP)
                         v                +-------------+
                    +---------------+
                    | LocalHarness  | (Go binary, WebSocket + protobuf)
                    | + Gemini API  |
                    +---------------+
```

### 2.3 設計亮點（讀完原始碼最有感的幾點）

1. **單一 `AgentConfig` 抽象基底，策略繫於 `create_strategy()`**
   `connection.py:32-103` 把 backend 選擇權交給 config subclass —— `LocalAgentConfig.create_strategy()` 回傳 `LocalConnectionStrategy`。將來要加 `RemoteAgentConfig`、`CloudRunAgentConfig` 不用改 `Agent` 類別。

2. **Hook 系統有明確 taxonomy，不是雜亂的 callback list**
   三個 ABC（`InspectHook` / `DecideHook` / `TransformHook`）+ 9 個 concrete subclass，每個 hook 嚴格知道自己能不能修改資料、能不能阻斷流程。

3. **Policy 不是 hook，是「會編譯成 hook」的 DSL**
   `policy.enforce(policies)` 把宣告式 policy 列表轉成一個 `_PolicyDecideHook` 實例。`Specific Deny > Specific Ask > Specific Allow > Wildcard Deny > Wildcard Ask > Wildcard Allow` 的 bucket priority 排序很乾淨。

4. **Triggers vs Hooks 是明確劃分的兩種 concern**
   `triggers/README.md` 開頭表格直接點明：hook = 反應 agent lifecycle、trigger = 反應外部事件（cron / file watch / webhook）。

5. **ToolContext 與 HookContext 故意切開**
   `hooks/README.md` 直接說 _"These systems do not share state"_，理由：lifecycle / threading model / 用途不同。

---

## 3. anila-agent 對照表 — 同樣概念兩家怎麼做

> 這節是報告主體。每個 module 都用「Antigravity 怎麼做 / anila-agent 怎麼做 / 設計差異 / 誰贏 / 怎麼借鑑」五段式評估。

### 3.1 Agent 組裝 / lifecycle

| 維度 | Google Antigravity | anila-agent (現況) |
|---|---|---|
| 入口類別 | `google.antigravity.Agent` (`agent.py:36-238`) | `anila_agent.core.agent.AssembledAgent` + `core.runner.AnilaRunner` (兩個類別) |
| Lifecycle | `async with Agent(config) as agent:`（async context manager，內部 `AsyncExitStack` 管 trigger / mcp / connection） | `await runner.start()` → 多次 `await runner.send()` |
| Config | Pydantic `AgentConfig` ABC + 子類 `LocalAgentConfig`（單一物件） | dataclass-based `AppConfig` 由 4 個 YAML 拼起來 (`utils/config.py`) |
| 結構 | 1 個 `Agent` 物件聚合 strategy/conversation/runners/MCP | 拆成 `build_agent()`（一次性 wiring 函式）+ `AnilaRunner`（送 prompt 的 stateful 物件） |

**設計差異**

Antigravity 把「config → wiring → 執行」全部塞進 `Agent.__aenter__()`，可以把 trigger / MCP / hook 全部用 `AsyncExitStack` 串起來自動 teardown；anila-agent 則是「build (純函式) → AnilaRunner (有狀態)」兩段式。

**誰的設計比較好？**

- **Antigravity 贏** 在 lifecycle 簡潔、teardown 一致。但 `AgentConfig.model_copy(deep=True)` 的副作用（破壞 hook 物件 identity）需要用 `self._pending_hooks = list(config.hooks)` 繞，這段 hack 在 `agent.py:56-61` 有註解，是被 deep copy 逼出來的。
- **anila-agent 贏** 在 build/run 分離 → testability、`build_agent()` 是純函式可以多次組裝不同變體（測試 / staging / prod）。

**怎麼借鑑**

P1 重構建議：保留 `build_agent()` 純函式，但把 `AnilaRunner` 改成 async context manager（`async with AnilaRunner(assembled) as runner:`），用 `AsyncExitStack` 統一管理「trigger runner + MCP bridge + retriever connection pool」的 lifecycle。

---

### 3.2 Hooks — 最值得抄的部分

#### 3.2.1 Antigravity 怎麼做

**三類 hook（type-level 分類）**：

| 類別 | 簽章 | 能不能改資料 | 能不能阻斷 | 例子 |
|---|---|---|---|---|
| `InspectHook[T]` | `async run(ctx, data: T) -> None` | 不行 | 不行 | `OnSessionStartHook`、`PostToolCallHook`、`PostTurnHook`、`OnCompactionHook` |
| `DecideHook[T]` | `async run(ctx, data: T) -> HookResult` | 不行 | 可（`HookResult(allow=False)`） | `PreTurnHook`、`PreToolCallDecideHook` |
| `TransformHook[T, R]` | `async run(ctx, data: T) -> R` | 可 | 可（拋 exception） | `OnToolErrorHook`、`OnInteractionHook` |

**9 個具體 hook**（`hooks/hooks.py:150-238`）：
- Session: `OnSessionStartHook`、`OnSessionEndHook`
- Turn: `PreTurnHook`、`PostTurnHook`
- Tool: `PreToolCallDecideHook`、`PostToolCallHook`、`OnToolErrorHook`
- Interaction: `OnInteractionHook`（user prompt for clarification）
- Compaction: `OnCompactionHook`（context window 收斂事件）

**Decorator factory**（`hooks/hooks.py:244-288`）：

```python
pre_turn              = _make_hook_decorator(PreTurnHook)
pre_tool_call_decide  = _make_hook_decorator(PreToolCallDecideHook)
on_interaction        = _make_hook_decorator(OnInteractionHook)
on_compaction         = _make_hook_decorator(OnCompactionHook)
on_session_start      = _make_hook_decorator(OnSessionStartHook, pass_data=False)
on_session_end        = _make_hook_decorator(OnSessionEndHook, pass_data=False)
post_turn             = _make_hook_decorator(PostTurnHook)
post_tool_call        = _make_hook_decorator(PostToolCallHook)
on_tool_error         = _make_hook_decorator(OnToolErrorHook)
```

寫起來是這樣（`examples/getting_started/hooks.py`）：

```python
@hooks.pre_tool_call_decide
async def pre_tool(data: types.ToolCall) -> types.HookResult:
    return types.HookResult(allow=True)
```

**HookContext 三層 scope**（`hooks/hooks.py:34-86`）：

```
SessionContext (一場 agent session)
    └── TurnContext (一個 user 提問 → 完整回應週期)
            └── OperationContext (一次 tool call / interaction)
```

每層繼承 parent 的 store，narrow scope 可以讀 broad scope 的值（向上查），但反向不行 — 確保 turn 結束自然清掉 turn 級資料。

#### 3.2.2 anila-agent 現在怎麼做

`anila_agent/core/hooks.py` 6 個 event：`PRE_TOOL_USE / POST_TOOL_USE / STOP / SESSION_START / USER_PROMPT_SUBMIT / PERMISSION_REQUEST`。

```python
# anila_agent/core/hooks.py:79-90
@dataclass(frozen=True)
class HookSpec:
    event: HookEvent
    callback: HookCallback   # 一律是 Callable[[Any], HookOutput | Awaitable[HookOutput]]
    matcher: str = ".*"      # regex 配 tool name
```

特色：
- **單一 `HookOutput` 回傳型別**(`models/schemas.py`)，靠裡面的 flag 決定行為（`continue_`、`decision="block"`、`additional_context`、`updated_input`、`stop_reason`）—— 這是從 claude-code-src 沿襲下來的設計。
- **aggregate 邏輯**（`_AggregatedHookResult`）：last-writer-wins for `updated_input`、union for `additional_contexts`。
- **底層橋接 openai-agents `RunHooks`**：`AnilaRunHooks.on_tool_start` → `fire(PRE_TOOL_USE)`。
- 沒有 HookContext 概念。

#### 3.2.3 設計差異與評比

| 維度 | Antigravity | anila-agent | 評比 |
|---|---|---|---|
| 型別表達力 | Generic `InspectHook[T]` 強型別簽章 | 一律 `Callable[[Any], HookOutput]` | **Antigravity 完勝**：模型化清楚到 mypy 直接幫你檢查 hook 拿到的是什麼資料 |
| 寫法 | `@pre_tool_call_decide` decorator | YAML 設定 `callback: "module.attr"` → 動態 import | anila-agent 較適合「使用者寫設定不寫程式」的 template 場景；Antigravity 較適合 SDK consumer |
| 阻斷流程語義 | `HookResult(allow=False, message)` 明確 | 看 `out.continue_` / `out.decision == "block"` 兩個欄位 | **Antigravity 較清楚**：anila-agent 的「abort vs block」二元差異需要對 schema 熟才看得懂 |
| Context | 三層 scope (Session/Turn/Operation) 自然繼承 | 沒有 — 跨 hook 共用狀態要靠閉包或全域 | **Antigravity 贏**：有 correlation ID / per-turn tracing 需求時直接用 TurnContext.set/get |
| Matcher | 無（hook 自己內部判斷） | regex on tool name | anila-agent 較有彈性，但等於把判斷邏輯下放到 hook 內部 |
| Tool-level metadata | 沒有 | `tools/base.py` 有 `ToolMetadata(is_read_only, is_destructive, requires_confirmation, category)` | anila-agent 較細緻；Antigravity 用 `BuiltinTools.read_only()` class method 等列舉 |

#### 3.2.4 結論

**借鑑優先 P0**：把 anila-agent 的 hook 系統升級成 Antigravity 的 Inspect/Decide/Transform 三類強型別架構，但保留 YAML 設定式註冊（template 場景仍有價值）。

具體做法：
1. 新增 `anila_agent/core/hook_types.py`，定義 `InspectHook[T]` / `DecideHook[T]` / `TransformHook[T,R]` 三個 ABC。
2. 把現有 `HookSpec` 改名為 `LegacyHookSpec` 並標 deprecated，新版採 ABC subclass instance 直接註冊。
3. 加 `HookContext` 三層 scope（從 `hooks/hooks.py:34-86` 幾乎可以原文搬）。
4. Decorator factory：原樣搬 `_make_hook_decorator`。

---

### 3.3 Policy — 第二個必抄

#### 3.3.1 Antigravity 怎麼做

`hooks/policy.py` 整支 19KB 是一個獨立的 DSL，**不是 hook、是「會編譯成 PreToolCallDecideHook 的設定語言」**。

核心 API（`policy.py:135-376`）：

```python
allow(tool, *, when=None, name="")       # APPROVE
deny(tool, *, when=None, name="")        # DENY
ask_user(tool, *, handler, when=None, name="")  # ASK_USER

allow_all()                              # ≡ allow("*")
deny_all()                               # ≡ deny("*")
confirm_run_command(handler=None)        # 預設 policy: 其他都准，run_command 要審
safe_defaults(handler)                   # read_only 全准 + 其他都問
workspace_only(workspaces)               # 把 file_tools 限定在 workspace 內

enforce(policies) -> PreToolCallDecideHook
```

**優先級 bucket（`policy.py:381-409`）**：

```
Level 0 — Specific DENY     (例：deny("run_command"))
Level 1 — Specific ASK_USER (例：ask_user("run_command", handler=...))
Level 2 — Specific APPROVE  (例：allow("read_file"))
Level 3 — Wildcard DENY     (例：deny("*") == deny_all())
Level 4 — Wildcard ASK_USER (例：ask_user("*", handler=...))
Level 5 — Wildcard APPROVE  (例：allow("*") == allow_all())
```

「**Specific > Wildcard、Deny > Ask > Allow**」就一條簡單規則。Bucket 內部「first match wins」，所以註冊順序在同一 bucket 內有意義。

**Predicate（`policy.py:422-466`）支援三種簽章**：
```python
# 1. 收 args dict
policy.deny("run_command", when=lambda args: "rm" in args.get("CommandLine", ""))

# 2. 收 Pydantic model（自動 model_validate）
class RunArgs(BaseModel):
    CommandLine: str
policy.deny("run_command", when=lambda args: "rm" in args.CommandLine, ...)
# 第一個 param 標 RunArgs 就會自動 model_validate

# 3. 收完整 ToolCall（標 types.ToolCall）
policy.deny("run_command", when=lambda tc: ...)
```

**`workspace_only` 路徑安全**（`policy.py:282-376`）：

特別值得讚賞——它把符號連結、Windows / macOS 大小寫不敏感、新建檔案不存在等等坑都處理掉了：

```python
def _secure_normalize_path(path) -> pathlib.Path:
    # resolve(strict=False) - 解析 symlink 但允許 path 還不存在
    return pathlib.Path(path).resolve()

def is_path_in_workspace(target_path, workspace_path) -> bool:
    # 1. 都先 _secure_normalize_path
    # 2. OS-aware case folding (Windows/macOS 不敏感、Linux 敏感)
    # 3. 用 parts list 結構性比對，避免「/foo/bar」前綴匹配到「/foo/bartender」
```

**Fail-closed 預設**（`policy.py:516-528`）：任何 predicate 拋例外 / `_secure_normalize_path` OSError 一律當不允許，註解直接寫 _"Security Fallback: Fail-closed"_。

#### 3.3.2 anila-agent 現在怎麼做

完全沒有 policy DSL。要「禁 destructive tool」要：
1. 在 `tools/base.py` 給 tool 標 `is_destructive=True`
2. 在 `configs/tools.yaml` 註冊一個 PreToolUse hook callback
3. 那個 callback 自己讀 metadata 決定 block/allow

實際上 anila-agent 連 destructive 預設要不要擋都沒有強制——是 _by convention_。

#### 3.3.3 差異與評比

| 維度 | Antigravity | anila-agent |
|---|---|---|
| 表達力 | 宣告式 list，bucket 排序自動 | 命令式 hook callback |
| 安全預設 | `LocalAgentConfig` 預設 `confirm_run_command()`（除非顯式 `allow_all()`，否則 `run_command` 一律 deny） | 完全無預設 |
| 動態判斷 | predicate 三種簽章自動辨識 | 要在 callback 內手寫 |
| 路徑安全 | `workspace_only` 內建（含 symlink/case-folding） | 沒有 |

**Antigravity 完勝**。這塊是 anila-agent 上線到中科院 / 國軍交付場景**安全攻擊面最大的洞**。

#### 3.3.4 結論

**借鑑優先 P0**：原樣 port `hooks/policy.py` 到 `anila_agent/core/policy.py`，把：
- `BuiltinTools.read_only()` / `file_tools()` 換成 anila-agent 的 ToolMetadata 查詢（用 `get_metadata(tool).is_read_only`）
- `_PolicyDecideHook` 換成註冊到我們 hook registry 的 `PreToolUse` matcher hook
- `confirm_run_command` 改成 `confirm_destructive_tools()`，掃 `ToolRegistry` 找 `is_destructive=True` 的工具
- `workspace_only` 直接搬

工作量約 2-3 天，主要時間在 unit test（`policy_test.py` 是 31KB，可以一起 port）。

---

### 3.4 Triggers — anila-agent 沒有的新概念

#### 3.4.1 是什麼

> **Triggers are long-lived async functions that run alongside an agent session. They react to external events (cron schedules, file changes, webhooks) and push messages back into the agent.**
> — `triggers/README.md`

簡言之：在 agent session 啟動時跑起來、session 結束時 cancel 的背景 `asyncio.Task`，**主動把訊息塞進 agent 對話**。

#### 3.4.2 核心 API（`triggers/triggers.py`）

```python
class TriggerContext:
    async def send(self, content: str) -> None:
        await self._connection.send_trigger_notification(content)

# Trigger 就是任何 async def 收 TriggerContext
Trigger = Callable[[TriggerContext], Awaitable[None]]

@trigger
async def my_trigger(ctx: TriggerContext) -> None:
    ...
```

**內建 helpers**（`triggers/helpers.py`）：

```python
every(interval_seconds, callback)        # 固定間隔
on_file_change(path, callback)           # watchfiles 監控 FS 變化（lazy import）
```

#### 3.4.3 Runtime 設計（`trigger_runner.py`）

- 每個 trigger 是獨立 `asyncio.create_task` —— **沒有 ordering 保證，trigger 之間不互相影響**
- 例外處理：non-Cancelled exception 一律 log 後吞掉，**不自動重啟** → 簡單但意味著 trigger 自己要 retry loop
- 用 async context manager (`async with TriggerRunner(...)`)，配合 `Agent` 的 `AsyncExitStack` 自動 teardown

範例（`examples/getting_started/triggers.py:80-133`）：

```python
async def _poll_queue_callback(ctx: TriggerContext) -> None:
    if not _standby_active:
        return
    if new_ticket_arrived():
        await ctx.send("[SYSTEM ALERT] New critical ticket assigned: b/98765 ...")

config = LocalAgentConfig(
    triggers=[every(1, _poll_queue_callback)],
)
async with Agent(config) as agent:
    await agent.chat("Standby and notify me of critical tickets.")
    await asyncio.sleep(5)   # 期間 trigger 在背景跑
    await agent.chat("Anything come in?")  # agent 已看到 trigger 注入的訊息
```

#### 3.4.4 為什麼這對 anila-agent 是新能力

anila-agent 目前是純「user prompt → response」迴圈，沒有：
- ✗ 任何「無 user prompt 也能 push 訊息給 agent」的機制
- ✗ 監控外部訊號（檔案變化 / cron / webhook）的內建抽象
- ✗ Job 結束後自動通知 agent 的 channel

但 ANILA 平台確實有這個需求（從 MEMORY 看得到 studio job、ComfyUI pipeline、Triton 模型載入）—— 如果 sub-agent 能在「ComfyUI 任務完成」「Triton 模型 ready」「DB 新增 KB document」這類事件後自動續推一輪，能解鎖很多原本要靠 user 手動戳的 workflow。

#### 3.4.5 設計亮點

1. **Lazy import** (`helpers.py:101-108`)：`watchfiles` 只在實際用 `on_file_change` 時才 import，不污染 SDK 啟動。
2. **明確不重啟** (`trigger_runner.py:117-139`)：例外吞掉而非重啟，避免 trigger bug 把 session 拖死或無限重跑。註解 _"no auto-restart, no impact on other triggers or the session"_。
3. **與 hooks 互補的設計分工**（`triggers/README.md` 表格）：
   - hook = inline、blocking、反應 agent lifecycle、可以修改/阻擋
   - trigger = background、async、反應外部事件、只能 send message
4. **`send_trigger_notification`**（`connection.py:191-197`）是 Connection ABC 的 abstract method —— 表示這是「跨 backend 的核心動作」，不是 Local-only。

#### 3.4.6 結論

**借鑑優先 P1**：在 `anila_agent/triggers/` 新建 module，原樣 port `Trigger` / `TriggerContext` / `TriggerRunner` / `every` / `on_file_change`。

實作要點：
1. `TriggerContext.send()` 在 anila-agent 怎麼接？
   - 短期：直接 append 到 `short_term` Session（openai-agents `Session.add_messages([{"role": "system", "content": "[TRIGGER] ..."}])`），下一次 `runner.send()` 時自然帶進去
   - 長期：擴充 `AnilaRunner` 加 `inject_message()` API，立即 trigger 一輪 LLM call（如果 idle）
2. 起停：把 `TriggerRunner` 也納入 `AnilaRunner` 的 `AsyncExitStack`
3. anila-agent 場景下最有價值的 trigger：
   - `on_db_change` — pgvector 表新增 row（趁機讓 agent 重新規劃）
   - `on_job_complete` — ComfyUI / Triton job 完成
   - `every(interval, healthcheck)` — 對 retriever / model server 做存活檢查

工作量 3-5 天。port 本身 2 天，整合 + test 2-3 天。

---

### 3.5 Connections — 多 backend 的擴展點

#### 3.5.1 Antigravity 怎麼做（`connections/connection.py`）

`Connection` / `ConnectionStrategy` / `AgentConfig` 三個 ABC，**完全與 backend 解耦**：

```python
class Connection(abc.ABC):
    @abstractmethod async def send(self, prompt, **kwargs) -> None: ...
    @abstractmethod def receive_steps(self) -> AsyncIterator[Step]: ...
    @abstractmethod async def send_trigger_notification(self, content: str) -> None: ...
    async def cancel(self) -> None: ...     # default no-op
    async def disconnect(self) -> None: ...
    async def signal_idle(self) -> None: ...
    async def wait_for_idle(self) -> None: ...
    async def wait_for_wakeup(self, timeout=300) -> bool: ...
    async def send_tool_results(self, results) -> None: ...
    async def delete(self) -> None: ...
    @property is_idle / conversation_id
```

`AgentConfig.create_strategy(tool_runner, hook_runner)` 是 ABC method —— 加新 backend 只要：
1. 寫一個 `MyConnection` 繼承 `Connection`
2. 寫一個 `MyConnectionStrategy` 繼承 `ConnectionStrategy`
3. 寫一個 `MyAgentConfig` 繼承 `AgentConfig` + 實作 `create_strategy()`

`Agent` 本體不用動 —— `agent.py:151-154` 只呼叫 `self._config.create_strategy(...)`。

唯一具體實作 `LocalConnection`（61KB）透過 WebSocket + protobuf 跟 Go binary 溝通；但 SDK 文件 (`connections/README.md`) 明寫 _"adding a new connection strategy... a remote connection strategy would go in connections/remote_connection/"_——預留擴充清楚。

#### 3.5.2 anila-agent 怎麼做

無對應抽象。直接呼叫 `agents.Runner.run(starting_agent=..., input=..., hooks=..., session=...)`（`anila_agent/core/runner.py:86-92`）—— 完全綁死在 `openai-agents` SDK。

#### 3.5.3 差異與評比

| 維度 | Antigravity | anila-agent |
|---|---|---|
| Backend 抽象 | 三層 ABC，加新 backend 不動 Agent | 直接呼叫 `openai-agents` Runner |
| 多 backend 場景 | 已預留 LocalConnection vs（未來）RemoteConnection | 換 backend = 重寫 runner.py |
| ABC method 廣度 | 11 個方法（含 idle / wakeup / cancel / delete / tool_results / trigger） | N/A |

**評估**：Antigravity 的多 backend 設計**對 anila-agent 短期沒緊迫**——我們是 on-prem template，backend 大概率永遠是 vLLM + openai-compatible。但**對 ANILA 整體平台有戰略價值**：未來如果想讓 sub-agent 也能跑在 Triton / 直接接 raw HTTP / 接 ANILA 自家 controller，這個抽象就是擋火牆。

#### 3.5.4 結論

**借鑑優先 P1（戰略性）**：

把 `core/runner.py` 重構成「`AnilaRuntime` ABC + `OpenAIAgentsRuntime` 具體實作」。

短期 deliverable（1-2 天）：
- 只抽出 ABC，把 `AnilaRunner.send()` 拆成 `_runtime.run(...)` + outer wiring
- 留一個唯一 `OpenAIAgentsRuntime` 實作

中期（1 週）：增加第二個 `RawHttpRuntime`（直接打 OpenAI compatible endpoint，不依賴 `openai-agents`），用來在 ANILA 內某些不能引入 openai-agents 依賴的場景跑。

**注意**：Antigravity 的 `Connection` 介面有兩個我們暫時不需要的方法：
- `wait_for_wakeup(timeout)`：給 trigger / async 場景用，要連 trigger 一起 port 才有意義
- `send_tool_results(results)`：因為它的 tool 是「Connection 收到 model 的 ToolCall → Python 端執行 → 結果回 send_tool_results」這條路；anila-agent / openai-agents 是 in-process 同步呼叫，不需要

---

### 3.6 Conversation — 帶歷史的 session wrapper

#### 3.6.1 Antigravity 怎麼做（`conversation/conversation.py`）

`Conversation` 在 `Connection` 之上加：

```python
self._steps: list[Step]                # 全 trajectory
self._turn_start_indices: list[int]    # 每個 user prompt 在 history 裡的位置
self._compaction_indices: list[int]    # 上下文窗收斂事件位置
self._cumulative_usage: UsageMetadata  # token 計帳
self._turn_usage: UsageMetadata | None # 本回合的 token
self._max_history_size: int            # 預設 10_000，超過 trim 舊的並調整 indices
```

API：
- `chat(prompt) -> ChatResponse` — 高階一句搞定
- `send(prompt)` + `receive_steps()` — 低階分離控制
- `receive_chunks()` — 把 step 拆解成 `Thought` / `Text` / `ToolCall` 三類 stream chunk（給 UI 用）
- 屬性：`history` / `last_response` / `turn_count` / `compaction_indices` / `total_usage` / `last_turn_usage` / `is_idle` / `conversation_id`

`ChatResponse`（`types.py:763+`）也很有意思 —— **同一個 stream 可以有多個 cursor 並行消費**（`response.thoughts` / `response.tool_calls` / `async for chunk in response`），共用一個內部 buffer。

#### 3.6.2 anila-agent 怎麼做

- 歷史 = openai-agents 的 `Session`（`agents.memory.session.Session`），背後是 SQLite (`.anila/sessions/anila.db`)
- 沒有 turn index / compaction index 概念
- Token 統計沒有
- Stream 沒有（`Runner.run()` 是 await 一次拿完整結果）—— 雖然 openai-agents 有 `Runner.run_streamed()` 但目前 anila-agent 沒用

#### 3.6.3 差異與評比

| 維度 | Antigravity | anila-agent |
|---|---|---|
| 歷史儲存 | 純記憶體 list + max size trim | SQLite (持久化) |
| Turn tracking | 有 `turn_start_indices` | 隱含在 SQLite 的 turn_id 欄位 |
| Compaction tracking | 顯式記錄 `compaction_indices` | 沒有對應概念 |
| Token usage | 累計 + per-turn | 沒有 |
| Streaming | 多 cursor（chunks/thoughts/tool_calls） | 沒有 |
| `chat()` vs `send()/receive_steps()` 分離 | 有 | 沒有 |

#### 3.6.4 結論

**借鑑優先 P2**：anila-agent 用 openai-agents `Session` 已經夠用，**但 token usage + streaming 兩個能力很值得加**。

具體建議：
1. **加 token usage**（0.5d）：在 `AnilaRunner` 加 `self._cumulative_usage`，從 `result.raw_responses` 取 usage 累計。
2. **加 streaming 介面**（2d）：包一個 `AnilaChatResponse` wrapper，底層用 `Runner.run_streamed()`。供 CLI 顯示 token、API server 走 SSE。
3. **Compaction index 不抄**：openai-agents 沒有對應事件，要抄等於要實作整個 context compaction 機制，過度設計。

---

### 3.7 Tools — `ToolContext` auto-injection 是亮點

#### 3.7.1 Antigravity 怎麼做

`ToolRunner`（`tools/tool_runner.py`）核心特性：

1. **註冊時做 signature 解析**（`tool_runner.py:41-70`）：
   ```python
   def _find_context_param(fn) -> str | None:
       hints = typing.get_type_hints(target)
       for name, ann in hints.items():
           if ann is ToolContext: return name
           if typing.get_origin(ann) is Union and ToolContext in typing.get_args(ann):
               return name  # 處理 Optional[ToolContext]
       return None
   ```
   每個 tool 註冊時就決定「這個函式要不要塞 ToolContext」，存在 `self._context_params` dict 裡。

2. **執行時自動注入**（`tool_runner.py:232-274`）：
   ```python
   def _inject_context(self, tool_name, kwargs):
       ctx_param = self._context_params.get(tool_name)
       if ctx_param is not None and self._context is not None:
           if ctx_param not in kwargs:
               return {**kwargs, ctx_param: self._context}
       return kwargs
   ```

3. **schema 生成時隱藏 ToolContext 參數**（`tool_runner.py:73-99`）：給 LLM 看的 schema 不會包含 ctx_param，避免 LLM 嘗試填它。

4. **`ToolContext` 提供的能力**（`tools/tool_context.py`）：
   ```python
   ctx.conversation_id           # 識別
   ctx.is_idle                   # connection 狀態
   await ctx.send(message)       # 把訊息塞進對話（同 trigger）
   ctx.get_state(key) / ctx.set_state(key, value)  # per-conversation KV store
   ```

5. **sync tool 自動跑在 thread**（`tool_runner.py:221-230`）：
   ```python
   async def _execute_fn(self, fn, **kwargs):
       if not _is_async(fn):
           result = await asyncio.to_thread(fn, **kwargs)
       else:
           result = fn(**kwargs)
       if asyncio.iscoroutine(result): return await result
       return result
   ```

6. **batch tool calls 並行執行**（`tool_runner.py:276-315`）：`asyncio.gather` 一起跑、結果按順序回傳；每個 tool 自己 catch exception 避免 sibling cancel。

#### 3.7.2 anila-agent 怎麼做

`tools/registry.py` + `tools/base.py`：
- 簡單 dict `{tool_name: FunctionTool}`，靠 openai-agents 的 `function_tool` decorator 生成 schema
- 沒有 ToolContext / 沒有 per-conversation KV store
- 並行：靠 openai-agents 內部處理

#### 3.7.3 差異與評比

| 維度 | Antigravity | anila-agent |
|---|---|---|
| ToolContext | 有，靠型別自動注入 + schema 隱藏 | 沒有 |
| Per-conversation 狀態 | `ctx.get_state/set_state` | 沒有，要靠閉包 / global |
| Tool 主動發訊息給 agent | `await ctx.send(...)` | 沒有 |
| Sync/async 統一處理 | `asyncio.to_thread` + coroutine 檢查 | `openai-agents` 處理 |
| 並行 batch | `asyncio.gather` + per-tool exception isolation | `openai-agents` 處理 |
| Tool metadata | 只有 BuiltinTools enum 分類（read_only/file_tools/...） | `ToolMetadata(is_read_only/is_destructive/requires_confirmation/category)` 較細 |

#### 3.7.4 結論

**借鑑優先 P1**：

1. **`ToolContext` + auto-injection**（1-2d）：
   - 新建 `anila_agent/tools/context.py` 定義 `AnilaToolContext`
   - 改 `tools/base.py:anila_tool` 在 build 階段檢測 ToolContext 參數
   - 改 `function_tool` 包裝以隱藏 ctx_param —— **這部分要研究 openai-agents `FunctionTool.params_json_schema` 怎麼處理**，可能不是直接搬
   - 配 `AnilaRunHooks` 在每個 turn 開始時把 ctx 塞進 RunContextWrapper

2. **Per-conversation state**：用 openai-agents 的 `RunContextWrapper.context` 機制（已內建），不用自己重做

3. **`ctx.send()` 主動發訊息**：與 `Trigger` 共用同一個底層機制（注入到 Session）

4. **tool metadata 不變**：anila-agent 的 `ToolMetadata` 比 Antigravity 的 `BuiltinTools` 列舉表達力更強，**這塊我們贏，保留**

---

### 3.8 MCP — 三種 transport vs 我們的單一管道

#### 3.8.1 Antigravity 怎麼做（`mcp/bridge.py`）

```python
class McpBridge:
    async def connect(self, server_cfg: types.McpServerConfig):
        if server_cfg.type == "stdio":  await self.connect_stdio(...)
        elif server_cfg.type == "sse":  await self.connect_sse(...)
        elif server_cfg.type == "http": await self.connect_streamable_http(...)

    async def connect_stdio(self, command, args): ...
    async def connect_sse(self, url, headers=None): ...
    async def connect_streamable_http(self, url, headers, timeout=30, sse_read_timeout=300, terminate_on_close=True): ...

    @property tools -> list[ToolWithSchema]
```

關鍵設計：
- 用官方 `mcp.client.session_group.ClientSessionGroup` 管多 server
- `connect_*` 把抓到的 tools 包成 `ToolWithSchema`（內含 input JSON schema + 一個會 `session_group.call_tool(name, kwargs)` 的 wrapper function）
- `bridge.stop()` 統一 teardown

`McpServerConfig` 是 union type（`types.py`）：
- `McpStdioServer(command, type="stdio", args)`
- `McpSseServer(url, type="sse", headers)`
- `McpStreamableHttpServer(url, type="http", headers, timeout, sse_read_timeout, terminate_on_close)`

#### 3.8.2 anila-agent 現在怎麼做

`anila_agent/utils/config.py:ToolsConfig.mcp_servers: list[dict]`——但**通路：在 codebase 裡完全沒被消費**。`configs/tools.yaml` 解析出來後沒人讀（沒有對應的 bridge 或 tool registry 邏輯）。等於 MCP 整塊是 placeholder。

#### 3.8.3 差異與評比

Antigravity 完勝 —— anila-agent 還沒實作。

#### 3.8.4 結論

**借鑑優先 P1**：

直接把 `mcp/bridge.py`（170 行）整支搬過來，調整：
1. import path：`from google.antigravity.tools.tool_runner import ToolWithSchema` 改成 anila-agent 對應的 `FunctionTool` builder
2. tool 註冊路徑：原碼是 _"Tools from the MCP server are now registered in tool_runner"_（其實它是回傳 `list[ToolWithSchema]` 讓 caller 自己塞），我們可以直接 push 進 `ToolRegistry`
3. 配 `AppConfig.tools.mcp_servers` 的 YAML schema：
   ```yaml
   mcp_servers:
     - type: stdio
       command: npx
       args: ["my-mcp-server"]
     - type: sse
       url: https://anila-mcp.local/sse
       headers:
         Authorization: Bearer ${ANILA_MCP_TOKEN}
   ```

工作量 1-2 天。

---

### 3.9 Skills — 跟 anila-agent 的 slash command 是兩件事

#### 3.9.1 Antigravity 怎麼做

`skills_paths: list[str]` 在 `LocalAgentConfig.skills_paths`（`local_connection_config.py:60`）—— 一條 path 是一個目錄，目錄結構：

```
skills/google-antigravity-sdk/
├── SKILL.md         # 主要說明 + 觸發條件
├── examples/        # 示例程式
└── references/      # 參考資料
```

關鍵設計：**skill 不是 SDK 邏輯，是「丟給 LocalHarness (Go binary) 的 path」**——SDK Python 層完全不解析 skill 內容，只是把路徑傳下去（`local_connection.py:1365, 1384, 1514`）。

Go binary 收到後做什麼從 Python source 看不出來，但從 `examples/getting_started/agent_skills.py` 推測：harness 讀 SKILL.md，根據觸發條件決定要不要把 skill 內容塞進 system prompt。

#### 3.9.2 anila-agent 怎麼做

`anila_agent/cli/commands.py` 的 slash command 系統：
- `/help`、`/exit`、`/clear`、`/model`、`/memory list/scan/extract`、`/cost`
- Command kind = `local`（不打 LLM）/ `prompt`（生成內容送進下一輪 user prompt）
- 從 `claude-code-src commands.ts` port 過來

**這跟 Antigravity skills 是兩件事**：
- anila-agent slash command = REPL UX，user 在互動介面打的指令
- Antigravity skills = system instruction extension，agent 自己看的「能力說明書」

倒是 anila-agent 的 `prompts/system.md` + `prompts/agent.md` 文件**比較接近 Antigravity skill 概念**——但目前是固定一份，沒有「動態載入多個」的機制。

#### 3.9.3 結論

**借鑑優先 P2 並且部分**：

Antigravity 的 skill 載入機制太依賴 Go binary，不適合直接搬。但可以借鑑「可掛載多份能力說明書」的概念：

1. 短期（0.5d）：在 `AgentConfig` 加 `skills_paths: list[Path]`，啟動時讀每個目錄的 SKILL.md，**append 進 `instructions` 字串**（類似 `core/agent.py:127-131` 已經做的 MEMORY.md 注入）
2. 中期：給 skill 一個 frontmatter schema（`name` / `description` / `triggers` / `keywords`），上 turn pre-check 時用簡單 keyword overlap 決定要不要動態 enable
3. 不要做：複製整套 Go binary 內藏的「skill 自動選擇」演算法 — 那需要另一支模型

---

### 3.10 Utils / Interactive — REPL pattern

#### 3.10.1 對照

| 維度 | Antigravity (`utils/interactive.py`) | anila-agent (`cli/app.py` + `cli/commands.py`) |
|---|---|---|
| 風格 | 簡潔，~290 行單檔 | 拆 3 檔，較工程化 |
| 輸入 | 自製 `async_input`（thread-based、handle cancellation） | `prompt_toolkit.PromptSession` + `FileHistory` |
| 輸出 | print | `rich` renderer |
| Slash command | 沒有 | 完整 dispatch |
| Tool confirm hook | `ToolConfirmationHook` (DecideHook) | YAML 設定 hook |
| Question hook | `AskQuestionHook` (TransformHook) | 沒有對應 |

**anila-agent 完勝**：UX 完整度、可擴充性都比 Antigravity 強。Antigravity 那 290 行只是 demo 等級。

#### 3.10.2 唯一值得借鑑的小東西

`async_input`（`interactive.py:48-77`）—— 用 daemon thread + `loop.create_future()` 做的 cancellable async input，比 `asyncio.to_thread(input)` 乾淨（後者在 loop teardown 時會 hang）。

```python
async def async_input(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def _read_input():
        try:
            result = input(prompt)
            if not future.cancelled():
                loop.call_soon_threadsafe(future.set_result, result)
        except BaseException as e:
            if not future.cancelled():
                loop.call_soon_threadsafe(future.set_exception, e)
    thread = threading.Thread(target=_read_input, daemon=True)
    thread.start()
    return await future
```

**借鑑優先 P2，工作量 0.5d**：如果 anila-agent CLI 需要在背景 trigger 跑著時讀 user input、又要能被 SIGTERM 乾淨切掉，這 trick 可以解決 `prompt_toolkit` 在某些 terminal 下的 hang 問題。**目前不急**。

---

## 4. 可借鑑 pattern 詳列

> 對應 §3 的結論，本節整理成可直接派工的 ticket。

### P0 — 立刻納入下一輪 sprint

#### P0-1：三類強型別 hook 系統

| 項 | 內容 |
|---|---|
| 來源 | `hooks/hooks.py:95-238`（ABC + concrete subclasses） |
| 進 anila-agent | 新建 `anila_agent/core/hook_types.py`（ABC） + 重構 `core/hooks.py`（保留 YAML registration） |
| 為何有價值 | mypy / IDE 直接知道 hook 拿到什麼 payload；TOCTOU 順序由型別保證 |
| 整合做法 | 新增 ABC，舊 `HookSpec` 標 deprecated 但保留，新版 hook 直接用 `class MyHook(PreToolCallDecideHook)` |
| 工作量 | **1-2d** |
| 風險 | 既有 hook callback 需要 wrap 一層 adapter，但向後相容可保 |

#### P0-2：Hook decorator factory

| 項 | 內容 |
|---|---|
| 來源 | `hooks/hooks.py:244-288`（`_make_hook_decorator`） |
| 進 anila-agent | `core/hook_types.py` 一起放，提供 `@pre_tool_use`、`@post_tool_use`、`@on_session_start` 等 decorator |
| 為何有價值 | 讓 sub-agent template user 寫 hook 不必背 ABC 樣板 |
| 工作量 | **0.5d**（搬整段 + 改 hook 類別 import 即可） |

#### P0-3：Policy DSL

| 項 | 內容 |
|---|---|
| 來源 | `hooks/policy.py`（19KB） |
| 進 anila-agent | 新建 `anila_agent/core/policy.py`，註冊到 `PRE_TOOL_USE` event 的特殊 hook |
| 為何有價值 | 工具控管從「寫 callback」變「列宣告」；安全預設大幅提升；運維可讀懂 policy |
| 整合做法 | (1) `allow/deny/ask_user` builder + `enforce()` factory 原樣搬；(2) `_PolicyDecideHook.run()` 改成從 `tool_call.args` 或 anila-agent 的 PreToolUseInput 取 args；(3) `confirm_run_command` 在 anila-agent 對應應為 `confirm_destructive_tools()`，掃 ToolMetadata 找 `is_destructive=True` 的 tool |
| 工作量 | **2-3d**（含 policy_test.py port） |
| 風險 | 跟 P0-1 的 hook 系統重構有依賴關係——建議 P0-1 / P0-2 / P0-3 一起做 |

#### P0-4：`workspace_only` 路徑安全

| 項 | 內容 |
|---|---|
| 來源 | `hooks/policy.py:282-376`（`_secure_normalize_path` + `is_path_in_workspace` + `workspace_only`） |
| 進 anila-agent | 跟 P0-3 一起放在 `core/policy.py` |
| 為何有價值 | sub-agent 上線到客戶環境後，**file_tools 預設要鎖在 workspace** 是基本盤；目前 anila-agent 沒有 |
| 工作量 | **1d**（搬 + 配合 anila-agent file_tools 名單調整 + 加 test case 含 symlink） |
| 注意 | `_is_case_insensitive` 用 `path.samefile(parent / swapped_name)` 偵測（`policy.py:293-321`），這個 trick 處理 macOS/Windows 大小寫不敏感的問題很漂亮，原樣保留 |

---

### P1 — 中期重構排程（下下個 sprint）

#### P1-1：HookContext 三層 scope

| 項 | 內容 |
|---|---|
| 來源 | `hooks/hooks.py:34-86`（SessionContext / TurnContext / OperationContext） |
| 進 anila-agent | `core/hook_types.py` 補上 |
| 為何有價值 | hook 之間共享狀態（correlation_id / per-turn 統計 / per-call audit）有正規路徑；不必靠 global |
| 工作量 | **1d** |
| 注意 | 與 P0-1 hook 重構一起做更省工 |

#### P1-2：Triggers 子系統

| 項 | 內容 |
|---|---|
| 來源 | `triggers/triggers.py`、`trigger_runner.py`、`helpers.py` |
| 進 anila-agent | 新建 `anila_agent/triggers/` 整個 module |
| 為何有價值 | 解鎖「ComfyUI 任務完成自動通知 sub-agent」「pgvector 新文件後重算規劃」這類能力 |
| 整合做法 | (1) `TriggerContext.send()` 短期接到 `session.add_messages()`；(2) `TriggerRunner` 納入 `AnilaRunner.AsyncExitStack`；(3) `every()` / `on_file_change()` 兩個 helper 原樣搬 |
| 工作量 | **3-5d** |
| 注意 | `watchfiles` lazy import 不要破壞掉（`helpers.py:101-108`），保持 import 時不依賴 |

#### P1-3：ConnectionStrategy 多 backend 抽象

| 項 | 內容 |
|---|---|
| 來源 | `connections/connection.py`（ABC 部分） |
| 進 anila-agent | 重構 `core/runner.py` 為 `AnilaRuntime` ABC + `OpenAIAgentsRuntime` 實作 |
| 為何有價值 | 將來 anila-agent 可選擇不同 backend（openai-agents / raw HTTP / Triton 直連）而不動 sub-agent 程式 |
| 整合做法 | (1) ABC 抽出 `run(input, hooks, session, max_turns) -> RunSummary`；(2) `OpenAIAgentsRuntime` 把現在的 `Runner.run(...)` 包進去；(3) `AgentConfig` 加 `runtime: Literal["openai_agents", ...]` 欄位 |
| 工作量 | **1-2d**（單一 backend 抽 ABC）；**1w+**（加第二個 backend） |

#### P1-4：ToolContext + auto-injection

| 項 | 內容 |
|---|---|
| 來源 | `tools/tool_context.py`、`tools/tool_runner.py:41-99`（signature 解析 + 注入） |
| 進 anila-agent | 新建 `anila_agent/tools/context.py` + 改 `tools/base.py` |
| 為何有價值 | tool 內部可以 (1) 知道自己在哪個 conversation (2) 存 state 跨 invocation (3) 主動 push 訊息 |
| 整合做法 | 詳見 §3.7.4 |
| 工作量 | **1-2d** |
| 風險 | 跟 openai-agents `FunctionTool.params_json_schema` 的互動需要查 ——可能要在 `function_tool(**kwargs)` 那層動 |

#### P1-5：McpBridge

| 項 | 內容 |
|---|---|
| 來源 | `mcp/bridge.py`（170 行） |
| 進 anila-agent | 新建 `anila_agent/mcp/bridge.py`，把 `ToolsConfig.mcp_servers` 接起來 |
| 為何有價值 | anila-agent 目前 MCP 整塊是 placeholder；接好後可以接 anila-cli MCP / Atlassian MCP / 其他 |
| 整合做法 | 詳見 §3.8.4 |
| 工作量 | **1-2d** |

---

### P2 — 評估後可考慮

#### P2-1：disable vs deny 二維工具控管

| 項 | 內容 |
|---|---|
| 來源 | `types.py:311-365`（`CapabilitiesConfig`）+ `tools/README.md`（決策矩陣） |
| 進 anila-agent | `AgentConfig.tools` 增加 `enabled_tools` / `disabled_tools` 兩個欄位 |
| 為何有價值 | `disabled_tools` 從 model context 移除 → 省 token；`deny` 是 runtime 拒絕 → 模型仍可看見 |
| 工作量 | **0.5d**（純 flag + 文件） |
| 注意 | 跟 ToolMetadata 整合：`disabled_tools` 把整個 tool 從 `all_tools` 列表移除前，先檢查 ToolMetadata 是否標記 `requires_confirmation` |

#### P2-2：Conversation 層 / Token usage / Streaming

| 項 | 內容 |
|---|---|
| 來源 | `conversation/conversation.py:36-373`、`types.py:763-973`（`ChatResponse`） |
| 進 anila-agent | 擴充 `AnilaRunner`，加 `total_usage` / `last_turn_usage` / `stream()` 方法 |
| 為何有價值 | API server / CLI 可顯示 token 耗用；長對話 streaming UI |
| 工作量 | **2-3d** |
| 注意 | openai-agents 的 `Runner.run_streamed()` 接口跟 Antigravity `ChatResponse` 不一樣，要包一層 adapter |

#### P2-3：`async_input` cancellable input

| 項 | 內容 |
|---|---|
| 來源 | `utils/interactive.py:48-77` |
| 進 anila-agent | `cli/app.py` 在 SIGTERM teardown 有 hang 時可考慮 |
| 工作量 | **0.5d** |
| 注意 | 目前 anila-agent CLI 沒回報這個問題，不急 |

---

## 5. 不該抄的部分（會跟 ANILA 整體架構衝突）

### 5.1 LocalConnection / localharness Go binary

- **問題**：Antigravity 把 agent loop 整個丟給 Go binary，透過 WebSocket + protobuf 溝通。這對 ANILA 是雙重不合：
  1. ANILA 是 on-prem，不能假設客戶能拿 Google 簽過的 binary
  2. ANILA 用 vLLM + openai-compatible 不是 Gemini
- **結論**：完全不抄。anila-agent 的 runtime 就是 openai-agents（或未來自家 RawHttpRuntime），不需要分離 binary。

### 5.2 GeminiConfig / ModelConfig / `gemini-3.1-flash-image-preview` 預設

- **問題**：`types.py:135-168` 整套設定鎖 Gemini，連 image model 都預設 `gemini-3.1-flash-image-preview`（`types.py:355`）
- **結論**：完全不抄。anila-agent 的 model 設定是 `ModelConfig(model, base_url, api_key, settings)`，對 vLLM endpoint 是適配的。如果 anila-agent 要支援圖像生成，會走 ANILA 平台自家的 ComfyUI / Triton pipeline，不是 Gemini。

### 5.3 `localharness_pb2.py` + WebSocket transport

- **問題**：25KB 的 protobuf bindings + WebSocket framing 都是 Go binary 專屬，沒有對應的 ANILA 場景。
- **結論**：完全不抄。

### 5.4 Antigravity 的 `BuiltinTools` enum 模型

- **問題**：`types.py:212-308` 把 11 個 built-in tool 名稱（`run_command` / `view_file` / `edit_file` / `find_file` / `list_directory` / `search_directory` / `create_file` / `ask_question` / `start_subagent` / `generate_image` / `finish`）寫死在 enum。這對應 Go binary 的內建 tool，不是 SDK 自己的。
- **結論**：anila-agent 沒有「Go binary 內建工具」這層，built-in tools 全部走 Python（`anila_agent/tools/*`）。**不抄 enum**，但**借鑑「給工具集打 label 方便寫 policy」的概念**（已經有 ToolMetadata.category，更靈活）。

### 5.5 `StreamableHttp` MCP 特定的 timeout/sse_read_timeout 細節

- **問題**：`bridge.py:115-139` 對 `terminate_on_close` 等 flag 是針對 Google 自家後端的細節。
- **結論**：基本 API 抄，這些 fine-grained 參數**先用 sensible defaults，等實際接到 MCP server 出問題再調**。

### 5.6 `compaction_threshold` 與 `OnCompactionHook`

- **問題**：`types.py:344-345` + `hooks/hooks.py:230-238`，這是 Go binary 自己的 context window 收斂機制，SDK 只是觀察 hook。
- **結論**：anila-agent 目前 max_turns 配 openai-agents Session 就夠，**不抄 compaction**。如果未來 LLM context 限制成為痛點，那時可以考慮自己實作（但會是另一個 ticket）。

---

## 6. 特別關注：Triggers + Connections 對 anila-agent 的戰略意義

### 6.1 Triggers — 從「對話式 agent」到「always-on agent」

**現在的 anila-agent**：

```
user prompt ── send() ──> Runner.run() ──> response ──> wait for next prompt
```

每一輪都是同步 request/response，user 不主動 prompt 就不會有任何動作。

**加 trigger 之後**：

```
                          ┌── on_db_change ── 新 KB doc 進來 ──┐
                          ├── on_job_complete ── ComfyUI 完成 ──┤
agent session active ──> agent loop ←─ ctx.send(...) ───────────┤
                          ├── every(60) ── 健康檢查 ────────────┤
                          └── on_file_change ── 設定檔改動 ─────┘
```

Sub-agent 變成「在 session 期間隨時可被外部事件喚醒」。

**ANILA 平台場景三個具體 use case**：

1. **studio job 完成自動續推**
   user 跟 sub-agent 說「等 ComfyUI job 跑完幫我把結果整理進 PRD」→ trigger 監控 job status DB → 完成後 `ctx.send("Job xxx finished. Results at /path/to/output.json. Continue.")` → sub-agent 自動跑下一輪 LLM 規劃。
   **省的 user 動作**：定期回頭問「好了沒？」。

2. **KB 文件改動自動 reindex 通知**
   trigger 監控 pgvector embedding 表 → 偵測到 ANILA collection 新增/更新 doc → 通知 sub-agent「retriever 已有新內容，下次 query 會涵蓋」。

3. **vLLM / Triton 健康檢查**
   `every(30, healthcheck)` → 偵測到後端模型 server 異常 → sub-agent 知道後續 LLM call 會失敗，提前向 user 報告或切到 fallback。

### 6.2 Connections — 從「綁 openai-agents」到「ANILA 自家 runtime 也可」

ANILA 平台未來可能的 sub-agent backend：

| Backend | 適用場景 | 目前可行性 |
|---|---|---|
| openai-agents on vLLM | 預設、最成熟 | ✅ 現在用的 |
| Raw HTTP on vLLM | 不能引 openai-agents 依賴的場景（例如 dependency conflict） | 要靠 ConnectionStrategy 抽象才好做 |
| Triton agentic backbone | 如果 ANILA 自家做了 agent loop in Triton | 戰略可能性，尚未實作 |
| Google Gemini (sub-agent in cloud test mode) | 只在 dev mode、user 自己接的場景 | 暫不規劃 |

**沒有 ConnectionStrategy 抽象**，每加一種 backend 都要重寫 `core/runner.py` 大半邏輯（hook 派發、session 注入、max_turns 處理）。**有抽象之後**只要寫一個新 `*Runtime` 實作，最多 200-400 行，hook / policy / trigger / MCP 全部維持不動。

### 6.3 兩者聯動

最有戰略意義的場景是 **trigger × connection 都到位之後**：

```python
# 例：ANILA studio sub-agent 配置（未來）
config = AnilaAgentConfig(
    runtime="openai_agents",   # 或 "raw_http"
    model=ModelConfig(base_url="http://vllm:8000/v1", model="anila-gemma4"),
    triggers=[
        every(60, healthcheck_vllm),
        on_db_change("studio_jobs", on_job_finished),
        on_file_change("/data/comfyui/output", on_image_ready),
    ],
    policies=[
        deny_all(),
        allow_all_except("run_command"),  # 通常不需要 shell
        workspace_only(["/workspace", "/data/anila"]),
    ],
    mcp_servers=[
        McpStdioServer(command="anila-mcp-cli", args=["studio"]),
    ],
)
async with AnilaAgent(config) as agent:
    await agent.chat("Plan and execute the upload + indexing flow.")
```

—— 這就是 anila-agent 從「對話模板」走向「自動化 sub-agent 平台」的關鍵升級路徑。

---

## 7. 整體工作量估計與排程建議

| Sprint | Tickets | 工作量 | 累計 |
|---|---|---|---|
| Sprint A (1 週) | P0-1 + P0-2 + P0-4 | 1.5-2.5d + 0.5d + 1d = **3-4d** | hook 系統 + workspace policy |
| Sprint B (1 週) | P0-3 + 整合測試 | 2-3d + 2d test 補強 = **4-5d** | Policy DSL 完整上線 |
| Sprint C (1.5 週) | P1-1 + P1-4 + P1-5 | 1d + 1-2d + 1-2d = **3-5d** | HookContext / ToolContext / MCP |
| Sprint D (1.5 週) | P1-2 | 3-5d + test = **5-7d** | Triggers 子系統 |
| Sprint E (1-2 週) | P1-3 | 1-2d ABC + 5d 第二 backend | ConnectionStrategy + Raw HTTP runtime |
| 後續 | P2 系列 | 4-6d 合計 | 可選擴充 |

**Critical Path**：P0-1 → P0-3（hook 系統重構為 P0-3 policy 的依賴）→ Sprint C/D 可平行。

---

## 8. 附錄 A：原始碼快速索引

### Antigravity SDK 重要檔案 + 行號

| 概念 | 檔案 : 行號 | 大小 |
|---|---|---|
| `Agent.__aenter__` lifecycle | `agent.py:92-182` | — |
| `_make_hook_decorator` factory | `hooks/hooks.py:244-275` | — |
| `_HOOK_TYPE_REGISTRY` 表驅動 | `hooks/hook_runner.py:27-37` | — |
| `HookRunner.dispatch_pre_tool_call` (TOCTOU 保證) | `hooks/hook_runner.py:181-204` | — |
| `Policy` dataclass | `hooks/policy.py:109-127` | — |
| Policy priority bucket index | `hooks/policy.py:381-409` | — |
| `_evaluate_predicate` (3 種 signature 自動判斷) | `hooks/policy.py:422-466` | — |
| `_secure_normalize_path` + `is_path_in_workspace` | `hooks/policy.py:282-347` | — |
| `confirm_run_command` 預設 | `hooks/policy.py:242-276` | — |
| `enforce()` factory | `hooks/policy.py:598-628` | — |
| `Trigger` type alias + `@trigger` decorator | `triggers/triggers.py:53-78` | — |
| `every()` / `on_file_change()` | `triggers/helpers.py:39-123` | — |
| `TriggerRunner._run_trigger`（吞例外、不重啟） | `triggers/trigger_runner.py:115-139` | — |
| `Connection` ABC（11 個方法） | `connections/connection.py:106-197` | — |
| `LocalConnectionStrategy.__init__` (skills_paths 傳遞) | `connections/local/local_connection.py:1356-1395` | — |
| `Conversation.receive_chunks` (多 cursor stream) | `conversation/conversation.py:160-196` | — |
| `ToolRunner._find_context_param` (型別偵測) | `tools/tool_runner.py:41-70` | — |
| `ToolRunner._inject_context` | `tools/tool_runner.py:232-251` | — |
| `ToolRunner.process_tool_calls` (batch 並行 + per-tool isolation) | `tools/tool_runner.py:276-315` | — |
| `McpBridge.connect` 三種 transport 分派 | `mcp/bridge.py:71-93` | — |
| `BuiltinTools.read_only()` 等 class methods | `types.py:241-308` | — |
| `CapabilitiesConfig.enabled_tools` / `disabled_tools` | `types.py:311-365` | — |
| `async_input` (cancellable stdin) | `utils/interactive.py:48-77` | — |

### anila-agent 對應檔案

| 概念 | 檔案 |
|---|---|
| Agent 組裝 | `anila_agent/core/agent.py` (150 行) |
| Hook system | `anila_agent/core/hooks.py` (358 行) |
| Runner | `anila_agent/core/runner.py` (111 行) |
| Tool registry + metadata | `anila_agent/tools/registry.py` + `anila_agent/tools/base.py` |
| Memory (long-term) | `anila_agent/memory/long_term.py` + `anila_agent/memory/store.py` |
| CLI REPL | `anila_agent/cli/app.py` + `anila_agent/cli/commands.py` |
| Config (YAML 4 檔) | `anila_agent/utils/config.py` |

---

## 9. 附錄 B：「同名概念兩家行為對照」速查表

| 概念 | Antigravity 名 | anila-agent 名 | 對等程度 |
|---|---|---|---|
| Agent | `Agent` | `AssembledAgent` + `AnilaRunner` | 70% |
| Hook 註冊 | `HookRunner.register_hook(hook_instance)` | `HookRegistry.register(spec)` | 50% (型別系統不同) |
| Tool 註冊 | `ToolRunner.register(callable)` | `ToolRegistry.add(FunctionTool)` | 80% |
| Tool metadata | `BuiltinTools` enum | `ToolMetadata` dataclass | 不同切法 |
| Tool exec | `ToolRunner.execute()` + auto-thread for sync | openai-agents `Runner` 內部 | 透明（unanila-agent 不可見） |
| Policy | `policy.allow/deny/ask_user + enforce()` | 沒有 | N/A |
| Session 歷史 | `Conversation.history` (in-mem) | openai-agents `Session` (SQLite) | 不同儲存策略 |
| Streaming | `ChatResponse.thoughts / tool_calls / async for` | 沒有 | N/A |
| MCP | `McpBridge` (stdio/SSE/HTTP) | placeholder in YAML | N/A |
| Trigger | `Trigger` + `TriggerRunner` | 沒有 | N/A |
| Backend 抽象 | `Connection` / `ConnectionStrategy` ABC | 沒有 (綁 openai-agents) | N/A |
| Memory (long-term) | 沒有 | `LongTermMemory` + `MemdirStore` (Markdown frontmatter) | anila-agent 多 |
| Slash command (CLI) | 沒有（只有 `exit`/`quit`） | 完整 `/help/exit/clear/model/memory/cost` | anila-agent 多 |
| Skill (能力說明書) | `skills_paths` (傳給 Go binary) | 沒有 (但 prompts/system.md 類似) | 設計不同 |

---

**Last updated**: 2026-05-26 · **By**: ANILA 平台分析 (antigravity-sdk-python deep dive subagent)

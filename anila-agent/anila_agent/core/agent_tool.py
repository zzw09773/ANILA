"""AgentTool — 把 sub-agent 包成一個 tool,讓 parent agent 可呼叫它做 sub-routine 派工。

本模組對應 enhancement roadmap P0-8 任務,主要參考兩個上游實作:

* claude-code-src `src/tools/AgentTool/AgentTool.tsx` — Claude Code 的 sub-agent
  dispatch 機制(parent 透過 ``Task`` tool 派工給子 agent,子 agent 跑完回結果)。
* openai-agents `Agent.as_tool()` — 把 Agent 物件包成一個 ``FunctionTool``,parent
  agent 可以呼叫它,執行完回到 parent 的 conversation 繼續推進。

設計重點(跟 ``Handoff`` 區別):

* AgentTool 是 **sub-routine 模式** — sub-agent 跑完一段,結果以 tool result 回給
  parent;parent agent 繼續持有控制權與 conversation,sub-agent 只是一次性的派工。
* ``Handoff`` 則是 **控制權轉移** — parent agent 結束、conversation history 轉到
  下一個 agent 接手,parent 不再對話。openai-agents `handoffs/` 套件處理那條路。

兩者剛好在 parent agent 的角度形成「呼叫 vs 棄場」對立。本 P0-8 只做 sub-routine
那條;handoff 走 P1 階段另開。

prefix_strategy 欄位(``"share"`` / ``"fork"``)在 P1-1 已實作真實 prompt-cache
prefix 行為:

* ``"share"``(預設):sub-agent 沿用 parent 整段 conversation 前綴,只在尾巴
  追加輕量 sub instruction。**最積極的 prompt cache 命中**,適合需要看 parent
  context 才知道接力做什麼的子任務(例如同一段研究的後續細項)。
* ``"fork"``:sub-agent 從 parent message 前 K 條(預設
  :data:`anila_agent.core.prompt_cache.DEFAULT_PREFIX_MESSAGE_COUNT`)切斷,
  尾巴接 fork boilerplate(``<fork-subagent-boilerplate>``)。前綴與 parent
  byte-identical → vLLM ``--enable-prefix-caching`` 命中;sub instruction
  不同 → child 內容差異化。

prefix bytes 採 deterministic JSON 序列化(``sort_keys=True`` + 固定
separators),確保同樣 parent messages → 同樣 bytes → 同樣 SHA-256 hash →
vLLM prefix cache 同一個 slot。詳見 :mod:`anila_agent.core.prompt_cache`。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agents import Agent, FunctionTool, Runner
from agents.tool_context import ToolContext

from anila_agent.core.context import AnilaToolContext
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import (
    HookRegistry,
    fire_subagent_dispatch_end,
    fire_subagent_dispatch_start,
)
from anila_agent.core.prompt_cache import (
    DEFAULT_PREFIX_MESSAGE_COUNT,
    Message,
    SubagentPrefix,
    build_subagent_prefix,
)
from anila_agent.tools.base import ToolMetadata
from anila_agent.tracing import Tracer

# ---------------------------------------------------------------------------
# 型別 alias
# ---------------------------------------------------------------------------

# prefix_strategy 兩種模式;細節說明見 module docstring。
PrefixStrategy = Literal["share", "fork"]

# sub-agent runner 的抽象 — 接 (sub_agent, prompt) 回 awaitable 任意結果。
# 預設用 ``agents.Runner.run``,測試可注入 mock。
SubAgentRunner = Callable[[Agent[Any], str], Awaitable[Any]]

# sub-agent 派工的預設 timeout(秒)。LLM call + tool loop 一般幾分鐘內可完成,
# 超過視為卡死,fail-fast 回 error JSON 避免拖死 parent。
DEFAULT_SUBAGENT_TIMEOUT_SECONDS: float = 300.0

# AgentTool 預設綁的 metadata。sub-agent 通常會跑 LLM,標為 high cost、非唯讀、
# 非 concurrency_safe(同一 sub-agent 內可能有 stateful operation)。
_DEFAULT_AGENT_TOOL_METADATA = ToolMetadata(
    is_read_only=False,
    is_destructive=False,
    concurrency_safe=False,
    cost_estimate="high",
    requires_approval=False,
    is_open_world=False,
    category="agent",
)


# ---------------------------------------------------------------------------
# AgentTool dataclass — 一份不可變的 sub-agent 派工 spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTool:
    """把一個 sub-agent 封裝為 tool spec。

    本物件本身是 spec(資料容器),真正可被 openai-agents Runner 認識的物件是
    ``self.tool``(:class:`agents.FunctionTool`),由 :func:`make_agent_tool` 在
    build 時組好並設為 frozen 欄位。

    Attributes:
        sub_agent: 要被包成 tool 的子 agent。
        name: tool 名稱(LLM 看到的)。預設為 ``f"call_{sub_agent.name}"``。
        description: tool 描述,給 LLM 判斷何時呼叫。
        metadata: ToolMetadata — 預設 cost=high、非唯讀、非 concurrency safe。
        prefix_strategy: ``"share"`` / ``"fork"``;見 module docstring 對兩種策略的說明。
        fork_point: ``"fork"`` 策略下截斷 parent message 的 index。None 則
            用 :data:`anila_agent.core.prompt_cache.DEFAULT_PREFIX_MESSAGE_COUNT`;
            ``"share"`` 策略下此欄位被忽略。
        timeout_seconds: sub-agent 跑超時就 fail-fast。預設 ``DEFAULT_SUBAGENT_TIMEOUT_SECONDS``。
        runner: 注入點 — 由本欄位指定的 callable 來實際跑 sub-agent;預設用
            ``agents.Runner.run``。測試以 ``AsyncMock`` 或自寫 dummy runner 取代。
        tool: 由 factory 在 build 時組好的 :class:`agents.FunctionTool` 實體。
            這個物件才是真正可被 parent agent 的 tool registry 接收的 tool。
    """

    sub_agent: Agent[Any]
    name: str
    description: str
    metadata: ToolMetadata = field(default_factory=lambda: _DEFAULT_AGENT_TOOL_METADATA)
    prefix_strategy: PrefixStrategy = "share"
    fork_point: int | None = None
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    runner: SubAgentRunner | None = None
    tool: FunctionTool | None = None
    # P1-17 — 注入 hook registry + event bus 供 SUBAGENT_DISPATCH_* event 用;
    # 兩者皆 optional,None 時 dispatch 不 fire hook(向後相容)。
    hook_registry: HookRegistry | None = None
    event_bus: EventBus | None = None


# ---------------------------------------------------------------------------
# Sub-context 工廠 — parent ctx → sub ctx
# ---------------------------------------------------------------------------


def _create_sub_context(
    parent_ctx: AnilaToolContext,
    *,
    sub_agent_name: str,
    sub_tool_call_id: str,
) -> AnilaToolContext:
    """以 parent context 為基底,建一個給 sub-agent 用的 child context。

    繼承 session_id / turn_id / workspace / user_id / caller_id / file_state_cache,
    但 ``agent_name`` 改為 sub-agent、``tool_call_id`` 換成 sub-call 用的新 id。
    metadata 淺拷貝後加上 ``"parent_agent"`` 與 ``"parent_tool_call_id"`` 兩個欄位,
    讓 trace / log 可以還原派工鏈。
    """
    sub_metadata = dict(parent_ctx.metadata)
    sub_metadata["parent_agent"] = parent_ctx.agent_name
    sub_metadata["parent_tool_call_id"] = parent_ctx.tool_call_id

    return AnilaToolContext(
        session_id=parent_ctx.session_id,
        turn_id=parent_ctx.turn_id,
        tool_call_id=sub_tool_call_id,
        agent_name=sub_agent_name,
        workspace=parent_ctx.workspace,
        file_state_cache=parent_ctx.file_state_cache,
        user_id=parent_ctx.user_id,
        caller_id=parent_ctx.caller_id,
        metadata=sub_metadata,
    )


# ---------------------------------------------------------------------------
# 預設 sub-agent runner — 走 openai-agents 原生 Runner.run
# ---------------------------------------------------------------------------


async def _default_sub_agent_runner(sub_agent: Agent[Any], prompt: str) -> Any:
    """預設 sub-agent runner:呼叫 ``agents.Runner.run`` 取回 final output。

    沒帶 ``session`` / ``hooks`` / ``max_turns`` — 都吃 openai-agents 預設。
    後續 P1 若要支援 sub-agent 也帶 hook / memory,可改用 ``AnilaRunner`` 重新組裝。
    """
    result = await Runner.run(starting_agent=sub_agent, input=prompt)
    return result.final_output


# ---------------------------------------------------------------------------
# 從 tool 反查 spec(供 registry / log 用)
# ---------------------------------------------------------------------------


_AGENT_TOOL_SPEC_ATTR = "__anila_agent_tool_spec__"


def get_agent_tool_spec(tool: FunctionTool) -> AgentTool | None:
    """取出 FunctionTool 上掛的 :class:`AgentTool` spec(若存在)。

    用於 registry 過濾或 trace span 取 sub_agent 名稱;非 AgentTool 包出來的
    function tool 會回 None。
    """
    spec = getattr(tool, _AGENT_TOOL_SPEC_ATTR, None)
    return spec if isinstance(spec, AgentTool) else None


# ---------------------------------------------------------------------------
# JSON schema — sub-agent 的輸入參數固定為 {prompt, context_summary?}
# ---------------------------------------------------------------------------


def _build_args_schema() -> dict[str, Any]:
    """產生 sub-agent tool 的 args JSON schema。

    ``prompt`` 必填(parent agent 要派給 sub-agent 的任務說明);
    ``context_summary`` 選填(parent 額外提供的背景摘要,例如「使用者問什麼」)。

    schema 用 ``additionalProperties: False`` 以符合 openai-agents 預設的
    strict JSON schema 要求。
    """
    return {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "要派給 sub-agent 執行的任務描述。",
            },
            "context_summary": {
                "type": "string",
                "description": "選填:parent 提供給 sub-agent 的背景脈絡摘要。",
            },
        },
        "required": ["prompt", "context_summary"],
        "additionalProperties": False,
    }


def _compose_sub_agent_input(prompt: str, context_summary: str | None) -> str:
    """把 args 中的 ``prompt`` 與 ``context_summary`` 組成 sub-agent 的單一輸入字串。

    若 ``context_summary`` 為空就只回 prompt;否則用標題形式分區。本函式刻意不
    依賴 templating engine,維持純 std lib。
    """
    if not context_summary:
        return prompt
    return f"[context]\n{context_summary}\n\n[task]\n{prompt}"


# ---------------------------------------------------------------------------
# Description 推導
# ---------------------------------------------------------------------------


_DEFAULT_DESCRIPTION_PREVIEW_LEN = 100


def _infer_description(sub_agent: Agent[Any]) -> str:
    """從 sub-agent 的 ``instructions`` 取前 ~100 字當 tool description。

    若 ``instructions`` 不是 str(openai-agents 允許 callable instructions),
    或為空,就回一個保底字串。
    """
    raw = getattr(sub_agent, "instructions", None)
    if isinstance(raw, str) and raw.strip():
        snippet = raw.strip().replace("\n", " ")
        if len(snippet) > _DEFAULT_DESCRIPTION_PREVIEW_LEN:
            snippet = snippet[:_DEFAULT_DESCRIPTION_PREVIEW_LEN].rstrip() + "..."
        return f"派工給 sub-agent `{sub_agent.name}`:{snippet}"
    return f"派工給 sub-agent `{sub_agent.name}`(無 instructions 摘要可用)。"


# ---------------------------------------------------------------------------
# make_agent_tool — 主 factory
# ---------------------------------------------------------------------------


def make_agent_tool(
    sub_agent: Agent[Any],
    *,
    name: str | None = None,
    description: str | None = None,
    metadata: ToolMetadata | None = None,
    prefix_strategy: PrefixStrategy = "share",
    fork_point: int | None = None,
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    runner: SubAgentRunner | None = None,
    tracer: Tracer | None = None,
    hook_registry: HookRegistry | None = None,
    event_bus: EventBus | None = None,
) -> AgentTool:
    """把 sub-agent 包成一個 :class:`AgentTool` spec(內含 FunctionTool 可用實體)。

    包出來的 ``AgentTool.tool`` 可直接餵給 parent agent 的 tool registry。

    Args:
        sub_agent: 被包裝的子 agent。
        name: tool 名稱(LLM 看到的);預設 ``f"call_{sub_agent.name}"``。
        description: tool 描述;預設由 sub_agent.instructions 推導前 ~100 字。
        metadata: ToolMetadata;預設 cost=high / category=agent / 非唯讀。
        prefix_strategy: ``"share"`` / ``"fork"``;見 module docstring 對兩種策略的說明。
        fork_point: ``"fork"`` 策略下截斷 parent message 的 index;None 則
            用 :data:`anila_agent.core.prompt_cache.DEFAULT_PREFIX_MESSAGE_COUNT`。
            ``"share"`` 策略下此參數被忽略。
        timeout_seconds: sub-agent 跑超時就回 error JSON。預設
            ``DEFAULT_SUBAGENT_TIMEOUT_SECONDS``(300s)。
        runner: 注入點 — 改用自訂 callable 跑 sub-agent;預設走 ``Runner.run``。
        tracer: 若提供則整個 dispatch 包一層 span(``agent_tool.dispatch.<sub_agent.name>``);
            未提供則不做 tracing。
        hook_registry: P1-17 — 若提供,dispatch 前後 fire
            ``SUBAGENT_DISPATCH_START`` / ``SUBAGENT_DISPATCH_END`` event。
            未提供則 dispatch 不 fire hook(向後相容)。
        event_bus: P1-17 — 搭配 hook_registry 用,把 hook 事件也送進 event bus。

    Returns:
        :class:`AgentTool` — 內含已組好的 ``tool`` (FunctionTool),可加入 registry。
    """
    resolved_name = name if name is not None else f"call_{sub_agent.name}"
    resolved_description = description if description is not None else _infer_description(sub_agent)
    resolved_metadata = metadata if metadata is not None else _DEFAULT_AGENT_TOOL_METADATA
    resolved_runner: SubAgentRunner = runner if runner is not None else _default_sub_agent_runner

    # 先建 spec(沒 tool 欄位);稍後把 tool 用 object.__setattr__ 塞回去(frozen dataclass)。
    spec = AgentTool(
        sub_agent=sub_agent,
        name=resolved_name,
        description=resolved_description,
        metadata=resolved_metadata,
        prefix_strategy=prefix_strategy,
        fork_point=fork_point,
        timeout_seconds=timeout_seconds,
        runner=resolved_runner,
        hook_registry=hook_registry,
        event_bus=event_bus,
    )

    async def _on_invoke_tool(ctx: ToolContext[Any], input_json: str) -> str:
        """sub-agent dispatch 的 FunctionTool 回呼。

        流程:
        1. 解析 input_json 取 prompt / context_summary。
        2. 從 ctx.context 拿 parent :class:`AnilaToolContext`(若有)。
        3. 建一份 sub-context(新 tool_call_id、agent_name 改 sub_agent)。
        4. 用 tracer 包 span(若提供)。
        5. 透過 runner 跑 sub-agent;timeout / 例外都吃掉,回 error JSON 不 raise。
        6. 成功:回 sub-agent final output(str 化)給 parent 當 tool result。
        """
        return await _dispatch_sub_agent(
            spec=spec,
            ctx=ctx,
            input_json=input_json,
            tracer=tracer,
        )

    function_tool = FunctionTool(
        name=resolved_name,
        description=resolved_description,
        params_json_schema=_build_args_schema(),
        on_invoke_tool=_on_invoke_tool,
        strict_json_schema=True,
    )

    # 把 ToolMetadata 與 AgentTool spec 都掛到 function_tool 上,讓 registry /
    # log / get_metadata 都能反查回來。
    function_tool.__anila_metadata__ = resolved_metadata
    setattr(function_tool, _AGENT_TOOL_SPEC_ATTR, spec)

    # frozen dataclass — 用 object.__setattr__ 回填 tool 欄位。
    object.__setattr__(spec, "tool", function_tool)
    return spec


# ---------------------------------------------------------------------------
# Dispatch 主流程 — sub-agent 派工 + 錯誤處理 + tracing
# ---------------------------------------------------------------------------


async def _dispatch_sub_agent(
    *,
    spec: AgentTool,
    ctx: ToolContext[Any],
    input_json: str,
    tracer: Tracer | None,
) -> str:
    """執行 sub-agent dispatch — 解析輸入、建 sub-context、跑 sub-agent、回字串。

    所有錯誤(JSON parse 失敗 / sub-agent raise / timeout)一律捕捉並轉成 error
    JSON 字串回給 parent,**不 raise**(避免直接炸掉 parent 的 tool loop)。
    """
    sub_agent = spec.sub_agent

    # 1. parse args(失敗也回 error JSON,不 raise)
    try:
        args = json.loads(input_json) if input_json else {}
    except json.JSONDecodeError as exc:
        return _error_json(f"invalid input_json: {exc}")

    prompt = args.get("prompt", "")
    context_summary = args.get("context_summary")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error_json("missing or empty 'prompt' argument")

    # 給 sub-agent runner 看的「pure prompt」(legacy 字串組合);prompt-cache
    # 友善的 message list 由 ``build_subagent_prefix`` 另外組,寫進 span 即可。
    sub_input = _compose_sub_agent_input(prompt, context_summary)

    # 2. 建 sub-context(若 parent ctx.context 是 AnilaToolContext)
    parent_anila_ctx = _extract_anila_context(ctx)
    sub_tool_call_id = f"sub_{uuid.uuid4().hex[:12]}"
    if parent_anila_ctx is not None:
        sub_anila_ctx = _create_sub_context(
            parent_anila_ctx,
            sub_agent_name=sub_agent.name,
            sub_tool_call_id=sub_tool_call_id,
        )
    else:
        # parent 沒給 AnilaToolContext 也允許跑(向後相容用),但 sub_anila_ctx 為 None。
        sub_anila_ctx = None

    # 2.5 P1-1 — 算 prompt-cache prefix(從 parent ctx.metadata 取 parent_messages,
    # 若呼叫端沒給就用空 list;空 list 仍可產生 deterministic hash,代表
    # 「沒 parent history」的 baseline cache slot)。
    parent_messages = _extract_parent_messages(parent_anila_ctx)
    prefix_info: SubagentPrefix = build_subagent_prefix(
        parent_messages,
        strategy=spec.prefix_strategy,
        directive=sub_input,
        fork_point=spec.fork_point,
    )

    # 3. 跑 sub-agent — 包 trace span(若有 tracer);錯誤一律轉 error JSON。
    runner = spec.runner if spec.runner is not None else _default_sub_agent_runner

    async def _run_with_timeout() -> Any:
        return await asyncio.wait_for(
            runner(sub_agent, sub_input),
            timeout=spec.timeout_seconds,
        )

    span_name = f"agent_tool.dispatch.{sub_agent.name}"
    span_attributes: dict[str, Any] = {
        "agent_tool.name": spec.name,
        "agent_tool.sub_agent": sub_agent.name,
        "agent_tool.prefix_strategy": spec.prefix_strategy,
        "agent_tool.timeout_seconds": spec.timeout_seconds,
        "agent_tool.sub_tool_call_id": sub_tool_call_id,
        # P1-1 — prompt-cache prefix 資訊(供 trace 後端統計 cache 命中率)
        "prompt_cache.strategy": prefix_info.strategy,
        "prompt_cache.prefix_hash": prefix_info.prefix_hash,
        "prompt_cache.prefix_messages": prefix_info.prefix_message_count,
        "prompt_cache.prefix_bytes": len(prefix_info.prefix_bytes),
    }
    if parent_anila_ctx is not None:
        span_attributes["agent_tool.parent_agent"] = parent_anila_ctx.agent_name
        span_attributes["agent_tool.parent_tool_call_id"] = parent_anila_ctx.tool_call_id
    if sub_anila_ctx is not None:
        span_attributes["agent_tool.sub_agent_name"] = sub_anila_ctx.agent_name

    # P1-17 — dispatch 前 fire SUBAGENT_DISPATCH_START hook(若有 registry)。
    parent_agent_name = parent_anila_ctx.agent_name if parent_anila_ctx is not None else ""
    await fire_subagent_dispatch_start(
        spec.hook_registry,
        spec.event_bus,
        parent_agent=parent_agent_name,
        sub_agent=sub_agent.name,
        tool_name=spec.name,
        sub_tool_call_id=sub_tool_call_id,
        prompt=sub_input,
    )

    if tracer is not None:
        with tracer.start_span(span_name, attributes=span_attributes) as span:
            output, error = await _execute_dispatch(_run_with_timeout, sub_agent.name, span)
    else:
        output, error = await _execute_dispatch(_run_with_timeout, sub_agent.name, span=None)

    # P1-17 — dispatch 後 fire SUBAGENT_DISPATCH_END hook(成功或失敗都會 fire)。
    await fire_subagent_dispatch_end(
        spec.hook_registry,
        spec.event_bus,
        parent_agent=parent_agent_name,
        sub_agent=sub_agent.name,
        tool_name=spec.name,
        sub_tool_call_id=sub_tool_call_id,
        output=output,
        error=error,
    )
    return output


async def _execute_dispatch(
    run_coro_factory: Callable[[], Awaitable[Any]],
    sub_agent_name: str,
    span: Any | None,
) -> tuple[str, str | None]:
    """實際呼叫 sub-agent runner 並把例外攔截轉 error JSON。

    ``span`` 為 :class:`anila_agent.tracing.Span` 或 None;有 span 時把 error 詳情
    寫進 span attributes 並設 status='error',方便 tracing 後端撈出原因。

    Returns:
        ``(output_str, error_summary)``:成功時 ``error_summary`` 為 None;
        timeout / exception 時 ``output_str`` 為 error JSON、``error_summary`` 為錯誤摘要。
        P1-17 起把 error 摘要回傳給 caller 以便 fire SUBAGENT_DISPATCH_END hook。
    """
    try:
        result = await run_coro_factory()
    except asyncio.TimeoutError:
        msg = f"sub-agent {sub_agent_name} timed out"
        if span is not None:
            span.status = "error"
            span.error = msg
        return _error_json(msg), msg
    except Exception as exc:
        msg = f"sub-agent {sub_agent_name} failed: {type(exc).__name__}: {exc}"
        if span is not None:
            span.status = "error"
            span.error = msg
        return _error_json(msg), msg

    # sub-agent final_output 可能是 str / dict / dataclass — 統一字串化給 parent。
    output_str = result if isinstance(result, str) else str(result)
    if span is not None:
        span.attributes["agent_tool.output_chars"] = len(output_str)
    return output_str, None


def _extract_anila_context(ctx: ToolContext[Any]) -> AnilaToolContext | None:
    """從 openai-agents :class:`ToolContext` 上挖出 :class:`AnilaToolContext`。

    呼叫端可把 AnilaToolContext 放在 ``ctx.context``(openai-agents 標準 context 槽);
    若不是 AnilaToolContext 則回 None,代表 parent 沒走 Anila context flow。
    """
    inner = getattr(ctx, "context", None)
    if isinstance(inner, AnilaToolContext):
        return inner
    return None


# P1-1 — parent context 上若有 ``parent_messages`` 就拿來算 prompt-cache prefix。
# 為了與既有 AnilaToolContext 介面相容(不改 schema),改放在 ``metadata`` 內,
# 由呼叫端按需注入;沒給就 fallback 空 list。
_PARENT_MESSAGES_METADATA_KEY: str = "parent_messages"


def _extract_parent_messages(parent_anila_ctx: AnilaToolContext | None) -> list[Message]:
    """從 parent context 的 metadata 取出 parent_messages 供 prefix hash 用。

    呼叫端若想啟用 prompt-cache prefix,需在 parent ``AnilaToolContext.metadata``
    放入 ``"parent_messages": [...]``。若沒提供或型別不對,回空 list(仍可
    產 deterministic hash,代表「無 parent history」baseline)。

    Args:
        parent_anila_ctx: parent :class:`AnilaToolContext` 或 None。

    Returns:
        parent message list(可能為空)。dict items only — 非 dict 會被略掉
        以免污染 deterministic 序列化。
    """
    if parent_anila_ctx is None:
        return []
    raw = parent_anila_ctx.metadata.get(_PARENT_MESSAGES_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    # 只接受 dict-like message。其他型別跳過,確保 deterministic 序列化不爆掉。
    return [m for m in raw if isinstance(m, dict)]


def _error_json(message: str) -> str:
    """把錯誤訊息序列化為 ``{"error": ...}`` JSON 字串。"""
    return json.dumps({"error": message}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registry 整合 helper
# ---------------------------------------------------------------------------


def register_agent_as_tool(
    sub_agent: Agent[Any],
    registry: Any,
    *,
    name: str | None = None,
    description: str | None = None,
    metadata: ToolMetadata | None = None,
    prefix_strategy: PrefixStrategy = "share",
    fork_point: int | None = None,
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS,
    runner: SubAgentRunner | None = None,
    tracer: Tracer | None = None,
    hook_registry: HookRegistry | None = None,
    event_bus: EventBus | None = None,
) -> AgentTool:
    """方便 helper:把 sub-agent 包成 AgentTool 並 add 進 registry 一次完成。

    ``registry`` 採 duck typing(只需要有 ``add(tool)`` method);
    型別寫 Any 避免 import :class:`ToolRegistry` 造成 cycle 風險(目前無 cycle,
    但保留彈性供測試 mock)。

    Returns:
        :class:`AgentTool` — 已 register;呼叫端通常拿來 debug / 寫 log。
    """
    spec = make_agent_tool(
        sub_agent,
        name=name,
        description=description,
        metadata=metadata,
        prefix_strategy=prefix_strategy,
        fork_point=fork_point,
        timeout_seconds=timeout_seconds,
        runner=runner,
        tracer=tracer,
        hook_registry=hook_registry,
        event_bus=event_bus,
    )
    assert spec.tool is not None  # make_agent_tool 一定有設;assert 給 type checker 看
    registry.add(spec.tool)
    return spec


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_SUBAGENT_TIMEOUT_SECONDS",
    "AgentTool",
    "PrefixStrategy",
    "SubAgentRunner",
    "get_agent_tool_spec",
    "make_agent_tool",
    "register_agent_as_tool",
]

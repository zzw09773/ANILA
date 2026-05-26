"""Hook surface ported from claude-code-src `types/hooks.ts`。

公開的事件如下：

    PreToolUse        — 工具執行前觸發；可改寫輸入或封鎖該次呼叫。
    PostToolUse       — 工具執行後觸發；可注入下一輪要附加的 context。
    Stop              — Agent 產出最終輸出時觸發。
    SessionStart      — 每個 session 開始前觸發一次。
    UserPromptSubmit  — 使用者在 REPL 送出新訊息時觸發。
    PermissionRequest — 工具呼叫需要顯式核可時觸發。
    AgentStart        — Agent 被執行前觸發（每次目前 Agent 切換都會觸發一次）。
    AgentEnd          — Agent 產生輸出時觸發；等同 Stop，但保留為獨立事件以利區分。
    Handoff           — 控制權從一個 Agent 交接到另一個時觸發。

以上事件會橋接到 openai-agents 的 `RunHooks` lifecycle callback。Hook callback 必須回傳
`HookOutput`；runner 會依事件聚合結果，`updated_input` 採 last-writer-wins、
`additional_context` 採聯集語意。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Sequence

from agents import Agent, AgentHooks, RunContextWrapper, RunHooks, Tool
from agents.items import ModelResponse, TResponseInputItem

from anila_agent.core.events import EventBus
from anila_agent.core.hook_flavors import HookABC
from anila_agent.models.schemas import HookOutput


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PERMISSION_REQUEST = "PermissionRequest"
    AGENT_START = "AgentStart"
    AGENT_END = "AgentEnd"
    HANDOFF = "Handoff"


@dataclass(frozen=True)
class PreToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]
    tool_call_id: str | None
    agent_name: str


@dataclass(frozen=True)
class PostToolUseInput:
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: Any
    tool_call_id: str | None
    agent_name: str


@dataclass(frozen=True)
class StopInput:
    agent_name: str
    final_output: Any
    turns_used: int


@dataclass(frozen=True)
class SessionStartInput:
    session_id: str
    agent_name: str


@dataclass(frozen=True)
class UserPromptSubmitInput:
    prompt: str
    session_id: str


@dataclass(frozen=True)
class AgentStartInput:
    """`AgentStart` hook 的 payload — 每次目前 Agent 切換到該 Agent 都會帶。"""

    agent_name: str


@dataclass(frozen=True)
class AgentEndInput:
    """`AgentEnd` hook 的 payload — Agent 產生最終輸出時帶（含當下輸出值）。"""

    agent_name: str
    output: Any


@dataclass(frozen=True)
class HandoffInput:
    """`Handoff` hook 的 payload — 兩個 Agent 名稱在交接當下帶。"""

    from_agent: str
    to_agent: str


HookCallback = Callable[[Any], "HookOutput | Awaitable[HookOutput]"]


@dataclass(frozen=True)
class HookSpec:
    """Declarative hook registration。

    matcher：regex 配 tool name（PreToolUse / PostToolUse），或填 "*" / 空字串 / ".*"
             代表全配（event-level 事件）。
    callback：可以是 sync / async callable（向後相容），也可以直接是 `HookABC` instance
              （P0-4 起支援 command / http / prompt flavor）。

    注意：dataclass 用 `frozen=True`，註冊後不應再就地改 callback；要新增 hook 請另建一個
    `HookSpec` 用 `HookRegistry.register(...)`。
    """

    event: HookEvent
    callback: HookCallback | HookABC
    matcher: str = ".*"


@dataclass
class _AggregatedHookResult:
    block: bool = False
    abort: bool = False
    reason: str | None = None
    stop_reason: str | None = None
    additional_contexts: list[str] | None = None
    updated_input: dict[str, Any] | None = None


class HookRegistry:
    """保存 hook spec 並依事件 / tool name 找出該觸發的 hook chain。

    P0-4 起：
        - 同一個 `(event, matcher)` 可掛多個 hook（chain），順序為註冊順序，
          逐一執行，任一 hook 回 block 或 abort 就停（見 `fire(...)`）。
        - 支援用 `register_hook(event, hook, matcher)` 直接掛 `HookABC` instance（含
          CommandHook / HttpHook / PromptHook），不必自己組 `HookSpec`。
        - 支援 decorator factory：`@registry.pre_tool_use("Bash.*")` 等寫法，
          對齊 antigravity SDK 的 `@pre_turn` / `@post_tool_call`。
    """

    def __init__(self, specs: Sequence[HookSpec] = ()) -> None:
        self._specs: list[HookSpec] = list(specs)

    def register(self, spec: HookSpec) -> None:
        """加入一筆 `HookSpec`。chain 順序即註冊順序。"""
        self._specs.append(spec)

    def register_hook(
        self,
        event: HookEvent,
        hook: HookCallback | HookABC,
        *,
        matcher: str = ".*",
    ) -> HookSpec:
        """便利 API：直接掛 callable 或 `HookABC` instance；回傳建立的 `HookSpec`。"""
        spec = HookSpec(event=event, callback=hook, matcher=matcher)
        self._specs.append(spec)
        return spec

    def specs_for(self, event: HookEvent, tool_name: str | None = None) -> list[HookSpec]:
        """找出對應 event + tool name 的 hook chain；保留註冊順序。"""
        import re

        out: list[HookSpec] = []
        for spec in self._specs:
            if spec.event is not event:
                continue
            if tool_name is None or spec.matcher in ("*", "", ".*"):
                out.append(spec)
                continue
            try:
                if re.fullmatch(spec.matcher, tool_name):
                    out.append(spec)
            except re.error:
                # 設定檔 regex 寫壞了，退回字面比對。
                if spec.matcher == tool_name:
                    out.append(spec)
        return out

    # ------------------------------------------------------------------
    # Decorator factory — 對齊 antigravity SDK 的 `@pre_turn` / `@post_tool_call`
    # ------------------------------------------------------------------

    def _decorator_for(
        self, event: HookEvent
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """產生對應 event 的 decorator factory。

        允許兩種寫法：
            @registry.pre_tool_use         # 不帶括號 — 套用 default matcher ".*"
            def cb(payload): ...

            @registry.pre_tool_use("Bash.*")   # 帶 matcher
            def cb(payload): ...
        """

        def factory(*args: Any, **kwargs: Any) -> Any:
            # 不帶括號：第一個 arg 直接是 callback
            if len(args) == 1 and callable(args[0]) and not isinstance(args[0], HookABC):
                cb = args[0]
                self.register(HookSpec(event=event, callback=cb))
                return cb
            matcher = kwargs.get("matcher") or (args[0] if args else ".*")

            def _wrap(cb: HookCallback) -> HookCallback:
                self.register(HookSpec(event=event, callback=cb, matcher=matcher))
                return cb

            return _wrap

        factory.__name__ = f"register_{event.value}"
        factory.__doc__ = (
            f"Decorator：把 callback 註冊為 `{event.value}` hook。"
            "可選 `matcher` 位置參數設定 tool name regex。"
        )
        return factory

    @property
    def pre_tool_use(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `PreToolUse` hook。"""
        return self._decorator_for(HookEvent.PRE_TOOL_USE)

    @property
    def post_tool_use(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `PostToolUse` hook。"""
        return self._decorator_for(HookEvent.POST_TOOL_USE)

    @property
    def stop(self) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `Stop` hook。"""
        return self._decorator_for(HookEvent.STOP)

    @property
    def session_start(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `SessionStart` hook。"""
        return self._decorator_for(HookEvent.SESSION_START)

    @property
    def user_prompt_submit(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `UserPromptSubmit` hook。"""
        return self._decorator_for(HookEvent.USER_PROMPT_SUBMIT)

    @property
    def permission_request(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `PermissionRequest` hook。"""
        return self._decorator_for(HookEvent.PERMISSION_REQUEST)

    @property
    def agent_start(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `AgentStart` hook。"""
        return self._decorator_for(HookEvent.AGENT_START)

    @property
    def agent_end(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `AgentEnd` hook。"""
        return self._decorator_for(HookEvent.AGENT_END)

    @property
    def handoff(
        self,
    ) -> Callable[..., Callable[[HookCallback], HookCallback]]:
        """Decorator：註冊 `Handoff` hook。"""
        return self._decorator_for(HookEvent.HANDOFF)


async def _invoke(callback: HookCallback | HookABC, payload: Any) -> HookOutput:
    """執行單一 hook callback，回傳 `HookOutput`。

    `callback` 可為下列三種之一：
        - `HookABC` instance：呼叫其 `run(payload)`（含 CommandHook / HttpHook / PromptHook）。
        - sync callable：直接呼叫並期待回傳 `HookOutput`。
        - async callable / coroutine：await 結果。
    """
    if isinstance(callback, HookABC):
        result: Any = await callback.run(payload)
    else:
        result = callback(payload)
        if inspect.isawaitable(result):
            result = await result
    if not isinstance(result, HookOutput):
        raise TypeError(
            f"hook callback {getattr(callback, '__qualname__', callback)} returned "
            f"{type(result).__name__}, expected HookOutput"
        )
    return result


async def fire(
    registry: HookRegistry,
    event: HookEvent,
    payload: Any,
    *,
    tool_name: str | None = None,
    bus: EventBus | None = None,
) -> _AggregatedHookResult:
    """執行 hook chain 並聚合結果。

    Chain 行為（P0-4）：
        - 依註冊順序執行。
        - 任一 hook 回 `block` 或 `continue_=False` → 立即停止（後續 hook 不執行）。
        - `additional_context` 跨 hook 取聯集。
        - `updated_input` 採 last-writer-wins。
    """
    agg = _AggregatedHookResult()
    contexts: list[str] = []
    for spec in registry.specs_for(event, tool_name):
        out = await _invoke(spec.callback, payload)
        if bus is not None:
            bus.emit(
                "hook_fired",
                event=event.value,
                callback=getattr(spec.callback, "__qualname__", repr(spec.callback)),
                tool_name=tool_name,
                decision=out.decision,
            )
        if out.additional_context:
            contexts.append(out.additional_context)
        if out.updated_input is not None:
            agg.updated_input = dict(out.updated_input)
        if out.continue_ is False:
            agg.abort = True
            agg.stop_reason = out.stop_reason or out.reason
            break  # chain 早停 — abort 比 block 更強
        if out.decision == "block":
            agg.block = True
            agg.reason = out.reason
            break  # chain 早停 — 已經 block 就不必再跑後面 hook
    if contexts:
        agg.additional_contexts = contexts
    return agg


class AnilaRunHooks(RunHooks[Any]):
    """把 openai-agents 的 lifecycle callback 橋接到 Anila hook 事件系統。

    Runner 用 registry + event bus 建構此物件後傳入 `Runner.run`，
    各 lifecycle callback 對應到的 Anila 事件如下：

        on_agent_start -> AgentStart
        on_agent_end   -> AgentEnd + Stop（兩者皆觸發）
        on_handoff     -> Handoff
        on_tool_start  -> PreToolUse
        on_tool_end    -> PostToolUse
        on_llm_*       -> 僅發送到 event bus，預設不對應到 hook 事件
    """

    def __init__(
        self,
        registry: HookRegistry,
        bus: EventBus,
        *,
        agent_name: str,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._agent_name = agent_name
        self._turns = 0

    @property
    def turns(self) -> int:
        return self._turns

    async def on_agent_start(
        self,
        context: Any,
        agent: Agent[Any],
    ) -> None:
        """Agent 被執行前觸發，每次目前 Agent 切換時都會呼叫一次。"""
        payload = AgentStartInput(agent_name=agent.name)
        self._bus.emit("agent_started", agent=agent.name)
        await fire(self._registry, HookEvent.AGENT_START, payload, bus=self._bus)

    async def on_handoff(
        self,
        context: RunContextWrapper[Any],
        from_agent: Agent[Any],
        to_agent: Agent[Any],
    ) -> None:
        """Agent 控制權交接時觸發；payload 帶來源與目標 Agent 名稱。"""
        payload = HandoffInput(from_agent=from_agent.name, to_agent=to_agent.name)
        self._bus.emit("handoff", from_agent=from_agent.name, to_agent=to_agent.name)
        await fire(self._registry, HookEvent.HANDOFF, payload, bus=self._bus)

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        self._turns += 1
        self._bus.emit("llm_started", agent=agent.name, turn=self._turns)

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        self._bus.emit("llm_ended", agent=agent.name, turn=self._turns)

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        tool_input, call_id = _extract_tool_call(context)
        payload = PreToolUseInput(
            tool_name=tool.name,
            tool_input=tool_input,
            tool_call_id=call_id,
            agent_name=agent.name,
        )
        self._bus.emit("tool_started", tool=tool.name, agent=agent.name, input=tool_input)
        agg = await fire(
            self._registry, HookEvent.PRE_TOOL_USE, payload, tool_name=tool.name, bus=self._bus
        )
        if agg.abort:
            from agents.exceptions import UserError

            raise UserError(agg.stop_reason or "Aborted by PreToolUse hook")
        if agg.block:
            from agents.exceptions import UserError

            raise UserError(agg.reason or f"PreToolUse blocked {tool.name}")
        # updated_input is honoured by mutating tool_arguments on the ToolContext.
        if agg.updated_input is not None and hasattr(context, "tool_arguments"):
            try:
                import json

                context.tool_arguments = json.dumps(agg.updated_input)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: str,
    ) -> None:
        tool_input, call_id = _extract_tool_call(context)
        payload = PostToolUseInput(
            tool_name=tool.name,
            tool_input=tool_input,
            tool_output=result,
            tool_call_id=call_id,
            agent_name=agent.name,
        )
        self._bus.emit("tool_ended", tool=tool.name, agent=agent.name)
        await fire(
            self._registry, HookEvent.POST_TOOL_USE, payload, tool_name=tool.name, bus=self._bus
        )

    async def on_agent_end(
        self,
        context: Any,
        agent: Agent[Any],
        output: Any,
    ) -> None:
        """Agent 產生最終輸出時觸發；同時 fire `AgentEnd` 與 `Stop`（向後相容）。"""
        stop_payload = StopInput(
            agent_name=agent.name, final_output=output, turns_used=self._turns
        )
        end_payload = AgentEndInput(agent_name=agent.name, output=output)
        self._bus.emit("turn_ended", agent=agent.name, turns=self._turns)
        await fire(self._registry, HookEvent.AGENT_END, end_payload, bus=self._bus)
        await fire(self._registry, HookEvent.STOP, stop_payload, bus=self._bus)


class AnilaAgentHooks(AgentHooks[Any]):
    """Per-agent lifecycle hook 抽象。

    用法：把實例設到 `agent.hooks`，可只對該 Agent 觀察事件，
    與全域 `AnilaRunHooks` 並存（兩層皆會收到 callback）。

    所有 callback 預設 no-op；subclass 只覆寫需要的 method 即可。
    建構時可選擇傳入 `EventBus` 把 per-agent 事件也送進事件流。
    """

    def __init__(self, *, bus: EventBus | None = None) -> None:
        self._bus = bus

    async def on_start(self, context: Any, agent: Agent[Any]) -> None:
        """目前 Agent 切換到本 Agent 之前觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_started", agent=agent.name)

    async def on_end(self, context: Any, agent: Agent[Any], output: Any) -> None:
        """本 Agent 產生最終輸出時觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_ended", agent=agent.name)

    async def on_handoff(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        source: Agent[Any],
    ) -> None:
        """另一個 Agent 把控制權交給本 Agent 時觸發；`source` 是來源 Agent。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_handoff", agent=agent.name, source=source.name)

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        """本 Agent 即將執行 tool 之前觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_tool_started", agent=agent.name, tool=tool.name)

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: str,
    ) -> None:
        """本 Agent 的 tool 執行完成後觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_tool_ended", agent=agent.name, tool=tool.name)

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """本 Agent 即將呼叫 LLM 之前觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_llm_started", agent=agent.name)

    async def on_llm_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        response: ModelResponse,
    ) -> None:
        """本 Agent 收到 LLM 回覆後觸發。"""
        if self._bus is not None:
            self._bus.emit("agent_hooks_llm_ended", agent=agent.name)


def _extract_tool_call(context: Any) -> tuple[dict[str, Any], str | None]:
    """Pull tool_arguments + tool_call_id from a ToolContext if available."""
    args_raw = getattr(context, "tool_arguments", None)
    call_id = getattr(context, "tool_call_id", None)
    if isinstance(args_raw, str):
        try:
            import json

            parsed = json.loads(args_raw)
            if isinstance(parsed, dict):
                return parsed, call_id
        except Exception:  # noqa: BLE001
            pass
    if isinstance(args_raw, dict):
        return args_raw, call_id
    return {}, call_id


def fire_session_start(
    registry: HookRegistry, bus: EventBus, *, session_id: str, agent_name: str
) -> Awaitable[_AggregatedHookResult]:
    return fire(
        registry,
        HookEvent.SESSION_START,
        SessionStartInput(session_id=session_id, agent_name=agent_name),
        bus=bus,
    )


def fire_user_prompt_submit(
    registry: HookRegistry, bus: EventBus, *, prompt: str, session_id: str
) -> Awaitable[_AggregatedHookResult]:
    return fire(
        registry,
        HookEvent.USER_PROMPT_SUBMIT,
        UserPromptSubmitInput(prompt=prompt, session_id=session_id),
        bus=bus,
    )


def specs_from_config(
    entries: Iterable[dict[str, Any]],
    event: HookEvent,
    *,
    auto_memory_enabled: bool,
) -> list[HookSpec]:
    """Translate `tools.yaml` hook entries into HookSpec objects.

    Entries with `when: auto_memory` are skipped unless auto memory is enabled.
    """
    import importlib

    out: list[HookSpec] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        if when == "auto_memory" and not auto_memory_enabled:
            continue
        callback_path = entry.get("callback")
        if not callback_path:
            continue
        module_path, _, attr = callback_path.rpartition(".")
        if not module_path:
            raise ValueError(f"Invalid callback path: {callback_path!r}")
        callback = getattr(importlib.import_module(module_path), attr)
        out.append(HookSpec(event=event, callback=callback, matcher=entry.get("matcher", ".*")))
    return out

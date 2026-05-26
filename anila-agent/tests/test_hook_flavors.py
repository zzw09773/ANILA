"""P0-4 Hook flavor 擴充單元測試。

驗證項目：
- `PythonHook` 可包 sync / async callable，回傳 `HookOutput`。
- `CommandHook` 透過 subprocess 觸發；exit code 0 / 1 / 2 對應 allow / block / warn。
- `HttpHook` 用 `httpx.MockTransport` mock；2xx allow、4xx block、5xx warn。
- `PromptHook` stub 直接 raise `NotImplementedError`，不真正呼叫 LLM。
- decorator factory（`@registry.pre_tool_use` 等）能把 callback 註冊到對應 event。
- Hook chain 行為：多個 hook 依序執行；任一 block 後早停（後續 hook 不跑）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from anila_agent.core.events import EventBus
from anila_agent.core.hook_flavors import (
    CommandHook,
    HookABC,
    HookFlavor,
    HttpHook,
    PromptHook,
    PythonHook,
)
from anila_agent.core.hooks import (
    HookEvent,
    HookRegistry,
    HookSpec,
    PreToolUseInput,
    fire,
)
from anila_agent.models.schemas import HookOutput


def _make_payload(tool: str = "search", agent: str = "root") -> PreToolUseInput:
    """產生 `PreToolUseInput`，重複使用減少 boilerplate。"""
    return PreToolUseInput(
        tool_name=tool, tool_input={"q": "test"}, tool_call_id="call-1", agent_name=agent
    )


# ---------------------------------------------------------------------------
# PythonHook — sync / async callable
# ---------------------------------------------------------------------------


async def test_python_hook_sync_callable_returns_hook_output() -> None:
    """sync function 包成 PythonHook 後 `run` 回 HookOutput。"""

    def cb(payload: Any) -> HookOutput:
        assert payload.tool_name == "search"
        return HookOutput(additional_context="sync ok")

    hook = PythonHook(callback=cb)

    out = await hook.run(_make_payload())

    assert hook.flavor is HookFlavor.PYTHON
    assert out.additional_context == "sync ok"
    assert out.continue_ is True


async def test_python_hook_async_callable_is_awaited() -> None:
    """async function 包成 PythonHook 後 `run` 會 await 完成。"""

    async def cb(payload: Any) -> HookOutput:
        return HookOutput(decision="block", reason="async block")

    hook = PythonHook(callback=cb)
    out = await hook.run(_make_payload())

    assert out.decision == "block"
    assert out.reason == "async block"


async def test_python_hook_callable_must_return_hook_output() -> None:
    """callback 若回傳非 `HookOutput` 應該 raise TypeError，避免 silent allow。"""

    def cb(payload: Any) -> str:
        return "oops"

    hook = PythonHook(callback=cb)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        await hook.run(_make_payload())


# ---------------------------------------------------------------------------
# CommandHook — exit code 0 / 1 / 其他
# ---------------------------------------------------------------------------


async def test_command_hook_exit_zero_returns_allow() -> None:
    """exit 0 視為 allow；HookOutput 預設值。"""
    hook = CommandHook(command="exit 0", timeout_sec=5.0)
    out = await hook.run(_make_payload())

    assert hook.flavor is HookFlavor.COMMAND
    assert out.continue_ is True
    assert out.decision is None


async def test_command_hook_exit_zero_with_json_stdout_merges() -> None:
    """exit 0 + stdout 為合法 HookOutput JSON 時應 merge 結果。"""
    # 用 printf 把 JSON 寫到 stdout，再 exit 0。
    hook = CommandHook(
        command='printf \'{"additional_context": "from-cmd"}\' && exit 0',
        timeout_sec=5.0,
    )
    out = await hook.run(_make_payload())

    assert out.additional_context == "from-cmd"
    assert out.continue_ is True


async def test_command_hook_exit_one_blocks_with_stderr_reason() -> None:
    """exit 1 視為 block；reason 取 stderr 內容。"""
    hook = CommandHook(
        command="echo 'no good' 1>&2 && exit 1",
        timeout_sec=5.0,
    )
    out = await hook.run(_make_payload())

    assert out.decision == "block"
    assert out.reason is not None
    assert "no good" in out.reason


async def test_command_hook_exit_two_warns_but_allows() -> None:
    """exit code 2 視為 unexpected，log warn 但不 block（fail-open）。"""
    hook = CommandHook(command="exit 2", timeout_sec=5.0)
    out = await hook.run(_make_payload())

    assert out.continue_ is True
    assert out.decision is None


async def test_command_hook_timeout_blocks() -> None:
    """執行逾時應視為 block，reason 帶逾時訊息。"""
    hook = CommandHook(command="sleep 5", timeout_sec=0.1)
    out = await hook.run(_make_payload())

    assert out.decision == "block"
    assert out.reason is not None
    assert "逾時" in out.reason


# ---------------------------------------------------------------------------
# HttpHook — 2xx / 4xx / 5xx
# ---------------------------------------------------------------------------


def _mock_client(handler: Any) -> httpx.AsyncClient:
    """用 `httpx.MockTransport` 建立可注入到 HttpHook 的 async client。"""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=5.0)


async def test_http_hook_2xx_returns_allow() -> None:
    """2xx 視為 allow（HookOutput 預設值）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/hook"
        return httpx.Response(200, json={})

    async with _mock_client(handler) as client:
        hook = HttpHook(url="http://test/hook", client=client)
        out = await hook.run(_make_payload())

    assert hook.flavor is HookFlavor.HTTP
    assert out.continue_ is True
    assert out.decision is None


async def test_http_hook_2xx_with_json_body_merges() -> None:
    """2xx + body 是合法 HookOutput JSON 時應 merge。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"additional_context": "via http"})

    async with _mock_client(handler) as client:
        hook = HttpHook(url="http://test/hook", client=client)
        out = await hook.run(_make_payload())

    assert out.additional_context == "via http"


async def test_http_hook_4xx_blocks_with_body_reason() -> None:
    """4xx 視為 block，reason 取 response body。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden by policy")

    async with _mock_client(handler) as client:
        hook = HttpHook(url="http://test/hook", client=client)
        out = await hook.run(_make_payload())

    assert out.decision == "block"
    assert out.reason is not None
    assert "forbidden by policy" in out.reason


async def test_http_hook_5xx_warns_but_allows() -> None:
    """5xx 視為 unexpected，log warn 但不 block（fail-open）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream broken")

    async with _mock_client(handler) as client:
        hook = HttpHook(url="http://test/hook", client=client)
        out = await hook.run(_make_payload())

    assert out.continue_ is True
    assert out.decision is None


async def test_http_hook_body_is_json_of_payload() -> None:
    """HttpHook 應該以 payload 的 JSON 為 request body。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={})

    async with _mock_client(handler) as client:
        hook = HttpHook(url="http://test/hook", client=client)
        await hook.run(_make_payload(tool="search", agent="root"))

    assert captured["content_type"] == "application/json"
    assert '"tool_name": "search"' in captured["body"]
    assert '"agent_name": "root"' in captured["body"]


# ---------------------------------------------------------------------------
# PromptHook — stub
# ---------------------------------------------------------------------------


async def test_prompt_hook_run_raises_not_implemented() -> None:
    """PromptHook 仍是 stub — 呼叫 `run` 應該 raise `NotImplementedError`。"""
    hook = PromptHook(prompt_template="Is $ARGUMENTS dangerous?", model="haiku")

    assert hook.flavor is HookFlavor.PROMPT
    with pytest.raises(NotImplementedError):
        await hook.run(_make_payload())


# ---------------------------------------------------------------------------
# HookRegistry 接受 HookABC instance（透過 fire 跑通）
# ---------------------------------------------------------------------------


async def test_registry_accepts_hook_abc_instance() -> None:
    """`HookSpec.callback` 可放 `HookABC` instance；`fire(...)` 會走 `HookABC.run`。"""
    registry = HookRegistry()

    class _CountingHook(HookABC):
        flavor = HookFlavor.PYTHON

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, payload: Any) -> HookOutput:
            self.calls += 1
            return HookOutput(additional_context=f"calls={self.calls}")

    hook = _CountingHook()
    registry.register(HookSpec(event=HookEvent.PRE_TOOL_USE, callback=hook))

    agg = await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload(), tool_name="search")

    assert hook.calls == 1
    assert agg.additional_contexts == ["calls=1"]


# ---------------------------------------------------------------------------
# Decorator factory
# ---------------------------------------------------------------------------


async def test_decorator_factory_registers_without_matcher() -> None:
    """`@registry.pre_tool_use` 不帶括號時應註冊 default matcher。"""
    registry = HookRegistry()
    captured: list[str] = []

    @registry.pre_tool_use
    def cb(payload: Any) -> HookOutput:
        captured.append(payload.tool_name)
        return HookOutput()

    await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload("search"), tool_name="search")

    assert captured == ["search"]


async def test_decorator_factory_registers_with_matcher() -> None:
    """`@registry.pre_tool_use("Bash.*")` 應該只在 tool name 配 regex 時觸發。"""
    registry = HookRegistry()
    captured: list[str] = []

    @registry.pre_tool_use("Bash.*")
    def cb(payload: Any) -> HookOutput:
        captured.append(payload.tool_name)
        return HookOutput()

    # 不配 — 不該觸發
    await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload("search"), tool_name="search")
    assert captured == []

    # 配 — 該觸發
    await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload("BashRun"), tool_name="BashRun")
    assert captured == ["BashRun"]


async def test_decorator_factory_covers_all_events() -> None:
    """所有 HookEvent 都要有對應 decorator 屬性，避免漏 event。"""
    registry = HookRegistry()
    expected_attrs = [
        "pre_tool_use",
        "post_tool_use",
        "stop",
        "session_start",
        "user_prompt_submit",
        "permission_request",
        "agent_start",
        "agent_end",
        "handoff",
    ]
    for attr in expected_attrs:
        assert callable(getattr(registry, attr)), f"missing decorator: {attr}"


# ---------------------------------------------------------------------------
# Hook chain：多 hook 順序 + block 早停
# ---------------------------------------------------------------------------


async def test_hook_chain_runs_in_registration_order() -> None:
    """多個 hook 同 event 同 matcher，應依註冊順序執行。"""
    registry = HookRegistry()
    order: list[str] = []

    @registry.pre_tool_use
    def first(payload: Any) -> HookOutput:
        order.append("first")
        return HookOutput(additional_context="first-ctx")

    @registry.pre_tool_use
    def second(payload: Any) -> HookOutput:
        order.append("second")
        return HookOutput(additional_context="second-ctx")

    @registry.pre_tool_use
    def third(payload: Any) -> HookOutput:
        order.append("third")
        return HookOutput()

    agg = await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload(), tool_name="search")

    assert order == ["first", "second", "third"]
    assert agg.additional_contexts == ["first-ctx", "second-ctx"]


async def test_hook_chain_block_short_circuits() -> None:
    """chain 中某 hook 回 block 後，後續 hook 不應再執行。"""
    registry = HookRegistry()
    order: list[str] = []

    @registry.pre_tool_use
    def first(payload: Any) -> HookOutput:
        order.append("first")
        return HookOutput()

    @registry.pre_tool_use
    def second(payload: Any) -> HookOutput:
        order.append("second")
        return HookOutput(decision="block", reason="nope")

    @registry.pre_tool_use
    def third(payload: Any) -> HookOutput:
        order.append("third")
        return HookOutput()

    agg = await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload(), tool_name="search")

    assert order == ["first", "second"]  # third 不該跑
    assert agg.block is True
    assert agg.reason == "nope"


async def test_hook_chain_abort_short_circuits() -> None:
    """chain 中 hook 回 `continue_=False` 視為 abort，後續 hook 不應再執行。"""
    registry = HookRegistry()
    order: list[str] = []

    @registry.pre_tool_use
    def first(payload: Any) -> HookOutput:
        order.append("first")
        return HookOutput(continue_=False, stop_reason="time up")

    @registry.pre_tool_use
    def second(payload: Any) -> HookOutput:
        order.append("second")
        return HookOutput()

    agg = await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload(), tool_name="search")

    assert order == ["first"]
    assert agg.abort is True
    assert agg.stop_reason == "time up"


async def test_hook_chain_mixed_flavors() -> None:
    """chain 內可混用 PythonHook（callable）與 HookABC instance（command / http）。"""
    registry = HookRegistry()
    order: list[str] = []

    @registry.pre_tool_use
    def py(payload: Any) -> HookOutput:
        order.append("python")
        return HookOutput(additional_context="py-ctx")

    # CommandHook 一定 allow（exit 0）— 確認 HookABC 也能掛進 chain
    registry.register_hook(
        HookEvent.PRE_TOOL_USE,
        CommandHook(command="exit 0", timeout_sec=5.0),
    )

    @registry.pre_tool_use
    def tail(payload: Any) -> HookOutput:
        order.append("tail")
        return HookOutput()

    agg = await fire(registry, HookEvent.PRE_TOOL_USE, _make_payload(), tool_name="search")

    assert order == ["python", "tail"]
    assert agg.additional_contexts == ["py-ctx"]


# ---------------------------------------------------------------------------
# Event bus 整合 — hook chain 觸發時應 emit hook_fired
# ---------------------------------------------------------------------------


async def test_hook_fired_event_emitted_for_each_chain_step() -> None:
    """chain 每執行一個 hook，event bus 都要收到一筆 `hook_fired`（block 早停後不再 emit）。"""
    registry = HookRegistry()
    bus = EventBus()
    events: list[tuple[str, dict[str, Any]]] = []
    bus.on_any(lambda e: events.append((e.kind, dict(e.payload))))

    @registry.pre_tool_use
    def first(payload: Any) -> HookOutput:
        return HookOutput()

    @registry.pre_tool_use
    def second(payload: Any) -> HookOutput:
        return HookOutput(decision="block", reason="x")

    @registry.pre_tool_use
    def third(payload: Any) -> HookOutput:
        return HookOutput()

    await fire(
        registry,
        HookEvent.PRE_TOOL_USE,
        _make_payload(),
        tool_name="search",
        bus=bus,
    )

    fired = [p for kind, p in events if kind == "hook_fired"]
    assert len(fired) == 2  # third 已被早停
    assert fired[-1]["decision"] == "block"

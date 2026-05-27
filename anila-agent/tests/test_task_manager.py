"""P2-12 Task / background task tool 的 unit test。

涵蓋:

* ``TaskManager`` start / status / wait / cancel / list_tasks 基本流程。
* task callable raise → ``state=FAILED``,``error`` 寫上 type+msg。
* task 超時 → ``state=FAILED``,``error`` 以 ``timeout:`` 開頭。
* cancel 中途 task → ``state=CANCELLED``。
* ``register_task_type`` 拒絕 non-coroutine / 非法字元 / 重複註冊。
* 3 個 meta-tool (task_start / task_status / task_wait) JSON I/O 流程。
* tracing:每個 task 一條 trace,且 ``trace_id`` 寫進 ``task.metadata``。
* on_complete trigger callback 跟 P1-4 trigger 子系統互通(把 message
  queue 進 SessionContext)。
* meta-tool ``ToolMetadata`` 屬性符合 P0-2 AnilaTool 介面。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from anila_agent.core.hook_context import SessionContext
from anila_agent.core.task_manager import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    Task,
    TaskManager,
    TaskState,
)
from anila_agent.tools.base import get_metadata
from anila_agent.tools.task import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    MAX_WAIT_TIMEOUT_SECONDS,
    TASK_START_TOOL_NAME,
    TASK_STATUS_TOOL_NAME,
    TASK_WAIT_TOOL_NAME,
    _clear_task_type_registry,
    build_task_meta_tools,
    get_registered_task_types,
    register_task_type,
    unregister_task_type,
)
from anila_agent.tracing import Span, Trace, Tracer, TracingProcessor


# ---------------------------------------------------------------------------
# fixture / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    """每個 test 前後都把 process-wide task type registry 清乾淨。"""
    _clear_task_type_registry()
    yield
    _clear_task_type_registry()


class _CapturingTracingProcessor(TracingProcessor):
    """收集 tracer 事件以便斷言 trace 結構。"""

    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.spans: list[Span] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.traces.append(trace)

    def on_trace_end(self, trace: Trace) -> None:
        # trace 結束時不再加,因為 on_trace_start 已收
        pass

    def on_span_start(self, span: Span) -> None:
        self.spans.append(span)

    def on_span_end(self, span: Span) -> None:
        pass


async def _invoke_tool(tool: Any, args: dict[str, Any]) -> dict[str, Any]:
    """便利包:呼叫 FunctionTool 並把 JSON 字串 parse 回 dict。"""
    raw = await tool.on_invoke_tool(None, json.dumps(args))
    assert isinstance(raw, str)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# TaskState enum
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_state_terminal_set() -> None:
    """COMPLETED / FAILED / CANCELLED 為終態;PENDING / RUNNING 不是。"""
    assert TaskState.COMPLETED.is_terminal
    assert TaskState.FAILED.is_terminal
    assert TaskState.CANCELLED.is_terminal
    assert not TaskState.PENDING.is_terminal
    assert not TaskState.RUNNING.is_terminal


@pytest.mark.unit
def test_task_state_str_compatible() -> None:
    """TaskState 繼承 str,可直接跟字串比對(便於 JSON 序列化)。"""
    assert TaskState.RUNNING == "RUNNING"
    assert TaskState.COMPLETED.value == "COMPLETED"


# ---------------------------------------------------------------------------
# Task dataclass 預設值與序列化
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_defaults() -> None:
    """Task 預設值:PENDING、UUID task_id、空 metadata。"""
    t = Task(description="demo")
    assert t.state == TaskState.PENDING
    assert isinstance(t.task_id, str) and len(t.task_id) == 32
    assert t.started_at is None
    assert t.ended_at is None
    assert t.metadata == {}


@pytest.mark.unit
def test_task_to_dict_serializes_state_value() -> None:
    """Task.to_dict 把 state 序列化成字串 value(避免 JSON enum 問題)。"""
    t = Task(description="x", state=TaskState.RUNNING)
    payload = t.to_dict()
    assert payload["state"] == "RUNNING"
    assert payload["started_at"] is None


# ---------------------------------------------------------------------------
# TaskManager 基本 start / status / wait 流程
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_start_and_wait_completes() -> None:
    """task 正常完成:RUNNING → COMPLETED,result 與 progress=1.0 都寫好。"""
    manager = TaskManager()

    async def add(a: int, b: int) -> int:
        await asyncio.sleep(0)
        return a + b

    task_id = await manager.start("add 1+2", add, 1, b=2)
    task = await manager.wait(task_id, timeout=2.0)

    assert task.state == TaskState.COMPLETED
    assert task.result == 3
    assert task.progress == 1.0
    assert task.error is None
    assert task.started_at is not None
    assert task.ended_at is not None


@pytest.mark.unit
async def test_status_returns_running_then_completed() -> None:
    """task 進行中 status 應為 RUNNING;wait 後變 COMPLETED。"""
    manager = TaskManager()
    blocker = asyncio.Event()

    async def slow() -> str:
        await blocker.wait()
        return "done"

    task_id = await manager.start("slow", slow)
    # 給 scheduler 一次跑機會,讓 _run_task 進入 RUNNING
    await asyncio.sleep(0)
    assert (await manager.status(task_id)).state == TaskState.RUNNING

    blocker.set()
    task = await manager.wait(task_id, timeout=2.0)
    assert task.state == TaskState.COMPLETED
    assert task.result == "done"


@pytest.mark.unit
async def test_status_unknown_task_raises_keyerror() -> None:
    """未知 task_id status / wait / cancel 都 raise KeyError。"""
    manager = TaskManager()
    with pytest.raises(KeyError):
        await manager.status("nope")
    with pytest.raises(KeyError):
        await manager.wait("nope")
    with pytest.raises(KeyError):
        await manager.cancel("nope")


# ---------------------------------------------------------------------------
# 例外 → FAILED
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_failing_task_captures_error() -> None:
    """task callable raise → state=FAILED,error 包含 type+msg。"""
    manager = TaskManager()

    async def boom() -> None:
        raise RuntimeError("kaboom!")

    task_id = await manager.start("boom", boom)
    task = await manager.wait(task_id, timeout=1.0)

    assert task.state == TaskState.FAILED
    assert task.error is not None
    assert "RuntimeError" in task.error
    assert "kaboom" in task.error
    assert task.result is None


# ---------------------------------------------------------------------------
# timeout → FAILED with timeout error
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_timeout_marks_task_failed() -> None:
    """task 超時 → state=FAILED,error 以 'timeout:' 開頭。"""
    manager = TaskManager(default_timeout_seconds=0.05)

    async def sleeper() -> None:
        await asyncio.sleep(5.0)

    task_id = await manager.start("sleeper", sleeper)
    task = await manager.wait(task_id, timeout=1.0)
    assert task.state == TaskState.FAILED
    assert task.error is not None
    assert task.error.startswith("timeout:")


@pytest.mark.unit
async def test_start_override_timeout() -> None:
    """start() 的 timeout_seconds 應覆寫 manager 預設。"""
    manager = TaskManager(default_timeout_seconds=10.0)

    async def sleeper() -> None:
        await asyncio.sleep(5.0)

    task_id = await manager.start("sleeper", sleeper, timeout_seconds=0.05)
    task = await manager.wait(task_id, timeout=1.0)
    assert task.state == TaskState.FAILED
    assert task.error is not None
    assert "timeout" in task.error


# ---------------------------------------------------------------------------
# cancel → CANCELLED
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cancel_mid_run() -> None:
    """cancel 中途 task → state=CANCELLED,task_manager 不 propagate CancelledError。"""
    manager = TaskManager()
    started = asyncio.Event()

    async def long_running() -> None:
        started.set()
        await asyncio.sleep(60.0)

    task_id = await manager.start("long", long_running)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task = await manager.cancel(task_id)
    assert task.state == TaskState.CANCELLED
    assert task.error == "cancelled"


@pytest.mark.unit
async def test_cancel_completed_task_is_noop() -> None:
    """cancel 已終態 task 不應改變 state。"""
    manager = TaskManager()

    async def fast() -> int:
        return 1

    task_id = await manager.start("fast", fast)
    await manager.wait(task_id, timeout=1.0)
    completed = await manager.status(task_id)
    assert completed.state == TaskState.COMPLETED

    cancelled = await manager.cancel(task_id)
    assert cancelled.state == TaskState.COMPLETED
    assert cancelled.result == 1


# ---------------------------------------------------------------------------
# list_tasks + state filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_list_tasks_filter() -> None:
    """list_tasks 預設回全部;state_filter 應只回該 state。"""
    manager = TaskManager()

    async def ok() -> str:
        return "ok"

    async def bad() -> None:
        raise ValueError("nope")

    a = await manager.start("ok", ok)
    b = await manager.start("bad", bad)
    await manager.wait(a, timeout=1.0)
    await manager.wait(b, timeout=1.0)

    all_tasks = await manager.list_tasks()
    assert {t.task_id for t in all_tasks} == {a, b}

    completed = await manager.list_tasks(state_filter=TaskState.COMPLETED)
    assert [t.task_id for t in completed] == [a]
    failed = await manager.list_tasks(state_filter=TaskState.FAILED)
    assert [t.task_id for t in failed] == [b]


# ---------------------------------------------------------------------------
# tracing — 每個 task 一條 trace
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_each_task_gets_its_own_trace() -> None:
    """task 完成後,tracer 應該收到一條 trace,task.metadata 有 trace_id。"""
    tracer = Tracer()
    proc = _CapturingTracingProcessor()
    tracer.register_processor(proc)

    manager = TaskManager(tracer=tracer)

    async def hello() -> str:
        return "hi"

    task_id = await manager.start(
        "say hi",
        hello,
        metadata={"type": "demo"},
    )
    task = await manager.wait(task_id, timeout=1.0)

    assert task.state == TaskState.COMPLETED
    assert len(proc.traces) == 1
    trace = proc.traces[0]
    assert trace.name == "trace.task.demo"
    assert trace.metadata["task_id"] == task_id
    # task.metadata 必定有 trace_id
    assert task.metadata.get("trace_id") == trace.trace_id


@pytest.mark.unit
async def test_failing_task_still_closes_trace() -> None:
    """task fail 不應影響 trace 收尾;trace.end_time 仍會被填。"""
    tracer = Tracer()
    proc = _CapturingTracingProcessor()
    tracer.register_processor(proc)

    manager = TaskManager(tracer=tracer)

    async def boom() -> None:
        raise RuntimeError("x")

    task_id = await manager.start("boom", boom, metadata={"type": "demo"})
    task = await manager.wait(task_id, timeout=1.0)

    assert task.state == TaskState.FAILED
    assert len(proc.traces) == 1
    assert proc.traces[0].end_time is not None


# ---------------------------------------------------------------------------
# on_complete trigger callback — 跟 P1-4 trigger 子系統互通
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_on_complete_fires_with_task_snapshot() -> None:
    """on_complete 在 task 終態時 fire,且能拿到 task snapshot。"""
    captured: list[Task] = []

    async def on_complete(task: Task) -> None:
        captured.append(task)

    manager = TaskManager(on_complete=on_complete)

    async def ok() -> int:
        return 42

    task_id = await manager.start("ok", ok)
    await manager.wait(task_id, timeout=1.0)

    assert len(captured) == 1
    assert captured[0].task_id == task_id
    assert captured[0].state == TaskState.COMPLETED
    assert captured[0].result == 42


@pytest.mark.unit
async def test_on_complete_can_inject_message_into_session() -> None:
    """on_complete 可以呼 session.queue_message,對接 P1-4 trigger 流程
    (對應 user case「studio job 完成自動續推」)。"""
    session = SessionContext(session_id="sess-1")

    async def on_complete(task: Task) -> None:
        session.queue_message(
            f"[task] {task.description} 完成: {task.result}",
            metadata={"task_id": task.task_id, "state": task.state.value},
        )

    manager = TaskManager(on_complete=on_complete)

    async def ok() -> str:
        return "done"

    task_id = await manager.start("studio job", ok)
    await manager.wait(task_id, timeout=1.0)

    msg = session.message_queue.get_nowait()
    assert msg["content"] == "[task] studio job 完成: done"
    assert msg["metadata"]["task_id"] == task_id
    assert msg["metadata"]["state"] == "COMPLETED"


@pytest.mark.unit
async def test_on_complete_exception_does_not_break_state() -> None:
    """on_complete raise 時不應改變 task state。"""

    async def bad_callback(task: Task) -> None:
        raise RuntimeError("callback crashed")

    manager = TaskManager(on_complete=bad_callback)

    async def ok() -> int:
        return 1

    task_id = await manager.start("ok", ok)
    task = await manager.wait(task_id, timeout=1.0)
    assert task.state == TaskState.COMPLETED
    assert task.result == 1


# ---------------------------------------------------------------------------
# register_task_type registry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_register_task_type_rejects_non_coroutine() -> None:
    """非 async function 應 raise TypeError。"""

    def sync_fn() -> None:
        return None

    with pytest.raises(TypeError):
        register_task_type("sync", sync_fn)  # type: ignore[arg-type]


@pytest.mark.unit
def test_register_task_type_rejects_invalid_name() -> None:
    """空字串 / 含特殊字元應被拒。"""

    async def ok() -> None:
        return None

    with pytest.raises(ValueError):
        register_task_type("", ok)
    with pytest.raises(ValueError):
        register_task_type("bad name!", ok)
    with pytest.raises(ValueError):
        register_task_type("rm -rf /", ok)


@pytest.mark.unit
def test_register_task_type_duplicate_requires_overwrite() -> None:
    """同名重複註冊預設失敗;overwrite=True 才放行。"""

    async def a() -> int:
        return 1

    async def b() -> int:
        return 2

    register_task_type("dup", a)
    with pytest.raises(ValueError):
        register_task_type("dup", b)
    register_task_type("dup", b, overwrite=True)
    assert "dup" in get_registered_task_types()


@pytest.mark.unit
def test_unregister_task_type_idempotent() -> None:
    """unregister 不存在 key 應 no-op,不 raise。"""

    async def ok() -> None:
        return None

    register_task_type("x", ok)
    unregister_task_type("x")
    unregister_task_type("x")  # second call no-op
    assert "x" not in get_registered_task_types()


# ---------------------------------------------------------------------------
# 3 個 meta-tool integration with P0-2 AnilaTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_task_meta_tools_returns_three() -> None:
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    names = {t.name for t in tools}
    assert names == {
        TASK_START_TOOL_NAME,
        TASK_STATUS_TOOL_NAME,
        TASK_WAIT_TOOL_NAME,
    }


@pytest.mark.unit
def test_meta_tools_have_correct_metadata() -> None:
    """meta-tool 必須帶 AnilaTool metadata:category=task,非 deferred。"""
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    for tool in tools:
        meta = get_metadata(tool)
        assert meta.category == "task"
        assert meta.is_deferred is False
        assert meta.cost_estimate == "free"


# ---------------------------------------------------------------------------
# meta-tool: task_start 流程
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_task_start_unknown_type_fails_closed() -> None:
    """未註冊 type → ok=False,error=unknown_task_type,且列出 available types。"""
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)

    payload = await _invoke_tool(
        start_tool,
        {"type": "not_registered", "description": "x"},
    )
    assert payload["ok"] is False
    assert payload["error"] == "unknown_task_type"
    assert payload["available"] == []


@pytest.mark.unit
async def test_task_start_missing_args_fails() -> None:
    """type / description 必填。"""
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)

    p1 = await _invoke_tool(start_tool, {"description": "no type"})
    assert p1["error"] == "missing_type"

    p2 = await _invoke_tool(start_tool, {"type": "x"})
    assert p2["error"] == "missing_description"


@pytest.mark.unit
async def test_task_start_invokes_registered_callable() -> None:
    """task_start → registered callable → 拿 task_id,之後 status 可查到 COMPLETED。"""

    async def add(a: int, b: int) -> int:
        return a + b

    register_task_type("add", add)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)
    status_tool = next(t for t in tools if t.name == TASK_STATUS_TOOL_NAME)

    start_payload = await _invoke_tool(
        start_tool,
        {
            "type": "add",
            "description": "add 1+2",
            "args": [1, 2],
        },
    )
    assert start_payload["ok"] is True
    task_id = start_payload["task_id"]

    # 等 task 跑完
    await manager.wait(task_id, timeout=1.0)
    status_payload = await _invoke_tool(status_tool, {"task_id": task_id})
    assert status_payload["ok"] is True
    assert status_payload["state"] == "COMPLETED"
    assert status_payload["result"] == 3


@pytest.mark.unit
async def test_task_start_with_kwargs_and_metadata() -> None:
    """args + kwargs + metadata 全部正確 forward。"""

    async def greet(name: str, greeting: str = "hello") -> str:
        return f"{greeting}, {name}"

    register_task_type("greet", greet)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)

    payload = await _invoke_tool(
        start_tool,
        {
            "type": "greet",
            "description": "greet alice",
            "args": ["alice"],
            "kwargs": {"greeting": "hi"},
            "metadata": {"source": "test"},
        },
    )
    task_id = payload["task_id"]
    task = await manager.wait(task_id, timeout=1.0)
    assert task.result == "hi, alice"
    assert task.metadata["type"] == "greet"
    assert task.metadata["source"] == "test"


@pytest.mark.unit
async def test_task_start_invalid_args_type() -> None:
    """args 必須是 list,kwargs 必須是 dict。"""

    async def noop() -> None:
        return None

    register_task_type("noop", noop)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)

    p1 = await _invoke_tool(
        start_tool,
        {"type": "noop", "description": "x", "args": "not a list"},
    )
    assert p1["error"] == "args_must_be_list"

    p2 = await _invoke_tool(
        start_tool,
        {"type": "noop", "description": "x", "kwargs": ["not", "dict"]},
    )
    assert p2["error"] == "kwargs_must_be_object"


@pytest.mark.unit
async def test_task_start_invalid_json_returns_error() -> None:
    """非合法 JSON args 應回 invalid_json,不該爆 exception。"""
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)

    raw = await start_tool.on_invoke_tool(None, "{not json")
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_json"


# ---------------------------------------------------------------------------
# meta-tool: task_status / task_wait 流程
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_task_status_unknown_task_id() -> None:
    """未知 task_id status 應 ok=False。"""
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    status_tool = next(t for t in tools if t.name == TASK_STATUS_TOOL_NAME)

    payload = await _invoke_tool(status_tool, {"task_id": "nope"})
    assert payload["ok"] is False
    assert payload["error"] == "task_not_found"


@pytest.mark.unit
async def test_task_wait_short_timeout_returns_running() -> None:
    """task 還沒完成時 wait 應在 timeout 後回 snapshot(state=RUNNING),
    task 仍在背景跑,後續可繼續 wait。"""

    blocker = asyncio.Event()

    async def slow() -> str:
        await blocker.wait()
        return "done"

    register_task_type("slow", slow)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)
    wait_tool = next(t for t in tools if t.name == TASK_WAIT_TOOL_NAME)

    p = await _invoke_tool(start_tool, {"type": "slow", "description": "s"})
    task_id = p["task_id"]

    waited = await _invoke_tool(
        wait_tool, {"task_id": task_id, "max_wait_seconds": 0.05}
    )
    assert waited["ok"] is True
    assert waited["state"] == "RUNNING"  # 短 wait 後仍在跑

    blocker.set()
    final = await _invoke_tool(
        wait_tool, {"task_id": task_id, "max_wait_seconds": 1.0}
    )
    assert final["state"] == "COMPLETED"
    assert final["result"] == "done"


@pytest.mark.unit
async def test_task_wait_invalid_max_wait() -> None:
    """max_wait_seconds <=0 或非數字應 ok=False。"""

    async def ok() -> None:
        return None

    register_task_type("ok", ok)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)
    wait_tool = next(t for t in tools if t.name == TASK_WAIT_TOOL_NAME)

    p = await _invoke_tool(start_tool, {"type": "ok", "description": "x"})
    tid = p["task_id"]

    bad_num = await _invoke_tool(
        wait_tool, {"task_id": tid, "max_wait_seconds": "abc"}
    )
    assert bad_num["error"] == "invalid_max_wait_seconds"

    nonpos = await _invoke_tool(
        wait_tool, {"task_id": tid, "max_wait_seconds": 0}
    )
    assert nonpos["error"] == "max_wait_seconds_must_be_positive"


@pytest.mark.unit
async def test_task_wait_caps_at_max() -> None:
    """LLM 傳 max_wait_seconds > 上限應自動 capped 至 MAX_WAIT_TIMEOUT_SECONDS。
    這裡用 task 立即完成,所以實際 wait 時間遠低於 cap;斷言只看 cap 不會 reject。"""

    async def fast() -> int:
        return 7

    register_task_type("fast", fast)
    manager = TaskManager()
    tools = build_task_meta_tools(manager)
    start_tool = next(t for t in tools if t.name == TASK_START_TOOL_NAME)
    wait_tool = next(t for t in tools if t.name == TASK_WAIT_TOOL_NAME)

    p = await _invoke_tool(start_tool, {"type": "fast", "description": "x"})
    tid = p["task_id"]

    payload = await _invoke_tool(
        wait_tool,
        {"task_id": tid, "max_wait_seconds": MAX_WAIT_TIMEOUT_SECONDS + 9999},
    )
    assert payload["ok"] is True
    assert payload["state"] == "COMPLETED"
    assert payload["result"] == 7


# ---------------------------------------------------------------------------
# 預設 timeout 常數一致
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_constants_sane() -> None:
    """常數預設值應該合理(對應 spec 描述)。"""
    assert DEFAULT_TASK_TIMEOUT_SECONDS == 300.0
    assert DEFAULT_WAIT_TIMEOUT_SECONDS == 60.0
    assert MAX_WAIT_TIMEOUT_SECONDS == 300.0

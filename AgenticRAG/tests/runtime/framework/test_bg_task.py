"""Sprint 6 tests — BG_TASK runtime + tool actions + Runner integration."""

from __future__ import annotations

import asyncio

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    Agent,
    BgTaskHandle,
    BgTaskRunner,
    BgTaskState,
    ChatCompletionResponse,
    FileSink,
    FinishReason,
    MemorySink,
    Message,
    Runner,
    ToolCall,
    Usage,
    make_bg_task_actions,
    make_cancel_bg_task_action,
    make_check_bg_task_action,
    make_list_bg_tasks_action,
)
from agentic_rag.runtime.framework.exceptions import UserError


# ── Test scaffolding ─────────────────────────────────────────────────


def _ctx(params=None) -> ActionContext:
    return ActionContext(
        run_id="r", agent_name="a", params=params or {}, history=()
    )


def _bg_action(handler, *, name: str = "ingest") -> Action:
    return Action(
        name=name,
        description="bg work",
        kind=ActionKind.BG_TASK,
        handler=handler,
    )


# ── MemorySink ───────────────────────────────────────────────────────


def test_memory_sink_writes_and_reads() -> None:
    s = MemorySink(max_chars=1000)
    s.write("hello ")
    s.write("world")
    assert s.read() == "hello world"


def test_memory_sink_trims_oldest_when_capped() -> None:
    s = MemorySink(max_chars=10)
    s.write("aaaaa")
    s.write("bbbbb")
    s.write("ccccc")
    out = s.read()
    # Cap is 10; final must be the most recent 10 chars.
    assert len(out) <= 10
    assert "ccccc" in out


def test_memory_sink_tail_chars() -> None:
    s = MemorySink(max_chars=1000)
    s.write("0123456789ABCDEF")
    assert s.read(tail_chars=4) == "CDEF"


def test_memory_sink_invalid_max_chars_raises() -> None:
    with pytest.raises(UserError):
        MemorySink(max_chars=0)


# ── FileSink ─────────────────────────────────────────────────────────


def test_file_sink_writes_to_disk(tmp_path) -> None:
    sink = FileSink(tmp_path, "task_xyz")
    sink.write("line one\n")
    sink.write("line two\n")
    sink.close()
    text = (tmp_path / "task_xyz.log").read_text(encoding="utf-8")
    assert text == "line one\nline two\n"


def test_file_sink_read_returns_full_text(tmp_path) -> None:
    sink = FileSink(tmp_path, "t1")
    sink.write("aaaaaaaaaa")
    assert sink.read() == "aaaaaaaaaa"
    assert sink.read(tail_chars=3) == "aaa"


def test_file_sink_read_missing_returns_empty(tmp_path) -> None:
    sink = FileSink(tmp_path, "never-written")
    assert sink.read() == ""


# ── BgTaskRunner spawn ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_returns_handle_immediately() -> None:
    started_event = asyncio.Event()
    finish_event = asyncio.Event()

    async def slow_work(ctx, write_progress):
        started_event.set()
        await finish_event.wait()
        write_progress("done")
        return {"rows_processed": 10}

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(slow_work), _ctx())
    # Spawn is non-blocking — handle returned before slow_work finishes.
    assert handle.task_id.startswith("bg_")
    assert not handle.is_done()
    await started_event.wait()  # confirm async task picked up
    assert handle.state in (BgTaskState.PENDING, BgTaskState.RUNNING)
    finish_event.set()
    await runner.wait(handle.task_id)
    assert handle.state is BgTaskState.COMPLETED
    assert handle.result == {"rows_processed": 10}


@pytest.mark.asyncio
async def test_spawn_writes_progress_to_sink() -> None:
    async def streamer(ctx, write_progress):
        for i in range(3):
            write_progress(f"line {i}\n")
        return None

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(streamer), _ctx())
    await runner.wait(handle.task_id)
    assert handle.state is BgTaskState.COMPLETED
    assert handle.output_tail() == "line 0\nline 1\nline 2\n"
    assert handle.progress_chars == len("line 0\nline 1\nline 2\n")


@pytest.mark.asyncio
async def test_spawn_handler_failure_records_error() -> None:
    async def boom(ctx, write_progress):
        write_progress("about to crash\n")
        raise RuntimeError("disk full")

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(boom), _ctx())
    await runner.wait(handle.task_id)
    assert handle.state is BgTaskState.FAILED
    assert handle.error is not None
    assert "disk full" in str(handle.error)
    # Pre-crash output preserved.
    assert "about to crash" in handle.output_tail()


@pytest.mark.asyncio
async def test_spawn_legacy_single_arg_handler_via_metadata() -> None:
    """Single-arg handlers can still write progress via ctx.metadata."""

    async def legacy(ctx):
        write = ctx.metadata["_bg_write_progress"]
        write("legacy progress\n")
        return "ok"

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(legacy), _ctx())
    await runner.wait(handle.task_id)
    assert handle.result == "ok"
    assert "legacy progress" in handle.output_tail()


@pytest.mark.asyncio
async def test_cancel_signals_handler_and_marks_cancelled() -> None:
    async def long_running(ctx, write_progress):
        cancel = ctx.metadata["_bg_cancel_signal"]
        for _ in range(100):
            if cancel.is_set():
                write_progress("aborted\n")
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        return "should not reach"

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(long_running), _ctx())
    await asyncio.sleep(0.02)
    assert runner.cancel(handle.task_id) is True
    await runner.wait(handle.task_id)
    assert handle.state is BgTaskState.CANCELLED


@pytest.mark.asyncio
async def test_cancel_returns_false_when_already_done() -> None:
    async def quick(ctx, write_progress):
        return "ok"

    runner = BgTaskRunner()
    handle = runner.spawn(_bg_action(quick), _ctx())
    await runner.wait(handle.task_id)
    assert runner.cancel(handle.task_id) is False


@pytest.mark.asyncio
async def test_spawn_with_file_sink(tmp_path) -> None:
    async def writer(ctx, write_progress):
        write_progress("written to disk\n")
        return None

    runner = BgTaskRunner(output_dir=tmp_path, default_sink="file")
    handle = runner.spawn(_bg_action(writer), _ctx())
    await runner.wait(handle.task_id)
    assert handle.output_path == str(tmp_path / f"{handle.task_id}.log")
    assert "written to disk" in (tmp_path / f"{handle.task_id}.log").read_text("utf-8")


def test_runner_rejects_sync_tool_kind() -> None:
    async def h(ctx, w):
        return None

    sync_action = Action(
        name="x", description="", kind=ActionKind.SYNC_TOOL, handler=h
    )
    runner = BgTaskRunner()
    with pytest.raises(UserError, match="only spawns BG_TASK"):
        runner.spawn(sync_action, _ctx())


def test_runner_constructor_validates_default_sink() -> None:
    with pytest.raises(UserError):
        BgTaskRunner(default_sink="bogus")
    with pytest.raises(UserError):
        BgTaskRunner(default_sink="file")  # missing output_dir


# ── BgTaskHandle ─────────────────────────────────────────────────────


def test_handle_to_summary_serialises_cleanly() -> None:
    h = BgTaskHandle(action_name="ingest")
    summary = h.to_summary()
    assert summary["task_id"] == h.task_id
    assert summary["action_name"] == "ingest"
    assert summary["state"] == "pending"
    assert summary["is_done"] is False
    assert summary["error"] is None


# ── Runner integration ──────────────────────────────────────────────


class _ScriptedProvider:
    def __init__(self, responses):
        self._scripted = list(responses)

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kw):
        raise NotImplementedError


def _resp(text="", tool_calls=(), finish=FinishReason.STOP):
    return ChatCompletionResponse(
        message=Message.assistant(content=text, tool_calls=tool_calls),
        usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
        finish_reason=finish,
    )


@pytest.mark.asyncio
async def test_runner_dispatches_bg_task_returns_handle_immediately() -> None:
    """Runner doesn't await BG_TASK handler — returns the handle and continues."""
    finish_event = asyncio.Event()

    async def slow(ctx, write_progress):
        await finish_event.wait()
        return "done"

    bg_action = Action(
        name="ingest", description="", kind=ActionKind.BG_TASK, handler=slow
    )
    tc = ToolCall(id="c1", name="ingest", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="kicked off the ingest"),
        ]
    )
    agent = Agent(
        name="ops", instructions="", provider=provider, model="m",
        actions=[bg_action],
    )
    bg_runner = BgTaskRunner()
    runner = Runner(bg_task_runner=bg_runner)

    # Run completes WITHOUT waiting for the BG task.
    result = await runner.run(agent, "kick off ingest")
    assert result.final_output == "kicked off the ingest"
    # The BG task is still in flight.
    handles = bg_runner.list_handles()
    assert len(handles) == 1
    assert handles[0].state in (BgTaskState.PENDING, BgTaskState.RUNNING)
    # Let it finish for cleanup.
    finish_event.set()
    await bg_runner.wait(handles[0].task_id)


@pytest.mark.asyncio
async def test_runner_lazy_constructs_default_bg_runner() -> None:
    """Runner() with no kwargs gets an in-memory BgTaskRunner on demand."""

    async def quick(ctx, write_progress):
        return "fast"

    bg_action = Action(
        name="quick", description="", kind=ActionKind.BG_TASK, handler=quick
    )
    tc = ToolCall(id="c1", name="quick", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="ok"),
        ]
    )
    agent = Agent(
        name="a", instructions="", provider=provider, model="m", actions=[bg_action]
    )
    runner = Runner()
    await runner.run(agent, "go")
    assert runner.bg_task_runner is not None
    handles = runner.bg_task_runner.list_handles()
    assert len(handles) == 1


# ── Tool actions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_bg_task_action_returns_state_and_output_tail() -> None:
    async def streamer(ctx, write_progress):
        write_progress("step1\n")
        write_progress("step2\n")
        return {"steps": 2}

    bg_runner = BgTaskRunner()
    handle = bg_runner.spawn(_bg_action(streamer), _ctx())
    await bg_runner.wait(handle.task_id)

    check = make_check_bg_task_action(bg_runner)
    result = await check.handler(_ctx({"task_id": handle.task_id}))
    assert not result.is_error
    assert result.output["state"] == "completed"
    assert "step1" in result.output["output_tail"]


@pytest.mark.asyncio
async def test_check_bg_task_action_unknown_id_errors() -> None:
    bg_runner = BgTaskRunner()
    check = make_check_bg_task_action(bg_runner)
    result = await check.handler(_ctx({"task_id": "missing"}))
    assert result.is_error
    assert "unknown" in result.error


@pytest.mark.asyncio
async def test_cancel_bg_task_action() -> None:
    async def long_running(ctx, write_progress):
        cancel = ctx.metadata["_bg_cancel_signal"]
        for _ in range(100):
            if cancel.is_set():
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)

    bg_runner = BgTaskRunner()
    handle = bg_runner.spawn(_bg_action(long_running), _ctx())
    await asyncio.sleep(0.02)

    cancel_action = make_cancel_bg_task_action(bg_runner)
    result = await cancel_action.handler(_ctx({"task_id": handle.task_id}))
    assert not result.is_error
    assert result.output["cancelled"] is True
    await bg_runner.wait(handle.task_id)
    assert handle.state is BgTaskState.CANCELLED


@pytest.mark.asyncio
async def test_list_bg_tasks_action_with_state_filter() -> None:
    async def quick(ctx, w):
        return "ok"

    bg_runner = BgTaskRunner()
    h1 = bg_runner.spawn(_bg_action(quick, name="a"), _ctx())
    h2 = bg_runner.spawn(_bg_action(quick, name="b"), _ctx())
    await bg_runner.wait(h1.task_id)
    await bg_runner.wait(h2.task_id)

    list_action = make_list_bg_tasks_action(bg_runner)
    all_result = await list_action.handler(_ctx({}))
    assert all_result.output["count"] == 2

    completed_result = await list_action.handler(_ctx({"state": "completed"}))
    assert completed_result.output["count"] == 2

    pending_result = await list_action.handler(_ctx({"state": "pending"}))
    assert pending_result.output["count"] == 0


def test_make_bg_task_actions_returns_three_actions() -> None:
    bg_runner = BgTaskRunner()
    actions = make_bg_task_actions(bg_runner)
    names = sorted(a.name for a in actions)
    assert names == ["cancel_bg_task", "check_bg_task", "list_bg_tasks"]

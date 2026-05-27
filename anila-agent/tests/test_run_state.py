"""P1-8 RunState HITL pause/resume + tool approval — unit tests。

驗證項目:
- ``RunState.to_json`` / ``from_json`` 雙向 roundtrip(含 datetime + nested objects)。
- 序列化是 deterministic(同 state → 同 bytes)。
- :class:`HITLController` 的 4 種 PauseReason 進入 / 離開流程。
- :class:`JsonFileStateStore` save / load / delete / list_run_ids。
- approval REPL(用 monkeypatch input 模擬一連串指令)。
- streaming integration(``pause_streaming_if_needed`` + paused state 不 yield event)。
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from anila_agent.cli.approval_repl import ReplResult, run_approval_repl
from anila_agent.core.concurrency import ToolCall
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import HookRegistry
from anila_agent.core.run_state import (
    HITLController,
    JsonFileStateStore,
    PauseReason,
    RunState,
    pause_streaming_if_needed,
)
from anila_agent.core.streaming import (
    AnilaStreamRunner,
    RunItemStreamEvent,
    StreamChunk,
    StreamEvent,
)

# ---------------------------------------------------------------------------
# RunState 基本 lifecycle / dataclass 行為
# ---------------------------------------------------------------------------


def _make_paused_state() -> RunState:
    """共用 fixture builder:建一個 paused_for_approval state 含 2 個 pending。"""
    state = RunState.create(
        agent_name="researcher",
        messages=[
            {"role": "user", "content": "find papers about RLHF"},
            {"role": "assistant", "content": "I will search."},
        ],
        metadata={"trace_id": "trace-xyz"},
    )
    HITLController().pause(
        state,
        PauseReason.TOOL_APPROVAL,
        pending_tool_calls=[
            ToolCall(name="web_search", args={"q": "RLHF"}, call_id="c1"),
            ToolCall(name="write_file", args={"path": "/tmp/r.md"}, call_id="c2"),
        ],
    )
    return state


def test_create_defaults_status_running_and_uuid_run_id() -> None:
    s = RunState.create("agent-x")
    assert s.status == "running"
    assert s.agent_name == "agent-x"
    # uuid4 hex 是 32 字元純 hex。
    assert len(s.run_id) == 32
    assert all(c in "0123456789abcdef" for c in s.run_id)
    assert s.started_at.tzinfo is not None  # 必須 tz-aware


def test_is_paused_and_is_terminal_flags() -> None:
    s = RunState.create("a")
    assert not s.is_paused
    assert not s.is_terminal
    s.status = "paused_for_approval"
    assert s.is_paused
    s.status = "paused_for_input"
    assert s.is_paused
    s.status = "completed"
    assert not s.is_paused
    assert s.is_terminal
    s.status = "failed"
    assert s.is_terminal


def test_find_pending_returns_matching_or_none() -> None:
    s = _make_paused_state()
    assert s.find_pending("c1") is not None
    assert s.find_pending("c1").name == "web_search"
    assert s.find_pending("nope") is None


# ---------------------------------------------------------------------------
# serialization roundtrip + deterministic
# ---------------------------------------------------------------------------


def test_to_json_from_json_roundtrip_preserves_all_fields() -> None:
    original = _make_paused_state()
    original.metadata["nested"] = {"k1": [1, 2, 3], "k2": "中文"}
    raw = original.to_json()
    restored = RunState.from_json(raw)

    assert restored.run_id == original.run_id
    assert restored.agent_name == original.agent_name
    assert restored.status == original.status
    assert restored.messages == original.messages
    assert restored.approved_tool_call_ids == original.approved_tool_call_ids
    assert restored.denied_tool_call_ids == original.denied_tool_call_ids
    assert restored.started_at == original.started_at
    assert restored.paused_at == original.paused_at
    assert restored.pause_reason == original.pause_reason
    assert restored.metadata == original.metadata
    # ToolCall round-trip(包括 args 內容)
    assert len(restored.pending_tool_calls) == 2
    assert restored.pending_tool_calls[0] == original.pending_tool_calls[0]
    assert restored.pending_tool_calls[1] == original.pending_tool_calls[1]


def test_to_json_is_deterministic_same_state_same_bytes() -> None:
    s = _make_paused_state()
    # 在 metadata 插入不同 key insert 順序,確認 sort_keys 把它打平。
    s.metadata = {}
    s.metadata["zz"] = 1
    s.metadata["aa"] = 2
    raw_a = s.to_json()
    # 重新 round-trip 後再 to_json,bytes 應完全相同。
    s2 = RunState.from_json(raw_a)
    raw_b = s2.to_json()
    assert raw_a == raw_b
    # 另一個內容相同但 dict 插入順序不同的 metadata 也產生相同 bytes。
    s3 = RunState.from_json(raw_a)
    s3.metadata = {"aa": 2, "zz": 1}  # 不同插入順序
    assert s3.to_json() == raw_a


def test_to_json_serializes_datetime_with_timezone() -> None:
    s = RunState.create("a")
    s.started_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    raw = s.to_json()
    assert '"started_at":"2026-01-02T03:04:05+00:00"' in raw


def test_from_json_rejects_invalid_status() -> None:
    s = RunState.create("a")
    raw = s.to_json().replace('"status":"running"', '"status":"bogus"')
    with pytest.raises(ValueError, match="invalid status"):
        RunState.from_json(raw)


def test_from_json_rejects_invalid_pause_reason() -> None:
    s = _make_paused_state()
    raw = s.to_json().replace(
        '"pause_reason":"tool_approval"',
        '"pause_reason":"made_up_reason"',
    )
    with pytest.raises(ValueError, match="invalid pause_reason"):
        RunState.from_json(raw)


def test_from_json_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        RunState.from_json("[]")


# ---------------------------------------------------------------------------
# HITLController — 4 種 PauseReason + approve / deny / resume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected_status",
    [
        (PauseReason.TOOL_APPROVAL, "paused_for_approval"),
        (PauseReason.USER_INPUT_REQUIRED, "paused_for_input"),
        (PauseReason.GUARDRAIL_TRIPWIRE, "paused_for_approval"),
        (PauseReason.BUDGET_EXCEEDED, "paused_for_approval"),
    ],
)
def test_pause_with_each_reason_sets_correct_status(
    reason: PauseReason, expected_status: str
) -> None:
    s = RunState.create("a")
    HITLController().pause(s, reason)
    assert s.status == expected_status
    assert s.pause_reason is reason
    assert s.paused_at is not None
    assert s.paused_at.tzinfo is not None


def test_pause_rejects_non_running_state() -> None:
    s = _make_paused_state()
    with pytest.raises(ValueError, match="只能從 running"):
        HITLController().pause(s, PauseReason.TOOL_APPROVAL)


def test_approve_moves_call_id_to_approved_and_removes_pending() -> None:
    s = _make_paused_state()
    HITLController().approve(s, "c1")
    assert "c1" in s.approved_tool_call_ids
    assert s.find_pending("c1") is None
    # c2 不受影響
    assert s.find_pending("c2") is not None


def test_approve_unknown_call_id_raises() -> None:
    s = _make_paused_state()
    with pytest.raises(ValueError, match="找不到 pending"):
        HITLController().approve(s, "nope")


def test_deny_records_reason_and_removes_pending() -> None:
    s = _make_paused_state()
    HITLController().deny(s, "c2", "user revoked write access")
    assert "c2" in s.denied_tool_call_ids
    assert s.deny_reasons["c2"] == "user revoked write access"
    assert s.find_pending("c2") is None


def test_deny_unknown_call_id_raises() -> None:
    s = _make_paused_state()
    with pytest.raises(ValueError, match="找不到 pending"):
        HITLController().deny(s, "nope", "reason")


def test_provide_input_only_in_paused_for_input() -> None:
    s = RunState.create("a")
    HITLController().pause(s, PauseReason.USER_INPUT_REQUIRED)
    HITLController().provide_input(s, {"role": "user", "content": "yes do it"})
    assert s.messages[-1]["content"] == "yes do it"


def test_provide_input_rejects_other_states() -> None:
    s = _make_paused_state()  # paused_for_approval
    with pytest.raises(ValueError, match="paused_for_input"):
        HITLController().provide_input(s, {"role": "user", "content": "x"})


def test_resume_succeeds_when_no_pending() -> None:
    s = _make_paused_state()
    ctrl = HITLController()
    ctrl.approve(s, "c1")
    ctrl.deny(s, "c2", "no thanks")
    ctrl.resume(s)
    assert s.status == "running"
    assert s.pause_reason is None
    assert s.paused_at is None


def test_resume_rejects_if_still_pending() -> None:
    s = _make_paused_state()
    HITLController().approve(s, "c1")
    with pytest.raises(ValueError, match="仍有 pending"):
        HITLController().resume(s)


def test_resume_rejects_running_state() -> None:
    s = RunState.create("a")
    with pytest.raises(ValueError, match="只能從 paused"):
        HITLController().resume(s)


def test_complete_writes_final_output_and_clears_pause() -> None:
    s = RunState.create("a")
    HITLController().complete(s, "done!")
    assert s.status == "completed"
    assert s.final_output == "done!"


def test_complete_rejects_terminal() -> None:
    s = RunState.create("a")
    HITLController().complete(s, "x")
    with pytest.raises(ValueError, match="terminal"):
        HITLController().complete(s, "y")


def test_fail_writes_failure_reason() -> None:
    s = RunState.create("a")
    HITLController().fail(s, "guardrail tripped")
    assert s.status == "failed"
    assert s.failure_reason == "guardrail tripped"


def test_fail_rejects_terminal() -> None:
    s = RunState.create("a")
    HITLController().complete(s, "x")
    with pytest.raises(ValueError, match="terminal"):
        HITLController().fail(s, "boom")


# ---------------------------------------------------------------------------
# JsonFileStateStore
# ---------------------------------------------------------------------------


def test_store_creates_dir_on_init(tmp_path: Path) -> None:
    target = tmp_path / "states"
    assert not target.exists()
    JsonFileStateStore(target)
    assert target.is_dir()


def test_store_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "states")
    state = _make_paused_state()
    path = store.save(state)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == state.to_json()

    restored = store.load(state.run_id)
    assert restored.run_id == state.run_id
    assert restored.status == state.status
    assert restored.pending_tool_calls == state.pending_tool_calls


def test_store_load_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "states")
    with pytest.raises(FileNotFoundError):
        store.load("does-not-exist")


def test_store_delete_returns_true_then_false(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "states")
    state = RunState.create("a")
    store.save(state)
    assert store.delete(state.run_id) is True
    assert store.delete(state.run_id) is False


def test_store_list_run_ids_sorted(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "states")
    for name in ("zebra", "alpha", "mike"):
        s = RunState.create(name, run_id=name)
        store.save(s)
    assert store.list_run_ids() == ["alpha", "mike", "zebra"]


def test_store_rejects_unsafe_run_id(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "states")
    for bad in ("../escape", "a/b", "", ".", ".."):
        with pytest.raises(ValueError, match="不合法的 run_id"):
            store.path_for(bad)


def test_store_save_load_deterministic_bytes(tmp_path: Path) -> None:
    """同 state 內容 save 兩次,bytes 應完全相同。"""
    store = JsonFileStateStore(tmp_path / "states")
    state = _make_paused_state()
    path = store.save(state)
    bytes_a = path.read_bytes()

    # 把 state load 回來再 save 一次
    restored = store.load(state.run_id)
    store.save(restored)
    bytes_b = path.read_bytes()
    assert bytes_a == bytes_b


# ---------------------------------------------------------------------------
# Approval REPL
# ---------------------------------------------------------------------------


def _silent_console() -> Console:
    """產一個寫進 StringIO buffer 的 rich Console(測試用,不污染 stdout)。"""
    return Console(file=io.StringIO(), force_terminal=False, width=120)


def _scripted_input(lines: list[str]):
    """把指令清單做成 input_provider stub。"""
    it = iter(lines)

    def _ask(_prompt: str) -> str:
        return next(it)

    return _ask


def test_repl_help_then_quit_no_modification(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)

    result = run_approval_repl(
        state,
        store,
        input_provider=_scripted_input(["help", "status", "quit"]),
        console=_silent_console(),
    )
    assert isinstance(result, ReplResult)
    assert result.quit_requested is True
    assert result.resumed is False
    assert result.modified is False
    # state 還是 paused
    assert state.is_paused


def test_repl_approve_deny_continue_flow(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)

    result = run_approval_repl(
        state,
        store,
        input_provider=_scripted_input(
            [
                "approve c1",
                "deny c2 not safe",
                "continue",
            ]
        ),
        console=_silent_console(),
    )
    assert result.modified is True
    assert result.resumed is True
    assert result.quit_requested is False
    assert state.status == "running"
    assert "c1" in state.approved_tool_call_ids
    assert state.deny_reasons["c2"] == "not safe"
    # 確認檔案也被更新
    persisted = store.load(state.run_id)
    assert persisted.status == "running"


def test_repl_unknown_command_does_not_crash(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)
    result = run_approval_repl(
        state,
        store,
        input_provider=_scripted_input(["wat", "approve c1", "quit"]),
        console=_silent_console(),
    )
    assert result.modified is True  # approve 成功了
    assert result.quit_requested is True


def test_repl_invalid_args_do_not_modify_state(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)
    result = run_approval_repl(
        state,
        store,
        input_provider=_scripted_input(
            [
                "approve",  # 缺 call_id
                "deny c1",  # 缺 reason
                "approve unknownid",  # 找不到 pending
                "quit",
            ]
        ),
        console=_silent_console(),
    )
    assert result.modified is False
    assert state.is_paused
    assert state.find_pending("c1") is not None


def test_repl_continue_blocked_when_pending_still_present(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)
    result = run_approval_repl(
        state,
        store,
        input_provider=_scripted_input(["continue", "quit"]),
        console=_silent_console(),
    )
    assert result.resumed is False
    assert state.is_paused


def test_repl_eof_treated_as_quit(tmp_path: Path) -> None:
    """input_provider raise EOFError 視同 user quit。"""
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)

    def _ask(_prompt: str) -> str:
        raise EOFError

    result = run_approval_repl(
        state,
        store,
        input_provider=_ask,
        console=_silent_console(),
    )
    assert result.quit_requested is True
    assert result.modified is False


def test_repl_monkeypatch_builtins_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """確認預設 input_provider 走 builtins.input(用 monkeypatch 取代)。"""
    store = JsonFileStateStore(tmp_path)
    state = _make_paused_state()
    store.save(state)
    queue = iter(["approve c1", "approve c2", "continue"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(queue))
    result = run_approval_repl(
        state, store, console=_silent_console()
    )
    assert result.resumed is True
    assert state.approved_tool_call_ids == ["c1", "c2"]


# ---------------------------------------------------------------------------
# Streaming integration — pause_streaming_if_needed + paused state 不 yield
# ---------------------------------------------------------------------------


def test_pause_streaming_if_needed_returns_true_for_paused() -> None:
    paused = _make_paused_state()
    assert pause_streaming_if_needed(paused) is True
    running = RunState.create("a")
    assert pause_streaming_if_needed(running) is False
    assert pause_streaming_if_needed(None) is False


def test_pause_streaming_if_needed_for_terminal_states() -> None:
    s = RunState.create("a")
    HITLController().complete(s, "done")
    # completed 不算 paused,不應該停 yield
    assert pause_streaming_if_needed(s) is False


async def _list_to_async(chunks: list[StreamChunk]) -> AsyncIterator[StreamChunk]:
    """把 list 轉成 AsyncIterator,測試組 mock chunk 用。"""
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_streaming_pause_signal_blocks_final_output_yield() -> None:
    """若 caller 把 paused state 加到 ``stream_source`` 之後,
    runner 雖然會走完 chunk loop,但 caller 應該透過
    :func:`pause_streaming_if_needed` 決定不消費 final_output。

    本 test 模擬「stream 結束時 caller 檢查 state 為 paused → 丟掉 final_output
    event」的行為,確保 helper 與 streaming runner 配合可用。
    """
    registry = HookRegistry()
    bus = EventBus()
    runner = AnilaStreamRunner(registry, bus, default_agent_name="a")

    chunks: list[StreamChunk] = [
        {"kind": "raw_token", "delta": "hi"},
        {"kind": "agent_ended", "agent": "a", "output": "hello"},
    ]

    # 模擬:在 stream 結束前 state 被 HITL 暫停。
    state = RunState.create("a")
    HITLController().pause(
        state,
        PauseReason.TOOL_APPROVAL,
        pending_tool_calls=[ToolCall(name="x", call_id="cx")],
    )

    yielded: list[StreamEvent] = []
    async for ev in runner.run_streamed(_list_to_async(chunks), model="m"):
        # caller 在 final_output 之前判斷:若 state 已 paused,停止消費。
        if (
            isinstance(ev, RunItemStreamEvent)
            and ev.item_type == "final_output"
            and pause_streaming_if_needed(state)
        ):
            # 跟 spec 行為一致:不 yield final_output 給上層 consumer。
            break
        yielded.append(ev)

    # final_output 沒被消費 → yielded list 內不應該包含 item_type=="final_output"。
    types = [
        getattr(ev, "item_type", None)
        for ev in yielded
        if isinstance(ev, RunItemStreamEvent)
    ]
    assert "final_output" not in types


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_pause_reason_str_values_match_enum() -> None:
    """PauseReason 是 str enum,JSON 序列化要拿 ``.value``。"""
    assert PauseReason.TOOL_APPROVAL.value == "tool_approval"
    assert PauseReason.USER_INPUT_REQUIRED.value == "user_input_required"
    assert PauseReason.GUARDRAIL_TRIPWIRE.value == "guardrail_tripwire"
    assert PauseReason.BUDGET_EXCEEDED.value == "budget_exceeded"


def test_metadata_with_non_serializable_raises_on_to_json() -> None:
    """非 JSON-friendly metadata 值會在 to_json 時 raise。"""
    s = RunState.create("a")
    s.metadata["bad"] = {1, 2, 3}  # set 不能 JSON-serialize
    with pytest.raises(TypeError):
        s.to_json()

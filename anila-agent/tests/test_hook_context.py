"""P1-3 HookContext 三層 scope 單元測試。

驗收項目:

* 三層 dataclass 嵌套關係正確 (Session ⊃ Turn ⊃ Operation 反向 parent 連結)
* `start_turn()` / `start_operation()` 是 contextmanager,結束自動 `mark_ended`
* 巢狀 start_turn 內可多次 start_operation,序號 / parent 都對
* `HookContext.from_*` 建構 helper 各層都能反推
* 與 P0-3 `AnilaToolContext` 整合 —— operation 可掛 tool_context,並可從 ctx 直接拿
* 與 P0-5 `HookExecutor` 整合 —— Inspect/Decide/Transform hook 可消化 HookContext
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from anila_agent.core import (
    AnilaToolContext,
    Decision,
    HookContext,
    HookExecutor,
    OperationContext,
    SessionContext,
    TurnContext,
)


# ---------------------------------------------------------------------------
# 三層 dataclass 嵌套關係
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_context_minimal_fields() -> None:
    """SessionContext 只給 session_id,其它欄位拿 sensible default。"""
    sess = SessionContext(session_id="sess-1")
    assert sess.session_id == "sess-1"
    assert sess.user_id is None
    assert sess.ended_at is None
    assert sess.metadata == {}
    assert sess.turn_history == []
    # started_at 是 epoch seconds,非 0
    assert sess.started_at > 0


@pytest.mark.unit
def test_turn_context_can_be_standalone() -> None:
    """TurnContext 可單獨建構 (session 為 None),用於測試 / 獨立 hook。"""
    turn = TurnContext(turn_id="t1", turn_index=0)
    assert turn.turn_id == "t1"
    assert turn.session is None
    assert turn.messages_in_this_turn == []


@pytest.mark.unit
def test_operation_context_standalone() -> None:
    """OperationContext 可單獨建構,turn / session property 回 None。"""
    op = OperationContext(operation_id="op1", operation_type="tool_call")
    assert op.operation_id == "op1"
    assert op.operation_type == "tool_call"
    assert op.turn is None
    assert op.session is None
    assert op.tool_context is None


# ---------------------------------------------------------------------------
# start_turn / start_operation context manager
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_start_turn_context_manager_marks_ended() -> None:
    """`with session.start_turn()` 結束後 turn.ended_at 自動填上。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        assert turn.ended_at is None
        assert turn.session is sess
        assert turn.turn_index == 0  # 第一個 turn,index 0
    # 離開 with 之後
    assert turn.ended_at is not None
    assert turn.duration is not None
    assert turn.duration >= 0


@pytest.mark.unit
def test_start_turn_records_started_at_before_block() -> None:
    """turn.started_at 應在進入 with 之前已被 default_factory 填好。"""
    sess = SessionContext(session_id="s")
    before = time.time()
    with sess.start_turn() as turn:
        after = time.time()
        assert before <= turn.started_at <= after


@pytest.mark.unit
def test_start_operation_context_manager_marks_ended() -> None:
    """`with turn.start_operation()` 結束後 op.ended_at 自動填上。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        with turn.start_operation("tool_call") as op:
            assert op.ended_at is None
            assert op.operation_type == "tool_call"
            assert op.turn is turn
            assert op.session is sess  # 反查
        assert op.ended_at is not None
        assert op.duration is not None


@pytest.mark.unit
def test_multiple_turns_have_sequential_index() -> None:
    """同一個 session 連開兩個 turn,index 自動遞增。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn() as t0:
        assert t0.turn_index == 0
    with sess.start_turn() as t1:
        assert t1.turn_index == 1
    assert len(sess.turn_history) == 2
    # turn_history 是淺拷貝,外部 mutate 不影響內部
    sess.turn_history.clear()
    assert len(sess.turn_history) == 2


@pytest.mark.unit
def test_explicit_turn_index_overrides_auto() -> None:
    """顯式傳 turn_index 會蓋過自動序號。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn(turn_index=42) as t:
        assert t.turn_index == 42


@pytest.mark.unit
def test_start_operation_default_id_is_unique() -> None:
    """連續開兩個 op,id 各自唯一。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        with turn.start_operation("a") as op1:
            pass
        with turn.start_operation("b") as op2:
            pass
        assert op1.operation_id != op2.operation_id


@pytest.mark.unit
def test_mark_ended_idempotent() -> None:
    """重複呼叫 mark_ended 不會覆蓋第一次的時間。"""
    op = OperationContext(operation_id="x", operation_type="tool_call")
    op.mark_ended()
    first = op.ended_at
    time.sleep(0.001)
    op.mark_ended()
    assert op.ended_at == first


@pytest.mark.unit
def test_start_operation_carries_tool_context() -> None:
    """start_operation 可帶入 P0-3 AnilaToolContext。"""
    tc = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="call-1",
        agent_name="anila",
    )
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        with turn.start_operation("tool_call", tool_context=tc) as op:
            assert op.tool_context is tc
            # 從 op.tool_context 可拿 P0-3 既有欄位
            assert op.tool_context.tool_call_id == "call-1"
            assert op.tool_context.agent_name == "anila"


# ---------------------------------------------------------------------------
# HookContext wrapper —— 三層整合
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hook_context_from_session() -> None:
    """from_session: 只有 session,turn / operation 為 None。"""
    sess = SessionContext(session_id="s")
    hc = HookContext.from_session(sess)
    assert hc.session is sess
    assert hc.turn is None
    assert hc.operation is None
    assert hc.tool_context is None


@pytest.mark.unit
def test_hook_context_from_turn() -> None:
    """from_turn: 反推 session,operation 為 None。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        hc = HookContext.from_turn(turn)
        assert hc.session is sess
        assert hc.turn is turn
        assert hc.operation is None


@pytest.mark.unit
def test_hook_context_from_turn_requires_session() -> None:
    """orphan TurnContext (session=None) 不能 from_turn,丟 ValueError。"""
    turn = TurnContext(turn_id="t", turn_index=0)
    with pytest.raises(ValueError, match="TurnContext.session is None"):
        HookContext.from_turn(turn)


@pytest.mark.unit
def test_hook_context_from_operation() -> None:
    """from_operation: 三層皆 non-None,可直接讀各層 attribute。"""
    sess = SessionContext(session_id="s", user_id="u")
    with sess.start_turn() as turn:
        with turn.start_operation("tool_call") as op:
            hc = HookContext.from_operation(op)
            assert hc.session is sess
            assert hc.session.user_id == "u"
            assert hc.turn is turn
            assert hc.operation is op


@pytest.mark.unit
def test_hook_context_from_operation_requires_turn() -> None:
    """orphan OperationContext (turn=None) 不能 from_operation。"""
    op = OperationContext(operation_id="x", operation_type="tool_call")
    with pytest.raises(ValueError, match="OperationContext.turn is None"):
        HookContext.from_operation(op)


@pytest.mark.unit
def test_hook_context_tool_context_shortcut() -> None:
    """ctx.tool_context 是 operation.tool_context 的捷徑。"""
    tc = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="call-1",
        agent_name="anila",
    )
    sess = SessionContext(session_id="s")
    with sess.start_turn() as turn:
        with turn.start_operation("tool_call", tool_context=tc) as op:
            hc = HookContext.from_operation(op)
            assert hc.tool_context is tc


@pytest.mark.unit
def test_hook_context_each_layer_metadata_independent() -> None:
    """三層各自的 metadata dict 不互相干擾。"""
    sess = SessionContext(session_id="s", metadata={"session_key": 1})
    with sess.start_turn(metadata={"turn_key": 2}) as turn:
        with turn.start_operation("op", metadata={"op_key": 3}) as op:
            hc = HookContext.from_operation(op)
            assert hc.session.metadata == {"session_key": 1}
            assert hc.turn is not None
            assert hc.turn.metadata == {"turn_key": 2}
            assert hc.operation is not None
            assert hc.operation.metadata == {"op_key": 3}


# ---------------------------------------------------------------------------
# 與 P0-5 HookExecutor 整合
# ---------------------------------------------------------------------------


@dataclass
class _RecordingInspect:
    """InspectHook —— 把 HookContext 各層 id 記到 trace。"""

    trace: list[str]

    def inspect(self, ctx: HookContext, payload: Any) -> None:
        self.trace.append(
            f"session={ctx.session.session_id}"
            f"|turn={ctx.turn.turn_id if ctx.turn else 'none'}"
            f"|op={ctx.operation.operation_id if ctx.operation else 'none'}"
            f"|payload={payload}"
        )


@dataclass
class _OperationAwareDecide:
    """DecideHook —— 只在 operation_type=='destructive' 時 DENY。"""

    def decide(self, ctx: HookContext, payload: Any) -> Decision:
        if ctx.operation is not None and ctx.operation.operation_type == "destructive":
            return Decision.deny(reason="destructive blocked", source="test")
        return Decision.allow(reason="ok", source="test")


@dataclass
class _TurnMetadataTransform:
    """TransformHook —— 從 ctx.turn.metadata 取 prefix 串到 payload 前面。"""

    def transform(self, ctx: HookContext, payload: str) -> str:
        prefix = ""
        if ctx.turn is not None:
            prefix = str(ctx.turn.metadata.get("prefix", ""))
        return f"{prefix}{payload}"


@pytest.mark.unit
def test_hook_executor_consumes_hook_context_inspect() -> None:
    """InspectHook chain 收到 HookContext,可讀三層 id。"""
    trace: list[str] = []
    inspector = _RecordingInspect(trace=trace)
    sess = SessionContext(session_id="sess-X")
    with sess.start_turn(turn_id="turn-Y") as turn:
        with turn.start_operation("tool_call", operation_id="op-Z") as op:
            ctx = HookContext.from_operation(op)
            HookExecutor.run_inspect([inspector], ctx, "hello")

    assert trace == ["session=sess-X|turn=turn-Y|op=op-Z|payload=hello"]


@pytest.mark.unit
def test_hook_executor_decide_uses_operation_field() -> None:
    """DecideHook 可從 ctx.operation 取 operation_type 作判斷依據。"""
    decider = _OperationAwareDecide()
    sess = SessionContext(session_id="s")

    # operation_type='read' → ALLOW
    with sess.start_turn() as turn:
        with turn.start_operation("read") as op:
            ctx = HookContext.from_operation(op)
            decision = HookExecutor.run_decide([decider], ctx, "payload")
            assert decision.is_allow

    # operation_type='destructive' → DENY
    with sess.start_turn() as turn:
        with turn.start_operation("destructive") as op:
            ctx = HookContext.from_operation(op)
            decision = HookExecutor.run_decide([decider], ctx, "payload")
            assert decision.is_deny
            assert decision.reason == "destructive blocked"


@pytest.mark.unit
def test_hook_executor_transform_reads_turn_metadata() -> None:
    """TransformHook 可從 ctx.turn.metadata 拿資料來變換 payload。"""
    transformer = _TurnMetadataTransform()
    sess = SessionContext(session_id="s")
    with sess.start_turn(metadata={"prefix": ">>> "}) as turn:
        with turn.start_operation("op") as op:
            ctx = HookContext.from_operation(op)
            out = HookExecutor.run_transform([transformer], ctx, "world")
            assert out == ">>> world"


@pytest.mark.unit
def test_hook_executor_full_pipeline_with_hook_context() -> None:
    """完整 pipeline: Inspect 全跑 -> Decide ALLOW -> Transform 跑;且 HookContext 各層都通。"""
    trace: list[str] = []
    sess = SessionContext(session_id="s")
    with sess.start_turn(metadata={"prefix": "[T]"}) as turn:
        with turn.start_operation("read", operation_id="op1") as op:
            ctx = HookContext.from_operation(op)
            result = HookExecutor.run_pipeline(
                ctx,
                "hello",
                inspect_hooks=[_RecordingInspect(trace=trace)],
                decide_hooks=[_OperationAwareDecide()],
                transform_hooks=[_TurnMetadataTransform()],
            )

    assert len(trace) == 1
    assert "op=op1" in trace[0]
    assert result.decision.is_allow
    assert result.transform_executed
    assert result.payload == "[T]hello"


@pytest.mark.unit
def test_hook_executor_pipeline_denies_skips_transform_with_hook_context() -> None:
    """destructive operation 被 DecideHook DENY,Transform 不執行,payload 為 None。"""
    sess = SessionContext(session_id="s")
    with sess.start_turn(metadata={"prefix": "[T]"}) as turn:
        with turn.start_operation("destructive") as op:
            ctx = HookContext.from_operation(op)
            result = HookExecutor.run_pipeline(
                ctx,
                "hello",
                inspect_hooks=[],
                decide_hooks=[_OperationAwareDecide()],
                transform_hooks=[_TurnMetadataTransform()],
            )

    assert result.decision.is_deny
    assert not result.transform_executed
    assert result.payload is None


# ---------------------------------------------------------------------------
# 與 P0-3 AnilaToolContext 整合 —— operation.tool_context 在 hook 內可讀
# ---------------------------------------------------------------------------


@dataclass
class _ReadToolContextInspect:
    """InspectHook —— 讀 ctx.tool_context.workspace 並記下。"""

    seen: list[str]

    def inspect(self, ctx: HookContext, payload: Any) -> None:
        if ctx.tool_context is None or ctx.tool_context.workspace is None:
            self.seen.append("no-workspace")
        else:
            self.seen.append(str(ctx.tool_context.workspace))


@pytest.mark.unit
def test_hook_can_read_tool_context_via_hook_context(tmp_path: Any) -> None:
    """hook 透過 ctx.tool_context 直接拿到 P0-3 workspace。"""
    tc = AnilaToolContext(
        session_id="s",
        turn_id=0,
        tool_call_id="call-1",
        agent_name="anila",
        workspace=tmp_path,
    )
    sess = SessionContext(session_id="s")
    seen: list[str] = []
    with sess.start_turn() as turn:
        with turn.start_operation("tool_call", tool_context=tc) as op:
            ctx = HookContext.from_operation(op)
            HookExecutor.run_inspect([_ReadToolContextInspect(seen=seen)], ctx, None)

    assert seen == [str(tmp_path.resolve())]

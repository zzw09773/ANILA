"""Sprint 5 tests — Coordinator + WorkerTask + tool Actions + notifications."""

from __future__ import annotations


import pytest

from agentic_rag.runtime.framework import (
    ActionContext,
    Agent,
    ChatCompletionResponse,
    Coordinator,
    FinishReason,
    Message,
    TaskNotification,
    Usage,
    WorkerState,
    build_task_notification,
    collect_for_summary,
    make_check_worker_action,
    make_coordinator_actions,
    make_spawn_worker_action,
    make_wait_for_workers_action,
    parse_all,
    parse_task_notification,
)
from agentic_rag.runtime.framework.exceptions import UserError


# ── Test scaffolding ─────────────────────────────────────────────────


class _ScriptedProvider:
    def __init__(self, responses):
        self._scripted = list(responses)

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        if not self._scripted:
            raise AssertionError("provider exhausted")
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kw):
        raise NotImplementedError


def _resp(text=""):
    return ChatCompletionResponse(
        message=Message.assistant(content=text),
        usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
        finish_reason=FinishReason.STOP,
    )


def _worker(name: str, response_text: str = "done") -> Agent:
    return Agent(
        name=name,
        instructions="be terse",
        provider=_ScriptedProvider([_resp(response_text)]),
        model="m",
    )


# ── Task notification protocol ───────────────────────────────────────


def test_build_then_parse_round_trips() -> None:
    s = build_task_notification("t-1", "completed", "found 3 docs", "doc list...")
    parsed = parse_task_notification(s)
    assert parsed is not None
    assert parsed.task_id == "t-1"
    assert parsed.status == "completed"
    assert parsed.summary == "found 3 docs"
    assert parsed.result == "doc list..."


def test_parse_returns_none_on_no_match() -> None:
    assert parse_task_notification("plain text no notification here") is None


def test_parse_all_finds_multiple_notifications() -> None:
    blob = (
        build_task_notification("t-1", "completed", "ok", "first body")
        + "\n\n"
        + build_task_notification("t-2", "failed", "err", "second body")
    )
    notifs = parse_all(blob)
    assert len(notifs) == 2
    assert notifs[0].task_id == "t-1"
    assert notifs[1].status == "failed"


def test_collect_for_summary_round_trips_through_parse_all() -> None:
    a = TaskNotification(task_id="a", status="completed", summary="x", result="A")
    b = TaskNotification(task_id="b", status="failed", summary="y", result="B")
    blob = collect_for_summary([a, b])
    parsed = parse_all(blob)
    assert [p.task_id for p in parsed] == ["a", "b"]
    assert parsed[1].result == "B"


def test_attribute_escaping_protects_embedded_quotes() -> None:
    """Quote chars in attributes must not break the wrapper."""
    s = build_task_notification("id", "completed", 'has "quote" inside', "body")
    parsed = parse_task_notification(s)
    assert parsed is not None
    assert parsed.summary == 'has "quote" inside'


def test_body_escaping_blocks_embedded_close_tag() -> None:
    """An embedded </task-notification> in body must not truncate."""
    body = "before </task-notification> after"
    s = build_task_notification("id", "completed", "summary", body)
    parsed = parse_task_notification(s)
    assert parsed is not None
    assert parsed.result == body


# ── Coordinator constructor ──────────────────────────────────────────


def test_coordinator_rejects_empty_workers() -> None:
    with pytest.raises(UserError):
        Coordinator(workers={})


def test_coordinator_worker_types_property() -> None:
    coord = Coordinator(workers={"a": _worker("a"), "b": _worker("b")})
    assert coord.worker_types == ["a", "b"]


# ── spawn_worker / wait ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_worker_runs_to_completion() -> None:
    coord = Coordinator(workers={"verifier": _worker("verifier", "verified ok")})
    task = coord.spawn_worker("verifier", "check x")
    assert task.state in (WorkerState.PENDING, WorkerState.RUNNING)

    finished = await coord.wait(task)
    assert finished.state is WorkerState.COMPLETED
    assert finished.result is not None
    assert finished.result.final_output == "verified ok"
    assert finished.finished_at is not None


@pytest.mark.asyncio
async def test_spawn_worker_unknown_agent_raises() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    with pytest.raises(UserError, match="no worker agent type"):
        coord.spawn_worker("missing", "x")


@pytest.mark.asyncio
async def test_wait_can_receive_task_id_string() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    task = coord.spawn_worker("a", "hi")
    finished = await coord.wait(task.task_id)
    assert finished.task_id == task.task_id
    assert finished.state is WorkerState.COMPLETED


@pytest.mark.asyncio
async def test_worker_failure_recorded_on_task() -> None:
    """Provider raises mid-run → state transitions to FAILED, error captured."""

    class _Boom:
        async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
            raise RuntimeError("provider down")

        async def embeddings(self, texts, *, model, **kw):
            raise NotImplementedError

    failing_agent = Agent(
        name="oops", instructions="", provider=_Boom(), model="m"
    )
    coord = Coordinator(workers={"oops": failing_agent})
    task = coord.spawn_worker("oops", "go")
    await coord.wait(task)
    assert task.state is WorkerState.FAILED
    assert task.error is not None


# ── gather_parallel ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_parallel_runs_n_workers() -> None:
    # Each spawn gets its own agent, each agent its own scripted provider —
    # otherwise the second .pop(0) on a shared provider crashes.
    def fresh(text):
        return Agent(
            name="verifier",
            instructions="",
            provider=_ScriptedProvider([_resp(text)]),
            model="m",
        )

    # Coordinator constructor wants ONE Agent per type. We reuse the worker
    # name but the spawn path uses the provider attached at construction.
    # For multi-prompt, build a custom provider that returns N canned
    # responses in sequence — that's the realistic local-deployment shape.
    multi_provider = _ScriptedProvider([_resp("hit-A"), _resp("hit-B"), _resp("hit-C")])
    multi_agent = Agent(
        name="searcher", instructions="", provider=multi_provider, model="m"
    )
    coord = Coordinator(workers={"searcher": multi_agent})

    results = await coord.gather_parallel(
        "searcher", ["query A", "query B", "query C"]
    )
    assert len(results) == 3
    outputs = sorted(r.result.final_output for r in results)
    assert outputs == ["hit-A", "hit-B", "hit-C"]


@pytest.mark.asyncio
async def test_gather_parallel_empty_returns_empty() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    assert await coord.gather_parallel("a", []) == []


# ── run_sequential ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_sequential_executes_in_order() -> None:
    p = _ScriptedProvider([_resp("step-1"), _resp("step-2"), _resp("step-3")])
    a = Agent(name="indexer", instructions="", provider=p, model="m")
    coord = Coordinator(workers={"indexer": a})
    results = await coord.run_sequential(
        [("indexer", "do A"), ("indexer", "do B"), ("indexer", "do C")]
    )
    assert [r.result.final_output for r in results] == ["step-1", "step-2", "step-3"]


@pytest.mark.asyncio
async def test_run_sequential_stop_on_failure() -> None:
    """When stop_on_failure=True, a failed step halts the pipeline."""

    class _FailOnSecond:
        def __init__(self):
            self.calls = 0

        async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("step 2 broke")
            return _resp(f"step-{self.calls}")

        async def embeddings(self, texts, *, model, **kw):
            raise NotImplementedError

    a = Agent(name="indexer", instructions="", provider=_FailOnSecond(), model="m")
    coord = Coordinator(workers={"indexer": a})
    results = await coord.run_sequential(
        [("indexer", "A"), ("indexer", "B"), ("indexer", "C")],
        stop_on_failure=True,
    )
    assert len(results) == 2
    assert results[0].state is WorkerState.COMPLETED
    assert results[1].state is WorkerState.FAILED


# ── cancel ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_returns_false_on_already_finished_task() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    task = coord.spawn_worker("a", "x")
    await coord.wait(task)
    assert coord.cancel(task) is False


# ── Tool actions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_worker_action_returns_task_id() -> None:
    coord = Coordinator(workers={"verifier": _worker("verifier", "v-result")})
    spawn = make_spawn_worker_action(coord)
    ctx = ActionContext(
        run_id="r",
        agent_name="coord",
        params={"agent_type": "verifier", "prompt": "check x"},
        history=(),
    )
    result = await spawn.handler(ctx)
    assert not result.is_error
    assert "task_id" in result.output
    assert result.output["agent_type"] == "verifier"


@pytest.mark.asyncio
async def test_spawn_worker_action_rejects_unknown_type() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    spawn = make_spawn_worker_action(coord)
    ctx = ActionContext(
        run_id="r",
        agent_name="coord",
        params={"agent_type": "missing", "prompt": "x"},
        history=(),
    )
    result = await spawn.handler(ctx)
    assert result.is_error
    assert "unknown agent_type" in result.error


@pytest.mark.asyncio
async def test_spawn_worker_action_honors_allowed_worker_types() -> None:
    coord = Coordinator(
        workers={"verifier": _worker("verifier"), "summariser": _worker("summariser")}
    )
    spawn = make_spawn_worker_action(coord, allowed_worker_types=["verifier"])
    # summariser exists but is NOT in the allowlist
    ctx = ActionContext(
        run_id="r",
        agent_name="coord",
        params={"agent_type": "summariser", "prompt": "x"},
        history=(),
    )
    result = await spawn.handler(ctx)
    assert result.is_error
    assert "not allowed" in result.error


@pytest.mark.asyncio
async def test_check_worker_action_returns_notification() -> None:
    coord = Coordinator(workers={"v": _worker("v", "found x")})
    task = coord.spawn_worker("v", "go")
    await coord.wait(task)

    check = make_check_worker_action(coord)
    ctx = ActionContext(
        run_id="r", agent_name="c", params={"task_id": task.task_id}, history=()
    )
    result = await check.handler(ctx)
    assert not result.is_error
    assert result.output["state"] == "completed"
    assert result.output["is_done"] is True
    parsed = parse_task_notification(result.output["notification"])
    assert parsed is not None
    assert parsed.task_id == task.task_id


@pytest.mark.asyncio
async def test_wait_for_workers_action_returns_joined_notifications() -> None:
    p = _ScriptedProvider([_resp("a-result"), _resp("b-result")])
    multi_agent = Agent(name="searcher", instructions="", provider=p, model="m")
    coord = Coordinator(workers={"searcher": multi_agent})
    coord.spawn_worker("searcher", "A")
    coord.spawn_worker("searcher", "B")

    wait_action = make_wait_for_workers_action(coord)
    ctx = ActionContext(run_id="r", agent_name="c", params={}, history=())
    result = await wait_action.handler(ctx)
    assert not result.is_error
    assert len(result.output["completed"]) == 2
    notifs = parse_all(result.output["notifications"])
    assert len(notifs) == 2


@pytest.mark.asyncio
async def test_make_coordinator_actions_returns_three_actions() -> None:
    coord = Coordinator(workers={"a": _worker("a")})
    actions = make_coordinator_actions(coord)
    names = sorted(a.name for a in actions)
    assert names == ["check_worker", "spawn_worker", "wait_for_workers"]


# ── AgenticRAG bridge ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_builds_framework_coordinator_from_agent_definitions() -> None:
    from agentic_rag.models.agent import AgentDefinition, PermissionMode
    from agentic_rag.runtime.bridge import build_framework_coordinator

    definitions = {
        "verifier": AgentDefinition(
            agent_type="verifier",
            description="Verifies claims",
            system_prompt="be precise",
            model="m",
            max_turns=3,
            permission_mode=PermissionMode.READ_ONLY,
        ),
    }
    p = _ScriptedProvider([_resp("verified")])
    coord = build_framework_coordinator(
        definitions, provider=p, default_model="m"
    )
    assert coord.worker_types == ["verifier"]
    task = coord.spawn_worker("verifier", "check claim")
    await coord.wait(task)
    assert task.state is WorkerState.COMPLETED


def test_bridge_is_parallel_safe_maps_read_only() -> None:
    from agentic_rag.models.agent import AgentDefinition, PermissionMode
    from agentic_rag.runtime.bridge import is_parallel_safe

    ro = AgentDefinition(agent_type="x", permission_mode=PermissionMode.READ_ONLY)
    rw = AgentDefinition(agent_type="y", permission_mode=PermissionMode.DEFAULT)
    assert is_parallel_safe(ro) is True
    assert is_parallel_safe(rw) is False

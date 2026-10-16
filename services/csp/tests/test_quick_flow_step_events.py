"""Engine observations are real node boundaries, never timers or skipped branches."""

import asyncio

from app.services.quick_flow import execute_graph, validate_graph


def _graph():
    return validate_graph(
        {
            "nodes": [
                {"id": "in", "kind": "input"},
                {"id": "model", "kind": "model"},
                {"id": "out", "kind": "output"},
            ],
            "edges": [
                {"source": "in", "target": "model"},
                {"source": "model", "target": "out"},
            ],
        }
    )


def test_observer_reports_started_before_model_and_finished_after_real_result():
    async def scenario():
        events = []
        started, release = asyncio.Event(), asyncio.Event()

        async def observer(event, step):
            events.append((event, step))

        async def model(*args):
            started.set()
            await release.wait()
            return "合成回答"

        task = asyncio.create_task(
            execute_graph(
                _graph(),
                "合成問題",
                model_call=model,
                rag_call=None,
                tool_call=None,
                document_call=None,
                on_step=observer,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        assert [(kind, step["node_id"]) for kind, step in events] == [
            ("step_started", "in"),
            ("step_finished", "in"),
            ("step_started", "model"),
        ]
        assert events[-1][1]["status"] == "running"
        release.set()
        result = await task
        assert result["status"] == "completed"
        assert [step["status"] for kind, step in events if kind == "step_finished"] == [
            "completed"
        ] * 3
        assert events[3][1]["elapsed_ms"] >= 0
        assert events[3][1]["output"]["text"] == "合成回答"

    asyncio.run(scenario())


def test_observer_gets_independent_copy_and_errors_finish_actual_failed_step():
    events = []

    async def observer(event, step):
        events.append((event, step))
        if event == "step_started":
            step["node_id"] = "observer-cannot-rewrite-engine"

    async def model(*args):
        raise RuntimeError("synthetic-private-provider-error")

    result = asyncio.run(
        execute_graph(
            _graph(),
            "合成",
            model_call=model,
            rag_call=None,
            tool_call=None,
            document_call=None,
            on_step=observer,
        )
    )
    assert result["status"] == "failed"
    assert [step["node_id"] for step in result["steps"]] == ["in", "model"]
    assert events[-1][0] == "step_finished" and events[-1][1]["status"] == "failed"
    assert events[-1][1]["elapsed_ms"] >= 0
    assert "synthetic-private" not in str(events[-1][1]["error"])


def test_unselected_condition_branch_never_emits_started_or_finished():
    graph = validate_graph(
        {
            "nodes": [
                {"id": "in", "kind": "input"},
                {"id": "check", "kind": "condition", "config": {"value": "yes"}},
                {"id": "selected", "kind": "prompt"},
                {"id": "skipped", "kind": "prompt"},
                {"id": "out", "kind": "output"},
            ],
            "edges": [
                {"source": "in", "target": "check"},
                {"source": "check", "target": "selected", "branch": "true"},
                {"source": "check", "target": "skipped", "branch": "false"},
                {"source": "selected", "target": "out"},
                {"source": "skipped", "target": "out"},
            ],
        }
    )
    events = []

    async def observe(event, step):
        events.append((event, step["node_id"]))

    result = asyncio.run(
        execute_graph(
            graph,
            "yes",
            model_call=None,
            rag_call=None,
            tool_call=None,
            document_call=None,
            on_step=observe,
        )
    )
    assert result["status"] == "completed"
    assert all(node_id != "skipped" for _, node_id in events)
    assert [node_id for event, node_id in events if event == "step_started"] == [
        "in",
        "check",
        "selected",
        "out",
    ]


def test_observer_denial_prevents_later_model_execution():
    calls = []

    async def observe(event, step):
        if event == "step_started" and step["node_id"] == "model":
            raise PermissionError("synthetic-private-denial")

    async def model(*args):
        calls.append(True)
        return "must-not-run"

    result = asyncio.run(
        execute_graph(
            _graph(),
            "合成",
            model_call=model,
            rag_call=None,
            tool_call=None,
            document_call=None,
            on_step=observe,
        )
    )
    assert result["status"] == "failed" and not calls


def test_cancellation_does_not_forge_finished_model_event():
    async def scenario():
        events = []
        started = asyncio.Event()

        async def observe(event, step):
            events.append((event, step["node_id"]))

        async def model(*args):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            execute_graph(
                _graph(),
                "合成",
                model_call=model,
                rag_call=None,
                tool_call=None,
                document_call=None,
                on_step=observe,
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("true execution cancellation must propagate")
        assert ("step_started", "model") in events
        assert ("step_finished", "model") not in events

    asyncio.run(scenario())

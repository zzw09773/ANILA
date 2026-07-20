from __future__ import annotations

import json

from anila_contracts import Classification

from anila_agent.observability.timeline import (
    TimelineEmitter,
    TimelineRetriever,
    TimelineRunHooks,
)


def _emitter() -> TimelineEmitter:
    return TimelineEmitter(
        task_id="1",
        trace_id="trace",
        agent_id="agent",
        session_id="session",
        run_id="run",
        classification=Classification.UNCLASSIFIED,
    )


def _events(emitter: TimelineEmitter) -> list[dict]:
    return [
        json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
        for frame in emitter.drain()
    ]


async def test_hooks_emit_agent_tool_and_skill_lifecycle_without_raw_output():
    emitter = _emitter()
    hooks = TimelineRunHooks(emitter)
    agent = type("Agent", (), {"name": "official"})()
    tool = type("Tool", (), {"name": "search_documents"})()
    skill = type("Tool", (), {"name": "load_skill"})()
    await hooks.on_agent_start(None, agent)
    await hooks.on_tool_start(None, agent, skill)
    await hooks.on_tool_end(None, agent, skill, "PRIVATE RAW BODY")
    await hooks.on_tool_start(None, agent, tool)
    await hooks.on_tool_end(None, agent, tool, [{"secret": "not projected"}])
    await hooks.on_agent_end(None, agent, "RAW MODEL OUTPUT")
    events = _events(emitter)
    assert [event["kind"] for event in events] == [
        "agent", "skill", "skill", "tool", "tool", "agent"
    ]
    wire = json.dumps(events)
    assert "PRIVATE RAW BODY" not in wire
    assert "RAW MODEL OUTPUT" not in wire
    assert "not projected" not in wire


async def test_retriever_emits_content_free_count_only_events():
    class Retriever:
        name = "csp"

        @property
        def metadata(self):
            return {"collection_id": 12}

        async def search(self, query, k=5):
            assert query == "TOP SECRET QUERY"
            return [object(), object()]

        async def fetch(self, doc_id):
            return None

    emitter = _emitter()
    hits = await TimelineRetriever(Retriever(), emitter).search("TOP SECRET QUERY", k=2)
    assert len(hits) == 2
    events = _events(emitter)
    assert [event["status"] for event in events] == ["running", "completed"]
    assert all(event["kind"] == "retrieval" for event in events)
    assert "TOP SECRET QUERY" not in json.dumps(events)


def test_cancel_terminal_is_exactly_once():
    emitter = _emitter()
    emitter.cancel()
    emitter.cancel()
    events = _events(emitter)
    assert len(events) == 1
    assert events[0]["status"] == "cancelled"

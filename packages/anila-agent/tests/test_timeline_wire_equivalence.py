from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

from anila_contracts import Classification, StepEvent

from anila_agent.observability.timeline import (
    TimelineEmitter,
    TimelineRetriever,
    TimelineRunHooks,
)


def _payloads(frames: list[str]) -> list[StepEvent]:
    return [
        StepEvent.model_validate_json(
            next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        )
        for frame in frames
    ]


def _profile(events: list[StepEvent]) -> list[list[str]]:
    return [[event.kind.value, event.status.value] for event in events]


def _emitter(agent_id: str) -> TimelineEmitter:
    return TimelineEmitter(
        task_id="task",
        trace_id="trace",
        agent_id=agent_id,
        session_id="session",
        run_id="run",
        classification=Classification.UNCLASSIFIED,
    )


def test_official_and_langchain_adapters_match_frozen_named_sse_profile():
    asyncio.run(_assert_wire_equivalence())


async def _assert_wire_equivalence():
    fixture_path = (
        Path(__file__).parents[2]
        / "anila-contracts"
        / "tests"
        / "fixtures"
        / "gate4-timeline-profile-v1.json"
    )
    frozen = json.loads(fixture_path.read_text(encoding="utf-8"))

    official = _emitter("official")
    hooks = TimelineRunHooks(official)
    agent = type("Agent", (), {"name": "official"})()
    skill = type("Tool", (), {"name": "load_skill"})()
    tool = type("Tool", (), {"name": "search_documents"})()
    await hooks.on_tool_start(None, agent, skill)
    await hooks.on_tool_end(None, agent, skill, None)
    await hooks.on_tool_start(None, agent, tool)
    await hooks.on_tool_end(None, agent, tool, None)

    class Retriever:
        name = "csp"

        @property
        def metadata(self):
            return {}

        async def search(self, query, k=5):
            return []

        async def fetch(self, doc_id):
            return None

    await TimelineRetriever(Retriever(), official).search("redacted")
    official_frames = official.drain()

    adapter_path = Path(__file__).parents[1] / "examples" / "langchain_timeline_adapter.py"
    spec = importlib.util.spec_from_file_location("gate4_langchain_adapter", adapter_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    langchain_frames: list[str] = []
    adapter = module.AnilaTimelineCallback(
        task_id="task",
        trace_id="trace",
        agent_id="langchain",
        session_id="session",
        run_id="run",
        classification=Classification.UNCLASSIFIED,
        send_sse=langchain_frames.append,
    )
    adapter.on_chain_start({"name": "skill"}, {}, run_id="skill")
    adapter.on_chain_end({}, run_id="skill")
    adapter.on_tool_start({"name": "search_documents"}, "redacted", run_id="tool")
    adapter.on_tool_end(None, run_id="tool")
    adapter.on_retriever_start({"name": "csp"}, "redacted", run_id="retrieval")
    adapter.on_retriever_end([], run_id="retrieval")

    assert all(frame.startswith(f"event: {frozen['event_name']}\n") for frame in official_frames)
    assert all(frame.startswith(f"event: {frozen['event_name']}\n") for frame in langchain_frames)
    official_events = _payloads(official_frames)
    langchain_events = _payloads(langchain_frames)
    assert all(event.schema_version == frozen["schema_version"] for event in official_events)
    assert all(event.schema_version == frozen["schema_version"] for event in langchain_events)
    assert _profile(official_events) == frozen["lifecycle"]
    assert _profile(langchain_events) == frozen["lifecycle"]

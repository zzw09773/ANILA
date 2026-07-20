from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from anila_contracts import Classification, StepEvent


def _load_adapter():
    path = Path(__file__).parents[1] / "examples" / "langchain_timeline_adapter.py"
    spec = importlib.util.spec_from_file_location("langchain_timeline_adapter", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.AnilaTimelineCallback


def _payload(frame: str) -> StepEvent:
    data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    return StepEvent.model_validate(json.loads(data))


def test_langchain_adapter_uses_frozen_named_sse_contract_and_redacts_inputs():
    frames: list[str] = []
    adapter = _load_adapter()(
        task_id="1",
        trace_id="trace",
        agent_id="lc-agent",
        session_id="session",
        run_id="run",
        classification=Classification.UNCLASSIFIED,
        send_sse=frames.append,
    )
    adapter.on_chain_start({"name": "citation-skill"}, {"secret": "RAW"}, run_id="skill")
    adapter.on_chain_end({"secret": "RAW OUTPUT"}, run_id="skill")
    adapter.on_tool_start({"name": "search_documents"}, "RAW ARGS", run_id="tool")
    adapter.on_tool_end("RAW RESULT", run_id="tool")
    adapter.on_retriever_start({"name": "csp"}, "RAW QUERY", run_id="retrieval")
    adapter.on_retriever_end([{"raw": "DOC"}], run_id="retrieval")
    events = [_payload(frame) for frame in frames]
    assert [event.kind.value for event in events] == [
        "skill", "skill", "tool", "tool", "retrieval", "retrieval"
    ]
    assert all(frame.startswith("event: anila.step\n") for frame in frames)
    wire = "".join(frames)
    for raw in ("RAW ARGS", "RAW RESULT", "RAW QUERY", "RAW OUTPUT", '"raw": "DOC"'):
        assert raw not in wire

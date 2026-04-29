"""Tests for the JSON-line wire protocol."""

from __future__ import annotations

import json

from shared.wire import (
    DONE_EVENT_TYPE,
    JobSpec,
    decode_line,
    encode_event,
)


def test_jobspec_serialize_round_trip() -> None:
    spec = JobSpec(
        code="x = 1",
        body={"action_id": "btn"},
        valves={"endpoint": "https://x"},
        user={"id": 7, "username": "alice"},
        metadata={"started_at": "2026-04-29T00:00:00Z"},
        mode="exec",
    )
    raw = spec.serialize()
    parsed = JobSpec.deserialize(raw)
    assert parsed == spec


def test_event_encode_terminates_with_newline() -> None:
    line = encode_event({"type": "status", "description": "go"})
    assert line.endswith("\n")
    obj = json.loads(line.rstrip("\n"))
    assert obj == {"type": "status", "description": "go"}


def test_decode_line_strips_newline() -> None:
    parsed = decode_line('{"type":"status","description":"hi"}\n')
    assert parsed == {"type": "status", "description": "hi"}


def test_jobspec_extract_mode_default_empty_kwargs() -> None:
    """Extract mode is allowed to omit valves/user/metadata in JSON;
    deserialize must accept that and default to {} (empty dicts).
    """
    raw = json.dumps(
        {"code": "x=1", "body": {}, "mode": "extract"}
    )
    spec = JobSpec.deserialize(raw)
    assert spec.mode == "extract"
    assert spec.valves == {}
    assert spec.user == {}
    assert spec.metadata == {}


def test_done_sentinel_constant() -> None:
    assert DONE_EVENT_TYPE == "__done__"

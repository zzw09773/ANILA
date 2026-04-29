"""Integration tests for ``sandbox/runtime.py``.

Each test spawns the runtime as a real subprocess (mirroring the
production sandbox daemon's behavior) and parses the JSON-line
events from stdout.

Run from the project root with ``PYTHONPATH=anila-functions-worker``
so ``shared.wire`` resolves the same way the runtime expects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shared.wire import DONE_EVENT_TYPE, ERROR_EVENT_TYPE, JobSpec


WORKER_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = WORKER_ROOT / "sandbox" / "runtime.py"


def _run(spec: JobSpec, *, timeout: float = 10.0) -> list[dict]:
    """Spawn runtime.py with ``spec`` on stdin, return parsed events."""
    completed = subprocess.run(
        [sys.executable, "-u", str(RUNTIME_PATH)],
        input=spec.serialize(),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PYTHONPATH": str(WORKER_ROOT)},
        check=False,
    )
    return [
        json.loads(line) for line in completed.stdout.splitlines() if line
    ]


SAMPLE_OK = """
class Action:
    actions = [{"id": "btn", "name": "Btn", "icon_url": None}]
    async def action(self, body, __event_emitter__=None, **kw):
        await __event_emitter__({"type": "status", "description": "hi"})
"""

SAMPLE_NO_ACTION_CLASS = "x = 1"

SAMPLE_RAISES = """
class Action:
    actions = [{"id": "x", "name": "X", "icon_url": None}]
    async def action(self, body, __event_emitter__=None, **kw):
        raise ValueError("boom")
"""


def test_runtime_runs_action_and_emits_done() -> None:
    spec = JobSpec(code=SAMPLE_OK, body={"action_id": "btn"})
    events = _run(spec)
    types = [e["type"] for e in events]
    assert "status" in types
    assert types[-1] == DONE_EVENT_TYPE


def test_runtime_emits_error_on_missing_action_class() -> None:
    spec = JobSpec(code=SAMPLE_NO_ACTION_CLASS, body={})
    events = _run(spec)
    assert events[0]["type"] == ERROR_EVENT_TYPE
    assert events[-1]["type"] == DONE_EVENT_TYPE


def test_runtime_emits_error_on_action_exception() -> None:
    spec = JobSpec(code=SAMPLE_RAISES, body={})
    events = _run(spec)
    error_events = [e for e in events if e["type"] == ERROR_EVENT_TYPE]
    assert error_events
    assert "ValueError" in error_events[0]["message"]
    assert events[-1]["type"] == DONE_EVENT_TYPE


def test_runtime_handles_bad_job_spec() -> None:
    completed = subprocess.run(
        [sys.executable, "-u", str(RUNTIME_PATH)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
        env={"PYTHONPATH": str(WORKER_ROOT)},
        check=False,
    )
    events = [
        json.loads(line) for line in completed.stdout.splitlines() if line
    ]
    assert events[0]["type"] == ERROR_EVENT_TYPE
    assert events[-1]["type"] == DONE_EVENT_TYPE

"""R5 official-agent Silver conformance tests.

These tests stay local: the Runner/model/retriever boundaries are replaced by
small fakes, while manifest, durable records and canonical StepEvent objects
remain real package contracts.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from anila_contracts import AgentManifest, Classification

from anila_agent.config import AgentConfig, AppConfig, ModelConfig
from anila_agent.observability.timeline import TimelineEmitter
from anila_agent.runtime import run as run_module
from anila_agent.runtime.admission import (
    AgentAdmissionError,
    admit_startup,
    build_agent_manifest,
    canonical_manifest_json,
)
from anila_agent.runtime.model import build_model
from anila_agent.runtime.task import (
    FileTaskStore,
    IdempotencyConflict,
    SingleTaskRunner,
    TaskStatus,
    TaskStoreConflict,
    TaskStoreCorruption,
    request_digest,
)

pytestmark = pytest.mark.unit


def _cfg(base_url: str = "https://csp.internal/v1") -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            base_url=base_url,
            model="gpt-oss-20b",
            api_key="EMPTY",
            ssl_verify=True,
            timeout=5,
        ),
        agent=AgentConfig(name="Research Agent", max_turns=4),
        home=Path(".anila"),
        log_level="INFO",
    )


def test_manifest_is_canonical_and_csp_bound() -> None:
    manifest = build_agent_manifest(_cfg())
    parsed = AgentManifest.model_validate(manifest.model_dump(mode="json"))
    assert parsed == manifest
    assert parsed.model_binding is not None
    assert parsed.model_binding.gateway == "csp"
    assert canonical_manifest_json(manifest) == canonical_manifest_json(parsed)


def test_non_csp_base_url_fails_startup_admission() -> None:
    with pytest.raises(AgentAdmissionError, match="裸模型 endpoint"):
        admit_startup(_cfg("http://gpt-oss-20b:8000/v1"), csp_base_url="https://csp.internal")


def test_model_client_strict_mode_rejects_raw_endpoint() -> None:
    with pytest.raises(AgentAdmissionError, match="裸模型 endpoint"):
        build_model(
            _cfg("http://gpt-oss-20b:8000/v1").model,
            csp_base_url="https://csp.internal",
            require_csp_endpoint=True,
        )


@pytest.mark.asyncio
async def test_run_once_state_reuses_sdk_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Runner:
        @staticmethod
        async def run(*args: object, **kwargs: object) -> object:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(final_output="resumed")

    monkeypatch.setattr(run_module, "Runner", _Runner)
    state = object()
    assembled = SimpleNamespace(agent=object(), context=object(), max_turns=4)
    result = await run_module.run_once_state(assembled, state)  # type: ignore[arg-type]
    assert result.final_output == "resumed"
    assert captured["args"] == (assembled.agent, state)
    assert captured["kwargs"]["context"] is assembled.context  # type: ignore[index]


def test_task_store_idempotency_and_restart_recovery(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path / "tasks")
    store.ensure_writable()
    assert not list((tmp_path / "tasks").glob(".task-store-probe.*"))
    first = store.create(
        task_id="task-1",
        session_id="session-1",
        invocation_id="inv-1",
        idempotency_key="idem-1",
        request_hash=request_digest("request-1"),
    )
    replay = store.create(
        task_id="task-2",
        session_id="session-1",
        invocation_id="inv-2",
        idempotency_key="idem-1",
        request_hash=request_digest("request-1"),
    )
    assert replay.run_id == first.run_id
    with pytest.raises(IdempotencyConflict):
        store.create(
            task_id="task-3",
            session_id="session-1",
            invocation_id="inv-3",
            idempotency_key="idem-1",
            request_hash=request_digest("different"),
        )

    first.status = TaskStatus.RUNNING
    first.state_string = '{"sdk":"state"}'
    store.save(first)
    recovered = store.recover()
    assert recovered[0].status is TaskStatus.PAUSED
    assert store.get("task-1").error == "recovered_after_restart"


def test_task_store_concurrent_same_key_is_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "tasks"

    def _create(index: int):
        return FileTaskStore(root).create(
            task_id=f"task-concurrent-{index}",
            session_id="session-1",
            invocation_id=f"inv-{index}",
            idempotency_key="idem-concurrent",
            request_hash=request_digest("same-request"),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(_create, range(8)))
    assert len({record.run_id for record in records}) == 1
    assert len(list(root.glob("task-concurrent-*.json"))) == 1


def test_task_store_rejects_oversized_idempotency_key(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path / "tasks")
    with pytest.raises(ValueError, match="idempotency_key"):
        store.create(
            task_id="task-long-key",
            session_id="session-1",
            invocation_id="inv-1",
            idempotency_key="x" * 256,
            request_hash=request_digest("request"),
        )


def test_task_store_cancellation_wins_over_stale_completion(tmp_path: Path) -> None:
    """A late runner snapshot may not replace a cancelled terminal latch."""

    store = FileTaskStore(tmp_path / "tasks")
    running = store.create(
        task_id="task-race",
        session_id="session-1",
        invocation_id="inv-1",
        idempotency_key="idem-race",
        request_hash=request_digest("request-race"),
    )
    running.status = TaskStatus.RUNNING
    running = store.save(running)
    # Simulate a runner retaining an old in-memory snapshot while a different
    # request atomically latches cancellation in the durable store.
    stale_runner = type(running).from_json(running.to_json())
    assert store.cancel("task-race").status is TaskStatus.CANCELLED
    stale_runner.status = TaskStatus.COMPLETED
    stale_runner.result = "late answer"
    with pytest.raises(TaskStoreConflict):
        store.save(stale_runner)
    canonical = store.get("task-race")
    assert canonical.status is TaskStatus.CANCELLED
    assert canonical.result is None


def test_task_store_corrupt_or_noncanonical_record_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TaskStoreCorruption):
        FileTaskStore(root).create(
            task_id="task-new",
            session_id="session-1",
            invocation_id="inv-1",
            idempotency_key="idem-new",
            request_hash=request_digest("hash-new"),
        )

    store = FileTaskStore(tmp_path / "schema")
    record = store.create(
        task_id="task-schema",
        session_id="session-1",
        invocation_id="inv-1",
        idempotency_key="idem-schema",
        request_hash=request_digest("hash-schema"),
    )
    payload = record.to_dict()
    payload["unexpected"] = True
    store.path_for(record.task_id).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TaskStoreCorruption):
        store.create(
            task_id="task-other",
            session_id="session-1",
            invocation_id="inv-2",
            idempotency_key="idem-other",
            request_hash=request_digest("hash-other"),
        )


@pytest.mark.asyncio
async def test_single_task_runner_persists_history_and_step_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Result:
        final_output = "完成答案"
        interruptions: tuple[object, ...] = ()

    async def _fake_run_once(*args: object, **kwargs: object) -> _Result:
        return _Result()

    monkeypatch.setattr("anila_agent.runtime.task.run_once", _fake_run_once)
    store = FileTaskStore(tmp_path / "tasks")
    runner = SingleTaskRunner(store)
    timeline = TimelineEmitter(
        task_id="task-1",
        trace_id="trace-1",
        agent_id="research-agent",
        session_id="session-1",
        run_id="run-1",
        classification=Classification.UNCLASSIFIED,
        invocation_id="inv-1",
    )
    record = await runner.run(
        task_id="task-1",
        session_id="session-1",
        invocation_id="inv-1",
        idempotency_key="idem-1",
        user_input="請整理資料",
        assembled=SimpleNamespace(agent=object(), context=object(), max_turns=4),  # type: ignore[arg-type]
        timeline=timeline,
    )
    assert record.status is TaskStatus.COMPLETED
    assert record.history[-1] == {"role": "assistant", "content": "完成答案"}
    assert record.events
    assert {event["schema_version"] for event in record.events} == {"step-event/v1"}
    assert record.events[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_persists_terminal_latch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return SimpleNamespace(final_output="late", interruptions=())

    monkeypatch.setattr("anila_agent.runtime.task.run_once", _blocked)
    runner = SingleTaskRunner(FileTaskStore(tmp_path / "tasks"))
    task = runner.start(
        task_id="task-cancel",
        session_id="session-1",
        invocation_id="inv-1",
        idempotency_key="idem-cancel",
        user_input="cancel me",
        assembled=SimpleNamespace(agent=object(), context=object(), max_turns=4),  # type: ignore[arg-type]
    )
    await started.wait()
    cancelled = runner.cancel("task-cancel")
    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.store.get("task-cancel").status is TaskStatus.CANCELLED

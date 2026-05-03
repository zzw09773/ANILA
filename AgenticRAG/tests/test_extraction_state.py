"""Tests for B1 — persistent extraction cursor."""

from __future__ import annotations

import time


from agentic_rag.memory.extraction_state import CursorRecord, CursorStore
from agentic_rag.memory.extract_memories import MemoryExtractor


# ── CursorStore unit tests ────────────────────────────────────────────


def test_get_returns_none_for_missing_session(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    assert store.get("never-seen") is None


def test_set_then_get_round_trips(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    store.set("session_1", "msg-uuid-abc")
    record = store.get("session_1")
    assert record is not None
    assert record.session_id == "session_1"
    assert record.last_message_uuid == "msg-uuid-abc"
    assert record.saved_at > 0


def test_set_overwrites_previous_cursor(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    store.set("s1", "uuid_1")
    store.set("s1", "uuid_2")
    assert store.get("s1").last_message_uuid == "uuid_2"


def test_stale_cursor_returns_none(tmp_path) -> None:
    """Cursor older than stale_after_seconds is treated as missing."""
    store = CursorStore(state_dir=tmp_path, stale_after_seconds=1.0)
    store.set("s1", "uuid_1")
    # Simulate stale by sleeping briefly past the threshold.
    time.sleep(1.1)
    assert store.get("s1") is None


def test_delete_removes_persisted_cursor(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    store.set("s1", "uuid_1")
    store.delete("s1")
    assert store.get("s1") is None


def test_delete_missing_session_is_no_op(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    store.delete("never-existed")  # must not raise


def test_unsafe_session_id_is_sanitised_into_filename(tmp_path) -> None:
    """Session ids with slashes / dots must not escape state_dir."""
    store = CursorStore(state_dir=tmp_path)
    store.set("../escape/attempt", "uuid")
    record = store.get("../escape/attempt")
    assert record is not None
    # Cursor file should live inside state_dir, with chars sanitised.
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert "/" not in files[0].name and "\\" not in files[0].name


def test_corrupted_file_returns_none(tmp_path) -> None:
    """Malformed JSON on disk must not crash the read."""
    store = CursorStore(state_dir=tmp_path)
    # Write garbage by hand.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s1.json").write_text("{not json", encoding="utf-8")
    assert store.get("s1") is None


def test_record_is_stale_uses_explicit_now(tmp_path) -> None:
    rec = CursorRecord(
        session_id="s1",
        last_message_uuid="u1",
        saved_at=time.time() - 100,
    )
    assert rec.is_stale(stale_after_seconds=50.0) is True
    assert rec.is_stale(stale_after_seconds=200.0) is False


# ── MemoryExtractor wiring ────────────────────────────────────────────


def test_extractor_loads_persisted_cursor_lazily(tmp_path) -> None:
    """First run() reads persisted cursor; in-memory state had been None."""
    store = CursorStore(state_dir=tmp_path)
    store.set("session_x", "preserved-uuid")

    extractor = MemoryExtractor(
        memory_dir="/tmp/mem",
        cursor_store=store,
        session_id="session_x",
    )
    # Pre-call: in-memory cursor still None.
    assert extractor.last_message_uuid is None

    # Trigger lazy load via the private method (avoid full run() which
    # needs a forked-agent runner).
    extractor._maybe_load_persisted_cursor()
    assert extractor.last_message_uuid == "preserved-uuid"


def test_extractor_does_not_overwrite_caller_set_cursor(tmp_path) -> None:
    """If caller pre-populated last_message_uuid, persisted cursor doesn't override."""
    store = CursorStore(state_dir=tmp_path)
    store.set("s1", "from-disk")

    extractor = MemoryExtractor(
        memory_dir="/tmp/mem",
        cursor_store=store,
        session_id="s1",
        last_message_uuid="from-caller",
    )
    extractor._maybe_load_persisted_cursor()
    assert extractor.last_message_uuid == "from-caller"


def test_extractor_works_without_cursor_store(tmp_path) -> None:
    """No store configured → behaviour identical to before."""
    extractor = MemoryExtractor(memory_dir="/tmp/mem")
    extractor._maybe_load_persisted_cursor()  # no-op
    assert extractor.last_message_uuid is None


def test_extractor_persist_writes_to_store(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    extractor = MemoryExtractor(
        memory_dir="/tmp/mem",
        cursor_store=store,
        session_id="s1",
        last_message_uuid="advance-1",
    )
    extractor._persist_cursor()
    assert store.get("s1").last_message_uuid == "advance-1"


def test_extractor_persist_no_op_when_no_store_or_session(tmp_path) -> None:
    """Persist must be a silent no-op when prerequisites missing."""
    # No store
    extractor1 = MemoryExtractor(
        memory_dir="/tmp/mem", session_id="s1", last_message_uuid="x"
    )
    extractor1._persist_cursor()  # no raise

    # No session_id
    extractor2 = MemoryExtractor(
        memory_dir="/tmp/mem",
        cursor_store=CursorStore(state_dir=tmp_path),
        last_message_uuid="x",
    )
    extractor2._persist_cursor()
    assert not list(tmp_path.iterdir())  # nothing written


def test_extractor_reset_clears_persisted_cursor(tmp_path) -> None:
    store = CursorStore(state_dir=tmp_path)
    extractor = MemoryExtractor(
        memory_dir="/tmp/mem",
        cursor_store=store,
        session_id="s1",
        last_message_uuid="x",
    )
    extractor._persist_cursor()
    assert store.get("s1") is not None

    extractor.reset()
    assert store.get("s1") is None
    assert extractor.last_message_uuid is None

"""Tests for Sprint 13 PR A2 — session ownership persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from anila_core.api.session_owner import (
    ensure_session_owner,
    get_session_owner,
    get_session_owner_record,
    set_session_owner,
)
from anila_core.memory import close_all_connections
from anila_core.memory.sqlite_session import SqliteSession


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "owners.db"
    # Initialise schema by opening a SqliteSession; the helper module
    # piggy-backs on that connection.
    SqliteSession(db, "warmup")
    yield str(db.resolve())
    await close_all_connections()


@pytest.mark.asyncio
async def test_get_unknown_session_returns_none(db_path: str) -> None:
    assert await get_session_owner(db_path, "no-such-sid") is None


@pytest.mark.asyncio
async def test_set_then_get_roundtrip(db_path: str) -> None:
    await set_session_owner(db_path, "sid-1", "agent-foo")
    assert await get_session_owner(db_path, "sid-1") == "agent-foo"


@pytest.mark.asyncio
async def test_set_overwrites_existing_owner(db_path: str) -> None:
    await set_session_owner(db_path, "sid-x", "agent-a")
    await set_session_owner(db_path, "sid-x", "agent-b")
    assert await get_session_owner(db_path, "sid-x") == "agent-b"


@pytest.mark.asyncio
async def test_distinct_sessions_isolated(db_path: str) -> None:
    await set_session_owner(db_path, "sid-1", "agent-a")
    await set_session_owner(db_path, "sid-2", "agent-b")
    assert await get_session_owner(db_path, "sid-1") == "agent-a"
    assert await get_session_owner(db_path, "sid-2") == "agent-b"


@pytest.mark.asyncio
async def test_persists_across_helper_calls(db_path: str) -> None:
    """Connection cache reuse — same db_path must surface the prior write."""
    await set_session_owner(db_path, "sid-9", "agent-z")
    # Drop nothing; just call again. If the cache mis-handled commits the
    # second read would race or return None.
    await set_session_owner(db_path, "sid-10", "agent-q")
    assert await get_session_owner(db_path, "sid-9") == "agent-z"
    assert await get_session_owner(db_path, "sid-10") == "agent-q"


@pytest.mark.asyncio
async def test_ensure_session_owner_re_reads_persisted_hash(db_path: str) -> None:
    assert await ensure_session_owner(db_path, "sid-owner", "hash-a") is True
    assert await ensure_session_owner(db_path, "sid-owner", "hash-a") is True
    assert await ensure_session_owner(db_path, "sid-owner", "hash-b") is False


@pytest.mark.asyncio
async def test_ensure_session_owner_rejects_legacy_agent_row_without_hash(
    db_path: str,
) -> None:
    await set_session_owner(db_path, "sid-legacy", "agent-old")

    assert await ensure_session_owner(db_path, "sid-legacy", "hash-new") is False
    record = await get_session_owner_record(db_path, "sid-legacy")
    assert record is not None
    assert record.agent_id == "agent-old"
    assert record.owner_key_hash is None

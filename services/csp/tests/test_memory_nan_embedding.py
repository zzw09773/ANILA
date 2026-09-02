"""A non-finite embedding must not reach PostgreSQL.

OOBE walk 2026-09-02: the embedder returned an all-NaN query vector for one
turn. ``memory_service.retrieve`` sent it straight into
``embedding <=> CAST(:vec AS halfvec)``; pgvector refused (``NaN not allowed
in halfvec``), the request's transaction was left aborted, and the *next*
statement in the same turn — loading the user's attachments — failed with
``InFailedSqlTransaction``. The model then answered "the attachment contains
no text": a silent wrong answer caused by a memory lookup that is supposed
to be best-effort.

Invariant: an embedding containing NaN/inf is treated exactly like an embed
failure — logged, retrieval returns nothing, persist skips the vector — and
no SQL carrying the vector is executed.
"""

from __future__ import annotations

import logging
import math
from unittest.mock import AsyncMock, patch

import pytest

from app.services import memory_service as ms


def _nan_vector(dim: int = 8) -> list[float]:
    return [math.nan] * dim


class _RecordingSession:
    """Stands in for the SQLAlchemy session: any execute is a test failure."""

    def __init__(self):
        self.executed = []

    def execute(self, *args, **kwargs):  # pragma: no cover - must not be reached
        self.executed.append(args)
        raise AssertionError("SQL executed with a non-finite vector")


@pytest.mark.asyncio
async def test_retrieve_with_nan_embedding_returns_nothing_and_runs_no_sql(caplog, monkeypatch):
    """Kill: drop the finiteness check → the fake session's execute raises."""
    db = _RecordingSession()
    monkeypatch.setattr(ms, "get_setting", lambda _db, key: {"memory.retrieve_top_k": 3, "memory.retrieve_min_cosine": 0.4}[key])
    with patch.object(ms, "_embed", new=AsyncMock(return_value=(_nan_vector(), "nv-embed-v2", 8))):
        with caplog.at_level(logging.WARNING, logger=ms.logger.name):
            result = await ms.retrieve_relevant_chunks(db, 6, "附件裡第三行寫了什麼？")
    assert result == []
    assert db.executed == []
    assert any("non-finite" in r.getMessage() or "NaN" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_inf_is_refused_like_nan(monkeypatch):
    db = _RecordingSession()
    monkeypatch.setattr(ms, "get_setting", lambda _db, key: {"memory.retrieve_top_k": 3, "memory.retrieve_min_cosine": 0.4}[key])
    vec = [0.1] * 7 + [math.inf]
    with patch.object(ms, "_embed", new=AsyncMock(return_value=(vec, "nv-embed-v2", 8))):
        assert await ms.retrieve_relevant_chunks(db, 6, "x") == []
    assert db.executed == []


def test_vector_is_finite_helper():
    assert ms._vector_is_finite([0.0, 1.5, -2.0])
    assert not ms._vector_is_finite([0.0, math.nan])
    assert not ms._vector_is_finite([math.inf])
    assert not ms._vector_is_finite([])


@pytest.mark.asyncio
async def test_failed_retrieval_sql_does_not_poison_the_session(db, monkeypatch):
    """The second half of the incident: even when the vector is fine, a failing
    retrieval statement must not leave the request's transaction aborted —
    the attachment query that follows in the same session has to work.

    The test DB is SQLite, where the halfvec SQL fails by itself, which is
    exactly the shape we want: a DB-level failure inside retrieval.
    Kill: run the SELECT outside a savepoint → the session is left in a failed
    state / the exception escapes.
    """
    from sqlalchemy import text as _text
    monkeypatch.setattr(ms, "get_setting", lambda _db, key: {"memory.retrieve_top_k": 3, "memory.retrieve_min_cosine": 0.4}[key])
    with patch.object(ms, "_embed", new=AsyncMock(return_value=([0.1] * 8, "nv-embed-v2", 8))):
        result = await ms.retrieve_relevant_chunks(db, 6, "附件裡第三行寫了什麼？")
    assert result == []
    # the same session is still usable for the rest of the turn
    assert db.execute(_text("SELECT 1")).scalar() == 1

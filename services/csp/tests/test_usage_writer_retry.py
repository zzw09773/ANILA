"""Each worker flushes its own queue, and a failed commit keeps the rows."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.services import usage_writer as uw


class _Down:
    def bulk_insert_mappings(self, model, batch):
        raise RuntimeError("db down")

    def commit(self):
        raise AssertionError("commit")

    def rollback(self):
        return None

    def close(self):
        return None


class _Ok:
    def __init__(self) -> None:
        self.committed = False

    def bulk_insert_mappings(self, model, batch):
        self.batch = list(batch)

    def commit(self):
        self.committed = True

    def rollback(self):
        return None

    def close(self):
        return None


def test_failed_commit_returns_the_same_rows(monkeypatch):
    monkeypatch.setattr(uw, "SessionLocal", lambda: _Down())
    row = {"user_id": 1, "model_id": 2, "total_tokens": 3, "outcome": "partial"}
    left = asyncio.run(uw._flush_batch([row]))
    assert left == [row]


def test_successful_commit_returns_nothing(monkeypatch):
    db = _Ok()
    monkeypatch.setattr(uw, "SessionLocal", lambda: db)
    left = asyncio.run(uw._flush_batch([{"user_id": 1}]))
    assert left == []
    assert db.committed is True


def test_usage_writer_starts_outside_the_leader_loops():
    text = Path(__file__).resolve().parents[1].joinpath("app/main.py").read_text(
        encoding="utf-8"
    )
    start = text.split("async def _start_singleton_loops", 1)[1]
    body = start.split("return [task for task in tasks if task is not None]", 1)[0]
    assert "start_usage_writer" not in body
    assert "usage_writer_task = await start_usage_writer()" in text

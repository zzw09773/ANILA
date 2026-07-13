from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app.services import usage_writer


def test_transient_retry_exhaustion_does_not_poison_split(monkeypatch):
    calls: list[list[dict]] = []
    batch = [{"user_id": 1}, {"user_id": 2}]

    def fail_transient(rows):
        calls.append(rows)
        raise OperationalError("insert", {}, RuntimeError("connection lost"))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(usage_writer, "_write_batch", fail_transient)
    monkeypatch.setattr(usage_writer.asyncio, "sleep", no_sleep)

    with pytest.raises(OperationalError):
        asyncio.run(usage_writer._flush_batch(batch))

    assert calls == [batch, batch, batch]

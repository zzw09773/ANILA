"""Invariant: _poll_trace_spans must not hold a transaction across sleep."""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import sessionmaker


def test_poll_trace_spans_releases_transaction_before_sleep(db_engine, monkeypatch):
    """Rollback must land before await sleep, not after the next SELECT.

    Revert to ``rollback(); select; if empty: await sleep`` and
    ``in_transaction()`` is True inside the sleep stub.
    """
    from app.api.agents import health as health_mod

    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    db = Session()
    try:
        seen_in_txn: list[bool] = []
        past_deadline = {"v": False}
        start = 1_000_000.0

        async def slow_sleep(_interval):
            seen_in_txn.append(db.in_transaction())
            past_deadline["v"] = True

        def fake_monotonic():
            return start + (100.0 if past_deadline["v"] else 0.0)

        monkeypatch.setattr(health_mod.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(health_mod.asyncio, "sleep", slow_sleep)

        rows = asyncio.run(
            health_mod._poll_trace_spans(
                db, "trace-no-spans", timeout_s=10.0, interval_s=0.25
            )
        )
        assert rows == []
        assert seen_in_txn, "sleep stub never ran"
        assert all(v is False for v in seen_in_txn), (
            f"transaction still open during sleep: {seen_in_txn}"
        )
        assert not db.in_transaction()
    finally:
        db.close()

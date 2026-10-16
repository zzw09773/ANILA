"""MCP 的 DNS 與模擬 HTTP 等候不占用請求的資料庫連線。"""
from __future__ import annotations

import pytest
from sqlalchemy import event

from app.services import quick_mcp as mcp
from tests.test_quick_mcp import (
    approve,
    invoke,
    people as people,
    register,
    remote as remote,
    run,
    synthetic_keys as synthetic_keys,
)


@pytest.mark.parametrize("operation", ["register", "refresh", "approve", "call"])
@pytest.mark.parametrize("expire_on_commit", [False, True])
def test_network_boundaries_release_database_connection(
    db, people, remote, monkeypatch, operation, expire_on_commit
):
    service = None
    if operation != "register":
        service = register(db, people)
    if operation in ("refresh", "call"):
        service = approve(db, people, service)

    db.expire_on_commit = expire_on_commit
    db.commit()
    pool = {"out": 0}
    observations = []

    def checkout(*_):
        pool["out"] += 1

    def checkin(*_):
        pool["out"] -= 1

    engine = db.get_bind()
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    guard = mcp._guard_endpoint_async

    async def observe(url):
        observations.append((db.in_transaction(), pool["out"]))
        assert not db.in_transaction()
        assert pool["out"] == 0
        return await guard(url)

    monkeypatch.setattr(mcp, "_guard_endpoint_async", observe)
    try:
        if operation == "register":
            register(db, people)
        elif operation == "refresh":
            run(mcp.refresh_service(db, people["admin"], service["id"]))
        elif operation == "approve":
            approve(db, people, service)
        else:
            invoke(db, people, service)
        assert len(observations) == (4 if operation in ("register", "call") else 3)
        assert all(row == (False, 0) for row in observations)
    finally:
        db.rollback()
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)

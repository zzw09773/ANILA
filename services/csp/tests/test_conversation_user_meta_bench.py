# -*- coding: utf-8 -*-
"""Inv 4 — list stays O(1) extra queries with meta enrichment."""
from __future__ import annotations

import os
import time

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationUserMeta
from app.utils.security import create_access_token
from tests.conftest import make_user


def test_list_query_budget_with_meta(client: TestClient, db: Session, db_engine):
    """Mutant: N+1 get_user_meta in the list loop → QUERY_COUNT ≫ 12."""
    user = make_user(db, username="bench_meta")
    for i in range(200):
        c = Conversation(user_id=user.id, title=f"c{i}")
        db.add(c)
        db.flush()
        if i % 2 == 0:
            db.add(ConversationUserMeta(
                user_id=user.id,
                conversation_id=c.id,
                starred=True,
                folder="usr-a",
                user_tags=["t"],
            ))
    db.commit()
    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    })
    h = {"Authorization": f"Bearer {token}"}
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", _count)
    try:
        client.get("/api/conversations", headers=h)
        statements.clear()
        t0 = time.perf_counter()
        resp = client.get("/api/conversations", headers=h)
        ms = (time.perf_counter() - t0) * 1000
    finally:
        event.remove(db_engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    assert len(resp.json()) == 200
    print(f"\nBENCH QUERY_COUNT={len(statements)} WALL_MS={ms:.2f} N=200")
    # Auth + share lookup + conversation list + one meta batch ≪ N.
    assert len(statements) <= 12, f"too many queries: {len(statements)}"

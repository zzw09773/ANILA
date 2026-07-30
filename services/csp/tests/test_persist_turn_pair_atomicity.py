"""Invariant: persist_turn pair atomicity must survive pool-release commits.

``_embed`` releases the pooled connection via ``db.commit()``. If a user-chunk
INSERT is already pending when the assistant embed runs, that commit makes the
user chunk durable and a later assistant-embed failure can no longer roll it
back. Base behaviour: assistant embed failure → zero durable chunks.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.database import Base
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services import memory_service
from app.utils.security import hash_password


@pytest.fixture()
def memory_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(memory_service, "SessionLocal", Session)

    db = Session()
    user = User(
        username="mem-atomic-user",
        hashed_password=hash_password("password"),
        role="user",
        is_active=True,
        is_approved=True,
    )
    emb = ModelRegistry(
        name=memory_service._EMBED_MODEL_NAME,
        display_name="embed",
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
    )
    db.add_all([user, emb])
    db.commit()
    user_id = user.id
    db.close()
    yield Session, user_id
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_assistant_embed_failure_leaves_no_orphan_user_chunk(memory_db, monkeypatch):
    """Revert persist_turn to sequential _write_chunk and this goes red.

    Tracks staged vs durable roles across the ``db.commit()`` that ``_embed``
    uses for pool release — the same split the bug introduced.
    """
    Session, user_id = memory_db
    state = {"pending": [], "durable": []}

    calls = {"n": 0}

    async def flaky_embed(db, text_input):
        # Mirror production ``_embed``: commit releases the pool and makes any
        # pending INSERT durable before the outbound call.
        state["durable"].extend(state["pending"])
        state["pending"].clear()
        db.commit()
        calls["n"] += 1
        if calls["n"] == 1:
            return [0.1] * 8
        raise RuntimeError("assistant embed boom")

    def track_insert(db, **kwargs):
        state["pending"].append(kwargs["role"])

    monkeypatch.setattr(memory_service, "_embed", flaky_embed)
    monkeypatch.setattr(memory_service, "_insert_chunk", track_insert)
    monkeypatch.setattr(
        memory_service, "_extract_facts", AsyncMock(return_value=[])
    )

    asyncio.run(
        memory_service.persist_turn(
            user_id=user_id,
            conversation_id=1,
            user_message="hello user",
            assistant_message="hello assistant",
            is_encrypted=False,
        )
    )

    assert state["durable"] == [], (
        f"expected no durable chunks after assistant-embed failure, "
        f"got durable={state['durable']!r} pending={state['pending']!r}"
    )

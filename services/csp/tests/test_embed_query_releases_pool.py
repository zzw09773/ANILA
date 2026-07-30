"""Invariant: retrieval embed path must not pin a pooled connection across HTTP.

Proves the connection count in use drops during a stubbed slow embedder, via
SQLAlchemy pool checkout instrumentation — not merely that ``close()`` is called.

Also proves Inv3: attributes consumed after ``db.commit()`` come from a plain
snapshot, not a lazy ORM reload (which would re-checkout under expire).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.database import Base
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.utils.security import hash_password


def test_embed_query_releases_pooled_connection_during_outbound_http(monkeypatch):
    from app.api.ingestion import search as search_mod

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=0,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    checkout_count = {"n": 0}
    checkin_count = {"n": 0}

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):  # noqa: ARG001
        checkout_count["n"] += 1

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, connection_record):  # noqa: ARG001
        checkin_count["n"] += 1

    db = Session()
    try:
        user = User(
            username="pool-embed-user",
            hashed_password=hash_password("password"),
            role="user",
            is_active=True,
            is_approved=True,
        )
        model = ModelRegistry(
            name="nv-embed-pool-test",
            display_name="nv-embed-pool-test",
            model_type="embedding",
            endpoint_url="http://embedder.test/v1",
            api_version="v1",
            classification_ceiling="密",
            is_active=True,
        )
        db.add_all([user, model])
        db.commit()

        dim = 8
        checked_out_during_http: list[int] = []
        seen_model_types: list[str] = []

        async def slow_proxy(**kwargs):
            m = kwargs["model"]
            seen_model_types.append(type(m).__name__)
            # Snapshot occupancy while the "outbound" call is in flight.
            checked_out_during_http.append(engine.pool.checkedout())
            # Touch attrs proxy would read — must not re-checkout.
            _ = (
                m.id,
                m.name,
                m.model_type,
                m.endpoint_url,
                m.api_version,
                m.api_key_secret_ref,
                m.classification_ceiling,
            )
            checked_out_during_http.append(engine.pool.checkedout())
            await asyncio.sleep(0.05)
            checked_out_during_http.append(engine.pool.checkedout())
            return {"data": [{"embedding": [0.1] * dim}]}

        monkeypatch.setattr(search_mod, "proxy_request", slow_proxy)

        before_checkouts = checkout_count["n"]
        vec = asyncio.run(
            search_mod._embed_query(
                db, user, "nv-embed-pool-test", dim, "release me"
            )
        )

        assert len(vec) == dim
        assert checkout_count["n"] > before_checkouts, (
            "registry lookup must have checked out a pooled connection"
        )
        assert checkin_count["n"] >= 1, (
            "connection must have been checked back in before/during HTTP"
        )
        assert checked_out_during_http, "slow proxy stub never ran"
        assert all(n == 0 for n in checked_out_during_http), (
            f"pooled connection still held during outbound call: "
            f"{checked_out_during_http}"
        )
        assert seen_model_types == ["SimpleNamespace"], (
            f"proxy_request must receive a plain snapshot, got {seen_model_types}"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_embed_query_snapshot_survives_orm_expire(monkeypatch):
    """If release moves before the snapshot, expiring the ORM row breaks proxy.

    Simulates the expire_on_commit=True hazard: after commit, expire the ORM
    instance. The snapshot must still supply every attr proxy_request reads,
    including classification_ceiling — without triggering a checkout.
    """
    from app.api.ingestion import search as search_mod

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=0,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):  # noqa: ARG001
        pass

    db = Session()
    try:
        user = User(
            username="pool-snap-user",
            hashed_password=hash_password("password"),
            role="user",
            is_active=True,
            is_approved=True,
        )
        model = ModelRegistry(
            name="nv-embed-snap-test",
            display_name="nv-embed-snap-test",
            model_type="embedding",
            endpoint_url="http://embedder.test/v1",
            api_version="v1",
            classification_ceiling="營業秘密",
            is_active=True,
        )
        db.add_all([user, model])
        db.commit()

        dim = 4
        captured = {}

        async def capturing_proxy(**kwargs):
            m = kwargs["model"]
            captured["ceiling"] = m.classification_ceiling
            captured["endpoint"] = m.endpoint_url
            captured["kind"] = type(m)
            captured["checkedout"] = engine.pool.checkedout()
            return {"data": [{"embedding": [0.2] * dim}]}

        monkeypatch.setattr(search_mod, "proxy_request", capturing_proxy)

        # Wrap _embed_query's commit so we expire the ORM model immediately
        # after release — any path that still handed the ORM instance to
        # proxy_request would re-checkout on attribute access.
        real_commit = db.commit

        def commit_and_expire():
            real_commit()
            db.expire(model)

        monkeypatch.setattr(db, "commit", commit_and_expire)

        vec = asyncio.run(
            search_mod._embed_query(
                db, user, "nv-embed-snap-test", dim, "snap me"
            )
        )
        assert len(vec) == dim
        assert captured["kind"] is SimpleNamespace
        assert captured["ceiling"] == "營業秘密"
        assert captured["endpoint"] == "http://embedder.test/v1"
        assert captured["checkedout"] == 0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()

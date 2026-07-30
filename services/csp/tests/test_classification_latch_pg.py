"""PostgreSQL-only proofs for the classification latch FOR UPDATE half.

SQLite's ``with_for_update()`` is a silent no-op and the suite's StaticPool
shares one connection across "sessions", so the SQLite latch tests cannot
exercise row locking. This module uses two real connections against
PostgreSQL with a forced barrier between SELECT and UPDATE.

Skipped unless ``ANILA_TEST_PG_DSN`` is set. Does not touch docker, project
volumes, or the running platform database name (creates a throwaway DB via
``CREATE DATABASE`` on the same cluster, then ``Base.metadata.create_all`` —
never alembic against the live ``csp`` database).

How to run
==========

.. code-block:: bash

    cd services/csp
    ANILA_TEST_PG_DSN=postgresql://USER:PASS@127.0.0.1:PORT/postgres \\
      PYTHONPATH=../../packages/anila-core/src:../../packages/anila-agent:. \\
      pytest tests/test_classification_latch_pg.py -v

Use a maintenance DB name in the URL; the fixture creates
``latch_pg_<hex>`` and drops it afterward.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from urllib.parse import urlparse, urlunparse

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "ANILA_TEST_PG_DSN not set — needs PostgreSQL that can "
        "CREATE DATABASE (FOR UPDATE is a no-op on SQLite)"
    ),
)


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _dbname_of(dsn: str) -> str:
    path = urlparse(dsn).path or ""
    return path.lstrip("/") or "postgres"


@pytest.fixture(scope="module")
def pg_engine():
    """CREATE DATABASE → metadata.create_all → yield engine → DROP DATABASE."""
    assert _DSN
    from sqlalchemy import create_engine

    # Import models so Base.metadata is fully populated.
    import app.models  # noqa: F401
    from app.database import Base

    admin_db = _dbname_of(_DSN)
    scratch = f"latch_pg_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to use the DSN's own database"
    # Never mutate the live platform DB name.
    assert scratch != "csp"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = admin.cursor()
    try:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        cur.close()
        admin.close()

    scratch_dsn = _scratch_url(_DSN, scratch)
    engine = create_engine(scratch_dsn, pool_size=10, max_overflow=10)
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()
        admin = psycopg2.connect(_DSN)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = admin.cursor()
        try:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (scratch,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        finally:
            cur.close()
            admin.close()


@pytest.fixture()
def Session(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine, expire_on_commit=False)


def _seed_conversation(Session):
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.utils.security import hash_password

    s = Session()
    try:
        user = User(
            username=f"latch-pg-{uuid.uuid4().hex[:8]}",
            hashed_password=hash_password("password"),
            role="admin",
            is_active=True,
            is_approved=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        conv = Conversation(user_id=user.id, title="latch-pg")
        s.add(conv)
        s.commit()
        s.refresh(conv)
        return conv.id, str(user.id)
    finally:
        s.close()


def test_noop_does_not_block_concurrent_for_update(Session):
    """No-op latch must release RowShareLock so a peer FOR UPDATE does not wait.

    Revert the no-op ``db.commit()`` in ``apply_classification`` and the peer
    blocks for the full hold window.
    """
    from sqlalchemy import text

    from app.modules.policy import apply_classification
    from app.schemas.contracts.classification import ClassificationLevel

    conv_id, actor_id = _seed_conversation(Session)

    holder = Session()
    try:
        # Raise to 機密 first so the next call is a true no-op.
        apply_classification(
            holder,
            resource_type="conversation",
            resource_id=str(conv_id),
            new_level=ClassificationLevel.SECRET.value,
            actor_type="user",
            actor_id=actor_id,
            reason="manual_admin",
        )
        ev = apply_classification(
            holder,
            resource_type="conversation",
            resource_id=str(conv_id),
            new_level=ClassificationLevel.SECRET.value,
            actor_type="user",
            actor_id=actor_id,
            reason="manual_admin",
        )
        assert ev is None
        assert not holder.in_transaction()

        waited = {"t": None}

        def peer():
            p = Session()
            try:
                t0 = time.monotonic()
                p.execute(
                    text(
                        "SELECT 1 FROM conversations WHERE id = :i FOR UPDATE"
                    ),
                    {"i": conv_id},
                )
                waited["t"] = time.monotonic() - t0
                p.rollback()
            finally:
                p.close()

        th = threading.Thread(target=peer)
        th.start()
        th.join(timeout=5.0)
        assert waited["t"] is not None, "peer FOR UPDATE did not complete"
        assert waited["t"] < 1.0, (
            f"peer FOR UPDATE blocked {waited['t']:.2f}s — no-op still holding lock"
        )
    finally:
        holder.close()


def test_sql_failure_after_for_update_clears_aborted_txn(Session, monkeypatch):
    """DB failure after FOR UPDATE must not leave 25P02 for later ops.

    Revert the ``except: db.rollback(); raise`` wrapper in
    ``apply_classification`` and the subsequent query fails with
    InFailedSqlTransaction / 25P02. SQLite cannot prove this (it clears
    the txn on statement error).
    """
    from sqlalchemy import text

    import app.modules.policy.service as svc
    from app.models.conversation import Conversation
    from app.modules.policy import apply_classification

    conv_id, actor_id = _seed_conversation(Session)

    def boom(db_arg, **kwargs):
        db_arg.execute(text("SELECT * FROM __no_such_table_latch_poison__"))

    monkeypatch.setattr(svc, "_write_event", boom)

    db = Session()
    try:
        with pytest.raises(Exception):
            apply_classification(
                db,
                resource_type="conversation",
                resource_id=str(conv_id),
                new_level="營業秘密",
                actor_type="user",
                actor_id=actor_id,
                reason="manual_admin",
            )
        # Same session must still work (proxy swallows and continues).
        assert (
            db.query(Conversation.id)
            .filter(Conversation.id == conv_id)
            .scalar()
            == conv_id
        )
    finally:
        db.close()


def test_for_update_barrier_keeps_max_under_interleave(Session, monkeypatch):
    """Two real connections + barrier between SELECT and UPDATE → max wins.

    A naive "start both at once" harness reports 0 failures for the broken
    (bare db.get) code too — the barrier is the whole point. Revert
    ``_resolve_resource`` to ignore ``for_update`` (bare ``db.get``) and this
    test accumulates persisted downgrades.
    """
    from sqlalchemy import text

    import app.modules.policy.service as svc
    from app.modules.policy import apply_classification, effective_level
    from app.schemas.contracts.classification import ClassificationLevel

    conv_id, actor_id = _seed_conversation(Session)

    barrier_box: dict[str, threading.Barrier | None] = {"b": None}
    real_write = svc._write_event

    def hooked(db, **kwargs):
        b = barrier_box["b"]
        if b is not None:
            try:
                b.wait(timeout=4.0)
            except threading.BrokenBarrierError:
                pass
        return real_write(db, **kwargs)

    monkeypatch.setattr(svc, "_write_event", hooked)

    rounds = 8
    downgrades = 0
    for r in range(rounds):
        s = Session()
        try:
            s.execute(
                text(
                    "UPDATE conversations SET classification_level = '無機密' "
                    "WHERE id = :i"
                ),
                {"i": conv_id},
            )
            s.commit()
        finally:
            s.close()

        barrier_box["b"] = threading.Barrier(2)
        order = [("機密", "營業秘密"), ("營業秘密", "機密")][r % 2]
        errors: list[str] = []

        def racer(level: str) -> None:
            db = Session()
            try:
                apply_classification(
                    db,
                    resource_type="conversation",
                    resource_id=str(conv_id),
                    new_level=level,
                    actor_type="user",
                    actor_id=actor_id,
                    reason="manual_admin",
                )
            except Exception as exc:  # noqa: BLE001 — collect for assert
                errors.append(repr(exc))
            finally:
                db.close()

        threads = [threading.Thread(target=racer, args=(lvl,)) for lvl in order]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "racer thread hung (lock deadlock?)"

        verify = Session()
        try:
            lvl = effective_level(
                verify,
                resource_type="conversation",
                resource_id=str(conv_id),
            )
            if lvl != ClassificationLevel.SECRET:
                downgrades += 1
        finally:
            verify.close()

    assert downgrades == 0, (
        f"FOR UPDATE interleave persisted {downgrades}/{rounds} downgrades"
    )

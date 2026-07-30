"""PostgreSQL-only proof: non-admin cannot persist an agent-level downgrade.

X.6 — two real connections + a forced barrier between the stale-row read
and the write. SQLite cannot prove this (shared fixture connection;
``with_for_update`` is a silent no-op).

Skipped unless ``ANILA_TEST_PG_DSN`` is set. Creates a throwaway DB via
``CREATE DATABASE`` — never alembic against the live platform ``csp`` DB.

How to run
==========

.. code-block:: bash

    cd services/csp
    ANILA_TEST_PG_DSN=postgresql://USER:PASS@127.0.0.1:PORT/postgres \\
      PYTHONPATH=../../packages/anila-core/src:../../packages/anila-agent:. \\
      pytest tests/test_agent_classification_race_pg.py -v
"""

from __future__ import annotations

import os
import threading
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
        "CREATE DATABASE (agent downgrade race is invisible on SQLite)"
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
    assert _DSN
    from sqlalchemy import create_engine

    import app.models  # noqa: F401
    from app.database import Base

    admin_db = _dbname_of(_DSN)
    scratch = f"agentcls_pg_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to use the DSN's own database"
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


def _seed_agent(Session, *, level: str = "無機密") -> int:
    from app.models.agent import Agent
    from app.models.user import User
    from app.utils.security import hash_password

    s = Session()
    try:
        user = User(
            username=f"agentcls-{uuid.uuid4().hex[:8]}",
            hashed_password=hash_password("password"),
            role="developer",
            is_active=True,
            is_approved=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        agent = Agent(
            name=f"agentcls-{uuid.uuid4().hex[:8]}",
            owner_user_id=user.id,
            endpoint_url="http://agent:9100",
            description_for_router="X.6 race harness",
            approval_status="registered",
            default_classification_level=level,
            requires_encryption=level in ("密", "機密"),
        )
        s.add(agent)
        s.commit()
        s.refresh(agent)
        return agent.id
    finally:
        s.close()


def _broken_orm_apply(agent, level, *, db=None, allow_downgrade=False):
    """Mutant: plain ORM write ignoring the conditional UPDATE (pre-X.6)."""
    from app.api.agents._common import (
        _classification_write_barrier,
        effective_agent_policy_level,
        parse_stored_classification_level,
        requires_controlled_access,
    )

    previous_stored = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    previous_effective = effective_agent_policy_level(agent)
    stored_transition = (previous_stored, level)
    new_bool = requires_controlled_access(level)
    level_changed = previous_stored != level
    bool_changed = bool(getattr(agent, "requires_encryption", False)) != new_bool
    if not level_changed and not bool_changed:
        return None, False, stored_transition
    _classification_write_barrier()
    agent.default_classification_level = level.to_storage()
    agent.requires_encryption = new_bool
    new_effective = effective_agent_policy_level(agent)
    if previous_effective != new_effective:
        return (previous_effective, new_effective), True, stored_transition
    return None, True, stored_transition


def _run_barrier_race(Session, monkeypatch, *, apply_fn, rounds: int = 8) -> int:
    """Force read→barrier→write interleave; count persisted 機密→密.

    Each round starts committed at 機密. Both threads SELECT, then wait on
    a barrier. The stale thread overwrites its identity-map view to 無機密
    (refuse then passes for a write of 密). Without the conditional
    UPDATE, that write can land and persist a downgrade.
    """
    from sqlalchemy import text

    import app.api.agents._common as common
    from app.models.agent import Agent
    from app.models.user import User
    from app.schemas.contracts.classification import ClassificationLevel

    agent_id = _seed_agent(Session, level="機密")
    monkeypatch.setattr(common, "apply_default_classification_level", apply_fn)

    downgrades = 0
    for _ in range(rounds):
        s = Session()
        try:
            s.execute(
                text(
                    "UPDATE agents SET default_classification_level = '機密', "
                    "requires_encryption = true WHERE id = :i"
                ),
                {"i": agent_id},
            )
            s.commit()
        finally:
            s.close()

        barrier = threading.Barrier(2)
        errors: list[str] = []

        def keep_secret() -> None:
            db = Session()
            try:
                agent = db.query(Agent).filter(Agent.id == agent_id).one()
                owner = (
                    db.query(User).filter(User.id == agent.owner_user_id).one()
                )
                common.refuse_classification_downgrade(
                    agent, ClassificationLevel.SECRET, owner
                )
                try:
                    barrier.wait(timeout=4.0)
                except threading.BrokenBarrierError:
                    pass
                common.apply_default_classification_level(
                    agent,
                    ClassificationLevel.SECRET,
                    db=db,
                    allow_downgrade=False,
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
                db.rollback()
            finally:
                db.close()

        def stale_lower() -> None:
            db = Session()
            try:
                agent = db.query(Agent).filter(Agent.id == agent_id).one()
                owner = (
                    db.query(User).filter(User.id == agent.owner_user_id).one()
                )
                # Stale session-local view (committed row stays 機密).
                agent.default_classification_level = "無機密"
                agent.requires_encryption = False
                common.refuse_classification_downgrade(
                    agent, ClassificationLevel.RESTRICTED, owner
                )
                try:
                    barrier.wait(timeout=4.0)
                except threading.BrokenBarrierError:
                    pass
                common.apply_default_classification_level(
                    agent,
                    ClassificationLevel.RESTRICTED,
                    db=db,
                    allow_downgrade=False,
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
                db.rollback()
            finally:
                db.close()

        threads = [
            threading.Thread(target=keep_secret),
            threading.Thread(target=stale_lower),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), f"racer hung; errors={errors}"

        verify = Session()
        try:
            lvl = (
                verify.query(Agent.default_classification_level)
                .filter(Agent.id == agent_id)
                .scalar()
            )
            if lvl != "機密":
                downgrades += 1
        finally:
            verify.close()

    return downgrades


def test_conditional_update_barrier_blocks_downgrade(Session, monkeypatch):
    """Fixed path: 0 persisted downgrades over 8 barrier-forced attempts."""
    import app.api.agents._common as common

    # Bind the real function object before any patching.
    real_apply = common.apply_default_classification_level
    downgrades = _run_barrier_race(
        Session, monkeypatch, apply_fn=real_apply, rounds=8
    )
    assert downgrades == 0, (
        f"conditional UPDATE still persisted {downgrades}/8 downgrades"
    )


def test_mutant_orm_write_produces_downgrades(Session, monkeypatch):
    """Revert to plain ORM assign → some rounds persist 機密→密."""
    downgrades = _run_barrier_race(
        Session, monkeypatch, apply_fn=_broken_orm_apply, rounds=8
    )
    assert downgrades > 0, (
        "mutant ORM write produced 0/8 downgrades — barrier not engaging"
    )

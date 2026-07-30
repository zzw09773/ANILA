"""P4.7 — PostgreSQL FK / migration invariants for agent_collection_bindings.

Runs the **shipped** alembic chain (``alembic upgrade head``, including
r1_0017) against a disposable scratch database — never a retyped DDL
copy of the migration. Diverging the test from the migration is
therefore impossible.

Invariant 1: after a bound collection is deleted, the scalar mirror must
still name a surviving member of the set (not NULL while the set is
non-empty). Proven by the shipped INSERT/DELETE sync trigger.

Invariant 2: downgrade must collapse every non-empty junction set into
``agents.bound_collection_id`` before DROP, so a subsequent upgrade
restores a non-empty singleton. Silent unbind is forbidden; set→scalar
lossy collapse to MIN is accepted. Proven by calling the shipped
``downgrade()`` / ``upgrade()`` callables from r1_0017 after head.

Skipped unless ``ANILA_TEST_PG_DSN`` is set. Does not touch project
volumes, docker, or the running platform database name (creates a
throwaway DB via ``CREATE DATABASE`` on the same cluster).

How to run (dev / CI)
=====================

Point ``ANILA_TEST_PG_DSN`` at a PostgreSQL URL whose role can
``CREATE DATABASE`` and run the full alembic chain (needs pgvector —
same image as ``csp-db``). Typical local form against a maintenance DB
on the compose Postgres (NOT the live ``csp`` database name):

.. code-block:: bash

    cd services/csp
    ANILA_TEST_PG_DSN=postgresql://csp:SECRET@127.0.0.1:5433/postgres \\
      PYTHONPATH=../../packages/anila-core/src:../../packages/anila-agent:. \\
      pytest tests/test_agent_collection_bindings_pg.py -v

Use a maintenance DB name in the URL (``postgres`` or any existing DB);
the fixture creates ``p47_acb_<hex>`` and drops it afterward. Never set
the DSN path to the live platform database and run ``alembic upgrade``
on that name — this fixture refuses to migrate the DSN's own database.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "ANILA_TEST_PG_DSN not set — needs PostgreSQL that can "
        "CREATE DATABASE + alembic upgrade head (pgvector)"
    ),
)

_CSP_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _CSP_ROOT / "alembic.ini"


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _dbname_of(dsn: str) -> str:
    path = urlparse(dsn).path or ""
    return path.lstrip("/") or "postgres"


@pytest.fixture(scope="module")
def migrated_pg():
    """CREATE DATABASE → alembic upgrade head → yield DSN → DROP DATABASE."""
    assert _DSN
    admin_db = _dbname_of(_DSN)
    scratch = f"p47_acb_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to migrate the DSN's own database"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = admin.cursor()
    try:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        cur.close()
        admin.close()

    scratch_dsn = _scratch_url(_DSN, scratch)
    prev_mig = os.environ.get("MIGRATION_DATABASE_URL")
    prev_db = os.environ.get("DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = scratch_dsn
    os.environ["DATABASE_URL"] = scratch_dsn
    try:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(_ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", scratch_dsn)
        # cwd matters for alembic script_location relative paths.
        old_cwd = os.getcwd()
        os.chdir(_CSP_ROOT)
        try:
            command.upgrade(cfg, "head")
        finally:
            os.chdir(old_cwd)
        yield scratch_dsn
    finally:
        if prev_mig is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = prev_mig
        if prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_db

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


@pytest.fixture
def pg(migrated_pg):
    conn = psycopg2.connect(migrated_pg)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        yield conn, cur, migrated_pg
    finally:
        cur.close()
        conn.close()


def _insert_owner(cur, suffix: str) -> int:
    cur.execute(
        "INSERT INTO users (username, hashed_password, role, is_approved) "
        "VALUES (%s, 'x', 'developer', true) RETURNING id",
        (f"p47_owner_{suffix}",),
    )
    return cur.fetchone()[0]


def _insert_collections(cur, owner_id: int, suffix: str) -> tuple[int, int]:
    cfg = json.dumps({"strategy": "fixed"})
    cur.execute(
        """
        INSERT INTO ingestion_collections
          (name, chunking_config, embedding_model, embedding_dim, created_by)
        VALUES
          (%s, %s::jsonb, 'nvidia/NV-embed-V2', 4000, %s),
          (%s, %s::jsonb, 'nvidia/NV-embed-V2', 4000, %s)
        RETURNING id
        """,
        (f"c1_{suffix}", cfg, owner_id, f"c2_{suffix}", cfg, owner_id),
    )
    rows = cur.fetchall()
    return rows[0][0], rows[1][0]


def _insert_agent(cur, *, name: str, owner_id: int, mirror) -> int:
    cur.execute(
        """
        INSERT INTO agents (
          name, endpoint_url, description_for_router, owner_user_id,
          bound_collection_id
        ) VALUES (%s, 'http://agent.example', %s, %s, %s)
        RETURNING id
        """,
        (name, name, owner_id, mirror),
    )
    return cur.fetchone()[0]


def test_alembic_head_installed_r1_0017_objects(pg):
    """Sanity: shipped upgrade created the junction table + sync trigger."""
    _conn, cur, _dsn = pg
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name = 'agent_collection_bindings'
        """
    )
    assert cur.fetchone() is not None

    cur.execute(
        """
        SELECT 1 FROM pg_trigger t
          JOIN pg_class c ON c.oid = t.tgrelid
         WHERE c.relname = 'agent_collection_bindings'
           AND t.tgname = 'trg_acb_sync_bound_collection_mirror'
           AND NOT t.tgisinternal
        """
    )
    assert cur.fetchone() is not None

    cur.execute(
        """
        SELECT 1 FROM pg_proc
         WHERE proname = 'sync_agent_bound_collection_mirror'
        """
    )
    assert cur.fetchone() is not None


def test_mirror_repoints_to_surviving_member_after_collection_delete(pg):
    """Invariant 1: delete the mirrored collection → mirror = surviving id."""
    _conn, cur, _dsn = pg
    suffix = uuid.uuid4().hex[:8]
    owner_id = _insert_owner(cur, suffix)
    c1, c2 = _insert_collections(cur, owner_id, suffix)
    mirror, other = (c1, c2) if c1 < c2 else (c2, c1)

    agent_id = _insert_agent(
        cur, name=f"a_{suffix}", owner_id=owner_id, mirror=mirror
    )
    cur.execute(
        "INSERT INTO agent_collection_bindings(agent_id, collection_id) "
        "VALUES (%s, %s), (%s, %s)",
        (agent_id, c1, agent_id, c2),
    )
    cur.execute(
        "SELECT bound_collection_id FROM agents WHERE id = %s", (agent_id,)
    )
    assert cur.fetchone()[0] == mirror

    cur.execute("DELETE FROM ingestion_collections WHERE id = %s", (mirror,))

    cur.execute(
        "SELECT collection_id FROM agent_collection_bindings "
        "WHERE agent_id = %s ORDER BY 1",
        (agent_id,),
    )
    remaining = [r[0] for r in cur.fetchall()]
    assert remaining == [other]

    cur.execute(
        "SELECT bound_collection_id FROM agents WHERE id = %s", (agent_id,)
    )
    assert cur.fetchone()[0] == other, (
        "mirror must name the surviving member, not NULL"
    )


def test_downgrade_collapse_then_upgrade_keeps_previously_bound_agents(pg):
    """Invariant 2: 0/1/2-binding agents survive shipped downgrade→upgrade."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    import migrations.versions.r1_0017_agent_collection_bindings as rev

    _conn, cur, scratch_dsn = pg
    suffix = uuid.uuid4().hex[:8]
    owner_id = _insert_owner(cur, f"dg_{suffix}")
    c1, c2 = _insert_collections(cur, owner_id, f"dg_{suffix}")

    a0 = _insert_agent(
        cur, name=f"unbound_{suffix}", owner_id=owner_id, mirror=None
    )
    a1 = _insert_agent(
        cur, name=f"one_{suffix}", owner_id=owner_id, mirror=c1
    )
    cur.execute(
        "INSERT INTO agent_collection_bindings(agent_id, collection_id) "
        "VALUES (%s, %s)",
        (a1, c1),
    )
    a2 = _insert_agent(
        cur, name=f"two_{suffix}", owner_id=owner_id, mirror=c1
    )
    cur.execute(
        "INSERT INTO agent_collection_bindings(agent_id, collection_id) "
        "VALUES (%s, %s), (%s, %s)",
        (a2, c1, a2, c2),
    )
    # Drift mirror to NULL without firing the sync trigger so downgrade
    # collapse is forced to repair it.
    cur.execute("ALTER TABLE agent_collection_bindings DISABLE TRIGGER ALL")
    cur.execute(
        "UPDATE agents SET bound_collection_id = NULL WHERE id = %s", (a2,)
    )
    cur.execute("ALTER TABLE agent_collection_bindings ENABLE TRIGGER ALL")
    cur.execute("SELECT bound_collection_id FROM agents WHERE id = %s", (a2,))
    assert cur.fetchone()[0] is None

    original = {a0: set(), a1: {c1}, a2: {c1, c2}}

    # Call the shipped revision callables (not a retyped SQL copy).
    engine = create_engine(scratch_dsn)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as sa_conn:
        mc = MigrationContext.configure(sa_conn)
        with Operations.context(mc):
            rev.downgrade()
            rev.upgrade()

    cur.execute(
        "SELECT agent_id, collection_id FROM agent_collection_bindings "
        "WHERE agent_id = ANY(%s) ORDER BY agent_id, collection_id",
        ([a0, a1, a2],),
    )
    restored: dict[int, set[int]] = {a0: set(), a1: set(), a2: set()}
    for agent_id, cid in cur.fetchall():
        restored[agent_id].add(cid)

    assert restored[a0] == set()
    assert restored[a1] == {c1}
    assert restored[a2], "previously 2-bound agent must not come back unbound"
    assert restored[a2].issubset(original[a2])
    assert len(restored[a2]) == 1  # lossy collapse → singleton

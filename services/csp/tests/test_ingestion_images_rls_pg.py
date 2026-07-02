"""Postgres integration test for the ingestion_images RLS design (#116).

RLS is a Postgres-only feature, so this cannot run on the SQLite test DB. It is
SKIPPED unless ``ANILA_TEST_PG_DSN`` points at a Postgres a *superuser* can use
(the superuser creates the fixtures + a throwaway NOBYPASSRLS role to play the
app role under RLS). It verifies, against a self-contained table that mirrors
migration 0037's policy + resolver:

  * a NOBYPASSRLS role sees ONLY rows whose collection_id == the
    ``anila.collection_id`` GUC (nothing when unset);
  * INSERT is rejected when the row's collection_id != the GUC (WITH CHECK
    reuses the USING expr under FORCE RLS);
  * the SECURITY DEFINER resolver returns the collection_id regardless of the
    GUC / RLS (the by-PK blob path's escape from the chicken-and-egg).

Run: ``ANILA_TEST_PG_DSN=postgresql://csp:csp@127.0.0.1:5533/csp pytest \
        tests/test_ingestion_images_rls_pg.py``
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_TEST_PG_DSN (superuser) not set — RLS needs Postgres"
)

_POLICY = "collection_id = NULLIF(current_setting('anila.collection_id', true), '')::int"


async def _run() -> dict:
    tbl = f"_rls_img_{uuid.uuid4().hex[:8]}"
    fn = f"_rls_resolve_{uuid.uuid4().hex[:8]}"
    role = f"_rls_app_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(_DSN)
    out: dict = {}
    try:
        # ── fixtures (as superuser) — mirror migration 0037 ──
        await conn.execute(
            f"CREATE TABLE {tbl} (id bigserial primary key, "
            f"collection_id int not null, val text)"
        )
        await conn.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        await conn.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        await conn.execute(f"CREATE POLICY p ON {tbl} FOR ALL USING ({_POLICY})")
        await conn.execute(
            f"CREATE FUNCTION {fn}(p_id bigint) RETURNS integer "
            f"LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public "
            f"AS $f$ SELECT collection_id FROM {tbl} WHERE id=p_id $f$"
        )
        await conn.execute(f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS")
        await conn.execute(f"GRANT ALL ON {tbl} TO {role}")
        await conn.execute(f"GRANT USAGE, SELECT ON SEQUENCE {tbl}_id_seq TO {role}")
        await conn.execute(f"GRANT EXECUTE ON FUNCTION {fn}(bigint) TO {role}")

        # seed two collections' rows (as superuser; needs the GUC under FORCE)
        async with conn.transaction():
            await conn.execute("SET LOCAL anila.collection_id = 1")
            await conn.execute(f"INSERT INTO {tbl}(collection_id,val) VALUES ($1,$2)", 1, "a")
            await conn.execute(f"INSERT INTO {tbl}(collection_id,val) VALUES ($1,$2)", 1, "b")
        async with conn.transaction():
            await conn.execute("SET LOCAL anila.collection_id = 2")
            row2_id = await conn.fetchval(
                f"INSERT INTO {tbl}(collection_id,val) VALUES ($1,$2) RETURNING id", 2, "x"
            )

        # ── act as the NOBYPASSRLS app role ──
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            await conn.execute("SET LOCAL anila.collection_id = 1")
            out["visible_c1"] = await conn.fetchval(f"SELECT count(*) FROM {tbl}")
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            await conn.execute("SET LOCAL anila.collection_id = 2")
            out["visible_c2"] = await conn.fetchval(f"SELECT count(*) FROM {tbl}")
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            out["visible_noguc"] = await conn.fetchval(f"SELECT count(*) FROM {tbl}")
        # wrong-collection INSERT blocked
        out["wrong_insert_blocked"] = False
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL ROLE {role}")
                await conn.execute("SET LOCAL anila.collection_id = 1")
                await conn.execute(f"INSERT INTO {tbl}(collection_id,val) VALUES ($1,$2)", 2, "evil")
        except asyncpg.exceptions.InsufficientPrivilegeError:
            # An RLS WITH CHECK violation is reported as 42501
            # (insufficient_privilege), NOT 23514 (check_violation).
            out["wrong_insert_blocked"] = True
        # resolver bypass: app role, no GUC → direct read hidden, resolver returns it
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {role}")
            out["direct_noguc"] = await conn.fetchval(
                f"SELECT collection_id FROM {tbl} WHERE id=$1", row2_id
            )
            out["resolver"] = await conn.fetchval(f"SELECT {fn}($1)", row2_id)
        return out
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
        await conn.execute(f"DROP FUNCTION IF EXISTS {fn}(bigint)")
        await conn.execute(f"DROP ROLE IF EXISTS {role}")
        await conn.close()


def test_ingestion_images_rls_design():
    r = asyncio.run(_run())
    assert r["visible_c1"] == 2, r          # GUC=1 sees both collection-1 rows
    assert r["visible_c2"] == 1, r          # GUC=2 sees only the collection-2 row
    assert r["visible_noguc"] == 0, r       # no GUC → RLS hides everything
    assert r["wrong_insert_blocked"], r     # INSERT into a foreign collection rejected
    assert r["direct_noguc"] is None, r     # app role can't read the row without the GUC
    assert r["resolver"] == 2, r            # SECURITY DEFINER resolver bypasses RLS

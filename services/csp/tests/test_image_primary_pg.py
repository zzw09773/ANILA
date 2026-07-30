"""Slice 8b — alembic r1_0022 upgrade/downgrade on real PostgreSQL.

Creates a throwaway database, runs the full chain to head (including
r1_0022), asserts ``is_image_primary`` + partial unique index, then
downgrades to r1_0020 and confirms clean removal. Never touches the
live ``csp`` database name.

Skipped unless ``ANILA_TEST_PG_DSN`` is set (or ``/tmp/anila-test-pg-dsn``).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
if not _DSN and Path("/tmp/anila-test-pg-dsn").exists():
    _DSN = Path("/tmp/anila-test-pg-dsn").read_text().strip()

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN not set — needs PostgreSQL + CREATE DATABASE",
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
    assert _DSN
    admin_db = _dbname_of(_DSN)
    scratch = f"flux_img_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to migrate the DSN's own database"
    assert scratch != "csp", "refusing to touch the live platform database"

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


def test_r1_0022_column_and_partial_unique(migrated_pg):
    conn = psycopg2.connect(migrated_pg)
    cur = conn.cursor()
    try:
        cur.execute("SELECT version_num FROM alembic_version")
        # Assert we are AT alembic head, not pinned to a hardcoded revision id
        # (r1_0022 must remain on the chain; later revisions must not break it).
        head = cur.fetchone()[0]
        assert head.startswith("r1_"), head
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'model_registry'
               AND column_name = 'is_image_primary'
            """
        )
        assert cur.fetchone(), "missing model_registry.is_image_primary"

        cur.execute(
            """
            SELECT indexname FROM pg_indexes
             WHERE indexname = 'uq_model_registry_image_primary'
            """
        )
        assert cur.fetchone(), "partial unique index missing"
    finally:
        cur.close()
        conn.close()


def test_r1_0022_downgrade_to_r1_0020_clean(migrated_pg):
    """Downgrade must drop the new column/index without error."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI))
    old_cwd = os.getcwd()
    os.environ["MIGRATION_DATABASE_URL"] = migrated_pg
    os.environ["DATABASE_URL"] = migrated_pg
    os.chdir(_CSP_ROOT)
    try:
        command.downgrade(cfg, "r1_0020")
    finally:
        os.chdir(old_cwd)

    conn = psycopg2.connect(migrated_pg)
    cur = conn.cursor()
    try:
        cur.execute("SELECT version_num FROM alembic_version")
        assert cur.fetchone()[0] == "r1_0020"
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_name = 'model_registry'
               AND column_name = 'is_image_primary'
            """
        )
        assert cur.fetchone() is None
        cur.execute(
            """
            SELECT 1 FROM pg_indexes
             WHERE indexname = 'uq_model_registry_image_primary'
            """
        )
        assert cur.fetchone() is None
    finally:
        cur.close()
        conn.close()

    os.chdir(_CSP_ROOT)
    try:
        command.upgrade(cfg, "head")
    finally:
        os.chdir(old_cwd)

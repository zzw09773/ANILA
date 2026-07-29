# -*- coding: utf-8 -*-
"""P1.1 — departments three-level tree (院 → 所 → 組).

SYSTEM-MAP requires a parent/child department tree so usage rollup (P1.2)
and unit-admin scoping (P1.3) can walk descendants. The prior schema was a
flat ``departments`` table with no ``parent_id``.

This migration adds nullable ``parent_id`` (self-FK, ``ON DELETE RESTRICT``)
plus a partial index on non-NULL parents. NULL parent = root (院). Depth is
derived in application code and hard-capped at 3 — no level/path columns.

Idempotency
===========

Upgrade uses raw ``IF NOT EXISTS`` / ``pg_constraint`` guards (same idiom as
``0028_chunk_parent_child``) so a DB that already gained ``parent_id`` via
``create_all`` + legacy ``startup_migrations`` does not wedge on
``DuplicateColumn``. Downgrade drops are similarly tolerant (``IF EXISTS``).

SQLite note: the partial index predicate is emitted for Postgres here;
SQLite receives the matching partial index only via SQLAlchemy
``create_all`` / model ``sqlite_where`` (acceptable for the test env).

Revision ID: r1_0009
Revises: r1_0008
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0009"
down_revision: Union[str, None] = "r1_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Column first (no inline FK): IF NOT EXISTS survives legacy backfill.
    op.execute(
        """
        ALTER TABLE departments
          ADD COLUMN IF NOT EXISTS parent_id INTEGER
        """
    )
    # Named FK — guard via pg_constraint so re-runs / wedge DBs are safe.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'fk_departments_parent_id'
               AND conrelid = 'departments'::regclass
          ) THEN
            ALTER TABLE departments
              ADD CONSTRAINT fk_departments_parent_id
              FOREIGN KEY (parent_id) REFERENCES departments(id)
              ON DELETE RESTRICT;
          END IF;
        END $$;
        """
    )
    # Partial index — parity with model postgresql_where.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_departments_parent_id
          ON departments (parent_id)
          WHERE parent_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # Tolerant drops: index → FK → column.
    op.execute("DROP INDEX IF EXISTS ix_departments_parent_id")
    op.execute(
        "ALTER TABLE departments DROP CONSTRAINT IF EXISTS fk_departments_parent_id"
    )
    op.execute("ALTER TABLE departments DROP COLUMN IF EXISTS parent_id")

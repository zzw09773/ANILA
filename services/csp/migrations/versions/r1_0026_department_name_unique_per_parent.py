# -*- coding: utf-8 -*-
"""Department names are unique per parent, not globally.

``departments.name`` carried a global ``UNIQUE`` from ``0001_initial_schema``
(``departments_name_key``). That was survivable while the table was flat, but
the real org shape is 院部 → 研究所／中心 → 組／科 and two different 所 can each
run a 企劃組. A global unique rejects the second one.

Replaces it with two partial unique indexes (mirrors the model
``__table_args__``):

* ``uq_departments_parent_id_name`` — UNIQUE (parent_id, name)
  WHERE parent_id IS NOT NULL
* ``uq_departments_root_name`` — UNIQUE (name) WHERE parent_id IS NULL

Two indexes, not one, because PostgreSQL treats NULLs in a unique index as
distinct: a plain UNIQUE (parent_id, name) would happily accept two root
departments both called 院部.

Pre-existing violations
=======================

The old constraint was strictly stronger than the new pair, so data written
through it cannot violate the new rule. A database whose unique constraint was
lost along the way (hand-patched, or built by ``create_all`` on an old model)
still can. Rather than silently dropping or renaming rows — losing whatever
users and usage rows point at them — ``upgrade()`` aborts with the offending
(parent_id, name) groups listed, so an operator can merge or rename them and
re-run. Nothing is deleted by this migration.

Also creates the plain ``ix_departments_name`` lookup index the model declares
(``index=True``); it used to be served by the implicit index behind
``departments_name_key``, which this migration drops.

Revision ID: r1_0026
Revises: r1_0025
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0026"
down_revision: Union[str, None] = "r1_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DUPLICATE_SCAN = """
    SELECT parent_id, name, count(*) AS n
      FROM departments
     GROUP BY parent_id, name
    HAVING count(*) > 1
     ORDER BY n DESC, name
     LIMIT 20
"""


def _abort_on_duplicates() -> None:
    """Refuse to run if the new rule is already violated. Never deletes."""
    rows = op.get_bind().execute(sa.text(_DUPLICATE_SCAN)).fetchall()
    if not rows:
        return
    listed = "; ".join(
        f"parent_id={row[0] if row[0] is not None else 'NULL(根)'} name={row[1]!r} x{row[2]}"
        for row in rows
    )
    raise RuntimeError(
        "r1_0026 aborted: departments already contain duplicate names under the "
        "same parent, so the new partial unique indexes cannot be created. "
        "No rows were changed. Merge or rename these first, then re-run "
        f"`alembic upgrade head`. Offending groups (max 20): {listed}"
    )


def upgrade() -> None:
    _abort_on_duplicates()

    # Global UNIQUE(name) from 0001 — unnamed there, so Postgres called it
    # departments_name_key. IF EXISTS keeps DBs that never had it re-runnable.
    op.execute(
        "ALTER TABLE departments DROP CONSTRAINT IF EXISTS departments_name_key"
    )
    # A DB built by create_all on the pre-r1_0026 model has the uniqueness in a
    # unique index instead of a constraint; drop that shape too before the
    # non-unique lookup index below takes the same name.
    op.execute("DROP INDEX IF EXISTS ix_departments_name")

    op.execute("CREATE INDEX IF NOT EXISTS ix_departments_name ON departments (name)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_departments_parent_id_name
          ON departments (parent_id, name)
          WHERE parent_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_departments_root_name
          ON departments (name)
          WHERE parent_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_departments_root_name")
    op.execute("DROP INDEX IF EXISTS uq_departments_parent_id_name")
    op.execute("DROP INDEX IF EXISTS ix_departments_name")

    # Restoring the global UNIQUE is only possible if names happen to be
    # globally distinct again — which is exactly what r1_0026 stopped
    # requiring. If two 所 have each grown a 企劃組 since the upgrade, recreate
    # the pre-r1_0026 unique index shape instead of failing the downgrade, and
    # leave the global constraint off; the operator has to decide which name
    # wins before the flat model can be restored.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM departments GROUP BY name HAVING count(*) > 1) THEN
            ALTER TABLE departments ADD CONSTRAINT departments_name_key UNIQUE (name);
          ELSE
            RAISE WARNING 'r1_0026 downgrade: departments.name is no longer globally unique; '
                          'left the global UNIQUE off and restored the plain lookup index only. '
                          'Rename the duplicates and add departments_name_key by hand if needed.';
            CREATE INDEX IF NOT EXISTS ix_departments_name ON departments (name);
          END IF;
        END $$;
        """
    )

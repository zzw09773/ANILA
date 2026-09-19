# -*- coding: utf-8 -*-
"""Case-insensitive unique identity for model_registry.name.

``model_registry.name`` has been UNIQUE since 0001, but PostgreSQL's default
UNIQUE is case-sensitive. ``nvidia/NV-embed-V2`` and ``nvidia/nv-embed-v2``
could both exist; retrieval and designation then depend on which spelling
the caller happened to use (r1_0032 documented this as #56 item 8).

Adds a functional unique index on ``lower(name)``. The exact UNIQUE(name)
stays — lookups by the stored spelling still use it.

Pre-existing case-folded duplicates
===================================

If two rows already differ only by case, creating the index would fail
mid-statement. ``upgrade()`` aborts first with the colliding groups listed.
Nothing is renamed or deleted: the operator merges or retires one row, then
re-runs. Same shape as r1_0026.

Revision ID: r1_0043
Revises: r1_0042
Create Date: 2026-09-19
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0043"
down_revision: Union[str, None] = "r1_0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DUPLICATE_SCAN = """
    SELECT lower(name) AS lname,
           count(*) AS n,
           string_agg(name || ' (id=' || id::text || ')', ', ' ORDER BY id) AS names
      FROM model_registry
     GROUP BY lower(name)
    HAVING count(*) > 1
     ORDER BY n DESC, lname
     LIMIT 20
"""


def _abort_on_duplicates() -> None:
    """Refuse to run if lower(name) is already violated. Never deletes."""
    rows = op.get_bind().execute(sa.text(_DUPLICATE_SCAN)).fetchall()
    if not rows:
        return
    listed = "; ".join(
        f"lower={row[0]!r} x{row[1]} [{row[2]}]" for row in rows
    )
    raise RuntimeError(
        "r1_0043 aborted: model_registry already contains names that collide "
        "when compared case-insensitively, so uq_model_registry_name_lower "
        "cannot be created. No rows were changed. Merge or retire the extra "
        "spellings first, then re-run `alembic upgrade head`. Offending "
        f"groups (max 20): {listed}"
    )


def upgrade() -> None:
    _abort_on_duplicates()
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_registry_name_lower "
        "ON model_registry (lower(name))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_model_registry_name_lower")

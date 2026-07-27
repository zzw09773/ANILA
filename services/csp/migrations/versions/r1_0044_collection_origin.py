# -*- coding: utf-8 -*-
"""Add ``ingestion_collections.origin`` — product-surface provenance.

Revision ID: r1_0044
Revises: r1_0043
Create Date: 2026-07-27

⚠ Integrator: ``down_revision = "r1_0043"`` points at the real
messages-parent_id revision (not a bridge stub). Databases previously
migrated through a no-op stub at ``r1_0043`` must be dropped and rebuilt
— ``upgrade head`` will not replay the real DDL.

Why this exists
---------------
CSP governance UI and ANILALM ("你的知識庫") share one ``ingestion_collections``
table. Ownership (``created_by``) is not the right discriminator — a developer
may create a collection in CSP for a project and it must NOT appear in their
personal knowledge-base inventory. ``origin`` records WHERE the row was
created (``csp`` vs ``anilalm``). This is product inventory partitioning,
NOT an authorization boundary.

Assumptions
-----------
Product owner confirmed deployments are still test/dev with **zero**
existing collections. This migration refuses to proceed if any row exists
rather than guessing a backfill value.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0044"
down_revision: Union[str, None] = "r1_0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingestion_collections"
_COLUMN = "origin"
_CHECK = "ck_ingestion_collections_origin"
_ORIGINS_SQL = "'csp', 'anilalm'"


def _has_table(bind, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def _has_column(bind, table: str, column: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_constraint(bind, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return

    if not _has_column(bind, _TABLE, _COLUMN):
        # Refuse to invent provenance for pre-existing rows.
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {_TABLE}")
        ).scalar()
        if count and int(count) > 0:
            raise RuntimeError(
                f"{_TABLE} already has {count} row(s); refusing to add "
                f"NOT NULL {_COLUMN} without an agreed backfill. Stop and "
                "ask the product owner — do not guess csp vs anilalm."
            )
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(length=20),
                nullable=False,
                # Transient default so ADD COLUMN NOT NULL succeeds on an
                # empty table; dropped immediately below so inserts must
                # set origin explicitly (API surfaces do this server-side).
                server_default="csp",
            ),
        )
        op.alter_column(_TABLE, _COLUMN, server_default=None)

    if not _has_constraint(bind, _CHECK):
        op.create_check_constraint(
            _CHECK,
            _TABLE,
            f"{_COLUMN} IN ({_ORIGINS_SQL})",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    if _has_constraint(bind, _CHECK):
        op.drop_constraint(_CHECK, _TABLE, type_="check")
    if _has_column(bind, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)

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

import logging

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


logger = logging.getLogger("alembic.runtime.migration")


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
        # Pre-existing rows are backfilled to the governance surface, loudly.
        #
        # The first version refused outright when the table was non-empty, on
        # the grounds that provenance should not be invented. The intent was
        # right; the consequence was that this migration could not run on any
        # database that had ever held a collection — which is every test and
        # development database, and CI proved it by failing there. A migration
        # that cannot be applied is not a safety property, it is a broken
        # migration.
        #
        # 'csp' is the conservative direction, not an arbitrary pick: a row
        # wrongly marked 'csp' disappears from the personal product, while one
        # wrongly marked 'anilalm' puts an organisation's knowledge base into
        # someone's personal shelf — the exact failure this column exists to
        # prevent. The product owner confirmed (2026-07-27) that no deployment
        # holds collections yet, so in practice this only ever touches fixtures;
        # the warning is here so that if it ever does touch real rows, the
        # operator has a count to re-scope from rather than a silent rewrite.
        count = int(
            bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar() or 0
        )
        if count:
            logger.warning(
                "r1_0044: %s has %d pre-existing row(s); backfilling %s='csp' "
                "(governance surface). Re-scope any that belong to the personal "
                "product before users notice them missing there.",
                _TABLE,
                count,
                _COLUMN,
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

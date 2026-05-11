"""Admin-managed trusted-host allow-list for SSRF guard bypass.

Phase 2 of the inference-stack decoupling work. ``ANILA_TRUSTED_HOSTS``
env was Phase 1 — admin appends a service name, edits compose, restarts
CSP. Works but every model service requires a yaml edit + restart cycle.

This migration introduces a DB-backed table so admins can manage the
allow-list from a UI page (``/trusted-hosts``) without redeploying CSP.

The env stays valid as a bootstrap / fallback for agent / worker
contexts that never connect to CSP's DB. CSP's startup hook backfills
each env entry into this table once (idempotent on the unique
``host`` column) so the two sources never drift.

Authorisation: owner-only mutations, admin-tier can read (so admins
can audit what's trusted without being able to grow the surface).

Column layout:
- ``host`` is the bare hostname (no scheme / port / path) — same string
  the SSRF guard sees as ``urlparse().hostname``. Case-insensitive
  lookup; we store lowercase by convention but enforce in the service
  layer rather than via a DB constraint (admin UI normalises before
  POST).
- ``note`` is free-text shown in the admin UI — e.g. "gemma4 in
  anila-models-net for Phase 1 cutover". Helps the next admin
  understand why a host is trusted.
- ``created_by_user_id`` ties the row to whoever added it. NULL after
  the user is deleted (ON DELETE SET NULL keeps audit trail).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trusted_hosts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("host", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("trusted_hosts")

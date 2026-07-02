"""Sprint 13 PR A3 — per-agent runtime configuration column.

Adds ``agents.runtime_config`` JSONB so admins can edit (without code
changes or restarts):

  * Per-tool permission policy (allow_list, deny_list, ASK/DENY tools)
  * Workspace capability dict (max_bytes, allow_network, mounts, …)
  * Tool guardrail bundles (regex-block patterns, max output length, …)

The column is nullable; an unset row means "use whatever defaults the
agent code has hard-coded". Agents poll this value every 30 s
(Sprint 13 PR A4) and apply it to their next turn — no restart needed.

Schema decisions
================

  * JSONB so admins can extend the shape without DB migrations. The
    agent-side parser must tolerate unknown keys.
  * Nullable rather than ``DEFAULT '{}'`` so we can distinguish "admin
    has not customised this agent" (NULL → use code defaults) from
    "admin explicitly set empty config" (``{}`` → enforce empty
    permission lists, no guardrails). The hot-reload code branches on
    NULL vs empty-dict.
  * No CHECK constraint on the JSON shape — Pydantic schemas in
    ``app.api.agents`` validate inbound writes; older / newer versions
    of the agent-side parser may understand different shapes.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "agents" not in inspector.get_table_names():  # pragma: no cover
        return

    existing = {col["name"] for col in inspector.get_columns("agents")}
    if "runtime_config" in existing:
        return

    # JSONB on Postgres, JSON on SQLite (tests). Mirrors the
    # ``JSONValue`` pattern in app/models/agent.py.
    json_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.add_column(
        "agents",
        sa.Column("runtime_config", json_type, nullable=True),
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS runtime_config")

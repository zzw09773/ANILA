"""Add platform_links.is_public for "everyone with the right role can see" links.

Without this flag the access-control algorithm shipped in 0012 was strictly
default-deny: non-admin users see *no* links until an explicit grant exists.
That's the right policy for sensitive services (NotebookLM / codeserver /
GitLab), but it's wrong for the ANILA portal link itself, which should be
visible to everyone passing the role gate.

After this migration the algorithm becomes:

    active? → role gate? → admin bypass? → is_public? → grant exists?

``is_public`` slots between the admin bypass and the grant check, so a
public link still respects ``required_roles`` (e.g., "public to developers
only" is expressible).

We backfill **every existing row** to ``is_public = true`` to grandfather
the pre-migration behaviour where any active link was visible to any
authenticated user. New rows default to ``is_public = false`` so future
links must opt in to public visibility.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: add the column with DEFAULT false so the NOT NULL constraint
    # is satisfied for existing rows immediately. This is a constant-time
    # operation in PG 11+ (the default lives in the catalog, no table
    # rewrite).
    op.add_column(
        "platform_links",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Step 2: backfill every existing row to is_public=true to preserve the
    # pre-migration "any active link is visible to any user" behaviour.
    # New rows continue to default to false (private) — opt-in public.
    op.execute("UPDATE platform_links SET is_public = true")


def downgrade() -> None:
    op.drop_column("platform_links", "is_public")

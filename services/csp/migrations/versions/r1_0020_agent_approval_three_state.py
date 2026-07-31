# -*- coding: utf-8 -*-
"""OE-1 — agent approval_status seven-gate → three-state.

SYSTEM-MAP (知識庫怎麼運作): register → admin assigns → usable.
There is no connection / trace / security-review ceremony.

Maps existing rows onto ``registered`` / ``approved`` / ``disabled``
without making anything that was usable become unusable:

| old                        | new        | why                                      |
|----------------------------|------------|------------------------------------------|
| draft                      | registered | shadow inventory residue                 |
| pending                    | registered | pre-r1_0004 synonym of not-yet-live      |
| pending_connection_test    | registered | first-gate residue                       |
| pending_trace_test         | registered | second-gate residue                      |
| pending_security_review    | registered | third-gate residue; still not approved   |
| registered                 | registered | already OE-1                             |
| approved                   | approved   | usable stays usable                      |
| rejected                   | disabled   | rejected is outside the three-state set  |
| disabled                   | disabled   | already terminal off                     |

Also resets the column server_default from ``pending_connection_test``
to ``registered``. Does **not** drop ``trace_test_*`` / ``audit_level``
columns (diagnostic residue; D1 may retire the span tables later).

Revision ID: r1_0020
Revises: r1_0019
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0020"
down_revision: Union[str, None] = "r1_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE_CASE = """
UPDATE agents SET approval_status = CASE approval_status
  WHEN 'draft' THEN 'registered'
  WHEN 'pending' THEN 'registered'
  WHEN 'pending_connection_test' THEN 'registered'
  WHEN 'pending_trace_test' THEN 'registered'
  WHEN 'pending_security_review' THEN 'registered'
  WHEN 'registered' THEN 'registered'
  WHEN 'approved' THEN 'approved'
  WHEN 'rejected' THEN 'disabled'
  WHEN 'disabled' THEN 'disabled'
  ELSE 'registered'
END
"""

_DOWNGRADE_CASE = """
UPDATE agents SET approval_status = CASE approval_status
  WHEN 'registered' THEN 'pending_connection_test'
  WHEN 'approved' THEN 'approved'
  WHEN 'disabled' THEN 'disabled'
  ELSE 'pending_connection_test'
END
"""


def upgrade() -> None:
    op.execute(sa.text(_UPGRADE_CASE))
    op.alter_column(
        "agents",
        "approval_status",
        server_default="registered",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE_CASE))
    op.alter_column(
        "agents",
        "approval_status",
        server_default="pending_connection_test",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )

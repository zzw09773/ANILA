"""Add ``agent_prompts`` — per-agent developer-authored preset prompts.

Developers author a short list of preset prompts per agent in the CSP
console; the ANILA chat UI surfaces them for the active agent. Plain text
templates, no execution — per-project customisation for the air-gapped
deployment.

Revision ID: 0041
Revises: 0040
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_prompts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_agent_prompts_agent_id", "agent_prompts", ["agent_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_prompts_agent_id", table_name="agent_prompts")
    op.drop_table("agent_prompts")

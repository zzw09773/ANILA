"""Generalize ``agent_prompts`` → ``agent_functions`` (extensible).

0041 shipped a single hardcoded function type (preset prompts). The
platform needs developers to design DIFFERENT function kinds per agent
without a schema change each time, so this generalises the table into a
``kind`` + ``config`` (JSONB) container. Adding a new function kind later
is a frontend renderer + (optional) executor — no migration.

Initial kinds:
- ``preset_prompt``  config = {"text": str, "autosend": bool}
- ``prompt_action``  config = {"template": str}   (post-message action)

Revision ID: 0042
Revises: 0041
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("agent_prompts", "agent_functions")
    op.execute("ALTER INDEX ix_agent_prompts_agent_id RENAME TO ix_agent_functions_agent_id")

    op.add_column(
        "agent_functions",
        sa.Column(
            "kind",
            sa.String(length=40),
            nullable=False,
            server_default="preset_prompt",
        ),
    )
    op.add_column(
        "agent_functions",
        sa.Column(
            "config",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB, "postgresql"),
            nullable=False,
            server_default="{}",
        ),
    )

    # Backfill: existing rows are all preset prompts whose body lived in
    # prompt_text. Move it into config.text so the new code path reads
    # everything from config uniformly.
    op.execute(
        "UPDATE agent_functions "
        "SET config = jsonb_build_object('text', prompt_text, 'autosend', false) "
        "WHERE prompt_text IS NOT NULL"
    )

    op.drop_column("agent_functions", "prompt_text")


def downgrade() -> None:
    op.add_column(
        "agent_functions",
        sa.Column("prompt_text", sa.Text(), nullable=True),
    )
    op.execute("UPDATE agent_functions SET prompt_text = config->>'text'")
    op.drop_column("agent_functions", "config")
    op.drop_column("agent_functions", "kind")
    op.execute("ALTER INDEX ix_agent_functions_agent_id RENAME TO ix_agent_prompts_agent_id")
    op.rename_table("agent_functions", "agent_prompts")

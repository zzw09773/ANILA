"""Remove unused agent and model registry metadata columns.

Some databases may still have these nullable columns from an earlier
revision. They are not used by the application and are removed from the
current schema. The values are intentionally discarded.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0069"
down_revision: Union[str, None] = "r1_0068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "agents" in tables:
        existing_fks = {
            fk.get("name")
            for fk in inspector.get_foreign_keys("agents")
        }
        agent_columns = {
            column["name"] for column in inspector.get_columns("agents")
        }
        retired_agent_columns = (
            "aiia_doc_path",
            "vv_status",
            "last_reviewer_id",
            "source_commit_sha",
        )
        if (
            set(retired_agent_columns) & agent_columns
            or "fk_agents_last_reviewer_id" in existing_fks
        ):
            with op.batch_alter_table("agents") as batch_op:
                if "fk_agents_last_reviewer_id" in existing_fks:
                    batch_op.drop_constraint(
                        "fk_agents_last_reviewer_id", type_="foreignkey"
                    )
                for name in retired_agent_columns:
                    if name in agent_columns:
                        batch_op.drop_column(name)

    if "model_registry" in tables:
        model_columns = {
            column["name"] for column in inspector.get_columns("model_registry")
        }
        retired_model_columns = (
            "model_card_url",
            "training_dataset_ref",
            "weights_sha256",
            "intended_use",
            "limitations",
        )
        if set(retired_model_columns) & model_columns:
            with op.batch_alter_table("model_registry") as batch_op:
                for name in retired_model_columns:
                    if name in model_columns:
                        batch_op.drop_column(name)


def downgrade() -> None:
    raise RuntimeError(
        "This metadata removal is irreversible; restore a database backup to revert."
    )

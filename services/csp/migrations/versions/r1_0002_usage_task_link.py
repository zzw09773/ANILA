# -*- coding: utf-8 -*-
"""Slice 2b-C — token_usage ↔ task 連結(doc 04 AC10 usage 歸戶、doc 10
Slice 2 Done 舊流量標記)。

- ``token_usage.task_id``:nullable + FK tasks ON DELETE SET NULL(刪任務
  保留用量史,同 policy_decisions 的 SET NULL 姿勢)+ partial index
  (legacy 列佔多數、task_id 為 NULL,比照 0027 caller_* partial index)。
- ``token_usage.legacy_runtime_call``:boolean NOT NULL server_default
  false —— /v1 chat 無 task 的舊流量標記;非 chat 寫入者(embedding /
  judge / ingestion)維持 false。

Revision ID: r1_0002
Revises: r1_0001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0002"
down_revision: Union[str, None] = "r1_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "token_usage",
        sa.Column("task_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "token_usage",
        sa.Column(
            "legacy_runtime_call",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_foreign_key(
        "fk_token_usage_task_id_tasks",
        "token_usage",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_token_usage_task_id",
        "token_usage",
        ["task_id"],
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_token_usage_task_id", table_name="token_usage")
    op.drop_constraint(
        "fk_token_usage_task_id_tasks", "token_usage", type_="foreignkey"
    )
    op.drop_column("token_usage", "legacy_runtime_call")
    op.drop_column("token_usage", "task_id")

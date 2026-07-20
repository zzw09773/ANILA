# -*- coding: utf-8 -*-
"""Allow Agent usage without an informational base model.

Revision ID: r1_0018
Revises: r1_0017
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0018"
down_revision: Union[str, None] = "r1_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The runtime ORM has carried these attribution/latency columns and
    # dashboard indexes for years, but the Alembic chain never created them
    # on a genuinely fresh host.  Durable closure now writes the complete ORM
    # row, so repair that historical drift before relaxing model attribution.
    op.add_column(
        "token_usage", sa.Column("department_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_token_usage_department_id_departments",
        "token_usage",
        "departments",
        ["department_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "token_usage", sa.Column("request_duration_ms", sa.Integer(), nullable=True)
    )
    for index_name, columns in (
        ("idx_usage_user_time", ["user_id", "request_timestamp"]),
        ("idx_usage_department_time", ["department_id", "request_timestamp"]),
        ("idx_usage_model_time", ["model_id", "request_timestamp"]),
        ("idx_usage_timestamp", ["request_timestamp"]),
        ("idx_usage_apikey_time", ["api_key_id", "request_timestamp"]),
    ):
        op.create_index(index_name, "token_usage", columns)
    op.alter_column(
        "token_usage", "model_id", existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    # Fails closed if operators have not backfilled nullable Agent rows.
    op.alter_column(
        "token_usage", "model_id", existing_type=sa.Integer(), nullable=False
    )
    for index_name in (
        "idx_usage_apikey_time",
        "idx_usage_timestamp",
        "idx_usage_model_time",
        "idx_usage_department_time",
        "idx_usage_user_time",
    ):
        op.drop_index(index_name, table_name="token_usage")
    op.drop_column("token_usage", "request_duration_ms")
    op.drop_constraint(
        "fk_token_usage_department_id_departments",
        "token_usage",
        type_="foreignkey",
    )
    op.drop_column("token_usage", "department_id")

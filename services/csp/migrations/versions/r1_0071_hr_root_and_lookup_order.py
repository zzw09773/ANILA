"""根單位名稱，以及人資查詢開始時間。

根單位名稱用來把一級單位掛在同一個院根之下。
查詢開始時間用來丟掉比已套用結果更早開始的那一次。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0071"
down_revision: Union[str, None] = "r1_0070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROOT_NAME = "國家中山科學研究院"


def _columns(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    settings_columns = _columns(inspector, "hr_oracle_settings")
    if "hr_oracle_settings" in inspector.get_table_names() and "root_unit_name" not in settings_columns:
        op.add_column(
            "hr_oracle_settings",
            sa.Column(
                "root_unit_name",
                sa.String(length=100),
                nullable=False,
                server_default=_ROOT_NAME,
            ),
        )
    user_columns = _columns(inspector, "users")
    if "hr_lookup_started_at" not in user_columns:
        op.add_column(
            "users",
            sa.Column("hr_lookup_started_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hr_lookup_started_at" in _columns(inspector, "users"):
        op.drop_column("users", "hr_lookup_started_at")
    if "root_unit_name" in _columns(inspector, "hr_oracle_settings"):
        op.drop_column("hr_oracle_settings", "root_unit_name")

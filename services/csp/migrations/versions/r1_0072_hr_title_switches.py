"""主管職稱的兩個開關。

人資有職稱就授與。清單留空表示任何職稱都算。
升級時兩個開關打開，兩份職稱清單清成空。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0072"
down_revision: Union[str, None] = "r1_0071"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _empty_json_default():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'[]'::jsonb")
    return sa.text("'[]'")


def _clear_title_lists() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE hr_oracle_settings "
            "SET unit_admin_titles = '[]'::jsonb, declass_titles = '[]'::jsonb"
        )
        return
    op.execute(
        "UPDATE hr_oracle_settings SET unit_admin_titles = '[]', declass_titles = '[]'"
    )


def _set_empty_title_defaults() -> None:
    bind = op.get_bind()
    empty = _empty_json_default()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("hr_oracle_settings") as batch:
            batch.alter_column("unit_admin_titles", server_default=empty)
            batch.alter_column("declass_titles", server_default=empty)
        return
    op.alter_column("hr_oracle_settings", "unit_admin_titles", server_default=empty)
    op.alter_column("hr_oracle_settings", "declass_titles", server_default=empty)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hr_oracle_settings" not in inspector.get_table_names():
        return
    columns = _columns(inspector, "hr_oracle_settings")
    if "auto_unit_admin" not in columns:
        op.add_column(
            "hr_oracle_settings",
            sa.Column(
                "auto_unit_admin",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    if "auto_declass" not in columns:
        op.add_column(
            "hr_oracle_settings",
            sa.Column(
                "auto_declass",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    _clear_title_lists()
    _set_empty_title_defaults()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = _columns(inspector, "hr_oracle_settings")
    if "auto_declass" in columns:
        op.drop_column("hr_oracle_settings", "auto_declass")
    if "auto_unit_admin" in columns:
        op.drop_column("hr_oracle_settings", "auto_unit_admin")

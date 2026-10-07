"""人資資料庫設定，以及人資授與的單位管理員／降密審批。

既有指派的 source 補成 manual。人資登入寫入的列才是 hr。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0070"
down_revision: Union[str, None] = "r1_0069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EMPTY_JSON = "[]"


def _json_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()
    return sa.JSON()


def _empty_json_default():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text(f"'{_EMPTY_JSON}'::jsonb")
    return sa.text(f"'{_EMPTY_JSON}'")


def _columns(inspector, table: str) -> set[str]:
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = _columns(inspector, "users")
    if "display_name" not in user_columns:
        op.add_column("users", sa.Column("display_name", sa.String(length=100), nullable=True))
    if "department_source" not in user_columns:
        op.add_column(
            "users",
            sa.Column("department_source", sa.String(length=16), nullable=True),
        )
    if "hr_titles" not in user_columns:
        op.add_column("users", sa.Column("hr_titles", _json_type(), nullable=True))

    for table in ("unit_admin_assignments", "classification_authority_assignments"):
        if "source" not in _columns(inspector, table):
            op.add_column(
                table,
                sa.Column(
                    "source",
                    sa.String(length=16),
                    nullable=False,
                    server_default="manual",
                ),
            )

    inspector = sa.inspect(bind)
    if "hr_oracle_settings" not in inspector.get_table_names():
        op.create_table(
            "hr_oracle_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("host", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("port", sa.Integer(), nullable=False, server_default="1521"),
            sa.Column("service_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("db_user", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("password_envelope", sa.Text(), nullable=True),
            sa.Column("table_name", sa.String(length=256), nullable=False, server_default=""),
            sa.Column(
                "unit_admin_titles",
                _json_type(),
                nullable=False,
                server_default=_empty_json_default(),
            ),
            sa.Column(
                "declass_titles",
                _json_type(),
                nullable=False,
                server_default=_empty_json_default(),
            ),
            sa.Column(
                "health_status",
                sa.String(length=20),
                nullable=False,
                server_default="unknown",
            ),
            sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("health_detail", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["updated_by_user_id"],
                ["users.id"],
                name="fk_hr_oracle_settings_updated_by_user",
                ondelete="SET NULL",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    blocked: list[str] = []
    if "hr_oracle_settings" in tables:
        saved = bind.execute(sa.text("SELECT COUNT(*) FROM hr_oracle_settings")).scalar() or 0
        if saved:
            blocked.append("hr_oracle_settings")
    for table in ("unit_admin_assignments", "classification_authority_assignments"):
        if table in tables and "source" in _columns(inspector, table):
            granted = bind.execute(
                sa.text(f"SELECT COUNT(*) FROM {table} WHERE source = 'hr'")
            ).scalar() or 0
            if granted:
                blocked.append(table)
    if blocked:
        raise RuntimeError(
            "r1_0070 不能降版：還有人資授與的權限或已儲存的人資設定"
            f"（{'、'.join(blocked)}）。"
            "先收回 source 為 hr 的列，並刪掉人資設定之後才能降。"
            "降版會拿掉 source，再升級時這些列會變成手動授與，之後不會再被收回。"
        )
    if "hr_oracle_settings" in tables:
        op.drop_table("hr_oracle_settings")
    for table, column in (
        ("classification_authority_assignments", "source"),
        ("unit_admin_assignments", "source"),
        ("users", "hr_titles"),
        ("users", "department_source"),
        ("users", "display_name"),
    ):
        if column in _columns(inspector, table):
            op.drop_column(table, column)

# -*- coding: utf-8 -*-
"""使用者自訂文字型 skill。

一列一個版本。已發布的列不原地改寫；新草稿是另一列。

Revision ID: r1_0066
Revises: r1_0065
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0066"
down_revision: Union[str, None] = "r1_0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lineage_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column(
            "auto_apply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_user_skills_owner_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name="fk_user_skills_department_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name="fk_user_skills_reviewed_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "lineage_id", "version", name="uq_user_skills_lineage_version"
        ),
    )
    op.create_index("ix_user_skills_lineage_id", "user_skills", ["lineage_id"])
    op.create_index("ix_user_skills_owner", "user_skills", ["owner_user_id"])
    op.create_index("ix_user_skills_department", "user_skills", ["department_id"])
    op.create_index(
        "ix_user_skills_status_scope", "user_skills", ["status", "scope"]
    )

    # 名稱佔用跟版本列分開：同一條的草稿與已發布同名只留一列。
    # NULL 在 UNIQUE 裡不相等，所以三個層級各自用部分索引。
    op.create_table(
        "user_skill_name_claims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lineage_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("name_key", sa.String(length=40), nullable=False),
    )
    op.create_index(
        "ix_user_skill_name_claims_lineage",
        "user_skill_name_claims",
        ["lineage_id"],
    )
    op.create_index(
        "uq_skill_name_personal",
        "user_skill_name_claims",
        ["owner_user_id", "name_key"],
        unique=True,
        postgresql_where=sa.text("scope = 'personal'"),
        sqlite_where=sa.text("scope = 'personal'"),
    )
    op.create_index(
        "uq_skill_name_unit",
        "user_skill_name_claims",
        ["department_id", "name_key"],
        unique=True,
        postgresql_where=sa.text("scope = 'unit'"),
        sqlite_where=sa.text("scope = 'unit'"),
    )
    op.create_index(
        "uq_skill_name_campus",
        "user_skill_name_claims",
        ["name_key"],
        unique=True,
        postgresql_where=sa.text("scope = 'campus'"),
        sqlite_where=sa.text("scope = 'campus'"),
    )


def downgrade() -> None:
    op.drop_index("uq_skill_name_campus", table_name="user_skill_name_claims")
    op.drop_index("uq_skill_name_unit", table_name="user_skill_name_claims")
    op.drop_index("uq_skill_name_personal", table_name="user_skill_name_claims")
    op.drop_index("ix_user_skill_name_claims_lineage", table_name="user_skill_name_claims")
    op.drop_table("user_skill_name_claims")
    op.drop_index("ix_user_skills_status_scope", table_name="user_skills")
    op.drop_index("ix_user_skills_department", table_name="user_skills")
    op.drop_index("ix_user_skills_owner", table_name="user_skills")
    op.drop_index("ix_user_skills_lineage_id", table_name="user_skills")
    op.drop_table("user_skills")

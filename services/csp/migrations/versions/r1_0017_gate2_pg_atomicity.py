# -*- coding: utf-8 -*-
"""Gate 2 PostgreSQL concurrency and atomicity invariants.

Revision ID: r1_0017
Revises: r1_0016
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r1_0017"
down_revision: Union[str, None] = "r1_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEVELS_SQL = "'無機密', '營業秘密', '機密', '極機密', '絕對機密'"


def upgrade() -> None:
    # Gate 2 auth/session code reads these fields on every runtime User load,
    # but the prior revision created no columns on a fresh Alembic database.
    # Additive backfill keeps existing users valid and prevents a migrated
    # fresh host from failing before any concurrency control can execute.
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # Fresh Alembic had additional long-standing ORM/schema gaps on rows that
    # Gate 2 locks and audits.  Without these additive columns, a non-superuser
    # runtime User load (eager Department) or atomic AuditLog insert fails.
    op.add_column(
        "departments",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column("context_window", sa.Integer(), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("base_model_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_model_registry_base_model_id",
        "model_registry",
        "model_registry",
        ["base_model_id"],
        ["id"],
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("key_suffix", sa.String(length=4), nullable=True),
    )
    op.execute(
        "UPDATE api_keys SET key_suffix = '????' WHERE key_suffix IS NULL"
    )
    op.alter_column("api_keys", "key_suffix", nullable=False)
    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_logs_actor_user_id",
        "audit_logs",
        "users",
        ["actor_user_id"],
        ["id"],
    )
    op.add_column(
        "audit_logs",
        sa.Column("actor_username", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'success'"),
        ),
    )
    op.add_column(
        "audit_logs",
        sa.Column("ip_address", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_logs", sa.Column("metadata_json", sa.Text(), nullable=True)
    )
    for column in (
        "actor_user_id",
        "actor_username",
        "action",
        "resource_type",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_audit_logs_{column}", "audit_logs", [column]
        )

    # r1_0016 used a non-canonical constraint name.  Reconciliation checks
    # names deliberately so a superficially similar/mutated constraint cannot
    # make the governance inventory green.
    for table_name in ("user_facts", "conversation_memory_chunks"):
        op.drop_constraint(
            f"ck_{table_name}_classification_level",
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            f"ck_{table_name}_classification_level_gate2_level",
            table_name,
            f"classification_level IN ({_LEVELS_SQL})",
        )
    op.create_check_constraint(
        "ck_users_token_version_nonnegative",
        "users",
        "token_version >= 0",
    )
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Never hide a pre-existing split-brain attempt by choosing an arbitrary
    # winner.  Migration must stop for explicit incident reconciliation.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT task_id
                  FROM task_runs
                 WHERE status IN ('queued', 'running')
                 GROUP BY task_id
                HAVING count(*) > 1
              ) THEN
                RAISE EXCEPTION
                  'multiple active task_runs exist; reconcile before r1_0017';
              END IF;
            END;
            $$
            """
        )
    )
    op.create_index(
        "uq_task_runs_one_active_per_task",
        "task_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_task_runs_one_active_per_task", table_name="task_runs"
    )
    for table_name in ("user_facts", "conversation_memory_chunks"):
        op.drop_constraint(
            f"ck_{table_name}_classification_level_gate2_level",
            table_name,
            type_="check",
        )
        op.create_check_constraint(
            f"ck_{table_name}_classification_level",
            table_name,
            f"classification_level IN ({_LEVELS_SQL})",
        )
    for column in reversed((
        "actor_user_id",
        "actor_username",
        "action",
        "resource_type",
        "status",
        "created_at",
    )):
        op.drop_index(f"ix_audit_logs_{column}", table_name="audit_logs")
    op.drop_column("audit_logs", "metadata_json")
    op.drop_column("audit_logs", "ip_address")
    op.drop_column("audit_logs", "status")
    op.drop_column("audit_logs", "actor_username")
    op.drop_constraint(
        "fk_audit_logs_actor_user_id", "audit_logs", type_="foreignkey"
    )
    op.drop_column("audit_logs", "actor_user_id")
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "key_suffix")
    op.drop_column("model_registry", "updated_at")
    op.drop_constraint(
        "fk_model_registry_base_model_id",
        "model_registry",
        type_="foreignkey",
    )
    op.drop_column("model_registry", "base_model_id")
    op.drop_column("model_registry", "context_window")
    op.drop_column("departments", "updated_at")
    op.drop_column("users", "updated_at")
    op.drop_constraint(
        "ck_users_token_version_nonnegative", "users", type_="check"
    )
    op.drop_column("users", "token_version")

# -*- coding: utf-8 -*-
"""Slice 3a — 四級分類 schema 升級 + 治理三表(SYSTEM-MAP §8)。

新表:
- ``classification_events``(欄位逐字;reason 7 值封閉 enum 在
  契約層,DB 存開放 String —— 同 policy_decisions 模式)
- ``declassification_requests``(status 5 值,
  fail-closed 預設 pending_supervisor;變體 A approved_via 二選一 +
  紙本代錄三欄)
- ``classification_authority_assignments``(「機密審批權責」指派;
  必附核定依據公文文號/簽呈;supervisor_approval 構件 = 本表 +
  declassification_requests 上的 supervisor 欄位)

共通四欄位(classification_level / classification_latched_at /
classification_source / classification_event_id)掛載對象 —— 本 slice
落在**現存**的表:

| 資源              | 現制表                  | 本檔動作                     |
|-------------------|-------------------------|------------------------------|
| Task              | tasks                   | 補 3 欄(level 已在 r1_0001)|
| Conversation      | conversations           | 加 4 欄 + backfill           |
| Message           | messages                | 加 4 欄                      |
| SourceSnapshot    | source_snapshots        | 補 3 欄(level 已在 r1_0001)|
| Collection        | ingestion_collections   | 加 4 欄(floor=無機密)      |
| Document          | ingestion_documents     | 加 4 欄(floor=無機密)      |
| Chunk             | document_chunks         | 加 4 欄(PG-only 表,0014 建;|
|                   |                         | ORM 不建模,寫入走 worker SDK,|
|                   |                         | chunk 級 latch 接線於後續 slice)|
| AgentRun          | task_runs               | 補 3 欄(現制對應表;獨立    |
|                   |                         | agent_runs 表未存在)        |
| Artifact          | artifacts               | 4 欄已於 r1_0007 建(Slice 8a)|
| ServiceLaunch     | ——(Slice 7 才建表)   | 深後補(deferred)           |
| ExportRecord      | export_records          | 4 欄已於 r1_0007 建(Slice 8a)|

四級字彙(SYSTEM-MAP §8):無機密 / 營業秘密 / 密 / 機密。

Backfill(floor=最低安全起點;legacy classified → 最高級 機密):
- ``conversations.classified=false/null → 無機密``(server_default 即是)
- ``conversations.classified=true → 機密``;``classification_inherited=true``
  另補一筆 ``memory_inherited`` ClassificationEvent
- ``agents.requires_encryption=true → default_classification_level=密``
- collections / documents 全部 floor=無機密(server_default)
- **舊 boolean 欄位原封不動**(downgrade 只卸新欄新表,boolean 側零資料損失)

Revision ID: r1_0003
Revises: r1_0002
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "r1_0003"
down_revision: Union[str, None] = "r1_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

json_type = sa.JSON().with_variant(JSONB, "postgresql")

# SYSTEM-MAP §8 四級字彙;backfill 只用這兩個端點值。
_UNCLASSIFIED = "無機密"
_RESTRICTED = "密"
_SECRET = "機密"  # legacy classified=true → 最高級(保守)

# 加「4 欄全套」的現存表(Chunk 的 document_chunks 是 PG-only、0014 以
# raw SQL 建立,一樣用 add_column —— migration 實務上只跑 PG)。
_FULL_COLUMN_TABLES = (
    "conversations",
    "messages",
    "ingestion_collections",
    "ingestion_documents",
    "document_chunks",
)
# r1_0001 已有 classification_level,只補其餘 3 欄的表。
_PARTIAL_COLUMN_TABLES = ("tasks", "task_runs", "source_snapshots")

_COMMON_TAIL_COLUMNS = (
    "classification_latched_at",
    "classification_source",
    "classification_event_id",
)


def _created_at_column() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _add_common_tail_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column("classification_latched_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        table,
        sa.Column("classification_source", sa.String(length=50), nullable=True),
    )
    op.add_column(
        table,
        sa.Column(
            "classification_event_id",
            sa.Integer(),
            sa.ForeignKey(
                "classification_events.id",
                name=f"fk_{table}_classification_event_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )


def upgrade() -> None:
    # ── 1. classification_events(doc 08 §6;append-only)────────────────
    op.create_table(
        "classification_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("previous_level", sa.String(length=20), nullable=False),
        sa.Column("new_level", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "inherited_from_resource_type", sa.String(length=50), nullable=True
        ),
        sa.Column(
            "inherited_from_resource_id", sa.String(length=100), nullable=True
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        _created_at_column(),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_classification_events_resource",
        "classification_events",
        ["resource_type", "resource_id"],
    )

    # ── 2. declassification_requests(doc 08 §8)──────────────────────────
    op.create_table(
        "declassification_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("from_level", sa.String(length=20), nullable=False),
        sa.Column("to_level", sa.String(length=20), nullable=False),
        sa.Column("requested_by_admin_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_redaction_summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending_supervisor",  # fail-closed 預設
        ),
        sa.Column("supervisor_user_id", sa.Integer(), nullable=True),
        sa.Column("supervisor_comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("approved_via", sa.String(length=32), nullable=True),
        sa.Column("authority_reference", sa.String(length=255), nullable=True),
        sa.Column(
            "authority_title_name", sa.String(length=255), nullable=True
        ),
        sa.Column("recorded_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "resulting_resource_id", sa.String(length=100), nullable=True
        ),
        sa.Column(
            "audit_event_ids",
            json_type,
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        _created_at_column(),
        # 申請人 FK 不設 ondelete:降級治理紀錄不得因刪帳號連帶蒸發。
        sa.ForeignKeyConstraint(["requested_by_admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["supervisor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_declassification_requests_status",
        "declassification_requests",
        ["status"],
    )
    op.create_index(
        "ix_declassification_requests_resource",
        "declassification_requests",
        ["resource_type", "resource_id"],
    )

    # ── 3. classification_authority_assignments(doc 08 §7/§12)──────────
    op.create_table(
        "classification_authority_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("authority_reference", sa.String(length=255), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("confirmed_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        _created_at_column(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_classification_authority_assignments_user",
        "classification_authority_assignments",
        ["user_id"],
    )

    # ── 4. 共通欄位掛載(見模組 docstring 的 11 資源對照表)──────────────
    for table in _FULL_COLUMN_TABLES:
        op.add_column(
            table,
            sa.Column(
                "classification_level",
                sa.String(length=20),
                nullable=False,
                server_default=_UNCLASSIFIED,
            ),
        )
        _add_common_tail_columns(table)
    for table in _PARTIAL_COLUMN_TABLES:
        _add_common_tail_columns(table)

    # agents.default_classification_level(doc 08 §3 bridge 第 3 列)。
    op.add_column(
        "agents",
        sa.Column(
            "default_classification_level",
            sa.String(length=20),
            nullable=False,
            server_default=_UNCLASSIFIED,
        ),
    )

    # ── 5. Backfill(floor;舊 boolean 欄位原封不動)──────────────────────
    # classified=true → 機密(SECRET;false/null → 無機密由 server_default 落定)。
    op.execute(
        sa.text(
            "UPDATE conversations SET "
            f"classification_level = '{_SECRET}', "
            "classification_source = 'legacy_backfill', "
            "classification_latched_at = COALESCE(classified_at, CURRENT_TIMESTAMP) "
            "WHERE classified"
        )
    )
    # classification_inherited=true → inherited_from event
    # (來源 chunk 已不可考,inherited_from_* 留 NULL,provenance 由 reason 承載)。
    op.execute(
        sa.text(
            "INSERT INTO classification_events "
            "(resource_type, resource_id, previous_level, new_level, reason, created_at) "
            f"SELECT 'conversation', CAST(id AS VARCHAR), '{_UNCLASSIFIED}', "
            f"'{_SECRET}', 'memory_inherited', "
            "COALESCE(classified_at, CURRENT_TIMESTAMP) "
            "FROM conversations WHERE classified AND classification_inherited"
        )
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET classification_event_id = ("
            " SELECT ce.id FROM classification_events ce"
            " WHERE ce.resource_type = 'conversation'"
            " AND ce.resource_id = CAST(conversations.id AS VARCHAR)"
            " ORDER BY ce.id DESC LIMIT 1"
            ") WHERE classified AND classification_inherited"
        )
    )
    # requires_encryption=true → default_classification_level=密(floor;
    # 對齊 runtime `_agent_policy_level` 的 RESTRICTED)。
    op.execute(
        sa.text(
            f"UPDATE agents SET "
            f"default_classification_level = '{_RESTRICTED}' "
            "WHERE requires_encryption"
        )
    )


def downgrade() -> None:
    # 先卸掛在既有表上的新欄位(含指向 classification_events 的 FK 欄),
    # 再卸三張新表;舊 boolean 欄位從未被改動,零資料損失。
    op.drop_column("agents", "default_classification_level")
    for table in _PARTIAL_COLUMN_TABLES:
        for column in reversed(_COMMON_TAIL_COLUMNS):
            op.drop_column(table, column)
    for table in _FULL_COLUMN_TABLES:
        for column in reversed(_COMMON_TAIL_COLUMNS):
            op.drop_column(table, column)
        op.drop_column(table, "classification_level")
    op.drop_index(
        "ix_classification_authority_assignments_user",
        table_name="classification_authority_assignments",
    )
    op.drop_table("classification_authority_assignments")
    op.drop_index(
        "ix_declassification_requests_resource",
        table_name="declassification_requests",
    )
    op.drop_index(
        "ix_declassification_requests_status",
        table_name="declassification_requests",
    )
    op.drop_table("declassification_requests")
    op.drop_index(
        "ix_classification_events_resource",
        table_name="classification_events",
    )
    op.drop_table("classification_events")

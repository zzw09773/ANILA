# -*- coding: utf-8 -*-
"""Slice 5a — Agent Registry 升級(doc 05 §3/§4/§6/§12、doc 10 Slice 5)。

在既有 ``agents`` 表補齊 doc 05 §3 AgentDefinition schema 尚未存在的欄位,並把
``approval_status`` 由現況三值(pending/approved/rejected)擴為七值狀態機
(draft / pending_connection_test / pending_trace_test / pending_security_review
/ approved / rejected / disabled),以「連得上 → trace 過 → 安全審查」三關把守。

新增欄位(doc 05 §3「尚未存在」清單逐字 + §4 manifest/§6 Full Trace 落章):

| 欄位                    | 型別          | 說明                                            |
|-------------------------|---------------|-------------------------------------------------|
| owner_department_id     | int FK        | doc 05 §3 owner_department_id?(SET NULL)       |
| runtime_type            | str NOT NULL  | 5 值;現況 backfill = openai_compatible_agent    |
| agent_version           | str NULL      | manifest.version(§13 名 agent_version)         |
| audit_level             | str NOT NULL  | v1 policy:approved 必為 full_trace(backfill)   |
| classification_ceiling  | str NULL      | 分類上限(NULL = 無上限)                        |
| manifest_url            | str NULL      | GET /.well-known/anila-agent.json 來源           |
| healthcheck_url         | str NULL      | GET /health                                     |
| supported_task_types    | json NULL     | doc 05 §3 string[]                              |
| output_schema           | json NULL     | doc 05 §3                                        |
| allowed_tool_ids        | json NULL     | doc 05 §3 string[]                              |
| manifest_json           | json NULL     | 驗證後留存的 manifest 快照(§4)                 |
| trace_callback_mode     | str NULL      | manifest trace.callback_mode(§4)               |
| trace_test_passed_at    | datetime NULL | Full Trace 落章時間(§6 approval blocker)       |
| trace_test_report       | json NULL     | trace-test 逐項報告                             |

``approval_status`` backfill(doc 05 §3;pending 為唯一需搬遷值):
- ``pending  → pending_connection_test``(第一關 = 連線測試)
- ``approved → approved``、``rejected → rejected``(原值不動)

欄位需先由 VARCHAR(20) 加寬為 VARCHAR(30) —— 新值 pending_connection_test /
pending_security_review 皆 23 字,原長度容不下。

downgrade:先把七值收斂回三值(未核准者 → pending、disabled → rejected,
approved/rejected 無損),再把欄位縮回 VARCHAR(20),最後卸新欄。對
pending/approved/rejected 三者無損還原(§3「三值語義」)。

Revision ID: r1_0004
Revises: r1_0003
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "r1_0004"
down_revision: Union[str, None] = "r1_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

json_type = sa.JSON().with_variant(JSONB, "postgresql")

_DEFAULT_RUNTIME_TYPE = "openai_compatible_agent"
_DEFAULT_AUDIT_LEVEL = "full_trace"


def upgrade() -> None:
    # ── 1. 加寬 approval_status(七值最長 23 字 > 原 VARCHAR(20))────────────
    op.alter_column(
        "agents",
        "approval_status",
        type_=sa.String(length=30),
        existing_type=sa.String(length=20),
        existing_nullable=False,
    )

    # ── 2. 新增 doc 05 §3/§4/§6 欄位 ─────────────────────────────────────────
    op.add_column(
        "agents",
        sa.Column(
            "owner_department_id",
            sa.Integer(),
            sa.ForeignKey(
                "departments.id",
                name="fk_agents_owner_department_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "runtime_type",
            sa.String(length=40),
            nullable=False,
            server_default=_DEFAULT_RUNTIME_TYPE,
        ),
    )
    op.add_column("agents", sa.Column("agent_version", sa.String(length=40), nullable=True))
    op.add_column(
        "agents",
        sa.Column(
            "audit_level",
            sa.String(length=20),
            nullable=False,
            server_default=_DEFAULT_AUDIT_LEVEL,
        ),
    )
    op.add_column(
        "agents", sa.Column("classification_ceiling", sa.String(length=20), nullable=True)
    )
    op.add_column("agents", sa.Column("manifest_url", sa.String(length=500), nullable=True))
    op.add_column("agents", sa.Column("healthcheck_url", sa.String(length=500), nullable=True))
    op.add_column("agents", sa.Column("supported_task_types", json_type, nullable=True))
    op.add_column("agents", sa.Column("output_schema", json_type, nullable=True))
    op.add_column("agents", sa.Column("allowed_tool_ids", json_type, nullable=True))
    op.add_column("agents", sa.Column("manifest_json", json_type, nullable=True))
    op.add_column(
        "agents", sa.Column("trace_callback_mode", sa.String(length=20), nullable=True)
    )
    op.add_column("agents", sa.Column("trace_test_passed_at", sa.DateTime(), nullable=True))
    op.add_column("agents", sa.Column("trace_test_report", json_type, nullable=True))

    # ── 3. approval_status backfill(pending → pending_connection_test)────────
    op.execute(
        sa.text(
            "UPDATE agents SET approval_status = 'pending_connection_test' "
            "WHERE approval_status = 'pending'"
        )
    )
    op.alter_column(
        "agents",
        "approval_status",
        server_default="pending_connection_test",
        existing_type=sa.String(length=30),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 先把七值收斂回三值(在縮回 VARCHAR(20) 之前,確保值都塞得下)。
    op.execute(
        sa.text(
            "UPDATE agents SET approval_status = CASE "
            "WHEN approval_status = 'approved' THEN 'approved' "
            "WHEN approval_status = 'rejected' THEN 'rejected' "
            "WHEN approval_status = 'disabled' THEN 'rejected' "
            "ELSE 'pending' END"
        )
    )
    op.drop_column("agents", "trace_test_report")
    op.drop_column("agents", "trace_test_passed_at")
    op.drop_column("agents", "trace_callback_mode")
    op.drop_column("agents", "manifest_json")
    op.drop_column("agents", "allowed_tool_ids")
    op.drop_column("agents", "output_schema")
    op.drop_column("agents", "supported_task_types")
    op.drop_column("agents", "healthcheck_url")
    op.drop_column("agents", "manifest_url")
    op.drop_column("agents", "classification_ceiling")
    op.drop_column("agents", "audit_level")
    op.drop_column("agents", "agent_version")
    op.drop_column("agents", "runtime_type")
    op.drop_constraint(
        "fk_agents_owner_department_id", "agents", type_="foreignkey"
    )
    op.drop_column("agents", "owner_department_id")
    # 縮回 VARCHAR(20),清掉七值 server_default(還原現況:無 server_default)。
    op.alter_column(
        "agents",
        "approval_status",
        type_=sa.String(length=20),
        existing_type=sa.String(length=30),
        existing_nullable=False,
        server_default=None,
    )

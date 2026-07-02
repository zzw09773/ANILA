# -*- coding: utf-8 -*-
"""Slice 6a — Model Gateway Hardening(doc 04 §2/§8/§9/§11、doc 10 Slice 6)。

把既有 ``model_registry`` 的語意 formalize 成 doc 04 §2 的 ``ModelEndpoint``
目標 schema,並收斂 health 字彙為 doc 04 §9 / doc 01 拍板的五態。

新增欄位(doc 04 §2 目標 schema 逐字,順序照文件):

| 欄位                    | 型別          | 說明                                                 |
|-------------------------|---------------|------------------------------------------------------|
| protocol                | str NOT NULL  | doc §2 ``"openai_compatible" | "custom_adapter"``；   |
|                         |               | backfill = ``openai_compatible``(現況全是此形態)。  |
| api_key_secret_ref      | text NULL     | doc §3 per-model secret ref。存 ``enc::v1::`` AES-GCM |
|                         |               | envelope(與 csk-/ingestion 憑證同一套 crypto);      |
|                         |               | NULL = 退回全域 ``MODEL_GATEWAY_API_KEY``(MVP)。     |
| classification_ceiling  | str NULL      | doc §5 分類上限(NULL = 不設限)。                    |
| owner_department_id     | int FK NULL   | doc §2 ``owner_department_id?``(departments SET NULL)|
| supports_streaming      | bool NOT NULL | doc §2；backfill = true(LLM 常態串流)。             |
| supports_json_schema    | bool NOT NULL | doc §2；backfill = false(保守起點,admin 逐一開)。  |
| supports_tools          | bool NOT NULL | doc §2；backfill = false(同上)。                    |

**allowed_task_types 不加**:doc 04 §11 缺欄清單列了 ``allowed_task_types``,
但 §2 目標 schema 未列(文件內部不一致)。依「§2 目標 schema 為準」拍板
**不新增** ``allowed_task_types``;此裁決由 orchestrator 落 ADR。

health_status 字彙遷移(doc 04 §9 / doc 01 §32 拍板,保留欄位、就地改值):

    online     → healthy
    connecting → degraded
    offline    → unhealthy
    NULL / ''  → unknown

``disabled`` 為新增第五態(承接 ``is_active=false`` / approval disabled),
由讀取端(API / health loop)在呈現時填,不需 data migration 產生。欄位
server_default 由 ``offline`` 改為 ``unknown``(doc 01 驗收 5:初始未檢查 →
unknown)。欄寬 VARCHAR(20) 已能容納最長的 ``unhealthy``(9)/``degraded``(8),
不需加寬。

downgrade:health_status 反向映回三值(healthy→online、degraded→connecting、
unhealthy→offline、unknown→offline),server_default 還原 ``offline``,再卸新欄。
per-model secret ref / ceiling / supports_* 為新增資訊,downgrade 直接丟棄。

Revision ID: r1_0005
Revises: r1_0004
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0005"
down_revision: Union[str, None] = "r1_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PROTOCOL = "openai_compatible"

# doc 04 §9 / doc 01 §32 拍板映射(升級方向)。
_HEALTH_UPGRADE_SQL = (
    "UPDATE model_registry SET health_status = CASE "
    "WHEN health_status = 'online' THEN 'healthy' "
    "WHEN health_status = 'connecting' THEN 'degraded' "
    "WHEN health_status = 'offline' THEN 'unhealthy' "
    "WHEN health_status IS NULL OR health_status = '' THEN 'unknown' "
    "ELSE health_status END"
)
# downgrade 反向映射(disabled 無舊值對應 → 落 offline 最保守)。
_HEALTH_DOWNGRADE_SQL = (
    "UPDATE model_registry SET health_status = CASE "
    "WHEN health_status = 'healthy' THEN 'online' "
    "WHEN health_status = 'degraded' THEN 'connecting' "
    "WHEN health_status = 'unhealthy' THEN 'offline' "
    "ELSE 'offline' END"
)


def upgrade() -> None:
    # ── 0. 冪等補齊 health 欄(修 Alembic 鏈缺漏) ────────────────────────────────
    # health_status / health_checked_at 一直由 ORM(models/model_registry.py)宣告,
    # 但從無任何 migration 建立它們 —— 走過 startup ``Base.metadata.create_all``
    # fallback 的既有部署 DB 有這兩欄,乾淨 Alembic-only DB 卻沒有(doc 10 §17.3
    # 警告的 schema 漂移)。本 migration 的 health 值映射 UPDATE 需要它們,故先用
    # inspector 冪等補建:既有 DB 跳過、乾淨 DB 建欄。downgrade 不 DROP(見下)。
    _insp = sa.inspect(op.get_bind())
    _mr_cols = {c["name"] for c in _insp.get_columns("model_registry")}
    if "health_status" not in _mr_cols:
        op.add_column(
            "model_registry",
            sa.Column(
                "health_status",
                sa.String(length=20),
                nullable=False,
                server_default="unknown",
            ),
        )
    if "health_checked_at" not in _mr_cols:
        op.add_column(
            "model_registry",
            sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        )

    # ── 1. doc 04 §2 目標 schema 新欄 ──────────────────────────────────────────
    op.add_column(
        "model_registry",
        sa.Column(
            "protocol",
            sa.String(length=30),
            nullable=False,
            server_default=_DEFAULT_PROTOCOL,
        ),
    )
    op.add_column(
        "model_registry", sa.Column("api_key_secret_ref", sa.Text(), nullable=True)
    )
    op.add_column(
        "model_registry",
        sa.Column("classification_ceiling", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "owner_department_id",
            sa.Integer(),
            sa.ForeignKey(
                "departments.id",
                name="fk_model_registry_owner_department_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "supports_streaming",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "supports_json_schema",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "model_registry",
        sa.Column(
            "supports_tools",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ── 2. health_status 三值 → 五態(保留欄位、就地改值)───────────────────────
    op.execute(sa.text(_HEALTH_UPGRADE_SQL))
    op.alter_column(
        "model_registry",
        "health_status",
        server_default="unknown",
        existing_type=sa.String(length=20),
        existing_nullable=True,
    )


def downgrade() -> None:
    # 先把五態映回三值,再還原 server_default,最後卸新欄。
    op.execute(sa.text(_HEALTH_DOWNGRADE_SQL))
    op.alter_column(
        "model_registry",
        "health_status",
        server_default="offline",
        existing_type=sa.String(length=20),
        existing_nullable=True,
    )
    op.drop_column("model_registry", "supports_tools")
    op.drop_column("model_registry", "supports_json_schema")
    op.drop_column("model_registry", "supports_streaming")
    op.drop_constraint(
        "fk_model_registry_owner_department_id",
        "model_registry",
        type_="foreignkey",
    )
    op.drop_column("model_registry", "owner_department_id")
    op.drop_column("model_registry", "classification_ceiling")
    op.drop_column("model_registry", "api_key_secret_ref")
    op.drop_column("model_registry", "protocol")

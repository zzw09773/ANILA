"""ISO/IEC 42001:2023 traceability columns.

Phase 1 of the AI Management System (AIMS) rollout — see
`docs/governance/iso-42001-compliance.md` §4. Two tables get
non-functional metadata columns so audit / V&V trails exist:

`agents` (A.5.2 / A.5.4 / A.6.2.4 / A.6.2.5):
  * ``source_commit_sha`` — git SHA the deployed agent binary was
    built from. Pairs with image tag traceability (CI work,
    pending).
  * ``last_reviewer_id`` — user id of the last reviewer (PR / MR
    approver). Required for A.6.2.4 V&V audit trail.
  * ``vv_status`` — verification & validation outcome
    (``pending`` / ``passed`` / ``failed`` / ``waived``). Gates
    production routing; ``pending`` agents may still be registered
    but router can refuse to dispatch.
  * ``aiia_doc_path`` — relative path to the agent's AI Impact
    Assessment in `docs/governance/aiia/<agent>.md`. Required for
    A.5.2 / A.5.4.

`model_registry` (A.6.2.7 / A.7.5 / A.10.3):
  * ``model_card_url`` — link to filled-in model card in
    `docs/governance/model-cards/<model>.md` or external URL.
  * ``training_dataset_ref`` — short reference to training data
    provenance (e.g. "Gemma upstream", "internal-fine-tune-2026-04").
  * ``weights_sha256`` — checksum of deployed weights, used to
    detect silent upstream changes (R-007).
  * ``intended_use`` — single-sentence intended use statement.
  * ``limitations`` — known limitations, free text.

All columns are nullable so existing rows are unaffected; the UI /
admin layer is responsible for prompting fill-in when missing. A
follow-up migration may flip ``vv_status`` to NOT NULL once all
production rows are populated.

No data backfill — historical rows are flagged for retro-AIIA in
governance Phase 2 (Q3 2026).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agents: AI Impact Assessment + V&V traceability ──────────────────
    op.add_column(
        "agents",
        sa.Column("source_commit_sha", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("last_reviewer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column(
            "vv_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "agents",
        sa.Column("aiia_doc_path", sa.String(length=500), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_last_reviewer_id",
        source_table="agents",
        referent_table="users",
        local_cols=["last_reviewer_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    # ── model_registry: technical documentation (model card) ─────────────
    op.add_column(
        "model_registry",
        sa.Column("model_card_url", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("training_dataset_ref", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("weights_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("intended_use", sa.Text(), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("limitations", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # ── model_registry ───────────────────────────────────────────────────
    op.drop_column("model_registry", "limitations")
    op.drop_column("model_registry", "intended_use")
    op.drop_column("model_registry", "weights_sha256")
    op.drop_column("model_registry", "training_dataset_ref")
    op.drop_column("model_registry", "model_card_url")

    # ── agents ───────────────────────────────────────────────────────────
    op.drop_constraint("fk_agents_last_reviewer_id", "agents", type_="foreignkey")
    op.drop_column("agents", "aiia_doc_path")
    op.drop_column("agents", "vv_status")
    op.drop_column("agents", "last_reviewer_id")
    op.drop_column("agents", "source_commit_sha")

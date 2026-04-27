"""Sprint 4 — collections become first-class; drop agent coupling.

The Sprint 1–3 architecture treated every collection as the property of
exactly one agent. Smoke-testing on real workflows showed this was
over-coupling: ANILA's posture is "platform = pgvector infrastructure",
agent backends just configure ``DB_URL + COLLECTION_ID`` and the
platform doesn't care which agent reads what. Collections want to be
first-class resources owned by users, not glued to agents.

This migration drops every ``agent_id`` reference in the ingestion
schema and re-keys the document_chunks RLS policy from
``anila.agent_id`` to ``anila.collection_id``. The engine-level
isolation (Sprint 1 Layer 2 + G2 gate) survives — just at a different
scope. Each agent backend opens its connection with
``SET LOCAL anila.collection_id = N`` and only sees that collection's
chunks; the policy enforces it even when the application code forgets.

agent_llm_credentials gets renamed to user_llm_credentials with the
FK rebased onto users (devs' judge LLM creds are user-scoped, not
agent-scoped — one OpenAI key serves all the dev's collections).

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 0. Clean up any orphan tables from prior failed runs ────────────────
    # CSP's lifespan fallback ``Base.metadata.create_all`` runs when an
    # alembic upgrade fails — it sees the new ``UserLlmCredential``
    # ORM class and creates ``user_llm_credentials`` separately.
    # That blocks the RENAME below. Drop it eagerly; it's empty anyway
    # because the rename is what should fill it.
    op.execute("DROP TABLE IF EXISTS user_llm_credentials CASCADE")

    # ── 1. ingestion_collections.created_by backfill + NOT NULL ─────────────
    # Existing rows have agent_id but possibly NULL created_by. Pull the
    # owning user from the agent.owner_user_id chain so nothing is
    # orphaned when we drop agent_id below.
    op.execute(
        """
        UPDATE ingestion_collections c
           SET created_by = a.owner_user_id
          FROM agents a
         WHERE c.agent_id = a.id
           AND c.created_by IS NULL
        """
    )
    # Some seeded collections may still be NULL (orphan agents); drop
    # them rather than fail the migration. Sprint 1–3 dev seeds are
    # disposable.
    op.execute("DELETE FROM ingestion_collections WHERE created_by IS NULL")
    op.execute(
        "ALTER TABLE ingestion_collections ALTER COLUMN created_by SET NOT NULL"
    )

    # ── 2. Drop agent-keyed RLS infra on document_chunks ────────────────────
    # Order matters: drop the policy first, then the constraints, then
    # the column. Dropping the policy doesn't fail if the column it
    # references later vanishes, but doing it first keeps psql logs
    # clean.
    op.execute("DROP POLICY IF EXISTS chunks_agent_isolation ON document_chunks")
    op.execute(
        "ALTER TABLE document_chunks "
        "DROP CONSTRAINT IF EXISTS chunks_agent_required"
    )
    op.execute("DROP INDEX IF EXISTS ix_chunks_agent_collection")
    op.execute("ALTER TABLE document_chunks DROP COLUMN agent_id")

    # ── 3. Recreate RLS keyed on collection_id ──────────────────────────────
    # Same defence-in-depth pattern as Sprint 1 § 3.3 Layer 2 — engine
    # filters rows even if the application code forgets to scope. Just
    # the GUC name changes.
    op.execute(
        "CREATE INDEX ix_chunks_collection_only ON document_chunks(collection_id)"
    )
    op.execute(
        """
        CREATE POLICY chunks_collection_isolation ON document_chunks
            FOR ALL
            USING (
                collection_id = NULLIF(
                    current_setting('anila.collection_id', true),
                    ''
                )::int
            )
        """
    )

    # ── 4. Drop agent_id from collections (and the dependent constraint) ────
    op.execute(
        "ALTER TABLE ingestion_collections "
        "DROP CONSTRAINT IF EXISTS uq_collections_agent_name"
    )
    op.execute("DROP INDEX IF EXISTS ix_collections_agent_id")
    op.execute("ALTER TABLE ingestion_collections DROP COLUMN agent_id")

    # No new uniqueness constraint on (created_by, name): different
    # users may legitimately want a collection named ``laws``. The UI
    # disambiguates by displaying the owner alongside.

    # ── 5. agent_llm_credentials → user_llm_credentials ─────────────────────
    # 0017 already shipped both ``agent_id`` (FK agents) and ``created_by``
    # (FK users, audit nullable). Sprint 4 promotes ``created_by`` to the
    # primary owner key and drops ``agent_id`` entirely. Renaming the
    # table to user_llm_credentials reflects the new ownership model.
    #
    # Order:
    # 1. Drop FK + UNIQUE on agent_id.
    # 2. Backfill created_by from agent.owner_user_id where it's NULL
    #    (existing rows from 0017 should have it set, but be defensive).
    # 3. Promote created_by to NOT NULL + replace its FK with ON DELETE
    #    CASCADE (was SET NULL — Sprint 4 ownership wants stricter).
    # 4. Drop agent_id column.
    # 5. Rename table + add the new (created_by, name) UNIQUE.
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "DROP CONSTRAINT IF EXISTS agent_llm_credentials_agent_id_fkey"
    )
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "DROP CONSTRAINT IF EXISTS uq_agent_llm_credentials_name"
    )
    op.execute(
        """
        UPDATE agent_llm_credentials c
           SET created_by = a.owner_user_id
          FROM agents a
         WHERE c.agent_id = a.id AND c.created_by IS NULL
        """
    )
    # Drop any rows still NULL (orphan agents); same dev-cleanup posture
    # as we used for ingestion_collections.created_by above.
    op.execute("DELETE FROM agent_llm_credentials WHERE created_by IS NULL")
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "ALTER COLUMN created_by SET NOT NULL"
    )
    # Replace 0017's ``ON DELETE SET NULL`` FK with CASCADE (ownership).
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "DROP CONSTRAINT IF EXISTS agent_llm_credentials_created_by_fkey"
    )
    op.execute("DROP INDEX IF EXISTS ix_agent_llm_credentials_agent_id")
    op.execute("ALTER TABLE agent_llm_credentials DROP COLUMN agent_id")
    op.execute("ALTER TABLE agent_llm_credentials RENAME TO user_llm_credentials")
    op.execute(
        "ALTER TABLE user_llm_credentials "
        "ADD CONSTRAINT user_llm_credentials_created_by_fkey "
        "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE user_llm_credentials "
        "ADD CONSTRAINT uq_user_llm_credentials_name "
        "UNIQUE (created_by, name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_llm_credentials_created_by "
        "ON user_llm_credentials(created_by)"
    )


def downgrade() -> None:
    """Reverse Sprint 4. Lossy when the active agents table can't
    map back to the original ownership graph (e.g. an agent has been
    deleted since), but a best-effort restore for ops rollback.
    """
    # Reverse user_llm_credentials → agent_llm_credentials
    op.execute(
        "ALTER INDEX IF EXISTS ix_user_llm_credentials_created_by "
        "RENAME TO ix_agent_llm_credentials_agent_id"
    )
    op.execute(
        "ALTER TABLE user_llm_credentials "
        "DROP CONSTRAINT IF EXISTS uq_user_llm_credentials_name"
    )
    op.execute(
        "ALTER TABLE user_llm_credentials "
        "DROP CONSTRAINT IF EXISTS user_llm_credentials_created_by_fkey"
    )
    op.execute("ALTER TABLE user_llm_credentials RENAME TO agent_llm_credentials")
    op.execute(
        "ALTER TABLE agent_llm_credentials RENAME COLUMN created_by TO agent_id"
    )
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "ADD CONSTRAINT agent_llm_credentials_agent_id_fkey "
        "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE agent_llm_credentials "
        "ADD CONSTRAINT uq_agent_llm_credentials_name UNIQUE (agent_id, name)"
    )

    # Reverse RLS — re-add agent_id columns + agent-keyed policy.
    op.execute(
        "ALTER TABLE ingestion_collections "
        "ADD COLUMN agent_id INTEGER REFERENCES agents(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX ix_collections_agent_id ON ingestion_collections(agent_id)"
    )

    op.execute("DROP POLICY IF EXISTS chunks_collection_isolation ON document_chunks")
    op.execute("DROP INDEX IF EXISTS ix_chunks_collection_only")
    op.execute("ALTER TABLE document_chunks ADD COLUMN agent_id INTEGER")
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD CONSTRAINT chunks_agent_required CHECK (agent_id IS NOT NULL)"
    )
    op.execute(
        "CREATE INDEX ix_chunks_agent_collection "
        "ON document_chunks(agent_id, collection_id)"
    )
    op.execute(
        """
        CREATE POLICY chunks_agent_isolation ON document_chunks
            FOR ALL
            USING (
                agent_id = NULLIF(
                    current_setting('anila.agent_id', true),
                    ''
                )::int
            )
        """
    )

    op.execute("ALTER TABLE ingestion_collections ALTER COLUMN created_by DROP NOT NULL")

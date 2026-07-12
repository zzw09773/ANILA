# -*- coding: utf-8 -*-
"""Gate 2 G3-G5 classified memory governance.

Revision ID: r1_0016
Revises: r1_0015
Create Date: 2026-07-12

``r1_0014``/``r1_0015`` are produced by the preceding authentication and
retrieval slices.  This branch intentionally references the final integrated
revision even when reviewed in isolation.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0016"
down_revision: Union[str, None] = "r1_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEVELS_SQL = "'無機密', '營業秘密', '機密', '極機密', '絕對機密'"


def _add_memory_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("classification_level", sa.String(length=20), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("classification_source", sa.String(length=80), nullable=True),
    )
    op.add_column(table_name, sa.Column("source_task_id", sa.Integer(), nullable=True))
    op.add_column(
        table_name, sa.Column("source_snapshot_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        f"fk_{table_name}_source_task",
        table_name,
        "tasks",
        ["source_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        f"ix_{table_name}_source_task_id", table_name, ["source_task_id"]
    )
    op.create_index(
        f"ix_{table_name}_source_snapshot_id", table_name, ["source_snapshot_id"]
    )
    op.create_foreign_key(
        f"fk_{table_name}_source_snapshot",
        table_name,
        "source_snapshots",
        ["source_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _create_association_table(
    table_name: str,
    owner_column: str,
    owner_table: str,
    value_column: str,
    value_table: str,
) -> None:
    op.create_table(
        table_name,
        sa.Column(owner_column, sa.BigInteger(), primary_key=True),
        sa.Column(value_column, sa.Integer(), primary_key=True),
        sa.ForeignKeyConstraint(
            [owner_column], [f"{owner_table}.id"], ondelete="CASCADE"
        ),
        # RESTRICT is deliberate: deleting a governance source must not erase
        # the access requirement while leaving derived memory readable.
        sa.ForeignKeyConstraint(
            [value_column], [f"{value_table}.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        f"ix_{table_name}_{value_column}", table_name, [value_column]
    )


def upgrade() -> None:
    _add_memory_columns("user_facts")
    _add_memory_columns("conversation_memory_chunks")

    _create_association_table(
        "user_fact_required_compartments",
        "fact_id",
        "user_facts",
        "compartment_id",
        "security_compartments",
    )
    _create_association_table(
        "memory_chunk_required_compartments",
        "chunk_id",
        "conversation_memory_chunks",
        "compartment_id",
        "security_compartments",
    )
    _create_association_table(
        "user_fact_source_collections",
        "fact_id",
        "user_facts",
        "collection_id",
        "ingestion_collections",
    )
    _create_association_table(
        "memory_chunk_source_collections",
        "chunk_id",
        "conversation_memory_chunks",
        "collection_id",
        "ingestion_collections",
    )

    # Legacy rows with a verifiable source inherit it.  Missing/corrupt source
    # metadata is not treated as public: it receives the fail-closed top floor.
    op.execute(
        f"""
        UPDATE user_facts AS fact
        SET classification_level = CASE
                WHEN conversation.classification_level IN ({_LEVELS_SQL})
                    THEN conversation.classification_level
                ELSE '絕對機密'
            END,
            classification_source = CASE
                WHEN conversation.classification_level IN ({_LEVELS_SQL})
                    THEN 'r1_0016:source_conversation'
                ELSE 'r1_0016:legacy_unknown_source'
            END
        FROM conversations AS conversation
        WHERE fact.source_conversation_id = conversation.id
        """
    )
    op.execute(
        """
        UPDATE user_facts
        SET classification_level = '絕對機密',
            classification_source = 'r1_0016:legacy_unknown_source'
        WHERE classification_level IS NULL OR classification_source IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE conversation_memory_chunks AS chunk
        SET classification_level = CASE
                WHEN conversation.classification_level IN ({_LEVELS_SQL})
                    THEN conversation.classification_level
                ELSE '絕對機密'
            END,
            classification_source = CASE
                WHEN conversation.classification_level IN ({_LEVELS_SQL})
                    THEN 'r1_0016:source_conversation'
                ELSE 'r1_0016:legacy_unknown_source'
            END
        FROM conversations AS conversation
        WHERE chunk.conversation_id = conversation.id
        """
    )
    op.execute(
        """
        UPDATE conversation_memory_chunks
        SET classification_level = '絕對機密',
            classification_source = 'r1_0016:legacy_unknown_source'
        WHERE classification_level IS NULL OR classification_source IS NULL
        """
    )
    op.execute(
        """
        UPDATE conversation_memory_chunks
        SET is_encrypted = true
        WHERE classification_level IN ('機密', '極機密', '絕對機密')
        """
    )

    op.execute(
        """
        INSERT INTO user_fact_source_collections (fact_id, collection_id)
        SELECT fact.id, conversation.collection_id
        FROM user_facts AS fact
        JOIN conversations AS conversation
          ON conversation.id = fact.source_conversation_id
        WHERE conversation.collection_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO memory_chunk_source_collections (chunk_id, collection_id)
        SELECT chunk.id, conversation.collection_id
        FROM conversation_memory_chunks AS chunk
        JOIN conversations AS conversation ON conversation.id = chunk.conversation_id
        WHERE conversation.collection_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE user_facts AS fact
        SET classification_level = collection.classification_level,
            classification_source = 'r1_0016:source_collection'
        FROM user_fact_source_collections AS source
        JOIN ingestion_collections AS collection
          ON collection.id = source.collection_id
        WHERE source.fact_id = fact.id
          AND anila_gate2_classification_rank(fact.classification_level)
              < anila_gate2_classification_rank(collection.classification_level)
        """
    )
    op.execute(
        """
        UPDATE conversation_memory_chunks AS chunk
        SET classification_level = collection.classification_level,
            classification_source = 'r1_0016:source_collection'
        FROM memory_chunk_source_collections AS source
        JOIN ingestion_collections AS collection
          ON collection.id = source.collection_id
        WHERE source.chunk_id = chunk.id
          AND anila_gate2_classification_rank(chunk.classification_level)
              < anila_gate2_classification_rank(collection.classification_level)
        """
    )
    op.execute(
        """
        UPDATE conversation_memory_chunks
        SET is_encrypted = true
        WHERE classification_level IN ('機密', '極機密', '絕對機密')
        """
    )
    op.execute(
        """
        INSERT INTO user_fact_required_compartments (fact_id, compartment_id)
        SELECT source.fact_id, requirement.compartment_id
        FROM user_fact_source_collections AS source
        JOIN collection_required_compartments AS requirement
          ON requirement.collection_id = source.collection_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO memory_chunk_required_compartments (chunk_id, compartment_id)
        SELECT source.chunk_id, requirement.compartment_id
        FROM memory_chunk_source_collections AS source
        JOIN collection_required_compartments AS requirement
          ON requirement.collection_id = source.collection_id
        ON CONFLICT DO NOTHING
        """
    )

    for table_name in ("user_facts", "conversation_memory_chunks"):
        op.alter_column(table_name, "classification_level", nullable=False)
        op.alter_column(table_name, "classification_source", nullable=False)
        op.create_check_constraint(
            f"ck_{table_name}_classification_level",
            table_name,
            f"classification_level IN ({_LEVELS_SQL})",
        )
        op.create_check_constraint(
            f"ck_{table_name}_classification_source",
            table_name,
            "length(trim(classification_source)) > 0",
        )
        op.create_index(
            f"ix_{table_name}_classification_level",
            table_name,
            ["classification_level"],
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_no_write_down()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND anila_gate2_classification_rank(NEW.classification_level)
                   < anila_gate2_classification_rank(OLD.classification_level)
            THEN
                NEW.classification_level := OLD.classification_level;
                NEW.classification_source := OLD.classification_source;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table_name in ("user_facts", "conversation_memory_chunks"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_no_write_down
            BEFORE UPDATE OF classification_level ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_no_write_down()
            """
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_source_floor()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            source_level text;
            source_conversation integer;
            source_owner integer;
        BEGIN
            IF TG_TABLE_NAME = 'user_facts' THEN
                source_conversation := NEW.source_conversation_id;
            ELSE
                source_conversation := NEW.conversation_id;
            END IF;
            IF source_conversation IS NULL THEN
                IF TG_OP = 'INSERT' THEN
                    RAISE EXCEPTION 'new memory row requires source conversation';
                END IF;
            ELSE
                SELECT classification_level, user_id
                INTO STRICT source_level, source_owner
                FROM conversations WHERE id = source_conversation;
                IF source_owner <> NEW.user_id THEN
                    RAISE EXCEPTION 'memory source conversation owner mismatch';
                END IF;
            END IF;
            IF NEW.source_task_id IS NOT NULL THEN
                SELECT classification_level, requester_user_id
                INTO STRICT source_level, source_owner
                FROM tasks WHERE id = NEW.source_task_id;
                IF source_owner <> NEW.user_id THEN
                    RAISE EXCEPTION 'memory source task owner mismatch';
                END IF;
                IF anila_gate2_classification_rank(NEW.classification_level)
                   < anila_gate2_classification_rank(source_level)
                THEN
                    NEW.classification_level := source_level;
                    NEW.classification_source := 'db_trigger:source_task';
                END IF;
            END IF;
            IF NEW.source_snapshot_id IS NOT NULL THEN
                SELECT snapshot.classification_level, task.requester_user_id
                INTO STRICT source_level, source_owner
                FROM source_snapshots AS snapshot
                JOIN tasks AS task ON task.id = snapshot.task_id
                WHERE snapshot.id = NEW.source_snapshot_id;
                IF source_owner <> NEW.user_id THEN
                    RAISE EXCEPTION 'memory source snapshot owner mismatch';
                END IF;
                IF NEW.source_task_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM source_snapshots
                    WHERE id = NEW.source_snapshot_id
                      AND task_id = NEW.source_task_id
                ) THEN
                    RAISE EXCEPTION 'memory task/snapshot provenance mismatch';
                END IF;
                IF anila_gate2_classification_rank(NEW.classification_level)
                   < anila_gate2_classification_rank(source_level)
                THEN
                    NEW.classification_level := source_level;
                    NEW.classification_source := 'db_trigger:source_snapshot';
                END IF;
            END IF;
            IF source_conversation IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT classification_level INTO STRICT source_level
            FROM conversations WHERE id = source_conversation;
            IF anila_gate2_classification_rank(NEW.classification_level)
               < anila_gate2_classification_rank(source_level)
            THEN
                NEW.classification_level := source_level;
                NEW.classification_source := 'db_trigger:source_conversation';
            END IF;
            IF TG_TABLE_NAME = 'conversation_memory_chunks'
               AND anila_gate2_classification_rank(NEW.classification_level)
                   >= anila_gate2_classification_rank('機密')
            THEN
                NEW.is_encrypted := true;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_chunks_source_floor
        BEFORE INSERT OR UPDATE OF classification_level, conversation_id,
            source_task_id, source_snapshot_id
        ON conversation_memory_chunks
        FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_source_floor()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_user_facts_source_floor
        BEFORE INSERT OR UPDATE OF classification_level, source_conversation_id,
            source_task_id, source_snapshot_id
        ON user_facts
        FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_source_floor()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_conversation_upgrade()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF anila_gate2_classification_rank(NEW.classification_level)
               > anila_gate2_classification_rank(OLD.classification_level)
            THEN
                UPDATE conversation_memory_chunks
                SET classification_level = NEW.classification_level,
                    classification_source = 'db_trigger:conversation_upgrade',
                    is_encrypted = CASE
                        WHEN anila_gate2_classification_rank(NEW.classification_level)
                             >= anila_gate2_classification_rank('機密')
                        THEN true ELSE is_encrypted END
                WHERE conversation_id = NEW.id;
                UPDATE user_facts
                SET classification_level = NEW.classification_level,
                    classification_source = 'db_trigger:conversation_upgrade'
                WHERE source_conversation_id = NEW.id;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_conversation_upgrade
        AFTER UPDATE OF classification_level ON conversations
        FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_conversation_upgrade()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_collection_source_added()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE source_level text;
        BEGIN
            SELECT classification_level INTO STRICT source_level
            FROM ingestion_collections WHERE id = NEW.collection_id;
            IF TG_TABLE_NAME = 'user_fact_source_collections' THEN
                UPDATE user_facts
                SET classification_level = source_level,
                    classification_source = 'db_trigger:collection_source'
                WHERE id = NEW.fact_id
                  AND anila_gate2_classification_rank(classification_level)
                      < anila_gate2_classification_rank(source_level);
                INSERT INTO user_fact_required_compartments(fact_id, compartment_id)
                SELECT NEW.fact_id, compartment_id
                FROM collection_required_compartments
                WHERE collection_id = NEW.collection_id
                ON CONFLICT DO NOTHING;
            ELSE
                UPDATE conversation_memory_chunks
                SET classification_level = source_level,
                    classification_source = 'db_trigger:collection_source',
                    is_encrypted = CASE
                        WHEN anila_gate2_classification_rank(source_level)
                             >= anila_gate2_classification_rank('機密')
                        THEN true ELSE is_encrypted END
                WHERE id = NEW.chunk_id
                  AND anila_gate2_classification_rank(classification_level)
                      < anila_gate2_classification_rank(source_level);
                INSERT INTO memory_chunk_required_compartments(chunk_id, compartment_id)
                SELECT NEW.chunk_id, compartment_id
                FROM collection_required_compartments
                WHERE collection_id = NEW.collection_id
                ON CONFLICT DO NOTHING;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    for table_name in (
        "user_fact_source_collections",
        "memory_chunk_source_collections",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_floor
            AFTER INSERT ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_collection_source_added()
            """
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_collection_upgrade()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF anila_gate2_classification_rank(NEW.classification_level)
               > anila_gate2_classification_rank(OLD.classification_level)
            THEN
                UPDATE user_facts AS fact
                SET classification_level = NEW.classification_level,
                    classification_source = 'db_trigger:collection_upgrade'
                FROM user_fact_source_collections AS source
                WHERE source.collection_id = NEW.id AND source.fact_id = fact.id;
                UPDATE conversation_memory_chunks AS chunk
                SET classification_level = NEW.classification_level,
                    classification_source = 'db_trigger:collection_upgrade',
                    is_encrypted = CASE
                        WHEN anila_gate2_classification_rank(NEW.classification_level)
                             >= anila_gate2_classification_rank('機密')
                        THEN true ELSE chunk.is_encrypted END
                FROM memory_chunk_source_collections AS source
                WHERE source.collection_id = NEW.id AND source.chunk_id = chunk.id;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_collection_upgrade
        AFTER UPDATE OF classification_level ON ingestion_collections
        FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_collection_upgrade()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION anila_gate2_memory_compartment_requirement_added()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO user_fact_required_compartments(fact_id, compartment_id)
            SELECT source.fact_id, NEW.compartment_id
            FROM user_fact_source_collections AS source
            WHERE source.collection_id = NEW.collection_id
            ON CONFLICT DO NOTHING;
            INSERT INTO memory_chunk_required_compartments(chunk_id, compartment_id)
            SELECT source.chunk_id, NEW.compartment_id
            FROM memory_chunk_source_collections AS source
            WHERE source.collection_id = NEW.collection_id
            ON CONFLICT DO NOTHING;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_collection_compartment_added
        AFTER INSERT ON collection_required_compartments
        FOR EACH ROW EXECUTE FUNCTION anila_gate2_memory_compartment_requirement_added()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_memory_collection_compartment_added "
        "ON collection_required_compartments"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS anila_gate2_memory_compartment_requirement_added()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_memory_collection_upgrade ON ingestion_collections"
    )
    op.execute("DROP FUNCTION IF EXISTS anila_gate2_memory_collection_upgrade()")
    for table_name in (
        "memory_chunk_source_collections",
        "user_fact_source_collections",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_floor ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS anila_gate2_memory_collection_source_added()")
    op.execute("DROP TRIGGER IF EXISTS trg_memory_conversation_upgrade ON conversations")
    op.execute("DROP FUNCTION IF EXISTS anila_gate2_memory_conversation_upgrade()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_user_facts_source_floor ON user_facts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_memory_chunks_source_floor "
        "ON conversation_memory_chunks"
    )
    op.execute("DROP FUNCTION IF EXISTS anila_gate2_memory_source_floor()")
    for table_name in ("conversation_memory_chunks", "user_facts"):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_no_write_down ON {table_name}"
        )
    op.execute("DROP FUNCTION IF EXISTS anila_gate2_memory_no_write_down()")

    for table_name in ("conversation_memory_chunks", "user_facts"):
        op.drop_index(
            f"ix_{table_name}_classification_level", table_name=table_name
        )
        op.drop_constraint(
            f"ck_{table_name}_classification_source",
            table_name,
            type_="check",
        )
        op.drop_constraint(
            f"ck_{table_name}_classification_level",
            table_name,
            type_="check",
        )

    for table_name, value_column in (
        ("memory_chunk_source_collections", "collection_id"),
        ("user_fact_source_collections", "collection_id"),
        ("memory_chunk_required_compartments", "compartment_id"),
        ("user_fact_required_compartments", "compartment_id"),
    ):
        op.drop_index(
            f"ix_{table_name}_{value_column}", table_name=table_name
        )
        op.drop_table(table_name)

    for table_name in ("conversation_memory_chunks", "user_facts"):
        op.drop_index(
            f"ix_{table_name}_source_snapshot_id", table_name=table_name
        )
        op.drop_index(f"ix_{table_name}_source_task_id", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_source_snapshot", table_name, type_="foreignkey"
        )
        op.drop_constraint(
            f"fk_{table_name}_source_task", table_name, type_="foreignkey"
        )
        op.drop_column(table_name, "source_snapshot_id")
        op.drop_column(table_name, "source_task_id")
        op.drop_column(table_name, "classification_source")
        op.drop_column(table_name, "classification_level")

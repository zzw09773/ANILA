# -*- coding: utf-8 -*-
"""Gate 2 G1b classification backfill, reconciliation, and value constraints.

This migration deliberately fails before changing data when it finds an
unknown classification string or a broken document/chunk ownership join.
Known lower rows are then raised to the effective collection/document floor;
the migration never lowers a classification.

Revision ID: r1_0011
Revises: r1_0010
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r1_0011"
down_revision: Union[str, None] = "r1_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEVELS = ("無機密", "營業秘密", "機密", "極機密", "絕對機密")
_LEVEL_SQL = ", ".join(f"'{level}'" for level in _LEVELS)

# Every persisted classification-domain value in the r1_0010 schema. This is
# intentionally broader than columns named ``classification_level``: ceilings,
# export target floors, and transition provenance are equally policy-bearing.
_CLASSIFICATION_COLUMNS = (
    ("tasks", "classification_level"),
    ("task_runs", "classification_level"),
    ("trace_spans", "classification_level"),
    ("policy_decisions", "classification_level"),
    ("source_snapshots", "classification_level"),
    ("citations", "classification_level"),
    ("conversations", "classification_level"),
    ("messages", "classification_level"),
    ("ingestion_collections", "classification_level"),
    ("ingestion_documents", "classification_level"),
    ("document_chunks", "classification_level"),
    ("agents", "default_classification_level"),
    ("agents", "classification_ceiling"),
    ("model_registry", "classification_ceiling"),
    ("registered_services", "classification_ceiling"),
    ("service_launches", "classification_level"),
    ("service_audit_callbacks", "classification_level"),
    ("artifacts", "classification_level"),
    ("artifact_versions", "classification_level"),
    ("export_records", "target_classification_floor"),
    ("export_records", "classification_level"),
    ("classification_events", "previous_level"),
    ("classification_events", "new_level"),
    ("declassification_requests", "from_level"),
    ("declassification_requests", "to_level"),
)

# r1_0010 allowed these policy-bearing fields to be NULL. Gate 2 makes the
# least-privilege value explicit and durable for both ORM and direct SQL paths.
_REQUIRED_NOT_NULL_DEFAULTS = (
    ("agents", "classification_ceiling"),
    ("model_registry", "classification_ceiling"),
    ("registered_services", "classification_ceiling"),
    ("service_audit_callbacks", "classification_level"),
    ("export_records", "target_classification_floor"),
)

_PROVENANCE_TABLES = (
    "tasks",
    "task_runs",
    "source_snapshots",
    "conversations",
    "messages",
    "ingestion_collections",
    "ingestion_documents",
    "document_chunks",
    "artifacts",
    "export_records",
)

_LOCK_TABLES = tuple(sorted({table for table, _column in _CLASSIFICATION_COLUMNS}))


def _rank(expression: str) -> str:
    return (
        f"CASE {expression} "
        "WHEN '無機密' THEN 0 "
        "WHEN '營業秘密' THEN 1 "
        "WHEN '機密' THEN 2 "
        "WHEN '極機密' THEN 3 "
        "WHEN '絕對機密' THEN 4 END"
    )


def _constraint_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_gate2_level"


def _lock_classification_tables() -> None:
    # SHARE ROW EXCLUSIVE conflicts with normal INSERT/UPDATE/DELETE locks.
    # The maintenance transaction therefore owns a stable reconciliation set
    # from preflight through final validation and schema guard installation.
    op.execute(
        sa.text(
            "LOCK TABLE "
            + ", ".join(_LOCK_TABLES)
            + " IN SHARE ROW EXCLUSIVE MODE"
        )
    )


def _fail_on_invalid_values() -> None:
    for table, column in _CLASSIFICATION_COLUMNS:
        op.execute(
            sa.text(
                f"""
                DO $$
                DECLARE bad_count BIGINT;
                BEGIN
                  SELECT COUNT(*) INTO bad_count
                    FROM {table}
                   WHERE {column} IS NOT NULL
                     AND {column} NOT IN ({_LEVEL_SQL});
                  IF bad_count <> 0 THEN
                    RAISE EXCEPTION
                      'Gate 2 classification preflight failed: %.% has % invalid rows',
                      '{table}', '{column}', bad_count;
                  END IF;
                END $$;
                """
            )
        )


def _fail_on_broken_hierarchy() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE bad_count BIGINT;
            BEGIN
              SELECT COUNT(*) INTO bad_count
                FROM ingestion_documents d
                LEFT JOIN ingestion_collections c ON c.id = d.collection_id
               WHERE c.id IS NULL;
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 classification preflight failed: % orphan documents',
                  bad_count;
              END IF;

              SELECT COUNT(*) INTO bad_count
                FROM document_chunks ch
                LEFT JOIN ingestion_documents d ON d.id = ch.document_id
                LEFT JOIN ingestion_collections c ON c.id = ch.collection_id
               WHERE d.id IS NULL OR c.id IS NULL
                  OR d.collection_id <> ch.collection_id;
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 classification preflight failed: % orphan or cross-collection chunks',
                  bad_count;
              END IF;

              SELECT COUNT(*) INTO bad_count
                FROM service_audit_callbacks cb
                JOIN service_launches sl ON sl.launch_id = cb.launch_id
               WHERE cb.service_id IS DISTINCT FROM sl.service_id;
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 classification preflight failed: % cross-service callbacks',
                  bad_count;
              END IF;
            END $$;
            """
        )
    )


def _fail_on_below_hierarchy() -> None:
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE bad_count BIGINT;
            BEGIN
              SELECT COUNT(*) INTO bad_count
                FROM ingestion_documents d
                JOIN ingestion_collections c ON c.id = d.collection_id
               WHERE {_rank('d.classification_level')} <
                     {_rank('c.classification_level')};
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 final validation failed: % documents below collection',
                  bad_count;
              END IF;

              SELECT COUNT(*) INTO bad_count
                FROM document_chunks ch
                JOIN ingestion_documents d
                  ON d.id = ch.document_id
                 AND d.collection_id = ch.collection_id
                JOIN ingestion_collections c ON c.id = ch.collection_id
               WHERE {_rank('ch.classification_level')} < GREATEST(
                         {_rank('d.classification_level')},
                         {_rank('c.classification_level')}
                     );
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 final validation failed: % chunks below source floor',
                  bad_count;
              END IF;

              SELECT COUNT(*) INTO bad_count
                FROM service_audit_callbacks cb
                JOIN service_launches sl ON sl.launch_id = cb.launch_id
               WHERE {_rank('cb.classification_level')} <
                     {_rank('sl.classification_level')};
              IF bad_count <> 0 THEN
                RAISE EXCEPTION
                  'Gate 2 final validation failed: % callbacks below launch floor',
                  bad_count;
              END IF;
            END $$;
            """
        )
    )


def _fail_on_null_values() -> None:
    for table, column in _CLASSIFICATION_COLUMNS:
        op.execute(
            sa.text(
                f"""
                DO $$
                DECLARE null_count BIGINT;
                BEGIN
                  SELECT COUNT(*) INTO null_count
                    FROM {table}
                   WHERE {column} IS NULL;
                  IF null_count <> 0 THEN
                    RAISE EXCEPTION
                      'Gate 2 classification backfill failed: %.% has % NULL rows',
                      '{table}', '{column}', null_count;
                  END IF;
                END $$;
                """
            )
        )


def upgrade() -> None:
    _lock_classification_tables()
    _fail_on_invalid_values()
    _fail_on_broken_hierarchy()

    # A missing ceiling is not "unlimited" after Gate 2. Existing NULLs get
    # the least-privilege explicit ceiling; signed pilot configuration may
    # raise it through the governed registry later.
    for table in ("agents", "model_registry", "registered_services"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET classification_ceiling = '無機密' "
                "WHERE classification_ceiling IS NULL"
            )
        )

    # An existing allow record implies the destination floor was at least the
    # exported artifact level. Use that minimal consistent value instead of
    # inventing a more permissive destination classification.
    op.execute(
        sa.text(
            """
            UPDATE export_records
               SET target_classification_floor = classification_level
             WHERE target_classification_floor IS NULL
            """
        )
    )

    # Preserve the launch level where provenance exists. Unlinked historical
    # callbacks are conservatively over-classified instead of guessed low.
    op.execute(
        sa.text(
            """
            UPDATE service_audit_callbacks cb
               SET classification_level = COALESCE(
                   (SELECT sl.classification_level
                      FROM service_launches sl
                     WHERE sl.launch_id = cb.launch_id),
                   '絕對機密'
               )
             WHERE cb.classification_level IS NULL
            """
        )
    )

    callback_rank = _rank("cb.classification_level")
    launch_rank = _rank("sl.classification_level")
    op.execute(
        sa.text(
            f"""
            UPDATE service_audit_callbacks cb
               SET classification_level = sl.classification_level
              FROM service_launches sl
             WHERE sl.launch_id = cb.launch_id
               AND {callback_rank} < {launch_rank}
            """
        )
    )

    for table in _PROVENANCE_TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                   SET classification_latched_at = COALESCE(
                           classification_latched_at, CURRENT_TIMESTAMP
                       ),
                       classification_source = COALESCE(
                           classification_source, 'gate2_reconciliation'
                       )
                 WHERE classification_latched_at IS NULL
                    OR classification_source IS NULL
                """
            )
        )

    # Documents inherit the collection floor. Only rows below the floor move.
    op.execute(
        sa.text(
            f"""
            UPDATE ingestion_documents d
               SET classification_level = c.classification_level,
                   classification_latched_at = CURRENT_TIMESTAMP,
                   classification_source = 'collection_inherited'
              FROM ingestion_collections c
             WHERE c.id = d.collection_id
               AND {_rank('d.classification_level')} <
                   {_rank('c.classification_level')}
            """
        )
    )

    # Chunks inherit max(existing, document, collection). The preflight above
    # guarantees every CASE has a value and the joins cannot silently omit rows.
    chunk_rank = _rank("ch.classification_level")
    document_rank = _rank("d.classification_level")
    collection_rank = _rank("c.classification_level")
    op.execute(
        sa.text(
            f"""
            UPDATE document_chunks ch
               SET classification_level = CASE GREATEST(
                       {chunk_rank}, {document_rank}, {collection_rank}
                   )
                       WHEN 0 THEN '無機密'
                       WHEN 1 THEN '營業秘密'
                       WHEN 2 THEN '機密'
                       WHEN 3 THEN '極機密'
                       WHEN 4 THEN '絕對機密'
                   END,
                   classification_latched_at = CURRENT_TIMESTAMP,
                   classification_source = 'ingestion_effective_backfill'
              FROM ingestion_documents d, ingestion_collections c
             WHERE d.id = ch.document_id
               AND c.id = ch.collection_id
               AND {chunk_rank} < GREATEST(
                   {document_rank}, {collection_rank}
               )
            """
        )
    )

    # Constrain every future non-NULL value to the canonical five-level set.
    for table, column in _CLASSIFICATION_COLUMNS:
        constraint = _constraint_name(table, column)
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                f"CHECK ({column} IN ({_LEVEL_SQL})) NOT VALID"
            )
        )
        op.execute(
            sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")
        )

    for table, column in _REQUIRED_NOT_NULL_DEFAULTS:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                "SET DEFAULT '無機密'"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"
            )
        )

    # Re-run every invariant while the writer-blocking locks are still held.
    # This is the final statement group before Alembic advances the revision.
    _fail_on_invalid_values()
    _fail_on_broken_hierarchy()
    _fail_on_below_hierarchy()
    _fail_on_null_values()


def downgrade() -> None:
    # Never lower already-reconciled classifications during downgrade. Only
    # remove the value-domain guards introduced by this revision.
    for table, column in reversed(_CLASSIFICATION_COLUMNS):
        op.execute(
            sa.text(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "
                f"{_constraint_name(table, column)}"
            )
        )

    # Restore only r1_0010 schema permissiveness. Reconciled values and raised
    # hierarchy levels are deliberately left untouched.
    for table, column in reversed(_REQUIRED_NOT_NULL_DEFAULTS):
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT"
            )
        )

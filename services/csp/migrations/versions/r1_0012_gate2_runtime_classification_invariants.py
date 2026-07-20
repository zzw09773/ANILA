# -*- coding: utf-8 -*-
"""Gate 2 runtime classification floors and monotonic parent propagation.

The r1_0011 reconciliation proves the historical database once.  This
revision keeps the same hierarchy true while the system is live:

* collection/document/chunk values remain in the canonical five-level set;
* document writes are raised to their collection floor;
* chunk writes are raised to max(document, collection, requested value);
* collection/document upgrades raise descendants in the same transaction;
* unapproved parent downgrades are ignored; the existing
  ``declassification_approved`` marker may lower a parent but never its
  descendants;
* chunk updates are monotonic and include the existing row in their floor.

Revision ID: r1_0012
Revises: r1_0011
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r1_0012"
down_revision: Union[str, None] = "r1_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            r"""
            CREATE OR REPLACE FUNCTION anila_gate2_classification_rank(level_value TEXT)
            RETURNS INTEGER
            LANGUAGE plpgsql
            IMMUTABLE
            STRICT
            AS $$
            BEGIN
              CASE level_value
                WHEN '無機密' THEN RETURN 0;
                WHEN '營業秘密' THEN RETURN 1;
                WHEN '機密' THEN RETURN 2;
                WHEN '極機密' THEN RETURN 3;
                WHEN '絕對機密' THEN RETURN 4;
                ELSE
                  RAISE EXCEPTION 'invalid Gate 2 classification value: %', level_value
                    USING ERRCODE = '23514';
              END CASE;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_touch_collection_classification()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
              IF NEW.classification_level IS NULL THEN
                RAISE EXCEPTION 'ingestion_collections.classification_level cannot be NULL'
                  USING ERRCODE = '23502';
              END IF;
              PERFORM anila_gate2_classification_rank(NEW.classification_level);

              IF TG_OP = 'UPDATE' THEN
                IF anila_gate2_classification_rank(NEW.classification_level) <
                   anila_gate2_classification_rank(OLD.classification_level)
                   AND NOT (
                     NEW.classification_source IS NOT DISTINCT FROM
                         'declassification_approved'
                     AND NEW.classification_latched_at IS DISTINCT FROM
                         OLD.classification_latched_at
                     AND NEW.classification_event_id IS NOT NULL
                     AND NEW.classification_event_id IS DISTINCT FROM
                         OLD.classification_event_id
                   ) THEN
                  NEW.classification_level := OLD.classification_level;
                  NEW.classification_latched_at := OLD.classification_latched_at;
                  NEW.classification_source := OLD.classification_source;
                  NEW.classification_event_id := OLD.classification_event_id;
                END IF;
              END IF;

              IF TG_OP = 'INSERT' THEN
                NEW.classification_latched_at := COALESCE(
                    NEW.classification_latched_at, CURRENT_TIMESTAMP
                );
                NEW.classification_source := COALESCE(
                    NEW.classification_source, 'collection_classification_trigger'
                );
              ELSIF NEW.classification_level IS DISTINCT FROM OLD.classification_level THEN
                IF NEW.classification_latched_at IS NOT DISTINCT FROM
                   OLD.classification_latched_at THEN
                  NEW.classification_latched_at := CURRENT_TIMESTAMP;
                END IF;
                IF NEW.classification_source IS NOT DISTINCT FROM OLD.classification_source THEN
                  NEW.classification_source := 'collection_classification_trigger';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_enforce_document_floor()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
              collection_level TEXT;
            BEGIN
              IF NEW.classification_level IS NULL THEN
                RAISE EXCEPTION 'ingestion_documents.classification_level cannot be NULL'
                  USING ERRCODE = '23502';
              END IF;
              PERFORM anila_gate2_classification_rank(NEW.classification_level);

              IF TG_OP = 'UPDATE' THEN
                IF anila_gate2_classification_rank(NEW.classification_level) <
                   anila_gate2_classification_rank(OLD.classification_level)
                   AND NOT (
                     NEW.classification_source IS NOT DISTINCT FROM
                         'declassification_approved'
                     AND NEW.classification_latched_at IS DISTINCT FROM
                         OLD.classification_latched_at
                     AND NEW.classification_event_id IS NOT NULL
                     AND NEW.classification_event_id IS DISTINCT FROM
                         OLD.classification_event_id
                   ) THEN
                  NEW.classification_level := OLD.classification_level;
                  NEW.classification_latched_at := OLD.classification_latched_at;
                  NEW.classification_source := OLD.classification_source;
                  NEW.classification_event_id := OLD.classification_event_id;
                END IF;
              END IF;

              -- Global lock order for ingestion hierarchy: collection, document.
              SELECT c.classification_level
                INTO collection_level
                FROM ingestion_collections c
               WHERE c.id = NEW.collection_id
                 FOR SHARE;
              IF NOT FOUND THEN
                RAISE EXCEPTION 'collection % does not exist', NEW.collection_id
                  USING ERRCODE = '23503';
              END IF;

              IF anila_gate2_classification_rank(NEW.classification_level) <
                 anila_gate2_classification_rank(collection_level) THEN
                NEW.classification_level := collection_level;
                NEW.classification_latched_at := CURRENT_TIMESTAMP;
                NEW.classification_source := 'collection_floor_trigger';
              ELSIF TG_OP = 'INSERT' THEN
                NEW.classification_latched_at := COALESCE(
                    NEW.classification_latched_at, CURRENT_TIMESTAMP
                );
                NEW.classification_source := COALESCE(
                    NEW.classification_source, 'document_classification_trigger'
                );
              ELSIF NEW.classification_level IS DISTINCT FROM OLD.classification_level THEN
                IF NEW.classification_latched_at IS NOT DISTINCT FROM
                   OLD.classification_latched_at THEN
                  NEW.classification_latched_at := CURRENT_TIMESTAMP;
                END IF;
                IF NEW.classification_source IS NOT DISTINCT FROM OLD.classification_source THEN
                  NEW.classification_source := 'document_classification_trigger';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_enforce_chunk_floor()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
              collection_level TEXT;
              document_level TEXT;
              document_collection_id BIGINT;
              effective_level TEXT;
            BEGIN
              IF NEW.classification_level IS NULL THEN
                RAISE EXCEPTION 'document_chunks.classification_level cannot be NULL'
                  USING ERRCODE = '23502';
              END IF;
              PERFORM anila_gate2_classification_rank(NEW.classification_level);

              -- Match the SDK lock order so either side of a concurrent parent
              -- upgrade deterministically converges without a low child row.
              SELECT c.classification_level
                INTO collection_level
                FROM ingestion_collections c
               WHERE c.id = NEW.collection_id
                 FOR SHARE;
              IF NOT FOUND THEN
                RAISE EXCEPTION 'collection % does not exist', NEW.collection_id
                  USING ERRCODE = '23503';
              END IF;

              SELECT d.collection_id, d.classification_level
                INTO document_collection_id, document_level
                FROM ingestion_documents d
               WHERE d.id = NEW.document_id
                 FOR SHARE;
              IF NOT FOUND THEN
                RAISE EXCEPTION 'document % does not exist', NEW.document_id
                  USING ERRCODE = '23503';
              END IF;
              IF document_collection_id <> NEW.collection_id THEN
                RAISE EXCEPTION
                  'document % belongs to collection %, not %',
                  NEW.document_id, document_collection_id, NEW.collection_id
                  USING ERRCODE = '23514';
              END IF;

              effective_level := NEW.classification_level;
              IF TG_OP = 'UPDATE' AND
                 anila_gate2_classification_rank(OLD.classification_level) >
                 anila_gate2_classification_rank(effective_level) THEN
                effective_level := OLD.classification_level;
              END IF;
              IF anila_gate2_classification_rank(document_level) >
                 anila_gate2_classification_rank(effective_level) THEN
                effective_level := document_level;
              END IF;
              IF anila_gate2_classification_rank(collection_level) >
                 anila_gate2_classification_rank(effective_level) THEN
                effective_level := collection_level;
              END IF;

              IF effective_level IS DISTINCT FROM NEW.classification_level THEN
                NEW.classification_level := effective_level;
                NEW.classification_latched_at := CURRENT_TIMESTAMP;
                NEW.classification_source := 'ingestion_effective_trigger';
              ELSIF TG_OP = 'INSERT' THEN
                NEW.classification_latched_at := COALESCE(
                    NEW.classification_latched_at, CURRENT_TIMESTAMP
                );
                NEW.classification_source := COALESCE(
                    NEW.classification_source, 'ingestion_effective_trigger'
                );
              ELSIF NEW.classification_level IS DISTINCT FROM OLD.classification_level THEN
                IF NEW.classification_latched_at IS NOT DISTINCT FROM
                   OLD.classification_latched_at THEN
                  NEW.classification_latched_at := CURRENT_TIMESTAMP;
                END IF;
                IF NEW.classification_source IS NOT DISTINCT FROM OLD.classification_source THEN
                  NEW.classification_source := 'chunk_classification_trigger';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_cascade_document_upgrade()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
              IF anila_gate2_classification_rank(NEW.classification_level) >
                 anila_gate2_classification_rank(OLD.classification_level) THEN
                -- document_chunks is FORCE RLS. Trigger execution under the
                -- runtime csp_app role must set the exact collection scope.
                PERFORM set_config(
                    'anila.collection_id', NEW.collection_id::TEXT, true
                );
                UPDATE document_chunks ch
                   SET classification_level = NEW.classification_level,
                       classification_latched_at = CURRENT_TIMESTAMP,
                       classification_source = 'document_upgrade_cascade'
                 WHERE ch.document_id = NEW.id
                   AND ch.collection_id = NEW.collection_id
                   AND anila_gate2_classification_rank(ch.classification_level) <
                       anila_gate2_classification_rank(NEW.classification_level);
              END IF;
              RETURN NULL;
            END;
            $$;

            CREATE OR REPLACE FUNCTION anila_gate2_cascade_collection_upgrade()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
              IF anila_gate2_classification_rank(NEW.classification_level) >
                 anila_gate2_classification_rank(OLD.classification_level) THEN
                UPDATE ingestion_documents d
                   SET classification_level = NEW.classification_level,
                       classification_latched_at = CURRENT_TIMESTAMP,
                       classification_source = 'collection_upgrade_cascade'
                 WHERE d.collection_id = NEW.id
                   AND anila_gate2_classification_rank(d.classification_level) <
                       anila_gate2_classification_rank(NEW.classification_level);

                -- Defence in depth for rows created before document triggers,
                -- or a higher document whose chunk somehow remained lower.
                PERFORM set_config(
                    'anila.collection_id', NEW.id::TEXT, true
                );
                UPDATE document_chunks ch
                   SET classification_level = NEW.classification_level,
                       classification_latched_at = CURRENT_TIMESTAMP,
                       classification_source = 'collection_upgrade_cascade'
                 WHERE ch.collection_id = NEW.id
                   AND anila_gate2_classification_rank(ch.classification_level) <
                       anila_gate2_classification_rank(NEW.classification_level);
              END IF;
              RETURN NULL;
            END;
            $$;

            CREATE TRIGGER trg_gate2_collection_classification_insert
              BEFORE INSERT ON ingestion_collections
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_touch_collection_classification();
            CREATE TRIGGER trg_gate2_collection_classification_update
              BEFORE UPDATE OF classification_level ON ingestion_collections
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_touch_collection_classification();
            CREATE TRIGGER trg_gate2_collection_upgrade_cascade
              AFTER UPDATE OF classification_level ON ingestion_collections
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_cascade_collection_upgrade();

            CREATE TRIGGER trg_gate2_document_classification_insert
              BEFORE INSERT ON ingestion_documents
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_enforce_document_floor();
            CREATE TRIGGER trg_gate2_document_classification_update
              BEFORE UPDATE OF classification_level, collection_id ON ingestion_documents
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_enforce_document_floor();
            CREATE TRIGGER trg_gate2_document_upgrade_cascade
              AFTER UPDATE OF classification_level ON ingestion_documents
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_cascade_document_upgrade();

            CREATE TRIGGER trg_gate2_chunk_classification_insert
              BEFORE INSERT ON document_chunks
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_enforce_chunk_floor();
            CREATE TRIGGER trg_gate2_chunk_classification_update
              BEFORE UPDATE OF classification_level, document_id, collection_id
              ON document_chunks
              FOR EACH ROW EXECUTE FUNCTION anila_gate2_enforce_chunk_floor();
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_gate2_chunk_classification_update
              ON document_chunks;
            DROP TRIGGER IF EXISTS trg_gate2_chunk_classification_insert
              ON document_chunks;
            DROP TRIGGER IF EXISTS trg_gate2_document_upgrade_cascade
              ON ingestion_documents;
            DROP TRIGGER IF EXISTS trg_gate2_document_classification_update
              ON ingestion_documents;
            DROP TRIGGER IF EXISTS trg_gate2_document_classification_insert
              ON ingestion_documents;
            DROP TRIGGER IF EXISTS trg_gate2_collection_upgrade_cascade
              ON ingestion_collections;
            DROP TRIGGER IF EXISTS trg_gate2_collection_classification_update
              ON ingestion_collections;
            DROP TRIGGER IF EXISTS trg_gate2_collection_classification_insert
              ON ingestion_collections;

            DROP FUNCTION IF EXISTS anila_gate2_cascade_collection_upgrade();
            DROP FUNCTION IF EXISTS anila_gate2_cascade_document_upgrade();
            DROP FUNCTION IF EXISTS anila_gate2_enforce_chunk_floor();
            DROP FUNCTION IF EXISTS anila_gate2_enforce_document_floor();
            DROP FUNCTION IF EXISTS anila_gate2_touch_collection_classification();
            DROP FUNCTION IF EXISTS anila_gate2_classification_rank(TEXT);
            """
        )
    )

"""True PostgreSQL proof for r1_0016 memory classification triggers."""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker


DSN = os.environ.get("GATE2_MEMORY_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="GATE2_MEMORY_PG_DSN 未設定，略過真 PostgreSQL migration 測試"
)

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/r1_0016_gate2_memory_classification_governance.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("gate2_memory_r1_0016", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bootstrap(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE users (
            id serial PRIMARY KEY,
            username text NOT NULL UNIQUE
        );
        CREATE TABLE ingestion_collections (
            id serial PRIMARY KEY,
            created_by integer NOT NULL REFERENCES users(id),
            classification_level varchar(20) NOT NULL
        );
        CREATE TABLE security_compartments (
            id serial PRIMARY KEY,
            code text NOT NULL,
            is_active boolean NOT NULL DEFAULT true
        );
        CREATE TABLE collection_required_compartments (
            collection_id integer NOT NULL REFERENCES ingestion_collections(id),
            compartment_id integer NOT NULL REFERENCES security_compartments(id),
            PRIMARY KEY(collection_id, compartment_id)
        );
        CREATE TABLE clearance_grants (
            id serial PRIMARY KEY,
            subject_user_id integer NOT NULL REFERENCES users(id),
            max_classification_level varchar(20) NOT NULL,
            valid_from timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            basis_ticket varchar(255) NOT NULL,
            issued_by_user_id integer NOT NULL REFERENCES users(id),
            revoked_at timestamptz,
            revoked_by_user_id integer REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE clearance_grant_compartments (
            clearance_grant_id integer NOT NULL REFERENCES clearance_grants(id),
            compartment_id integer NOT NULL REFERENCES security_compartments(id),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(clearance_grant_id, compartment_id)
        );
        CREATE TABLE collection_access_grants (
            id serial PRIMARY KEY,
            clearance_grant_id integer NOT NULL REFERENCES clearance_grants(id),
            collection_id integer NOT NULL REFERENCES ingestion_collections(id),
            membership_granted boolean NOT NULL,
            need_to_know boolean NOT NULL,
            basis_ticket varchar(255) NOT NULL,
            issued_by_user_id integer NOT NULL REFERENCES users(id),
            revoked_at timestamptz,
            revoked_by_user_id integer REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE conversations (
            id serial PRIMARY KEY,
            user_id integer NOT NULL REFERENCES users(id),
            collection_id integer REFERENCES ingestion_collections(id),
            classification_level varchar(20) NOT NULL
        );
        CREATE TABLE tasks (
            id serial PRIMARY KEY,
            requester_user_id integer NOT NULL REFERENCES users(id),
            conversation_id integer REFERENCES conversations(id),
            classification_level varchar(20) NOT NULL,
            trace_id text NOT NULL,
            selected_collection_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_snapshot_id integer
        );
        CREATE TABLE source_snapshots (
            id serial PRIMARY KEY,
            task_id integer NOT NULL REFERENCES tasks(id),
            classification_level varchar(20) NOT NULL,
            collection_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            document_ids jsonb NOT NULL DEFAULT '[]'::jsonb
        );
        CREATE TABLE user_facts (
            id bigserial PRIMARY KEY,
            user_id integer NOT NULL REFERENCES users(id),
            key varchar(120) NOT NULL,
            value text NOT NULL,
            source_conversation_id integer REFERENCES conversations(id),
            source_message_id integer,
            confidence double precision NOT NULL DEFAULT 1.0,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, key)
        );
        CREATE TABLE conversation_memory_chunks (
            id bigserial PRIMARY KEY,
            user_id integer NOT NULL REFERENCES users(id),
            conversation_id integer NOT NULL REFERENCES conversations(id),
            message_id integer,
            role varchar(20) NOT NULL,
            content text NOT NULL,
            embedding halfvec(4000) NOT NULL,
            is_encrypted boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE OR REPLACE FUNCTION anila_gate2_classification_rank(level text)
        RETURNS integer LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        BEGIN
            CASE level
                WHEN '無機密' THEN RETURN 0;
                WHEN '營業秘密' THEN RETURN 1;
                WHEN '機密' THEN RETURN 2;
                WHEN '極機密' THEN RETURN 3;
                WHEN '絕對機密' THEN RETURN 4;
                ELSE RAISE EXCEPTION 'unknown classification %%', level;
            END CASE;
        END;
        $$;
        """
    )
    conn.exec_driver_sql(
        """
        INSERT INTO users(id, username) VALUES (1, 'owner'), (2, 'other');
        INSERT INTO ingestion_collections(id, created_by, classification_level)
        VALUES (10, 1, '機密');
        INSERT INTO security_compartments(id, code) VALUES (20, 'PROGRAM_PG');
        INSERT INTO clearance_grants(
            id, subject_user_id, max_classification_level, valid_from,
            expires_at, basis_ticket, issued_by_user_id
        ) VALUES (
            40, 1, '絕對機密', CURRENT_TIMESTAMP - interval '1 minute',
            CURRENT_TIMESTAMP + interval '1 hour', 'TEST-PG', 1
        );
        INSERT INTO clearance_grant_compartments(
            clearance_grant_id, compartment_id
        ) VALUES (40, 20);
        INSERT INTO collection_access_grants(
            clearance_grant_id, collection_id, membership_granted,
            need_to_know, basis_ticket, issued_by_user_id
        ) VALUES (40, 10, true, true, 'TEST-PG', 1);
        INSERT INTO collection_required_compartments(collection_id, compartment_id)
        VALUES (10, 20);
        INSERT INTO conversations(id, user_id, collection_id, classification_level)
        VALUES (30, 1, 10, '極機密'), (31, 1, NULL, '無機密');
        INSERT INTO user_facts(user_id, key, value, source_conversation_id)
        VALUES (1, 'known', 'known-source', 30),
               (1, 'unknown', 'missing-source', NULL);
        INSERT INTO conversation_memory_chunks(
            user_id, conversation_id, role, content, embedding
        ) VALUES (
            1, 30, 'user', 'legacy classified chunk',
            ('[' || repeat('0,', 3999) || '0]')::halfvec
        );
        """
    )


def test_r1_0016_backfill_and_runtime_triggers_are_fail_closed():
    assert DSN is not None
    engine = sa.create_engine(DSN)
    schema = f"gate2_memory_{uuid.uuid4().hex[:12]}"
    migration = _load_migration()
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}", public')
            _bootstrap(conn)
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                migration.upgrade()

            rows = conn.exec_driver_sql(
                """
                SELECT key, classification_level, classification_source
                FROM user_facts ORDER BY key
                """
            ).all()
            assert rows == [
                ("known", "極機密", "r1_0016:source_conversation"),
                ("unknown", "絕對機密", "r1_0016:legacy_unknown_source"),
            ]
            chunk = conn.exec_driver_sql(
                """
                SELECT classification_level, classification_source, is_encrypted
                FROM conversation_memory_chunks WHERE id = 1
                """
            ).one()
            assert chunk == ("極機密", "r1_0016:source_conversation", True)

            # Runtime cannot write a lower chunk than its source conversation.
            inserted = conn.exec_driver_sql(
                """
                INSERT INTO conversation_memory_chunks(
                    user_id, conversation_id, role, content, embedding,
                    classification_level, classification_source
                ) VALUES (
                    1, 30, 'assistant', 'attempted write-down',
                    ('[' || repeat('0,', 3999) || '0]')::halfvec,
                    '無機密', 'test:low'
                ) RETURNING id, classification_level, classification_source,
                            is_encrypted
                """
            ).one()
            assert inserted[1:] == (
                "極機密",
                "db_trigger:source_conversation",
                True,
            )

            # A direct classification downgrade is preserved at the old floor.
            conn.exec_driver_sql(
                "UPDATE conversation_memory_chunks SET classification_level='無機密' "
                "WHERE id=1"
            )
            assert conn.exec_driver_sql(
                "SELECT classification_level FROM conversation_memory_chunks WHERE id=1"
            ).scalar_one() == "極機密"

            # New facts must have a source, and the source owner must match.
            with pytest.raises(DBAPIError, match="source conversation"):
                with conn.begin_nested():
                    conn.exec_driver_sql(
                        """
                        INSERT INTO user_facts(
                            user_id, key, value, classification_level,
                            classification_source
                        ) VALUES (1, 'no-source', 'x', '無機密', 'test')
                        """
                    )
            with pytest.raises(DBAPIError, match="owner mismatch"):
                with conn.begin_nested():
                    conn.exec_driver_sql(
                        """
                        INSERT INTO user_facts(
                            user_id, key, value, source_conversation_id,
                            classification_level, classification_source
                        ) VALUES (2, 'wrong-owner', 'x', 30, '無機密', 'test')
                        """
                    )

            # Collection provenance adds both its floor and compartment.
            fact_id = conn.exec_driver_sql(
                "SELECT id FROM user_facts WHERE key='unknown'"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO user_fact_source_collections(fact_id, collection_id) "
                "VALUES (%s, 10)",
                (fact_id,),
            )
            assert conn.exec_driver_sql(
                "SELECT count(*) FROM user_fact_required_compartments "
                "WHERE fact_id=%s AND compartment_id=20",
                (fact_id,),
            ).scalar_one() == 1

            # Later source upgrades cascade; derived memory never goes stale-low.
            conn.exec_driver_sql(
                "UPDATE conversations SET classification_level='絕對機密' WHERE id=30"
            )
            assert conn.exec_driver_sql(
                "SELECT count(*) FROM conversation_memory_chunks "
                "WHERE conversation_id=30 AND classification_level='絕對機密'"
            ).scalar_one() == 2
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()


def test_runtime_for_share_blocks_concurrent_clearance_revoke():
    """A decision keeps grant/access rows stable across the embedding call."""

    assert DSN is not None
    from app.services import memory_service

    engine = sa.create_engine(DSN)
    schema = f"gate2_memory_lock_{uuid.uuid4().hex[:12]}"
    migration = _load_migration()
    try:
        with engine.begin() as setup:
            setup.exec_driver_sql(
                "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"
            )
            setup.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            setup.exec_driver_sql(f'SET search_path TO "{schema}", public')
            _bootstrap(setup)
            context = MigrationContext.configure(setup)
            with Operations.context(context):
                migration.upgrade()

        reader_connection = engine.connect()
        reader_transaction = reader_connection.begin()
        try:
            reader_connection.exec_driver_sql(
                f'SET search_path TO "{schema}", public'
            )
            reader_session = sessionmaker(bind=reader_connection)()
            grants = memory_service._load_active_memory_grants(
                reader_session, user_id=1
            )
            assert [grant.grant_id for grant in grants] == [40]

            # A management revoke needs an UPDATE lock on the grant row.  It
            # must wait behind the runtime decision's FOR SHARE, not race the
            # later embedding/search/injection use.
            with pytest.raises(DBAPIError, match="lock timeout|canceling statement"):
                with engine.begin() as mutator:
                    mutator.exec_driver_sql(
                        f'SET search_path TO "{schema}", public'
                    )
                    mutator.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                    mutator.exec_driver_sql(
                        "UPDATE clearance_grants "
                        "SET revoked_at=CURRENT_TIMESTAMP, revoked_by_user_id=1 "
                        "WHERE id=40"
                    )
        finally:
            reader_transaction.rollback()
            reader_connection.close()
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()

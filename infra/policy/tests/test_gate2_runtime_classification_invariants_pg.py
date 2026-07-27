"""True-PostgreSQL acceptance for the Gate 2 runtime classification floor."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from typing import Iterator
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[3]
CSP_DIR = ROOT / "services/csp"
TEST_EMBEDDING_FINGERPRINT = "sha256:" + ("b" * 64)


def _admin_url() -> URL:
    raw = os.environ.get("ANILA_GATE2_TEST_PG_ADMIN_URL")
    if not raw:
        pytest.skip("ANILA_GATE2_TEST_PG_ADMIN_URL is not set")
    url = make_url(raw)
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        pytest.fail("ANILA_GATE2_TEST_PG_ADMIN_URL must use PostgreSQL")
    return url


def _replace_database(url: URL, database: str) -> str:
    return url.set(database=database).render_as_string(hide_password=False)


@pytest.fixture
def runtime_database() -> Iterator[str]:
    admin = _admin_url()
    database = f"gate2_runtime_floor_{uuid.uuid4().hex[:10]}"
    maintenance = create_engine(
        _replace_database(admin, "postgres"),
        isolation_level="AUTOCOMMIT",
    )
    with maintenance.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{database}"'))
    database_url = _replace_database(admin, database)
    try:
        _run_alembic(database_url, "head")
        yield database_url
    finally:
        with maintenance.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        maintenance.dispose()


def _run_alembic(database_url: str, target: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "gate2-app-db",
            # Prepend rather than replace — see test_audit_logs_detail_trgm_pg.
            # Overwriting PYTHONPATH with "." only works where the packages are
            # installed editable into site-packages.
            "PYTHONPATH": os.pathsep.join(
                p for p in (".", os.environ.get("PYTHONPATH", "")) if p
            ),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=CSP_DIR,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade {target} failed:\n{result.stdout}\n{result.stderr}"
        )


def _seed_hierarchy(
    database_url: str,
    *,
    collection_level: str = "無機密",
    document_level: str = "無機密",
) -> tuple[int, int]:
    engine = create_engine(database_url)
    suffix = uuid.uuid4().hex[:10]
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                """
                INSERT INTO users (username, role, is_active, is_approved)
                VALUES (:username, 'owner', true, true)
                RETURNING id
                """
            ),
            {"username": f"gate2-runtime-{suffix}"},
        ).scalar_one()
        collection_id = conn.execute(
            text(
                """
                INSERT INTO ingestion_collections
                    (name, created_by, classification_level,
                     embedding_fingerprint)
                VALUES (:name, :user_id, :classification_level,
                        :embedding_fingerprint)
                RETURNING id
                """
            ),
            {
                "name": f"gate2-runtime-{suffix}",
                "user_id": user_id,
                "classification_level": collection_level,
                "embedding_fingerprint": TEST_EMBEDDING_FINGERPRINT,
            },
        ).scalar_one()
        document_id = conn.execute(
            text(
                """
                INSERT INTO ingestion_documents
                    (collection_id, filename, sha256, uploaded_by,
                     classification_level)
                VALUES (:collection_id, 'synthetic.txt', :sha256, :user_id,
                        :classification_level)
                RETURNING id
                """
            ),
            {
                "collection_id": collection_id,
                "sha256": suffix.ljust(64, "0"),
                "user_id": user_id,
                "classification_level": document_level,
            },
        ).scalar_one()
        generation_id = conn.execute(
            text(
                """
                INSERT INTO ingestion_document_generations
                    (document_id, collection_id, generation_number, status,
                     embedding_model, embedding_fingerprint, embedding_dim,
                     chunk_count, activated_at)
                VALUES (:document_id, :collection_id, 1, 'active',
                        'gate2-runtime-test', :embedding_fingerprint, 1536,
                        0, CURRENT_TIMESTAMP)
                RETURNING id
                """
            ),
            {
                "document_id": document_id,
                "collection_id": collection_id,
                "embedding_fingerprint": TEST_EMBEDDING_FINGERPRINT,
            },
        ).scalar_one()
        conn.execute(
            text(
                """
                UPDATE ingestion_documents
                   SET active_generation_id=:generation_id,
                       availability_status='available',
                       processing_stage='complete',
                       status='indexed'
                 WHERE id=:document_id
                """
            ),
            {"generation_id": generation_id, "document_id": document_id},
        )
    engine.dispose()
    return int(collection_id), int(document_id)


def _insert_chunk(
    conn,
    *,
    collection_id: int,
    document_id: int,
    chunk_key: str,
    classification_level: str = "無機密",
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO document_chunks
                (collection_id, document_id, generation_id,
                 is_active_generation, chunk_key, content,
                 classification_level)
            VALUES (:collection_id, :document_id,
                    (SELECT active_generation_id FROM ingestion_documents
                      WHERE id=:document_id), true, :chunk_key,
                    'synthetic runtime floor fixture', :classification_level)
            """
        ),
        {
            "collection_id": collection_id,
            "document_id": document_id,
            "chunk_key": chunk_key,
            "classification_level": classification_level,
        },
    )


def _approved_parent_declassification(
    conn,
    *,
    resource_type: str,
    resource_id: int,
    target_level: str,
) -> None:
    table = {
        "collection": "ingestion_collections",
        "document": "ingestion_documents",
    }[resource_type]
    previous_level = conn.execute(
        text(f"SELECT classification_level FROM {table} WHERE id=:id"),
        {"id": resource_id},
    ).scalar_one()
    event_id = conn.execute(
        text(
            """
            INSERT INTO classification_events
                (resource_type, resource_id, previous_level, new_level, reason)
            VALUES (:resource_type, :resource_id, :previous_level,
                    :new_level, 'declassification_copy')
            RETURNING id
            """
        ),
        {
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "previous_level": previous_level,
            "new_level": target_level,
        },
    ).scalar_one()
    conn.execute(
        text(
            f"UPDATE {table} "
            "SET classification_level=:target_level, "
            "classification_source='declassification_approved', "
            "classification_latched_at=clock_timestamp(), "
            "classification_event_id=:event_id WHERE id=:id"
        ),
        {
            "target_level": target_level,
            "event_id": event_id,
            "id": resource_id,
        },
    )


def _below_source_count(conn, collection_id: int) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                  FROM document_chunks ch
                  JOIN ingestion_documents d ON d.id = ch.document_id
                  JOIN ingestion_collections c ON c.id = ch.collection_id
                 WHERE ch.collection_id = :collection_id
                   AND (
                     anila_gate2_classification_rank(ch.classification_level) <
                     anila_gate2_classification_rank(d.classification_level)
                     OR
                     anila_gate2_classification_rank(ch.classification_level) <
                     anila_gate2_classification_rank(c.classification_level)
                   )
                """
            ),
            {"collection_id": collection_id},
        ).scalar_one()
    )


def test_direct_low_writes_are_raised_and_parent_upgrade_cascades(
    runtime_database: str,
) -> None:
    collection_id, document_id = _seed_hierarchy(
        runtime_database,
        collection_level="機密",
        document_level="無機密",
    )
    engine = create_engine(runtime_database)
    with engine.begin() as conn:
        # Document INSERT was below its collection and must already be raised.
        assert conn.execute(
            text("SELECT classification_level FROM ingestion_documents WHERE id=:id"),
            {"id": document_id},
        ).scalar_one() == "機密"

        _insert_chunk(
            conn,
            collection_id=collection_id,
            document_id=document_id,
            chunk_key="direct-low",
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM document_chunks "
                "WHERE chunk_key='direct-low'"
            )
        ).scalar_one() == "機密"

        conn.execute(
            text(
                "UPDATE document_chunks SET classification_level='無機密' "
                "WHERE chunk_key='direct-low'"
            )
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM document_chunks "
                "WHERE chunk_key='direct-low'"
            )
        ).scalar_one() == "機密"

        conn.execute(
            text(
                "UPDATE ingestion_documents SET classification_level='絕對機密' "
                "WHERE id=:id"
            ),
            {"id": document_id},
        )
        row = conn.execute(
            text(
                "SELECT classification_level, classification_source, "
                "classification_latched_at FROM document_chunks "
                "WHERE chunk_key='direct-low'"
            )
        ).mappings().one()
        assert row["classification_level"] == "絕對機密"
        assert row["classification_source"] == "document_upgrade_cascade"
        assert row["classification_latched_at"] is not None
        assert _below_source_count(conn, collection_id) == 0
    engine.dispose()


def test_parent_downgrade_never_lowers_existing_children(runtime_database: str) -> None:
    collection_id, document_id = _seed_hierarchy(
        runtime_database,
        collection_level="絕對機密",
        document_level="絕對機密",
    )
    engine = create_engine(runtime_database)
    with engine.begin() as conn:
        _insert_chunk(
            conn,
            collection_id=collection_id,
            document_id=document_id,
            chunk_key="declassification-copy",
            classification_level="絕對機密",
        )
        # An unapproved in-place downgrade is ignored at both parent levels.
        conn.execute(
            text(
                "UPDATE ingestion_collections SET classification_level='無機密' "
                "WHERE id=:id"
            ),
            {"id": collection_id},
        )
        conn.execute(
            text(
                "UPDATE ingestion_documents SET classification_level='無機密' "
                "WHERE id=:id"
            ),
            {"id": document_id},
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM ingestion_collections "
                "WHERE id=:id"
            ),
            {"id": collection_id},
        ).scalar_one() == "絕對機密"
        assert conn.execute(
            text(
                "SELECT classification_level FROM ingestion_documents "
                "WHERE id=:id"
            ),
            {"id": document_id},
        ).scalar_one() == "絕對機密"

        # A marker copied without the approved flow's fresh event/timestamp is
        # not sufficient authorization.
        conn.execute(
            text(
                "UPDATE ingestion_collections "
                "SET classification_level='無機密', "
                "classification_source='declassification_approved' "
                "WHERE id=:id"
            ),
            {"id": collection_id},
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM ingestion_collections "
                "WHERE id=:id"
            ),
            {"id": collection_id},
        ).scalar_one() == "絕對機密"

        # The existing approved flow changes marker, event and timestamp in
        # the same UPDATE; that parent may lower, but descendants do not.
        _approved_parent_declassification(
            conn,
            resource_type="collection",
            resource_id=collection_id,
            target_level="無機密",
        )
        _approved_parent_declassification(
            conn,
            resource_type="document",
            resource_id=document_id,
            target_level="無機密",
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM ingestion_collections "
                "WHERE id=:id"
            ),
            {"id": collection_id},
        ).scalar_one() == "無機密"
        assert conn.execute(
            text(
                "SELECT classification_level FROM ingestion_documents "
                "WHERE id=:id"
            ),
            {"id": document_id},
        ).scalar_one() == "無機密"
        assert conn.execute(
            text(
                "SELECT classification_level FROM document_chunks "
                "WHERE chunk_key='declassification-copy'"
            )
        ).scalar_one() == "絕對機密"
    engine.dispose()


def test_chunk_update_can_never_lower_its_existing_level(runtime_database: str) -> None:
    collection_id, document_id = _seed_hierarchy(runtime_database)
    engine = create_engine(runtime_database)
    with engine.begin() as conn:
        _insert_chunk(
            conn,
            collection_id=collection_id,
            document_id=document_id,
            chunk_key="monotonic-child",
            classification_level="絕對機密",
        )
        conn.execute(
            text(
                "UPDATE document_chunks SET classification_level='無機密' "
                "WHERE chunk_key='monotonic-child'"
            )
        )
        assert conn.execute(
            text(
                "SELECT classification_level FROM document_chunks "
                "WHERE chunk_key='monotonic-child'"
            )
        ).scalar_one() == "絕對機密"
    engine.dispose()


@pytest.mark.parametrize("parent_kind", ["collection", "document"])
def test_runtime_csp_app_role_cascades_through_force_rls(
    runtime_database: str,
    parent_kind: str,
) -> None:
    """The formal runtime role, not only the migration superuser, must work."""

    collection_id, document_id = _seed_hierarchy(runtime_database)
    engine = create_engine(runtime_database)
    chunk_key = f"csp-app-{parent_kind}"
    with engine.begin() as conn:
        _insert_chunk(
            conn,
            collection_id=collection_id,
            document_id=document_id,
            chunk_key=chunk_key,
        )

    target_table = (
        "ingestion_collections"
        if parent_kind == "collection"
        else "ingestion_documents"
    )
    target_id = collection_id if parent_kind == "collection" else document_id
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE csp_app"))
        conn.execute(
            text(
                f"UPDATE {target_table} "
                "SET classification_level='極機密' WHERE id=:id"
            ),
            {"id": target_id},
        )

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT classification_level, classification_source "
                "FROM document_chunks WHERE chunk_key=:chunk_key"
            ),
            {"chunk_key": chunk_key},
        ).mappings().one()
        assert row["classification_level"] == "極機密"
        assert row["classification_source"] in {
            "collection_upgrade_cascade",
            "document_upgrade_cascade",
        }
        assert _below_source_count(conn, collection_id) == 0
    engine.dispose()


@pytest.mark.parametrize("parent_kind", ["collection", "document"])
@pytest.mark.parametrize("upgrade_first", [False, True])
def test_concurrent_ingest_and_parent_upgrade_converge_for_both_commit_orders(
    runtime_database: str,
    parent_kind: str,
    upgrade_first: bool,
) -> None:
    collection_id, document_id = _seed_hierarchy(runtime_database)
    engine = create_engine(runtime_database, pool_size=4, max_overflow=0)
    first_statement_done = Event()
    second_statement_started = Event()
    release_first_commit = Event()
    chunk_key = f"race-{parent_kind}-{'upgrade' if upgrade_first else 'ingest'}-first"

    update_target = (
        "ingestion_collections" if parent_kind == "collection" else "ingestion_documents"
    )
    update_id = collection_id if parent_kind == "collection" else document_id

    def ingest() -> None:
        with engine.connect() as conn:
            transaction = conn.begin()
            if not upgrade_first:
                _insert_chunk(
                    conn,
                    collection_id=collection_id,
                    document_id=document_id,
                    chunk_key=chunk_key,
                )
                first_statement_done.set()
                assert release_first_commit.wait(15)
            else:
                second_statement_started.set()
                _insert_chunk(
                    conn,
                    collection_id=collection_id,
                    document_id=document_id,
                    chunk_key=chunk_key,
                )
            transaction.commit()

    def upgrade() -> None:
        with engine.connect() as conn:
            transaction = conn.begin()
            if upgrade_first:
                conn.execute(
                    text(
                        f"UPDATE {update_target} "
                        "SET classification_level='機密' WHERE id=:id"
                    ),
                    {"id": update_id},
                )
                first_statement_done.set()
                assert release_first_commit.wait(15)
            else:
                second_statement_started.set()
                conn.execute(
                    text(
                        f"UPDATE {update_target} "
                        "SET classification_level='機密' WHERE id=:id"
                    ),
                    {"id": update_id},
                )
            transaction.commit()

    first = upgrade if upgrade_first else ingest
    second = ingest if upgrade_first else upgrade
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first)
        assert first_statement_done.wait(15)
        second_future = executor.submit(second)
        assert second_statement_started.wait(15)
        release_first_commit.set()
        first_future.result(timeout=20)
        second_future.result(timeout=20)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT classification_level, classification_source "
                "FROM document_chunks WHERE chunk_key=:chunk_key"
            ),
            {"chunk_key": chunk_key},
        ).mappings().one()
        assert row["classification_level"] == "機密"
        assert row["classification_source"] in {
            "collection_upgrade_cascade",
            "document_upgrade_cascade",
            "ingestion_effective_trigger",
        }
        assert _below_source_count(conn, collection_id) == 0
    engine.dispose()

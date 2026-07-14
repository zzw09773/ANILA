"""True-PostgreSQL acceptance tests for Gate 2 G1b.

These tests create disposable databases on the supplied PostgreSQL cluster and
are skipped outside the dedicated CI/local integration job.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[3]
CSP_DIR = ROOT / "services/csp"
CHECK_PATH = ROOT / "infra/policy/gate2/check_classification_reconciliation.py"
TEST_EMBEDDING_FINGERPRINT = "sha256:" + ("a" * 64)
SPEC = importlib.util.spec_from_file_location(
    "gate2_classification_reconciliation_pg", CHECK_PATH
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


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
def disposable_database() -> Iterator[tuple[str, str]]:
    admin = _admin_url()
    database = f"gate2_reconcile_{uuid.uuid4().hex[:10]}"
    maintenance_url = _replace_database(admin, "postgres")
    maintenance = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        yield database, _replace_database(admin, database)
    finally:
        with maintenance.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        maintenance.dispose()


def _run_alembic(database_url: str, target: str, *, check: bool = True):
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "gate2-app-db",
            # This suite intentionally upgrades seeded legacy collections through
            # Gate 3.  Supply the same explicit weight-identity contract that a
            # real migration operator must provide.
            "EMBEDDING_MODEL_FINGERPRINT": TEST_EMBEDDING_FINGERPRINT,
            "PYTHONPATH": ".",
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
    if check and result.returncode != 0:
        pytest.fail(f"alembic upgrade {target} failed:\n{result.stdout}\n{result.stderr}")
    return result


def _seed_lower_hierarchy(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                """
                INSERT INTO users (username, role, is_active, is_approved)
                VALUES ('gate2-owner', 'owner', true, true)
                RETURNING id
                """
            )
        ).scalar_one()
        collection_id = conn.execute(
            text(
                """
                INSERT INTO ingestion_collections
                    (name, created_by, classification_level)
                VALUES ('gate2-collection', :user_id, '機密')
                RETURNING id
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
        document_id = conn.execute(
            text(
                """
                INSERT INTO ingestion_documents
                    (collection_id, filename, sha256, uploaded_by,
                     classification_level)
                VALUES (:collection_id, 'synthetic.txt', :sha256, :user_id,
                        '無機密')
                RETURNING id
                """
            ),
            {
                "collection_id": collection_id,
                "sha256": "a" * 64,
                "user_id": user_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO document_chunks
                    (collection_id, document_id, chunk_key, content,
                     classification_level)
                VALUES (:collection_id, :document_id, 'leaf-1',
                        'synthetic fixture', '無機密')
                """
            ),
            {"collection_id": collection_id, "document_id": document_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO model_registry
                    (name, display_name, model_type, endpoint_url,
                     classification_ceiling)
                VALUES ('gate2-model', 'Gate2 model', 'llm',
                        'https://model.invalid/v1', NULL)
                """
            )
        )
        artifact_id = conn.execute(
            text(
                """
                INSERT INTO artifacts
                    (artifact_type, title, status, owner_user_id,
                     current_version, classification_level, created_at, updated_at)
                VALUES ('report', 'Gate2 synthetic', 'completed', :user_id,
                        1, '機密', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO export_records
                    (artifact_id, target_classification_floor,
                     classification_level, decision, created_at)
                VALUES (:artifact_id, NULL, '機密', 'allow', CURRENT_TIMESTAMP)
                """
            ),
            {"artifact_id": artifact_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO service_launches
                    (launch_id, classification_level, mode, issued_at,
                     expires_at)
                VALUES ('launch-gate2', '極機密', 'new_tab',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO service_audit_callbacks
                    (launch_id, event_type, classification_level, received_at)
                VALUES ('launch-gate2', 'test.low', '機密', CURRENT_TIMESTAMP)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO service_audit_callbacks
                    (launch_id, event_type, classification_level, received_at)
                VALUES ('launch-gate2', 'test.event', NULL, CURRENT_TIMESTAMP)
                """
            )
        )
    engine.dispose()


def test_backfill_reconciles_all_rows_and_is_rerunnable(
    disposable_database: tuple[str, str],
) -> None:
    _database, database_url = disposable_database
    _run_alembic(database_url, "r1_0010")
    _seed_lower_hierarchy(database_url)
    _run_alembic(database_url, "head")

    report = checker.collect_report(database_url, sample_size=5)
    assert report["passed"] is True
    assert report["hierarchy"]["documents"]["below_collection"] == 0
    assert report["hierarchy"]["chunks"]["below_document"] == 0

    engine = create_engine(database_url)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == report["schema"]["alembic"]["source_heads"][0]
        assert conn.execute(
            text("SELECT classification_level FROM ingestion_documents")
        ).scalar_one() == "機密"
        assert conn.execute(
            text("SELECT classification_level FROM document_chunks")
        ).scalar_one() == "機密"
        assert conn.execute(
            text("SELECT classification_ceiling FROM model_registry")
        ).scalar_one() == "無機密"
        assert conn.execute(
            text("SELECT classification_level FROM service_audit_callbacks")
        ).scalars().all() == ["極機密", "極機密"]
        assert conn.execute(
            text("SELECT target_classification_floor FROM export_records")
        ).scalar_one() == "機密"
        required = {
            item["key"]: item for item in report["schema"]["required_columns"]
        }
        for key in (
            "agents.classification_ceiling",
            "model_registry.classification_ceiling",
            "registered_services.classification_ceiling",
            "service_audit_callbacks.classification_level",
            "export_records.target_classification_floor",
        ):
            assert required[key]["passed"] is True
    engine.dispose()

    # A downgrade removes only guards and never lowers data; a second upgrade
    # must converge to the identical compliant report.
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "gate2-app-db",
            "PYTHONPATH": ".",
        }
    )
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "r1_0010"],
        cwd=CSP_DIR,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert down.returncode == 0, down.stderr

    engine = create_engine(database_url)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "r1_0010"
        assert conn.execute(
            text("SELECT classification_level FROM ingestion_documents")
        ).scalar_one() == "機密"
        assert conn.execute(
            text("SELECT target_classification_floor FROM export_records")
        ).scalar_one() == "機密"
        assert conn.execute(
            text(
                """
                SELECT COUNT(*)
                  FROM pg_constraint
                 WHERE conname LIKE 'ck_%_gate2_level'
                """
            )
        ).scalar_one() == 0
        for table, column in (
            ("agents", "classification_ceiling"),
            ("model_registry", "classification_ceiling"),
            ("registered_services", "classification_ceiling"),
            ("service_audit_callbacks", "classification_level"),
            ("export_records", "target_classification_floor"),
        ):
            schema_row = conn.execute(
                text(
                    """
                    SELECT is_nullable, column_default
                      FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = :table
                       AND column_name = :column
                    """
                ),
                {"table": table, "column": column},
            ).mappings().one()
            assert schema_row["is_nullable"] == "YES"
            assert schema_row["column_default"] is None
    engine.dispose()

    _run_alembic(database_url, "head")
    assert checker.collect_report(database_url, sample_size=0)["passed"] is True


def test_checker_detects_legal_but_lower_child_mutation(
    disposable_database: tuple[str, str],
) -> None:
    _database, database_url = disposable_database
    _run_alembic(database_url, "r1_0010")
    _seed_lower_hierarchy(database_url)
    _run_alembic(database_url, "head")

    engine = create_engine(database_url)
    with engine.begin() as conn:
        # Mutation control: temporarily remove the live guard, then create a
        # value that remains inside the five-value CHECK domain but violates
        # the hierarchy.  The independent reconciliation checker must still
        # catch it instead of trusting the writer trigger.
        conn.execute(
            text(
                "ALTER TABLE ingestion_documents DISABLE TRIGGER "
                "trg_gate2_document_classification_update"
            )
        )
        conn.execute(
            text(
                """
                UPDATE ingestion_documents
                   SET classification_level = '無機密'
                """
            )
        )
    engine.dispose()

    report = checker.collect_report(database_url, sample_size=0)
    assert report["passed"] is False
    assert report["hierarchy"]["documents"]["below_collection"] == 1
    assert checker.main(
        ["--database-url", database_url, "--sample-size", "0"]
    ) == 1


def test_invalid_value_aborts_migration_without_partial_schema(
    disposable_database: tuple[str, str],
) -> None:
    _database, database_url = disposable_database
    _run_alembic(database_url, "r1_0010")
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO model_registry
                    (name, display_name, model_type, endpoint_url,
                     classification_ceiling)
                VALUES ('bad-model', 'Bad model', 'llm',
                        'https://model.invalid/v1', 'UNKNOWN')
                """
            )
        )
    engine.dispose()

    result = _run_alembic(database_url, "head", check=False)
    assert result.returncode != 0
    assert "classification preflight failed" in result.stderr

    engine = create_engine(database_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "r1_0010"
        )
        assert conn.execute(
            text(
                """
                SELECT COUNT(*)
                  FROM pg_constraint
                 WHERE conname =
                       'ck_model_registry_classification_ceiling_gate2_level'
                """
            )
        ).scalar_one() == 0
    engine.dispose()

"""True-PostgreSQL acceptance for r1_0021 ingestion generations."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError


ROOT = Path(__file__).resolve().parents[3]
CSP_DIR = ROOT / "services/csp"
FINGERPRINT = "sha256:" + ("a" * 64)


def _admin_url() -> URL:
    raw = os.environ.get("ANILA_GATE3_TEST_PG_ADMIN_URL") or os.environ.get(
        "ANILA_GATE2_TEST_PG_ADMIN_URL"
    )
    if not raw:
        pytest.skip("ANILA_GATE3_TEST_PG_ADMIN_URL is not set")
    return make_url(raw)


def _database_url(url: URL, database: str) -> str:
    return url.set(database=database).render_as_string(hide_password=False)


def _alembic(database_url: str, *arguments: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "gate3-app-db",
            "EMBEDDING_MODEL_FINGERPRINT": FINGERPRINT,
            # Prepend rather than replace — see the note in
            # test_audit_logs_detail_trgm_pg.py: overwriting PYTHONPATH with "."
            # makes this only work where the packages are installed editable.
            "PYTHONPATH": os.pathsep.join(
                p for p in (".", os.environ.get("PYTHONPATH", "")) if p
            ),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=CSP_DIR,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic {' '.join(arguments)} failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )


@pytest.fixture
def generation_database() -> Iterator[str]:
    admin = _admin_url()
    database = f"gate3_generation_{uuid.uuid4().hex[:10]}"
    maintenance = create_engine(
        _database_url(admin, "postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = _database_url(admin, database)
    try:
        _alembic(url, "upgrade", "r1_0020")
        yield url
    finally:
        with maintenance.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
        maintenance.dispose()


def _seed_legacy_rows(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        owner_id = int(
            connection.execute(
                text(
                    "INSERT INTO users(username,role,is_active,is_approved) "
                    "VALUES('gate3-owner','user',true,true) RETURNING id"
                )
            ).scalar_one()
        )
        collection_id = int(
            connection.execute(
                text(
                    "INSERT INTO ingestion_collections"
                    "(name,chunking_config,embedding_model,embedding_dim,status,created_by) "
                    "VALUES('legacy','{}','embed',4000,'active',:owner) RETURNING id"
                ),
                {"owner": owner_id},
            ).scalar_one()
        )
        zero_id = int(
            connection.execute(
                text(
                    "INSERT INTO ingestion_documents"
                    "(collection_id,filename,sha256,status,uploaded_by) "
                    "VALUES(:collection,'zero.txt',:sha,'indexed',:owner) RETURNING id"
                ),
                {"collection": collection_id, "sha": "0" * 64, "owner": owner_id},
            ).scalar_one()
        )
        chunked_id = int(
            connection.execute(
                text(
                    "INSERT INTO ingestion_documents"
                    "(collection_id,filename,sha256,status,uploaded_by) "
                    "VALUES(:collection,'chunked.txt',:sha,'pending',:owner) RETURNING id"
                ),
                {"collection": collection_id, "sha": "1" * 64, "owner": owner_id},
            ).scalar_one()
        )
        connection.execute(
            text(
                "INSERT INTO document_chunks"
                "(collection_id,document_id,chunk_key,content,metadata,chunk_type) "
                "VALUES(:collection,:document,'legacy-1','legacy','{}','heading')"
            ),
            {"collection": collection_id, "document": chunked_id},
        )
        assert zero_id != chunked_id
    engine.dispose()


def test_backfill_and_active_pointer_guards(generation_database: str) -> None:
    _seed_legacy_rows(generation_database)
    _alembic(generation_database, "upgrade", "r1_0021")
    engine = create_engine(generation_database)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT d.filename,d.availability_status,d.processing_stage,"
                "g.status,g.chunk_count FROM ingestion_documents d "
                "JOIN ingestion_document_generations g ON g.id=d.active_generation_id "
                "ORDER BY d.filename"
            )
        ).all()
        assert rows == [
            ("chunked.txt", "available", "complete", "active", 1),
            ("zero.txt", "available", "complete", "active", 0),
        ]
        assert connection.execute(
            text(
                "SELECT count(*) FROM document_chunks "
                "WHERE generation_id IS NOT NULL AND is_active_generation=true"
            )
        ).scalar_one() == 1
        assert connection.execute(
            text(
                "SELECT count(*) FROM ingestion_collections "
                "WHERE embedding_fingerprint=:fingerprint"
            ),
            {"fingerprint": FINGERPRINT},
        ).scalar_one() == 1

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_document_generations"
                "(document_id,collection_id,generation_number,status,embedding_model,"
                "embedding_fingerprint,embedding_dim,chunk_count) "
                "SELECT id,collection_id,2,'staging','embed',:fingerprint,4000,0 "
                "FROM ingestion_documents WHERE filename='zero.txt'"
            ),
            {"fingerprint": FINGERPRINT},
        )

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE ingestion_documents SET active_generation_id=("
                    "SELECT id FROM ingestion_document_generations "
                    "WHERE status='staging') WHERE filename='zero.txt'"
                )
            )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE ingestion_document_generations SET status='retired' "
                    "WHERE id=(SELECT active_generation_id FROM ingestion_documents "
                    "WHERE filename='zero.txt')"
                )
            )
    engine.dispose()

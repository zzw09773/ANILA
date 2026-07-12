"""True PostgreSQL acceptance for Gate 2 SourceSnapshot/Citation sealing."""

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


def _admin_url() -> URL:
    raw = os.environ.get("ANILA_GATE2_TEST_PG_ADMIN_URL")
    if not raw:
        pytest.skip("ANILA_GATE2_TEST_PG_ADMIN_URL is not set")
    return make_url(raw)


def _database_url(url: URL, database: str) -> str:
    return url.set(database=database).render_as_string(hide_password=False)


def _alembic(database_url: str, *arguments: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "gate2-app-db",
            "PYTHONPATH": ".",
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
def snapshot_database() -> Iterator[str]:
    admin = _admin_url()
    database = f"gate2_snapshot_{uuid.uuid4().hex[:10]}"
    maintenance = create_engine(
        _database_url(admin, "postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = _database_url(admin, database)
    try:
        _alembic(url, "upgrade", "head")
        yield url
    finally:
        with maintenance.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
        maintenance.dispose()


def _seed_unsealed_snapshot(database_url: str) -> tuple[int, int]:
    engine = create_engine(database_url)
    suffix = uuid.uuid4().hex
    with engine.begin() as connection:
        user_id = int(
            connection.execute(
                text(
                    """
                    INSERT INTO users (username, role, is_active, is_approved)
                    VALUES (:username, 'user', true, true)
                    RETURNING id
                    """
                ),
                {"username": f"snapshot-{suffix}"},
            ).scalar_one()
        )
        task_id = int(
            connection.execute(
                text(
                    """
                    INSERT INTO tasks
                        (title, task_type, requester_user_id, source_scope,
                         selected_collection_ids, classification_level)
                    VALUES ('snapshot test', 'query', :user_id, 'project',
                            '[101]'::jsonb, '機密')
                    RETURNING id
                    """
                ),
                {"user_id": user_id},
            ).scalar_one()
        )
        snapshot_id = int(
            connection.execute(
                text(
                    """
                    INSERT INTO source_snapshots
                        (task_id, origin, source_scope, collection_ids,
                         classification_level)
                    VALUES (:task_id, 'collection', 'project', '[101]'::jsonb,
                            '機密')
                    RETURNING id
                    """
                ),
                {"task_id": task_id},
            ).scalar_one()
        )
        connection.execute(
            text(
                "UPDATE tasks SET source_snapshot_id=:snapshot_id WHERE id=:task_id"
            ),
            {"snapshot_id": snapshot_id, "task_id": task_id},
        )
    engine.dispose()
    return task_id, snapshot_id


def _seal(connection, snapshot_id: int) -> None:
    connection.execute(
        text(
            """
            UPDATE source_snapshots
               SET document_ids='[202]'::jsonb,
                   chunk_ids='["303"]'::jsonb,
                   document_versions=CAST(:versions AS jsonb),
                   retrieval_queries='["query"]'::jsonb,
                   content_hash=:content_hash,
                   payload_ref=:payload_ref,
                   classification_level='機密',
                   classification_latched_at=CURRENT_TIMESTAMP,
                   classification_source='retrieval_snapshot_seal'
             WHERE id=:snapshot_id
            """
        ),
        {
            "versions": '{"202":"' + "a" * 64 + '"}',
            "content_hash": "b" * 64,
            "payload_ref": f"snapshot://{snapshot_id}",
            "snapshot_id": snapshot_id,
        },
    )


def test_valid_seal_and_citation_are_persisted_and_immutable(
    snapshot_database: str,
) -> None:
    _task_id, snapshot_id = _seed_unsealed_snapshot(snapshot_database)
    engine = create_engine(snapshot_database)
    with engine.begin() as connection:
        _seal(connection, snapshot_id)
        connection.execute(
            text(
                """
                INSERT INTO citations
                    (source_snapshot_id, document_id, chunk_id, used_by,
                     classification_level)
                VALUES (:snapshot_id, 202, '303', 'answer', '機密')
                """
            ),
            {"snapshot_id": snapshot_id},
        )

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE source_snapshots SET retrieval_queries='[\"changed\"]'::jsonb "
                    "WHERE id=:snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE citations SET quote_preview='changed' "
                    "WHERE source_snapshot_id=:snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            )
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT retrieval_queries FROM source_snapshots WHERE id=:id"
            ),
            {"id": snapshot_id},
        ).scalar_one() == ["query"]
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM citations WHERE source_snapshot_id=:id"
            ),
            {"id": snapshot_id},
        ).scalar_one() == 1
    engine.dispose()


@pytest.mark.parametrize(
    "document_id,chunk_id,classification_level,used_by",
    [
        (202, "missing", "機密", "answer"),
        (999, "303", "機密", "answer"),
        (202, "303", "絕對機密", "artifact"),
        (202, "303", "機密", "unknown"),
    ],
)
def test_citation_must_be_member_of_sealed_snapshot(
    snapshot_database: str,
    document_id: int,
    chunk_id: str,
    classification_level: str,
    used_by: str,
) -> None:
    _task_id, snapshot_id = _seed_unsealed_snapshot(snapshot_database)
    engine = create_engine(snapshot_database)
    with engine.begin() as connection:
        _seal(connection, snapshot_id)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO citations
                        (source_snapshot_id, document_id, chunk_id, used_by,
                         classification_level)
                    VALUES (:snapshot_id, :document_id, :chunk_id, :used_by,
                            :classification_level)
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "used_by": used_by,
                    "classification_level": classification_level,
                },
            )
    engine.dispose()


def test_invalid_hash_and_none_origin_cannot_seal(snapshot_database: str) -> None:
    _task_id, snapshot_id = _seed_unsealed_snapshot(snapshot_database)
    engine = create_engine(snapshot_database)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE source_snapshots SET content_hash='ABC', "
                    "document_versions='{}'::jsonb, payload_ref='snapshot://bad' "
                    "WHERE id=:id"
                ),
                {"id": snapshot_id},
            )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE source_snapshots SET origin='none', "
                    "content_hash=:hash, document_versions='{}'::jsonb, "
                    "payload_ref='snapshot://none' WHERE id=:id"
                ),
                {"hash": "c" * 64, "id": snapshot_id},
            )
    engine.dispose()


@pytest.mark.parametrize("score", ["NaN", "Infinity", "-Infinity"])
def test_citation_score_must_be_finite(
    snapshot_database: str,
    score: str,
) -> None:
    _task_id, snapshot_id = _seed_unsealed_snapshot(snapshot_database)
    engine = create_engine(snapshot_database)
    with engine.begin() as connection:
        _seal(connection, snapshot_id)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO citations
                        (source_snapshot_id, document_id, chunk_id, used_by,
                         classification_level, score)
                    VALUES (:snapshot_id, 202, '303', 'answer', '機密',
                            CAST(:score AS double precision))
                    """
                ),
                {"snapshot_id": snapshot_id, "score": score},
            )
    engine.dispose()


def test_downgrade_removes_guards_without_lowering_evidence(
    snapshot_database: str,
) -> None:
    _task_id, snapshot_id = _seed_unsealed_snapshot(snapshot_database)
    engine = create_engine(snapshot_database)
    with engine.begin() as connection:
        _seal(connection, snapshot_id)
    _alembic(snapshot_database, "downgrade", "r1_0014")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT content_hash FROM source_snapshots WHERE id=:id"),
            {"id": snapshot_id},
        ).scalar_one() == "b" * 64
        assert connection.execute(
            text(
                "SELECT COUNT(*) FROM pg_trigger "
                "WHERE tgname LIKE 'trg_gate2_source_snapshot%'"
            )
        ).scalar_one() == 0
    engine.dispose()

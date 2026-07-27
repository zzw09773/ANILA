# -*- coding: utf-8 -*-
"""Real-PG evidence: collection ``origin`` partitions CSP vs ANILALM inventory.

A1 — list isolation across governance (``/api/ingestion``) and personal
     (``/api/personal``) surfaces.
A2 — by-id + documents sub-resource on the wrong surface → 404 (not 403).

Why real PG: SQLite ``create_all`` does not exercise the alembic CHECK or
the empty-table NOT NULL add path in ``r1_0044``.

    ANILA_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55441/csp \\
        python -m pytest tests/test_collection_origin_surface_pg.py -q
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN / TEST_POSTGRES_URL not set —— 需要可丟棄的真 PostgreSQL",
)

CSP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = CSP_ROOT / "migrations"
_FINGERPRINT = "sha256:" + ("ab" * 32)

_CREATE_BODY = {
    "name": "origin-probe",
    "chunking_config": {"strategy": "hierarchical", "params": {}},
    "embedding_model": "nvidia/NV-embed-V2",
    "embedding_dim": 4000,
}


def _with_database(name: str) -> str:
    return make_url(_DSN).set(database=name).render_as_string(hide_password=False)


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _run_alembic(url: str, verb: str, target: str) -> None:
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = url
    try:
        getattr(command, verb)(_alembic_config(url), target)
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


@pytest.fixture()
def pg_client(monkeypatch):
    """Throwaway PG database migrated to head + TestClient bound to it."""
    from app.config import settings
    from app.database import get_db
    from app.main import app
    from app.models.user import User
    from app.utils.security import hash_password

    monkeypatch.setattr(settings, "EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)

    admin = create_engine(_DSN, isolation_level="AUTOCOMMIT")
    db_name = f"origin_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = _with_database(db_name)
    engine = None
    try:
        _run_alembic(url, "upgrade", "head")
        engine = create_engine(url)
        Session = sessionmaker(bind=engine)

        # Seed a user the login path can authenticate.
        db = Session()
        try:
            user = User(
                username=f"origin-{uuid.uuid4().hex[:8]}",
                role="user",
                hashed_password=hash_password("password"),
                is_active=True,
                is_approved=True,
            )
            db.add(user)
            db.commit()
            username = user.username
        finally:
            db.close()

        def override_get_db():
            session = Session()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app, raise_server_exceptions=True) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": username, "password": "password"},
            )
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            client.headers.update({"Authorization": f"Bearer {token}"})
            yield client
        app.dependency_overrides.clear()
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin.dispose()


def test_a1_list_isolation_by_surface(pg_client):
    """Governance list sees only csp-origin; personal list sees only anilalm."""
    client = pg_client

    csp_body = {**_CREATE_BODY, "name": "gov-only"}
    personal_body = {**_CREATE_BODY, "name": "kb-only"}

    r_csp = client.post("/api/ingestion/collections", json=csp_body)
    assert r_csp.status_code == 201, r_csp.text
    csp_id = r_csp.json()["id"]

    r_pers = client.post("/api/personal/collections", json=personal_body)
    assert r_pers.status_code == 201, r_pers.text
    personal_id = r_pers.json()["id"]
    assert csp_id != personal_id

    gov_list = client.get("/api/ingestion/collections")
    assert gov_list.status_code == 200, gov_list.text
    gov_ids = {row["id"] for row in gov_list.json()}
    assert gov_ids == {csp_id}

    pers_list = client.get("/api/personal/collections")
    assert pers_list.status_code == 200, pers_list.text
    pers_ids = {row["id"] for row in pers_list.json()}
    assert pers_ids == {personal_id}


def test_a2_cross_surface_by_id_and_documents_are_404(pg_client):
    """csp-origin collection is invisible on the personal surface (404)."""
    client = pg_client

    r_csp = client.post(
        "/api/ingestion/collections",
        json={**_CREATE_BODY, "name": "gov-for-404"},
    )
    assert r_csp.status_code == 201, r_csp.text
    csp_id = r_csp.json()["id"]

    # Direct by-id on the wrong surface → 404 (not 403).
    detail = client.get(f"/api/personal/collections/{csp_id}")
    assert detail.status_code == 404, detail.text
    assert "403" not in detail.text

    # Sub-resource that reuses the shared access helper → 404 too.
    docs = client.get(f"/api/personal/collections/{csp_id}/documents")
    assert docs.status_code == 404, docs.text

    # Symmetry: personal-origin invisible on governance surface.
    r_pers = client.post(
        "/api/personal/collections",
        json={**_CREATE_BODY, "name": "kb-for-404"},
    )
    assert r_pers.status_code == 201, r_pers.text
    personal_id = r_pers.json()["id"]

    assert client.get(f"/api/ingestion/collections/{personal_id}").status_code == 404
    assert (
        client.get(f"/api/ingestion/collections/{personal_id}/documents").status_code
        == 404
    )


def test_g1_conversation_rejects_cross_surface_collection(pg_client):
    """ANILALM conversation cannot bind a CSP-origin collection id."""
    client = pg_client

    r_csp = client.post(
        "/api/ingestion/collections",
        json={**_CREATE_BODY, "name": "csp-for-conv"},
    )
    assert r_csp.status_code == 201, r_csp.text
    csp_id = r_csp.json()["id"]

    r_pers = client.post(
        "/api/personal/collections",
        json={**_CREATE_BODY, "name": "kb-for-conv"},
    )
    assert r_pers.status_code == 201, r_pers.text
    personal_id = r_pers.json()["id"]

    # Red sequence from the review: create with CSP id must fail.
    bad = client.post(
        "/api/conversations",
        json={
            "title": "leak-probe",
            "origin": "anilalm",
            "collection_id": csp_id,
        },
    )
    assert bad.status_code == 404, bad.text

    # Same-origin still works.
    good = client.post(
        "/api/conversations",
        json={
            "title": "ok-probe",
            "origin": "anilalm",
            "collection_id": personal_id,
        },
    )
    assert good.status_code == 201, good.text
    assert good.json()["collection_id"] == personal_id

    listed = client.get(
        "/api/conversations",
        params={"origin": "anilalm", "collection_id": csp_id},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    own = client.get(
        "/api/conversations",
        params={"origin": "anilalm", "collection_id": personal_id},
    )
    assert own.status_code == 200, own.text
    assert all(row["collection_id"] == personal_id for row in own.json())
    assert all(row["collection_id"] != csp_id for row in own.json())


def test_g2_task_rejects_cross_surface_collection(pg_client):
    """POST /api/tasks rejects CSP-origin ids in selected_collection_ids."""
    client = pg_client

    r_csp = client.post(
        "/api/ingestion/collections",
        json={**_CREATE_BODY, "name": "csp-for-task"},
    )
    assert r_csp.status_code == 201, r_csp.text
    csp_id = r_csp.json()["id"]

    r_pers = client.post(
        "/api/personal/collections",
        json={**_CREATE_BODY, "name": "kb-for-task"},
    )
    assert r_pers.status_code == 201, r_pers.text
    personal_id = r_pers.json()["id"]

    bad = client.post(
        "/api/tasks",
        json={
            "title": "task-leak-probe",
            "task_type": "query",
            "source_scope": "project",
            "selected_collection_ids": [csp_id],
            "requested_output_type": "answer",
        },
    )
    assert bad.status_code == 404, bad.text

    good = client.post(
        "/api/tasks",
        json={
            "title": "task-ok-probe",
            "task_type": "query",
            "source_scope": "project",
            "selected_collection_ids": [personal_id],
            "requested_output_type": "answer",
        },
    )
    assert good.status_code == 201, good.text
    assert personal_id in good.json()["selected_collection_ids"]
    assert csp_id not in good.json()["selected_collection_ids"]

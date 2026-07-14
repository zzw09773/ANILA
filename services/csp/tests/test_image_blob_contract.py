"""Contract tests for ``GET /api/ingestion/images/{image_id}/blob``.

This endpoint streams the original image bytes (PNG/JPEG/...) so the
extracted ``anila-studio`` service can hydrate slide thumbnails without
touching the csp-db share-volume directly.

Pinned behaviour:
- 200 + raw bytes + correct Content-Type for owner-or-admin
- 404 when image_id doesn't exist
- 403 when caller doesn't own the collection containing the image
- Streaming response (NOT base64-in-JSON — large PDF rasters would
  blow heap budgets if buffered in memory)

The ``ingestion_images`` table is created via raw DDL by alembic
migration 0026 and has no SQLAlchemy model class. The test creates a
SQLite-compatible subset (no halfvec column) directly via Core
metadata so the endpoint's lookup query works under the in-memory test
DB.
"""
from __future__ import annotations

import os

# Same shape as other client-based tests — the global conftest only sets the
# SQLite DB; the lifespan-startup security gate needs an explicit opt-in.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.ingestion import image_blob
from app.api.ingestion.image_blob import router as image_blob_router
from app.main import app
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.auth_service import create_tokens

from tests.conftest import make_user


# Until router.py picks the new router up (separate main-thread change),
# tests register it explicitly. Idempotent: include_router is a no-op
# when the same path/router pair already exists, and inspecting routes
# avoids registering twice on repeat test runs.
def _ensure_blob_router_registered() -> None:
    paths = {getattr(r, "path", None) for r in app.routes}
    if "/api/ingestion/images/{image_id}/blob" not in paths:
        app.include_router(image_blob_router)


_ensure_blob_router_registered()


# ── Helpers ───────────────────────────────────────────────────────────────


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _create_images_table(db_engine) -> None:
    """SQLite-friendly DDL mirroring the production schema, sans halfvec
    columns and the HNSW index (only metadata lookup is exercised here)."""
    with db_engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS ingestion_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL,
                document_id INTEGER NOT NULL,
                image_id TEXT NOT NULL,
                page INTEGER,
                storage_path TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT 'image/png',
                alt_text TEXT,
                caption TEXT
            )
            """
        ))


def _insert_image(db, *, collection_id: int, document_id: int,
                  storage_path: str, mime: str = "image/png",
                  image_id_text: str = "img-x") -> int:
    """Insert a row and return its primary-key id."""
    result = db.execute(text(
        """
        INSERT INTO ingestion_images
            (collection_id, document_id, image_id, storage_path, mime, page, caption)
        VALUES (:cid, :did, :iid, :sp, :mime, :page, :caption)
        """
    ), {
        "cid": collection_id, "did": document_id, "iid": image_id_text,
        "sp": storage_path, "mime": mime, "page": 1, "caption": "test caption",
    })
    db.commit()
    # SQLite: lastrowid is on the underlying cursor
    pk = result.lastrowid
    assert pk is not None, "INSERT did not return an id"
    return pk


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def alice(db):
    return make_user(db, username="alice", role="user")


@pytest.fixture
def bob(db):
    return make_user(db, username="bob", role="user")


@pytest.fixture(autouse=True)
def _allow_document_data_clearance(monkeypatch):
    """Keep blob contract tests focused on route wiring and streaming.

    The canonical evaluator has its own policy matrix; the regression below
    overrides this stub to prove a denied live decision stops the byte sink.
    """
    monkeypatch.setattr(
        image_blob,
        "_require_document_data_clearance",
        lambda _db, *, user, document: None,
    )


@pytest.fixture
def alice_image(db, db_engine, alice, tmp_path, monkeypatch) -> tuple[int, Path]:
    """Insert one ingestion_image row owned by alice + write its blob
    to a temp INGESTION_UPLOAD_DIR. Returns (image_pk, abs_blob_path)."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(upload_dir))

    # Create the table (raw DDL — no model)
    _create_images_table(db_engine)

    coll = IngestionCollection(
        name="alice-collection",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)

    doc = IngestionDocument(
        collection_id=coll.id,
        filename="paper.pdf",
        sha256="a" * 64,
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    storage_path = "alice/img-1.png"
    blob_path = upload_dir / storage_path
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    raw_bytes = b"\x89PNG\r\n\x1a\n" + b"\xde\xad\xbe\xef" * 64
    blob_path.write_bytes(raw_bytes)

    pk = _insert_image(
        db, collection_id=coll.id, document_id=doc.id, storage_path=storage_path,
        mime="image/png",
    )
    return pk, blob_path


# ── 200 happy path ────────────────────────────────────────────────────────


def test_image_blob_200_streams_bytes(
    client: TestClient, db, alice, alice_image,
):
    image_pk, blob_path = alice_image
    expected = blob_path.read_bytes()

    resp = client.get(
        f"/api/ingestion/images/{image_pk}/blob",
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == expected
    # Streaming response so Content-Type tracks the row.mime field.
    assert resp.headers["content-type"].startswith("image/png")
    # Spec says StreamingResponse — must not be wrapped as JSON.
    assert resp.headers["content-type"] != "application/json"


def test_image_blob_content_type_tracks_mime(
    client: TestClient, db, alice, db_engine, tmp_path, monkeypatch,
):
    """A JPEG-stored row should serve ``image/jpeg`` not image/png."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(upload_dir))
    _create_images_table(db_engine)

    coll = IngestionCollection(
        name="c2",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)

    doc = IngestionDocument(
        collection_id=coll.id, filename="x.pdf", sha256="b" * 64, status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    sp = "alice/img-jpg.jpg"
    (upload_dir / sp).parent.mkdir(parents=True, exist_ok=True)
    (upload_dir / sp).write_bytes(b"\xff\xd8\xff\xe0fake")

    pk = _insert_image(
        db, collection_id=coll.id, document_id=doc.id,
        storage_path=sp, mime="image/jpeg",
    )

    resp = client.get(
        f"/api/ingestion/images/{pk}/blob",
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/jpeg")


# ── 404: image not found ──────────────────────────────────────────────────


def test_image_blob_404_when_image_missing(
    client: TestClient, db, db_engine, alice,
):
    _create_images_table(db_engine)
    resp = client.get(
        "/api/ingestion/images/999999/blob",
        headers=_bearer(alice),
    )
    assert resp.status_code == 404, resp.text


# ── 403: image belongs to another user's collection ───────────────────────


def test_image_blob_403_when_caller_not_owner(
    client: TestClient, db, alice, bob, alice_image,
):
    image_pk, _ = alice_image
    resp = client.get(
        f"/api/ingestion/images/{image_pk}/blob",
        headers=_bearer(bob),
    )
    assert resp.status_code == 403, resp.text


def test_image_blob_live_data_clearance_denial_precedes_stream(
    client: TestClient, db, alice, alice_image, monkeypatch,
):
    image_pk, _ = alice_image
    streamed = False

    def deny(_db, *, user, document):
        raise HTTPException(
            status_code=403,
            detail="clearance/compartment/need-to-know/collection grant insufficient",
        )

    def stream(**kwargs):
        nonlocal streamed
        streamed = True
        raise AssertionError("blob sink must not run after denied live authority")

    monkeypatch.setattr(image_blob, "_require_document_data_clearance", deny)
    monkeypatch.setattr(image_blob, "stream_scoped_image_blob", stream)

    response = client.get(
        f"/api/ingestion/images/{image_pk}/blob",
        headers=_bearer(alice),
    )

    assert response.status_code == 403
    assert streamed is False

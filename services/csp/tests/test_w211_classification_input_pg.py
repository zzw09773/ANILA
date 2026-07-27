# -*- coding: utf-8 -*-
"""真 PostgreSQL 證據:W2-11 上傳上調 latch + ClassificationEvent + CHECK。

為什麼一定要真 PG
-----------------
SQLite ``create_all`` 沒有 migration 的 CHECK 約束。五級字面量寫入路徑若只在
SQLite 測過,會重演「SQLite 全綠、真 PG 500」——本檔釘死:

1. 上傳宣告「機密」到「無機密」collection → collection latch 升級且寫
   ClassificationEvent(走既有 ``apply_classification``)。
2. ``classification_sampling_reviews`` 的五級 CHECK 拒絕非法字面量。

執行方式(需要可丟棄的 PostgreSQL,superuser DSN)::

    ANILA_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55441/csp \\
        python -m pytest tests/test_w211_classification_input_pg.py
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import UploadFile
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
_FINGERPRINT = "sha256:" + ("cd" * 32)
_REVISION = "r1_0042"


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
def pg_url():
    admin = create_engine(_DSN, isolation_level="AUTOCOMMIT")
    db_name = f"w211_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = _with_database(db_name)
    try:
        _run_alembic(url, "upgrade", "head")
        yield url
    finally:
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


@pytest.mark.asyncio
async def test_upload_declaring_confidential_latches_unclassified_collection(
    pg_url, tmp_path, monkeypatch
):
    """A1:上傳宣告機密 → collection latch 升級 + ClassificationEvent。"""
    from app.api.ingestion import documents
    from app.models.classification import ClassificationEvent
    from app.models.ingestion import IngestionCollection, IngestionDocument
    from app.models.user import User
    from app.utils.security import hash_password

    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    db = Session()
    documents._UPLOAD_DIR = str(tmp_path)
    monkeypatch.setenv("EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)

    try:
        user = User(
            username=f"w211pg-{uuid.uuid4().hex[:8]}",
            role="user",
            hashed_password=hash_password("password"),
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.flush()
        coll = IngestionCollection(
            name="pg-unclassified",
            chunking_config={"strategy": "fixed", "params": {}},
            embedding_model="test-embed",
            embedding_fingerprint=_FINGERPRINT,
            embedding_dim=4000,
            status="active",
            document_count=0,
            chunk_count=0,
            bytes_stored=0,
            created_by=user.id,
            classification_level="無機密",
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)
        collection_id = coll.id

        await documents.upload_document(
            collection_id,
            UploadFile(filename="secret.txt", file=BytesIO(b"classified body")),
            title="涉密附件",
            classification_level="機密",
            db=db,
            current_user=user,
        )

        db.expire_all()
        coll2 = db.get(IngestionCollection, collection_id)
        assert coll2 is not None
        assert coll2.classification_level == "機密"
        assert coll2.classification_source == "uploader_declared"
        assert coll2.classification_event_id is not None

        events = (
            db.query(ClassificationEvent)
            .filter(
                ClassificationEvent.resource_type == "collection",
                ClassificationEvent.resource_id == str(collection_id),
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].previous_level == "無機密"
        assert events[0].new_level == "機密"
        assert events[0].reason == "manual_admin"

        doc = (
            db.query(IngestionDocument)
            .filter(IngestionDocument.collection_id == collection_id)
            .one()
        )
        assert doc.classification_level == "機密"
        assert doc.classification_source == "uploader_declared"
    finally:
        db.close()
        engine.dispose()


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    import zipfile

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members:
            zf.writestr(name, payload)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_zip_declaring_above_floor_latches_collection(pg_url, tmp_path, monkeypatch):
    """Zip declared above floor → collection latches + one ClassificationEvent."""
    from app.api.ingestion import documents
    from app.models.classification import ClassificationEvent
    from app.models.ingestion import IngestionCollection, IngestionDocument
    from app.models.user import User
    from app.utils.security import hash_password

    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    db = Session()
    documents._UPLOAD_DIR = str(tmp_path)
    monkeypatch.setenv("EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)

    try:
        user = User(
            username=f"w211zip-{uuid.uuid4().hex[:8]}",
            role="user",
            hashed_password=hash_password("password"),
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.flush()
        coll = IngestionCollection(
            name="pg-zip-unclassified",
            chunking_config={"strategy": "fixed", "params": {}},
            embedding_model="test-embed",
            embedding_fingerprint=_FINGERPRINT,
            embedding_dim=4000,
            status="active",
            document_count=0,
            chunk_count=0,
            bytes_stored=0,
            created_by=user.id,
            classification_level="營業秘密",
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)
        collection_id = coll.id

        payload = _zip_bytes([("member.txt", b"zip classified body")])
        resp = await documents.upload_zip(
            collection_id,
            UploadFile(filename="bundle.zip", file=BytesIO(payload)),
            preserve_folder_structure=False,
            classification_level="極機密",
            db=db,
            current_user=user,
        )
        assert resp.enqueued == 1

        db.expire_all()
        coll2 = db.get(IngestionCollection, collection_id)
        assert coll2 is not None
        assert coll2.classification_level == "極機密"
        assert coll2.classification_source == "uploader_declared"
        assert coll2.classification_event_id is not None

        events = (
            db.query(ClassificationEvent)
            .filter(
                ClassificationEvent.resource_type == "collection",
                ClassificationEvent.resource_id == str(collection_id),
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].previous_level == "營業秘密"
        assert events[0].new_level == "極機密"

        docs = (
            db.query(IngestionDocument)
            .filter(IngestionDocument.collection_id == collection_id)
            .all()
        )
        assert len(docs) == 1
        assert docs[0].classification_level == "極機密"
        assert docs[0].classification_source == "uploader_declared"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_zip_declaring_below_floor_rejected_creates_zero_docs(
    pg_url, tmp_path, monkeypatch
):
    """Zip declared below floor → 400 and zero documents created."""
    from fastapi import HTTPException

    from app.api.ingestion import documents
    from app.models.ingestion import IngestionCollection, IngestionDocument
    from app.models.user import User
    from app.utils.security import hash_password

    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    db = Session()
    documents._UPLOAD_DIR = str(tmp_path)
    monkeypatch.setenv("EMBEDDING_MODEL_FINGERPRINT", _FINGERPRINT)

    try:
        user = User(
            username=f"w211zipb-{uuid.uuid4().hex[:8]}",
            role="user",
            hashed_password=hash_password("password"),
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.flush()
        coll = IngestionCollection(
            name="pg-zip-floor",
            chunking_config={"strategy": "fixed", "params": {}},
            embedding_model="test-embed",
            embedding_fingerprint=_FINGERPRINT,
            embedding_dim=4000,
            status="active",
            document_count=0,
            chunk_count=0,
            bytes_stored=0,
            created_by=user.id,
            classification_level="營業秘密",
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)
        collection_id = coll.id

        payload = _zip_bytes([("member.txt", b"should not land")])
        with pytest.raises(HTTPException) as excinfo:
            await documents.upload_zip(
                collection_id,
                UploadFile(filename="low.zip", file=BytesIO(payload)),
                preserve_folder_structure=False,
                classification_level="無機密",
                db=db,
                current_user=user,
            )
        assert excinfo.value.status_code == 400
        assert "低於" in str(excinfo.value.detail)

        db.expire_all()
        assert (
            db.query(IngestionDocument)
            .filter(IngestionDocument.collection_id == collection_id)
            .count()
            == 0
        )
        coll2 = db.get(IngestionCollection, collection_id)
        assert coll2 is not None
        assert coll2.classification_level == "營業秘密"
    finally:
        db.close()
        engine.dispose()


def test_sampling_review_check_rejects_unknown_level(pg_url):
    """Illegal classification literal must fail closed on real PG CHECK."""
    from app.models.ingestion import IngestionCollection, IngestionDocument
    from app.models.user import User
    from app.utils.security import hash_password

    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        user = User(
            username=f"w211chk-{uuid.uuid4().hex[:8]}",
            role="admin",
            hashed_password=hash_password("password"),
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.flush()
        coll = IngestionCollection(
            name="chk-kb",
            chunking_config={"strategy": "fixed", "params": {}},
            embedding_model="test-embed",
            embedding_fingerprint=_FINGERPRINT,
            embedding_dim=4000,
            status="active",
            document_count=0,
            chunk_count=0,
            bytes_stored=0,
            created_by=user.id,
            classification_level="無機密",
        )
        db.add(coll)
        db.flush()
        doc = IngestionDocument(
            collection_id=coll.id,
            filename="x.txt",
            title="x",
            sha256="b" * 64,
            mime_type="text/plain",
            bytes=1,
            storage_path="/tmp/x",
            status="indexed",
            chunk_count=0,
            uploaded_by=user.id,
            classification_level="無機密",
            classification_latched_at=datetime.now(timezone.utc),
            classification_source="collection_inherited",
        )
        db.add(doc)
        db.commit()

        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO classification_sampling_reviews "
                        "(document_id, collection_id, reviewer_user_id, "
                        "document_level_at_review, attested_level, outcome) "
                        "VALUES (:d, :c, :u, :lvl, :bad, 'confirmed')"
                    ),
                    {
                        "d": doc.id,
                        "c": coll.id,
                        "u": user.id,
                        "lvl": "無機密",
                        "bad": "秘密",
                    },
                )
    finally:
        db.close()
        engine.dispose()


def test_alembic_head_is_r1_0042_single(pg_url):
    from alembic.script import ScriptDirectory

    cfg = _alembic_config(pg_url)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ["r1_0042"]
    engine = create_engine(pg_url)
    with engine.connect() as conn:
        current = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert current == "r1_0042"
    engine.dispose()

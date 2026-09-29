"""平台版本與更新稽核。

版本來自 VERSION 檔。更新／回復寫進既有稽核表，操作者是主機帳號。
回復前要數得出更新之後新增的對話、訊息、文件。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.message import Message
from app.services.auth_service import create_tokens
from app.services import platform_release
from app.services.platform_release import (
    count_created_since,
    read_platform_version,
    record_platform_event,
    version_file_candidates,
)
from tests.conftest import make_user


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def test_version_file_is_what_the_dashboard_reads(tmp_path, monkeypatch):
    path = tmp_path / "VERSION"
    path.write_text("2026.09.29-4\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.services.platform_release.version_file_candidates",
        lambda: [path],
    )
    assert read_platform_version() == "2026.09.29-4"


def test_container_layout_reads_app_version_first(tmp_path, monkeypatch):
    app_version = tmp_path / "VERSION"
    app_version.write_text("2026.09.29-8\n", encoding="utf-8")
    repo = tmp_path / "repo"
    nested = repo / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    fake = nested / "platform_release.py"
    fake.write_text("", encoding="utf-8")
    (repo / "VERSION").write_text("from-repo\n", encoding="utf-8")
    monkeypatch.setattr(platform_release, "__file__", str(fake))
    monkeypatch.setattr(platform_release, "_CONTAINER_VERSION", app_version)
    assert version_file_candidates()[0] == app_version
    assert read_platform_version() == "2026.09.29-8"


def test_short_container_path_does_not_raise(tmp_path, monkeypatch):
    missing = tmp_path / "missing-version"
    monkeypatch.setattr(
        platform_release,
        "__file__",
        "/app/app/services/platform_release.py",
    )
    monkeypatch.setattr(platform_release, "_CONTAINER_VERSION", missing)
    paths = version_file_candidates()
    assert paths[0] == missing
    assert len(paths) == 1
    assert read_platform_version() == "dev"


def test_missing_version_file_is_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.platform_release.version_file_candidates",
        lambda: [tmp_path / "missing"],
    )
    assert read_platform_version() == "dev"


def test_logged_in_user_sees_version(client: TestClient, db: Session, monkeypatch, tmp_path):
    path = tmp_path / "VERSION"
    path.write_text("2026.09.29-1\n", encoding="utf-8")
    monkeypatch.setattr(
        "app.services.platform_release.version_file_candidates",
        lambda: [path],
    )
    user = make_user(db, username="ver-user", role="user")
    resp = client.get("/api/platform-version", headers=_bearer(user))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"version": "2026.09.29-1"}


def test_version_requires_login(client: TestClient):
    assert client.get("/api/platform-version").status_code == 401


def test_record_function_writes_the_audit_row(db: Session):
    row = record_platform_event(
        db,
        action="update",
        operator="c1147259",
        from_version="2026.09.29-1",
        to_version="2026.09.29-2",
        result="success",
    )
    assert row.actor_username == "c1147259"
    assert row.actor_user_id is None
    assert row.detail == "2026.09.29-1 → 2026.09.29-2"
    assert row.status == "success"
    assert row.resource_type == "platform_release"
    assert '"operator": "c1147259"' in row.metadata_json


def test_record_function_accepts_adopt(db: Session):
    row = record_platform_event(
        db,
        action="adopt",
        operator="c1147259",
        from_version="none",
        to_version="2026.09.29-1",
        result="success",
    )
    assert row.action == "platform_adopt"
    assert row.status == "success"


def test_platform_updates_post_is_gone(client: TestClient):
    resp = client.post(
        "/api/admin/platform-updates",
        json={
            "action": "update",
            "operator": "c1147259",
            "from_version": "2026.09.29-1",
            "to_version": "2026.09.29-2",
            "result": "success",
        },
    )
    assert resp.status_code == 404, resp.text


def test_record_function_truncates_operator_to_100(db: Session):
    row = record_platform_event(
        db,
        action="update",
        operator="u" * 150,
        from_version="2026.09.29-1",
        to_version="2026.09.29-2",
        result="success",
    )
    assert row.actor_username == "u" * 100


def test_cli_truncates_operator_before_opening_the_row(monkeypatch):
    seen = {}

    class _Db:
        def close(self):
            return None

    def fake_record(_db, **kwargs):
        seen.update(kwargs)

        class _Row:
            id = 1

        return _Row()

    monkeypatch.setattr("app.platform_release_cli.SessionLocal", lambda: _Db())
    monkeypatch.setattr("app.platform_release_cli.record_platform_event", fake_record)
    from app.platform_release_cli import main

    rc = main([
        "--action", "update",
        "--operator", "u" * 150,
        "--from-version", "2026.09.29-1",
        "--to-version", "2026.09.29-2",
        "--result", "success",
    ])
    assert rc == 0
    assert seen["operator"] == "u" * 100


def test_cli_blank_operator_does_not_open_the_database(monkeypatch):
    def boom():
        raise AssertionError("空白的操作者不該連資料庫")

    monkeypatch.setattr("app.platform_release_cli.SessionLocal", boom)
    from app.platform_release_cli import main

    assert main([
        "--action", "update",
        "--operator", "   ",
        "--from-version", "2026.09.29-1",
        "--to-version", "2026.09.29-2",
        "--result", "success",
    ]) == 2


def test_record_function_rejects_unknown_result(db: Session):
    try:
        record_platform_event(
            db,
            action="update",
            operator="c1147259",
            from_version="2026.09.29-1",
            to_version="2026.09.29-2",
            result="maybe",
        )
    except ValueError:
        return
    raise AssertionError("不正確的結果應該被拒絕")


def test_counts_only_rows_created_after_the_update(db: Session):
    user = make_user(db, username="rel-counter", role="user")
    before = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    after = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)
    since = datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    old_conv = Conversation(user_id=user.id, title="舊", created_at=before)
    new_conv = Conversation(user_id=user.id, title="新", created_at=after)
    db.add_all([old_conv, new_conv])
    db.flush()
    db.add(Message(conversation_id=old_conv.id, role="user", content="舊", created_at=before))
    db.add(Message(conversation_id=new_conv.id, role="user", content="新", created_at=after))
    collection = IngestionCollection(
        name="庫",
        chunking_config={},
        embedding_dim=8,
        created_by=user.id,
    )
    db.add(collection)
    db.flush()
    db.add(IngestionDocument(
        collection_id=collection.id,
        filename="old.txt",
        sha256="a" * 64,
        uploaded_at=before,
    ))
    db.add(IngestionDocument(
        collection_id=collection.id,
        filename="new.txt",
        sha256="b" * 64,
        uploaded_at=after,
    ))
    db.commit()
    counts = count_created_since(db, since)
    assert counts == {"conversations": 1, "messages": 1, "documents": 1}

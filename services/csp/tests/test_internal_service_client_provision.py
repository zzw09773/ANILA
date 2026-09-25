"""CSP provisions internal service credentials onto a private file.

Humans do not copy the plaintext. The provisioner creates the row and
the file, repeats as a no-op, rotates after the interval while the
previous token still verifies, and refuses to re-issue a revoked client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.database import Base, get_db
from app.main import app
from app.models.audit_log import AuditLog
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.services import agent_credential_service, internal_service_clients as isc
from app.services.internal_service_clients import (
    InternalServiceClientSpec,
    ProvisionOutcome,
    ensure_internal_service_clients,
    note_provision_outcomes,
    parse_internal_service_clients,
)
from app.services.service_token_envelope import (
    compute_lookup_hash,
    decode_service_token_envelope,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import make_user

ROUTER = InternalServiceClientSpec("router-primary", "router", "router test")


def _ensure(db, directory, **kwargs):
    kwargs.setdefault("specs", (ROUTER,))
    kwargs.setdefault("file_gid", None)
    kwargs.setdefault("rotate_after", timedelta(days=30))
    kwargs.setdefault("grace", timedelta(hours=24))
    return ensure_internal_service_clients(db, directory=directory, **kwargs)


def _token_file(directory):
    return directory / "router-primary.token"


def _plaintext(directory) -> str:
    return _token_file(directory).read_text(encoding="utf-8").strip()


def _assert_mode(path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o640, oct(mode)


def _assert_secret_absent(secret: str, caplog, db) -> None:
    assert secret
    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
        if isinstance(record.args, tuple):
            rendered = " ".join(str(arg) for arg in record.args)
            assert secret not in rendered
    for audit in db.query(AuditLog).all():
        blob = f"{audit.detail or ''} {audit.metadata_json or ''}"
        assert secret not in blob


def test_parse_list_is_data_driven_and_does_not_log_raw_values(caplog):
    caplog.set_level(logging.DEBUG)
    leaked = "csk-should-not-appear-in-logs"
    broken = (
        '[{"client_name":"router-primary","client_type":"nope","note":"'
        + leaked
        + '"}]'
    )
    assert parse_internal_service_clients(broken)[0].client_name == "router-primary"
    assert leaked not in caplog.text

    specs = parse_internal_service_clients(
        '[{"client_name":"ingestion-worker","client_type":"worker",'
        '"description":"pipeline"},'
        '{"client_name":"router-primary","client_type":"router"}]'
    )
    assert [(s.client_name, s.client_type) for s in specs] == [
        ("ingestion-worker", "worker"),
        ("router-primary", "router"),
    ]


def test_provision_creates_row_and_file_and_is_idempotent(db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    directory = tmp_path / "service-clients"

    first = _ensure(db, directory)
    assert [(item.client_name, item.action) for item in first] == [
        ("router-primary", "created")
    ]
    path = _token_file(directory)
    assert path.is_file()
    _assert_mode(path)
    token = _plaintext(directory)
    assert token.startswith("csk-")

    row = db.query(ServiceClient).filter_by(client_name="router-primary").one()
    assert row.client_type == "router"
    assert row.is_active is True
    assert row.is_legacy is False
    assert row.service_token_lookup_hash == compute_lookup_hash(token)
    assert db.query(ServiceClient).count() == 1

    second = _ensure(db, directory)
    assert second[0].action == "unchanged"
    assert _plaintext(directory) == token
    assert db.query(ServiceClient).count() == 1
    assert (
        db.query(ServiceClient).one().service_token_lookup_hash
        == compute_lookup_hash(token)
    )
    _assert_secret_absent(token, caplog, db)


def test_provision_rotates_after_interval_and_keeps_previous_token(db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    directory = tmp_path / "service-clients"
    _ensure(db, directory)
    original = _plaintext(directory)
    row = db.query(ServiceClient).filter_by(client_name="router-primary").one()
    row.service_token_issued_at = datetime.now(timezone.utc) - timedelta(days=31)
    row.service_token_rotated_at = None
    db.commit()

    rotated = _ensure(db, directory)
    assert rotated[0].action == "rotated"
    current = _plaintext(directory)
    assert current.startswith("csk-")
    assert current != original

    fresh = agent_credential_service.verify_service_token(db, token=current)
    assert fresh is not None
    assert fresh.used_previous_token is False

    previous = agent_credential_service.verify_service_token(db, token=original)
    assert previous is not None
    assert previous.used_previous_token is True
    _assert_secret_absent(original, caplog, db)
    _assert_secret_absent(current, caplog, db)


def test_revoked_client_is_not_reissued_and_file_is_removed(db, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    directory = tmp_path / "service-clients"
    directory.mkdir()
    stale = generate_service_token()
    (directory / "router-primary.token").write_text(stale + "\n", encoding="utf-8")
    row = ServiceClient(
        client_name="router-primary",
        client_type="router",
        service_token_envelope=encode_service_token_envelope(stale),
        service_token_lookup_hash=compute_lookup_hash(stale),
        service_token_issued_at=datetime.now(timezone.utc),
        is_legacy=False,
        is_active=False,
        revoked_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    original_hash = row.service_token_lookup_hash

    outcome = _ensure(db, directory)
    assert outcome[0].action == "revoked"
    assert not (directory / "router-primary.token").exists()
    again = db.query(ServiceClient).one()
    assert again.is_active is False
    assert again.service_token_lookup_hash == original_hash
    assert db.query(ServiceClient).count() == 1
    assert "refusing to re-issue" in caplog.text
    _assert_secret_absent(stale, caplog, db)

    outcome = _ensure(db, directory)
    assert outcome[0].action == "revoked"
    assert db.query(ServiceClient).filter_by(is_active=True).count() == 0


def test_configured_list_omitting_router_primary_still_provisions_it(
    db, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "ANILA_INTERNAL_SERVICE_CLIENTS",
        '[{"client_name":"ingestion-worker","client_type":"worker",'
        '"description":"pipeline"}]',
        raising=False,
    )
    specs = parse_internal_service_clients(settings.ANILA_INTERNAL_SERVICE_CLIENTS)
    names = [spec.client_name for spec in specs]
    assert "router-primary" in names
    assert "ingestion-worker" in names
    router = next(spec for spec in specs if spec.client_name == "router-primary")
    assert router.client_type == "router"
    assert names.count("router-primary") == 1

    directory = tmp_path / "service-clients"
    outcomes = ensure_internal_service_clients(
        db,
        directory=directory,
        file_gid=None,
        rotate_after=timedelta(days=30),
    )
    by_name = {item.client_name: item.action for item in outcomes}
    assert by_name["router-primary"] == "created"
    assert by_name["ingestion-worker"] == "created"
    assert (directory / "router-primary.token").is_file()
    assert (directory / "ingestion-worker.token").is_file()


def test_router_primary_configured_as_non_router_is_still_a_router(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "ANILA_INTERNAL_SERVICE_CLIENTS",
        '[{"client_name":"router-primary","client_type":"worker","file_gid":10002}]',
        raising=False,
    )
    specs = parse_internal_service_clients(settings.ANILA_INTERNAL_SERVICE_CLIENTS)
    routers = [spec for spec in specs if spec.client_name == "router-primary"]
    assert len(routers) == 1
    assert routers[0].client_type == "router"


def test_configured_router_primary_keeps_its_own_file_gid():
    specs = parse_internal_service_clients(
        '[{"client_name":"ingestion-worker","client_type":"worker","file_gid":10001},'
        '{"client_name":"router-primary","client_type":"router","file_gid":10002}]'
    )
    by_name = {spec.client_name: spec for spec in specs}
    assert set(by_name) == {"ingestion-worker", "router-primary"}
    assert by_name["router-primary"].client_type == "router"
    assert by_name["router-primary"].file_gid == 10002
    assert by_name["ingestion-worker"].file_gid == 10001


def test_configured_list_can_add_ingestion_worker(db, tmp_path):
    directory = tmp_path / "service-clients"
    specs = (
        ROUTER,
        InternalServiceClientSpec("ingestion-worker", "worker", "worker test"),
    )
    ensure_internal_service_clients(
        db,
        specs=specs,
        directory=directory,
        file_gid=None,
        rotate_after=timedelta(days=30),
    )
    names = {
        row.client_name: row.client_type for row in db.query(ServiceClient).all()
    }
    assert names == {"router-primary": "router", "ingestion-worker": "worker"}
    assert (directory / "ingestion-worker.token").is_file()
    assert (directory / "router-primary.token").is_file()


def _asgi_get(db_engine, path: str, headers: dict | None = None):
    """HTTP GET without Starlette's portal-backed TestClient.

    In this environment that client can block inside ``start_task_soon``
    before lifespan reports startup, so the 200/403 assertions never run.
    The router suite uses this same in-process transport for that reason.
    Lifespan stays off: background probes must not sit on startup.
    """
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    async def _call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            return await http.get(path, headers=headers or {})

    try:
        return asyncio.run(_call())
    finally:
        app.dependency_overrides.pop(get_db, None)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/service-clients",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 9),
        }
    )


def test_file_token_reaches_router_primary_and_legacy_token_is_rejected(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    """The credential CSP wrote is a router client; the fleet secret is not."""
    caplog.set_level(logging.DEBUG)
    from app.config import settings

    legacy = "legacy-shared-token-not-in-the-credential-file"
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", legacy, raising=False)

    model = ModelRegistry(
        name="prov-router-llm",
        display_name="prov-router-llm",
        model_type="llm",
        endpoint_url="https://llm.example.internal/v1",
        is_active=True,
        is_router_primary=True,
    )
    db.add(model)
    db.commit()

    directory = tmp_path / "service-clients"
    _ensure(db, directory)
    token = _plaintext(directory)
    _assert_secret_absent(token, caplog, db)
    assert token != legacy

    admitted = _asgi_get(
        db_engine,
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": token},
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["name"] == "prov-router-llm"

    rejected = _asgi_get(
        db_engine,
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": legacy},
    )
    assert rejected.status_code == 403, rejected.text
    assert token not in rejected.text


def test_seeded_legacy_row_is_replaced_and_rejected_immediately(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    """Migration 0027's fleet secret must not stay valid, even inside grace."""
    caplog.set_level(logging.DEBUG)
    from app.config import settings

    legacy = "legacy-shared-token-from-migration-0027"
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", legacy, raising=False)
    now = datetime.now(timezone.utc)
    db.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(legacy),
            service_token_lookup_hash=compute_lookup_hash(legacy),
            service_token_issued_at=now,
            is_legacy=True,
            is_active=True,
        )
    )
    db.add(
        ModelRegistry(
            name="legacy-router-llm",
            display_name="legacy-router-llm",
            model_type="llm",
            endpoint_url="https://llm.example.internal/v1",
            is_active=True,
            is_router_primary=True,
        )
    )
    db.commit()

    directory = tmp_path / "service-clients"
    outcome = _ensure(db, directory)
    assert outcome[0].action == "replaced_legacy"
    token = _plaintext(directory)
    assert token.startswith("csk-")
    assert token != legacy
    _assert_secret_absent(legacy, caplog, db)
    _assert_secret_absent(token, caplog, db)

    row = db.query(ServiceClient).filter_by(client_name="router-primary").one()
    assert row.is_legacy is False
    assert row.service_token_previous_lookup_hash is None
    assert agent_credential_service.verify_service_token(db, token=legacy) is None
    fresh = agent_credential_service.verify_service_token(db, token=token)
    assert fresh is not None
    assert fresh.used_previous_token is False

    admitted = _asgi_get(
        db_engine,
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": token},
    )
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["name"] == "legacy-router-llm"

    rejected = _asgi_get(
        db_engine,
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": legacy},
    )
    assert rejected.status_code == 403, rejected.text
    assert token not in rejected.text
    assert legacy not in rejected.text


def test_active_token_equal_to_legacy_env_is_replaced_without_grace(
    db, tmp_path, monkeypatch
):
    from app.config import settings

    legacy = "legacy-shared-token-still-the-active-hash"
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", legacy, raising=False)
    db.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(legacy),
            service_token_lookup_hash=compute_lookup_hash(legacy),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()
    directory = tmp_path / "service-clients"
    _ensure(db, directory)
    assert _plaintext(directory) != legacy
    assert agent_credential_service.verify_service_token(db, token=legacy) is None


def test_legacy_previous_token_is_cleared_without_waiting_for_grace(
    db, tmp_path, monkeypatch
):
    from app.config import settings

    legacy = "legacy-shared-token-kept-as-previous"
    current = generate_service_token()
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", legacy, raising=False)
    now = datetime.now(timezone.utc)
    db.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(current),
            service_token_lookup_hash=compute_lookup_hash(current),
            service_token_previous_envelope=encode_service_token_envelope(legacy),
            service_token_previous_lookup_hash=compute_lookup_hash(legacy),
            service_token_previous_expires_at=now + timedelta(hours=23),
            service_token_issued_at=now - timedelta(days=1),
            service_token_rotated_at=now - timedelta(hours=1),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()
    directory = tmp_path / "service-clients"
    outcome = _ensure(db, directory)
    assert outcome[0].action == "legacy_previous_cleared"
    assert _plaintext(directory) == current
    assert agent_credential_service.verify_service_token(db, token=legacy) is None
    assert (
        agent_credential_service.verify_service_token(db, token=current).used_previous_token
        is False
    )


def test_two_provisioners_rotate_once_and_file_matches_committed_row(
    tmp_path, monkeypatch
):
    """Two replicas, no shared threading lock. One rotation, file matches it."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'prov.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    original = generate_service_token()
    seed = Session()
    seed.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(original),
            service_token_lookup_hash=compute_lookup_hash(original),
            service_token_issued_at=datetime.now(timezone.utc) - timedelta(days=40),
            is_legacy=False,
            is_active=True,
        )
    )
    seed.commit()
    seed.close()

    # SQLite shared locks deadlock if both readers upgrade together.
    # Releasing the read after the snapshot lets both attempt the
    # compare-and-swap. PostgreSQL keeps FOR UPDATE instead; see below.
    real_lock = isc._lock_named

    def _read_then_release(db, name):
        row = real_lock(db, name)
        db.commit()
        return row

    barrier = threading.Barrier(2)
    real_generate = isc.generate_service_token

    def _generate_and_wait():
        token = real_generate()
        barrier.wait(timeout=5)
        return token

    monkeypatch.setattr(isc, "_LOCK", nullcontext())
    monkeypatch.setattr(isc, "_lock_named", _read_then_release)
    monkeypatch.setattr(isc, "generate_service_token", _generate_and_wait)

    directory = tmp_path / "service-clients"
    errors: list[BaseException] = []

    def _run():
        session = Session()
        try:
            ensure_internal_service_clients(
                session,
                specs=(ROUTER,),
                directory=directory,
                file_gid=None,
                rotate_after=timedelta(days=30),
                grace=timedelta(hours=24),
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_run), threading.Thread(target=_run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []

    check = Session()
    try:
        row = check.query(ServiceClient).one()
        active = decode_service_token_envelope(row.service_token_envelope)
        assert _plaintext(directory) == active
        assert active != original
        current = agent_credential_service.verify_service_token(check, token=active)
        assert current is not None and current.used_previous_token is False
        previous = agent_credential_service.verify_service_token(check, token=original)
        assert previous is not None and previous.used_previous_token is True
        rotations = (
            check.query(AuditLog)
            .filter(AuditLog.action == agent_credential_service.AUDIT_TOKEN_ROTATED)
            .count()
        )
        assert rotations == 1
    finally:
        check.close()
    engine.dispose()


def test_postgres_dialect_locks_the_client_row_for_update():
    from sqlalchemy.dialects.postgresql import dialect as pg_dialect

    statement = (
        select(ServiceClient)
        .where(ServiceClient.client_name == "router-primary")
        .with_for_update()
    )
    sql = str(statement.compile(dialect=pg_dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in sql
    assert "with_for_update" in Path(isc.__file__).read_text(encoding="utf-8")


def test_auto_provision_disabled_is_not_ready(db_engine, monkeypatch):
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "0")
    note_provision_outcomes([ProvisionOutcome("router-primary", "unchanged")])
    try:
        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert body["service_client_provisioning"] == "disabled"
        assert "service_client_provisioning_failed" not in body
    finally:
        note_provision_outcomes([])


def _delivery_failure_blob(result, raised) -> str:
    if raised is not None:
        return json.dumps(raised.detail, ensure_ascii=False)
    return result.model_dump_json()


def _assert_file_delivery_failed(result, raised) -> None:
    from fastapi import HTTPException

    assert isinstance(raised, HTTPException) or (
        result is not None and getattr(result, "delivery", None) == "delivery_error"
    )
    if isinstance(raised, HTTPException):
        assert raised.status_code >= 400
    blob = _delivery_failure_blob(result, raised)
    assert "delivery_error" in blob
    assert "delivery=file" not in blob
    if result is not None and raised is None:
        assert result.delivery != "file"


def test_admin_create_reports_failure_when_file_sync_fails(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    from fastapi import HTTPException

    from app.api.service_clients import CreateServiceClientRequest, create_client
    from app.config import settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    monkeypatch.setattr(settings, "ANILA_SERVICE_CLIENT_DIR", str(tmp_path), raising=False)
    note_provision_outcomes([])

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isc, "write_token_file", _boom)
    admin = make_user(db, username="svc-admin-create-fail", role="admin")
    try:
        raised = None
        result = None
        try:
            result = create_client(
                CreateServiceClientRequest(
                    client_name="router-primary", client_type="router"
                ),
                _request(),
                admin,
                db,
            )
        except HTTPException as exc:
            raised = exc
        _assert_file_delivery_failed(result, raised)
        row = db.query(ServiceClient).filter_by(client_name="router-primary").one()
        minted = decode_service_token_envelope(row.service_token_envelope)
        blob = _delivery_failure_blob(result, raised)
        assert minted not in blob
        assert minted not in caplog.text
        assert not (tmp_path / "router-primary.token").exists()

        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert body["service_client_provisioning"] == "degraded"
        assert "router-primary" in body["service_client_provisioning_failed"]
        assert minted not in response.text
    finally:
        note_provision_outcomes([])


def test_admin_rotate_reports_failure_when_file_sync_fails(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    from fastapi import HTTPException

    from app.api.service_clients import (
        CreateServiceClientRequest,
        RotateClientRequest,
        create_client,
        rotate_client,
    )
    from app.config import settings

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    monkeypatch.setattr(settings, "ANILA_SERVICE_CLIENT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(
        settings, "ANILA_SERVICE_CLIENT_FILE_GID", os.getgid(), raising=False
    )
    note_provision_outcomes([])
    admin = make_user(db, username="svc-admin-rotate-fail", role="admin")
    created = create_client(
        CreateServiceClientRequest(client_name="router-primary", client_type="router"),
        _request(),
        admin,
        db,
    )
    original = (tmp_path / "router-primary.token").read_text(encoding="utf-8").strip()
    assert created.delivery == "file"

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isc, "write_token_file", _boom)
    note_provision_outcomes([])
    try:
        raised = None
        result = None
        try:
            result = rotate_client(
                created.client.id,
                RotateClientRequest(),
                _request(),
                admin,
                db,
            )
        except HTTPException as exc:
            raised = exc
        _assert_file_delivery_failed(result, raised)
        row = db.query(ServiceClient).filter_by(client_name="router-primary").one()
        minted = decode_service_token_envelope(row.service_token_envelope)
        assert minted != original
        blob = _delivery_failure_blob(result, raised)
        assert minted not in blob
        assert original not in blob
        assert minted not in caplog.text
        on_disk = (tmp_path / "router-primary.token").read_text(encoding="utf-8").strip()
        assert on_disk == original

        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert body["service_client_provisioning"] == "degraded"
        assert "router-primary" in body["service_client_provisioning_failed"]
        assert minted not in response.text
    finally:
        note_provision_outcomes([])


def test_emergency_issue_static_still_returns_the_token_once(
    db, tmp_path, monkeypatch
):
    """File sync failure must not hide the one-time emergency plaintext."""
    from app.api.service_clients import (
        CreateServiceClientRequest,
        create_client,
        issue_static_for_client,
    )
    from app.config import settings

    monkeypatch.setattr(settings, "ANILA_SERVICE_CLIENT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(
        settings, "ANILA_SERVICE_CLIENT_FILE_GID", os.getgid(), raising=False
    )
    admin = make_user(db, username="svc-admin-emergency", role="admin")
    note_provision_outcomes([])
    created = create_client(
        CreateServiceClientRequest(client_name="router-primary", client_type="router"),
        _request(),
        admin,
        db,
    )

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isc, "write_token_file", _boom)
    try:
        emergency = issue_static_for_client(created.client.id, _request(), admin, db)
        assert emergency.delivery == "emergency_only"
        assert emergency.service_token
        assert emergency.service_token.startswith("csk-")
        assert emergency.service_token in emergency.model_dump_json()
    finally:
        note_provision_outcomes([])


def test_chmod_failure_fails_publication_and_degrades_readiness(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    caplog.set_level(logging.DEBUG)

    def _deny(*_args, **_kwargs):
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "chmod", _deny)
    try:
        directory = tmp_path / "service-clients"
        outcome = _ensure(db, directory)
        assert outcome[0].action == "error"
        minted = decode_service_token_envelope(
            db.query(ServiceClient).one().service_token_envelope
        )
        assert minted not in caplog.text
        note_provision_outcomes(outcome)
        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert body["service_client_provisioning"] == "degraded"
        assert "router-primary" in body["service_client_provisioning_failed"]
        assert minted not in response.text
        path = directory / "router-primary.token"
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        note_provision_outcomes([])


def test_chown_failure_fails_publication_and_degrades_readiness(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    caplog.set_level(logging.DEBUG)
    foreign_gid = 10002 if os.getgid() != 10002 else 10003

    def _deny(*_args, **_kwargs):
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "chown", _deny)
    monkeypatch.setattr(os, "fchown", _deny)
    try:
        directory = tmp_path / "service-clients"
        outcome = ensure_internal_service_clients(
            db,
            specs=(ROUTER,),
            directory=directory,
            file_gid=foreign_gid,
            rotate_after=timedelta(days=30),
        )
        assert outcome[0].action == "error"
        minted = decode_service_token_envelope(
            db.query(ServiceClient).one().service_token_envelope
        )
        assert minted not in caplog.text
        note_provision_outcomes(outcome)
        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert "router-primary" in body["service_client_provisioning_failed"]
        assert minted not in response.text
    finally:
        note_provision_outcomes([])


def test_publication_fails_when_resulting_mode_is_wrong(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    real_chmod = os.chmod

    def _lie(path, mode):
        # Only the token file is left at the wrong mode. The directory must
        # keep its execute bit or the assertion cannot stat the result.
        if str(path).endswith(".token"):
            real_chmod(path, 0o644)
            return
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _lie)
    secret = "csk-mode-check-secret"
    try:
        isc.write_token_file(tmp_path, "router-primary", secret)
    except OSError:
        pass
    else:
        raise AssertionError("wrong mode was reported as a successful publication")
    path = tmp_path / "router-primary.token"
    assert not path.exists() or stat.S_IMODE(path.stat().st_mode) == 0o640
    if path.exists():
        assert (stat.S_IMODE(path.stat().st_mode) & 0o007) == 0
    assert secret not in caplog.text


def test_publication_fails_when_resulting_gid_does_not_match(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    foreign_gid = 10002 if os.getgid() != 10002 else 10003
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(os, "fchown", lambda *_args, **_kwargs: None)
    secret = "csk-gid-check-secret"
    try:
        isc.write_token_file(tmp_path, "router-primary", secret, gid=foreign_gid)
    except OSError:
        pass
    else:
        raise AssertionError("wrong gid was reported as a successful publication")
    path = tmp_path / "router-primary.token"
    if path.exists():
        assert path.stat().st_gid == foreign_gid
    assert secret not in caplog.text


def test_matching_file_chmod_failure_is_not_republished_as_success(
    tmp_path, monkeypatch
):
    secret = "csk-already-published"
    isc.write_token_file(tmp_path, "router-primary", secret)
    path = tmp_path / "router-primary.token"
    _assert_mode(path)

    def _deny(*_args, **_kwargs):
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "chmod", _deny)
    try:
        isc.write_token_file(tmp_path, "router-primary", secret)
    except OSError:
        pass
    else:
        raise AssertionError("chmod failure on an existing token file reported success")


def test_provision_write_failure_marks_health_degraded(
    db, db_engine, tmp_path, monkeypatch, caplog
):
    from app.config import settings

    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    caplog.set_level(logging.DEBUG)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(isc, "write_token_file", _boom)
    try:
        directory = tmp_path / "service-clients"
        outcome = _ensure(db, directory)
        assert outcome[0].action == "error"
        minted = decode_service_token_envelope(
            db.query(ServiceClient).one().service_token_envelope
        )
        note_provision_outcomes(outcome)
        assert minted not in caplog.text
        assert "disk full" not in caplog.text
        assert "health is degraded" in caplog.text

        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["status"] == "degraded"
        assert body["service_client_provisioning"] == "degraded"
        assert "router-primary" in body["service_client_provisioning_failed"]
        assert minted not in response.text

        note_provision_outcomes(
            [ProvisionOutcome("router-primary", "unchanged")]
        )
        recovered = _asgi_get(db_engine, "/health")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "healthy"
        assert recovered.json()["service_client_provisioning"] == "ok"
    finally:
        note_provision_outcomes([])


def test_provision_db_error_marks_health_degraded(db, db_engine, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    caplog.set_level(logging.ERROR)

    def _db_down(_db, _name):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(isc, "_lock_named", _db_down)
    try:
        outcome = _ensure(db, tmp_path / "service-clients")
        assert outcome[0].action == "error"
        note_provision_outcomes(outcome)
        assert "could not be provisioned" in caplog.text
        response = _asgi_get(db_engine, "/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "database unavailable" not in response.text
    finally:
        note_provision_outcomes([])


def test_file_provisioned_clients_hide_plaintext_except_emergency_reissue(
    db, tmp_path, monkeypatch
):
    from app.api.service_clients import (
        CreateServiceClientRequest,
        RotateClientRequest,
        create_client,
        issue_static_for_client,
        rotate_client,
    )
    from app.config import settings

    monkeypatch.setattr(settings, "ANILA_SERVICE_CLIENT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(
        settings, "ANILA_SERVICE_CLIENT_FILE_GID", os.getgid(), raising=False
    )
    admin = make_user(db, username="svc-admin", role="admin")
    request = _request()

    created = create_client(
        CreateServiceClientRequest(client_name="router-primary", client_type="router"),
        request,
        admin,
        db,
    )
    assert created.delivery == "file"
    assert created.service_token is None
    created_json = created.model_dump_json()
    file_token = (tmp_path / "router-primary.token").read_text(encoding="utf-8").strip()
    assert file_token.startswith("csk-")
    assert file_token not in created_json

    manual = create_client(
        CreateServiceClientRequest(client_name="batch-worker", client_type="worker"),
        request,
        admin,
        db,
    )
    assert manual.delivery == "response"
    assert manual.service_token and manual.service_token.startswith("csk-")

    rotated = rotate_client(
        created.client.id,
        RotateClientRequest(),
        request,
        admin,
        db,
    )
    assert rotated.delivery == "file"
    assert rotated.service_token is None
    rotated_file = (tmp_path / "router-primary.token").read_text(encoding="utf-8").strip()
    assert rotated_file != file_token
    assert rotated_file not in rotated.model_dump_json()

    emergency = issue_static_for_client(created.client.id, request, admin, db)
    assert emergency.delivery == "emergency_only"
    assert emergency.service_token
    assert emergency.service_token.startswith("csk-")
    assert emergency.service_token in emergency.model_dump_json()
    assert (
        (tmp_path / "router-primary.token").read_text(encoding="utf-8").strip()
        == emergency.service_token
    )


def test_console_shows_plaintext_only_on_emergency_reissue():
    vue = (
        Path(__file__).resolve().parents[3]
        / "apps/csp-governance-ui/src/views/ServiceClientsView.vue"
    )
    text = vue.read_text(encoding="utf-8")
    assert "緊急專用" in text
    assert "delivery === 'file'" in text
    assert "issueStaticForClient" in text
    rotate = text.split("async function handleRotate", 1)[1].split(
        "async function handleEmergencyReissue", 1
    )[0]
    create = text.split("async function handleCreate", 1)[1].split(
        "async function handleRotate", 1
    )[0]
    assert "service_token" not in create
    assert "service_token" not in rotate

"""Fail-stop startup migration and readiness-state regression tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import main as main_module
from app.config import settings
from app.database import Base
from app.services import startup_migrations


def _fake_app():
    return SimpleNamespace(
        state=SimpleNamespace(
            ingestion_relay_task=SimpleNamespace(done=lambda: False)
        )
    )


def test_alembic_failure_is_fail_stop_and_never_calls_create_all(monkeypatch):
    application = _fake_app()
    monkeypatch.setattr(settings, "SKIP_STARTUP_MIGRATIONS", False)

    def migration_failed():
        raise ValueError("broken migration")

    def forbidden_fallback(*args, **kwargs):
        raise AssertionError("create_all fallback must never run")

    monkeypatch.setattr(main_module, "_run_alembic_upgrade", migration_failed)
    monkeypatch.setattr(Base.metadata, "create_all", forbidden_fallback)

    with pytest.raises(RuntimeError, match="migration"):
        main_module._apply_schema_migrations(application)

    assert application.state.migration_status == "failed"
    assert application.state.migration_error == "ValueError"


def test_success_marks_ready_only_after_legacy_migrations_finish(monkeypatch):
    application = _fake_app()
    calls: list[str] = []
    monkeypatch.setattr(settings, "SKIP_STARTUP_MIGRATIONS", False)
    monkeypatch.setattr(
        main_module, "_run_alembic_upgrade", lambda: calls.append("alembic")
    )
    monkeypatch.setattr(
        startup_migrations,
        "run_startup_migrations",
        lambda: calls.append("legacy"),
    )

    main_module._apply_schema_migrations(application)

    assert calls == ["alembic", "legacy"]
    assert application.state.migration_status == "succeeded"
    assert application.state.migration_error is None


def test_schema_backfill_failure_propagates_and_skips_legacy_check(monkeypatch):
    calls: list[str] = []

    def broken_backfill(_engine):
        calls.append("backfill")
        raise ValueError("schema backfill failed")

    monkeypatch.setattr(startup_migrations, "_ensure_schema_backfills", broken_backfill)
    monkeypatch.setattr(
        startup_migrations,
        "_maybe_migrate_legacy_sqlite",
        lambda: calls.append("legacy"),
    )

    with pytest.raises(ValueError, match="schema backfill failed"):
        startup_migrations.run_startup_migrations()

    assert calls == ["backfill"]


def test_unexpected_legacy_check_failure_propagates(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        startup_migrations,
        "_ensure_schema_backfills",
        lambda _engine: calls.append("backfill"),
    )

    def broken_legacy_check():
        calls.append("legacy")
        raise OSError("legacy database unreadable")

    monkeypatch.setattr(
        startup_migrations, "_maybe_migrate_legacy_sqlite", broken_legacy_check
    )

    with pytest.raises(OSError, match="legacy database unreadable"):
        startup_migrations.run_startup_migrations()

    assert calls == ["backfill", "legacy"]


def test_post_alembic_failure_marks_failed_and_readiness_stays_503(monkeypatch):
    application = _fake_app()
    monkeypatch.setattr(settings, "SKIP_STARTUP_MIGRATIONS", False)
    monkeypatch.setattr(main_module, "_run_alembic_upgrade", lambda: None)
    monkeypatch.setattr(
        startup_migrations,
        "run_startup_migrations",
        lambda: (_ for _ in ()).throw(RuntimeError("legacy check failed")),
    )

    with pytest.raises(RuntimeError, match="migration"):
        main_module._apply_schema_migrations(application)

    assert application.state.migration_status == "failed"
    assert application.state.migration_error == "RuntimeError"
    readiness = main_module._readiness_response(application)
    assert readiness.status_code == 503


def test_explicit_test_skip_never_invokes_formal_migrations(monkeypatch):
    application = _fake_app()
    monkeypatch.setattr(settings, "SKIP_STARTUP_MIGRATIONS", True)

    def must_not_run():
        raise AssertionError("formal migration ran for SQLite unit fixture")

    monkeypatch.setattr(main_module, "_run_alembic_upgrade", must_not_run)

    main_module._apply_schema_migrations(application)

    assert application.state.migration_status == "skipped"


def test_sqlite_testclient_reports_explicitly_skipped_migration_as_ready(client):
    health = client.get("/health")
    readiness = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["migration_status"] == "skipped"
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "migration_status": "skipped",
        "ingestion_outbox_relay": "running",
    }


@pytest.mark.parametrize(
    ("migration_status", "overall_status", "ready_status"),
    [
        ("pending", "starting", 503),
        ("running", "starting", 503),
        ("failed", "unhealthy", 503),
        ("succeeded", "healthy", 200),
        ("skipped", "healthy", 200),
    ],
)
def test_health_and_readiness_expose_migration_state(
    migration_status, overall_status, ready_status
):
    application = _fake_app()
    application.state.migration_status = migration_status
    application.state.migration_error = "RuntimeError" if migration_status == "failed" else None

    health = main_module._migration_health_payload(application)
    readiness = main_module._readiness_response(application)

    assert health["status"] == overall_status
    assert health["migration_status"] == migration_status
    assert readiness.status_code == ready_status


def test_readiness_fails_when_ingestion_outbox_relay_stops():
    application = _fake_app()
    application.state.migration_status = "succeeded"
    application.state.migration_error = None
    application.state.ingestion_relay_task = SimpleNamespace(done=lambda: True)

    readiness = main_module._readiness_response(application)

    assert readiness.status_code == 503
    assert b'"ingestion_outbox_relay":"stopped"' in readiness.body

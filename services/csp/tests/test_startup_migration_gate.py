"""Fail-stop startup migration and readiness-state regression tests."""
from __future__ import annotations

import ast
from pathlib import Path
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


def test_schema_verification_failure_propagates(monkeypatch):
    """W2-6:startup 只驗證、不自癒,而驗證失敗必須往外傳。

    以前這裡測的是 ``_ensure_schema_backfills`` / ``_maybe_migrate_legacy_sqlite``
    —— 那兩個函式(啟動時補 DDL、無條件探測 legacy SQLite)已隨 r1_0036 移除。
    """
    calls: list[str] = []

    def broken_verify(_bind):
        calls.append("verify")
        raise startup_migrations.SchemaOutOfDateError("schema 落後")

    monkeypatch.setattr(startup_migrations, "verify_schema", broken_verify)

    with pytest.raises(startup_migrations.SchemaOutOfDateError, match="schema 落後"):
        startup_migrations.run_startup_migrations()

    assert calls == ["verify"]


def test_startup_no_longer_performs_any_ddl_or_legacy_import():
    """收編之後啟動路徑不得再有 DDL、也不得再有 legacy SQLite 匯入的殘留。

    掃描時把 docstring 拿掉再看 —— 病灶的名字本來就會出現在解釋它為什麼被移除
    的散文裡。用 AST 找「有沒有真的呼叫 execute / 匯入 sqlite3」比 grep 原始碼
    精確。
    """
    source = Path(startup_migrations.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "execute" not in called, "啟動路徑不得再送任何 SQL/DDL"
    assert "exec_driver_sql" not in called

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sqlite3" not in imported
    assert "pathlib" not in imported  # legacy 檔案探測用的

    # 舊機制的識別字(散文裡不會出現這些精確名稱)
    for banned in (
        "LEGACY_SQLITE_DEFAULTS",
        "LEGACY_SQLITE_ENV",
        "MIGRATION_ORDER",
        "bulk_save_objects",
        "setval",
    ):
        assert banned not in source, f"startup 路徑不該再出現 {banned!r}"

    assert not hasattr(startup_migrations, "_ensure_schema_backfills")
    assert not hasattr(startup_migrations, "_maybe_migrate_legacy_sqlite")
    assert not hasattr(startup_migrations, "MIGRATION_ORDER")
    assert not hasattr(startup_migrations, "LegacyMigrationError")


def test_schema_gap_message_points_at_alembic(db_engine):
    """schema 落後時的訊息必須指向 alembic,而不是叫人重啟服務。"""
    with db_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alerts")

    with pytest.raises(startup_migrations.SchemaOutOfDateError) as excinfo:
        startup_migrations.verify_schema(db_engine)

    message = str(excinfo.value)
    assert "alembic upgrade head" in message
    assert "alerts" in message


def test_schema_verification_passes_on_orm_created_schema(db_engine):
    assert startup_migrations.collect_schema_gaps(db_engine) == []


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


def test_schema_out_of_date_refuses_to_start_and_keeps_ready_at_503(monkeypatch):
    """W2-6 ⑤:schema 落後的庫 → 拒啟動,且 /ready 維持 503。

    走真實的 ``SchemaOutOfDateError``(不是任意例外),證明新的「檢查 + 拒啟動」
    語意確實接到 ``_apply_schema_migrations`` 的 fail-stop 上。
    """
    application = _fake_app()
    monkeypatch.setattr(settings, "SKIP_STARTUP_MIGRATIONS", False)
    monkeypatch.setattr(main_module, "_run_alembic_upgrade", lambda: None)

    def out_of_date():
        raise startup_migrations.SchemaOutOfDateError(
            "資料庫 schema 落後於程式碼,拒絕啟動。缺少:欄位 alerts.fingerprint"
            " —— schema 權威是 alembic:請對該資料庫執行 `alembic upgrade head`"
        )

    monkeypatch.setattr(startup_migrations, "run_startup_migrations", out_of_date)

    with pytest.raises(RuntimeError, match="migration"):
        main_module._apply_schema_migrations(application)

    assert application.state.migration_status == "failed"
    assert application.state.migration_error == "SchemaOutOfDateError"
    assert main_module._readiness_response(application).status_code == 503


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

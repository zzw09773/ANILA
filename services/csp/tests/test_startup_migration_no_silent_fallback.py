"""Boot-time alembic: no silent create_all fallback (HIGH, 2026-08-24 ruling).

Unfixed baseline (measured 2026-08-26 on HEAD d55aa959, scratch sqlite,
non-empty users(id, username) + alembic_version=r1_0035, with
``_run_alembic_upgrade`` patched to ``raise Exception("3")`` then TestClient):

* log: ``WARNING - Alembic upgrade failed, falling back to create_all: 3``
* process came up; ``GET /health`` → 200 ``{"status":"healthy",...}``
* users still lacked ``email`` (create_all cannot add columns); alembic_version
  stayed ``r1_0035``

After the 2026-08-26 ① correction: empty Postgres also runs alembic
(``MIGRATION_DATABASE_URL`` / superuser). ``create_all`` is only the sqlite
pytest host (the test runner is the signal, not "alembic is missing"). Same failure must not boot, must not
return /health 200, must not call create_all.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import app.main as main_mod
from app.database import Base
from app.main import app
from app.services import ingestion_pool

SKIP_ENV = "ANILA_SKIP_STARTUP_MIGRATIONS"
_ENABLING = ["1", "true", "yes", "TRUE", "YES", "True", " 1 ", " true"]
_NOT_ENABLING = ["", "0", "false", "no", "off", "2", "on", "y", "skip"]
_CSP_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _never_touch_live_migration_url(monkeypatch):
    """MIGRATION_DATABASE_URL in a developer shell may point at :5433 (live)."""
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)


def _scratch_engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'scratch.db'}")


def _url_of(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _stale_nonempty_engine(tmp_path: Path):
    """The 2026-08-26 measured shape: users without email, stamped r1_0035."""
    engine = _scratch_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)")
        )
        conn.execute(
            text(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('r1_0035')")
        )
    return engine


def _users_columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("users")}


def _spy_create_all(monkeypatch) -> list:
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(Base.metadata, "create_all", _spy)
    return calls


def _enter_upgrade_path(monkeypatch, *, upgrade):
    """Force the production (Postgres) upgrade path from under pytest."""
    monkeypatch.setattr(main_mod, "_is_sqlite_pytest_host", lambda _bind: False)
    monkeypatch.setattr(main_mod, "_alembic_available", lambda: True)
    monkeypatch.setattr(main_mod, "_alembic_head_revision", lambda: "r1_0036")
    monkeypatch.setattr(main_mod, "_run_alembic_upgrade", upgrade)


def _innermost(exc: BaseException) -> BaseException:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _innermost(exc.exceptions[0])
    return exc


async def _skip_ingestion_pool():
    return None


def test_unfixed_fallback_string_is_gone_from_main_and_readme():
    main_src = Path(main_mod.__file__).read_text(encoding="utf-8")
    readme = (_CSP_ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (_CSP_ROOT / "README.en.md").read_text(encoding="utf-8")
    for blob in (main_src, readme, readme_en):
        assert "falling back to create_all" not in blob
        assert "失敗才 fallback `create_all`" not in blob
        assert "falling back to `create_all` only on failure" not in blob


def test_sqlite_pytest_host_guard_is_not_gated_on_alembic_missing():
    """F-A: 'this is the test host' must be asked before 'is alembic installed'."""
    src = Path(main_mod.__file__).read_text(encoding="utf-8")
    start = src.index("def _apply_startup_schema")
    end = src.index("\ndef setup_logging")
    fn = src[start:end]
    hatch = fn.index("_is_sqlite_pytest_host")
    alembic_gate = fn.index("if not _alembic_available()")
    create = fn.index("Base.metadata.create_all")
    assert hatch < alembic_gate
    assert create < alembic_gate
    assert "Base.metadata.create_all" not in fn[alembic_gate:]


def test_empty_database_with_alembic_runs_upgrade_not_create_all(
    tmp_path, monkeypatch
):
    engine = _scratch_engine(tmp_path)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url_of(engine))
    create_all_calls = _spy_create_all(monkeypatch)
    empty_checks: list = []
    monkeypatch.setattr(
        main_mod,
        "_database_is_empty",
        lambda bind: empty_checks.append(bind) or False,
    )
    upgrade_calls: list[str] = []
    _enter_upgrade_path(
        monkeypatch, upgrade=lambda: upgrade_calls.append("upgrade")
    )

    main_mod._apply_startup_schema(engine)

    assert upgrade_calls == ["upgrade"]
    assert create_all_calls == []
    assert empty_checks == []


def test_empty_sqlite_pytest_host_create_all_even_when_alembic_is_installed(
    tmp_path, monkeypatch
):
    """F-A: sqlite pytest must not run the PG-only chain just because alembic is importable."""
    engine = _scratch_engine(tmp_path)
    monkeypatch.setattr(main_mod, "_alembic_available", lambda: True)
    upgrade_calls: list[str] = []
    monkeypatch.setattr(
        main_mod, "_run_alembic_upgrade", lambda: upgrade_calls.append("upgrade")
    )
    main_mod._apply_startup_schema(engine)
    assert upgrade_calls == []
    assert "users" in inspect(engine).get_table_names()
    assert "email" in _users_columns(engine)


def test_nonempty_sqlite_pytest_host_does_not_upgrade_even_when_alembic_is_installed(
    tmp_path, monkeypatch
):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.setattr(main_mod, "_alembic_available", lambda: True)

    def boom():
        raise Exception("sqlite pytest host must not reach alembic upgrade")

    monkeypatch.setattr(main_mod, "_run_alembic_upgrade", boom)
    main_mod._apply_startup_schema(engine)
    assert _users_columns(engine) == {"id", "username"}


def test_nonempty_upgrade_failure_does_not_create_all(tmp_path, monkeypatch):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url_of(engine))
    create_all_calls = _spy_create_all(monkeypatch)

    def boom():
        raise Exception("3")

    _enter_upgrade_path(monkeypatch, upgrade=boom)

    with pytest.raises(main_mod.StartupMigrationError, match=r"upgrade failed|\b3\b"):
        main_mod._apply_startup_schema(engine)

    assert create_all_calls == []
    assert _users_columns(engine) == {"id", "username"}
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "r1_0035"


def test_nonempty_upgrade_failure_prevents_health_200(monkeypatch):
    def boom():
        raise Exception("3")

    _enter_upgrade_path(monkeypatch, upgrade=boom)

    started = False
    response = None
    caught: BaseException | None = None
    try:
        with TestClient(app) as client:
            started = True
            response = client.get("/health")
    except BaseException as exc:
        caught = exc

    assert started is False, (
        "unfixed behaviour: lifespan continued after upgrade failure"
    )
    assert response is None
    assert caught is not None
    inner = _innermost(caught)
    assert isinstance(inner, main_mod.StartupMigrationError) or (
        inner.__cause__ is not None
        and isinstance(inner.__cause__, main_mod.StartupMigrationError)
    )
    if response is not None:
        assert response.status_code != 200


def test_nonempty_upgrade_announces_current_to_head(tmp_path, monkeypatch, caplog):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", _url_of(engine))
    _enter_upgrade_path(monkeypatch, upgrade=lambda: None)

    with caplog.at_level(logging.WARNING, logger="csp.startup_migration"):
        main_mod._apply_startup_schema(engine)

    messages = [r.getMessage() for r in caplog.records]
    matching = [
        m
        for m in messages
        if "STARTUP MIGRATION" in m
        and "即將" in m
        and "r1_0035" in m
        and "r1_0036" in m
        and "→" in m
    ]
    assert matching, messages


def test_skip_flag_refuses_upgrade_and_create_all(tmp_path, monkeypatch):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.setenv(SKIP_ENV, "1")
    create_all_calls = _spy_create_all(monkeypatch)
    upgrade_calls: list[str] = []
    _enter_upgrade_path(monkeypatch, upgrade=lambda: upgrade_calls.append("upgrade"))

    main_mod._apply_startup_schema(engine)

    assert upgrade_calls == []
    assert create_all_calls == []
    assert _users_columns(engine) == {"id", "username"}


@pytest.mark.parametrize("value", _ENABLING)
def test_skip_flag_truthy_values_refuse(value, tmp_path, monkeypatch):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.setenv(SKIP_ENV, value)
    upgrade_calls: list[str] = []
    _enter_upgrade_path(monkeypatch, upgrade=lambda: upgrade_calls.append("upgrade"))
    main_mod._apply_startup_schema(engine)
    assert upgrade_calls == []


@pytest.mark.parametrize("value", _NOT_ENABLING)
def test_skip_flag_non_truthy_still_migrates(value, tmp_path, monkeypatch):
    engine = _stale_nonempty_engine(tmp_path)
    if value == "":
        monkeypatch.delenv(SKIP_ENV, raising=False)
    else:
        monkeypatch.setenv(SKIP_ENV, value)
    upgrade_calls: list[str] = []
    _enter_upgrade_path(monkeypatch, upgrade=lambda: upgrade_calls.append("upgrade"))
    main_mod._apply_startup_schema(engine)
    assert upgrade_calls == ["upgrade"]


def test_skip_flag_allows_service_to_start(monkeypatch):
    monkeypatch.setenv(SKIP_ENV, "1")
    monkeypatch.setattr(ingestion_pool, "open_pool", _skip_ingestion_pool)
    backfills: list[str] = []
    import app.services.startup_migrations as startup_migrations

    monkeypatch.setattr(
        startup_migrations,
        "run_startup_migrations",
        lambda: backfills.append("backfill"),
    )
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert response.headers.get("content-type", "").startswith("application/json")
    assert backfills == []


def test_postgres_without_alembic_fails_closed(tmp_path, monkeypatch):
    """Missing alembic outside the sqlite pytest host must not boot."""
    engine = _stale_nonempty_engine(tmp_path)
    create_all_calls = _spy_create_all(monkeypatch)
    monkeypatch.setattr(main_mod, "_alembic_available", lambda: False)
    monkeypatch.setattr(main_mod, "_is_sqlite_pytest_host", lambda _bind: False)

    with pytest.raises(main_mod.StartupMigrationError, match="alembic is not installed"):
        main_mod._apply_startup_schema(engine)

    assert create_all_calls == []


def test_default_is_to_migrate(tmp_path, monkeypatch):
    engine = _stale_nonempty_engine(tmp_path)
    monkeypatch.delenv(SKIP_ENV, raising=False)
    upgrade_calls: list[str] = []
    _enter_upgrade_path(monkeypatch, upgrade=lambda: upgrade_calls.append("upgrade"))
    main_mod._apply_startup_schema(engine)
    assert upgrade_calls == ["upgrade"]

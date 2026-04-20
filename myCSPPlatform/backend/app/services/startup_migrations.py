"""Startup-time migrations.

Handles:
1. Backfilling new columns on existing Postgres deployments (lightweight DDL).
2. One-shot migration from a legacy SQLite database (``data/csp.db``) into
   PostgreSQL, so upgrading from earlier SQLite-based deployments does not
   silently drop user/key/usage data.

The SQLite migration is opt-in via the ``LEGACY_SQLITE_PATH`` env var (or the
default ``data/csp.db`` location). If the legacy file exists but Postgres
already has data the migration aborts to avoid clobbering a live deployment.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.database import SessionLocal, engine
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.alert import Alert
from app.models.audit_log import AuditLog
from app.models.auth_provider import AuthProvider
from app.models.department import Department
from app.models.external_identity import ExternalIdentity
from app.models.model_registry import ModelRegistry
from app.models.platform_link import PlatformLink
from app.models.quota_policy import QuotaPolicy
from app.models.token_usage import TokenUsage
from app.models.user import User, UserModelPermission

logger = logging.getLogger(__name__)


LEGACY_SQLITE_ENV = "LEGACY_SQLITE_PATH"
LEGACY_SQLITE_DEFAULTS = [
    "/app/legacy-data/csp.db",
    "data/csp.db",
]


# Tables migrated from SQLite, in FK-safe order.
# Each entry: (sqlite_table, sqlalchemy_model)
MIGRATION_ORDER = [
    ("auth_providers", AuthProvider),
    ("departments", Department),
    ("users", User),
    ("external_identities", ExternalIdentity),
    ("model_registry", ModelRegistry),
    ("platform_links", PlatformLink),
    ("alerts", Alert),
    ("audit_logs", AuditLog),
    ("user_model_permissions", UserModelPermission),
    ("api_keys", ApiKey),
    ("api_key_model_permissions", ApiKeyModelPermission),
    ("token_usage", TokenUsage),
]


def run_startup_migrations() -> None:
    """Entry point called from the FastAPI lifespan hook."""
    try:
        _ensure_schema_backfills(engine)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"啟動 schema 回填失敗: {exc}")

    try:
        _maybe_migrate_legacy_sqlite()
    except LegacyMigrationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"SQLite → Postgres 遷移檢查失敗: {exc}")


class LegacyMigrationError(RuntimeError):
    """Raised when the legacy SQLite migration cannot safely proceed."""


def _ensure_schema_backfills(bind: Engine) -> None:
    """Backfill newly added columns/indexes for pre-existing schemas."""
    _ensure_user_column(
        bind,
        column="token_version",
        postgres_ddl="ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        generic_ddl="ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_user_column(
        bind,
        column="is_approved",
        postgres_ddl="ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE",
        generic_ddl="ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT 1",
    )
    _ensure_user_column(
        bind,
        column="department_id",
        postgres_ddl=(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id "
            "INTEGER REFERENCES departments(id) ON DELETE SET NULL"
        ),
        generic_ddl="ALTER TABLE users ADD COLUMN department_id INTEGER",
    )
    _ensure_token_usage_column(
        bind,
        column="department_id",
        postgres_ddl=(
            "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS department_id "
            "INTEGER REFERENCES departments(id)"
        ),
        generic_ddl="ALTER TABLE token_usage ADD COLUMN department_id INTEGER",
    )
    _ensure_postgres_index(
        bind,
        "CREATE INDEX IF NOT EXISTS idx_usage_department_time "
        "ON token_usage (department_id, request_timestamp)",
    )


def _ensure_user_column(bind: Engine, *, column: str, postgres_ddl: str, generic_ddl: str) -> None:
    inspector = inspect(bind)
    if not inspector.has_table("users"):
        return
    existing_cols = {c["name"] for c in inspector.get_columns("users")}
    if column in existing_cols:
        return

    ddl = postgres_ddl if bind.dialect.name == "postgresql" else generic_ddl
    with bind.begin() as conn:
        conn.execute(text(ddl))
    logger.info(f"已補上 users.{column} 欄位")


def _ensure_token_usage_column(
    bind: Engine,
    *,
    column: str,
    postgres_ddl: str,
    generic_ddl: str,
) -> None:
    inspector = inspect(bind)
    if not inspector.has_table("token_usage"):
        return
    existing_cols = {c["name"] for c in inspector.get_columns("token_usage")}
    if column in existing_cols:
        return

    ddl = postgres_ddl if bind.dialect.name == "postgresql" else generic_ddl
    with bind.begin() as conn:
        conn.execute(text(ddl))
    logger.info(f"已補上 token_usage.{column} 欄位")


def _ensure_postgres_index(bind: Engine, ddl: str) -> None:
    if bind.dialect.name != "postgresql":
        return
    with bind.begin() as conn:
        conn.execute(text(ddl))


def _resolve_legacy_sqlite_path() -> Path | None:
    explicit = os.environ.get(LEGACY_SQLITE_ENV)
    candidates = [explicit] if explicit else LEGACY_SQLITE_DEFAULTS
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def _maybe_migrate_legacy_sqlite() -> None:
    legacy_path = _resolve_legacy_sqlite_path()
    if legacy_path is None:
        return

    logger.info(f"偵測到舊版 SQLite 資料庫: {legacy_path}")

    session = SessionLocal()
    try:
        has_users = session.query(User.id).limit(1).first() is not None
        has_keys = session.query(ApiKey.id).limit(1).first() is not None
        has_usage = session.query(TokenUsage.id).limit(1).first() is not None
    finally:
        session.close()

    if has_users or has_keys or has_usage:
        raise LegacyMigrationError(
            f"偵測到舊版 SQLite ({legacy_path}) 但目標 PostgreSQL 已有資料；"
            "請先備份並手動決定保留哪一份後再啟動（或移除 LEGACY_SQLITE_PATH）。"
        )

    _copy_sqlite_to_postgres(legacy_path)


def _copy_sqlite_to_postgres(legacy_path: Path) -> None:
    src = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    migrated_counts: dict[str, int] = {}

    session = SessionLocal()
    try:
        for sqlite_table, model in MIGRATION_ORDER:
            try:
                rows = src.execute(f"SELECT * FROM {sqlite_table}").fetchall()
            except sqlite3.OperationalError:
                logger.info(f"舊資料庫無 {sqlite_table} 資料表，略過")
                continue

            if not rows:
                continue

            model_cols = {c.name for c in model.__table__.columns}
            objects = []
            for row in rows:
                payload = {k: row[k] for k in row.keys() if k in model_cols}
                objects.append(model(**payload))

            session.bulk_save_objects(objects, return_defaults=False)
            migrated_counts[sqlite_table] = len(objects)

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        src.close()

    _resync_postgres_sequences()

    if migrated_counts:
        logger.warning(
            "已從 SQLite 遷移資料到 PostgreSQL: "
            + ", ".join(f"{k}={v}" for k, v in migrated_counts.items())
        )
    else:
        logger.info("舊版 SQLite 檔案為空，未遷移任何資料")


def _resync_postgres_sequences() -> None:
    """Bump Postgres SERIAL sequences past the inserted IDs."""
    if engine.dialect.name != "postgresql":
        return
    stmts = []
    for _, model in MIGRATION_ORDER:
        table = model.__table__.name
        pk_cols = [c.name for c in model.__table__.primary_key.columns]
        if len(pk_cols) != 1:
            continue
        pk = pk_cols[0]
        stmts.append(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{pk}'), "
            f"COALESCE((SELECT MAX({pk}) FROM {table}), 1))"
        )
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
            except Exception as exc:  # pragma: no cover
                logger.warning(f"重設序列失敗: {stmt} ({exc})")

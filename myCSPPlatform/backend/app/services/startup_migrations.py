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
    """Backfill newly added columns/indexes for pre-existing schemas.

    The 0001 alembic baseline does not match the current SQLAlchemy models
    for users/model_registry/token_usage. Rather than rewriting history we
    run idempotent ``ADD COLUMN IF NOT EXISTS`` here so any fresh or
    previously-initialised Postgres volume self-heals at startup.
    """

    # --- users -----------------------------------------------------------
    _ensure_column(
        bind, "users", "token_version",
        postgres_ddl="ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        generic_ddl="ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        bind, "users", "is_approved",
        postgres_ddl="ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE",
        generic_ddl="ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT 1",
    )
    _ensure_column(
        bind, "users", "department_id",
        postgres_ddl=(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id "
            "INTEGER REFERENCES departments(id) ON DELETE SET NULL"
        ),
        generic_ddl="ALTER TABLE users ADD COLUMN department_id INTEGER",
    )
    _ensure_column(
        bind, "users", "updated_at",
        postgres_ddl="ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
        generic_ddl="ALTER TABLE users ADD COLUMN updated_at TIMESTAMP",
    )
    # Sprint 6 X / B2：local_password_disabled flag。defaults FALSE，所以
    # 既有 row 載入後仍能用本機密碼登入；admin 切換才禁用。
    _ensure_column(
        bind, "users", "local_password_disabled",
        postgres_ddl=(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
            "local_password_disabled BOOLEAN NOT NULL DEFAULT FALSE"
        ),
        generic_ddl=(
            "ALTER TABLE users ADD COLUMN local_password_disabled BOOLEAN NOT NULL DEFAULT 0"
        ),
    )

    # --- model_registry --------------------------------------------------
    _ensure_column(
        bind, "model_registry", "health_status",
        postgres_ddl=(
            "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS "
            "health_status VARCHAR(20) DEFAULT 'offline'"
        ),
        generic_ddl="ALTER TABLE model_registry ADD COLUMN health_status VARCHAR(20) DEFAULT 'offline'",
    )
    _ensure_column(
        bind, "model_registry", "health_checked_at",
        postgres_ddl="ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMP NULL",
        generic_ddl="ALTER TABLE model_registry ADD COLUMN health_checked_at TIMESTAMP",
    )
    _ensure_column(
        bind, "model_registry", "context_window",
        postgres_ddl="ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS context_window INTEGER NULL",
        generic_ddl="ALTER TABLE model_registry ADD COLUMN context_window INTEGER",
    )
    _ensure_column(
        bind, "model_registry", "base_model_id",
        postgres_ddl=(
            "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS base_model_id "
            "INTEGER REFERENCES model_registry(id)"
        ),
        generic_ddl="ALTER TABLE model_registry ADD COLUMN base_model_id INTEGER",
    )
    _ensure_column(
        bind, "model_registry", "updated_at",
        postgres_ddl="ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
        generic_ddl="ALTER TABLE model_registry ADD COLUMN updated_at TIMESTAMP",
    )

    # --- token_usage -----------------------------------------------------
    _ensure_column(
        bind, "token_usage", "department_id",
        postgres_ddl=(
            "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS department_id "
            "INTEGER REFERENCES departments(id)"
        ),
        generic_ddl="ALTER TABLE token_usage ADD COLUMN department_id INTEGER",
    )
    _ensure_column(
        bind, "token_usage", "request_timestamp",
        postgres_ddl=(
            "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_timestamp "
            "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ),
        generic_ddl=(
            "ALTER TABLE token_usage ADD COLUMN request_timestamp "
            "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ),
    )
    _ensure_column(
        bind, "token_usage", "request_duration_ms",
        postgres_ddl="ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_duration_ms INTEGER NULL",
        generic_ddl="ALTER TABLE token_usage ADD COLUMN request_duration_ms INTEGER",
    )

    # --- token_usage indexes (must come after request_timestamp exists) --
    for index_ddl in (
        "CREATE INDEX IF NOT EXISTS idx_usage_user_time ON token_usage (user_id, request_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_usage_department_time ON token_usage (department_id, request_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_usage_model_time ON token_usage (model_id, request_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON token_usage (request_timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_usage_apikey_time ON token_usage (api_key_id, request_timestamp)",
    ):
        _ensure_postgres_index(bind, index_ddl)

    # --- departments ----------------------------------------------------
    _ensure_column(
        bind, "departments", "updated_at",
        postgres_ddl="ALTER TABLE departments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
        generic_ddl="ALTER TABLE departments ADD COLUMN updated_at TIMESTAMP",
    )

    # --- auth_providers (0001 baseline omitted the OIDC/LDAP columns) ---
    for col_name, ddl_suffix in [
        ("default_role", "VARCHAR(20) NOT NULL DEFAULT 'user'"),
        ("button_text", "VARCHAR(100) NULL"),
        ("auto_create_users", "BOOLEAN NULL DEFAULT TRUE"),
        ("default_department_id", "INTEGER NULL"),
        ("updated_at", "TIMESTAMP NULL"),
        ("oidc_client_id", "VARCHAR(255) NULL"),
        # client_secret 改 envelope（base64(nonce|tag|ct)），需更寬欄位
        ("oidc_client_secret", "VARCHAR(2000) NULL"),
        ("oidc_issuer_url", "VARCHAR(255) NULL"),
        ("oidc_authorization_endpoint", "VARCHAR(255) NULL"),
        ("oidc_token_endpoint", "VARCHAR(255) NULL"),
        ("oidc_userinfo_endpoint", "VARCHAR(255) NULL"),
        ("oidc_scopes", "VARCHAR(255) NULL"),
        ("oidc_subject_claim", "VARCHAR(100) NULL"),
        ("oidc_username_claim", "VARCHAR(100) NULL"),
        ("oidc_email_claim", "VARCHAR(100) NULL"),
        # ldap_* 欄位由 0021 migration 直接 DROP；這裡不再 ensure 它們存在。
    ]:
        _ensure_column(
            bind, "auth_providers", col_name,
            postgres_ddl=f"ALTER TABLE auth_providers ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE auth_providers ADD COLUMN {col_name} {ddl_suffix}",
        )

    # --- external_identities -------------------------------------------
    for col_name, ddl_suffix in [
        ("external_subject", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("external_username", "VARCHAR(255) NULL"),
        ("external_email", "VARCHAR(255) NULL"),
        ("last_login_at", "TIMESTAMP NULL"),
    ]:
        _ensure_column(
            bind, "external_identities", col_name,
            postgres_ddl=f"ALTER TABLE external_identities ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE external_identities ADD COLUMN {col_name} {ddl_suffix}",
        )

    # --- api_keys ------------------------------------------------------
    for col_name, ddl_suffix in [
        ("expires_at", "TIMESTAMP NULL"),
        ("key_suffix", "VARCHAR(4) NOT NULL DEFAULT ''"),
    ]:
        _ensure_column(
            bind, "api_keys", col_name,
            postgres_ddl=f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE api_keys ADD COLUMN {col_name} {ddl_suffix}",
        )

    # --- alerts --------------------------------------------------------
    for col_name, ddl_suffix in [
        ("category", "VARCHAR(50) NOT NULL DEFAULT 'general'"),
        ("severity", "VARCHAR(20) NOT NULL DEFAULT 'info'"),
        ("status", "VARCHAR(20) NOT NULL DEFAULT 'open'"),
        ("fingerprint", "VARCHAR(200) NOT NULL DEFAULT ''"),
        ("source_type", "VARCHAR(50) NULL"),
        ("source_id", "VARCHAR(100) NULL"),
        ("first_seen_at", "TIMESTAMP NULL"),
        ("last_seen_at", "TIMESTAMP NULL"),
        ("acknowledged_at", "TIMESTAMP NULL"),
        ("acknowledged_by_user_id", "INTEGER NULL"),
        ("resolved_at", "TIMESTAMP NULL"),
        ("metadata_json", "TEXT NULL"),
    ]:
        _ensure_column(
            bind, "alerts", col_name,
            postgres_ddl=f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE alerts ADD COLUMN {col_name} {ddl_suffix}",
        )

    # --- audit_logs ----------------------------------------------------
    for col_name, ddl_suffix in [
        ("status", "VARCHAR(20) NOT NULL DEFAULT 'ok'"),
        ("actor_user_id", "INTEGER NULL"),
        ("actor_username", "VARCHAR(100) NULL"),
        ("ip_address", "VARCHAR(64) NULL"),
        ("metadata_json", "TEXT NULL"),
    ]:
        _ensure_column(
            bind, "audit_logs", col_name,
            postgres_ddl=f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE audit_logs ADD COLUMN {col_name} {ddl_suffix}",
        )
    # Align resource_id type with model (0001 baseline declared INTEGER;
    # model declares VARCHAR(100)). Pydantic ResponseValidationError was
    # firing on GET /api/audit-logs because PG returned ints.
    _ensure_column_type_varchar(
        bind, "audit_logs", "resource_id", length=100,
    )

    # --- platform_links ------------------------------------------------
    for col_name, ddl_suffix in [
        ("icon", "VARCHAR(50) NULL"),
        ("sort_order", "INTEGER NULL DEFAULT 0"),
    ]:
        _ensure_column(
            bind, "platform_links", col_name,
            postgres_ddl=f"ALTER TABLE platform_links ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
            generic_ddl=f"ALTER TABLE platform_links ADD COLUMN {col_name} {ddl_suffix}",
        )


def _ensure_column(
    bind: Engine,
    table: str,
    column: str,
    *,
    postgres_ddl: str,
    generic_ddl: str,
) -> None:
    """Idempotently add ``column`` to ``table`` when missing."""
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return
    existing_cols = {c["name"] for c in inspector.get_columns(table)}
    if column in existing_cols:
        return

    ddl = postgres_ddl if bind.dialect.name == "postgresql" else generic_ddl
    with bind.begin() as conn:
        conn.execute(text(ddl))
    logger.info(f"已補上 {table}.{column} 欄位")


def _ensure_postgres_index(bind: Engine, ddl: str) -> None:
    if bind.dialect.name != "postgresql":
        return
    with bind.begin() as conn:
        conn.execute(text(ddl))


def _ensure_column_type_varchar(
    bind: Engine, table: str, column: str, *, length: int
) -> None:
    """Convert ``table.column`` to VARCHAR(length) if currently a non-text type.

    Used to heal cases where an early baseline used INTEGER/BIGINT for a column
    the SQLAlchemy model now declares as String. Idempotent — no-op if already
    textual. Postgres-only.
    """
    if bind.dialect.name != "postgresql":
        return
    with bind.begin() as conn:
        current = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
        if current is None or current in ("character varying", "text"):
            return
        conn.execute(
            text(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({length}) "
                f"USING {column}::varchar"
            )
        )
    logger.info(f"已將 {table}.{column} 型別對齊為 VARCHAR({length})")


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

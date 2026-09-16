"""Startup-time migrations.

Handles:
1. Backfilling new columns on existing Postgres deployments (lightweight DDL).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from app.database import engine

logger = logging.getLogger(__name__)


def run_startup_migrations() -> None:
    """Entry point called from the FastAPI lifespan hook."""
    try:
        _ensure_schema_backfills(engine)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"啟動 schema 回填失敗: {exc}")


@contextmanager
def _migration_bind(runtime_bind: Engine) -> Iterator[Engine]:
    """Yield an engine holding the *migration* identity, if one is configured.

    P2.7: the audit tables are no longer owned by the runtime role, so the
    boot-time ``ALTER TABLE`` on ``audit_logs`` has to come from the same
    identity alembic uses (``MIGRATION_DATABASE_URL``, see
    ``migrations/env.py``). When that env var is absent — SQLite unit tests,
    single-role local setups — we fall back to the runtime engine so behaviour
    is unchanged from before. We deliberately do NOT try to detect "same DSN"
    and skip: comparing URLs is fiddly (SQLAlchemy masks the password in
    ``str(url)``) and opening one short-lived connection costs nothing.

    The engine is short-lived and disposed on exit: this runs once per boot,
    a persistent privileged pool would be a standing liability.
    """
    migration_url = os.environ.get("MIGRATION_DATABASE_URL", "").strip()
    if not migration_url:
        yield runtime_bind
        return

    privileged = create_engine(migration_url, pool_pre_ping=True, poolclass=NullPool)
    try:
        yield privileged
    finally:
        privileged.dispose()


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
        bind, "model_registry", "is_internal",
        postgres_ddl=(
            "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS is_internal "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        ),
        generic_ddl=(
            "ALTER TABLE model_registry ADD COLUMN is_internal "
            "BOOLEAN NOT NULL DEFAULT 0"
        ),
    )
    _ensure_column(
        bind, "model_registry", "updated_at",
        postgres_ddl="ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
        generic_ddl="ALTER TABLE model_registry ADD COLUMN updated_at TIMESTAMP",
    )
    _ensure_column(
        bind, "model_registry", "thinking_levels_supported",
        postgres_ddl=(
            "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS "
            "thinking_levels_supported JSONB NULL"
        ),
        generic_ddl="ALTER TABLE model_registry ADD COLUMN thinking_levels_supported JSON",
    )
    _ensure_column(
        bind, "model_registry", "thinking_user_selectable",
        postgres_ddl=(
            "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS "
            "thinking_user_selectable BOOLEAN NOT NULL DEFAULT TRUE"
        ),
        generic_ddl=(
            "ALTER TABLE model_registry ADD COLUMN thinking_user_selectable "
            "BOOLEAN NOT NULL DEFAULT 1"
        ),
    )

    # --- conversations ---------------------------------------------------
    _ensure_column(
        bind, "conversations", "thinking_tier",
        postgres_ddl=(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS "
            "thinking_tier VARCHAR(16) NULL"
        ),
        generic_ddl="ALTER TABLE conversations ADD COLUMN thinking_tier VARCHAR(16)",
    )
    # Compact 三欄：FK 由 Alembic r1_0042 負責（ondelete=SET NULL）；
    # startup 只是欄位後援，已有欄就不重複加、也不補 FK。
    _ensure_column(
        bind, "conversations", "compact_summary",
        postgres_ddl=(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS "
            "compact_summary TEXT NULL"
        ),
        generic_ddl="ALTER TABLE conversations ADD COLUMN compact_summary TEXT",
    )
    _ensure_column(
        bind, "conversations", "compact_boundary_message_id",
        postgres_ddl=(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS "
            "compact_boundary_message_id INTEGER "
            "REFERENCES messages(id) ON DELETE SET NULL"
        ),
        generic_ddl=(
            "ALTER TABLE conversations ADD COLUMN compact_boundary_message_id INTEGER"
        ),
    )
    _ensure_column(
        bind, "conversations", "compact_updated_at",
        postgres_ddl=(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS "
            "compact_updated_at TIMESTAMPTZ NULL"
        ),
        generic_ddl="ALTER TABLE conversations ADD COLUMN compact_updated_at TIMESTAMP",
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
    _ensure_column(
        bind, "token_usage", "reasoning_tokens",
        postgres_ddl="ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS reasoning_tokens INTEGER NULL",
        generic_ddl="ALTER TABLE token_usage ADD COLUMN reasoning_tokens INTEGER",
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
    # P1.1 三層樹：parent_id 自參照 FK（RESTRICT）＋偏索引
    # Named constraint matches model + r1_0009 (fk_departments_parent_id).
    _ensure_column(
        bind, "departments", "parent_id",
        postgres_ddl=(
            "ALTER TABLE departments ADD COLUMN IF NOT EXISTS parent_id "
            "INTEGER CONSTRAINT fk_departments_parent_id "
            "REFERENCES departments(id) ON DELETE RESTRICT"
        ),
        generic_ddl=(
            "ALTER TABLE departments ADD COLUMN parent_id INTEGER "
            "CONSTRAINT fk_departments_parent_id "
            "REFERENCES departments(id) ON DELETE RESTRICT"
        ),
    )
    _dept_parent_index_ddl = (
        "CREATE INDEX IF NOT EXISTS ix_departments_parent_id "
        "ON departments (parent_id) WHERE parent_id IS NOT NULL"
    )
    if bind.dialect.name == "postgresql":
        _ensure_postgres_index(bind, _dept_parent_index_ddl)
    else:
        # Generic path (e.g. SQLite): _ensure_postgres_index early-returns;
        # SQLite supports this partial-index form, so create it here.
        inspector = inspect(bind)
        if inspector.has_table("departments"):
            with bind.begin() as conn:
                conn.execute(text(_dept_parent_index_ddl))

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

    # --- attachments (P1.5 extraction + budget) -------------------------
    # Matches r1_0011; page_count is for prompt labels only (not budget).
    for col_name, pg_suffix, generic_suffix in [
        ("extracted_text", "TEXT NULL", "TEXT"),
        ("token_count", "INTEGER NULL", "INTEGER"),
        (
            "extract_status",
            "VARCHAR(20) NOT NULL DEFAULT 'pending'",
            "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        ),
        ("extract_error", "VARCHAR(500) NULL", "VARCHAR(500)"),
        ("extracted_at", "TIMESTAMP NULL", "TIMESTAMP"),
        ("page_count", "INTEGER NULL", "INTEGER"),
    ]:
        _ensure_column(
            bind, "attachments", col_name,
            postgres_ddl=(
                f"ALTER TABLE attachments ADD COLUMN IF NOT EXISTS "
                f"{col_name} {pg_suffix}"
            ),
            generic_ddl=(
                f"ALTER TABLE attachments ADD COLUMN {col_name} {generic_suffix}"
            ),
        )

    # --- audit_logs (deliberately LAST) ---------------------------------
    # Kept at the end of this function on purpose: it is the only block that
    # can fail for a *permissions* reason, and ``run_startup_migrations``
    # swallows the exception into one generic error line. If it sat in the
    # middle, a failure here would silently skip every later backfill and the
    # platform would boot with columns quietly missing — "boots fine, breaks
    # later", exactly the shape this package exists to remove.
    # P2.7: r1_0027 took ownership of the audit tables away from ``csp_app``
    # so a leaked runtime credential can no longer rewrite audit history.
    # ALTER TABLE requires ownership, so this one block runs on a SECOND
    # engine holding the *migration* identity — everything above stays on
    # the runtime engine (smallest possible blast radius; no non-audit table
    # loses anything). r1_0027 also performs these same DDL steps, so on a
    # fresh database this block is a no-op; it exists only so a pre-r1_0027
    # Postgres volume still self-heals at boot.
    with _migration_bind(bind) as audit_bind:
        for col_name, ddl_suffix in [
            ("status", "VARCHAR(20) NOT NULL DEFAULT 'ok'"),
            ("actor_user_id", "INTEGER NULL"),
            ("actor_username", "VARCHAR(100) NULL"),
            ("ip_address", "VARCHAR(64) NULL"),
            ("metadata_json", "TEXT NULL"),
        ]:
            _ensure_column(
                audit_bind, "audit_logs", col_name,
                postgres_ddl=f"ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS {col_name} {ddl_suffix}",
                generic_ddl=f"ALTER TABLE audit_logs ADD COLUMN {col_name} {ddl_suffix}",
            )
        # Align resource_id type with model (0001 baseline declared INTEGER;
        # model declares VARCHAR(100)). Pydantic ResponseValidationError was
        # firing on GET /api/audit-logs because PG returned ints.
        _ensure_column_type_varchar(
            audit_bind, "audit_logs", "resource_id", length=100,
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

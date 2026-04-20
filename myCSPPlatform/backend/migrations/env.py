"""Alembic environment configuration for myCSPPlatform.

Reads DATABASE_URL from the app config so migrations use the same DB
as the running service. All SQLAlchemy models are imported here so
that `autogenerate` can detect schema differences.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make sure the app package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import settings
from app.database import Base

# ── Import every model so metadata is fully populated ────────────────────────
import app.models.alert           # noqa: F401
import app.models.api_key         # noqa: F401
import app.models.agent           # noqa: F401
import app.models.audit_log       # noqa: F401
import app.models.auth_provider   # noqa: F401
import app.models.department      # noqa: F401
import app.models.external_identity  # noqa: F401
import app.models.model_registry  # noqa: F401
import app.models.platform_link   # noqa: F401
import app.models.token_usage     # noqa: F401
import app.models.user            # noqa: F401

# ─────────────────────────────────────────────────────────────────────────────

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

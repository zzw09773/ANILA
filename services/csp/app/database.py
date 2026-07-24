from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings


def build_runtime_connect_args(database_url: str | None = None) -> dict:
    """Postgres ``options`` for the runtime engine only.

    Returns an empty dict for non-Postgres URLs (unit tests use SQLite).
    Alembic builds its own engine from ``MIGRATION_DATABASE_URL`` and must
    not call this helper — long migration locks would otherwise trip
    ``lock_timeout``.
    """
    url = settings.DATABASE_URL if database_url is None else database_url
    if not url.startswith("postgresql"):
        return {}
    return {
        "options": (
            f"-c lock_timeout={settings.ANILA_DB_LOCK_TIMEOUT_MS} "
            f"-c idle_in_transaction_session_timeout="
            f"{settings.ANILA_DB_IDLE_TX_TIMEOUT_MS}"
        )
    }


engine = create_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.DEBUG,
    connect_args=build_runtime_connect_args(),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

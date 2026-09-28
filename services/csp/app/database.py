from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings

# Several uvicorn workers each keep a pool. PgBouncer sits in front, so a
# small pool per process is enough: 32 workers × (2 + 2) = 128 clients,
# and the pooler multiplexes those onto far fewer Postgres sessions.
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=2,
    max_overflow=2,
    pool_timeout=30,
    pool_pre_ping=True,
    echo=False,
)

# expire_on_commit=False: the default True expires every loaded instance on
# commit, so the next attribute access silently checks a connection back out
# and leaves an open transaction for the rest of the request (including across
# outbound HTTP / SSE). Callers that need DB-computed values after commit must
# db.refresh() explicitly.
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.DEBUG,
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

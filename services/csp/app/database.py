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


# W2-1:連線池參數改走 env,並補上先前完全缺席的 pool_timeout / pool_recycle。
#
# 為什麼:原本 `pool_size=10, max_overflow=20` 寫死在程式裡(上限 30),
# `config.py` 無對應設定 → **生產無法調**。而 Starlette 對 199 個 sync endpoint
# 用 anyio 預設 limiter(40 tokens),40 條執行緒各持 1 連線而池只有 30 —— 兩個
# 數字從一開始就對不上。加上 RAG 每請求佔多條、SSE 每個串流 pin 1 條達 5 分鐘
# (`PROXY_STREAM_MAX_SECONDS=300`),實際 RAG 併發天花板約 10,第 11 個請求
# 阻塞後拋 TimeoutError,而**沒有 exception handler** → 使用者拿到裸 500。
#
# `pool_timeout` 先前是 SQLAlchemy 預設 30s(等 30 秒才炸,使用者早就放棄);
# `pool_recycle` 缺席意味著長命連線可能被 PG 端或中間設備靜默斷掉。
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.ANILA_DB_POOL_SIZE,
    max_overflow=settings.ANILA_DB_MAX_OVERFLOW,
    pool_timeout=settings.ANILA_DB_POOL_TIMEOUT_S,
    pool_recycle=settings.ANILA_DB_POOL_RECYCLE_S,
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

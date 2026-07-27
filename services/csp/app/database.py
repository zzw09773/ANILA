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

# ``expire_on_commit=False`` is a deadlock guard, not a micro-optimisation.
# It is THE load-bearing half of the 2026-07-25 pool-exhaustion fix.  Do not
# remove it on the assumption that ``Session.close()`` elsewhere covers the
# same ground — it does not, and the platform-wide freeze returns.
#
# What was actually measured (SQLAlchemy 2.0.43, pool checkout/checkin events):
#
#     expire_on_commit=True   conns held after commit()          = 0
#                             conns held after ONE post-commit
#                             attribute read                     = 1  (txn open)
#     expire_on_commit=False  same two probes                    = 0, 0
#
# So ``commit()`` already returns the connection to the pool.  What re-pins it
# is the *next attribute touch after* the commit: with the SQLAlchemy default
# (``True``) every ``commit()`` arms all ORM instances for a lazy re-SELECT, so
# that touch runs synchronous ``_load_expired`` DB I/O — on an ``async def``
# handler or inside a StreamingResponse body that means blocking the event-loop
# thread AND re-opening a transaction that holds its pooled connection until the
# response finishes.  That is the ring behind the cold-burst outage
# (``pg_active=31, idle_tx=30`` against ``pool_size=10 + max_overflow=20``):
# 30 streams each re-pinned a connection after ``_commit_stream_admission``
# and starved the control plane.
#
# Because the flag is global it repairs *every* SSE path at once, including
# ones nobody edited (e.g. ``app/api/agent_dispatch.py``'s streaming handler).
# ``proxy/service.py::_release_stream_admission_session`` is defence in depth
# on top of it, not a replacement for it.
#
# INVARIANTS this flag depends on — violate any of these and it turns unsafe:
#
# 1. Every TOCTOU / governance re-read that re-SELECTs a row already in the
#    identity map MUST chain ``populate_existing()``.  A plain Query that hits
#    a live (non-expired) identity-map entry throws away the row it just
#    SELECTed and returns the in-memory copy; with the default ``True`` the
#    commit-expiry hid that.  This flag removes that implicit mechanism, so
#    ``populate_existing()`` becomes the *only* thing forcing a refresh.
#    See ``services/retention_reaper.py`` (legal-hold re-check before unlink),
#    ``services/proxy/closure.py::_persist_once`` and
#    ``app/modules/policy/service.py::_resolve_resource_for_update``.
# 2. Only **already-loaded column** values survive on a detached instance.
#    Relationships do not: reading any unloaded relationship off a detached
#    object raises ``DetachedInstanceError`` (verified for all four ``Task``
#    relationships).  Post-release code may read scalars it loaded earlier —
#    never traverse.
# 3. **Never mutate a pre-close instance after the Session was closed.**
#    Measured: setting an attribute on a detached instance and calling
#    ``commit()`` raises nothing, logs nothing, and the write is silently
#    discarded (DB kept the old value).  Post-close durable writes must go
#    through a fresh load in the reopened Session, which is what
#    ``persist_task_call_closure`` does.
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

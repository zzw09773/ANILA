"""Async queue-based usage writer to avoid SQLite write contention."""
import asyncio
import logging
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models.token_usage import TokenUsage
from app.config import settings
from sqlalchemy.exc import DBAPIError, OperationalError

logger = logging.getLogger(__name__)

_usage_queue: asyncio.Queue | None = None


def get_usage_queue() -> asyncio.Queue:
    global _usage_queue
    if _usage_queue is None:
        _usage_queue = asyncio.Queue()
    return _usage_queue


async def enqueue_usage(
    api_key_id: int | None,
    user_id: int,
    department_id: int | None,
    model_id: int | None,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    request_duration_ms: int | None = None,
    conversation_id: str | None = None,
    trace_id: str | None = None,
    request_type: str = "chat",
    caller_agent_id: int | None = None,
    caller_client_id: int | None = None,
):
    """Push usage data into the async queue (non-blocking).

    ``api_key_id`` may be ``None`` for JWT / cookie-authenticated SPA traffic
    — such rows land in the dashboard's "Web UI" bucket via usage_service.

    ``request_type`` (Sprint 4 / migration 0020): 'chat' / 'embedding' /
    'judge'. Lets dashboards split spend by kind without joining
    ``model_registry.model_type``. Default 'chat' keeps every existing
    caller working unchanged.

    ``caller_agent_id`` / ``caller_client_id`` (Sprint 8 X / Phase G —
    migration 0027): when CSP forwards or receives s2s traffic on
    behalf of a known agent / service client, populate the matching
    column for "by agent" / "by base model" / "by client" dashboards.
    Both ``None`` (default) keeps direct UI / SDK traffic looking
    exactly like it did before.
    """
    queue = get_usage_queue()
    await queue.put({
        "api_key_id": api_key_id,
        "user_id": user_id,
        "department_id": department_id,
        "model_id": model_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "request_timestamp": datetime.now(timezone.utc),
        "request_duration_ms": request_duration_ms,
        "conversation_id": conversation_id,
        "trace_id": trace_id,
        "request_type": request_type,
        "caller_agent_id": caller_agent_id,
        "caller_client_id": caller_client_id,
    })


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    original = getattr(exc, "orig", None)
    code = str(getattr(original, "pgcode", "") or getattr(original, "sqlstate", ""))
    return code.startswith("08") or code in {"40001", "40P01", "55P03"}


def _write_batch(batch: list[dict]) -> None:
    if not batch:
        return
    db = SessionLocal()
    try:
        db.bulk_insert_mappings(TokenUsage, batch)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _flush_batch(batch: list[dict]):
    """Write usage with transient retry and poison-row isolation.

    The legacy taskless queue is intentionally retained, but one malformed FK
    must not roll back every unrelated row in the batch.
    """
    if not batch:
        return
    for attempt in range(1, 4):
        try:
            # SQLAlchemy's synchronous session/commit can block on a row or
            # transaction lock held by the request that just enqueued this
            # usage record.  Running it on the event-loop thread creates a
            # self-deadlock: the request cannot resume to release its
            # transaction while the writer is waiting for that transaction.
            # Keep the existing synchronous SessionLocal boundary, but move
            # the blocking DB work to the default executor so the ASGI loop
            # can continue and release request-scoped transactions.
            await asyncio.to_thread(_write_batch, batch)
            logger.info("已寫入 %s 筆用量記錄", len(batch))
            return
        except Exception as exc:
            if _is_transient(exc) and attempt < 3:
                logger.warning("用量 DB 暫時性錯誤，重試 %s/3: %s", attempt, exc)
                await asyncio.sleep(0.01 * attempt)
                continue
            if _is_transient(exc):
                # A connection/serialization/deadlock exhaustion says
                # nothing about row validity.  Propagate so the owning loop
                # retains and retries the whole legal batch; never bisect and
                # discard it as poison data.
                logger.error("用量 DB 暫時性錯誤重試耗盡，保留 batch: %s", exc)
                raise
            if len(batch) > 1:
                midpoint = len(batch) // 2
                logger.error(
                    "用量 batch 含無效資料，二分隔離 %s 筆: %s", len(batch), exc
                )
                await _flush_batch(batch[:midpoint])
                await _flush_batch(batch[midpoint:])
                return
            logger.error("捨棄單筆無效用量記錄: %s", exc)
            return


async def _usage_writer_loop():
    """Background task: flush usage queue periodically or when batch is full."""
    queue = get_usage_queue()
    batch: list[dict] = []

    while True:
        try:
            # Wait for data with timeout
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=settings.USAGE_FLUSH_INTERVAL
                )
                batch.append(item)
            except asyncio.TimeoutError:
                pass

            # Drain remaining items from queue (non-blocking)
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break

            # Flush if batch is full or timeout elapsed
            if len(batch) >= settings.USAGE_BATCH_SIZE or (batch and queue.empty()):
                await _flush_batch(batch)
                batch = []

        except asyncio.CancelledError:
            # Flush remaining on shutdown
            if batch:
                await _flush_batch(batch)
            break
        except Exception as e:
            logger.error(f"用量寫入迴圈錯誤: {e}")
            await asyncio.sleep(1)


async def start_usage_writer() -> asyncio.Task:
    """Start the background usage writer task."""
    task = asyncio.create_task(_usage_writer_loop())
    logger.info("用量寫入背景任務已啟動")
    return task

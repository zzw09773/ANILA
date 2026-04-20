"""Async queue-based usage writer to avoid SQLite write contention."""
import asyncio
import logging
from datetime import datetime, timezone
from app.database import SessionLocal
from app.models.token_usage import TokenUsage
from app.config import settings

logger = logging.getLogger(__name__)

_usage_queue: asyncio.Queue | None = None


def get_usage_queue() -> asyncio.Queue:
    global _usage_queue
    if _usage_queue is None:
        _usage_queue = asyncio.Queue()
    return _usage_queue


async def enqueue_usage(
    api_key_id: int,
    user_id: int,
    department_id: int | None,
    model_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    request_duration_ms: int | None = None,
    conversation_id: str | None = None,
    trace_id: str | None = None,
):
    """Push usage data into the async queue (non-blocking)."""
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
    })


async def _flush_batch(batch: list[dict]):
    """Write a batch of usage records to the database."""
    if not batch:
        return
    db = SessionLocal()
    try:
        db.bulk_insert_mappings(TokenUsage, batch)
        db.commit()
        logger.info(f"已寫入 {len(batch)} 筆用量記錄")
    except Exception as e:
        db.rollback()
        logger.error(f"寫入用量記錄失敗: {e}")
    finally:
        db.close()


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

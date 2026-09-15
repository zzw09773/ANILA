"""Async queue-based usage writer to avoid SQLite write contention."""
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models.token_usage import TokenUsage

logger = logging.getLogger(__name__)

_usage_queue: asyncio.Queue | None = None
USAGE_BATCH_SIZE = 100
USAGE_FLUSH_INTERVAL_SECONDS = 5


def get_usage_queue() -> asyncio.Queue:
    global _usage_queue
    if _usage_queue is None:
        _usage_queue = asyncio.Queue()
    return _usage_queue


async def enqueue_usage(
    api_key_id: int | None,
    user_id: int,
    department_id: int | None,
    model_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    request_duration_ms: int | None = None,
    conversation_id: str | None = None,
    trace_id: str | None = None,
    request_type: str = "chat",
    caller_agent_id: int | None = None,
    caller_client_id: int | None = None,
    invocation_id: str | None = None,
    usage_kind: str = "inference",
    token_source: str = "unknown",
    outcome: str = "success",
    model_name_snapshot: str | None = None,
    reasoning_tokens: int | None = None,
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
        "invocation_id": invocation_id,
        "usage_kind": usage_kind,
        "token_source": token_source,
        "outcome": outcome,
        "model_name_snapshot": model_name_snapshot,
        "reasoning_tokens": reasoning_tokens,
    })


def _is_invocation_conflict(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "invocation_id" in msg or "uq_token_usage_invocation_id" in msg


async def _flush_batch(batch: list[dict]):
    """Write a batch of usage records to the database."""
    if not batch:
        return
    db = SessionLocal()
    try:
        db.bulk_insert_mappings(TokenUsage, batch)
        db.commit()
        logger.info(f"已寫入 {len(batch)} 筆用量記錄")
        return
    except Exception as e:
        db.rollback()
        if not _is_invocation_conflict(e):
            logger.error(f"寫入用量記錄失敗: {e}")
            return
        written = 0
        skipped = 0
        for item in batch:
            try:
                db.add(TokenUsage(**{k: v for k, v in item.items() if hasattr(TokenUsage, k)}))
                db.commit()
                written += 1
            except IntegrityError as row_exc:
                db.rollback()
                if _is_invocation_conflict(row_exc):
                    skipped += 1
                    continue
                logger.error(f"寫入用量記錄失敗: {row_exc}")
            except Exception as row_exc:
                db.rollback()
                logger.error(f"寫入用量記錄失敗: {row_exc}")
        logger.info(f"用量批次含重複 invocation_id：寫入 {written} 筆、略過 {skipped} 筆")
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
                    queue.get(), timeout=USAGE_FLUSH_INTERVAL_SECONDS
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
            if len(batch) >= USAGE_BATCH_SIZE or (batch and queue.empty()):
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

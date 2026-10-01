"""Async queue-based usage writer to avoid SQLite write contention."""
import asyncio
import logging
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models.token_usage import TokenUsage

logger = logging.getLogger(__name__)

# 呼叫開始的 UTC 時間。排入用量時若沒有明確時間，就用這個，而不是佇列當下。
_bound_timestamp: ContextVar[datetime | None] = ContextVar(
    "usage_request_timestamp", default=None
)

_usage_queue: asyncio.Queue | None = None
USAGE_BATCH_SIZE = 100
USAGE_FLUSH_INTERVAL_SECONDS = 5
SHUTDOWN_DRAIN_SECONDS = 10
# Armed by stop_usage_writer so the drain and the lifespan wait share it.
_shutdown_deadline: float | None = None
_shutdown_pending = 0
_shutdown_logged = False


def get_usage_queue() -> asyncio.Queue:
    global _usage_queue
    if _usage_queue is None:
        _usage_queue = asyncio.Queue()
    return _usage_queue


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def bind_usage_timestamp(value: datetime) -> Token:
    """把這次上游呼叫的開始時間綁在目前工作上。結束時要 reset。"""
    return _bound_timestamp.set(_aware_utc(value))


def reset_usage_timestamp(token: Token) -> None:
    try:
        _bound_timestamp.reset(token)
    except ValueError:
        # 取消發生在另一個 context 時，這個 token 不屬於當下這一層。
        return


def resolve_usage_timestamp(explicit: datetime | None = None) -> datetime:
    """明確時間優先，其次是呼叫開始時綁定的時間，最後才是現在。"""
    if explicit is not None:
        return _aware_utc(explicit)
    bound = _bound_timestamp.get()
    if bound is not None:
        return _aware_utc(bound)
    return datetime.now(timezone.utc)


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
    request_timestamp: datetime | None = None,
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
        "request_timestamp": resolve_usage_timestamp(request_timestamp),
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


def queue_usage_nowait(item: dict) -> None:
    """Queue one usage row without awaiting. Safe while a stream is closing."""
    payload = dict(item)
    if payload.get("request_timestamp") is None:
        payload["request_timestamp"] = resolve_usage_timestamp()
    get_usage_queue().put_nowait(payload)


def _remember_uncommitted(count: int) -> None:
    global _shutdown_pending
    _shutdown_pending = count


def _uncommitted_count() -> int:
    queued = _usage_queue.qsize() if _usage_queue is not None else 0
    return _shutdown_pending + queued


def _log_uncommitted(count: int) -> None:
    global _shutdown_logged
    if count <= 0 or _shutdown_logged:
        return
    _shutdown_logged = True
    logger.error("關閉時仍有 %s 筆用量沒寫入", count)


def _clear_shutdown_state() -> None:
    global _shutdown_deadline, _shutdown_logged
    _shutdown_deadline = None
    _shutdown_logged = False
    _remember_uncommitted(0)


def _connect_timeout_listener(remaining: float):
    """Bound a new libpq connect to the drain budget.

    libpq treats 0 as "wait forever" and rounds values below 2 seconds
    up to 2. A shorter budget must not start that connect.
    """
    seconds = int(remaining)

    def _inject(dialect, conn_rec, cargs, cparams):
        if seconds < 2:
            raise TimeoutError("用量寫入的關閉期限不夠建立新的資料庫連線")
        cparams["connect_timeout"] = seconds

    return _inject


def _limit_checkout(db, deadline: float) -> None:
    """Check out a pooled connection without waiting past ``deadline``."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("用量寫入的關閉期限已到")
    bind = db.get_bind()
    pool = getattr(bind, "pool", None)
    previous = getattr(pool, "_timeout", None)
    capped = isinstance(previous, (int, float))
    registered = False
    listener = None
    if capped:
        pool._timeout = min(float(previous), remaining)
    try:
        dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
        if dialect_name == "postgresql" and hasattr(bind, "dispatch"):
            listener = _connect_timeout_listener(remaining)
            event.listen(bind, "do_connect", listener)
            registered = True
        db.connection()
    finally:
        if capped:
            pool._timeout = previous
        if registered and listener is not None:
            event.remove(bind, "do_connect", listener)


def _bump_after_commit(db, rows: list[dict]) -> None:
    """計數失敗不能把已經 commit 的用量列退回佇列。"""
    if not rows:
        return
    try:
        from app.services.quota_service import bump_committed_usage

        bump_committed_usage(db, rows)
    except Exception:
        logger.warning("用量計數累加失敗", exc_info=True)


def _apply_statement_timeout(db, deadline: float | None) -> None:
    """Keep the next statement inside the time still left on ``deadline``."""
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("用量寫入的關閉期限已到")
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    ms = max(1, int(remaining * 1000))
    db.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(ms)},
    ).scalar()


async def _flush_batch(batch: list[dict], *, deadline: float | None = None) -> list[dict]:
    """Write a batch. Return the rows that still need a commit.

    A failed commit leaves the rows in the returned list. The caller
    must not drop them. Duplicate ``invocation_id`` rows are skipped.

    When ``deadline`` is set, pooling and statements stop at that
    monotonic time instead of the engine's 30s pool timeout.
    """
    if not batch:
        _remember_uncommitted(0)
        return []
    _remember_uncommitted(len(batch))
    if deadline is not None and time.monotonic() >= deadline:
        return list(batch)
    db = SessionLocal()
    try:
        if deadline is not None:
            _limit_checkout(db, deadline)
            _apply_statement_timeout(db, deadline)
        db.bulk_insert_mappings(TokenUsage, batch)
        db.commit()
        _bump_after_commit(db, batch)
        logger.info(f"已寫入 {len(batch)} 筆用量記錄")
        _remember_uncommitted(0)
        return []
    except Exception as e:
        db.rollback()
        if not _is_invocation_conflict(e):
            logger.error(f"寫入用量記錄失敗: {e}")
            _remember_uncommitted(len(batch))
            return list(batch)
        written = 0
        skipped = 0
        written_rows: list[dict] = []
        pending: list[dict] = []
        for index, item in enumerate(batch):
            if deadline is not None and time.monotonic() >= deadline:
                pending.extend(batch[index:])
                break
            try:
                _apply_statement_timeout(db, deadline)
                db.add(TokenUsage(**{k: v for k, v in item.items() if hasattr(TokenUsage, k)}))
                db.commit()
                written += 1
                written_rows.append(item)
            except IntegrityError as row_exc:
                db.rollback()
                if _is_invocation_conflict(row_exc):
                    skipped += 1
                    continue
                logger.error(f"寫入用量記錄失敗: {row_exc}")
                pending.extend(batch[index:])
                break
            except Exception as row_exc:
                db.rollback()
                logger.error(f"寫入用量記錄失敗: {row_exc}")
                pending.extend(batch[index:])
                break
        logger.info(f"用量批次含重複 invocation_id：寫入 {written} 筆、略過 {skipped} 筆")
        _bump_after_commit(db, written_rows)
        _remember_uncommitted(len(pending))
        return pending
    finally:
        db.close()


def _take_queued(queue: asyncio.Queue, batch: list[dict]) -> None:
    while True:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return


async def _drain_on_shutdown(queue: asyncio.Queue, batch: list[dict]) -> list[dict]:
    """Commit whatever is still queued. Stop at the shared shutdown deadline."""
    armed = _shutdown_deadline
    deadline = armed if armed is not None else time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    while True:
        _take_queued(queue, batch)
        _remember_uncommitted(len(batch))
        if not batch:
            return []
        if time.monotonic() >= deadline:
            return batch
        batch = await _flush_batch(batch, deadline=deadline)
        if not batch:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _take_queued(queue, batch)
            _remember_uncommitted(len(batch))
            return batch
        await asyncio.sleep(min(1.0, remaining))


async def _usage_writer_loop():
    """Background task: flush usage queue periodically or when batch is full."""
    global _shutdown_logged
    # A deadline already armed belongs to this shutdown. Don't replace it
    # with a fresh 10s budget once the task is finally scheduled.
    if _shutdown_deadline is None:
        _shutdown_logged = False
        _remember_uncommitted(0)
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

            # Flush if batch is full or timeout elapsed. Keep every row
            # whose commit did not succeed.
            if len(batch) >= USAGE_BATCH_SIZE or (batch and queue.empty()):
                batch = await _flush_batch(batch)
                if batch:
                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            try:
                left = await _drain_on_shutdown(queue, batch)
            except asyncio.CancelledError:
                _log_uncommitted(_uncommitted_count())
                raise
            _log_uncommitted(len(left))
            raise
        except Exception as e:
            logger.error(f"用量寫入迴圈錯誤: {e}")
            await asyncio.sleep(1)


async def start_usage_writer() -> asyncio.Task:
    """Start the background usage writer task."""
    task = asyncio.create_task(_usage_writer_loop())
    logger.info("用量寫入背景任務已啟動")
    return task


async def stop_usage_writer(task: asyncio.Task) -> None:
    """Cancel the writer and wait only until the drain deadline.

    Checkout, statements, and this wait all use that one deadline.
    If the task is still running when time is up, the uncommitted
    count is logged and shutdown continues.
    """
    global _shutdown_deadline
    deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    _shutdown_deadline = deadline
    task.add_done_callback(lambda _task: _clear_shutdown_state())
    task.cancel()
    remaining = deadline - time.monotonic()
    if remaining < 0:
        remaining = 0.0
    done, _pending_tasks = await asyncio.wait({task}, timeout=remaining)
    if task not in done:
        _log_uncommitted(_uncommitted_count())
        return
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("用量寫入任務在關閉時失敗")

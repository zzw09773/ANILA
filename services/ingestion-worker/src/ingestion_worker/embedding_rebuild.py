"""Re-embed the corpus onto a new model without moving search until it finishes.

The console role is the target. ``embedding_activation.active_model_id``
is what search uses. This job fills the side table for the target, then
flips that pointer in one update. A restart continues from rows that
still have no vector for the target.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import asyncpg

from anila_core.embeddings.dims import ann_index_sql, fit_stored_vector
from anila_core.embeddings.swap import plan_write_targets
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore
from anila_core.storage.embeddable import fact_predicate, summary_predicate
from pgvector import HalfVector

from ingestion_worker.embedder import Embedder
from ingestion_worker.settings import settings

logger = logging.getLogger(__name__)

_PASS_BUDGET_S = 240
_BATCH = 16
_LOCK = 40580058


def direct_lock_dsn(database_url: str) -> str:
    """Session advisory locks need a connection the pooler will not rotate.

    Transaction pooling gives the server connection back at each commit.
    A ``pg_try_advisory_lock`` taken on that connection does not cover the
    next statement. When the URL host is the pooler, dial ``csp-db`` with
    the same credentials. Any other host is already a direct connection.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(database_url)
    if (parts.hostname or "") != "pgbouncer":
        return database_url
    hostport = parts.netloc.rsplit("@", 1)[-1]
    if not hostport.startswith("pgbouncer"):
        return database_url
    new_host = "csp-db" + hostport[len("pgbouncer") :]
    if "@" in parts.netloc:
        auth = parts.netloc.rsplit("@", 1)[0]
        netloc = f"{auth}@{new_host}"
    else:
        netloc = new_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class IngestTarget:
    model_id: int
    name: str
    native: int
    for_column: bool


async def load_ingest_targets(pool, embedder: Embedder) -> list[IngestTarget] | None:
    """Models a new chunk must be embedded with, or None when the schema is older.

    None tells the caller to keep the single embedder it already has.
    """
    try:
        async with pool.acquire() as conn:
            role = await conn.fetchrow(
                """
                SELECT id, name, embedding_native_dim
                  FROM model_registry
                 WHERE is_platform_embedding = true
                   AND model_type = 'embedding'
                   AND is_active = true
                 LIMIT 1
                """
            )
            act = await conn.fetchrow(
                """
                SELECT active_model_id, rebuild_status
                  FROM embedding_activation
                 WHERE id = 1
                """
            )
            active = None
            if act is not None and isinstance(act["active_model_id"], int):
                active = await conn.fetchrow(
                    """
                    SELECT id, name, embedding_native_dim
                      FROM model_registry
                     WHERE id = $1
                    """,
                    act["active_model_id"],
                )
    except Exception:
        logger.info("embedding plan unavailable; ingest keeps the single embedder")
        return None
    if role is None or not isinstance(role["id"], int):
        return None
    role_native = role["embedding_native_dim"]
    if not isinstance(role_native, int) or role_native <= 0:
        role_native = embedder.native_dim
    active_id = active["id"] if active is not None and isinstance(active["id"], int) else None
    active_name = active["name"] if active is not None else None
    active_native = (
        active["embedding_native_dim"]
        if active is not None and isinstance(active["embedding_native_dim"], int)
        else None
    )
    status = act["rebuild_status"] if act is not None else None
    planned = plan_write_targets(
        role_id=role["id"],
        role_name=role["name"],
        role_native=role_native,
        active_id=active_id,
        active_name=active_name,
        active_native=active_native,
        rebuild_status=status if isinstance(status, str) else None,
    )
    return [
        IngestTarget(
            model_id=item.model_id,
            name=item.name,
            native=item.native_dim,
            for_column=item.for_column,
        )
        for item in planned
    ]


async def embed_ingest_targets(
    pool,
    embedder: Embedder,
    texts: list[str],
) -> tuple[list[list[float]], str, int, list[tuple[int, list[list[float]], int]]]:
    """Embed ``texts`` for every model a new row needs.

    The first three return values are what the legacy column stores
    (the model search is still using). The list is side-table writes.
    When the activation schema is absent, this is one call on ``embedder``.
    """
    plan = await load_ingest_targets(pool, embedder)
    if not texts:
        name = embedder.model_name
        native = embedder.native_dim
        if plan:
            column = next((item for item in plan if item.for_column), plan[0])
            name = column.name
            native = column.native
        return [], name, native, []
    if plan is None:
        vectors = await embedder.embed(texts)
        return vectors, embedder.model_name, embedder.native_dim, []

    produced: dict[int, list[list[float]]] = {}
    for target in plan:
        same = (
            target.name == embedder.model_name
            and target.native == embedder.native_dim
        )
        if same:
            produced[target.model_id] = await embedder.embed(texts)
            continue
        worker = Embedder(
            settings,
            model_name=target.name,
            native_dim=target.native if target.native > 0 else None,
        )
        try:
            produced[target.model_id] = await worker.embed(texts)
        finally:
            await worker.close()
    column = next((item for item in plan if item.for_column), plan[0])
    slots = [
        (item.model_id, produced[item.model_id], item.native)
        for item in plan
    ]
    return produced[column.model_id], column.name, column.native, slots


async def rebuild_embeddings(ctx: dict) -> dict:
    """One resumable pass. Re-enqueues itself while rows remain.

    The advisory lock is session-scoped, so it is taken on a direct
    connection and held until the pass finishes. The pooled connection
    commits between batches; under transaction pooling that would move
    the lock onto a server connection the next statement does not use.
    """
    pool = ctx["pool"]
    started = time.monotonic()
    lock_conn = await asyncpg.connect(direct_lock_dsn(settings.database_url))
    try:
        try:
            locked = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK)
        except Exception:
            logger.exception("embedding rebuild could not take its lock")
            return {"status": "skipped"}
        if not locked:
            return {"status": "busy"}
        try:
            async with pool.acquire() as conn:
                return await _pass(ctx, conn, started)
        finally:
            try:
                await lock_conn.execute("SELECT pg_advisory_unlock($1)", _LOCK)
            except Exception:
                logger.exception("embedding rebuild unlock failed")
    finally:
        await lock_conn.close()


async def _pass(ctx: dict, conn: asyncpg.Connection, started: float) -> dict:
    row = await conn.fetchrow(
        """
        SELECT active_model_id, rebuild_target_model_id, rebuild_status, rebuild_started_at
          FROM embedding_activation
         WHERE id = 1
        """
    )
    if row is None or row["rebuild_status"] not in {"pending", "running"}:
        return {"status": "idle"}
    target_id = row["rebuild_target_model_id"]
    if not isinstance(target_id, int):
        return {"status": "idle"}
    model = await conn.fetchrow(
        """
        SELECT id, name, embedding_native_dim
          FROM model_registry
         WHERE id = $1
        """,
        target_id,
    )
    if model is None:
        await _mark_failed(conn, target_id, "重建目標模型不存在")
        return {"status": "failed"}

    await conn.execute(
        """
        UPDATE embedding_activation
           SET rebuild_status = 'running',
               rebuild_started_at = COALESCE(rebuild_started_at, NOW()),
               rebuild_updated_at = NOW()
         WHERE id = 1
           AND rebuild_status IN ('pending', 'running')
           AND rebuild_target_model_id = $1
        """,
        target_id,
    )

    native = model["embedding_native_dim"]
    embedder = Embedder(
        settings,
        model_name=model["name"],
        native_dim=native if isinstance(native, int) and native > 0 else None,
    )
    try:
        while time.monotonic() - started < _PASS_BUDGET_S:
            if not await _still_open(conn, target_id):
                return {"status": "cancelled"}
            batch = await _next_batch(ctx["pool"], conn, target_id, _BATCH)
            if not batch:
                return await _finish(ctx["pool"], conn, target_id)
            texts = [item["text"] for item in batch]
            try:
                vectors = await embedder.embed(texts)
            except Exception as exc:
                logger.warning("embedding rebuild batch failed: %s", exc)
                await _fail_batch(conn, target_id, batch, str(exc)[:500])
                await _publish_counts(ctx["pool"], conn, target_id)
                continue
            if len(vectors) != len(batch):
                await _fail_batch(conn, target_id, batch, "vector count mismatch")
                await _publish_counts(ctx["pool"], conn, target_id)
                continue
            await _write_batch(conn, target_id, batch, vectors, native)
            await _publish_counts(ctx["pool"], conn, target_id)
    finally:
        await embedder.close()

    await _requeue(ctx)
    total, done, errors = await _counts(ctx["pool"], conn, target_id)
    return {"status": "running", "done": done, "total": total, "errors": errors}


async def _still_open(conn, target_id: int) -> bool:
    status = await conn.fetchval(
        """
        SELECT rebuild_status FROM embedding_activation
         WHERE id = 1 AND rebuild_target_model_id = $1
        """,
        target_id,
    )
    return status in {"pending", "running"}


async def _next_batch(pool, conn, model_id: int, limit: int) -> list[dict]:
    items: list[dict] = []
    collections = await conn.fetch("SELECT id FROM ingestion_collections ORDER BY id")
    for coll in collections:
        if len(items) >= limit:
            break
        store = CollectionScopedPgVectorStore(pool, collection_id=int(coll["id"]))
        rows = await store.leaves_missing_embedding(model_id, limit - len(items))
        for subject_id, content in rows:
            items.append(
                {
                    "subject": "chunk",
                    "subject_id": subject_id,
                    "text": content,
                    "collection_id": int(coll["id"]),
                }
            )
    if len(items) < limit:
        facts = await conn.fetch(
            f"""
            SELECT id, key, value
              FROM user_facts
             WHERE {fact_predicate("user_facts", "$1")}
               AND NOT EXISTS (
                    SELECT 1 FROM embedding_vectors v
                     WHERE v.subject = 'fact'
                       AND v.subject_id = user_facts.id
                       AND v.model_id = $1
               )
             ORDER BY id
             LIMIT $2
            """,
            model_id,
            limit - len(items),
        )
        for row in facts:
            items.append(
                {
                    "subject": "fact",
                    "subject_id": int(row["id"]),
                    "text": f"{row['key']}: {row['value']}",
                    "collection_id": None,
                }
            )
    if len(items) < limit:
        summaries = await conn.fetch(
            f"""
            SELECT id, summary
              FROM conversation_summaries s
             WHERE {summary_predicate("s", "$1")}
               AND NOT EXISTS (
                    SELECT 1 FROM embedding_vectors v
                     WHERE v.subject = 'summary'
                       AND v.subject_id = s.id
                       AND v.model_id = $1
               )
             ORDER BY id
             LIMIT $2
            """,
            model_id,
            limit - len(items),
        )
        for row in summaries:
            items.append(
                {
                    "subject": "summary",
                    "subject_id": int(row["id"]),
                    "text": row["summary"],
                    "collection_id": None,
                }
            )
    return items


async def _write_batch(conn, model_id: int, batch: list[dict], vectors, native) -> None:
    declared = native if isinstance(native, int) and native > 0 else None
    for item, raw in zip(batch, vectors):
        stored = fit_stored_vector(raw, declared_native=declared)
        if item["subject"] == "chunk" and item["collection_id"] is not None:
            async with conn.transaction():
                await conn.execute(
                    f"SET LOCAL anila.collection_id = {int(item['collection_id'])}"
                )
                await _upsert(conn, item, model_id, stored)
        else:
            await _upsert(conn, item, model_id, stored)


async def _upsert(conn, item: dict, model_id: int, stored) -> None:
    await conn.execute(
        """
        INSERT INTO embedding_vectors
            (subject, subject_id, model_id, collection_id, dims, native_dims, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (subject, subject_id, model_id) DO UPDATE SET
            dims = EXCLUDED.dims,
            native_dims = EXCLUDED.native_dims,
            embedding = EXCLUDED.embedding,
            collection_id = EXCLUDED.collection_id
        """,
        item["subject"],
        item["subject_id"],
        model_id,
        item["collection_id"],
        stored.dims,
        stored.native_dims,
        HalfVector(stored.values),
    )
    await conn.execute(
        """
        DELETE FROM embedding_rebuild_failures
         WHERE subject = $1 AND subject_id = $2 AND model_id = $3
        """,
        item["subject"],
        item["subject_id"],
        model_id,
    )
    if 1 <= stored.dims <= 4000:
        nested = conn.transaction()
        await nested.start()
        try:
            await conn.execute(
                ann_index_sql(model_id=model_id, dims=stored.dims, subject=item["subject"])
            )
            await nested.commit()
        except Exception:
            await nested.rollback()


async def _fail_batch(conn, model_id: int, batch: list[dict], message: str) -> None:
    for item in batch:
        await conn.execute(
            """
            INSERT INTO embedding_rebuild_failures
                (subject, subject_id, model_id, attempts, last_error)
            VALUES ($1, $2, $3, 1, $4)
            ON CONFLICT (subject, subject_id, model_id) DO UPDATE SET
                attempts = embedding_rebuild_failures.attempts + 1,
                last_error = EXCLUDED.last_error
            """,
            item["subject"],
            item["subject_id"],
            model_id,
            message,
        )
    await conn.execute(
        """
        UPDATE embedding_activation
           SET rebuild_last_error = $1, rebuild_updated_at = NOW()
         WHERE id = 1
        """,
        message,
    )


async def _counts(pool, conn, model_id: int) -> tuple[int, int, int]:
    total = 0
    done = 0
    collections = await conn.fetch("SELECT id FROM ingestion_collections")
    for coll in collections:
        store = CollectionScopedPgVectorStore(pool, collection_id=int(coll["id"]))
        coll_total, coll_done = await store.leaf_embedding_counts(model_id)
        total += coll_total
        done += coll_done
    fact_sql = fact_predicate("user_facts", "$1")
    fact_done_sql = fact_predicate("f", "$1")
    sum_sql = summary_predicate("s", "$1")
    fact_total = int(
        await conn.fetchval(
            f"""
            SELECT count(*) FROM user_facts
             WHERE {fact_sql}
            """,
            model_id,
        )
        or 0
    )
    fact_done = int(
        await conn.fetchval(
            f"""
            SELECT count(*)
              FROM user_facts f
              JOIN embedding_vectors v
                ON v.subject = 'fact' AND v.subject_id = f.id AND v.model_id = $1
             WHERE {fact_done_sql}
            """,
            model_id,
        )
        or 0
    )
    sum_total = int(
        await conn.fetchval(
            f"""
            SELECT count(*) FROM conversation_summaries s
             WHERE {sum_sql}
            """,
            model_id,
        )
        or 0
    )
    sum_done = int(
        await conn.fetchval(
            f"""
            SELECT count(*)
              FROM conversation_summaries s
              JOIN embedding_vectors v
                ON v.subject = 'summary' AND v.subject_id = s.id AND v.model_id = $1
             WHERE {sum_sql}
            """,
            model_id,
        )
        or 0
    )
    errors = int(
        await conn.fetchval(
            """
            SELECT count(*) FROM embedding_rebuild_failures
             WHERE model_id = $1 AND attempts >= 3
            """,
            model_id,
        )
        or 0
    )
    return total + fact_total + sum_total, done + fact_done + sum_done, errors


async def _publish_counts(pool, conn, model_id: int) -> None:
    total, done, errors = await _counts(pool, conn, model_id)
    await conn.execute(
        """
        UPDATE embedding_activation
           SET rebuild_done = $1,
               rebuild_total = $2,
               rebuild_errors = $3,
               rebuild_updated_at = NOW()
         WHERE id = 1
           AND rebuild_status IN ('pending', 'running')
           AND rebuild_target_model_id = $4
        """,
        done,
        total,
        errors,
        model_id,
    )


def rebuild_ready_to_switch(*, done: int, total: int) -> bool:
    """只有每一筆可嵌入的資料都有目標模型向量時才把搜尋切過去。"""
    return done >= total


async def _finish(pool, conn, target_id: int) -> dict:
    total, done, errors = await _counts(pool, conn, target_id)
    if not rebuild_ready_to_switch(done=done, total=total):
        await _mark_failed(conn, target_id, "還有資料沒有目標模型的向量，搜尋仍用舊模型")
        await conn.execute(
            """
            UPDATE embedding_activation
               SET rebuild_done = $1, rebuild_total = $2, rebuild_errors = $3
             WHERE id = 1 AND rebuild_target_model_id = $4
            """,
            done,
            total,
            errors,
            target_id,
        )
        return {"status": "failed", "done": done, "total": total, "errors": errors}
    result = await conn.execute(
        """
        UPDATE embedding_activation
           SET previous_model_id = CASE
                   WHEN active_model_id IS DISTINCT FROM $1 THEN active_model_id
                   ELSE previous_model_id
               END,
               active_model_id = $1,
               switched_at = NOW(),
               rebuild_target_model_id = NULL,
               rebuild_status = 'complete',
               rebuild_done = $2,
               rebuild_total = $3,
               rebuild_errors = $4,
               rebuild_updated_at = NOW()
         WHERE id = 1
           AND rebuild_status IN ('pending', 'running')
           AND rebuild_target_model_id = $1
        """,
        target_id,
        done,
        total,
        errors,
    )
    switched = result.endswith("1")
    return {
        "status": "complete" if switched else "cancelled",
        "done": done,
        "total": total,
        "errors": errors,
    }


async def _mark_failed(conn, target_id: int, message: str) -> None:
    await conn.execute(
        """
        UPDATE embedding_activation
           SET rebuild_status = 'failed',
               rebuild_last_error = $1,
               rebuild_updated_at = NOW()
         WHERE id = 1
           AND rebuild_target_model_id = $2
           AND rebuild_status IN ('pending', 'running')
        """,
        message,
        target_id,
    )


async def _requeue(ctx: dict) -> None:
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        await redis.enqueue_job("rebuild_embeddings")
    except Exception:
        logger.exception("embedding rebuild re-enqueue failed")

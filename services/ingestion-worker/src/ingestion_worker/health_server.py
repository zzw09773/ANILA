"""Minimal internal-only HTTP health and Prometheus metrics surface."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any


async def _probe(ctx: dict[str, Any], timeout: float) -> tuple[bool, bool]:
    async def db_probe() -> bool:
        async with ctx["pool"].acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def redis_probe() -> bool:
        return bool(await ctx["redis"].ping())

    db_ok, redis_ok = await asyncio.gather(
        asyncio.wait_for(db_probe(), timeout),
        asyncio.wait_for(redis_probe(), timeout),
        return_exceptions=True,
    )
    return db_ok is True, redis_ok is True


def _supervisors_ready(ctx: dict[str, Any]) -> bool:
    for key in ("reaper_task", "similarity_recompute_task", "process_heartbeat_task"):
        task = ctx.get(key)
        if task is not None and task.done():
            return False
    return True


async def _metrics(ctx: dict[str, Any], timeout: float) -> str:
    async def query():
        async with ctx["pool"].acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT count(*) FILTER (WHERE status='running') AS running,
                       count(*) FILTER (WHERE status='retry_wait') AS retry_wait,
                       count(*) FILTER (WHERE status='dead_letter') AS dead_letter,
                       COALESCE(EXTRACT(EPOCH FROM
                         now()-min(enqueued_at) FILTER
                         (WHERE status IN ('dispatch_pending','queued','retry_wait'))),0)
                         AS queue_age
                  FROM ingestion_jobs
                """
            )

    row = await asyncio.wait_for(query(), timeout)
    heartbeat_age = max(0.0, time.monotonic() - ctx["process_heartbeat"])
    return "\n".join(
        [
            "# TYPE anila_ingestion_worker_up gauge",
            "anila_ingestion_worker_up 1",
            f"anila_ingestion_worker_heartbeat_age_seconds {heartbeat_age:.3f}",
            f"anila_ingestion_jobs_running {int(row['running'])}",
            f"anila_ingestion_jobs_retry_wait {int(row['retry_wait'])}",
            f"anila_ingestion_jobs_dead_letter {int(row['dead_letter'])}",
            f"anila_ingestion_queue_oldest_age_seconds {float(row['queue_age']):.3f}",
            "",
        ]
    )


async def start_server(ctx: dict[str, Any], *, port: int, timeout: float):
    ctx["process_heartbeat"] = time.monotonic()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        status = "200 OK"
        content_type = "application/json"
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
            parts = line.decode("ascii", "replace").split()
            path = parts[1] if len(parts) >= 2 else ""
            if path == "/health":
                body = json.dumps({"status": "alive"})
            elif path == "/ready":
                db_ok, redis_ok = await _probe(ctx, timeout)
                ready = db_ok and redis_ok and _supervisors_ready(ctx)
                if not ready:
                    status = "503 Service Unavailable"
                body = json.dumps(
                    {"status": "ready" if ready else "not_ready"}
                )
            elif path == "/metrics":
                content_type = "text/plain; version=0.0.4"
                body = await _metrics(ctx, timeout)
            else:
                status = "404 Not Found"
                body = json.dumps({"status": "not_found"})
        except Exception:
            status = "503 Service Unavailable"
            body = json.dumps({"status": "not_ready"})
        payload = body.encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_server(handle, "0.0.0.0", port)

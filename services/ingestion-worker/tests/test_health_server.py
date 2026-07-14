from __future__ import annotations

import asyncio

import pytest

from ingestion_worker.health_server import start_server


class Connection:
    async def fetchval(self, _sql):
        return 1

    async def fetchrow(self, _sql):
        return {"running": 1, "retry_wait": 2, "dead_letter": 3, "queue_age": 4.5}


class Acquire:
    async def __aenter__(self):
        return Connection()

    async def __aexit__(self, *_args):
        return None


class Pool:
    def acquire(self):
        return Acquire()


class Redis:
    async def ping(self):
        return True


async def _get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: worker\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data.decode()


@pytest.mark.asyncio
async def test_internal_health_ready_and_metrics_are_bounded_and_nonidentifying():
    ctx = {"pool": Pool(), "redis": Redis()}
    server = await start_server(ctx, port=0, timeout=0.5)
    port = server.sockets[0].getsockname()[1]
    try:
        ready = await _get(port, "/ready")
        metrics = await _get(port, "/metrics")
    finally:
        server.close()
        await server.wait_closed()

    assert "200 OK" in ready and '"status": "ready"' in ready
    assert "anila_ingestion_jobs_dead_letter 3" in metrics
    assert "anila_ingestion_queue_oldest_age_seconds 4.500" in metrics
    for secret_or_identifier in ("postgresql://", "redis://", "document_id", "payload"):
        assert secret_or_identifier not in ready + metrics


@pytest.mark.asyncio
async def test_readiness_fails_when_a_durable_supervisor_has_died():
    dead = asyncio.get_running_loop().create_future()
    dead.set_result(None)
    ctx = {
        "pool": Pool(),
        "redis": Redis(),
        "similarity_recompute_task": dead,
    }
    server = await start_server(ctx, port=0, timeout=0.5)
    port = server.sockets[0].getsockname()[1]
    try:
        ready = await _get(port, "/ready")
    finally:
        server.close()
        await server.wait_closed()

    assert "503 Service Unavailable" in ready
    assert '"status": "not_ready"' in ready

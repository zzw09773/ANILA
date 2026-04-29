"""anila-functions-worker-api — trusted gate, CSP-facing.

Three responsibilities:

  1. Accept HTTP from CSP (verifies ``X-Functions-Api-Secret``)
  2. Forward each request as one Unix-socket IPC call to the
     appropriate sandbox container
     (``/jobs-exec/control.sock`` for run, ``/jobs-extract/control.sock``
     for schema extraction)
  3. Stream the sandbox's event lines back to CSP as ``text/event-stream``

This service runs in the ``anila-internal`` docker network so CSP can
reach it; the sandboxes are on isolated networks. The shared docker
volumes ``jobs-exec`` and ``jobs-extract`` are mounted on both
worker-api and the corresponding sandbox so the Unix socket file is
visible to both.

Worker-api itself **does not execute user code** — it's a trusted thin
relay. The actual subprocess sandbox lives entirely inside the sandbox
container.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


logger = logging.getLogger("anila.functions.worker_api")
logging.basicConfig(level=logging.INFO)


JOBS_EXEC_DIR = Path(os.environ.get("JOBS_EXEC_DIR", "/jobs-exec"))
JOBS_EXTRACT_DIR = Path(os.environ.get("JOBS_EXTRACT_DIR", "/jobs-extract"))
EXEC_SOCKET = JOBS_EXEC_DIR / "control.sock"
EXTRACT_SOCKET = JOBS_EXTRACT_DIR / "control.sock"

API_SECRET = os.environ.get("ANILA_FUNCTIONS_API_SECRET", "")
SECRET_HEADER = "X-Functions-Api-Secret"


app = FastAPI(title="anila-functions-worker-api")


# ── Auth dependency ─────────────────────────────────────────────────────


def verify_secret(
    x_functions_api_secret: str | None = Header(None, alias=SECRET_HEADER)
) -> None:
    if not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="ANILA_FUNCTIONS_API_SECRET not configured on worker-api",
        )
    if x_functions_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="invalid api secret")


# ── Health ──────────────────────────────────────────────────────────────


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# ── /exec ───────────────────────────────────────────────────────────────


class ExecRequest(BaseModel):
    code: str
    body: dict
    valves: dict = {}
    user: dict = {}
    metadata: dict = {}


@app.post("/exec", dependencies=[Depends(verify_secret)])
async def exec_endpoint(payload: ExecRequest) -> StreamingResponse:
    """Forward to sandbox-exec via /jobs-exec/control.sock."""
    return StreamingResponse(
        _stream_from_sandbox(EXEC_SOCKET, payload.model_dump(), mode="exec"),
        media_type="text/event-stream",
    )


# ── /extract-meta ───────────────────────────────────────────────────────


class ExtractRequest(BaseModel):
    code: str


@app.post("/extract-meta", dependencies=[Depends(verify_secret)])
async def extract_meta_endpoint(payload: ExtractRequest) -> dict:
    """Forward to sandbox-extract; aggregate event stream into a single
    JSON response (CSP doesn't need streaming for save).
    """
    actions: list = []
    valves_schema: dict = {}
    metadata: dict = {}
    errors: list[str] = []
    strategy = "sandbox"

    job_spec = {
        "code": payload.code,
        "body": {},
        "valves": {},
        "user": {},
        "metadata": {},
        "mode": "extract",
    }
    async for line in _iter_lines_from_sandbox(EXTRACT_SOCKET, job_spec):
        try:
            event = json.loads(line.rstrip(b"\n").decode("utf-8"))
        except Exception as exc:  # pragma: no cover
            errors.append(f"bad line from sandbox: {exc}")
            continue
        kind = event.get("type")
        if kind == "extract_result":
            actions = event.get("actions") or []
            valves_schema = event.get("valves_schema") or {}
            metadata = event.get("metadata") or {}
            strategy = event.get("strategy") or strategy
        elif kind == "error":
            errors.append(event.get("message", "unknown error"))
        elif kind == "__done__":
            break

    return {
        "actions_meta_json": actions,
        "valves_schema_json": valves_schema,
        "metadata_json": metadata,
        "extract_strategy": strategy,
        "errors": errors,
    }


# ── Unix socket IPC helpers ─────────────────────────────────────────────


async def _stream_from_sandbox(
    socket_path: Path, payload: dict, mode: str
) -> AsyncIterator[bytes]:
    """Connect to sandbox Unix socket, send job, yield SSE bytes."""
    job = {**payload, "mode": mode}
    async for line in _iter_lines_from_sandbox(socket_path, job):
        # Wrap raw event line into SSE format for CSP / browser
        try:
            event = json.loads(line.rstrip(b"\n").decode("utf-8"))
        except Exception:
            continue
        if event.get("type") == "__done__":
            yield (
                b"event: function_done\ndata: "
                + json.dumps(
                    {"result": event.get("result")}, ensure_ascii=False
                ).encode("utf-8")
                + b"\n\n"
            )
            return
        yield (
            b"event: function_event\ndata: "
            + json.dumps(event, ensure_ascii=False).encode("utf-8")
            + b"\n\n"
        )


async def _iter_lines_from_sandbox(
    socket_path: Path, job: dict
) -> AsyncIterator[bytes]:
    """Open Unix socket, send one JobSpec line, yield each reply line."""
    import asyncio

    if not socket_path.exists():
        yield (
            json.dumps(
                {
                    "type": "error",
                    "message": f"sandbox socket missing: {socket_path}",
                }
            ).encode("utf-8")
            + b"\n"
        )
        yield b'{"type": "__done__", "result": null}\n'
        return

    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
        yield (
            json.dumps(
                {
                    "type": "error",
                    "message": f"sandbox connect failed: {exc}",
                }
            ).encode("utf-8")
            + b"\n"
        )
        yield b'{"type": "__done__", "result": null}\n'
        return

    try:
        writer.write(json.dumps(job, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                return
            yield line
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

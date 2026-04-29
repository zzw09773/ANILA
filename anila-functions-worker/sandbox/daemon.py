"""Sandbox daemon — Unix socket server, spawn-and-stream loop.

Listens on a Unix-domain socket inside the shared docker volume
(``/jobs-exec/control.sock`` or ``/jobs-extract/control.sock``).
Worker-api connects, writes one ``JobSpec`` JSON line, then reads the
event stream until the daemon emits ``__done__`` and closes.

Per-connection lifecycle:

  1. accept()
  2. read length-delimited or LF-terminated JobSpec line
  3. spawn ``python -u runtime.py`` as ``subproc`` user
     (Popen(user=..., group=..., preexec_fn=clear_ambient_caps))
  4. forward subprocess stdout lines back to the socket
  5. on subprocess exit: close connection
  6. if connection drops mid-run: SIGKILL the subprocess (no zombies)

Concurrency: bounded by ``MAX_CONCURRENT`` (8 for exec, 4 for extract).
Beyond that, accept() still happens but the handler immediately replies
with an ``error: queue_full`` event and closes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from sandbox.ambient import clear_ambient_caps
from shared.wire import DONE_EVENT_TYPE, ERROR_EVENT_TYPE, JobSpec, encode_event


logger = logging.getLogger("anila.functions.sandbox")

JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/jobs-exec"))
SOCKET_PATH = JOBS_DIR / "control.sock"
SUBPROC_UID = int(os.environ.get("SUBPROC_UID", "65534"))
SUBPROC_GID = int(os.environ.get("SUBPROC_GID", "65534"))
RUNTIME_SCRIPT = Path(__file__).parent / "runtime.py"

# Resource limits
MAX_TIMEOUT = int(os.environ.get("MAX_TIMEOUT", "30"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "8"))
MAX_STDOUT_KB = int(os.environ.get("MAX_STDOUT_KB", "256"))


_concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# ── Per-connection handler ──────────────────────────────────────────────


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """One IPC connection = one job invocation."""
    if _concurrency_semaphore.locked():
        await _send_event(writer, {"type": ERROR_EVENT_TYPE, "message": "queue_full"})
        await _send_event(writer, {"type": DONE_EVENT_TYPE, "result": None})
        writer.close()
        return

    async with _concurrency_semaphore:
        try:
            await _run_job(reader, writer)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("daemon connection handler crashed")
            try:
                await _send_event(
                    writer,
                    {
                        "type": ERROR_EVENT_TYPE,
                        "message": f"daemon: {type(exc).__name__}: {exc}",
                    },
                )
                await _send_event(
                    writer, {"type": DONE_EVENT_TYPE, "result": None}
                )
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def _run_job(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    raw_line = await reader.readline()
    if not raw_line:
        return  # client disconnected before sending spec

    try:
        spec = JobSpec.deserialize(raw_line.decode("utf-8"))
    except Exception as exc:
        await _send_event(
            writer,
            {"type": ERROR_EVENT_TYPE, "message": f"bad job spec: {exc}"},
        )
        await _send_event(writer, {"type": DONE_EVENT_TYPE, "result": None})
        return

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(RUNTIME_SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        user=SUBPROC_UID,
        group=SUBPROC_GID,
        preexec_fn=clear_ambient_caps,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(RUNTIME_SCRIPT.parent.parent),
            # Note: HTTP_PROXY etc. are inherited if set on the daemon's
            # container env (worker-net runs do, extract-net doesn't).
            "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
            "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
            "NO_PROXY": os.environ.get("NO_PROXY", ""),
        },
    )

    # Feed the spec to runtime.py via stdin
    assert proc.stdin is not None
    proc.stdin.write(spec.serialize().encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    forward_task = asyncio.create_task(
        _forward_subprocess_output(proc, writer)
    )

    try:
        await asyncio.wait_for(forward_task, timeout=MAX_TIMEOUT)
    except asyncio.TimeoutError:
        # Wall-clock exceeded — kill subprocess and report
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await _send_event(
            writer,
            {
                "type": ERROR_EVENT_TYPE,
                "message": f"timeout after {MAX_TIMEOUT}s",
            },
        )
        await _send_event(
            writer, {"type": DONE_EVENT_TYPE, "result": None}
        )
    finally:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _forward_subprocess_output(
    proc: asyncio.subprocess.Process,
    writer: asyncio.StreamWriter,
) -> None:
    """Tail subprocess stdout, forward each line to the IPC socket.

    Stops when subprocess closes stdout (EOF) or emits ``__done__``.
    Connection drops on the writer side surface as ``ConnectionError``;
    we kill the subprocess in that case so it doesn't keep running
    after the client gave up.
    """
    assert proc.stdout is not None
    bytes_seen = 0
    cap = MAX_STDOUT_KB * 1024
    while True:
        line = await proc.stdout.readline()
        if not line:
            return
        bytes_seen += len(line)
        if bytes_seen > cap:
            await _send_event(
                writer,
                {
                    "type": ERROR_EVENT_TYPE,
                    "message": f"stdout cap ({MAX_STDOUT_KB}KB) exceeded",
                },
            )
            await _send_event(
                writer, {"type": DONE_EVENT_TYPE, "result": None}
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return
        try:
            writer.write(line)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return
        # Stop early if subprocess emitted the done sentinel
        try:
            event = json.loads(line.decode("utf-8").rstrip("\n"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if event.get("type") == DONE_EVENT_TYPE:
            return


async def _send_event(writer: asyncio.StreamWriter, event: dict) -> None:
    writer.write(encode_event(event).encode("utf-8"))
    await writer.drain()


# ── Bootstrap ───────────────────────────────────────────────────────────


async def _serve() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    server = await asyncio.start_unix_server(
        handle_connection, path=str(SOCKET_PATH)
    )
    # Restrict socket access: owner = sandbox uid, group = anila-jobs.
    # Worker-api's ``web`` user is in anila-jobs supplementary group;
    # ``subproc`` user (user code) is NOT — so user code can't connect.
    os.chmod(SOCKET_PATH, 0o660)

    logger.info("sandbox daemon listening on %s", SOCKET_PATH)
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()

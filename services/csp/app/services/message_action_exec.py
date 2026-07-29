"""OW-3 exec surface — the entire in-process Python execution path.

docs/plans/ow3-message-actions-blueprint.md §Q3; risk acceptance at
docs/security/ow3-exec-risk-acceptance.md.

Honest capability statement (feeds the risk doc verbatim): runs inside the
csp FastAPI process as the container user, full stdlib + all installed
packages; can read ``os.environ`` (DB URL, ``MODEL_GATEWAY_API_KEY``,
``CSP_SERVICE_TOKEN``, JWT key paths), read files incl. ``secrets/*.pem``
(``:ro`` still readable), open its own DB connection as the app role
(bypassing API-layer scoping), import/mutate ``app.*``, make arbitrary
outbound network calls **bypassing ``url_guard.validate_outbound_url``**
(SSRF allow-list), spawn subprocesses, monkey-patch the process. **No
sandbox, no seccomp, no separate interpreter, no resource limits beyond
wall-clock timeout + output cap. Whoever can author an exec action is
equivalent to whoever can deploy code to the host.**

Anti-claims: timeout cannot kill the thread (CPython); on timeout → 504 +
audit row, thread keeps running. The semaphore bounds abandoned workers:
a stuck worker permanently consumes a slot until it returns; exhausting
all slots disables further exec (503) until workers finish or the service
restarts — intended fail-closed trade. Never claim 沙箱 / 已隔離 / 已終止.

Save-time ``validate_source()`` compiles + AST-checks and NEVER executes.
Runtime = ``run_in_executor`` + ``wait_for`` + semaphore + output cap;
compiled cache keyed ``(action_id, version)``.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import time
import traceback
from typing import Any

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


class NonStrReturnError(Exception):
    """``run(ctx)`` returned a non-str value; service maps to HTTP 502."""


# Compiled bytecode cache: (action_id, version) → code object.
_COMPILED_CACHE: dict[tuple[int, int], Any] = {}

_SEMAPHORE: asyncio.Semaphore | None = None

# Workers abandoned after 504 that still hold a semaphore slot.
_ABANDONED_WORKERS: int = 0


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(
            int(settings.ANILA_ACTION_EXEC_MAX_CONCURRENCY)
        )
    return _SEMAPHORE


def reset_runtime_state_for_tests() -> None:
    """Clear compiled cache + recreate semaphore (tests only)."""
    global _SEMAPHORE, _ABANDONED_WORKERS
    _COMPILED_CACHE.clear()
    _SEMAPHORE = None
    _ABANDONED_WORKERS = 0


def validate_source(source: str) -> None:
    """Compile + AST-check for a top-level ``def run``. NEVER executes.

    Raises HTTPException 400 with the blueprint §4 zh-TW detail strings.
    """
    try:
        tree = ast.parse(source, filename="<message_action>", mode="exec")
    except SyntaxError as exc:
        line = exc.lineno or 0
        msg = exc.msg or "syntax error"
        raise HTTPException(
            status_code=400,
            detail=f"動作程式碼語法錯誤（第 {line} 行）：{msg}",
        ) from exc

    has_run = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
            break
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            # Async run is not in the contract; reject so authors don't
            # think await works inside to_thread.
            raise HTTPException(
                status_code=400,
                detail="動作程式碼必須定義 run(ctx) 函式",
            )
    if not has_run:
        raise HTTPException(
            status_code=400,
            detail="動作程式碼必須定義 run(ctx) 函式",
        )

    # Compile only — never exec. Confirms bytecode is producible.
    compile(source, "<message_action>", "exec")


def get_compiled(action_id: int, version: int, source: str) -> Any:
    key = (action_id, version)
    cached = _COMPILED_CACHE.get(key)
    if cached is not None:
        return cached
    code = compile(source, f"<message_action:{action_id}:v{version}>", "exec")
    _COMPILED_CACHE[key] = code
    return code


def invalidate_cache(action_id: int) -> None:
    stale = [k for k in _COMPILED_CACHE if k[0] == action_id]
    for k in stale:
        del _COMPILED_CACHE[k]


def _execute_sync(code: Any, ctx: dict) -> Any:
    """Run ``run(ctx)`` in a worker thread; return value unchecked here."""
    namespace: dict[str, Any] = {"__name__": "<message_action>"}
    exec(code, namespace)  # noqa: S102 — intentional OW-3 exec surface
    run_fn = namespace.get("run")
    if not callable(run_fn):
        raise TypeError("run is not callable")
    return run_fn(ctx)


async def run_action(
    *,
    action_id: int,
    version: int,
    source: str,
    ctx: dict,
) -> tuple[str, bool, int]:
    """Execute with semaphore + wall-clock timeout + output cap.

    Returns ``(output, truncated, duration_ms)``.

    Raises:
      HTTPException 503 semaphore exhausted (all slots held, incl. abandoned)
      HTTPException 504 timeout (thread may still be running — slot held)
      NonStrReturnError when ``run`` returns a non-str (service → 502)
      Other worker exceptions propagate (service wraps generic 502)
    """
    global _ABANDONED_WORKERS

    timeout = float(settings.ANILA_ACTION_EXEC_TIMEOUT_SECONDS)
    max_chars = int(settings.ANILA_ACTION_OUTPUT_MAX_CHARS)
    sem = _semaphore()

    try:
        await asyncio.wait_for(sem.acquire(), timeout=2.0)
    except asyncio.TimeoutError as exc:
        logger.warning(
            "message_action_exec semaphore exhausted action_id=%s "
            "abandoned_workers=%s",
            action_id,
            _ABANDONED_WORKERS,
        )
        raise HTTPException(
            status_code=503,
            detail="平台忙碌中，請稍後再試",
        ) from exc

    abandoned = False
    released = False
    fut: Any = None
    started = time.monotonic()

    def _release_slot(_f: Any = None) -> None:
        nonlocal released
        global _ABANDONED_WORKERS
        if released:
            return
        released = True
        if _f is not None:
            try:
                _f.exception()
            except Exception:
                pass
        if abandoned:
            _ABANDONED_WORKERS = max(0, _ABANDONED_WORKERS - 1)
        sem.release()

    try:
        code = get_compiled(action_id, version, source)
        loop = asyncio.get_running_loop()
        # run_in_executor replaces to_thread to obtain a future handle;
        # contextvars are therefore not propagated into the worker.
        fut = loop.run_in_executor(None, _execute_sync, code, ctx)
        raw = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if fut is not None and fut.done():
            # Worker itself raised TimeoutError — work finished.
            _release_slot()
            raise
        # Wall-clock timeout: keep the slot until abandoned work finishes.
        abandoned = True
        _ABANDONED_WORKERS += 1
        fut.add_done_callback(_release_slot)
        raise HTTPException(
            status_code=504,
            detail=(
                f"動作執行逾時（{int(timeout)} 秒），已停止等待；"
                "背景可能仍在執行"
            ),
        ) from exc
    except BaseException:
        # Finished work (incl. SystemExit) → return slot now.
        # Cancel while work continues → hold slot like timeout.
        if fut is not None and not fut.done():
            abandoned = True
            _ABANDONED_WORKERS += 1
            fut.add_done_callback(_release_slot)
        else:
            _release_slot()
        raise
    else:
        _release_slot()

    if not isinstance(raw, str):
        raise NonStrReturnError("動作回傳值必須是字串")
    result = raw

    duration_ms = int((time.monotonic() - started) * 1000)
    truncated = False
    if len(result) > max_chars:
        result = result[:max_chars]
        truncated = True
    return result, truncated, duration_ms


def format_traceback() -> str:
    return traceback.format_exc()

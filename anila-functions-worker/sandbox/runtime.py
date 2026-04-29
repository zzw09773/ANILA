"""User-code execution wrapper.

Runs as a freshly-spawned subprocess for every Action invocation. The
sandbox daemon ``Popen``s us with ``user='subproc'`` (uid 65534) so we
inherit no SETUID/SETGID capability and no anila-jobs supplementary
group. Reads a single ``JobSpec`` from stdin, executes the user code,
emits events to stdout one-per-line.

Two modes (selected by ``JobSpec.mode``):

* ``exec``     — instantiate ``Action()``, optionally hydrate
                 ``Action.valves`` from a ``Valves(BaseModel)`` if user
                 declared one, then ``await Action.action(body,
                 __event_emitter__, __user__, __metadata__)``
* ``extract``  — *static-then-dynamic* schema introspection (delegates
                 to ``sandbox.extract.run_extract``); never executes
                 ``Action.action`` so module top-level side effects are
                 the only attack surface — and the extract container's
                 network has zero egress, so even those go nowhere

The wrapper *itself* swallows every user-code exception and turns it
into an ``error`` event so the daemon's per-connection loop sees a
clean exit. The sentinel ``__done__`` event always fires last (in
``finally``) so worker-api knows when to close the SSE channel.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any

# Subprocess runs from the sandbox image's WORKDIR (``/app``); ``shared``
# is on PYTHONPATH because the image copies it as a top-level package.
from shared.wire import (
    DONE_EVENT_TYPE,
    ERROR_EVENT_TYPE,
    JobSpec,
)


# ── Event helpers ───────────────────────────────────────────────────────


async def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def emit_error(message: str) -> None:
    await emit({"type": ERROR_EVENT_TYPE, "message": message})


async def emit_done(result: Any = None) -> None:
    await emit({"type": DONE_EVENT_TYPE, "result": result})


# ── Mode dispatch ───────────────────────────────────────────────────────


async def run_exec(spec: JobSpec, user_ns: dict) -> None:
    """exec mode: build Action(), inject reserved args, await action()."""
    action_cls = user_ns.get("Action")
    if action_cls is None:
        await emit_error("missing Action class")
        return

    instance = action_cls()
    valves_cls = user_ns.get("Valves")
    if valves_cls is not None:
        try:
            instance.valves = valves_cls(**spec.valves)
        except Exception as exc:
            await emit_error(f"valves init failed: {exc}")
            # Continue with bare instance — user code may not need valves

    try:
        result = await instance.action(
            body=spec.body,
            __event_emitter__=emit,
            __user__=spec.user,
            __metadata__=spec.metadata,
        )
        await emit_done(result)
    except Exception as exc:
        await emit_error(
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        await emit_done(None)


async def run_extract(spec: JobSpec, user_ns: dict) -> None:
    """extract mode: introspect Action.actions + Valves.schema only.

    Implementation deferred to ``sandbox.extract.run_extract`` which
    does the static-AST stage 1 first and only falls through to dynamic
    introspection (this path) if AST analysis flags a non-literal.
    """
    # Late import — runtime.py is the entrypoint for the *exec* sandbox
    # which doesn't ship extract.py; only the extract container has it.
    from sandbox.extract import run_extract as _do_extract  # noqa: WPS433
    await _do_extract(spec, user_ns, emit, emit_error, emit_done)


# ── Main ────────────────────────────────────────────────────────────────


async def main() -> None:
    raw = sys.stdin.read()
    try:
        spec = JobSpec.deserialize(raw)
    except Exception as exc:
        await emit_error(f"bad job spec: {exc}")
        await emit_done(None)
        return

    user_ns: dict = {"__name__": "__user_function__"}
    try:
        compiled = compile(spec.code, "<user_function>", "exec")
        exec(compiled, user_ns)
    except Exception as exc:
        await emit_error(
            f"compile/exec: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
        await emit_done(None)
        return

    if spec.mode == "extract":
        await run_extract(spec, user_ns)
    else:
        await run_exec(spec, user_ns)


if __name__ == "__main__":
    asyncio.run(main())

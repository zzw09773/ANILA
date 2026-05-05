"""Convenience factory: derive a memory reader from CallerContext.

Without this, every agent has to repeat the same boilerplate:

    if caller.has_callback_credentials:
        reader = HttpUserFactReader(
            base_url=caller.csp_base_url,
            service_token=caller.service_token,
        )

The factory collapses that to one call and returns ``None`` when
the caller lacks the required fields, so agent code can do:

    reader = make_user_memory_reader(ctx.caller)
    facts = await reader.get_user_facts(ctx.caller.user_id) if reader else []

The lazy / nullable pattern keeps agent dev paths (no proxy in
front, no service token) from blowing up when they touch memory
code — they just see "no facts available" and continue.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .http_user_facts import HttpUserFactReader

if TYPE_CHECKING:
    from anila_core.api.caller_context import CallerContext


def make_user_memory_reader(
    caller: "Optional[CallerContext]",
    *,
    timeout_seconds: float = 5.0,
) -> Optional[HttpUserFactReader]:
    """Return a configured :class:`HttpUserFactReader` or ``None``.

    Returns ``None`` when ``caller`` is missing any of:

    * ``user_id`` (don't know whose memory to read)
    * ``service_token`` (no auth to call CSP)
    * ``csp_base_url`` (no endpoint configured)

    The agent runtime treats ``None`` as "memory not available";
    it's the documented degradation path for dev / curl scenarios.
    """
    if caller is None or not caller.has_callback_credentials:
        return None
    # has_callback_credentials guards the three required fields,
    # but type checkers see them as ``Optional[...]`` — the asserts
    # narrow the types for the constructor call below.
    assert caller.csp_base_url is not None
    assert caller.service_token is not None
    return HttpUserFactReader(
        base_url=caller.csp_base_url,
        service_token=caller.service_token,
        timeout_seconds=timeout_seconds,
    )

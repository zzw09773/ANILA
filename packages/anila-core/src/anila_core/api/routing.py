"""The one place that answers "which endpoint is this request for?".

Every security or routing decision that keys off the request path must read
the same string the router dispatches on. That string is ``scope["path"]``,
written by the ASGI server from the request target alone.

It must never be ``request.url.path``. ``request.url`` is derived from the
caller's ``Host`` header — on starlette < 1.0.1 it is literally
``f"{scheme}://{host_header}{path}"`` (CVE-2026-48710) — so a request sent
with ``Host: attacker.example/api/auth/login`` makes ``url.path`` report an
exempt prefix while the router keeps dispatching to the real endpoint. Six
call sites across csp, this package and the Router each made that mistake
independently; consolidating them here means there is one line to keep
correct instead of six.

⚠ Reverting the body of :func:`routed_path` to ``request.url.path`` is
**invisible on starlette >= 1.0.1**, where the two strings are always equal
and no ``Host`` header can tell them apart. What keeps the revert
observable is a set of tests that substitute the public ``url`` property to
stand in for a future library regression — see
``services/csp/tests/test_csrf_host_header_bypass.py``,
``packages/anila-core/tests/test_middleware_public_path_host_header.py`` and
``services/anila-core-router/tests/test_primary_gate.py``. Deleting those
fixtures as "contrived" would silently remove the only coverage this
function has on the version we ship.

Caveat: the value is ``scope["path"]`` verbatim, which equals the router's
route path only while ``scope["root_path"]`` is empty. That holds for every
ANILA service today — nothing passes ``--root-path`` and nothing is mounted
under a sub-path.

If a ``root_path`` is ever introduced, **the fail direction depends on how
the caller compares**, and it is not uniform:

- Callers matching a *prefix* or testing *set membership* stop matching and
  fail closed — CSRF gets enforced on ``/api/auth/login`` (a 403), a service
  token gets demanded for ``/health``. Loud, and safe.
- A caller comparing for *equality* fails **open**: the comparison simply
  misses and the guarded branch is skipped. Measured — mounting the Router
  under ``/router`` makes ``_gate_on_primary`` return ``401 Missing Bearer
  API key`` instead of its ``503`` administrator hint, i.e. the gate is
  skipped rather than tightened. That particular gate only costs an
  operator hint, but the shape is general: an ``==`` against a literal path
  is not self-protecting, and a future one guarding something that matters
  would be an authorisation hole.

So: five of the six current callers fail closed; the Router's equality gate
does not. Anything new that compares with ``==`` needs its own thought here,
not an assumption inherited from this docstring.
"""

from __future__ import annotations

from starlette.requests import Request

__all__ = ["routed_path"]


def routed_path(request: Request) -> str:
    """Return the request path the router will dispatch on."""
    return request.scope["path"]

"""Collection product-surface provenance (inventory partitioning).

ANILA has two collection-facing products over one ``ingestion_collections``
table:

* CSP governance UI (``apps/csp-governance-ui``) → ``/api/ingestion/...``
* ANILALM personal knowledge base (``apps/anilalm``) → ``/api/personal/...``

``origin`` records WHERE a collection was created. A collection created in
CSP must not appear in ANILALM's "你的知識庫" even when the same user owns
it — a developer may create one for a project rather than as personal KB.

⚠ This is NOT an authorization control. The same user has the same rights
on both surfaces; nobody is denied access to their own data. Do not build
a role matrix here, and do not confuse this with ownership checks,
clearance, compartments, need-to-know, or the classification latch.
Future readers: if you are looking for a security boundary, look elsewhere.

Resolver contract
-----------------
``lookup_collection_for_surface`` / ``_require_collection_access`` take
``origin`` as a **required keyword**. Callers must decide:

* ``origin=SURFACE_CSP`` / ``SURFACE_ANILALM`` — product shelf filter
* ``origin=ANY_SURFACE`` — deliberate cross-product resolution (named,
  greppable; never an omitted / None ambient)

A forgotten decision is a TypeError at the call site, not a silent
cross-product leak. Dual-mounted routes may still use the URL-prefix
middleware + ``require_surface_origin()`` as a convenience to *supply*
that required argument; the prefix is not the load-bearing gate.

Retrieval / RLS stay keyed on the numeric collection id via
``anila.collection_id``. Origin never enters that path — an agent bound
to a collection keeps working regardless of origin.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Annotated, Literal, Union

from fastapi import Depends, Request
from starlette.types import ASGIApp, Receive, Scope, Send

CollectionOrigin = Literal["csp", "anilalm"]

SURFACE_CSP: CollectionOrigin = "csp"
SURFACE_ANILALM: CollectionOrigin = "anilalm"
VALID_ORIGINS: frozenset[str] = frozenset({SURFACE_CSP, SURFACE_ANILALM})

# Deliberate cross-product marker. Pass as ``origin=ANY_SURFACE`` — never
# rely on a missing / None ambient value to mean "all products".
ANY_SURFACE: Literal["*"] = "*"

OriginArg = Union[CollectionOrigin, Literal["*"]]

# Absolute URL prefixes after dual-mount. Relative route paths live under these.
GOVERNANCE_PREFIX = "/api/ingestion"
PERSONAL_PREFIX = "/api/personal"

# Paths under GOVERNANCE_PREFIX that are NOT collection-surface inventory
# (single-mount helpers; no origin filter).
_GOVERNANCE_NON_SURFACE_PREFIXES = (
    "/api/ingestion/users/",
    "/api/ingestion/chunking-preview",
)

_current_origin: ContextVar[CollectionOrigin | None] = ContextVar(
    "collection_surface_origin", default=None
)


def origin_for_path(path: str) -> CollectionOrigin | None:
    """Map a request path to a product-surface origin (or None)."""
    if path == PERSONAL_PREFIX or path.startswith(PERSONAL_PREFIX + "/"):
        return SURFACE_ANILALM
    if path == GOVERNANCE_PREFIX or path.startswith(GOVERNANCE_PREFIX + "/"):
        if any(path.startswith(p) for p in _GOVERNANCE_NON_SURFACE_PREFIXES):
            return None
        return SURFACE_CSP
    return None


def require_surface_origin() -> CollectionOrigin:
    """Return the ambient dual-mount origin or raise if unset.

    Convenience for handlers under ``/api/ingestion`` / ``/api/personal``.
    The value must still be passed explicitly into the resolver as
    ``origin=...`` — this helper only *supplies* the required argument.
    """
    origin = _current_origin.get()
    if origin is None:
        raise RuntimeError(
            "collection surface origin unset — route must be mounted under "
            f"{GOVERNANCE_PREFIX} (origin=csp) or {PERSONAL_PREFIX} "
            "(origin=anilalm), or pass origin= explicitly"
        )
    return origin


def surface_origin_dep(request: Request) -> CollectionOrigin:
    """FastAPI dependency: resolve origin from the request URL path."""
    origin = origin_for_path(request.url.path)
    if origin is None:
        raise RuntimeError(
            f"path {request.url.path!r} is not a collection product surface"
        )
    return origin


SurfaceOrigin = Annotated[CollectionOrigin, Depends(surface_origin_dep)]


class CollectionSurfaceMiddleware:
    """Bind ambient origin from the URL prefix for dual-mounted handlers.

    Pure ASGI (not BaseHTTPMiddleware) so StreamingResponse / SSE is not
    buffered. ContextVar feeds ``require_surface_origin()`` only; the
    resolver never reads this ambient value on its own.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = origin_for_path(scope.get("path", ""))
        token: Token | None = None
        if origin is not None:
            token = _current_origin.set(origin)
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                _current_origin.reset(token)

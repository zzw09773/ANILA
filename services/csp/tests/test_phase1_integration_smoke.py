"""Phase 1 integration smoke — confirm all 5 new endpoints land on the
FastAPI app's OpenAPI surface after main thread wires the routers.

This is a cross-cutting check rather than per-endpoint behaviour (each
endpoint has its own dedicated contract test). It catches the failure mode
of "subagent wrote the router but main thread forgot to include_router it",
which would otherwise be a silent 404 under production.

The 5 endpoints (Phase 1 deliverables):
  1. POST /api/ingestion/collections/{collection_id}/search       (chunk search)
  2. POST /api/ingestion/collections/{collection_id}/images/search (image search)
  3. GET  /api/ingestion/images/{image_id}/blob                    (image blob)
  4. GET  /.well-known/jwks.json                                   (JWKS)
  5. POST /api/auth/revoke                                         (admin force-revoke)
  6. GET  /api/auth/revocations                                    (cold-start sync)
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.main import app


REQUIRED_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/ingestion/collections/{collection_id}/search"):
        "chunk search (existed pre-extraction; contract locked in Phase 1)",
    ("POST", "/api/ingestion/collections/{collection_id}/images/search"):
        "image search (Phase 1A new)",
    ("GET", "/api/ingestion/images/{image_id}/blob"):
        "image blob (Phase 1A new)",
    ("GET", "/.well-known/jwks.json"):
        "JWKS (Phase 1C new)",
    ("POST", "/api/auth/revoke"):
        "admin force-revoke (Phase 1D new)",
    ("GET", "/api/auth/revocations"):
        "revocations cold-start sync (Phase 1D new)",
}


def _iter_mounted_routes(routes, prefix: str = ""):
    """Yield ``(method, path)`` including routers wrapped by FastAPI 0.141.

    ``include_router`` stores an ``_IncludedRouter`` on ``app.routes``.
    That wrapper has no ``methods`` and no ``routes``; the real table is
    ``original_router`` and the prefix is ``include_context.prefix``.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        include_context = getattr(route, "include_context", None)
        if original is not None and include_context is not None:
            child_prefix = prefix + (getattr(include_context, "prefix", "") or "")
            yield from _iter_mounted_routes(original.routes, child_prefix)
            low = getattr(original, "_low_priority_routes", None)
            if low:
                yield from _iter_mounted_routes(low, child_prefix)
            continue
        path = prefix + (getattr(route, "path", "") or "")
        if hasattr(route, "routes") and route.routes:
            yield from _iter_mounted_routes(route.routes, path)
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            yield method, path


def _route_index() -> set[tuple[str, str]]:
    """All (method, path) pairs mounted on the FastAPI app."""
    return set(_iter_mounted_routes(app.routes))


@pytest.mark.parametrize(
    "method,path",
    list(REQUIRED_ROUTES.keys()),
    ids=lambda pair: f"{pair}" if isinstance(pair, tuple) else str(pair),
)
def test_phase1_endpoint_is_mounted(method, path):
    """Each Phase 1 endpoint must be present on the production router.

    A missing route here means a router file exists but ``api_router.include_router``
    was not called for it — a silent 404 risk in prod that the per-endpoint
    contract test would not catch because contract tests construct their own
    isolated FastAPI app.
    """
    index = _route_index()
    assert (method, path) in index, (
        f"Phase 1 endpoint {method} {path} is not mounted — "
        f"check that the corresponding include_router(...) call is in "
        f"services/csp/app/api/router.py"
    )


def test_phase1_endpoint_count_matches():
    """Sanity: at least the 6 documented Phase 1 routes are present."""
    index = _route_index()
    missing = [pair for pair in REQUIRED_ROUTES if pair not in index]
    assert not missing, f"missing Phase 1 routes: {missing}"


def test_openapi_schema_renders_with_new_endpoints():
    """OpenAPI generation must not crash with new schemas registered.

    This catches Pydantic v2 schema generation errors that arise when a
    new request/response model has a config conflict.
    """
    schema = app.openapi()
    assert "paths" in schema
    # Spot check: at least one of our new paths shows up.
    assert "/api/ingestion/images/{image_id}/blob" in schema["paths"]
    assert "/.well-known/jwks.json" in schema["paths"]

"""Turn UnsafeEndpointError into the HTTP detail the console already understands."""

from __future__ import annotations

from anila_core.security.url_guard import REASON_HOST_NOT_TRUSTED, UnsafeEndpointError

HOST_NOT_TRUSTED = "host_not_trusted"


def unsafe_endpoint_http_detail(exc: UnsafeEndpointError, *, hint: str | None = None):
    """Structured detail only when the console can act on it.

    ``host_not_trusted`` is the private-IP case where the env switch is
    already on and the URL host is absent from the trusted-host list.
    Name-shape failures stay ``untrusted_host``. Everything else, including
    the private-endpoint switch being off, stays a plain string.
    """
    if exc.reason == REASON_HOST_NOT_TRUSTED and exc.host:
        return {
            "code": HOST_NOT_TRUSTED,
            "host": exc.host,
            "message": str(exc),
        }
    if exc.fixable_by_trust_host:
        detail = {
            "code": "untrusted_host",
            "host": exc.host,
            "reason": exc.reason,
            "message": str(exc),
        }
        if hint is not None:
            detail["hint"] = hint
        return detail
    return str(exc)

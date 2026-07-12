"""Call-time outbound SSRF guard for the CSP proxy.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). SECURITY-CRITICAL: ``_guard_outbound`` is the
call-time SSRF re-validation (TOCTOU / DNS-rebinding defense) — moved
unchanged.
"""
from fastapi import HTTPException

from anila_security import (
    ENDPOINT_KIND_GENERIC,
    UnsafeEndpointError,
    validate_outbound_url,
)


def _guard_outbound(url: str, endpoint_kind: str = ENDPOINT_KIND_GENERIC) -> None:
    """Call-time SSRF re-validation before forwarding to a stored endpoint URL.

    Agent / model ``endpoint_url`` is validated at registration, but DNS
    rebinding (TOCTOU) can change what the host resolves to between then and
    the actual forward. Re-run the central guard at call time. Trusted internal
    hosts (``ANILA_TRUSTED_HOSTS``) keep their narrow allow-list semantics for
    operator-owned intranet DNS answers, while loopback / metadata / link-local
    destinations still fail closed. Translates the typed guard error into a 502
    — the upstream endpoint is unsafe to reach.
    """
    try:
        validate_outbound_url(url, endpoint_kind=endpoint_kind)
    except UnsafeEndpointError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"上游端點未通過出向安全驗證: {exc}",
        ) from exc

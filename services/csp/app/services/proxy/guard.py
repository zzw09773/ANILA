"""Call-time outbound SSRF guard for the CSP proxy.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). SECURITY-CRITICAL: ``_guard_outbound`` is the
call-time SSRF re-validation (TOCTOU / DNS-rebinding defense) — moved
unchanged.
"""
from fastapi import HTTPException

from anila_core.security import UnsafeEndpointError, validate_outbound_url

def _guard_outbound(url: str) -> None:
    """Call-time SSRF re-validation before forwarding to a stored endpoint URL.

    Agent / model ``endpoint_url`` is validated at registration, but DNS
    rebinding (TOCTOU) can change what the host resolves to between then and
    the actual forward. Re-run the central guard at call time. Trusted internal
    hosts (``ANILA_TRUSTED_HOSTS``) short-circuit before any DNS lookup, so this
    is cheap on the proxy hot path; only untrusted BYO endpoints pay a
    resolution. Translates the typed guard error into a 502 — the upstream
    endpoint is unsafe to reach.
    """
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"上游端點未通過出向安全驗證: {exc}",
        ) from exc

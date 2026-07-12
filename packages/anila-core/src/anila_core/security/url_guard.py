"""Compatibility facade for :mod:`anila_security.url_guard`.

All stateful behavior, including the trusted-host provider registry, lives in
``anila-security``.  Re-exporting the same objects here prevents the legacy
and canonical import paths from developing separate security state.
"""

from anila_security.url_guard import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    FIXABLE_BY_TRUST_HOST,
    REASON_DENY_HOST,
    REASON_EMPTY,
    REASON_INTERNAL_ZONE,
    REASON_NO_HOSTNAME,
    REASON_PRIVATE_IP,
    REASON_SCHEME,
    REASON_SINGLE_LABEL,
    REASON_UNSAFE_IP,
    UnsafeEndpointError,
    clear_trusted_host_providers,
    register_trusted_host_provider,
    validate_outbound_url,
)

__all__ = [
    "ENDPOINT_KIND_AGENT",
    "ENDPOINT_KIND_GENERIC",
    "ENDPOINT_KIND_MODEL",
    "FIXABLE_BY_TRUST_HOST",
    "REASON_DENY_HOST",
    "REASON_EMPTY",
    "REASON_INTERNAL_ZONE",
    "REASON_NO_HOSTNAME",
    "REASON_PRIVATE_IP",
    "REASON_SCHEME",
    "REASON_SINGLE_LABEL",
    "REASON_UNSAFE_IP",
    "UnsafeEndpointError",
    "clear_trusted_host_providers",
    "register_trusted_host_provider",
    "validate_outbound_url",
]

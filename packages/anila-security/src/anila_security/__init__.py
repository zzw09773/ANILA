"""Security primitives shared across ANILA services.

The package deliberately has no dependency on CSP, anila-core, a database
schema, or a web framework.  Service layers translate these primitives into
their own HTTP and persistence contracts.
"""

from anila_security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
    legacy_fallback_count,
    reset_legacy_fallback_count,
)
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
    "decrypt_credential",
    "encrypt_credential",
    "legacy_fallback_count",
    "register_trusted_host_provider",
    "reset_legacy_fallback_count",
    "validate_outbound_url",
]

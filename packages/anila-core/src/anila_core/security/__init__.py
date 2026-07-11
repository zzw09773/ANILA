"""Backward-compatible facade for the standalone :mod:`anila_security`.

New service code must import ``anila_security`` directly.  This namespace is
kept so published ``anila-core`` users retain their existing import contract.
"""

from anila_security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    clear_trusted_host_providers,
    decrypt_credential,
    encrypt_credential,
    legacy_fallback_count,
    register_trusted_host_provider,
    reset_legacy_fallback_count,
    validate_outbound_url,
)

__all__ = [
    "decrypt_credential",
    "encrypt_credential",
    "legacy_fallback_count",
    "reset_legacy_fallback_count",
    "UnsafeEndpointError",
    "validate_outbound_url",
    "register_trusted_host_provider",
    "clear_trusted_host_providers",
    "ENDPOINT_KIND_MODEL",
    "ENDPOINT_KIND_AGENT",
    "ENDPOINT_KIND_GENERIC",
]

"""Security primitives shared across services.

Sprint 5 / Chunk X:
- ``credential_crypto`` for AES-256-GCM encrypted credential columns.
  CSP backend uses it at create time; ingestion-worker uses it at
  judge-call time.
- ``url_guard`` rejects user-supplied endpoint URLs that point at
  private / loopback / metadata addresses (SSRF defense for the
  Chunking Evaluator's BYO LLM credentials).
"""

from anila_core.security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
)
from anila_core.security.upstream_urls import (
    join_upstream_path,
    strip_trailing_api_version,
)
from anila_core.security.url_guard import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    clear_dns_cache,
    clear_trusted_host_providers,
    register_trusted_host_provider,
    validate_outbound_url,
)

__all__ = [
    "decrypt_credential",
    "encrypt_credential",
    "UnsafeEndpointError",
    "validate_outbound_url",
    "register_trusted_host_provider",
    "clear_trusted_host_providers",
    "clear_dns_cache",
    "ENDPOINT_KIND_MODEL",
    "ENDPOINT_KIND_AGENT",
    "ENDPOINT_KIND_GENERIC",
    "join_upstream_path",
    "strip_trailing_api_version",
]

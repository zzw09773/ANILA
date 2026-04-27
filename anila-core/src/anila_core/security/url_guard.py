"""Outbound URL validator for user-supplied endpoint_url fields.

Sprint 5 / Chunk X security review (H1): user_llm_credentials lets the
caller register an arbitrary endpoint URL. The Chunking Evaluator's
worker then issues outbound POSTs to it. Without scheme + host
filtering, any logged-in user can register
``http://csp-db:5432`` (or other internal services) and turn the worker
into a blind SSRF cannon: writes happen even though responses don't
flow back to the attacker.

This module is the central allow-list. Both CSP (at credential
create / update) and the worker (defense in depth at call time)
should call ``validate_outbound_url(url)`` and reject on raise.

Policy (current):
- Scheme must be ``https`` in production. ``http`` accepted only when
  ``ANILA_ALLOW_HTTP_ENDPOINT=1`` (dev / on-prem with TLS-terminating
  proxy in front).
- Hostname must resolve to a globally-routable address. RFC 1918,
  loopback, link-local, multicast, reserved blocks are blocked.
- Hostname literals matching the deny list (localhost, 169.254.169.254,
  *.internal, *.local) are blocked even before DNS resolution.

The DNS check is best-effort and runs synchronously — endpoint URLs are
registered rarely (once per BYO LLM key), not per-request, so the
cost is acceptable. A determined attacker can still race
DNS rebinding (TOCTOU) but at that point they need DNS authority for
a resolvable name; far higher bar than `host=csp-db`.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


_ALLOW_HTTP = os.environ.get("ANILA_ALLOW_HTTP_ENDPOINT") == "1"

# Hostnames that should never appear in a credential URL, even if they
# don't resolve. Catches typos / docker-compose service names that
# bypass DNS (resolved by the docker embedded resolver instead).
_DENY_HOSTS = frozenset({
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "broadcasthost",
    "169.254.169.254",  # cloud metadata
    "metadata.google.internal",
    "metadata",
})

# Suffixes that signal docker / k8s / mDNS internal-only zones.
_DENY_HOST_SUFFIXES: tuple[str, ...] = (
    ".internal",
    ".local",
    ".localdomain",
    ".cluster.local",
    ".svc",
    ".svc.cluster.local",
)


class UnsafeEndpointError(ValueError):
    """Raised when a user-supplied URL is unsafe for outbound calls."""


def _is_ip_literal(addr: str) -> bool:
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        return False


def _is_private_or_special(addr: str) -> bool:
    """True for any IP that should never be a legitimate LLM endpoint."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_outbound_url(url: str) -> None:
    """Raise ``UnsafeEndpointError`` if the URL would be unsafe to POST to.

    Caller is expected to translate the exception into the appropriate
    framework error (HTTPException 400 in CSP, log + skip in worker).
    """
    if not url or not isinstance(url, str):
        raise UnsafeEndpointError("endpoint_url must be a non-empty string")

    parsed = urlparse(url.strip())

    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        if not _ALLOW_HTTP:
            raise UnsafeEndpointError(
                "endpoint_url scheme must be 'https' "
                "(set ANILA_ALLOW_HTTP_ENDPOINT=1 in dev to relax)"
            )
    else:
        raise UnsafeEndpointError(
            f"endpoint_url scheme {parsed.scheme!r} not allowed "
            f"(use https://)"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeEndpointError("endpoint_url has no hostname")

    if host in _DENY_HOSTS:
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is on the deny list "
            f"(loopback / metadata / mDNS)"
        )

    if any(host.endswith(suffix) for suffix in _DENY_HOST_SUFFIXES):
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is in an internal-only zone"
        )

    # IP literal: validate directly without DNS round-trip.
    if _is_private_or_special(host):
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a private / loopback / "
            f"link-local / metadata IP"
        )

    # Single-label hostnames (no dot, e.g. "csp-db", "redis", "router")
    # are docker-compose / k8s service names — never legitimate public
    # endpoints. We block them up-front so we don't depend on the DNS
    # check below (which can be racy or unavailable at create-time).
    if "." not in host and not _is_ip_literal(host):
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a single-label name "
            f"(typo / docker service name?). Use a fully-qualified "
            f"public hostname."
        )

    # Hostname: resolve and reject if any answer is private. We intentionally
    # check ALL records (not just the first) so a multi-A record that
    # alternates public + private doesn't sneak through.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable — let the actual call fail. We don't want to block
        # legitimate transient DNS outages at credential-create time.
        return

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0] if sockaddr else ""
        if addr and _is_private_or_special(addr):
            raise UnsafeEndpointError(
                f"endpoint_url host {host!r} resolves to private/special "
                f"address {addr!r}"
            )

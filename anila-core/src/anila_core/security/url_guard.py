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
- Hostname must resolve to a globally-routable address. Loopback,
  link-local, multicast, reserved, and unspecified blocks are blocked
  unconditionally.
- RFC 1918 private IPs (10/8, 172.16/12, 192.168/16) are blocked by
  default, but ``ANILA_ALLOW_PRIVATE_ENDPOINT=1`` opts ON-PREM dev
  deployments into accepting them — agents on internal LAN are a
  legitimate first-class case for this platform.
- Hostname literals matching the deny list (localhost, 169.254.169.254,
  *.internal, *.local) are blocked even before DNS resolution and are
  NOT affected by either flag.

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


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


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


def _is_always_unsafe_ip(addr: str) -> bool:
    """Loopback / link-local / multicast / reserved / unspecified IPs.

    These categories are hard-rejected regardless of any opt-in flag —
    an LLM endpoint should never legitimately be at 127.0.0.1 or
    169.254.0.0/16. Cloud metadata is in 169.254/16 (link-local) so it's
    covered here in addition to the explicit name-based ``_DENY_HOSTS``.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_private_ip(addr: str) -> bool:
    """RFC 1918 private blocks (10/8, 172.16/12, 192.168/16) — opt-in only.

    On-prem ANILA deployments routinely sit on these — that's why this
    is a separate flag from ``_is_always_unsafe_ip``. ``ipaddress.is_private``
    in CPython technically also flags loopback / link-local; we exclude
    those by checking ``_is_always_unsafe_ip`` first at every call site.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if _is_always_unsafe_ip(addr):
        return False  # accounted for by the unsafe path
    return ip.is_private


def validate_outbound_url(url: str) -> None:
    """Raise ``UnsafeEndpointError`` if the URL would be unsafe to POST to.

    Caller is expected to translate the exception into the appropriate
    framework error (HTTPException 400 in CSP, log + skip in worker).
    """
    if not url or not isinstance(url, str):
        raise UnsafeEndpointError("endpoint_url must be a non-empty string")

    # Read flags fresh on every call so test harnesses / docker-compose
    # env updates take effect without restarting the importer.
    allow_http = _env_flag("ANILA_ALLOW_HTTP_ENDPOINT")
    allow_private = _env_flag("ANILA_ALLOW_PRIVATE_ENDPOINT")

    parsed = urlparse(url.strip())

    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        if not allow_http:
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
    if _is_always_unsafe_ip(host):
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a loopback / link-local / "
            f"metadata / reserved IP"
        )
    if _is_private_ip(host) and not allow_private:
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a private (RFC 1918) IP "
            f"(set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in on-prem dev to relax)"
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

    # Hostname: resolve and reject if any answer is unsafe. Same opt-in
    # rules as IP literals — link-local etc always blocked, RFC 1918
    # gated by ANILA_ALLOW_PRIVATE_ENDPOINT.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable — let the actual call fail. We don't want to block
        # legitimate transient DNS outages at credential-create time.
        return

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0] if sockaddr else ""
        if not addr:
            continue
        if _is_always_unsafe_ip(addr):
            raise UnsafeEndpointError(
                f"endpoint_url host {host!r} resolves to "
                f"loopback / link-local / metadata address {addr!r}"
            )
        if _is_private_ip(addr) and not allow_private:
            raise UnsafeEndpointError(
                f"endpoint_url host {host!r} resolves to private "
                f"(RFC 1918) address {addr!r} "
                f"(set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in on-prem dev)"
            )

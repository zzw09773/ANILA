"""Trusted-proxy-aware client IP resolution for audit attribution.

With the default empty ``ANILA_TRUSTED_PROXY_CIDRS``, the recorded IP is the
direct peer (typically the reverse proxy). Deployments that need the real
end-user client IP MUST set ``ANILA_TRUSTED_PROXY_CIDRS`` to the proxy's
exact address(es) (e.g. /32) or the dedicated frontend docker subnet — never
a broad intranet range that also contains end-user clients.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)

_WIDE_TRUST_MSG = "過寬的信任代理網段會讓內網用戶端偽造 X-Forwarded-For"

# (raw_settings_value, parsed networks) — lazy, invalidated when env string changes.
_trusted_cidrs_cache: tuple[str, tuple[ipaddress._BaseNetwork, ...]] | None = None


def _is_overly_wide(net: ipaddress._BaseNetwork) -> bool:
    if isinstance(net, ipaddress.IPv4Network):
        return net.prefixlen < 24
    if isinstance(net, ipaddress.IPv6Network):
        return net.prefixlen < 64
    return False


def _parse_trusted_cidrs(raw: str) -> list[ipaddress._BaseNetwork]:
    """Parse CSV CIDRs; skip bad entries; warn once per raw value on over-wide nets."""
    global _trusted_cidrs_cache
    if _trusted_cidrs_cache is not None and _trusted_cidrs_cache[0] == raw:
        return list(_trusted_cidrs_cache[1])

    networks: list[ipaddress._BaseNetwork] = []
    for part in (raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            logger.warning("ANILA_TRUSTED_PROXY_CIDRS: ignoring invalid CIDR %r", token)
            continue
        if _is_overly_wide(net):
            logger.warning(_WIDE_TRUST_MSG)
        networks.append(net)

    _trusted_cidrs_cache = (raw, tuple(networks))
    return list(networks)


def _parse_ip(value: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _in_trusted(addr: ipaddress._BaseAddress, networks: list[ipaddress._BaseNetwork]) -> bool:
    return any(addr in net for net in networks)


def resolve_client_ip(request: Request | None) -> str | None:
    """Return the real client IP for audit logging.

    Default (``ANILA_TRUSTED_PROXY_CIDRS`` empty): ignore ``X-Forwarded-For``
    and return ``request.client.host`` — the direct peer (proxy when CSP sits
    behind nginx). Operators MUST configure trusted proxy CIDRs to attribute
    the end-user client.

    When the immediate peer is inside a trusted CIDR, walk ``X-Forwarded-For``
    from right to left and return the first address that is *not* in a trusted
    CIDR (the client hop appended by our reverse proxy). Malformed entries are
    skipped; if none remain, fall back to the peer.
    """
    if request is None:
        return None
    peer = request.client.host if request.client else None
    trusted = _parse_trusted_cidrs(settings.ANILA_TRUSTED_PROXY_CIDRS)
    if not trusted:
        return peer

    peer_addr = _parse_ip(peer) if peer else None
    if peer_addr is None or not _in_trusted(peer_addr, trusted):
        return peer

    xff = request.headers.get("x-forwarded-for") or ""
    entries = [part.strip() for part in xff.split(",") if part.strip()]
    for entry in reversed(entries):
        addr = _parse_ip(entry)
        if addr is None:
            continue
        if not _in_trusted(addr, trusted):
            return str(addr)
    return peer

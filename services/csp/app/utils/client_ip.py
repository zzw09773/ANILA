"""Caller IP for audit logs — prefer what the reverse proxy observed.

Deployment fact (see ``infra/nginx/anila.conf``): nginx is the only
proxy in front of csp. It sets:

* ``X-Real-IP`` to ``$remote_addr`` (overwrites any client-supplied value)
* ``X-Forwarded-For`` via ``$proxy_add_x_forwarded_for``, which *appends*
  the real client address to whatever the caller already sent

Taking the first XFF hop therefore records the caller's claim, not the
address nginx saw. Prefer ``X-Real-IP``; if absent, take the *last*
non-empty XFF hop (what a trusted proxy appended). With no proxy headers
at all (direct container access, unit tests), fall back to the socket
peer — never invent an address.
"""
from __future__ import annotations

from starlette.requests import Request


def client_ip(request: Request | None) -> str | None:
    """Return the best-effort observed client address, or ``None``."""
    if request is None:
        return None

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip

    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [hop.strip() for hop in xff.split(",") if hop.strip()]
        if hops:
            return hops[-1]

    if request.client is not None:
        return request.client.host
    return None

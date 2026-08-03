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
- Scheme must be ``https`` unless explicitly relaxed per endpoint kind:
  ``ANILA_ALLOW_HTTP_ENDPOINT=1`` admits http for model / generic
  endpoints (PLAN.md P0.2, 2026-07-29: the intranet model gateway speaks
  plain http, production included); ``ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1``
  for agent endpoints. Default posture rejects http for every kind.
- ``grpc`` / ``grpcs`` are a **sibling** scheme branch (model kind only):
  ``grpcs`` always; cleartext ``grpc`` requires
  ``ANILA_ALLOW_GRPC_ENDPOINT=1``. Not folded into the http allow-list.
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
- ``ANILA_TRUSTED_HOSTS`` (comma-separated) is an explicit allow-list of
  hostnames whose host checks (deny list, internal-zone suffixes,
  single-label, private/loopback IP rules, DNS resolution) are skipped.
  Scheme validation still applies. Use case: docker service names in
  cross-stack networks (e.g. ``gemma4`` in ``anila-models-net``) where
  the platform deliberately calls inference servers via internal DNS.
  Only admins editing compose env can grow this list — not user-facing.

The DNS check is best-effort and runs synchronously — endpoint URLs are
registered rarely (once per BYO LLM key), not per-request, so the
cost is acceptable. A determined attacker can still race
DNS rebinding (TOCTOU) but at that point they need DNS authority for
a resolvable name; far higher bar than `host=csp-db`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


# ── endpoint_kind (Slice 6a, http 旗標分域) ─────────────────────────────────
# http 放寬旗標按端點類型分域。三種 kind:
#   'model'   — http 由 ``ANILA_ALLOW_HTTP_ENDPOINT`` 明確放行,預設拒收。
#               決策紀錄:PLAN.md P0.2(2026-07-29)——內網部署的模型 gateway
#               走 http,production 與 dev 同樣依此旗標判定。
#   'agent'   — http 由新旗標 ANILA_ALLOW_HTTP_AGENT_ENDPOINT 放行;為不打斷
#               既有內網 MLSteam 純 http NodePort agent,legacy
#               ANILA_ALLOW_HTTP_ENDPOINT 仍作 fallback(帶 deprecation 警告)。
#   'generic' — 既有全域語意(預設值);既有呼叫端零行為變更(ingestion 憑證
#               等)。http 由 ANILA_ALLOW_HTTP_ENDPOINT 放行。
ENDPOINT_KIND_MODEL = "model"
ENDPOINT_KIND_AGENT = "agent"
ENDPOINT_KIND_GENERIC = "generic"

_HTTP_MODEL_FLAG = "ANILA_ALLOW_HTTP_ENDPOINT"
_HTTP_AGENT_FLAG = "ANILA_ALLOW_HTTP_AGENT_ENDPOINT"

# Cleartext gRPC (``grpc://``) for model endpoints — sibling of the http
# model flag. ``grpcs://`` never needs this. Do NOT fold grpc into the http
# flag: that would couple two transports and invite collapsing the scheme
# branches into a shared allow-list (forbidden; see ``validate_outbound_url``).
_GRPC_MODEL_FLAG = "ANILA_ALLOW_GRPC_ENDPOINT"


def _is_production() -> bool:
    """Deployment posture via ``ANILA_ENV`` in {production, prod}.

    Not referenced by the scheme check since PLAN.md P0.2 (2026-07-29);
    retained for future posture-dependent rules.
    """
    return os.environ.get("ANILA_ENV", "").strip().lower() in {"production", "prod"}


def _reject_http_scheme(endpoint_kind: str) -> None:
    """Enforce the per-kind http acceptance rules. Raise on rejection.

    Called only when the scheme is ``http``. ``https`` is always fine and
    never reaches here.
    """
    allow_http = _env_flag(_HTTP_MODEL_FLAG)

    if endpoint_kind == ENDPOINT_KIND_MODEL:
        # 2026-07-29 拍板(PLAN.md P0.2):model http 一律由旗標明確放行,
        # production 不再無條件拒絕(氣隙內網模型 gateway 走純 http)。
        if not allow_http:
            raise UnsafeEndpointError(
                "model endpoint scheme must be 'https' "
                "(設 ANILA_ALLOW_HTTP_ENDPOINT=1 放寬;預設拒收 http)",
                reason=REASON_SCHEME,
            )
        return

    if endpoint_kind == ENDPOINT_KIND_AGENT:
        # agent endpoint transition：新旗標優先;legacy 全域旗標仍 fallback
        # (內網 MLSteam 純 http NodePort agent 靠它),但記 deprecation 警告。
        if _env_flag(_HTTP_AGENT_FLAG):
            return
        if allow_http:
            logger.warning(
                "agent endpoint http 放行走 legacy ANILA_ALLOW_HTTP_ENDPOINT "
                "(deprecated) — 請改設 ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1;"
                "legacy fallback 之後版本會移除"
            )
            return
        raise UnsafeEndpointError(
            "agent endpoint scheme must be 'https' "
            "(set ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1 to allow http agent "
            "endpoints, e.g. MLSteam NodePort)",
            reason=REASON_SCHEME,
        )

    # generic（既有語意，預設）
    if not allow_http:
        raise UnsafeEndpointError(
            "endpoint_url scheme must be 'https' "
            "(set ANILA_ALLOW_HTTP_ENDPOINT=1 in dev to relax)",
            reason=REASON_SCHEME,
        )


def _reject_grpc_scheme(scheme: str, endpoint_kind: str) -> None:
    """Enforce gRPC scheme acceptance. Raise on rejection.

    Called only when the scheme is ``grpc`` or ``grpcs``. Keyed on
    ``parsed.scheme`` from the caller — this is intentionally a sibling
    branch of ``_reject_http_scheme``, not a shared scheme allow-list.
    Collapsing http/https/grpc into one set would bypass the per-kind http
    flag split and silently widen what ordinary ``http://`` endpoints may
    reach.

    - ``grpcs`` (TLS): allowed for ``model`` only.
    - ``grpc`` (cleartext): allowed for ``model`` only when
      ``ANILA_ALLOW_GRPC_ENDPOINT=1`` (air-gap Triton often speaks plain
      gRPC on an internal port).
    - ``agent`` / ``generic``: always rejected — gRPC model transport is
      not an agent or BYO-credential surface.
    """
    if endpoint_kind != ENDPOINT_KIND_MODEL:
        raise UnsafeEndpointError(
            f"endpoint_url scheme {scheme!r} not allowed for "
            f"endpoint_kind={endpoint_kind!r} (grpc/grpcs 僅供 model)",
            reason=REASON_SCHEME,
        )
    if scheme == "grpcs":
        return
    # scheme == "grpc"
    if not _env_flag(_GRPC_MODEL_FLAG):
        raise UnsafeEndpointError(
            "model endpoint scheme must be 'grpcs' "
            "(設 ANILA_ALLOW_GRPC_ENDPOINT=1 放寬 cleartext grpc://;"
            "預設拒收)",
            reason=REASON_SCHEME,
        )


def _env_trusted_hosts() -> set[str]:
    """Comma-separated allow-list from ``ANILA_TRUSTED_HOSTS``.

    Read fresh on every call so docker-compose env edits take effect
    without restarting the importer. Empty / unset → empty set →
    no hosts bypass. Acts as the bootstrap / fallback layer when no
    DB-backed provider is registered (agent / worker contexts).
    """
    raw = os.environ.get("ANILA_TRUSTED_HOSTS", "").strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


# Process-local list of dynamic trusted-host sources. CSP registers a
# DB-backed cache at startup; agent / worker processes leave this empty
# and rely on the env fallback. Providers are additive: the final
# trusted-host set is the union of env + every provider.
_trusted_host_providers: list[Callable[[], set[str]]] = []


def register_trusted_host_provider(fn: Callable[[], set[str]]) -> None:
    """Register a callable that returns extra trusted hostnames at call time.

    Used by CSP to wire an admin-managed DB allow-list into the guard
    without making anila-core depend on the platform schema. Provider
    failures are swallowed (and logged) so a broken DB connection never
    weakens security validation — fallback is env-only.
    """
    _trusted_host_providers.append(fn)


def clear_trusted_host_providers() -> None:
    """Reset registered providers. Test-only — avoids leakage between
    monkeypatched fixtures."""
    _trusted_host_providers.clear()


def _trusted_hosts() -> set[str]:
    result = _env_trusted_hosts()
    for provider in _trusted_host_providers:
        try:
            extra = provider()
        except Exception:
            # A misbehaving provider must not break URL validation —
            # security should fail closed (still validate against env),
            # not fall apart. Log and move on.
            logger.warning(
                "trusted_host provider raised; falling back to env-only",
                exc_info=True,
            )
            continue
        if extra:
            result = result | {h.lower() for h in extra if h}
    return result


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


# Reason codes for typed error propagation. Frontend / API responses use
# these to decide whether the failure is "admin can fix by adding to
# trusted_hosts" (single-label / internal-zone) vs structural rejections
# that should never be bypassed (loopback / metadata / link-local).
REASON_EMPTY = "empty"
REASON_NO_HOSTNAME = "no_hostname"
REASON_SCHEME = "scheme"
REASON_DENY_HOST = "deny_host"
REASON_INTERNAL_ZONE = "internal_zone"
REASON_UNSAFE_IP = "unsafe_ip"
REASON_PRIVATE_IP = "private_ip"
REASON_SINGLE_LABEL = "single_label"

# Failure reasons that an admin can legitimately fix by adding the
# hostname to the trusted-hosts allow-list. Loopback / metadata /
# link-local are NEVER in here — they're outright dangerous.
FIXABLE_BY_TRUST_HOST = frozenset({REASON_SINGLE_LABEL, REASON_INTERNAL_ZONE})


class UnsafeEndpointError(ValueError):
    """Raised when a user-supplied URL is unsafe for outbound calls.

    Carries structured attributes so callers (CSP API layer) can render
    a typed 400 with actionable guidance ("add foobar to trusted hosts?")
    instead of opaque message strings. ``str(exc)`` still returns the
    human-readable message so legacy plain-string consumers keep working.
    """

    def __init__(
        self,
        message: str,
        *,
        host: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.host = host
        self.reason = reason

    @property
    def fixable_by_trust_host(self) -> bool:
        """True iff admin can plausibly fix by adding ``self.host`` to
        ``trusted_hosts``. False for any structurally-unsafe URL (loopback,
        metadata, etc.)."""
        return self.reason in FIXABLE_BY_TRUST_HOST and bool(self.host)


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


def validate_outbound_url(url: str, endpoint_kind: str = ENDPOINT_KIND_GENERIC) -> None:
    """Raise ``UnsafeEndpointError`` if the URL would be unsafe to POST to.

    Caller is expected to translate the exception into the appropriate
    framework error (HTTPException 400 in CSP, log + skip in worker).

    ``endpoint_kind`` (Slice 6a) domain-splits the http-scheme
    relaxation flag by endpoint class — ``'model'`` (http gated by
    ``ANILA_ALLOW_HTTP_ENDPOINT``, env-independent since PLAN.md P0.2),
    ``'agent'`` (own ``ANILA_ALLOW_HTTP_AGENT_ENDPOINT`` flag,
    legacy fallback), or ``'generic'`` (default; original global semantics,
    existing callers unaffected). ALL host / IP / DNS / trusted-host checks
    below are identical across kinds — the split touches scheme only.
    """
    if not url or not isinstance(url, str):
        raise UnsafeEndpointError(
            "endpoint_url must be a non-empty string",
            reason=REASON_EMPTY,
        )

    # Read flags fresh on every call so test harnesses / docker-compose
    # env updates take effect without restarting the importer.
    allow_private = _env_flag("ANILA_ALLOW_PRIVATE_ENDPOINT")

    parsed = urlparse(url.strip())

    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        # Per-kind http acceptance (raises on rejection). Scheme is validated
        # BEFORE the trusted-host bypass below, so a trusted host can never
        # rescue an http:// endpoint whose kind-flag is unset.
        _reject_http_scheme(endpoint_kind)
    elif parsed.scheme == "grpcs" or parsed.scheme == "grpc":
        # Additive sibling branch — keyed on scheme, own kind/flag gate.
        # Do NOT refactor into a shared allowlist set with http/https.
        _reject_grpc_scheme(parsed.scheme, endpoint_kind)
    else:
        raise UnsafeEndpointError(
            f"endpoint_url scheme {parsed.scheme!r} not allowed "
            f"(use https://)",
            reason=REASON_SCHEME,
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeEndpointError(
            "endpoint_url has no hostname",
            reason=REASON_NO_HOSTNAME,
        )

    # Admin-blessed hosts (docker service names and intranet FQDNs in
    # cross-stack networks, etc.) may bypass only narrow, operator-owned
    # host checks: fixable name-shape checks below and RFC1918 DNS answers.
    # Structural rejects such as loopback, metadata, link-local and IP
    # literals still fail closed. Scheme is already validated above.
    trusted = host in _trusted_hosts()

    if host in _DENY_HOSTS:
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is on the deny list "
            f"(loopback / metadata / mDNS)",
            host=host,
            reason=REASON_DENY_HOST,
        )

    if any(host.endswith(suffix) for suffix in _DENY_HOST_SUFFIXES):
        if trusted:
            return
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is in an internal-only zone",
            host=host,
            reason=REASON_INTERNAL_ZONE,
        )

    # IP literal: validate directly without DNS round-trip.
    if _is_always_unsafe_ip(host):
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a loopback / link-local / "
            f"metadata / reserved IP",
            host=host,
            reason=REASON_UNSAFE_IP,
        )
    if _is_private_ip(host) and not allow_private:
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a private (RFC 1918) IP "
            f"(set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in on-prem dev to relax)",
            host=host,
            reason=REASON_PRIVATE_IP,
        )

    # Single-label hostnames (no dot, e.g. "csp-db", "redis", "router")
    # are docker-compose / k8s service names — never legitimate public
    # endpoints. We block them up-front so we don't depend on the DNS
    # check below (which can be racy or unavailable at create-time).
    if "." not in host and not _is_ip_literal(host):
        if trusted:
            return
        raise UnsafeEndpointError(
            f"endpoint_url host {host!r} is a single-label name "
            f"(typo / docker service name?). Use a fully-qualified "
            f"public hostname.",
            host=host,
            reason=REASON_SINGLE_LABEL,
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
                f"loopback / link-local / metadata address {addr!r}",
                host=host,
                reason=REASON_UNSAFE_IP,
            )
        if _is_private_ip(addr) and not allow_private and not trusted:
            raise UnsafeEndpointError(
                f"endpoint_url host {host!r} resolves to private "
                f"(RFC 1918) address {addr!r} "
                f"(set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in on-prem dev)",
                host=host,
                reason=REASON_PRIVATE_IP,
            )

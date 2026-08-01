"""Request-scoped dispatch JWT for in-task agent→CSP callbacks (P2.1 W5).

CSP issues a short-lived RS256 dispatch token per request. While handling
that request the agent must present the *same* Bearer token on outbound
RAG search / trace export — not a static ``csk-``.

Stored in a :class:`contextvars.ContextVar` so each asyncio Task (and each
concurrent request) has an isolated copy. A module-global would leak one
caller's identity into another's callback under concurrency.

Invariant: the dispatch JWT may only be attached when the outbound origin
is CSP itself. A different origin falls back to the static credential or
refuses — never silently attaches the user JWT to a foreign host.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator
from urllib.parse import urlsplit

_DISPATCH_BEARER: ContextVar[str | None] = ContextVar(
    "anila_dispatch_bearer", default=None
)


class MissingDispatchTokenError(RuntimeError):
    """In-task outbound callback attempted with no dispatch JWT in scope."""


def get_dispatch_bearer() -> str | None:
    """Return the raw JWT (no ``Bearer `` prefix) for the current task, or None."""
    token = _DISPATCH_BEARER.get()
    if token is None:
        return None
    token = token.strip()
    return token or None


def require_dispatch_bearer() -> str:
    """Return the in-scope dispatch JWT or raise :class:`MissingDispatchTokenError`."""
    token = get_dispatch_bearer()
    if not token:
        raise MissingDispatchTokenError(
            "no dispatch JWT in request scope; in-task CSP callbacks must "
            "present the inbound Authorization Bearer token (P2.1). "
            "Out-of-task paths (config poll / revocation) are not covered "
            "here — see OWNER-QUESTIONS Q19."
        )
    return token


def same_csp_origin(left: str, right: str) -> bool:
    """True when ``left`` and ``right`` share scheme + host[:port] (case-insensitive)."""
    a, b = urlsplit((left or "").strip()), urlsplit((right or "").strip())
    if not a.scheme or not a.netloc or not b.scheme or not b.netloc:
        return False
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())


def resolve_outbound_bearer(
    *,
    fallback: str | None = None,
    target_base_url: str | None = None,
    csp_base_url: str | None = None,
) -> str:
    """Prefer request-scoped dispatch JWT **only** when the target is CSP.

    ``target_base_url`` / ``csp_base_url`` gate attachment: different origin
    → fall back to ``fallback`` or raise — never paste the user JWT onto a
    foreign collector. When both origin args are omitted (unit helpers),
    scoped JWT is allowed (caller has no outbound URL yet).

    Raises :class:`MissingDispatchTokenError` when neither an attachable
    scoped JWT nor ``fallback`` is available — never invents a token and
    never returns empty (callers must not POST unauthenticated).
    """
    scoped = get_dispatch_bearer()
    if scoped:
        if target_base_url is not None and csp_base_url is not None:
            if same_csp_origin(target_base_url, csp_base_url):
                return scoped
            # Foreign origin: never attach the dispatch JWT.
        elif target_base_url is None and csp_base_url is None:
            return scoped
        # Incomplete origin pair, or mismatched origins → do not attach JWT.
    if fallback and fallback.strip():
        return fallback.strip()
    if scoped and target_base_url is not None and csp_base_url is not None:
        if not same_csp_origin(target_base_url, csp_base_url):
            raise MissingDispatchTokenError(
                "dispatch JWT must not be sent to a non-CSP origin "
                f"({target_base_url!r} ≠ {csp_base_url!r}); no fallback credential"
            )
    raise MissingDispatchTokenError(
        "no dispatch JWT in request scope and no fallback credential; "
        "refusing to call CSP without auth"
    )


def _set_dispatch_bearer(token: str) -> Token:
    cleaned = (token or "").strip()
    if not cleaned:
        raise MissingDispatchTokenError("empty dispatch bearer")
    return _DISPATCH_BEARER.set(cleaned)


def _reset_dispatch_bearer(ctx_token: Token) -> None:
    """Reset the ContextVar; tolerate ASGI cross-context generator finalization.

    When an async generator that entered a scope is aclose()'d from a
    different Context (SSE client disconnect), ``Token.reset`` raises
    ``ValueError: Token was created in a different Context``. Suppressing
    that keeps the event-loop exception handler clean; uvicorn already
    gives each request a fresh Context, so the orphaned value cannot
    cross users. Prefer not to set/reset across a yield boundary at all
    (see service_wrapper streaming task).
    """
    try:
        _DISPATCH_BEARER.reset(ctx_token)
    except ValueError:
        # Cross-context finalize — scope dies with the request Context.
        pass


@contextmanager
def dispatch_bearer_scope(bearer: str) -> Iterator[str]:
    """Bind ``bearer`` (raw JWT) for the current task until the block exits.

    Must not span an async-generator suspension that ASGI may finalize in
    another Context — run such bodies in their own Task (own Context copy).
    """
    ctx_token = _set_dispatch_bearer(bearer)
    try:
        yield require_dispatch_bearer()
    finally:
        _reset_dispatch_bearer(ctx_token)


__all__ = [
    "MissingDispatchTokenError",
    "dispatch_bearer_scope",
    "get_dispatch_bearer",
    "require_dispatch_bearer",
    "resolve_outbound_bearer",
    "same_csp_origin",
]

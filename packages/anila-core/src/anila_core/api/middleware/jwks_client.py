"""Async JWKS client for agent-side dispatch-token verification (P2.1).

Mirrors the production-proven design in ``services/anila-studio`` /
``services/asr-gateway``:

* lazy first fetch
* TTL cache (default 3600s)
* forced refetch on unknown kid
* background refresh
* ``asyncio.Lock`` dedupe

CA trust is an explicit file path (``ca_file``) passed to httpx ``verify=``.
This module never reads ``SSL_CERT_FILE`` (that env replaces the whole
trust store — a known footgun on this platform).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
import time
from typing import Any, Callable, Awaitable

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicKey,
    RSAPublicNumbers,
)


logger = logging.getLogger(__name__)

JWKS_PATH = "/.well-known/jwks.json"
DEFAULT_JWKS_TTL_SECONDS = 3600
_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


class JwksError(Exception):
    """Base class for JWKS-related failures."""


class JwksFetchError(JwksError):
    """HTTP layer failed: connection error, non-2xx, or non-JSON body."""


class JwksPayloadError(JwksError):
    """JWKS document was reachable but malformed (RFC 7517 violation)."""


class JwksKeyNotFoundError(JwksError):
    """Requested ``kid`` was not in the JWKS even after a forced refetch."""


def derive_jwks_url(csp_base_url: str) -> str:
    """Build ``{base}/.well-known/jwks.json`` from the platform base URL."""
    base = (csp_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}{JWKS_PATH}"


def _b64url_decode_int(value: str) -> int:
    if not isinstance(value, str) or not value:
        raise JwksPayloadError("base64url field empty or non-string")
    pad = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
    except (ValueError, TypeError) as exc:
        raise JwksPayloadError(f"invalid base64url: {exc}") from exc
    return int.from_bytes(raw, "big")


def jwk_to_public_key(jwk: dict[str, Any]) -> RSAPublicKey:
    """Turn one JWK entry into an :class:`RSAPublicKey` (RSA / RS256 only)."""
    if not isinstance(jwk, dict):
        raise JwksPayloadError("JWK entry is not an object")
    kty = jwk.get("kty")
    if kty != "RSA":
        raise JwksPayloadError(f"unsupported kty {kty!r}; only RSA is accepted")
    n_b64 = jwk.get("n")
    e_b64 = jwk.get("e")
    if not n_b64 or not e_b64:
        raise JwksPayloadError("JWK missing 'n' or 'e' field")
    return RSAPublicNumbers(
        e=_b64url_decode_int(e_b64),
        n=_b64url_decode_int(n_b64),
    ).public_key()


def parse_jwks(payload: Any) -> dict[str, RSAPublicKey]:
    """Validate JWKS shape and return ``kid -> public_key``."""
    if not isinstance(payload, dict):
        raise JwksPayloadError("JWKS payload is not a JSON object")
    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise JwksPayloadError("JWKS payload missing non-empty 'keys' array")
    out: dict[str, RSAPublicKey] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            raise JwksPayloadError("JWKS key entry is not an object")
        kid = entry.get("kid")
        if not kid or not isinstance(kid, str):
            raise JwksPayloadError("JWK missing string 'kid'")
        out[kid] = jwk_to_public_key(entry)
    return out


FetchFn = Callable[[], Awaitable[dict[str, RSAPublicKey]]]


class JwksClient:
    """Async JWKS fetcher + in-memory cache.

    Parameters
    ----------
    jwks_url:
        Absolute URL of the platform JWKS document.
    ca_file:
        Optional path to a PEM CA bundle. Passed to httpx as ``verify=``.
        ``None`` → httpx default trust store. Never uses ``SSL_CERT_FILE``.
    ttl_seconds:
        Cache lifetime (default 3600).
    fetch_fn:
        Optional override for tests (bypass HTTP).
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        ca_file: str | None = None,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        fetch_fn: FetchFn | None = None,
    ) -> None:
        self._jwks_url = (jwks_url or "").strip()
        self._ca_file = (ca_file or "").strip() or None
        self._ttl_seconds = max(int(ttl_seconds), 1)
        self._fetch_fn = fetch_fn
        self._cache: dict[str, RSAPublicKey] = {}
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self.fetch_count: int = 0  # test aid

    @property
    def jwks_url(self) -> str:
        return self._jwks_url

    @property
    def configured(self) -> bool:
        return bool(self._jwks_url)

    async def get_public_key(self, kid: str) -> RSAPublicKey:
        if not kid:
            raise JwksKeyNotFoundError("kid must be a non-empty string")
        if not self._jwks_url and self._fetch_fn is None:
            raise JwksFetchError("JWKS URL is not configured")

        if self._is_expired():
            await self._refresh_locked()

        key = self._cache.get(kid)
        if key is not None:
            return key

        logger.info("JWKS cache miss for kid=%s, forcing refetch", kid)
        await self._refresh_locked(force=True)

        key = self._cache.get(kid)
        if key is None:
            raise JwksKeyNotFoundError(
                f"kid {kid!r} not present in JWKS after forced refetch"
            )
        return key

    async def warm(self) -> None:
        await self._refresh_locked(force=True)

    async def start_background_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="anila-jwks-refresh"
        )

    async def stop_background_refresh(self) -> None:
        task = self._refresh_task
        self._refresh_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover
            logger.exception("JWKS background refresh task exited with error")

    def _is_expired(self) -> bool:
        if not self._cache:
            return True
        return (time.monotonic() - self._cached_at) >= self._ttl_seconds

    async def _refresh_locked(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and not self._is_expired():
                return
            new_cache = await self._fetch_jwks()
            self._cache = new_cache
            self._cached_at = time.monotonic()
            logger.debug(
                "JWKS cache refreshed with %d key(s): %s",
                len(new_cache),
                sorted(new_cache.keys()),
            )

    def _httpx_verify(self) -> bool | str:
        """Return httpx ``verify=`` value. Never touches ``SSL_CERT_FILE``."""
        if self._ca_file:
            return self._ca_file
        return True

    async def _fetch_jwks(self) -> dict[str, RSAPublicKey]:
        self.fetch_count += 1
        if self._fetch_fn is not None:
            return await self._fetch_fn()

        url = self._jwks_url
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, verify=self._httpx_verify()
            ) as http:
                response = await http.get(url)
        except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
            # OSError/ssl.SSLError: unreadable/absent ANILA_CA_FILE (or TLS
            # context build failure) must fail closed as JwksFetchError —
            # not bubble as an unhandled 500.
            raise JwksFetchError(f"failed to reach {url}: {exc}") from exc

        if response.status_code != 200:
            raise JwksFetchError(
                f"JWKS endpoint {url} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JwksFetchError(f"JWKS endpoint returned non-JSON body: {exc}") from exc
        return parse_jwks(payload)

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._ttl_seconds)
                await self._refresh_locked(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                logger.exception(
                    "JWKS background refresh failed; will retry next interval"
                )


__all__ = [
    "DEFAULT_JWKS_TTL_SECONDS",
    "JWKS_PATH",
    "JwksClient",
    "JwksError",
    "JwksFetchError",
    "JwksKeyNotFoundError",
    "JwksPayloadError",
    "derive_jwks_url",
    "jwk_to_public_key",
    "parse_jwks",
]

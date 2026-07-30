"""⚠⚠ VENDORED —— 本檔是 `services/anila-studio/app/services/jwks_client.py`
的逐位元組副本(vendor 於 ASR reclaim 2026-07-30,來源 md5 afe6eb4113bad8587abe3c68da59dc30)。

**改這裡就必須同步改 studio 那份,反之亦然。** 這是 JWT 信任錨的一部分:
兩份分歧 = 被撤銷的權杖在一個服務被擋、在另一個服務仍然通行。

為什麼是副本而不是共用套件:抽到 packages 才是正解,但那超出「語音當鍵盤」
的範圍。vendor 時只加了這段標頭,其餘一行未改。

隱性相依:app.config.settings 同名欄位(CSP_BASE_URL / JWKS_REFRESH_SECONDS / INTERNAL_TIMEOUT_CONNECT)。

原檔 docstring:
────────────────────────────────────────────────────────────────────────

JWKS client for anila-studio JWT verification.

CSP signs access tokens with an RSA private key (RS256) and publishes the
matching public key at ``${CSP_BASE_URL}/.well-known/jwks.json`` (RFC 7517).
anila-studio fetches that document, reconstructs the public key, and caches
it for the lifetime of the process so verifying a token never hits the
network on the hot path.

Design summary
--------------

* **Lazy cold-start** — no HTTP at import time. The first ``get_public_key``
  call (or an explicit :func:`start` from FastAPI lifespan) triggers the
  fetch.
* **TTL cache** — refreshed every ``settings.JWKS_REFRESH_SECONDS`` seconds.
  After the TTL elapses the next lookup re-issues the HTTP call before
  returning.
* **Forced refetch on miss** — looking up an unknown ``kid`` triggers
  exactly one extra fetch (covers the "key just rotated" case). If still
  missing we raise rather than return ``None`` so callers can map the
  failure to a 401 cleanly.
* **Background refresh task** — :func:`start` launches an
  ``asyncio.create_task`` loop that re-fetches every ``JWKS_REFRESH_SECONDS``
  to keep the cache warm. :func:`stop` cancels it.
* **Concurrency** — an ``asyncio.Lock`` dedupes concurrent cold-starts so
  the first burst of requests on a freshly-booted pod issues a single HTTP
  call rather than one per request.
* **No HTTP cache reliance** — CSP sets ``Cache-Control: max-age=3600`` but
  we never trust it; the TTL above is the only refresh signal.

Public surface
--------------

* :class:`JwksClient` — the actual implementation (handy in tests).
* :func:`get_public_key` — module-level helper that uses the singleton
  client. This is what callers (``app.auth`` in Wave-2) import.
* :func:`start` / :func:`stop` — FastAPI lifespan hooks.
* :class:`JwksError` (base), :class:`JwksFetchError`,
  :class:`JwksPayloadError`, :class:`JwksKeyNotFoundError`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicKey,
    RSAPublicNumbers,
)

from app.config import settings


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class JwksError(Exception):
    """Base class for JWKS-related failures.

    All callers can ``except JwksError`` to catch everything below.
    """


class JwksFetchError(JwksError):
    """HTTP layer failed: connection error, non-2xx response, non-JSON body."""


class JwksPayloadError(JwksError):
    """JWKS document was reachable but malformed (RFC 7517 violation)."""


class JwksKeyNotFoundError(JwksError):
    """Requested ``kid`` was not in the JWKS even after a forced refetch."""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


_JWKS_PATH = "/.well-known/jwks.json"
_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=settings.INTERNAL_TIMEOUT_CONNECT)


def _b64url_decode_int(value: str) -> int:
    """Inverse of csp's ``_int_to_base64url`` (RFC 7518 §6.3.1).

    JWKS encodes ``n`` / ``e`` as unpadded base64url; ``base64.urlsafe_b64decode``
    requires the padding back, so we pad to a multiple of 4 before decoding.
    """
    if not isinstance(value, str) or not value:
        raise JwksPayloadError("base64url field empty or non-string")
    pad = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
    except (ValueError, TypeError) as exc:
        raise JwksPayloadError(f"invalid base64url: {exc}") from exc
    return int.from_bytes(raw, "big")


def _jwk_to_public_key(jwk: dict[str, Any]) -> RSAPublicKey:
    """Turn one JWK entry into an :class:`RSAPublicKey`.

    Only RSA keys (``kty == "RSA"``) are supported today — RS256 is the
    only algorithm CSP issues with. EC / OKP keys raise.
    """
    if not isinstance(jwk, dict):
        raise JwksPayloadError("JWK entry is not an object")

    kty = jwk.get("kty")
    if kty != "RSA":
        raise JwksPayloadError(f"unsupported kty {kty!r}; only RSA is accepted")

    n_b64 = jwk.get("n")
    e_b64 = jwk.get("e")
    if not n_b64 or not e_b64:
        raise JwksPayloadError("JWK missing 'n' or 'e' field")

    n = _b64url_decode_int(n_b64)
    e = _b64url_decode_int(e_b64)
    return RSAPublicNumbers(e=e, n=n).public_key()


def _parse_jwks(payload: Any) -> dict[str, RSAPublicKey]:
    """Validate the JWKS document shape and return a ``kid -> public_key`` map."""
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
        out[kid] = _jwk_to_public_key(entry)
    return out


# ----------------------------------------------------------------------------
# JwksClient
# ----------------------------------------------------------------------------


class JwksClient:
    """Async JWKS fetcher + in-memory cache.

    The instance is fully self-contained — useful in tests where each test
    wants its own pristine cache. Production code goes through the module-
    level :func:`get_public_key` which uses a process-wide singleton.
    """

    def __init__(self) -> None:
        self._cache: dict[str, RSAPublicKey] = {}
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_public_key(self, kid: str) -> RSAPublicKey:
        """Return the RSA public key registered under ``kid``.

        Behaviour:

        1. If the cache is empty or expired → fetch first.
        2. If ``kid`` is in the (fresh) cache → return it.
        3. Otherwise force one refetch (key may have just rotated).
        4. If still missing → :class:`JwksKeyNotFoundError`.
        """
        if not kid:
            raise JwksKeyNotFoundError("kid must be a non-empty string")

        if self._is_expired():
            await self._refresh_locked()

        key = self._cache.get(kid)
        if key is not None:
            return key

        # Forced refetch on miss — covers rotation that happened mid-TTL
        logger.info("JWKS cache miss for kid=%s, forcing refetch", kid)
        await self._refresh_locked(force=True)

        key = self._cache.get(kid)
        if key is None:
            raise JwksKeyNotFoundError(
                f"kid {kid!r} not present in JWKS after forced refetch"
            )
        return key

    async def warm(self) -> None:
        """Eager warm-up — call from FastAPI startup so the first request
        doesn't pay the fetch cost.
        """
        await self._refresh_locked(force=True)

    async def start_background_refresh(self) -> None:
        """Launch the periodic refresh loop. Idempotent."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="jwks-refresh"
        )

    async def stop_background_refresh(self) -> None:
        """Cancel the periodic loop and wait for it to settle."""
        task = self._refresh_task
        self._refresh_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover — defensive
            logger.exception("JWKS background refresh task exited with error")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_expired(self) -> bool:
        if not self._cache:
            return True
        ttl = max(int(settings.JWKS_REFRESH_SECONDS), 1)
        return (time.monotonic() - self._cached_at) >= ttl

    async def _refresh_locked(self, *, force: bool = False) -> None:
        """Re-fetch JWKS under the singleton lock.

        ``force=False`` short-circuits if another coroutine already refreshed
        the cache while we waited for the lock — eliminates the thundering
        herd on cold-start.
        """
        async with self._lock:
            if not force and not self._is_expired():
                return
            new_cache = await self._fetch_jwks()
            # Atomic swap — keeps lookups during the refresh consistent.
            self._cache = new_cache
            self._cached_at = time.monotonic()
            logger.debug(
                "JWKS cache refreshed with %d key(s): %s",
                len(new_cache),
                sorted(new_cache.keys()),
            )

    async def _fetch_jwks(self) -> dict[str, RSAPublicKey]:
        url = settings.CSP_BASE_URL.rstrip("/") + _JWKS_PATH
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
                response = await http.get(url)
        except httpx.HTTPError as exc:
            raise JwksFetchError(f"failed to reach {url}: {exc}") from exc

        if response.status_code != 200:
            raise JwksFetchError(
                f"JWKS endpoint {url} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JwksFetchError(f"JWKS endpoint returned non-JSON body: {exc}") from exc

        return _parse_jwks(payload)

    async def _refresh_loop(self) -> None:
        """Background coroutine: sleep TTL, then refresh; repeat forever.

        Errors are logged but never propagated — a transient CSP outage
        must not crash the studio process. The next refresh attempt will
        either succeed or the cached keys keep serving until they really
        do go stale (which is bounded by token lifetime anyway).
        """
        while True:
            try:
                interval = max(int(settings.JWKS_REFRESH_SECONDS), 1)
                await asyncio.sleep(interval)
                await self._refresh_locked(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — best-effort background loop
                logger.exception(
                    "JWKS background refresh failed; will retry next interval"
                )


# ----------------------------------------------------------------------------
# Module-level singleton + lifespan helpers
# ----------------------------------------------------------------------------


_client_instance: JwksClient | None = None


def _get_client() -> JwksClient:
    """Lazy singleton accessor — first call creates the client."""
    global _client_instance
    if _client_instance is None:
        _client_instance = JwksClient()
    return _client_instance


def _reset_for_tests() -> None:
    """Test-only: drop the singleton so each test gets a fresh client.

    Not part of the public API — only the test fixture in
    ``tests/test_jwks_client.py`` should touch this.
    """
    global _client_instance
    _client_instance = None


async def get_public_key(kid: str) -> RSAPublicKey:
    """Return the public key for ``kid`` via the process-wide singleton."""
    return await _get_client().get_public_key(kid)


async def start(app: Any) -> None:  # noqa: ARG001 — FastAPI passes the app
    """FastAPI lifespan hook: warm the cache + launch the refresh task.

    ``app`` is accepted (and ignored) so this fits the standard lifespan
    signature: ``await jwks_client.start(app)``.
    """
    client = _get_client()
    await client.warm()
    await client.start_background_refresh()


async def stop(app: Any) -> None:  # noqa: ARG001
    """FastAPI lifespan hook: cancel the background refresh task."""
    client = _get_client()
    await client.stop_background_refresh()


__all__ = [
    "JwksClient",
    "JwksError",
    "JwksFetchError",
    "JwksKeyNotFoundError",
    "JwksPayloadError",
    "get_public_key",
    "start",
    "stop",
]

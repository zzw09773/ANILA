"""OIDC discovery decides four URLs; every one of them must pass the outbound guard.

HIGH finding (security sweep 2026-08-19, materials 2026-08-24): whoever answers
``{issuer}/.well-known/openid-configuration`` hands the platform four URLs.
``token_endpoint`` receives ``client_secret`` + ``code`` + ``code_verifier``;
``jwks_uri`` decides which keys we trust for ``id_token``. None of them was
validated. The fix is not "guard ``jwks_uri``" — that closes the trust-anchor
half and leaves the secret half untouched — so these tests discriminate per
field: each case poisons exactly one URL and keeps the other three good.

Flags are deliberately set *permissive* here (``ANILA_ALLOW_HTTP_ENDPOINT=1``,
``ANILA_ALLOW_PRIVATE_ENDPOINT=1``) to prove the https requirement on IdP URLs
does not depend on them, and the bad hosts are always-unsafe literals so the
private-endpoint flag cannot rescue them.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from app.services import external_auth_service as eas

_ISSUER = "https://idp.example"
_GOOD = {
    "issuer": _ISSUER,
    "authorization_endpoint": f"{_ISSUER}/authorize",
    "token_endpoint": f"{_ISSUER}/token",
    "userinfo_endpoint": f"{_ISSUER}/userinfo",
    "jwks_uri": f"{_ISSUER}/jwks",
}
_FIELDS = (
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
    "jwks_uri",
)
_BAD = {
    "http-scheme": "http://idp.example/x",
    "loopback": "https://127.0.0.1/x",
    "metadata": "https://169.254.169.254/x",
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """Stand-in for ``httpx.AsyncClient``: every GET answers with ``payload``."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(url)
        return _Resp(self.payload)


def _provider(**explicit):
    provider = MagicMock()
    provider.oidc_issuer_url = _ISSUER
    provider.oidc_client_id = "anila-client"
    provider.oidc_authorization_endpoint = explicit.get("authorization_endpoint")
    provider.oidc_token_endpoint = explicit.get("token_endpoint")
    provider.oidc_userinfo_endpoint = explicit.get("userinfo_endpoint")
    return provider


def _public_dns(host, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 0))]


@pytest.fixture(autouse=True)
def _permissive_flags_and_public_dns(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with patch("anila_core.security.url_guard.socket.getaddrinfo", _public_dns):
        yield


def _discovery(**overrides):
    return {**_GOOD, **overrides}


@pytest.mark.asyncio
async def test_clean_discovery_resolves_all_four_urls():
    client = _Client(_discovery())
    with patch.object(eas.httpx, "AsyncClient", lambda **kw: client):
        metadata = await eas._resolve_oidc_metadata(_provider())
    for field in _FIELDS:
        assert metadata[field] == _GOOD[field]


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("kind", sorted(_BAD))
@pytest.mark.asyncio
async def test_one_poisoned_discovery_url_is_rejected_by_name(field, kind):
    """Kill: drop the guard for any single field → exactly that row goes green."""
    client = _Client(_discovery(**{field: _BAD[kind]}))
    with patch.object(eas.httpx, "AsyncClient", lambda **kw: client):
        with pytest.raises(ValueError, match=field):
            await eas._resolve_oidc_metadata(_provider())


@pytest.mark.asyncio
async def test_all_four_poisoned_is_rejected_before_anything_is_used():
    """The acceptance wording from the ruling: four external URLs, all refused."""
    poisoned = {field: _BAD["http-scheme"] for field in _FIELDS}
    client = _Client(_discovery(**poisoned))
    with patch.object(eas.httpx, "AsyncClient", lambda **kw: client):
        with pytest.raises(ValueError):
            await eas._resolve_oidc_metadata(_provider())


@pytest.mark.asyncio
async def test_http_issuer_is_rejected_before_discovery_is_fetched():
    """Kill: validate the issuer only after the GET → ``calls`` is non-empty."""
    client = _Client(_discovery())
    provider = _provider()
    provider.oidc_issuer_url = "http://idp.example"
    with patch.object(eas.httpx, "AsyncClient", lambda **kw: client):
        with pytest.raises(ValueError, match="issuer"):
            await eas._resolve_oidc_metadata(provider)
    assert client.calls == []


@pytest.mark.asyncio
async def test_admin_entered_token_endpoint_is_guarded_too():
    """Explicit branch: three endpoints typed by an admin, issuer set.

    The DB row has no validator of its own (schemas/auth_provider.py), so the
    guard has to sit where the URL is used, not where it came from.
    """
    client = _Client(_discovery())
    provider = _provider(
        authorization_endpoint=_GOOD["authorization_endpoint"],
        token_endpoint=_BAD["loopback"],
        userinfo_endpoint=_GOOD["userinfo_endpoint"],
    )
    with patch.object(eas.httpx, "AsyncClient", lambda **kw: client):
        with pytest.raises(ValueError, match="token_endpoint"):
            await eas._resolve_oidc_metadata(provider)


@pytest.mark.asyncio
async def test_fetch_jwks_fallback_discovery_guards_jwks_uri():
    """``_fetch_jwks`` runs its own second discovery when metadata lacks
    ``jwks_uri``; a poisoned answer there must not be fetched."""
    client = _Client({"jwks_uri": _BAD["metadata"]})
    with pytest.raises(ValueError, match="jwks_uri"):
        await eas._fetch_jwks(client, _provider(), {"issuer": _ISSUER})
    assert client.calls == [f"{_ISSUER}/.well-known/openid-configuration"]


@pytest.mark.asyncio
async def test_fetch_jwks_guards_jwks_uri_already_in_metadata():
    client = _Client({"keys": []})
    with pytest.raises(ValueError, match="jwks_uri"):
        await eas._fetch_jwks(client, _provider(), {"jwks_uri": _BAD["loopback"]})
    assert client.calls == []

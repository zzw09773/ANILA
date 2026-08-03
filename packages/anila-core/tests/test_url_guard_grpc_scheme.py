"""Additive ``grpc`` / ``grpcs`` scheme branch — must not alter http behaviour.

Red line: scheme check stays an if/elif on ``parsed.scheme``, gated by its
own kind/flag. Collapsing into a shared allow-list set is forbidden.
"""
from __future__ import annotations

import pytest

from anila_core.security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

_HTTP_MODEL = "ANILA_ALLOW_HTTP_ENDPOINT"
_HTTP_AGENT = "ANILA_ALLOW_HTTP_AGENT_ENDPOINT"
_GRPC_MODEL = "ANILA_ALLOW_GRPC_ENDPOINT"
_PRIVATE = "ANILA_ALLOW_PRIVATE_ENDPOINT"
_PUBLIC_HTTP = "http://api.example.com/v1"
_PUBLIC_HTTPS = "https://api.example.com/v1"
_GRPC = "grpc://embed.example.com:9001"
_GRPCS = "grpcs://embed.example.com:9001"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        _HTTP_MODEL,
        _HTTP_AGENT,
        _GRPC_MODEL,
        _PRIVATE,
        "ANILA_ENV",
        "ANILA_TRUSTED_HOSTS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ── http behaviour unchanged ────────────────────────────────────────────────

def test_model_http_still_rejected_without_flag():
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)
    assert "ANILA_ALLOW_HTTP_ENDPOINT" in str(exc.value)


def test_model_http_still_ok_with_flag(monkeypatch):
    monkeypatch.setenv(_HTTP_MODEL, "1")
    validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_grpc_flag_does_not_admit_http(monkeypatch):
    """ANILA_ALLOW_GRPC_ENDPOINT must not widen ordinary http:// reach."""
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC_HTTP, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_http_flag_does_not_admit_grpc(monkeypatch):
    monkeypatch.setenv(_HTTP_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)


# ── grpc / grpcs ────────────────────────────────────────────────────────────

def test_model_grpcs_ok_without_cleartext_flag(monkeypatch):
    monkeypatch.setenv(_PRIVATE, "1")  # example.com may resolve private in CI
    # Use a hostname that won't hit private-IP after DNS, or allow private.
    validate_outbound_url(_GRPCS, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_grpc_requires_flag(monkeypatch):
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)
    assert "ANILA_ALLOW_GRPC_ENDPOINT" in str(exc.value)


def test_model_grpc_ok_with_flag(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    monkeypatch.setenv(_PRIVATE, "1")
    validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_agent_grpc_rejected_even_with_flag(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_AGENT)


def test_generic_grpc_rejected(monkeypatch):
    monkeypatch.setenv(_GRPC_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_GRPC, endpoint_kind=ENDPOINT_KIND_GENERIC)


def test_ftp_still_rejected():
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("ftp://api.example.com/v1", endpoint_kind=ENDPOINT_KIND_MODEL)


def test_https_still_ok():
    validate_outbound_url(_PUBLIC_HTTPS, endpoint_kind=ENDPOINT_KIND_MODEL)

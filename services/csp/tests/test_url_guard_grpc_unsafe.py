"""``grpc://`` does not get its own weaker host rules — the always-unsafe set holds.

Two reasons this file lives under ``services/csp/tests/`` rather than beside
the other ``anila_core`` guard tests: ``services/csp/pytest.ini`` sets
``testpaths = tests``, and no CI workflow points at
``packages/anila-core/tests``. The sibling file
``packages/anila-core/tests/test_url_guard_grpc_scheme.py`` covers the scheme
and endpoint-kind matrix but runs under a different rootdir and is in no suite
the project routinely runs, so nothing there would have gone red on a
regression. The strongest property in the gRPC package — that opening
``ANILA_ALLOW_GRPC_ENDPOINT`` widens the *scheme* and nothing else — is the
one that most needs a test somewhere the suite actually reaches.

The flag is set to 1 in **every** test below on purpose: the claim under test
is that even fully opted in, ``grpc://`` cannot reach loopback, link-local,
multicast, cloud metadata, or an unresolvable single-label docker service name.
"""
from __future__ import annotations

import pytest

from anila_core.security import UnsafeEndpointError, validate_outbound_url

_MODEL = "model"


@pytest.fixture(autouse=True)
def _grpc_fully_opted_in(monkeypatch):
    """Most permissive posture the operator can choose."""
    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ENV", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "grpc://127.0.0.1:9001",
        "grpc://localhost:9001",
        "grpc://127.1.2.3:9001",
        "grpc://[::1]:9001",
        "grpcs://127.0.0.1:9001",
        "grpcs://localhost:9001",
    ],
)
def test_loopback_blocked_even_with_grpc_flag(url):
    """csp reaching its own listeners over gRPC is the SSRF pivot, not a feature."""
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(url, _MODEL)


@pytest.mark.parametrize(
    "url",
    [
        "grpc://169.254.1.1:9001",
        "grpcs://169.254.1.1:9001",
        "grpc://[fe80::1]:9001",
    ],
)
def test_link_local_blocked_even_with_grpc_flag(url):
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(url, _MODEL)


@pytest.mark.parametrize(
    "url",
    [
        "grpc://169.254.169.254:9001",
        "grpcs://169.254.169.254:9001",
        "grpc://metadata.google.internal:9001",
    ],
)
def test_cloud_metadata_blocked_even_with_grpc_flag(url):
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(url, _MODEL)


@pytest.mark.parametrize(
    "url",
    [
        "grpc://224.0.0.1:9001",
        "grpc://239.255.255.250:9001",
        "grpcs://224.0.0.1:9001",
        "grpc://0.0.0.0:9001",
    ],
)
def test_multicast_and_unspecified_blocked_even_with_grpc_flag(url):
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(url, _MODEL)


@pytest.mark.parametrize(
    "url",
    ["grpc://csp-db:9001", "grpc://redis:9001", "grpcs://csp:9001"],
)
def test_untrusted_single_label_service_name_blocked(url, monkeypatch):
    """Single-label docker service names need ANILA_TRUSTED_HOSTS, gRPC included."""
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(url, _MODEL)


def test_grpc_flag_does_not_widen_anything_but_the_scheme(monkeypatch):
    """The one thing the flag is allowed to change, and its exact boundary.

    Opting in admits cleartext ``grpc://`` to a routable host and nothing
    else; with the flag off, the same URL is refused on the scheme.
    """
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "triton.example.com")
    validate_outbound_url("grpc://triton.example.com:9001", _MODEL)

    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "0")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("grpc://triton.example.com:9001", _MODEL)
    assert "ANILA_ALLOW_GRPC_ENDPOINT" in str(exc.value)

    # …and grpcs never needed the flag in the first place.
    validate_outbound_url("grpcs://triton.example.com:9001", _MODEL)

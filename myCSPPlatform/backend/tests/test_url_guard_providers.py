"""``register_trusted_host_provider`` hook + structured ``UnsafeEndpointError``.

Phase 2 (trusted_hosts DB-driven) introduces a callable hook so CSP can
plug a cached DB query into ``validate_outbound_url`` without anila-core
depending on the platform schema. This file pins the contract:

1. Providers stack additively on top of the env-based fallback.
2. A misbehaving provider (raises) must NOT weaken security — guard
   still enforces env + previously-resolved providers.
3. ``UnsafeEndpointError`` carries typed ``host`` / ``reason`` /
   ``fixable_by_trust_host`` so the API layer can render a confirm
   modal instead of an opaque alert.
"""
from __future__ import annotations

import pytest

from anila_core.security import (
    UnsafeEndpointError,
    clear_trusted_host_providers,
    register_trusted_host_provider,
    validate_outbound_url,
)
from anila_core.security.url_guard import (
    FIXABLE_BY_TRUST_HOST,
    REASON_DENY_HOST,
    REASON_INTERNAL_ZONE,
    REASON_PRIVATE_IP,
    REASON_SCHEME,
    REASON_SINGLE_LABEL,
    REASON_UNSAFE_IP,
)


@pytest.fixture(autouse=True)
def _reset_providers():
    """Each test starts with no registered providers so we don't leak
    state across tests / between this file and existing url_guard tests."""
    clear_trusted_host_providers()
    yield
    clear_trusted_host_providers()


# ── provider hook contract ────────────────────────────────────────────────────


def test_provider_adds_to_env_trusted_hosts(monkeypatch):
    """DB provider's hosts should be honored alongside env's."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "from-env")
    register_trusted_host_provider(lambda: {"from-provider"})

    validate_outbound_url("http://from-env:8000/v1")
    validate_outbound_url("http://from-provider:8000/v1")


def test_provider_only_no_env(monkeypatch):
    """Provider alone is sufficient — env can be empty."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    register_trusted_host_provider(lambda: {"db-only-host"})

    validate_outbound_url("http://db-only-host:8000/v1")


def test_provider_exception_swallowed_env_still_enforces(monkeypatch):
    """If a provider raises, validation must fall back to env (fail-safe),
    NOT bypass the host check entirely."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "safe-from-env")

    def boom():
        raise RuntimeError("DB down")

    register_trusted_host_provider(boom)

    # env-listed still works
    validate_outbound_url("http://safe-from-env:8000/v1")
    # provider raised → fallback to env-only → other hosts still blocked
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://not-in-env:8000/v1")


def test_multiple_providers_unioned(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    register_trusted_host_provider(lambda: {"host-a"})
    register_trusted_host_provider(lambda: {"host-b"})

    validate_outbound_url("http://host-a:8000/v1")
    validate_outbound_url("http://host-b:8000/v1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://host-c:8000/v1")


def test_provider_results_lowercased(monkeypatch):
    """Hostname compare is case-insensitive — provider can return mixed case."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    register_trusted_host_provider(lambda: {"MixedCaseHost"})

    validate_outbound_url("http://mixedcasehost:8000/v1")
    validate_outbound_url("http://MIXEDCASEHOST:8000/v1")


# ── structured exception ──────────────────────────────────────────────────────


def test_single_label_failure_is_fixable(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://foobar:8000/v1")
    assert exc.value.host == "foobar"
    assert exc.value.reason == REASON_SINGLE_LABEL
    assert exc.value.fixable_by_trust_host is True


def test_internal_zone_failure_is_fixable(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://inference.internal/v1")
    assert exc.value.host == "inference.internal"
    assert exc.value.reason == REASON_INTERNAL_ZONE
    assert exc.value.fixable_by_trust_host is True


@pytest.mark.parametrize("url,expected_reason", [
    ("http://127.0.0.1/v1", REASON_UNSAFE_IP),
    ("http://localhost/v1", REASON_DENY_HOST),
    # 169.254.169.254 是 cloud metadata,在 _DENY_HOSTS 字面 deny list
    # 內 → reason 是 deny_host (在 link-local IP 檢查之前先 fail)
    ("http://169.254.169.254/latest/", REASON_DENY_HOST),
    # 169.254.1.1 是同一個 link-local /16 但不是 metadata literal → unsafe_ip
    ("http://169.254.1.1/v1", REASON_UNSAFE_IP),
    ("http://10.0.0.1/v1", REASON_PRIVATE_IP),
])
def test_unsafe_failures_not_fixable_by_trust(monkeypatch, url, expected_reason):
    """loopback / metadata / link-local / private IP should NEVER be
    fixable by adding to trusted_hosts — those are structural rejections.
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(url)
    assert exc.value.reason == expected_reason
    assert exc.value.fixable_by_trust_host is False


def test_scheme_failure_not_fixable(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://example.com/v1")
    assert exc.value.reason == REASON_SCHEME
    assert exc.value.fixable_by_trust_host is False


def test_fixable_set_contract():
    """Sanity: only the two genuinely fixable reasons are in FIXABLE_BY_TRUST_HOST."""
    assert REASON_SINGLE_LABEL in FIXABLE_BY_TRUST_HOST
    assert REASON_INTERNAL_ZONE in FIXABLE_BY_TRUST_HOST
    # Negative cases (the dangerous ones should NEVER be in here)
    assert REASON_UNSAFE_IP not in FIXABLE_BY_TRUST_HOST
    assert REASON_DENY_HOST not in FIXABLE_BY_TRUST_HOST
    assert REASON_PRIVATE_IP not in FIXABLE_BY_TRUST_HOST
    assert REASON_SCHEME not in FIXABLE_BY_TRUST_HOST


def test_str_exception_still_human_readable():
    """str(exc) must still return the message for legacy plain-string consumers."""
    exc = UnsafeEndpointError("hello world", host="x", reason=REASON_SINGLE_LABEL)
    assert str(exc) == "hello world"

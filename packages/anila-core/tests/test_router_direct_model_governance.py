"""R7.1: Router direct-answer model ceiling auto-derived from CSP governance.

These cover the CSP fetch transport and the TTL provider that replaces the old
manual direct-answer ceiling env knob with registry-derived governance.  The
pure PolicyGate direct-answer semantics stay covered by
``test_router_runtime_core.py`` and are not duplicated here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from anila_contracts.classification import ClassificationLevel
from anila_core.router.csp_registry_client import (
    CspDirectModelGovernanceClient,
    DirectModelGovernanceProvider,
    DirectModelGovernanceUnavailable,
)
from anila_core.router.policy_gate import DirectModelGovernance


def _governance(ceiling: str = "機密") -> DirectModelGovernance:
    return DirectModelGovernance(
        model_id="google/gemma4",
        gateway="csp",
        classification_ceiling=ClassificationLevel.from_storage(ceiling),
    )


# ── CSP fetch transport ────────────────────────────────────────────────────


def test_governance_client_named_token_and_payload_parse() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "model_id": "google/gemma4",
                "gateway": "csp",
                "classification_ceiling": "機密",
            },
            request=request,
        )

    client = CspDirectModelGovernanceClient(
        "https://csp.test",
        service_token="csk-router-primary",
        transport=httpx.MockTransport(_handler),
    )
    governance = asyncio.run(client.fetch("google/gemma4"))

    assert governance.model_id == "google/gemma4"
    assert governance.gateway == "csp"
    assert governance.classification_ceiling == ClassificationLevel.from_storage("機密")
    assert captured["path"] == "/internal/v1/router/direct-model-governance"
    assert captured["query"] == {"model": "google/gemma4"}
    assert captured["headers"]["x-csp-service-token"] == "csk-router-primary"
    assert "authorization" not in captured["headers"]


def test_governance_client_placeholder_token_fails_closed() -> None:
    client = CspDirectModelGovernanceClient(
        "https://csp.test", service_token="not-set"
    )
    with pytest.raises(DirectModelGovernanceUnavailable):
        asyncio.run(client.fetch("google/gemma4"))


def test_governance_client_http_404_fails_closed() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "未註冊"}, request=request)

    client = CspDirectModelGovernanceClient(
        "https://csp.test",
        service_token="csk-router-primary",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(DirectModelGovernanceUnavailable):
        asyncio.run(client.fetch("google/gemma4"))


def test_governance_client_model_id_mismatch_fails_closed() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "other-model",
                "gateway": "csp",
                "classification_ceiling": "機密",
            },
            request=request,
        )

    client = CspDirectModelGovernanceClient(
        "https://csp.test",
        service_token="csk-router-primary",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(DirectModelGovernanceUnavailable):
        asyncio.run(client.fetch("google/gemma4"))


def test_governance_client_invalid_ceiling_fails_closed() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "google/gemma4",
                "gateway": "csp",
                "classification_ceiling": "not-a-level",
            },
            request=request,
        )

    client = CspDirectModelGovernanceClient(
        "https://csp.test",
        service_token="csk-router-primary",
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(DirectModelGovernanceUnavailable):
        asyncio.run(client.fetch("google/gemma4"))


# ── TTL provider ───────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_provider_fetch_success_returns_governance_within_ttl() -> None:
    calls: list[str] = []

    async def _fetch(model: str) -> DirectModelGovernance:
        calls.append(model)
        return _governance("機密")

    clock = _FakeClock()
    provider = DirectModelGovernanceProvider(
        model="google/gemma4", fetch=_fetch, ttl_seconds=300.0, clock=clock
    )

    first = asyncio.run(provider.get())
    assert first is not None
    assert first.classification_ceiling == ClassificationLevel.from_storage("機密")

    # Within the TTL a second call is served from cache — no extra fetch.
    clock.now = 100.0
    second = asyncio.run(provider.get())
    assert second is first
    assert calls == ["google/gemma4"]


def test_provider_fetch_failure_returns_none_and_denies() -> None:
    async def _fetch(model: str) -> DirectModelGovernance:
        raise DirectModelGovernanceUnavailable("boom")

    provider = DirectModelGovernanceProvider(
        model="google/gemma4", fetch=_fetch, ttl_seconds=300.0, clock=_FakeClock()
    )
    assert asyncio.run(provider.get()) is None


def test_provider_ttl_expiry_refetches_and_lowered_ceiling_takes_effect() -> None:
    ceilings = ["機密", "無機密"]

    async def _fetch(model: str) -> DirectModelGovernance:
        return _governance(ceilings.pop(0))

    clock = _FakeClock()
    provider = DirectModelGovernanceProvider(
        model="google/gemma4", fetch=_fetch, ttl_seconds=300.0, clock=clock
    )

    first = asyncio.run(provider.get())
    assert first is not None
    assert first.classification_ceiling == ClassificationLevel.from_storage("機密")

    # After one TTL the registry lowered the ceiling; the provider must refetch
    # and surface the new (lower) ceiling.
    clock.now = 300.0
    second = asyncio.run(provider.get())
    assert second is not None
    assert second.classification_ceiling == ClassificationLevel.from_storage("無機密")


def test_provider_denies_until_refresh_after_expiry_failure() -> None:
    state = {"fail": False}

    async def _fetch(model: str) -> DirectModelGovernance:
        if state["fail"]:
            raise DirectModelGovernanceUnavailable("registry down")
        return _governance("機密")

    clock = _FakeClock()
    provider = DirectModelGovernanceProvider(
        model="google/gemma4", fetch=_fetch, ttl_seconds=300.0, clock=clock
    )

    assert asyncio.run(provider.get()) is not None

    # Cache expires and the refetch fails — never serve the stale value.
    state["fail"] = True
    clock.now = 300.0
    assert asyncio.run(provider.get()) is None

    # Recovery within a later TTL window re-establishes governance.
    state["fail"] = False
    clock.now = 600.0
    assert asyncio.run(provider.get()) is not None

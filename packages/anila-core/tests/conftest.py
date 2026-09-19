"""Shared offline defaults for the anila-core test suite.

The Router resolves its base LLM against CSP on every turn
(``_csp_resolve_router_model``, added in 20e9c084) and fails closed when CSP
is unreachable. Most router tests are networkless and mock the LLM itself, so
this hop would otherwise hit whatever happens to answer on
``settings.csp_base_url`` and 404 the whole turn.

Default the hop off. Returning ``None`` reproduces the pre-feature behaviour
(the shared/env model wins).

Tests that DO want to exercise the real hop (e.g. a respx-mocked
``/api/router-models/resolve``) opt out with::

    @pytest.mark.real_router_model_resolve

otherwise this autouse stub would silently swallow their request and they
would pass without ever exercising the resolve contract.
"""

from __future__ import annotations

import pytest

from anila_core.api import router_server


@pytest.fixture(autouse=True)
def _no_per_request_router_model(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real_router_model_resolve"):
        return

    async def _resolve_none(
        request: object, caller_api_key: str, body: dict | None
    ) -> None:
        return None

    monkeypatch.setattr(router_server, "_csp_resolve_router_model", _resolve_none)

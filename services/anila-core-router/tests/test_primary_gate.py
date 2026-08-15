"""``_gate_on_primary`` must key off the path the router dispatches on.

When CSP has no primary routing model the Router answers 503 with an
instruction for the administrator, rather than forwarding to whatever
upstream happens to answer. That gate used to be selected on
``request.url.path``, which starlette < 1.0.1 assembles by concatenating
the caller's raw ``Host`` header with the path (CVE-2026-48710) — so
``Host: x/v1`` made the comparison miss, the gate was skipped, and the
request went on to the chat-completions endpoint ungated.

Two shapes of test, because neither alone is enough:

- ``Host``-based, which is what an attacker actually sends. Vacuous on
  starlette >= 1.0.1, where the header can no longer move the path.
- A substituted **public** ``url`` property, which keeps the fix
  observable on the version we install and stands in for any future
  library regression or a downgraded starlette.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.requests import HTTPConnection

GATED_PATH = "/v1/chat/completions"

# Host headers carrying a path segment, each one moving the path boundary
# so that ``url.path`` stops equalling ``GATED_PATH``.
POLLUTED_HOSTS = ["x/v1", "x/health", "x/v1/chat", "x/openapi.json"]

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
ACTIONABLE_DETAIL = (
    "ANILA Router 無可用主路由模型。"
    "請管理員前往 CSP Models 頁面指定一個 LLM 為「主路由」。"
)


@pytest.fixture
def url_hides_the_gated_path(monkeypatch):
    """``request.url`` prefixes the routed path so the ``==`` never matches."""
    monkeypatch.setattr(
        HTTPConnection,
        "url",
        property(lambda self: URL(f"http://attacker.example/x{self.scope['path']}")),
    )


@pytest.mark.parametrize("host", POLLUTED_HOSTS)
def test_polluted_host_cannot_skip_the_gate(client: TestClient, no_primary, host):
    resp = client.post(GATED_PATH, headers={"Host": host}, json=BODY)
    assert resp.status_code == 503, resp.text


def test_gate_holds_through_a_library_regression(
    client: TestClient, no_primary, url_hides_the_gated_path
):
    resp = client.post(GATED_PATH, json=BODY)
    assert resp.status_code == 503, resp.text


def test_clean_request_is_gated(client: TestClient, no_primary):
    """Control: the gate really does fire on an ordinary request."""
    resp = client.post(GATED_PATH, json=BODY)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == ACTIONABLE_DETAIL


def test_primary_guard_logs_downstream_detail_but_not_to_user(
    client: TestClient, monkeypatch, caplog
):
    downstream_detail = 'CSP 404: {"detail":"尚未指定 ANILA 主路由模型"}'

    async def _ensure_primary():
        return None, downstream_detail

    monkeypatch.setattr(router_main, "_ensure_primary", _ensure_primary)
    caplog.set_level("WARNING", logger="anila-router")

    resp = client.post(GATED_PATH, json=BODY)

    assert resp.status_code == 503, resp.text
    assert resp.json() == {"detail": ACTIONABLE_DETAIL}
    assert downstream_detail in caplog.text
    assert downstream_detail not in resp.json()["detail"]


def test_gate_does_not_fire_when_a_primary_exists(client: TestClient, with_primary):
    """Control: the fix must not turn a working Router into a 503 machine."""
    resp = client.post(GATED_PATH, json=BODY)
    assert resp.status_code != 503, resp.text


def test_health_is_never_gated(client: TestClient, no_primary):
    """Control: only the chat-completions path is gated."""
    resp = client.get("/health")
    assert resp.status_code != 503, resp.text

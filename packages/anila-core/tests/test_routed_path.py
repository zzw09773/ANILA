"""``routed_path`` is now the single line six security decisions rest on.

Consolidating the six call sites means there is one place to keep correct —
and one place whose revert has to stay visible. On starlette >= 1.0.1
``request.url.path`` and ``scope["path"]`` are always equal, so a revert of
the helper's body cannot be detected by any ``Host`` header. These tests
substitute the **public** ``url`` property instead, which is the only
mechanism that separates the two strings on the version we ship.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import URL
from starlette.requests import Request

from anila_core.api.routing import routed_path

ROUTED = "/v1/chat/completions"


def _scope(path: str, host: str = "router.internal") -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "server": ("router", 9000),
        "headers": [(b"host", host.encode())],
    }


class _PollutedUrlRequest(Request):
    """A request whose public ``url`` disagrees with its routed path."""

    @property
    def url(self) -> URL:
        return URL(f"http://attacker.example/health{self.scope['path']}")


def test_returns_the_scope_path():
    assert routed_path(Request(_scope(ROUTED))) == ROUTED


def test_ignores_a_substituted_url():
    request = _PollutedUrlRequest(_scope(ROUTED))

    # Premise first — if the substitution stops taking effect this test
    # fails loudly rather than passing for the wrong reason.
    assert request.url.path == "/health" + ROUTED

    assert routed_path(request) == ROUTED


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example/health",
        "attacker.example/v1",
        "attacker.example/api/auth/login",
    ],
)
def test_ignores_a_path_carrying_host_header(host):
    """Vacuous on starlette >= 1.0.1; the real defence is the test above."""
    assert routed_path(Request(_scope(ROUTED, host=host))) == ROUTED

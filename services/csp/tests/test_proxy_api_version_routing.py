"""/v1/chat/completions non-streaming dispatch must respect model.api_version.

The streaming branch (app/api/proxy.py chat_completions, ~L797-802) already
picks "/v2/chat/completions" vs "/v1/chat/completions" based on the
registered model's ``api_version``. The non-streaming branch (~L838-845)
used to hard-code ``endpoint_path="/v1/chat/completions"`` unconditionally,
so a v2-registered model's non-streaming calls were dispatched to the wrong
upstream path. This locks the fix and pins the (already-correct) v1 default
as a regression guard.
"""
from __future__ import annotations

import json as _json
import os

# Same house pattern as test_proxy_task_wiring.py: endpoint tests boot the
# app, and startup_security blocks the dev default secret in production mode.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services import proxy_service
from app.services.auth_service import create_tokens

from tests.conftest import make_model, make_user


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    """make_model's default endpoint is a single-label http host; only
    passes the call-time SSRF guard with the dev allowances set."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _UrlCapturingClient:
    """Fake httpx.AsyncClient recording the URL passed to .post()."""

    last_url: str = ""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_url = url
        return _PostResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )


def _patch_client(monkeypatch):
    _UrlCapturingClient.last_url = ""
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _UrlCapturingClient(*a, **k)
    )


def test_non_stream_v2_model_dispatches_to_v2_chat_completions(
    client: TestClient, db: Session, monkeypatch,
):
    """Regression: a model_registry row with api_version='v2' must dispatch
    its non-streaming call to /v2/chat/completions, not /v1."""
    admin = make_user(db, username="v2_admin", role="admin")
    model = make_model(db, name="v2-llm")
    model.api_version = "v2"
    db.commit()
    _patch_client(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(create_tokens(admin)["access_token"]),
        json={"model": "v2-llm", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200, resp.text
    assert _UrlCapturingClient.last_url == "http://mock-llm:8080/v2/chat/completions"


def test_non_stream_v1_model_still_dispatches_to_v1_chat_completions(
    client: TestClient, db: Session, monkeypatch,
):
    """Guard against the v2 fix regressing the (default) v1 path."""
    admin = make_user(db, username="v1_admin", role="admin")
    make_model(db, name="v1-llm")  # api_version defaults to 'v1'
    _patch_client(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(create_tokens(admin)["access_token"]),
        json={"model": "v1-llm", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200, resp.text
    assert _UrlCapturingClient.last_url == "http://mock-llm:8080/v1/chat/completions"

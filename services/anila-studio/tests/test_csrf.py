"""Cookie 工作階段打 Studio 的變更請求必須過 double-submit CSRF。

語意對齊 CSP：``anila_csrf`` cookie 與 ``X-CSRF-Token`` 要相同；
``Authorization: Bearer`` 豁免；GET 不檢查。判斷路徑用路由看到的
path，不用會被 Host 污染的 ``request.url.path``。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.requests import HTTPConnection, Request

ACCESS_COOKIE = "anila_access_token"
CSRF_COOKIE = "anila_csrf"
JOBS = "/api/studio/slides/jobs"


@pytest.fixture
def studio_app(monkeypatch):
    from app.services import job_store as job_store_mod
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod

    async def _noop(app=None):
        return None

    monkeypatch.setattr(jwks_mod, "start", _noop)
    monkeypatch.setattr(jwks_mod, "stop", _noop)

    class _Ready:
        ready = True

        async def start(self, app=None):
            return None

        async def stop(self, app=None):
            return None

    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: _Ready())
    monkeypatch.setattr(job_store_mod, "get_job_store", lambda: _Ready())

    from app.main import app

    return app


def _client(app, **cookies: str) -> TestClient:
    client = TestClient(app)
    for name, value in cookies.items():
        client.cookies.set(name, value)
    return client


def test_cookie_mutation_without_csrf_is_403(studio_app):
    with _client(studio_app, **{ACCESS_COOKIE: "session"}) as client:
        resp = client.post(JOBS, json={"collection_id": 1})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "CSRF 驗證失敗，請重新登入"


def test_cookie_mutation_rejects_a_mismatched_csrf_header(studio_app):
    with _client(
        studio_app, **{ACCESS_COOKIE: "session", CSRF_COOKIE: "fresh"}
    ) as client:
        resp = client.post(
            JOBS,
            json={"collection_id": 1},
            headers={"X-CSRF-Token": "stale"},
        )
    assert resp.status_code == 403, resp.text


def test_matching_csrf_is_not_blocked_by_the_middleware(studio_app):
    """標頭與 cookie 一致時，請求要進到認證，而不是停在 CSRF。"""
    with _client(
        studio_app, **{ACCESS_COOKIE: "not-a-jwt", CSRF_COOKIE: "fresh"}
    ) as client:
        resp = client.post(
            JOBS,
            json={"collection_id": 1},
            headers={"X-CSRF-Token": "fresh"},
        )
    assert resp.status_code != 403, resp.text


def test_bearer_request_is_csrf_exempt(studio_app):
    with _client(
        studio_app, **{ACCESS_COOKIE: "session", CSRF_COOKIE: "fresh"}
    ) as client:
        resp = client.post(
            JOBS,
            json={"collection_id": 1},
            headers={"Authorization": "Bearer other-token"},
        )
    assert resp.status_code != 403, resp.text


def test_get_with_session_cookie_does_not_require_csrf(studio_app):
    with _client(studio_app, **{ACCESS_COOKIE: "session"}) as client:
        resp = client.get("/health")
    assert resp.status_code != 403, resp.text


class _PollutedUrlRequest(Request):
    @property
    def url(self) -> URL:
        return URL(f"http://attacker.example/health{self.scope['path']}")


def test_should_skip_reads_the_routed_path():
    """url.path 被拼上 /health 時仍要檢查 CSRF。"""
    from app.middleware.csrf import _should_skip

    request = _PollutedUrlRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": JOBS,
            "raw_path": JOBS.encode(),
            "headers": [(b"cookie", b"anila_access_token=session")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 50000),
        }
    )
    assert request.url.path == f"/health{JOBS}"
    assert request.scope["path"] == JOBS
    assert _should_skip(request) is False


@pytest.fixture
def polluted_url(monkeypatch):
    """讓 request.url.path 看起來落在豁免前綴，路由路徑不變。"""

    def _fake_url(self) -> URL:
        return URL(f"http://attacker.example/health{self.scope['path']}")

    monkeypatch.setattr(HTTPConnection, "url", property(_fake_url))


def test_host_polluted_url_does_not_skip_csrf(studio_app, polluted_url):
    with _client(studio_app, **{ACCESS_COOKIE: "session"}) as client:
        resp = client.post(JOBS, json={"collection_id": 1})
    assert resp.status_code == 403, resp.text

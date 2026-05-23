"""Integration smoke for anila-studio's FastAPI app — lifespan + /health.

Verifies the wiring main.py does:
- lifespan startup: jwks_client.start() + revocation_cache.start() both called
- /health returns 200 + ready=True when both deps are healthy
- /health returns 503 + ready=False when revocation_cache.ready is False
- lifespan shutdown: both stops are called

The individual deps have their own deep tests (test_jwks_client,
test_revocation_cache); this file only asserts they are correctly wired
into the FastAPI lifespan.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def stub_deps(monkeypatch):
    """Patch jwks_client + revocation_cache so lifespan doesn't try to
    hit a real network / Redis during these smoke tests.

    Tracks call counts to verify wiring; the dep tests already cover
    the dep behaviour itself.
    """
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod

    calls = {
        "jwks_start": 0,
        "jwks_stop": 0,
        "rev_start": 0,
        "rev_stop": 0,
    }

    async def fake_jwks_start(app):
        calls["jwks_start"] += 1

    async def fake_jwks_stop(app):
        calls["jwks_stop"] += 1

    monkeypatch.setattr(jwks_mod, "start", fake_jwks_start)
    monkeypatch.setattr(jwks_mod, "stop", fake_jwks_stop)

    class _FakeCache:
        def __init__(self):
            self._ready = True

        @property
        def ready(self):
            return self._ready

        def set_ready(self, flag: bool):
            self._ready = flag

        async def start(self, app):
            calls["rev_start"] += 1
            self._ready = True

        async def stop(self, app):
            calls["rev_stop"] += 1
            self._ready = False

    fake_cache = _FakeCache()
    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: fake_cache)

    return calls, fake_cache


def test_health_ok_after_lifespan_startup(stub_deps):
    """/health returns 200 + ready=true once lifespan startup completes."""
    from app.main import app

    calls, _ = stub_deps
    with TestClient(app) as client:
        # TestClient context-manager triggers lifespan
        resp = client.get("/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["ready"] is True
        assert body["service"] == "anila-studio"
        assert "version" in body
        assert body["deps"]["revocation_cache"] is True

    # Both deps got started during enter, stopped during exit
    assert calls["jwks_start"] == 1
    assert calls["rev_start"] == 1
    assert calls["jwks_stop"] == 1
    assert calls["rev_stop"] == 1


def test_health_503_when_revocation_cache_not_ready(stub_deps):
    """If revocation_cache.ready=False (Redis disconnected), /health 503s."""
    from app.main import app

    calls, fake_cache = stub_deps
    with TestClient(app) as client:
        # Simulate Redis disconnect after startup
        fake_cache.set_ready(False)
        resp = client.get("/health")
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["ready"] is False
        assert body["deps"]["revocation_cache"] is False


def test_lifespan_failure_propagates(monkeypatch):
    """If a dep's start() raises, lifespan does not silently swallow."""
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod
    from app.main import app

    class _BoomError(RuntimeError):
        pass

    async def boom_start(app):
        raise _BoomError("simulated jwks failure")

    monkeypatch.setattr(jwks_mod, "start", boom_start)

    # Need a stub revocation cache too in case order matters
    class _NoopCache:
        ready = True

        async def start(self, app):
            pass

        async def stop(self, app):
            pass

    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: _NoopCache())

    with pytest.raises(_BoomError):
        with TestClient(app):
            pass  # lifespan startup runs on __enter__

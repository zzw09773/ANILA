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
    from app.services import job_reporting as reporting_mod
    from app.services import job_store as store_mod
    from app.services import job_supervisor as supervisor_mod
    from app.services import revocation_cache as rev_mod

    calls = {
        "jwks_start": 0,
        "jwks_stop": 0,
        "rev_start": 0,
        "rev_stop": 0,
        "artifact_probe": 0,
        "job_store_start": 0,
        "job_store_stop": 0,
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

    async def fake_artifact_probe():
        calls["artifact_probe"] += 1
        return True

    monkeypatch.setattr(reporting_mod, "probe_readiness", fake_artifact_probe)
    monkeypatch.setattr(
        reporting_mod,
        "reporting_status",
        lambda: {"ready": True, "enabled": True, "configured": True},
    )

    class _FakeJobStore:
        ready = True
        last_error = None

        async def start(self, app):
            calls["job_store_start"] += 1

        async def stop(self, app):
            calls["job_store_stop"] += 1

        async def probe(self):
            return self.ready

    fake_store = _FakeJobStore()
    monkeypatch.setattr(store_mod, "get_job_store", lambda: fake_store)

    class _FakeSupervisor:
        running = True

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

    fake_supervisor = _FakeSupervisor()
    monkeypatch.setattr(
        supervisor_mod,
        "get_job_supervisor",
        lambda: fake_supervisor,
    )

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
        assert body["deps"]["artifact_reporting"]["ready"] is True

    # Both deps got started during enter, stopped during exit
    assert calls["jwks_start"] == 1
    assert calls["rev_start"] == 1
    assert calls["artifact_probe"] == 1
    assert calls["job_store_start"] == 1
    assert calls["job_store_stop"] == 1
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


def test_health_503_when_artifact_reporting_not_ready(stub_deps, monkeypatch):
    from app.main import app
    from app.services import job_reporting

    monkeypatch.setattr(
        job_reporting,
        "reporting_status",
        lambda: {
            "ready": False,
            "enabled": True,
            "configured": True,
            "last_error": "CSP unavailable",
        },
    )
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert body["deps"]["revocation_cache"] is True
        assert body["deps"]["artifact_reporting"]["ready"] is False


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


def test_formal_profile_refuses_disabled_durable_supervisor(monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card")
    monkeypatch.setattr(settings, "STUDIO_DURABLE_SUPERVISOR", False)
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_REPORTING", True)
    monkeypatch.setattr(settings, "STUDIO_ARTIFACT_SERVICE_TOKEN", "csk-writer")
    monkeypatch.setattr(settings, "STUDIO_RUNTIME_SERVICE_TOKEN", "csk-runtime")

    with pytest.raises(RuntimeError, match="DURABLE_SUPERVISOR"):
        with TestClient(app):
            pass

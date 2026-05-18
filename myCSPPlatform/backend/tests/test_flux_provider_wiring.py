"""get_flux_provider() — env-var wiring for FLUX integration."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def reset_studio_module():
    """Each test gets a fresh studio module so env changes take effect."""
    import app.api.studio as studio
    yield
    importlib.reload(studio)


def test_provider_is_none_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    import app.api.studio as studio
    importlib.reload(studio)
    assert studio.get_flux_provider() is None


def test_provider_built_when_url_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "flux-cache"))
    monkeypatch.setenv("FLUX_MAX_CONCURRENT", "2")
    import app.api.studio as studio
    importlib.reload(studio)

    p = studio.get_flux_provider()
    assert p is not None
    assert p.flux_url == "http://flux2-dev:8000"
    assert p.max_concurrent == 2


def test_provider_uses_default_cache_dir(monkeypatch):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.delenv("FLUX_CACHE_DIR", raising=False)
    import app.api.studio as studio
    importlib.reload(studio)

    p = studio.get_flux_provider()
    assert p is not None
    # Default sits under INGESTION_UPLOAD_DIR
    assert "flux-cache" in str(p.cache_dir)


def test_provider_is_singleton_within_module(monkeypatch, tmp_path):
    """Two calls return the same object — semaphore state shared."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    import app.api.studio as studio
    importlib.reload(studio)

    a = studio.get_flux_provider()
    b = studio.get_flux_provider()
    assert a is b

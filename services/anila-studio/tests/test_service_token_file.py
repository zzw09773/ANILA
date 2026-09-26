"""anila-studio 只讀 CSP 寫好的憑證檔。

路徑有設時，檔案不在或讀不到就失敗即關閉，不改用 CSP_SERVICE_TOKEN。
檔案變了要重讀；CSP 回 401／403 時再讀一次。/health 只報來源，不報明文。
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings


def _reload():
    from app import service_token

    service_token.reload(force=True)
    return service_token


def test_configured_file_is_the_only_credential(tmp_path, monkeypatch):
    secret = "csk-studio-from-file"
    path = tmp_path / "anila-studio.token"
    path.write_text(secret + "\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-legacy-env", raising=False)
    token = _reload()
    assert token.source() == "file"
    assert token.token() == secret
    assert token.headers() == {"X-CSP-Service-Token": secret}
    assert token.health() == {"token_source": "file"}
    assert secret not in str(token.health())


def test_missing_file_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-legacy-env", raising=False)
    token = _reload()
    assert token.source() == "file_missing"
    assert token.token() == ""
    assert token.headers() == {}
    assert "csk-legacy-env" not in str(token.health())


def test_unreadable_file_does_not_fall_back(tmp_path, monkeypatch):
    secret = "csk-unreadable"
    path = tmp_path / "anila-studio.token"
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0)
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-legacy-env", raising=False)
    try:
        token = _reload()
        assert token.source() == "file_error"
        assert token.token() == ""
        assert token.headers() == {}
    finally:
        path.chmod(0o600)


def test_reread_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "anila-studio.token"
    path.write_text("csk-first\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    token = _reload()
    assert token.token() == "csk-first"
    path.write_text("csk-second\n", encoding="utf-8")
    assert token.token() == "csk-second"
    assert token.source() == "file"


@pytest.mark.asyncio
@respx.mock
async def test_image_primary_rereads_once_after_401(tmp_path, monkeypatch):
    path = tmp_path / "anila-studio.token"
    path.write_text("csk-stale\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "csk-legacy-env", raising=False)
    _reload()
    import app.services.flux_image_primary as mod

    url = f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/image-primary"
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            path.write_text("csk-fresh\n", encoding="utf-8")
            return httpx.Response(401, json={"detail": "stale"})
        assert request.headers["x-csp-service-token"] == "csk-fresh"
        return httpx.Response(
            200,
            json={
                "endpoint_url": "https://gw.example/v1",
                "name": "flux.2-pro",
            },
        )

    respx.get(url).mock(side_effect=respond)
    endpoint, model = await mod.get_image_primary()
    assert calls["n"] == 2
    assert endpoint == "https://gw.example/v1"
    assert model == "flux.2-pro"


def test_health_reports_token_source_without_the_secret(tmp_path, monkeypatch):
    secret = "csk-do-not-print"
    path = tmp_path / "anila-studio.token"
    path.write_text(secret + "\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod
    from app.main import app

    async def _noop(_app):
        return None

    class _Ready:
        ready = True

        async def start(self, _app):
            return None

        async def stop(self, _app):
            return None

    monkeypatch.setattr(jwks_mod, "start", _noop)
    monkeypatch.setattr(jwks_mod, "stop", _noop)
    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: _Ready())
    _reload()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_source"] == "file"
    assert secret not in response.text


def test_health_is_degraded_when_the_configured_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    from app.services import jwks_client as jwks_mod
    from app.services import revocation_cache as rev_mod
    from app.main import app

    async def _noop(_app):
        return None

    class _Ready:
        ready = True

        async def start(self, _app):
            return None

        async def stop(self, _app):
            return None

    monkeypatch.setattr(jwks_mod, "start", _noop)
    monkeypatch.setattr(jwks_mod, "stop", _noop)
    monkeypatch.setattr(rev_mod, "get_revocation_cache", lambda: _Ready())
    _reload()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["token_source"] == "file_missing"
    assert body["ready"] is False

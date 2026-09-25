"""The router takes its CSP credential from the provisioned token file."""
from __future__ import annotations

import json
import os
import time

import pytest

import main as router_main
from anila_core.api import router_server as router_server
from anila_core.config import settings


def _reset_token_resolution() -> None:
    os.environ.pop("ANILA_SERVICE_TOKEN_FILE", None)
    os.environ.pop("CSP_BOOTSTRAP_TOKEN", None)
    if router_main.ROUTER_STATE_FILE.exists():
        router_main.ROUTER_STATE_FILE.unlink()
    router_main._initialise_token_source()
    router_server.reset_router_model_cache()


@pytest.fixture(autouse=True)
def _restore_token_resolution():
    _reset_token_resolution()
    yield
    _reset_token_resolution()


def _point_at(monkeypatch, path) -> None:
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))


def test_file_beats_state_bootstrap_and_legacy(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="anila-router")
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("csk-from-file\n", encoding="utf-8")
    router_main._write_state_file(
        "csk-from-state",
        source_meta={"note": "test"},
    )
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")
    _point_at(monkeypatch, token_file)

    router_main._initialise_token_source()

    assert router_main._token_source == "file"
    assert router_main._service_token == "csk-from-file"
    assert settings.csp_service_token == "csk-from-file"
    # Seeding the state file from bootstrap must not run when the file won,
    # and must not replace the state file's existing token.
    stored = json.loads(router_main.ROUTER_STATE_FILE.read_text(encoding="utf-8"))
    assert stored["token"] == "csk-from-state"
    assert "csk-from-file" not in caplog.text
    assert "csk-from-state" not in caplog.text
    assert "csk-from-bootstrap" not in caplog.text
    assert "legacy-env-token" not in caplog.text


def test_fallback_order_is_state_then_bootstrap_then_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("ANILA_SERVICE_TOKEN_FILE", raising=False)
    router_main._write_state_file("csk-from-state", source_meta={"note": "test"})
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")

    token, source = router_main._load_service_token()
    assert (token, source) == ("csk-from-state", "state_file")

    router_main.ROUTER_STATE_FILE.unlink()
    token, source = router_main._load_service_token()
    assert (token, source) == ("csk-from-bootstrap", "bootstrap")

    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "")
    token, source = router_main._load_service_token()
    assert (token, source) == ("legacy-env-token", "legacy_env")


def test_bootstrap_fallback_seeds_state_file_without_logging_token(monkeypatch, caplog):
    caplog.set_level("INFO", logger="anila-router")
    monkeypatch.delenv("ANILA_SERVICE_TOKEN_FILE", raising=False)
    if router_main.ROUTER_STATE_FILE.exists():
        router_main.ROUTER_STATE_FILE.unlink()
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "")

    router_main._initialise_token_source()

    assert router_main._token_source == "bootstrap"
    assert router_main.ROUTER_STATE_FILE.is_file()
    stored = json.loads(router_main.ROUTER_STATE_FILE.read_text(encoding="utf-8"))
    assert stored["token"] == "csk-from-bootstrap"
    assert "csk-from-bootstrap" not in caplog.text


def test_configured_file_missing_fails_closed_without_logging_fallback_values(
    tmp_path, monkeypatch, caplog
):
    """A configured path that is not on disk is not a reason to use another secret."""
    caplog.set_level("INFO", logger="anila-router")
    missing = tmp_path / "router-primary.token"
    _point_at(monkeypatch, missing)
    router_main._write_state_file("csk-from-state", source_meta={"note": "test"})
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")

    router_main._initialise_token_source()

    assert router_main._token_source == "file_missing"
    assert router_main._service_token == ""
    assert settings.csp_service_token is None
    stored = json.loads(router_main.ROUTER_STATE_FILE.read_text(encoding="utf-8"))
    assert stored["token"] == "csk-from-state"
    assert "file_missing" in caplog.text
    assert "csk-from-state" not in caplog.text
    assert "csk-from-bootstrap" not in caplog.text
    assert "legacy-env-token" not in caplog.text

    router_main._reload_service_token(False)
    assert router_main._token_source == "file_missing"
    assert router_main._service_token == ""


def test_missing_configured_file_is_reread_when_csp_writes_it(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="anila-router")
    token_file = tmp_path / "router-primary.token"
    _point_at(monkeypatch, token_file)
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")
    router_main._initialise_token_source()
    assert router_main._token_source == "file_missing"
    assert router_main._service_token == ""

    token_file.write_text("csk-from-csp\n", encoding="utf-8")
    # A cached mtime must not freeze file_missing. The watcher re-reads
    # this source on its timer without a forced reload.
    router_main._file_mtime_ns = token_file.stat().st_mtime_ns
    router_main._reload_service_token(False)

    assert router_main._token_source == "file"
    assert router_main._service_token == "csk-from-csp"
    assert settings.csp_service_token == "csk-from-csp"
    assert "csk-from-csp" not in caplog.text
    assert "csk-from-bootstrap" not in caplog.text
    assert "legacy-env-token" not in caplog.text


@pytest.mark.asyncio
async def test_health_reports_file_missing(client, tmp_path, monkeypatch):
    missing = tmp_path / "router-primary.token"
    _point_at(monkeypatch, missing)
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")
    router_main._initialise_token_source()

    response = await client.get("/health")

    assert response.status_code == 200, response.text
    assert response.json()["token_source"] == "file_missing"
    assert "legacy-env-token" not in response.text


def test_empty_configured_file_does_not_fall_back(tmp_path, monkeypatch, caplog):
    caplog.set_level("ERROR", logger="anila-router")
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("   \n", encoding="utf-8")
    router_main._write_state_file("csk-from-state", source_meta={"note": "test"})
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")
    _point_at(monkeypatch, token_file)

    router_main._initialise_token_source()

    assert router_main._token_source == "file_error"
    assert router_main._service_token == ""
    assert settings.csp_service_token is None
    stored = json.loads(router_main.ROUTER_STATE_FILE.read_text(encoding="utf-8"))
    assert stored["token"] == "csk-from-state"
    assert "csk-from-state" not in caplog.text
    assert "csk-from-bootstrap" not in caplog.text
    assert "legacy-env-token" not in caplog.text
    assert "not using fallback" in caplog.text


def test_unreadable_configured_file_does_not_fall_back(tmp_path, monkeypatch, caplog):
    caplog.set_level("ERROR", logger="anila-router")
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("csk-unreadable-value\n", encoding="utf-8")
    token_file.chmod(0)
    router_main._write_state_file("csk-from-state", source_meta={"note": "test"})
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "csk-from-bootstrap")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "legacy-env-token")
    _point_at(monkeypatch, token_file)
    try:
        router_main._initialise_token_source()
        assert router_main._token_source == "file_error"
        assert router_main._service_token == ""
        assert settings.csp_service_token is None
        assert "csk-unreadable-value" not in caplog.text
        assert "csk-from-state" not in caplog.text
        assert "csk-from-bootstrap" not in caplog.text
        assert "legacy-env-token" not in caplog.text
    finally:
        token_file.chmod(0o600)


def test_file_error_reread_picks_up_repaired_file_without_mtime_change(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("\n", encoding="utf-8")
    _point_at(monkeypatch, token_file)
    router_main._initialise_token_source()
    assert router_main._token_source == "file_error"
    stamped = token_file.stat().st_mtime_ns

    token_file.write_text("csk-restored\n", encoding="utf-8")
    os.utime(token_file, ns=(stamped, stamped))
    router_main._reload_service_token(False)

    assert router_main._token_source == "file"
    assert router_main._service_token == "csk-restored"
    assert settings.csp_service_token == "csk-restored"


@pytest.mark.asyncio
async def test_health_reports_file_error(client, tmp_path, monkeypatch):
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("", encoding="utf-8")
    _point_at(monkeypatch, token_file)
    router_main._initialise_token_source()

    response = await client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_source"] == "file_error"
    assert "csk-" not in response.text


def test_reload_picks_up_rotated_file(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="anila-router")
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("csk-first\n", encoding="utf-8")
    _point_at(monkeypatch, token_file)
    router_main._initialise_token_source()
    assert router_main._service_token == "csk-first"

    token_file.write_text("csk-second\n", encoding="utf-8")
    future = time.time() + 10
    os.utime(token_file, (future, future))
    router_main._reload_service_token(False)

    assert router_main._token_source == "file"
    assert router_main._service_token == "csk-second"
    assert settings.csp_service_token == "csk-second"
    assert "csk-first" not in caplog.text
    assert "csk-second" not in caplog.text


@pytest.mark.asyncio
async def test_health_reports_token_source(client, tmp_path, monkeypatch):
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("csk-health-check\n", encoding="utf-8")
    _point_at(monkeypatch, token_file)
    router_main._initialise_token_source()

    response = await client.get("/health")

    assert response.status_code == 200, response.text
    assert response.json()["token_source"] == "file"
    assert "csk-health-check" not in response.text


@pytest.mark.asyncio
async def test_router_primary_rereads_file_once_after_rejection(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level("INFO")
    token_file = tmp_path / "router-primary.token"
    token_file.write_text("csk-old\n", encoding="utf-8")
    _point_at(monkeypatch, token_file)
    router_main._initialise_token_source()
    router_server.reset_router_model_cache()
    calls: list[str] = []

    class _Response:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers, timeout=5.0):
            token = headers["X-CSP-Service-Token"]
            calls.append(token)
            if token == "csk-old":
                token_file.write_text("csk-new\n", encoding="utf-8")
                return _Response(403)
            return _Response(200, {"name": "governed-model", "context_window": 4096})

    monkeypatch.setattr(router_server, "get_http_client", lambda: _Client())

    await router_server.refresh_router_model()

    assert calls == ["csk-old", "csk-new"]
    assert router_server.router_model_source() == "csp_registry"
    assert router_server.current_router_model() == "governed-model"
    assert router_main._service_token == "csk-new"
    assert "csk-old" not in caplog.text
    assert "csk-new" not in caplog.text

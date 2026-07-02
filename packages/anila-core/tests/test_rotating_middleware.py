"""Sprint 8 X / Phase B — RotatingServiceTokenMiddleware tests.

Covers:
* state file load on startup
* env-var fallback when state file absent
* dev mode (no token configured) lets requests through
* constant-time compare on hits
* hot reload after a single 403 (admin-rotation scenario)
* previous_token grace window
* corrupted state file falls back gracefully
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anila_core.api.middleware.auth import (
    CspServiceTokenMiddleware,
    RotatingServiceTokenMiddleware,
)


def _build_app(middleware_factory) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        type(middleware_factory),
        **{
            k: v
            for k, v in vars(middleware_factory).items()
            if not k.startswith("_") and k != "_init_kwargs"
        },
    )
    return app


def _build_app_with(mw_class, **kwargs) -> FastAPI:
    """Helper to wire a middleware class with kwargs into a tiny FastAPI app."""
    app = FastAPI()
    app.add_middleware(mw_class, **kwargs)

    @app.get("/foo")
    def foo():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Constant-time compare on the legacy (static) middleware.
# ---------------------------------------------------------------------------


def test_static_middleware_constant_time_compare_match():
    app = _build_app_with(CspServiceTokenMiddleware, service_token="hunter2")
    client = TestClient(app)
    r = client.get("/foo", headers={"X-CSP-Service-Token": "hunter2"})
    assert r.status_code == 200


def test_static_middleware_constant_time_compare_mismatch():
    app = _build_app_with(CspServiceTokenMiddleware, service_token="hunter2")
    client = TestClient(app)
    r = client.get("/foo", headers={"X-CSP-Service-Token": "wrong"})
    assert r.status_code == 403


def test_static_middleware_missing_header_401():
    app = _build_app_with(CspServiceTokenMiddleware, service_token="hunter2")
    client = TestClient(app)
    r = client.get("/foo")
    assert r.status_code == 401


def test_static_middleware_dev_mode_passes_through():
    app = _build_app_with(
        CspServiceTokenMiddleware, service_token="hunter2", dev_mode=True
    )
    client = TestClient(app)
    r = client.get("/foo")
    assert r.status_code == 200


def test_static_middleware_no_token_passes_through():
    """Local dev: empty service_token = "skip auth"."""
    app = _build_app_with(CspServiceTokenMiddleware, service_token="")
    client = TestClient(app)
    r = client.get("/foo")
    assert r.status_code == 200


def test_static_middleware_health_path_bypass():
    app = _build_app_with(CspServiceTokenMiddleware, service_token="hunter2")
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# RotatingServiceTokenMiddleware.
# ---------------------------------------------------------------------------


def _write_state(state_dir: Path, token: str, **extra) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "agent_id": 1, "schema_version": 1, **extra}
    p = state_dir / "service_token.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_rotating_loads_token_from_state_file(tmp_path):
    _write_state(tmp_path, "csk-from-disk")
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)

    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-from-disk"})
    assert r.status_code == 200

    r = client.get("/foo", headers={"X-CSP-Service-Token": "wrong"})
    assert r.status_code == 403


def test_rotating_falls_back_to_env_token_when_no_state(tmp_path):
    # tmp_path empty → no state file
    app = _build_app_with(
        RotatingServiceTokenMiddleware,
        state_dir=tmp_path,
        env_token="legacy-fleet-shared",
    )
    client = TestClient(app)
    r = client.get("/foo", headers={"X-CSP-Service-Token": "legacy-fleet-shared"})
    assert r.status_code == 200


def test_rotating_no_source_passes_through(tmp_path):
    """No state file + no env: local dev mode."""
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)
    r = client.get("/foo")  # no header
    assert r.status_code == 200


def test_rotating_dev_mode_passes_through(tmp_path):
    _write_state(tmp_path, "csk-from-disk")
    app = _build_app_with(
        RotatingServiceTokenMiddleware,
        state_dir=tmp_path,
        env_token="",
        dev_mode=True,
    )
    client = TestClient(app)
    r = client.get("/foo")
    assert r.status_code == 200


def test_rotating_hot_reload_after_admin_rotation(tmp_path):
    """Admin rotates → CLI rewrites state file → middleware reloads on first miss."""
    _write_state(tmp_path, "csk-old")
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)

    # Old token works.
    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-old"})
    assert r.status_code == 200

    # Admin rotates: rewrite state file with a new token.
    _write_state(tmp_path, "csk-new")

    # First request with new token: middleware has stale in-memory
    # copy, sees "wrong", reloads, retry succeeds.
    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-new"})
    assert r.status_code == 200

    # Old token rejected after reload (state file no longer has it).
    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-old"})
    assert r.status_code == 403


def test_rotating_previous_token_grace(tmp_path):
    """Explicit previous_token in state file is accepted alongside current."""
    _write_state(
        tmp_path,
        "csk-current",
        previous_token="csk-previous",
        previous_expires_at="2099-01-01T00:00:00Z",
    )
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)

    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-current"})
    assert r.status_code == 200
    r = client.get("/foo", headers={"X-CSP-Service-Token": "csk-previous"})
    assert r.status_code == 200
    r = client.get("/foo", headers={"X-CSP-Service-Token": "anything-else"})
    assert r.status_code == 403


def test_rotating_corrupt_state_falls_back_to_env(tmp_path):
    (tmp_path / "service_token.json").write_text("{not json", encoding="utf-8")
    app = _build_app_with(
        RotatingServiceTokenMiddleware,
        state_dir=tmp_path,
        env_token="legacy-token",
    )
    client = TestClient(app)
    r = client.get("/foo", headers={"X-CSP-Service-Token": "legacy-token"})
    assert r.status_code == 200


def test_rotating_missing_header_401(tmp_path):
    _write_state(tmp_path, "csk-x")
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)
    r = client.get("/foo")
    assert r.status_code == 401


def test_rotating_health_path_bypass(tmp_path):
    _write_state(tmp_path, "csk-x")
    app = _build_app_with(
        RotatingServiceTokenMiddleware, state_dir=tmp_path, env_token=""
    )
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200


def test_rotating_mode_property(tmp_path):
    """The .mode attribute lets /health expose token source for ops."""
    # state_file mode
    _write_state(tmp_path, "csk-x")
    mw = RotatingServiceTokenMiddleware(
        app=lambda *a, **kw: None, state_dir=tmp_path, env_token=""
    )
    assert mw.mode == "state_file"

    # env_legacy mode
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    mw = RotatingServiceTokenMiddleware(
        app=lambda *a, **kw: None, state_dir=empty_dir, env_token="legacy"
    )
    assert mw.mode == "env_legacy"

    # none mode
    mw = RotatingServiceTokenMiddleware(
        app=lambda *a, **kw: None, state_dir=empty_dir, env_token=""
    )
    assert mw.mode == "none"

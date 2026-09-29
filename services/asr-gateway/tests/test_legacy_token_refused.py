"""共用 CSP_SERVICE_TOKEN / CSP_BOOTSTRAP_TOKEN 非空時拒絕啟動。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_startup_refuses_nonempty_legacy_token(monkeypatch):
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "still-in-dotenv")
    with pytest.raises(RuntimeError, match="刪除"):
        with TestClient(create_app(skip_upstreams=True)):
            pass

    monkeypatch.delenv("CSP_SERVICE_TOKEN")
    monkeypatch.setenv("CSP_BOOTSTRAP_TOKEN", "still-in-dotenv")
    with pytest.raises(RuntimeError, match="CSP_BOOTSTRAP_TOKEN"):
        with TestClient(create_app(skip_upstreams=True)):
            pass

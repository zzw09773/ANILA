"""舊的共用 CSP_SERVICE_TOKEN 不得經由設定載入。"""
from __future__ import annotations

from anila_core.config import Settings


def test_env_and_dotenv_legacy_token_are_ignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CSP_SERVICE_TOKEN=from-dotenv\n", encoding="utf-8")
    assert Settings().csp_service_token is None
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "from-env")
    assert Settings().csp_service_token is None


def test_runtime_assignment_still_works():
    settings = Settings()
    settings.csp_service_token = "file-token"
    assert settings.csp_service_token == "file-token"

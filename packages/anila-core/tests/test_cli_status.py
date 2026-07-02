"""Tests for `anila-core status` CLI command."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anila_core.cli import status_cmd


class TestStatusHelpers:
    def test_load_manifest_name(self, tmp_path: Path):
        manifest = tmp_path / "anila.yaml"
        manifest.write_text("name: hr-agent\n", encoding="utf-8")
        assert status_cmd._load_manifest_name(str(manifest)) == "hr-agent"

    def test_load_manifest_name_missing_file(self, tmp_path: Path):
        assert status_cmd._load_manifest_name(str(tmp_path / "missing.yaml")) == ""


class TestStatusLookup:
    def test_list_agents_success(self, monkeypatch: pytest.MonkeyPatch):
        def fake_get(url, headers, timeout):
            assert url == "http://csp/api/agents"
            assert headers["Authorization"] == "Bearer jwt-token"
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "hr-agent", "approval_status": "pending"}],
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", fake_get)
        agents = status_cmd._list_agents("http://csp", "jwt-token")
        assert agents[0]["name"] == "hr-agent"

    def test_get_agent_success(self, monkeypatch: pytest.MonkeyPatch):
        def fake_get(url, headers, timeout):
            assert url == "http://csp/api/agents/7"
            return httpx.Response(
                200,
                json={"id": 7, "name": "finance-agent", "approval_status": "approved"},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(httpx, "get", fake_get)
        agent = status_cmd._get_agent("http://csp", "jwt-token", 7)
        assert agent["id"] == 7

    def test_list_agents_http_error_exits(self, monkeypatch: pytest.MonkeyPatch, capsys):
        def fake_get(url, headers, timeout):
            req = httpx.Request("GET", url)
            return httpx.Response(403, json={"detail": "forbidden"}, request=req)

        monkeypatch.setattr(httpx, "get", fake_get)

        with pytest.raises(SystemExit) as exc:
            status_cmd._list_agents("http://csp", "jwt-token")

        assert exc.value.code == 1
        assert "forbidden" in capsys.readouterr().err

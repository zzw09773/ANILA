"""Tests for `anila-core register` CLI helpers."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from anila_core.cli import register_cmd


class TestRegisterManifest:
    def test_load_manifest_success(self, tmp_path: Path):
        manifest = tmp_path / "anila.yaml"
        manifest.write_text(
            "\n".join(
                [
                    "name: hr-agent",
                    "description_for_router: Handles HR policy questions",
                    "endpoint_url: http://agent:9100",
                ]
            ),
            encoding="utf-8",
        )
        data = register_cmd._load_manifest(str(manifest))
        assert data["name"] == "hr-agent"

    def test_load_manifest_requires_name(self, tmp_path: Path):
        manifest = tmp_path / "anila.yaml"
        manifest.write_text("description_for_router: desc\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            register_cmd._load_manifest(str(manifest))
        assert exc.value.code == 1


class TestRegisterHTTP:
    def test_login_success(self, monkeypatch: pytest.MonkeyPatch):
        def fake_post(url, json, timeout):
            assert url == "http://csp/api/auth/login"
            return httpx.Response(
                200,
                json={"access_token": "jwt-token"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        token = register_cmd._login("http://csp", "dev", "password")
        assert token == "jwt-token"

    def test_register_success(self, monkeypatch: pytest.MonkeyPatch):
        def fake_post(url, json, headers, timeout):
            assert url == "http://csp/api/agents/register"
            assert headers["Authorization"] == "Bearer jwt-token"
            assert json["name"] == "hr-agent"
            return httpx.Response(
                200,
                json={"id": 7, "name": "hr-agent", "approval_status": "pending"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        result = register_cmd._register(
            "http://csp",
            "jwt-token",
            {
                "name": "hr-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Handles HR policy questions",
            },
        )
        assert result["id"] == 7

    def test_register_http_error_exits(self, monkeypatch: pytest.MonkeyPatch, capsys):
        def fake_post(url, json, headers, timeout):
            return httpx.Response(
                400,
                json={"detail": "duplicate name"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)

        with pytest.raises(SystemExit) as exc:
            register_cmd._register(
                "http://csp",
                "jwt-token",
                {
                    "name": "hr-agent",
                    "endpoint_url": "http://agent:9100",
                    "description_for_router": "desc",
                },
            )

        assert exc.value.code == 1
        assert "duplicate name" in capsys.readouterr().err

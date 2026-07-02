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


class TestRegisterFlags:
    """Slice 5c — runtime_type / classification_ceiling / version / draft."""

    def test_runtime_type_values_are_the_five_doc05_values(self):
        assert register_cmd._RUNTIME_TYPES == (
            "anila_agent",
            "langchain",
            "openwebui_pipe_compatible",
            "openai_compatible_agent",
            "custom_http",
        )

    def test_classification_ceilings_are_the_five_zh_tw_levels(self):
        assert register_cmd._CLASSIFICATION_CEILINGS == (
            "無機密",
            "營業秘密",
            "機密",
            "極機密",
            "絕對機密",
        )

    def test_validate_choice_accepts_valid(self):
        assert register_cmd._validate_choice(
            "custom_http", register_cmd._RUNTIME_TYPES, "--runtime-type"
        ) == "custom_http"
        assert register_cmd._validate_choice(
            "機密", register_cmd._CLASSIFICATION_CEILINGS, "--classification-ceiling"
        ) == "機密"

    def test_validate_choice_rejects_invalid_runtime_type(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd._validate_choice(
                "django", register_cmd._RUNTIME_TYPES, "--runtime-type"
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--runtime-type" in err
        assert "custom_http" in err

    def test_validate_choice_rejects_invalid_ceiling(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd._validate_choice(
                "top-secret", register_cmd._CLASSIFICATION_CEILINGS,
                "--classification-ceiling",
            )
        assert exc.value.code == 1
        assert "絕對機密" in capsys.readouterr().err

    def test_register_payload_passthrough(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 3, "name": "risk-agent", "approval_status": "draft"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        register_cmd._register(
            "http://csp",
            "jwt-token",
            {
                "name": "risk-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "desc",
                "runtime_type": "custom_http",
                "classification_ceiling": "機密",
                "version": "1.0.0",
                "draft": True,
            },
        )
        assert captured["runtime_type"] == "custom_http"
        assert captured["classification_ceiling"] == "機密"
        assert captured["version"] == "1.0.0"
        assert captured["draft"] is True

    def test_register_omits_absent_metadata(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 4, "name": "plain", "approval_status": "pending"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        register_cmd._register(
            "http://csp",
            "jwt-token",
            {
                "name": "plain",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "desc",
            },
        )
        for key in ("runtime_type", "classification_ceiling", "version", "draft"):
            assert key not in captured

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
    """runtime_type / default_classification_level / version / base model."""

    def test_runtime_type_values_are_the_five_doc05_values(self):
        assert register_cmd._RUNTIME_TYPES == (
            "anila_agent",
            "langchain",
            "openwebui_pipe_compatible",
            "openai_compatible_agent",
            "custom_http",
        )

    def test_classification_levels_are_the_four_zh_tw_levels(self):
        assert register_cmd._CLASSIFICATION_LEVELS == (
            "無機密",
            "營業秘密",
            "密",
            "機密",
        )

    def test_validate_choice_accepts_valid(self):
        assert register_cmd._validate_choice(
            "custom_http", register_cmd._RUNTIME_TYPES, "--runtime-type"
        ) == "custom_http"
        assert register_cmd._validate_choice(
            "密", register_cmd._CLASSIFICATION_LEVELS, "--classification-level"
        ) == "密"

    def test_validate_choice_rejects_invalid_runtime_type(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd._validate_choice(
                "django", register_cmd._RUNTIME_TYPES, "--runtime-type"
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--runtime-type" in err
        assert "custom_http" in err

    def test_validate_choice_rejects_invalid_level(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd._validate_choice(
                "極機密", register_cmd._CLASSIFICATION_LEVELS,
                "--classification-level",
            )
        assert exc.value.code == 1
        assert "極機密" in capsys.readouterr().err

    def test_register_payload_passthrough(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 3, "name": "risk-agent", "approval_status": "registered"},
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
                "default_classification_level": "機密",
                "version": "1.0.0",
            },
        )
        assert captured["runtime_type"] == "custom_http"
        assert captured["default_classification_level"] == "機密"
        assert captured["version"] == "1.0.0"

    def test_register_omits_absent_metadata(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 4, "name": "plain", "approval_status": "registered"},
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
        for key in (
            "runtime_type",
            "default_classification_level",
            "version",
            "base_model_name",
            "base_model_id",
        ):
            assert key not in captured


class TestBaseModelPayload:
    """The endpoint takes a model NAME or an id — the CLI must send one."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, manifest: dict) -> dict:
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 9, "name": manifest["name"],
                      "approval_status": "registered"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        register_cmd._register("http://csp", "jwt-token", manifest)
        return captured

    def test_base_model_is_sent_as_base_model_name(self, monkeypatch):
        captured = self._capture(monkeypatch, {
            "name": "name-agent",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "desc",
            "base_model": "gemma-x",
        })
        assert captured["base_model_name"] == "gemma-x"
        assert "base_model_id" not in captured

    def test_base_model_id_is_sent_when_supplied(self, monkeypatch):
        captured = self._capture(monkeypatch, {
            "name": "id-agent",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "desc",
            "base_model_id": 7,
        })
        assert captured["base_model_id"] == 7
        assert "base_model_name" not in captured


class TestDraftFlagRemoved:
    """`--draft` claimed a shadow registration the platform does not have.

    ``approval_status`` is a three-state machine (registered / approved /
    disabled) — no draft state exists, and the payload key was silently
    dropped by the server. A switch that lies is worse than no switch.
    """

    def test_draft_is_not_an_accepted_argument(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd.run(["--draft"])
        # argparse's "unrecognized arguments" exit code.
        assert exc.value.code == 2
        assert "--draft" in capsys.readouterr().err

    def test_draft_is_absent_from_the_help_text(self, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd.run(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--draft" not in out
        assert "shadow" not in out.lower()
        # The flag that replaced the dead ceiling switch is documented.
        assert "--classification-level" in out

    def test_register_never_sends_a_draft_key(self, monkeypatch):
        captured: dict = {}

        def fake_post(url, json, headers, timeout):
            captured.update(json)
            return httpx.Response(
                200,
                json={"id": 5, "name": "d", "approval_status": "registered"},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        register_cmd._register("http://csp", "jwt-token", {
            "name": "d",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "desc",
            "draft": True,          # a stale manifest key must not leak
            "shadow": True,
        })
        assert "draft" not in captured
        assert "shadow" not in captured


class TestManifestBaseModelGuard:
    """Both failures are caught locally, before any prompt or network call."""

    @staticmethod
    def _manifest(tmp_path, extra: str = "") -> str:
        p = tmp_path / "anila.yaml"
        p.write_text(
            "name: guard-agent\n"
            "description_for_router: guard\n"
            "endpoint_url: http://agent:9100\n" + extra,
            encoding="utf-8",
        )
        return str(p)

    def test_missing_base_model_exits_with_instructions(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            register_cmd.run(["--manifest", self._manifest(tmp_path)])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "base_model" in err
        assert "治理中心" in err

    def test_legacy_classification_ceiling_key_says_rename_it(self, tmp_path, capsys):
        manifest = self._manifest(
            tmp_path, extra='base_model: "m"\nclassification_ceiling: "機密"\n'
        )
        with pytest.raises(SystemExit) as exc:
            register_cmd.run(["--manifest", manifest])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "classification_ceiling" in err
        assert "default_classification_level" in err


class TestErrorDetailRendering:
    def test_422_validation_list_is_flattened_to_readable_text(self):
        resp = httpx.Response(
            422,
            json={"detail": [
                {"type": "missing", "loc": ["body", "base_model_id"],
                 "msg": "Field required"},
            ]},
            request=httpx.Request("POST", "http://csp/api/agents/register"),
        )
        rendered = register_cmd._extract_detail(resp)
        assert rendered == "base_model_id: Field required"

    def test_string_detail_passes_through(self):
        resp = httpx.Response(
            400,
            json={"detail": "底層模型「x」不存在"},
            request=httpx.Request("POST", "http://csp/api/agents/register"),
        )
        assert register_cmd._extract_detail(resp) == "底層模型「x」不存在"

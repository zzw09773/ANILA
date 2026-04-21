"""Tests for `anila-core init` CLI command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from anila_core.cli.init_cmd import _render, _slugify, _scaffold


class TestSlugify:
    def test_basic(self):
        assert _slugify("HR Policy Agent") == "hr-policy-agent"

    def test_special_chars(self):
        assert _slugify("my__agent!!") == "my--agent"

    def test_already_slug(self):
        assert _slugify("my-agent") == "my-agent"


class TestRender:
    def test_replaces_placeholder(self):
        assert _render("Hello {{NAME}}", {"NAME": "world"}) == "Hello world"

    def test_unknown_placeholder_preserved(self):
        assert _render("{{UNKNOWN}}", {}) == "{{UNKNOWN}}"

    def test_multiple_replacements(self):
        result = _render("{{A}} + {{B}}", {"A": "foo", "B": "bar"})
        assert result == "foo + bar"


class TestScaffold:
    def test_creates_expected_files(self, tmp_path):
        output = tmp_path / "my-agent"
        variables = {
            "AGENT_NAME": "my-agent",
            "AGENT_DISPLAY_NAME": "My Agent",
            "AGENT_DESCRIPTION": "Test agent description",
            "ENDPOINT_URL": "http://localhost:9100",
        }
        _scaffold(output, variables)

        assert (output / "agent.py").exists()
        assert (output / "anila.yaml").exists()
        assert (output / ".env.example").exists()
        assert (output / "Dockerfile").exists()
        assert (output / "requirements.txt").exists()
        assert (output / "README.md").exists()

    def test_placeholders_replaced_in_agent_py(self, tmp_path):
        output = tmp_path / "hr-agent"
        variables = {
            "AGENT_NAME": "hr-agent",
            "AGENT_DISPLAY_NAME": "Hr Agent",
            "AGENT_DESCRIPTION": "HR queries",
            "ENDPOINT_URL": "http://localhost:9101",
        }
        _scaffold(output, variables)

        content = (output / "agent.py").read_text()
        assert "hr-agent" in content
        assert "{{AGENT_NAME}}" not in content

    def test_anila_yaml_has_correct_name(self, tmp_path):
        output = tmp_path / "finance-agent"
        variables = {
            "AGENT_NAME": "finance-agent",
            "AGENT_DISPLAY_NAME": "Finance Agent",
            "AGENT_DESCRIPTION": "Finance reports",
            "ENDPOINT_URL": "http://localhost:9102",
        }
        _scaffold(output, variables)

        import yaml
        manifest = yaml.safe_load((output / "anila.yaml").read_text())
        assert manifest["name"] == "finance-agent"
        assert manifest["description_for_router"] == "Finance reports"
        assert manifest["endpoint_url"] == "http://localhost:9102"

    def test_fails_if_dir_exists(self, tmp_path):
        output = tmp_path / "existing-agent"
        output.mkdir()
        variables = {
            "AGENT_NAME": "existing-agent",
            "AGENT_DISPLAY_NAME": "Existing Agent",
            "AGENT_DESCRIPTION": "desc",
            "ENDPOINT_URL": "http://localhost:9100",
        }
        with pytest.raises(FileExistsError):
            _scaffold(output, variables)

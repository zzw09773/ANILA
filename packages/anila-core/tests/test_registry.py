"""Tests for AgentRegistry — loading, dedup, validation, per-agent model."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from anila_core.models.agent import AgentDefinition
from anila_core.registry.agent_registry import AgentRegistry, RegistryError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_yaml_file(tmp_path: Path, filename: str, data: dict) -> Path:
    path = tmp_path / filename
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def write_md_file(tmp_path: Path, filename: str, frontmatter: dict, body: str = "") -> Path:
    path = tmp_path / filename
    fm_text = yaml.dump(frontmatter).strip()
    content = f"---\n{fm_text}\n---\n\n{body}"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Basic registration
# ---------------------------------------------------------------------------

class TestRegisterAndGet:
    def test_register_and_get(self) -> None:
        registry = AgentRegistry()
        defn = AgentDefinition(agent_type="coder", description="Writes code")
        registry.register(defn)
        got = registry.get("coder")
        assert got.agent_type == "coder"
        assert got.description == "Writes code"

    def test_get_missing_raises(self) -> None:
        registry = AgentRegistry()
        with pytest.raises(RegistryError, match="Unknown agent_type"):
            registry.get("nonexistent")

    def test_get_or_none_missing(self) -> None:
        registry = AgentRegistry()
        assert registry.get_or_none("missing") is None

    def test_len(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="a"))
        registry.register(AgentDefinition(agent_type="b"))
        assert len(registry) == 2

    def test_contains(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="x"))
        assert "x" in registry
        assert "y" not in registry


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

class TestLoadFromFiles:
    def test_load_yaml_file(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "coder.yaml", {
            "agent_type": "coder",
            "description": "Writes Python code",
            "tools": ["bash", "file_write"],
            "max_turns": 15,
        })
        registry = AgentRegistry()
        registry.load_directory(tmp_path)
        defn = registry.get("coder")
        assert defn.description == "Writes Python code"
        assert "bash" in defn.tools
        assert defn.max_turns == 15

    def test_load_md_file_with_frontmatter(self, tmp_path: Path) -> None:
        write_md_file(
            tmp_path,
            "reviewer.md",
            frontmatter={"agent_type": "reviewer", "description": "Reviews code"},
            body="You are a code reviewer.",
        )
        registry = AgentRegistry()
        registry.load_directory(tmp_path)
        defn = registry.get("reviewer")
        assert defn.agent_type == "reviewer"
        # Body becomes system_prompt when not set in frontmatter
        assert "code reviewer" in defn.system_prompt

    def test_later_file_overrides_earlier(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "a_agent.yaml", {
            "agent_type": "coder",
            "description": "First definition",
        })
        write_yaml_file(tmp_path, "z_agent.yaml", {
            "agent_type": "coder",
            "description": "Second definition",
        })
        registry = AgentRegistry()
        registry.load_directory(tmp_path)
        assert registry.get("coder").description == "Second definition"

    def test_nonexistent_directory_raises(self) -> None:
        registry = AgentRegistry()
        with pytest.raises(RegistryError, match="Not a directory"):
            registry.load_directory("/nonexistent/path/xyz")


# ---------------------------------------------------------------------------
# Dedup / override
# ---------------------------------------------------------------------------

class TestDedup:
    def test_register_overrides_same_type(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="bot", description="v1"))
        registry.register(AgentDefinition(agent_type="bot", description="v2"))
        assert registry.get("bot").description == "v2"

    def test_list_types_no_duplicates(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="a"))
        registry.register(AgentDefinition(agent_type="a"))
        registry.register(AgentDefinition(agent_type="b"))
        assert registry.list_types() == ["a", "b"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_unknown_fields_raise(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "bad.yaml", {
            "agent_type": "bad",
            "nonexistent_field": "value",
        })
        registry = AgentRegistry()
        with pytest.raises(RegistryError, match="Unknown fields"):
            registry.load_directory(tmp_path)

    def test_missing_agent_type_raises(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "bad.yaml", {"description": "no type"})
        registry = AgentRegistry()
        with pytest.raises(RegistryError, match="missing required field: agent_type"):
            registry.load_directory(tmp_path)

    def test_invalid_permission_mode_raises(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "bad.yaml", {
            "agent_type": "x",
            "permission_mode": "super_admin",
        })
        registry = AgentRegistry()
        with pytest.raises(RegistryError, match="Invalid permission_mode"):
            registry.load_directory(tmp_path)

    def test_validate_tools_warns_unknown(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="a", tools=["bash", "unknown_tool"]))
        warnings = registry.validate_tools(known_tools={"bash", "file_read"})
        assert any("unknown_tool" in w for w in warnings)

    def test_validate_tools_wildcard_ok(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="a", tools=["*"]))
        warnings = registry.validate_tools(known_tools={"bash"})
        assert warnings == []


# ---------------------------------------------------------------------------
# Per-agent model
# ---------------------------------------------------------------------------

class TestPerAgentModel:
    def test_per_agent_model_field(self, tmp_path: Path) -> None:
        write_yaml_file(tmp_path, "agent.yaml", {
            "agent_type": "fast_agent",
            "model": "gpt-4o-mini",
        })
        registry = AgentRegistry()
        registry.load_directory(tmp_path)
        defn = registry.get("fast_agent")
        assert defn.model == "gpt-4o-mini"

    def test_default_model_is_none(self) -> None:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="default_agent"))
        assert registry.get("default_agent").model is None

    def test_allows_tool_with_list(self) -> None:
        defn = AgentDefinition(agent_type="x", tools=["bash", "file_read"])
        assert defn.allows_tool("bash")
        assert defn.allows_tool("file_read")
        assert not defn.allows_tool("file_write")

    def test_allows_tool_wildcard(self) -> None:
        defn = AgentDefinition(agent_type="x", tools=["*"])
        assert defn.allows_tool("anything")

    def test_allows_tool_empty_list_allows_all(self) -> None:
        defn = AgentDefinition(agent_type="x", tools=[])
        assert defn.allows_tool("anything")

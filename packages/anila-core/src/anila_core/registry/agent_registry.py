"""Agent Registry — loads AgentDefinition records from YAML or Markdown.

Loading rules:
  1. Built-in agents are registered first.
  2. Custom agents from a directory are merged: later files override earlier
     definitions for the same agent_type.
  3. Validation runs after all files are loaded.

Supported file formats:
  - .yaml / .yml: pure YAML with agent definition fields
  - .md: Markdown with YAML frontmatter block at the top
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from ..models.agent import AgentDefinition, PermissionMode


class RegistryError(Exception):
    """Raised for invalid agent definitions or registry state."""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a Markdown document.

    Returns (frontmatter_dict, body_text).
    Raises RegistryError if frontmatter block is malformed.
    """
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, text

    # Find closing ---
    rest = stripped[3:]
    end = rest.find("\n---")
    if end == -1:
        raise RegistryError("Unclosed YAML frontmatter block (missing closing ---)")

    yaml_block = rest[:end].strip()
    body = rest[end + 4:].strip()

    try:
        data = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"Invalid YAML frontmatter: {exc}") from exc

    return data, body


def _load_from_dict(data: dict, source_path: Optional[str] = None) -> AgentDefinition:
    """Build an AgentDefinition from a raw dict, validating required fields."""
    allowed_fields = {
        "agent_type",
        "description",
        "when_to_use",
        "tools",
        "model",
        "max_turns",
        "system_prompt",
        "permission_mode",
    }
    unknown = set(data.keys()) - allowed_fields
    if unknown:
        raise RegistryError(
            f"Unknown fields in agent definition: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed_fields)}"
        )

    if "agent_type" not in data:
        raise RegistryError("Agent definition missing required field: agent_type")

    # Normalize permission_mode
    if "permission_mode" in data:
        try:
            data["permission_mode"] = PermissionMode(data["permission_mode"])
        except ValueError:
            valid = [m.value for m in PermissionMode]
            raise RegistryError(
                f"Invalid permission_mode '{data['permission_mode']}'. Valid: {valid}"
            )

    return AgentDefinition(**data, source_path=source_path)


def _load_file(path: Path) -> AgentDefinition:
    """Load a single agent definition file."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise RegistryError(f"Invalid YAML in {path}: {exc}") from exc
    elif suffix == ".md":
        data, body = _parse_frontmatter(text)
        # Body of the .md file becomes the system_prompt if not set in frontmatter
        if body and "system_prompt" not in data:
            data["system_prompt"] = body
    else:
        raise RegistryError(f"Unsupported file type: {path.suffix}")

    return _load_from_dict(data, source_path=str(path))


class AgentRegistry:
    """Registry that holds all known AgentDefinition records.

    Usage:
        registry = AgentRegistry()
        registry.register(AgentDefinition(agent_type="coder", ...))
        registry.load_directory("/path/to/agents/")
        agent = registry.get("coder")
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        """Register an agent definition, overwriting any prior definition."""
        self._agents[definition.agent_type] = definition

    def load_directory(self, directory: str | Path) -> list[str]:
        """Load all agent definitions from .yaml, .yml, and .md files.

        Files are processed in alphabetical order. Later files override
        earlier definitions for the same agent_type.

        Returns the list of agent_type strings that were loaded.
        """
        dir_path = Path(directory)
        if not dir_path.is_dir():
            raise RegistryError(f"Not a directory: {directory}")

        loaded: list[str] = []
        errors: list[str] = []

        for file_path in sorted(dir_path.glob("**/*")):
            if file_path.suffix.lower() not in {".yaml", ".yml", ".md"}:
                continue
            if not file_path.is_file():
                continue
            try:
                definition = _load_file(file_path)
                self.register(definition)
                loaded.append(definition.agent_type)
            except RegistryError as exc:
                errors.append(f"{file_path}: {exc}")

        if errors:
            raise RegistryError(
                f"Errors loading agent directory {directory}:\n"
                + "\n".join(errors)
            )

        return loaded

    def get(self, agent_type: str) -> AgentDefinition:
        """Return the definition for the given agent_type.

        Raises RegistryError if not found.
        """
        if agent_type not in self._agents:
            available = sorted(self._agents.keys())
            raise RegistryError(
                f"Unknown agent_type '{agent_type}'. Available: {available}"
            )
        return self._agents[agent_type]

    def get_or_none(self, agent_type: str) -> Optional[AgentDefinition]:
        """Return the definition or None if not found."""
        return self._agents.get(agent_type)

    def list_types(self) -> list[str]:
        """Return all registered agent_type strings."""
        return sorted(self._agents.keys())

    def validate_tools(self, known_tools: set[str]) -> list[str]:
        """Check that all tool references in definitions exist in known_tools.

        Returns a list of warning strings for unknown tool references.
        Wildcard "*" is always valid.
        """
        warnings: list[str] = []
        for agent_type, defn in self._agents.items():
            for tool in defn.tools:
                if tool == "*":
                    continue
                if tool not in known_tools:
                    warnings.append(
                        f"Agent '{agent_type}' references unknown tool '{tool}'"
                    )
        return warnings

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_type: str) -> bool:
        return agent_type in self._agents

"""Config loading. YAML-first, env-var overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    """Walk up from this file to find the project root (the dir holding pyproject.toml)."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return here.parent


def anila_home() -> Path:
    """State directory for sessions/memory. Defaults to <project>/.anila."""
    override = os.environ.get("ANILA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return project_root() / ".anila"


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = project_root() / p
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Top-level YAML in {p} must be a mapping, got {type(data).__name__}")
    return data


def read_text(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = project_root() / p
    return p.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ModelConfig:
    model: str
    base_url: str | None
    api_key: str | None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryConfig:
    short_term_enabled: bool
    short_term_path: Path
    long_term_enabled: bool
    long_term_path: Path
    max_index_lines: int
    max_index_bytes: int
    max_files: int
    auto_memory_enabled: bool
    auto_memory_min_messages: int
    auto_memory_model: str | None


@dataclass(frozen=True)
class AgentConfig:
    name: str
    instructions: str
    tool_use_behavior: str
    max_turns: int
    permission_mode: str


@dataclass(frozen=True)
class ToolsConfig:
    builtin: list[str]
    pre_tool_use: list[dict[str, Any]]
    post_tool_use: list[dict[str, Any]]
    stop: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]


@dataclass(frozen=True)
class AppConfig:
    agent: AgentConfig
    model: ModelConfig
    memory: MemoryConfig
    tools: ToolsConfig
    home: Path


def load_config(config_dir: str | Path = "configs") -> AppConfig:
    """Read the four YAML files and merge with environment."""
    load_dotenv()

    root = project_root()
    cfg_dir = Path(config_dir)
    if not cfg_dir.is_absolute():
        cfg_dir = root / cfg_dir

    agent_raw = load_yaml(cfg_dir / "agent.yaml")
    model_raw = load_yaml(cfg_dir / "model.yaml")
    memory_raw = load_yaml(cfg_dir / "memory.yaml")
    tools_raw = load_yaml(cfg_dir / "tools.yaml")

    instructions_path = agent_raw.get("instructions_file", "anila_agent/prompts/system.md")
    instructions = read_text(instructions_path)

    home = anila_home()

    api_key_env = model_raw.get("api_key_env", "ANILA_API_KEY")
    model = ModelConfig(
        model=os.environ.get("ANILA_MODEL") or model_raw.get("model", "gpt-4o-mini"),
        base_url=os.environ.get("ANILA_BASE_URL") or model_raw.get("base_url"),
        api_key=os.environ.get(api_key_env) or os.environ.get("ANILA_API_KEY"),
        settings=dict(model_raw.get("settings") or {}),
    )

    short_term = memory_raw.get("short_term") or {}
    long_term = memory_raw.get("long_term") or {}
    auto_mem = memory_raw.get("auto_memory") or {}
    auto_enabled_env = os.environ.get("ANILA_AUTO_MEMORY")
    auto_enabled = (
        auto_enabled_env == "1"
        if auto_enabled_env is not None
        else bool(auto_mem.get("enabled", False))
    )
    memory = MemoryConfig(
        short_term_enabled=bool(short_term.get("enabled", True)),
        short_term_path=home / short_term.get("path", "sessions/anila.db"),
        long_term_enabled=bool(long_term.get("enabled", True)),
        long_term_path=home / long_term.get("path", "memory/"),
        max_index_lines=int(long_term.get("max_index_lines", 200)),
        max_index_bytes=int(long_term.get("max_index_bytes", 25_000)),
        max_files=int(long_term.get("max_files", 200)),
        auto_memory_enabled=auto_enabled,
        auto_memory_min_messages=int(auto_mem.get("min_messages_between_runs", 4)),
        auto_memory_model=auto_mem.get("model"),
    )

    agent = AgentConfig(
        name=agent_raw.get("name", "anila"),
        instructions=instructions,
        tool_use_behavior=agent_raw.get("tool_use_behavior", "run_llm_again"),
        max_turns=int(agent_raw.get("max_turns", 20)),
        permission_mode=agent_raw.get("permission_mode", "default"),
    )

    tools = ToolsConfig(
        builtin=list(tools_raw.get("builtin") or []),
        pre_tool_use=list((tools_raw.get("hooks") or {}).get("pre_tool_use") or []),
        post_tool_use=list((tools_raw.get("hooks") or {}).get("post_tool_use") or []),
        stop=list((tools_raw.get("hooks") or {}).get("stop") or []),
        mcp_servers=list(tools_raw.get("mcp_servers") or []),
    )

    return AppConfig(agent=agent, model=model, memory=memory, tools=tools, home=home)

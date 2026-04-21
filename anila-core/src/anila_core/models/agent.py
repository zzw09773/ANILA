"""Agent definition and task state models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .message import Usage


class PermissionMode(str, Enum):
    """Controls what operations an agent is permitted to perform."""

    DEFAULT = "default"
    READ_ONLY = "read_only"
    UNRESTRICTED = "unrestricted"


class AgentDefinition(BaseModel):
    """Definition of an agent loaded from YAML or Markdown frontmatter.

    Agents are identified by agent_type and can be loaded from:
    - YAML files (*.yaml / *.yml)
    - Markdown files with YAML frontmatter (*.md)

    Per-agent model override allows routing different agents to different
    model sizes (e.g. main loop uses a large model, background agents use
    a smaller model).
    """

    agent_type: str
    description: str = ""
    when_to_use: str = ""
    tools: list[str] = Field(default_factory=list)
    model: Optional[str] = None  # per-agent model override
    max_turns: int = 10
    system_prompt: str = ""
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    source_path: Optional[str] = None  # path to the definition file

    def allows_tool(self, tool_name: str) -> bool:
        """Return True if this agent is allowed to use the given tool."""
        if not self.tools:
            return True  # empty list = allow all
        if "*" in self.tools:
            return True
        return tool_name in self.tools


class TaskState(BaseModel):
    """Runtime state of a spawned agent task."""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: str
    status: Literal["pending", "running", "completed", "failed", "stopped"] = "pending"
    result: Optional[str] = None
    usage: Optional[Usage] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def mark_running(self) -> "TaskState":
        """Return a new TaskState with status=running."""
        return self.model_copy(
            update={"status": "running", "updated_at": datetime.utcnow()}
        )

    def mark_completed(self, result: str, usage: Optional[Usage] = None) -> "TaskState":
        """Return a new TaskState with status=completed."""
        return self.model_copy(
            update={
                "status": "completed",
                "result": result,
                "usage": usage,
                "updated_at": datetime.utcnow(),
            }
        )

    def mark_failed(self, error: str) -> "TaskState":
        """Return a new TaskState with status=failed."""
        return self.model_copy(
            update={"status": "failed", "error": error, "updated_at": datetime.utcnow()}
        )

    def mark_stopped(self) -> "TaskState":
        """Return a new TaskState with status=stopped."""
        return self.model_copy(
            update={"status": "stopped", "updated_at": datetime.utcnow()}
        )

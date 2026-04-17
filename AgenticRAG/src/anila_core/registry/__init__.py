"""Agent registry — local definitions and remote CSP manifests."""

from .agent_registry import AgentRegistry, RegistryError
from .remote_agent_manifest import RemoteAgentManifest, RemoteAgentRegistry

__all__ = [
    "AgentRegistry",
    "RegistryError",
    "RemoteAgentManifest",
    "RemoteAgentRegistry",
]

"""Agent-as-tool wrapper — turn a remote agent into a callable tool.

Mirrors openai-agents' ``Agent.as_tool()`` pattern, adapted for ANILA's
HTTP-between-agents architecture. Use this when one agent should be able
to *consult* a specialist mid-turn rather than handing off control.

Compared to :class:`HandoffRequest`:

- **Handoff** = control transfer; the source agent's run ends, the
  target takes over the conversation. Runner / Router catches and
  dispatches.
- **Agent-as-tool** = synchronous sub-call; the source agent stays in
  charge, dispatches the target through CSP, blocks on the reply,
  and uses the result as a normal tool output.

Wired-up by the agent factory::

    from anila_core.tools.agent_as_tool import make_agent_tool

    registry.register(make_agent_tool(manifest, csp_base_url, csp_api_key))

Once registered, the LLM picks the tool by name and the schema. The
tool body forwards via :func:`dispatch_to_agent_response` and returns
the dispatched agent's reply text.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..context.agent_context import get_current_context
from ..models.tool import ToolDefinition, ToolSafety
from ..registry.remote_agent_manifest import RemoteAgentManifest
from .dispatch_tool import dispatch_to_agent_response

logger = logging.getLogger(__name__)


_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_tool_name(agent_id: str, prefix: str) -> str:
    """Build a tool name acceptable to OpenAI / Anthropic schemas.

    Both providers cap tool names at ~64 chars and require ASCII
    alphanumerics + underscore. Sanitise + truncate.
    """
    cleaned = _NAME_SANITIZE_RE.sub("_", agent_id).strip("_") or "agent"
    name = f"{prefix}{cleaned}" if prefix else cleaned
    return name[:64]


def make_agent_tool(
    manifest: RemoteAgentManifest,
    *,
    csp_base_url: str,
    csp_api_key: str,
    name_prefix: str = "consult_",
    custom_description: Optional[str] = None,
    timeout: float = 120.0,
    session_id: Optional[str] = None,
) -> ToolDefinition:
    """Wrap ``manifest`` as a callable :class:`ToolDefinition`.

    Args:
        manifest: The :class:`RemoteAgentManifest` to call.
        csp_base_url: Base URL of myCSPPlatform.
        csp_api_key: Caller's CSP key (forwarded as Bearer auth).
        name_prefix: Prepended to ``agent_id`` to form the tool name.
            Default ``consult_`` reads naturally to the LLM
            ("consult_drone_specialist"). Pass ``""`` for a bare name.
        custom_description: Override ``manifest.description_for_router``.
            Default uses the manifest field — "When to use the tool"
            from the LLM's perspective.
        timeout: Per-call HTTP timeout in seconds.
        session_id: Override session_id passed to the dispatched agent.
            When ``None`` (default), the tool body pulls
            :attr:`AgentContext.session_id` at call time, so the
            dispatched agent shares the source agent's session.

    Tool input schema is intentionally tiny — just ``query: str``.
    Multi-message context construction is the source agent's job (it
    can summarise into the query). Keeping the surface small avoids
    the LLM constructing malformed context payloads.
    """
    description = custom_description or (
        manifest.description_for_router
        or f"Forward a query to the '{manifest.agent_id}' specialist."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The question to ask. Frame it the way you'd phrase "
                    "it to a colleague who has no context on this "
                    "conversation — restate any background it needs."
                ),
            },
            "system_prompt": {
                "type": "string",
                "description": (
                    "Optional system message override for the dispatched "
                    "agent. Leave empty to use its default."
                ),
            },
        },
        "required": ["query"],
    }
    captured_session_id = session_id

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        query = str(input.get("query", "")).strip()
        if not query:
            return "agent-as-tool error: 'query' is required"
        sys_override = input.get("system_prompt")
        sys_prompt = (
            str(sys_override).strip() if sys_override else None
        ) or None
        sid = captured_session_id
        if sid is None:
            ctx = get_current_context()
            if ctx is not None and ctx.session_id:
                sid = ctx.session_id
        try:
            result = await dispatch_to_agent_response(
                agent_id=manifest.agent_id,
                query=query,
                csp_base_url=csp_base_url,
                csp_api_key=csp_api_key,
                stream=False,
                system_prompt=sys_prompt,
                timeout=timeout,
                session_id=sid,
            )
            return str(result.get("content", "") or "")
        except Exception as exc:
            logger.warning(
                "agent_as_tool[%s] dispatch failed: %s", manifest.agent_id, exc
            )
            return f"agent '{manifest.agent_id}' dispatch error: {exc}"

    return ToolDefinition(
        name=_safe_tool_name(manifest.agent_id, name_prefix),
        description=description,
        input_schema=input_schema,
        safety=ToolSafety.READ_ONLY,
        implementation=_impl,
    )


__all__ = ["make_agent_tool"]

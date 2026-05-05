"""User personalization layer — backend-agnostic Protocol + helpers.

AgenticRAG ships a Protocol that callers can implement to feed
per-request user context (long-term facts, preferences, role
metadata, …) into the agent's system prompt. The framework itself
does NOT know where these facts come from — they could be a remote
HTTP API, a local SQLite cache, a static dict for testing, or a
vector store keyed off ``request.headers``. Whatever shape your
backend takes, implement :class:`UserContextProvider` against it
and pass the instance into ``create_app(user_context_provider=...)``.

This decoupled design is intentional: AgenticRAG is a downloadable
agent template, not a platform-specific binding. The default
:class:`NoopUserContextProvider` makes the dependency optional —
forks that don't need personalization see no change in behaviour.

For wiring examples (HTTP-backed, static-fact, REST against an
identity service, ...) see ``docs/examples/memory.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from fastapi import Request


@dataclass(frozen=True)
class UserFact:
    """A single piece of long-term knowledge about the calling user.

    Backend-agnostic shape — implementations map their own
    representations (DB row, JSON object, RDF triple, …) to this
    triple of ``(key, value, confidence)``.

    ``confidence`` is informational; the framework's default
    formatter uses all facts unconditionally regardless of value.
    Custom formatters in user code may filter on it (e.g. drop
    facts with confidence < 0.7).
    """

    key: str
    value: str
    confidence: float = 1.0


@runtime_checkable
class UserContextProvider(Protocol):
    """Pluggable source of per-request user personalization data.

    Implement against your own backend (HTTP API, DB, in-memory
    cache, …) and pass the instance into ``create_app``. The chat
    handlers call ``get_user_facts(request)`` once per chat turn,
    before the agent's system prompt is finalised.

    Implementations MUST:

    * Be safe to call concurrently for distinct requests.
    * Never raise on a missing-user / unauthenticated lookup —
      return an empty list and let the framework continue without
      personalization. Raising would crash the chat for one bad
      request, which is rarely what you want.
    * Surface unrecoverable backend failures (DB down, identity
      service 5xx) by returning ``[]`` and logging — same
      degradation contract.
    """

    async def get_user_facts(self, request: Request) -> list[UserFact]:
        """Return facts to inject into this turn's system prompt."""
        ...


class NoopUserContextProvider:
    """Default provider — returns no facts.

    AgenticRAG installs this when ``create_app`` is called without
    a ``user_context_provider`` argument. With Noop in place, chat
    handlers behave identically to pre-personalization deployments
    — the dependency exists but is inert.
    """

    async def get_user_facts(self, request: Request) -> list[UserFact]:
        return []


def format_user_facts_block(facts: list[UserFact]) -> Optional[str]:
    """Render facts as a Markdown block for system-prompt prepending.

    Returns ``None`` when ``facts`` is empty so the caller can
    write ``return enriched or base_prompt`` without an extra
    branch. The format mirrors typical "## Known about user" blocks
    LLMs respond well to.

    Customise by writing your own formatter — this helper is a
    convenience, not a protocol method. Forks that want a different
    layout (JSON dump, YAML, structured tags) can ignore it.
    """
    if not facts:
        return None
    lines = ["## 使用者背景（已記住的事實）"]
    for f in facts:
        lines.append(f"- **{f.key}**: {f.value}")
    lines.append("")
    lines.append(
        "以上是平台對使用者的長期記憶，請參考但不要原文照抄；"
        "若記憶內容與本次對話矛盾，以本次對話為準。"
    )
    return "\n".join(lines)

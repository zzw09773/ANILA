"""Task notification XML protocol — coordinator ↔ worker status messages.

Wire format:

    <task-notification task-id="abc-123" status="completed" summary="found 3 docs">
    The detailed result body goes here. Can span multiple lines.
    </task-notification>

Used by:

- Workers (spawned sub-agents) to emit status back to the coordinator
  agent. Status appears in the coordinator's prompt as visible text;
  the coordinator agent decides how to respond.
- The Coordinator class to format final-result blobs when summarising
  completed work back to the user.

Why XML and not JSON: LLMs read both, but XML's tag-based delimiters
survive line-wrapping inside Markdown rendering, whereas JSON braces
get mauled by code-block formatting in the assistant's reasoning
text. Single-line attribute schema also matches what the legacy
``coordinator.coordinator`` module already emits, so AgenticRAG's
existing prompts continue to recognise notifications produced by
either path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# ── Wire format ───────────────────────────────────────────────────────


_TEMPLATE = (
    '<task-notification task-id="{task_id}" status="{status}" summary="{summary}">'
    "{result}"
    "</task-notification>"
)


_NOTIFICATION_RE = re.compile(
    r'<task-notification\s+task-id="([^"]+)"\s+status="([^"]+)"\s+summary="([^"]*)">'
    r"(.*?)</task-notification>",
    re.DOTALL,
)


# ── Statuses ─────────────────────────────────────────────────────────


# Restricted vocabulary so the coordinator agent can pattern-match
# reliably. New statuses should be added here, not invented ad-hoc by
# callers.
KNOWN_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "cancelled", "timeout"}
)


# ── Builder / parser ─────────────────────────────────────────────────


def build_task_notification(
    task_id: str,
    status: str,
    summary: str,
    result: str,
) -> str:
    """Render a notification XML string.

    ``status`` is not enforced against ``KNOWN_STATUSES`` so a custom
    state can pass through, but emit a logger.warning at the call site
    if you go off-vocabulary — the coordinator's parsing-side
    pattern-matching may not recognise it.

    ``summary`` and ``result`` are escaped for double-quote / angle-bracket
    safety. We do NOT do full XML entity escaping; the format is
    deliberately small.
    """
    return _TEMPLATE.format(
        task_id=_escape_attr(task_id),
        status=_escape_attr(status),
        summary=_escape_attr(summary),
        result=_escape_body(result),
    )


@dataclass(frozen=True)
class TaskNotification:
    """Parsed notification."""

    task_id: str
    status: str
    summary: str
    result: str


def parse_task_notification(text: str) -> TaskNotification | None:
    """Extract the FIRST notification from ``text``. ``None`` if absent.

    Useful for processing one worker's most-recent status. To collect
    all notifications in a longer text blob, use ``parse_all``.
    """
    match = _NOTIFICATION_RE.search(text)
    if not match:
        return None
    return TaskNotification(
        task_id=_unescape(match.group(1)),
        status=_unescape(match.group(2)),
        summary=_unescape(match.group(3)),
        result=_unescape(match.group(4).strip()),
    )


def parse_all(text: str) -> list[TaskNotification]:
    """Extract every notification in ``text`` in document order."""
    out: list[TaskNotification] = []
    for match in _NOTIFICATION_RE.finditer(text):
        out.append(
            TaskNotification(
                task_id=_unescape(match.group(1)),
                status=_unescape(match.group(2)),
                summary=_unescape(match.group(3)),
                result=_unescape(match.group(4).strip()),
            )
        )
    return out


def collect_for_summary(notifications: Iterable[TaskNotification]) -> str:
    """Join notifications back into one prompt-ready summary block.

    Useful when the coordinator wants to re-display all worker results
    to the LLM in one user message after gather_parallel().
    """
    return "\n\n".join(
        build_task_notification(n.task_id, n.status, n.summary, n.result)
        for n in notifications
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _escape_attr(value: str) -> str:
    """Escape characters that would break a quoted XML attribute value."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(
        ">", "&gt;"
    )


def _escape_body(value: str) -> str:
    """Escape the body so an embedded ``</task-notification>`` doesn't
    truncate the wrapper."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(value: str) -> str:
    """Reverse the escape — order matters; ``&amp;`` last."""
    return (
        value.replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


__all__ = [
    "KNOWN_STATUSES",
    "TaskNotification",
    "build_task_notification",
    "collect_for_summary",
    "parse_all",
    "parse_task_notification",
]

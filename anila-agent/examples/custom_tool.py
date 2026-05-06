"""Define a custom tool with `@anila_tool` and pass it via `extra_tools`.

Use this pattern for domain-specific tools (database lookups, internal APIs, etc.).
"""

from __future__ import annotations

import asyncio

from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner
from anila_agent.tools.base import anila_tool
from anila_agent.utils.config import load_config
from anila_agent.utils.logging import configure


@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -> int:
    """Return the headcount for a department.

    Args:
        department: Department name, case-insensitive.
    """
    fixture = {"engineering": 42, "design": 7, "sales": 13}
    return fixture.get(department.lower(), 0)


async def main() -> None:
    configure()
    config = load_config()
    assembled = build_agent(
        config,
        session_id="example-custom-tool",
        extra_tools=[employee_count],
    )
    runner = AnilaRunner(assembled, session_id="example-custom-tool")
    summary = await runner.send("How many people work in engineering?")
    print(summary.final_output)


if __name__ == "__main__":
    asyncio.run(main())

"""The Router process must not load the agent-SDK tools it never calls.

2026-09-02 harness assessment: ``import anila_core.api.router_server`` pulled
in ``tools.shell`` (subprocess), ``tools.files``, ``tools.apply_patch``, the
filesystem long-term-memory backend and the mock provider — ~5,000 lines the
Router never invokes — purely through eager ``__init__`` imports
(``tools/__init__.py`` re-exporting everything so that ``dispatch_tool``
could be imported). This pins the import footprint in a fresh interpreter.
Package-level names stay available lazily (PEP 562), so
``from anila_core.tools import make_agent_tool`` keeps working for the SDK.
"""

from __future__ import annotations

import json
import subprocess
import sys

MUST_NOT_LOAD = (
    "anila_core.tools.shell",
    "anila_core.tools.files",
    "anila_core.tools.apply_patch",
    "anila_core.tools.plan_mode",
    "anila_core.tools.todo_write",
    "anila_core.tools.ask_user",
    "anila_core.memory.long_term.backends.filesystem",
    "anila_core.providers.mock",
    "anila_core.providers.openai_compat",
)


def _loaded_after(stmt: str) -> set[str]:
    code = (
        "import sys, json\n"
        f"{stmt}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('anila_core'))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_router_import_does_not_load_agent_tools():
    loaded = _loaded_after("import anila_core.api.router_server")
    assert "anila_core.tools.dispatch_tool" in loaded  # the one tool the router does use
    offenders = sorted(m for m in MUST_NOT_LOAD if m in loaded)
    assert not offenders, f"router process loaded modules it never calls: {offenders}"


def test_sdk_names_still_importable_lazily():
    """Kill: dropping the PEP 562 ``__getattr__`` breaks the SDK surface."""
    code = (
        "from anila_core.tools import make_agent_tool, exec_bash_tool, all_file_tools\n"
        "from anila_core.memory import SqliteSession, MemdirManager\n"
        "from anila_core.providers import MockProvider, OpenAICompatProvider\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("ok")

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_top_level_import_does_not_load_service_frameworks() -> None:
    package_src = Path(__file__).parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(package_src)
    program = """
import json, sys
import anila_contracts
expected = {
    'Classification', 'StepEvent', 'AgentError', 'TaskContext',
    'TraceContext', 'InvocationCommand', 'SourceSnapshot', 'SafeSummary',
    'RouteDecision', 'PolicyGateResult', 'AgentManifest', 'ExecutionGrant',
}
assert set(anila_contracts.__all__) == expected, anila_contracts.__all__
for name in expected:
    assert hasattr(anila_contracts, name), name
for helper in (
    'ClassificationLevel', 'StepKind', 'StepStatus', 'AgentErrorCode',
    'STEP_EVENT_SCHEMA_VERSION', 'AGENT_ERROR_SCHEMA_VERSION',
    'SourceScope', 'SnapshotOrigin', 'InvocationTargetKind', 'AuthAssurance',
    'TASK_CONTEXT_SCHEMA_VERSION', 'TRACE_CONTEXT_SCHEMA_VERSION',
    'INVOCATION_COMMAND_SCHEMA_VERSION', 'SOURCE_SNAPSHOT_SCHEMA_VERSION',
    'SAFE_SUMMARY_SCHEMA_VERSION',
):
    assert not hasattr(anila_contracts, helper), helper
blocked = ('fastapi', 'asyncpg', 'pgvector', 'sse_starlette', 'anila_core')
print(json.dumps([name for name in blocked if name in sys.modules]))
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert json.loads(result.stdout) == []


def test_runtime_dependency_list_is_intentionally_thin() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pydantic>=2.7,<3.0"' in pyproject
    for dependency in ("fastapi", "asyncpg", "pgvector", "sse-starlette"):
        assert dependency not in pyproject.split("[project.optional-dependencies]", 1)[0].lower()

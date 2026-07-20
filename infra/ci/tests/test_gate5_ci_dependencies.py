from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "gate1-ci.yml"


def _run_commands(workflow: dict) -> Iterable[str]:
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            command = step.get("run") if isinstance(step, dict) else None
            if not isinstance(command, str):
                continue
            pending: str | None = None
            for line in command.splitlines():
                line = line.strip()
                if pending is None:
                    match = re.search(r"python -m pip install\b.*", line)
                    if not match:
                        continue
                    pending = match.group(0)
                else:
                    pending += f" {line}"
                if not pending.endswith("\\"):
                    yield pending.rstrip("\\ ")
                    pending = None
            if pending is not None:
                yield pending.rstrip("\\ ")


def _has_editable_package(command: str, path: str) -> bool:
    return bool(re.search(rf"-e\s+['\"]?{re.escape(path)}['\"]?", command))


class Gate5CiDependencyTests(unittest.TestCase):
    def test_governance_r3_runtime_packages_share_one_local_editable_install(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        required = (
            "./packages/anila-contracts",
            "./packages/anila-security",
            "./packages/anila-core[dev]",
        )
        commands = list(_run_commands(workflow))
        self.assertTrue(
            any(
                all(_has_editable_package(command, path) for path in required)
                for command in commands
            ),
            "governance must install the local R3 runtime packages in one pip invocation",
        )

    def test_csp_trace_test_runtime_packages_share_one_local_editable_install(self) -> None:
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        required = (
            "./packages/anila-contracts",
            "./packages/anila-security",
            "./packages/anila-core[rag]",
            "./packages/anila-agent",
        )
        commands = list(_run_commands(workflow))
        self.assertTrue(
            any(
                all(_has_editable_package(command, path) for path in required)
                for command in commands
            ),
            "CSP tests must install the local agent and runtime packages in one pip invocation",
        )

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]


def _editable_install_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if "pip install" in line and "-e" in line
    ]


def test_make_install_resolves_internal_contract_from_this_checkout():
    """The developer install must not let PyPI satisfy the internal name."""
    makefile = (PACKAGE_ROOT / "Makefile").read_text(encoding="utf-8")
    assert any(
        "../anila-contracts" in line
        and ".[dev]" in line
        and line.count("-e") >= 2
        for line in _editable_install_lines(makefile)
    )


def test_agent_metadata_keeps_contract_version_constraint():
    """Metadata remains explicit while Makefile controls source provenance."""
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"anila-contracts>=2.0.0,<3.0.0"' in pyproject


def test_ci_installs_contract_and_agent_in_one_local_resolver_invocation():
    """CI must not resolve the bare internal distribution from a public index."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "gate1-ci.yml").read_text(
        encoding="utf-8"
    )
    start = workflow.index("- name: Install agent")
    end = workflow.find("\n      - name:", start + 1)
    agent_step = workflow[start:] if end == -1 else workflow[start:end]
    install_lines = _editable_install_lines(agent_step)
    assert any(
        "packages/anila-contracts" in line
        and "packages/anila-agent[" in line
        and line.count("-e") >= 2
        for line in install_lines
    )


def test_docker_and_offline_builds_use_local_contract_sources():
    """Container and air-gap entry points must preserve source provenance too."""
    dockerfile = (PACKAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    docker_flat = " ".join(dockerfile.split())
    assert re.search(r"COPY packages/anila-contracts ./anila-contracts", docker_flat)
    assert re.search(r"RUN pip install ./anila-contracts", docker_flat)
    assert 'pip install "./anila-agent[' in docker_flat

    offline = (PACKAGE_ROOT / "offline" / "build-wheelhouse.sh").read_text(
        encoding="utf-8"
    )
    assert "anila-contracts @ file://$CONTRACTS_ROOT" in offline
    assert "anila-agent[$EXTRAS] @ file://$ROOT" in offline


def test_make_docker_build_uses_package_local_dockerfile_and_repo_context():
    """Running make from this package must not create a nested Docker path."""
    makefile = (PACKAGE_ROOT / "Makefile").read_text(encoding="utf-8")
    command = next(
        line.strip()
        for line in makefile.splitlines()
        if line.strip().startswith("docker build ")
    )
    assert "-f Dockerfile" in command
    assert "-f packages/anila-agent/Dockerfile" not in command
    assert command.endswith("../..")

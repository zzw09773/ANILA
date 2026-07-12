from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]


def test_runtime_dependency_surface_is_cryptography_only():
    metadata = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["dependencies"] == ["cryptography>=42"]


def test_package_import_is_independent_from_anila_core_and_csp():
    source = PACKAGE_ROOT / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "import anila_security; "
        "assert 'anila_core' not in sys.modules; "
        "assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_runtime_consumers_use_canonical_package_not_core_facade():
    roots = [
        REPO_ROOT / "services" / "csp" / "app",
        REPO_ROOT / "services" / "ingestion-worker" / "src",
    ]
    files = [path for root in roots for path in root.rglob("*.py")]
    files.append(REPO_ROOT / "infra" / "deployment" / "scripts" / "reencrypt-credentials.py")

    stale = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in files
        if "anila_core.security" in path.read_text(encoding="utf-8")
    ]
    assert stale == []
    assert any(
        "from anila_security" in path.read_text(encoding="utf-8") for path in files
    )


def test_consumers_declare_and_install_local_security_package_before_core():
    core = tomllib.loads(
        (REPO_ROOT / "packages" / "anila-core" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    worker = tomllib.loads(
        (REPO_ROOT / "services" / "ingestion-worker" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert "anila-security>=0.1.0,<0.2.0" in core["project"]["dependencies"]
    assert not any(
        dependency.startswith("cryptography") for dependency in core["project"]["dependencies"]
    )
    assert "anila-security>=0.1.0,<0.2.0" in worker["project"]["dependencies"]
    csp_requirements = (REPO_ROOT / "services" / "csp" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    requirement_lines = {
        line.strip()
        for line in csp_requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert not any(
        line.startswith(("anila-security", "anila-contracts", "anila-core"))
        for line in requirement_lines
    )

    dockerfiles = [
        REPO_ROOT / "infra" / "docker" / "csp.Dockerfile",
        REPO_ROOT / "services" / "ingestion-worker" / "Dockerfile",
        REPO_ROOT / "services" / "anila-core-router" / "Dockerfile",
    ]
    for dockerfile in dockerfiles:
        content = dockerfile.read_text(encoding="utf-8")
        security_copy = content.index("COPY packages/anila-security")
        core_copy = content.index("COPY packages/anila-core")
        assert security_copy < core_copy, dockerfile.relative_to(REPO_ROOT)

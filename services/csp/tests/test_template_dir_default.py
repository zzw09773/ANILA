"""The template-download fallback path must point at a directory that exists.

Compose no longer supplies a template-directory override, so a wrong built-in
default shows up as a 404 on every
developer's "download the agent template" click when csp is started any
other way (bare uvicorn, a demo box). The existing
``tests/test_template_download.py`` cannot catch it here: both of its cases
error at fixture setup in this environment, which is precisely why the
stale path survived the §17.1 directory move.

This module deliberately avoids the TestClient fixture so it runs
everywhere and is a pure statement about the path.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.api.agents.registration import _default_template_dir, _repo_root


def test_repo_root_is_the_repository_root():
    """Six ``.parent`` hops land on the repo root, not somewhere above it."""
    root = _repo_root()
    assert (root / "services" / "csp").is_dir()
    assert (root / "packages").is_dir()


def test_default_template_dir_exists():
    template_dir = _default_template_dir()
    assert template_dir.is_dir(), (
        f"template fallback {template_dir} does not exist — every "
        f"/api/agents/template/download would 404 without the built-in fallback"
    )


def test_default_template_dir_is_the_agent_template():
    """It must be the package compose actually mounts, contents and all."""
    template_dir = _default_template_dir()
    assert template_dir == _repo_root() / "packages" / "anila-agent"
    # Marker files: a downloaded zip is only useful if these are in it.
    assert (template_dir / "pyproject.toml").is_file()
    assert (template_dir / "README.md").is_file()

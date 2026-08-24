"""Refuse alembic when anila_core resolves from outside this repo."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.alembic_path_guard import (
    expected_anila_core_src,
    refuse_foreign_anila_core,
    repo_root_from_csp_app,
)


def test_this_tree_anila_core_is_accepted():
    expected = expected_anila_core_src()
    refuse_foreign_anila_core(str(expected / "anila_core" / "__init__.py"))


def test_foreign_anila_core_fails_before_any_revision():
    foreign = (
        "/home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA"
        "/packages/anila-core/src/anila_core/__init__.py"
    )
    with pytest.raises(RuntimeError, match="outside this repo") as exc:
        refuse_foreign_anila_core(foreign)
    msg = str(exc.value)
    assert "anila_security" in msg
    assert "packages/anila-core/src" in msg
    assert "BEFORE site-packages" in msg or "site-packages" in msg


def test_repo_root_is_this_tree():
    root = repo_root_from_csp_app()
    assert (root / "packages" / "anila-core" / "src").is_dir()
    assert root.name == "ANILA"
    assert "anila-restart-20260729" in str(root)

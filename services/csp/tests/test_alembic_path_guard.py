"""Refuse alembic when anila_core resolves from outside this *source* tree.

Image layout (/app/app/x.py, no packages/anila-core/src above us) must
skip — never IndexError. The skip must log why, or it is indistinguishable
from "the guard never ran".
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.alembic_path_guard import (
    _SKIP_INFO,
    expected_anila_core_src,
    refuse_foreign_anila_core,
    repo_root_from_csp_app,
    source_tree_anila_core_src,
)


def test_this_tree_anila_core_is_accepted():
    expected = expected_anila_core_src()
    assert expected is not None
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
    """Root is the checkout that contains this module.

    services/csp/app/alembic_path_guard.py is four levels down. One parent
    too far does not hold that file directly. The directory name is not
    part of the contract — a worktree checkout is a source tree too.
    """
    root = repo_root_from_csp_app()
    assert root is not None
    here = Path(repo_root_from_csp_app.__code__.co_filename).resolve()
    assert root == here.parents[3]
    assert (root / "packages" / "anila-core" / "src" / "anila_core").is_dir()
    assert (root / "services" / "csp" / "app" / "alembic_path_guard.py").is_file()


def test_image_layout_skips_without_indexerror(tmp_path, caplog):
    """Shipping image: /app/app/x.py — three parents, no repo, no IndexError."""
    fake = tmp_path / "app" / "app" / "alembic_path_guard.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("# image layout\n")
    assert source_tree_anila_core_src(fake) is None
    with caplog.at_level(logging.INFO, logger="app.alembic_path_guard"):
        refuse_foreign_anila_core(
            "/usr/local/lib/python3.13/site-packages/anila_core/__init__.py",
            start=fake,
        )
    assert _SKIP_INFO in caplog.text
    assert "site-packages" in caplog.text


def test_walk_finds_src_regardless_of_depth(tmp_path):
    """Criterion is the directory, not parent count (a later move must not break)."""
    src = tmp_path / "packages" / "anila-core" / "src" / "anila_core"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    deep = tmp_path / "a" / "b" / "c" / "d" / "file.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("")
    found = source_tree_anila_core_src(deep)
    assert found == (tmp_path / "packages" / "anila-core" / "src").resolve()

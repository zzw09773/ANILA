"""Tests for GET /api/agents/anila-verify/download (P2.1 tier-2 verifier export)."""

from __future__ import annotations

from pathlib import Path

import anila_core

from app.api.agents.registration import (
    _ANILA_VERIFY_SOURCE,
    _resolve_anila_verify_source,
)
from tests.conftest import login, make_user

_SAMPLE_VERIFY_SRC = b'''"""Standalone JWT verifier (test fixture)."""

def verify_token(token: str) -> dict:
    return {"sub": "fixture"}
'''


def test_anila_verify_source_resolves_inside_installed_package():
    """Pin: constant tracks the installed package, not a repo-root walk.

    The 200/503 cases below monkeypatch the constant for behaviour; without
    this assertion a wrong module-level path (e.g. ``/packages/...``) stays
    green forever while production always 503s.
    """
    expected = (
        Path(anila_core.__file__).resolve().parent / "contrib" / "anila_verify.py"
    )
    assert _ANILA_VERIFY_SOURCE == expected
    assert _ANILA_VERIFY_SOURCE == _resolve_anila_verify_source()
    package_dir = Path(anila_core.__file__).resolve().parent
    assert _ANILA_VERIFY_SOURCE.is_relative_to(package_dir)


def test_resolve_anila_verify_follows_installed_package_file(monkeypatch, tmp_path):
    """Resolver must track ``anila_core.__file__``, not the repo layout.

    Under pytest ``pythonpath``, ``_repo_root()/packages/...`` and the
    installed path coincide — so equality alone cannot kill a repo-walk
    mutant. Point ``__file__`` at a fake site-packages tree that is *not*
    under the worktree ``packages/`` prefix.
    """
    fake_init = tmp_path / "site-packages" / "anila_core" / "__init__.py"
    fake_init.parent.mkdir(parents=True)
    fake_init.write_text("# fake installed anila_core\n")
    monkeypatch.setattr(anila_core, "__file__", str(fake_init))

    got = _resolve_anila_verify_source()
    assert got == fake_init.resolve().parent / "contrib" / "anila_verify.py"
    assert "packages/anila-core/src" not in got.as_posix()
    assert got.is_relative_to((tmp_path / "site-packages" / "anila_core").resolve())


def test_developer_can_download_anila_verify_when_present(
    client, db, monkeypatch, tmp_path
):
    make_user(db, username="dev_anila_verify", role="developer")
    token = login(client, "dev_anila_verify")

    present = tmp_path / "anila_verify.py"
    present.write_bytes(_SAMPLE_VERIFY_SRC)
    monkeypatch.setattr(
        "app.api.agents.registration._ANILA_VERIFY_SOURCE",
        present,
    )

    resp = client.get(
        "/api/agents/anila-verify/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-python")
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename=anila_verify.py" in cd
    assert resp.content == _SAMPLE_VERIFY_SRC
    assert resp.content  # never an empty 200


def test_missing_anila_verify_returns_503(client, db, monkeypatch, tmp_path):
    make_user(db, username="dev_anila_verify_missing", role="developer")
    token = login(client, "dev_anila_verify_missing")

    missing = tmp_path / "does-not-exist.py"
    monkeypatch.setattr(
        "app.api.agents.registration._ANILA_VERIFY_SOURCE",
        missing,
    )

    resp = client.get(
        "/api/agents/anila-verify/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 503
    assert resp.status_code != 200
    detail = resp.json().get("detail", "")
    assert "驗證器" in detail
    assert resp.content  # error body present, not empty success


def test_plain_user_cannot_download_anila_verify(client, db):
    make_user(db, username="user_anila_verify", role="user")
    token = login(client, "user_anila_verify")

    resp = client.get(
        "/api/agents/anila-verify/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Same gate as /api/agents/platform-ca/download
    assert resp.status_code == 403


def test_unauthenticated_cannot_download_anila_verify(client):
    resp = client.get("/api/agents/anila-verify/download")
    assert resp.status_code == 401

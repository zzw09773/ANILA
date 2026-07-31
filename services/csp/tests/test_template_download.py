"""Tests for the official agent template download endpoint."""

from __future__ import annotations

import io
import zipfile

from tests.conftest import login, make_user


def test_developer_can_download_template(client, db):
    make_user(db, username="dev_template", role="developer")
    token = login(client, "dev_template")

    resp = client.get(
        "/api/agents/template/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "anila-core-template/README.md" in names
    assert "anila-core-template/pyproject.toml" in names
    # The zip must carry an actual runnable project, not just its docs.
    # This used to assert ``src/anila_core/api/router_server.py``, a path
    # that has not existed since the §17.1 directory move (b5c5e32e) and
    # that the mounted template never contained — the assertion only
    # survived because both cases in this module error at fixture setup in
    # some environments. It is deliberately written to be agnostic about
    # WHICH template ships (anila-agent vs anila-core is an open question
    # for the owner: the zip root, the endpoint docstring and the UI
    # filename all say anila-core, while compose mounts anila-agent), so
    # settling that question does not require touching this line again.
    assert any(n.endswith(".py") for n in names)


def test_plain_user_cannot_download_template(client, db):
    make_user(db, username="user_template", role="user")
    token = login(client, "user_template")

    resp = client.get(
        "/api/agents/template/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403

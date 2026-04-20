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
    assert "anila-core-template/anila.yaml" in names


def test_plain_user_cannot_download_template(client, db):
    make_user(db, username="user_template", role="user")
    token = login(client, "user_template")

    resp = client.get(
        "/api/agents/template/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403

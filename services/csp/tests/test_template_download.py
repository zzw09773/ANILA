"""Tests for the official agent template download endpoint."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.api.agents import registration

from tests.conftest import login, make_user


def test_template_override_is_safe_for_shallow_production_image_path(
    monkeypatch,
):
    monkeypatch.setenv("ANILA_TEMPLATE_DIR", "/app/anila-template")
    shallow = Path("/app/app/api/agents/registration.py")

    assert registration._resolve_template_dir(shallow) == Path(
        "/app/anila-template"
    )


def test_missing_source_checkout_falls_back_without_parent_indexing(monkeypatch):
    monkeypatch.delenv("ANILA_TEMPLATE_DIR", raising=False)
    shallow = Path("/opt/csp/registration.py")
    assert registration._resolve_template_dir(shallow) == Path(
        "/app/anila-template"
    )
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
    assert "anila-core-template/anila_agent/serving/service_wrapper.py" in names


def test_plain_user_cannot_download_template(client, db):
    make_user(db, username="user_template", role="user")
    token = login(client, "user_template")

    resp = client.get(
        "/api/agents/template/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403

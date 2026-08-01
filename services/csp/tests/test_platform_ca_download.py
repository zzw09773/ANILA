"""Tests for GET /api/agents/platform-ca/download (P2.1 governance CA export)."""

from __future__ import annotations

from tests.conftest import login, make_user


def test_developer_can_download_platform_ca(client, db):
    make_user(db, username="dev_platform_ca", role="developer")
    token = login(client, "dev_platform_ca")

    resp = client.get(
        "/api/agents/platform-ca/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-pem-file")
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename=anila-platform-ca.pem" in cd
    body = resp.content
    assert body  # never an empty 200
    assert b"BEGIN CERTIFICATE" in body
    assert b"PRIVATE KEY" not in body


def test_platform_ca_bytes_are_certs_only(client, db):
    """Pin: served material is public certificates, never a private key."""
    make_user(db, username="dev_platform_ca_bytes", role="developer")
    token = login(client, "dev_platform_ca_bytes")

    resp = client.get(
        "/api/agents/platform-ca/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    text = resp.content.decode("ascii", errors="replace")
    assert "-----BEGIN CERTIFICATE-----" in text
    assert "PRIVATE KEY" not in text
    assert "BEGIN RSA PRIVATE KEY" not in text
    assert "BEGIN EC PRIVATE KEY" not in text
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in text


def test_plain_user_cannot_download_platform_ca(client, db):
    make_user(db, username="user_platform_ca", role="user")
    token = login(client, "user_platform_ca")

    resp = client.get(
        "/api/agents/platform-ca/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    # Same gate as /api/agents/template/download
    assert resp.status_code == 403


def test_unauthenticated_cannot_download_platform_ca(client):
    resp = client.get("/api/agents/platform-ca/download")
    assert resp.status_code == 401


def test_missing_platform_ca_returns_503(client, db, monkeypatch, tmp_path):
    make_user(db, username="dev_platform_ca_missing", role="developer")
    token = login(client, "dev_platform_ca_missing")

    missing = tmp_path / "does-not-exist.pem"
    monkeypatch.setattr(
        "app.api.agents.registration._PLATFORM_CA_BUNDLE",
        missing,
    )

    resp = client.get(
        "/api/agents/platform-ca/download",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 503
    assert resp.status_code != 200
    detail = resp.json().get("detail", "")
    assert "平台 CA" in detail
    assert resp.content  # error body present, not empty success
